
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase


# --------------------------------------------------
# 1. Load the Neo4j connection settings from .env
# --------------------------------------------------

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

if not all([NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD]):
    raise ValueError("Please check the Neo4j settings in your .env file.")


# --------------------------------------------------
# 2. Load the latest extracted data
# --------------------------------------------------

input_file = Path(__file__).parent / "extracted_graph_data.json"

with input_file.open("r", encoding="utf-8") as file:
    pages = json.load(file)

# Each page contains entities and relationships.
# We will merge repeated entities into one graph node.
print("Pages loaded:", len(pages))


# --------------------------------------------------
# 3. Prepare unique entities and relationships
# --------------------------------------------------

# A key of (entity name, entity type) avoids creating
# duplicate nodes for repeated occurrences of a scheme.
unique_entities = {}

for page in pages:
    page_number = page.get("page")
    source_file = page.get("source", "")

    for entity in page.get("entities", []):
        name = entity.get("name", "").strip()
        entity_type = entity.get("type", "Other")

        if not name:
            continue

        key = (name.casefold(), entity_type)

        if key not in unique_entities:
            unique_entities[key] = {
                "name": name,
                "type": entity_type,
                "details": entity.get("details", ""),
                "source_pages": [],
                "source_files": [],
            }

        # Preserve the page numbers where this entity appears.
        if page_number is not None:
            if page_number not in unique_entities[key]["source_pages"]:
                unique_entities[key]["source_pages"].append(page_number)

        if source_file:
            if source_file not in unique_entities[key]["source_files"]:
                unique_entities[key]["source_files"].append(source_file)


# Keep relationships together with the page where they were extracted.
relationships = []

for page in pages:
    page_entities = {
        entity.get("name", "").strip().casefold()
        for entity in page.get("entities", [])
        if entity.get("name")
    }

    for relationship in page.get("relationships", []):
        source = relationship.get("source", "").strip()
        target = relationship.get("target", "").strip()
        relation_type = relationship.get("type", "").strip()

        # Skip incomplete relationships.
        if not source or not target or not relation_type:
            continue

        # Skip relationships whose endpoints were not extracted
        # as entities on this page.
        if (
            source.casefold() not in page_entities
            or target.casefold() not in page_entities
        ):
            print("Skipped relationship with missing endpoint:", relationship)
            continue

        relationships.append({
            "source": source,
            "target": target,
            "type": relation_type,
            "details": relationship.get("details", ""),
            "page": page.get("page"),
            "source_file": page.get("source", ""),
        })


# --------------------------------------------------
# 4. Show a summary before writing to Neo4j
# --------------------------------------------------

print("Unique entity nodes prepared:", len(unique_entities))
print("Relationships prepared:", len(relationships))

# Stop if the input appears empty.
if not unique_entities:
    raise ValueError("No entities were found. Nothing will be imported.")


# --------------------------------------------------
# 5. Define safe, fixed Cypher queries
# --------------------------------------------------

# Entity types are stored as a property rather than used
# as dynamic Neo4j labels. This keeps the query simple.
create_node_query = """
MERGE (n:Entity {key: $key})
SET n.name = $name,
    n.type = $type,
    n.details = $details,
    n.source_pages = $source_pages,
    n.source_files = $source_files
"""

# Relationship types are stored as properties on a generic
# RELATED_TO relationship. This avoids dynamic query strings.
create_relationship_query = """
MATCH (source:Entity {key: $source_key})
MATCH (target:Entity {key: $target_key})
MERGE (source)-[r:RELATED_TO {
    type: $type,
    page: $page
}]->(target)
SET r.details = $details,
    r.source_file = $source_file
"""


# --------------------------------------------------
# 6. Connect and import using Neo4j transactions
# --------------------------------------------------

with GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
) as driver:

    # Confirm that the database is reachable.
    driver.verify_connectivity()
    print("\nConnected to Neo4j AuraDB.")

    with driver.session() as session:

        # Import each unique entity.
        for entity in unique_entities.values():
            # The key uniquely identifies an entity by name and type.
            key = (
                entity["name"].strip().casefold()
                + "|"
                + entity["type"].strip().casefold()
            )

            session.execute_write(
                lambda tx, key=key, entity=entity: tx.run(
                    create_node_query,
                    key=key,
                    name=entity["name"],
                    type=entity["type"],
                    details=entity["details"],
                    source_pages=entity["source_pages"],
                    source_files=entity["source_files"],
                ).consume()
            )

        # Create the extracted relationships.
        for relationship in relationships:
            source_name = relationship["source"].strip()
            target_name = relationship["target"].strip()

            # Find the matching entity keys.
            source_entity = next(
                (
                    entity for entity in unique_entities.values()
                    if entity["name"].casefold() == source_name.casefold()
                ),
                None,
            )

            target_entity = next(
                (
                    entity for entity in unique_entities.values()
                    if entity["name"].casefold() == target_name.casefold()
                ),
                None,
            )

            # Skip if either endpoint could not be identified.
            if source_entity is None or target_entity is None:
                print("Skipped unresolved relationship:", relationship)
                continue

            source_key = (
                source_entity["name"].casefold()
                + "|"
                + source_entity["type"].casefold()
            )

            target_key = (
                target_entity["name"].casefold()
                + "|"
                + target_entity["type"].casefold()
            )

            session.execute_write(
                lambda tx, rel=relationship,
                source_key=source_key, target_key=target_key: tx.run(
                    create_relationship_query,
                    source_key=source_key,
                    target_key=target_key,
                    type=rel["type"],
                    details=rel["details"],
                    page=rel["page"],
                    source_file=rel["source_file"],
                ).consume()
            )

    print("\nImport completed successfully.")


# --------------------------------------------------
# 7. Print a final summary from Neo4j
# --------------------------------------------------

with GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
) as driver:
    with driver.session() as session:
        node_count = session.run(
            "MATCH (n:Entity) RETURN count(n) AS total"
        ).single()["total"]

        relationship_count = session.run(
            "MATCH (:Entity)-[r:RELATED_TO]->(:Entity) "
            "RETURN count(r) AS total"
        ).single()["total"]

        print("Nodes in Neo4j:", node_count)
        print("Relationships in Neo4j:", relationship_count)
