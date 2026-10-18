
# ============================================================
# KNOWLEDGE GRAPH RAG PROJECT
# Step: Import extracted entities and relationships into Neo4j
# ============================================================

# STEP 1: Import the libraries we need.
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase


# ============================================================
# STEP 2: Load the Neo4j connection details from .env
# ============================================================

# Find the project folder containing this Python file.
PROJECT_FOLDER = Path(__file__).parent

# Load the environment variables from the project's .env file.
# This keeps passwords and API keys out of the Python code.
load_dotenv(PROJECT_FOLDER / ".env")

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

# Stop early if any required connection setting is missing.
if not all([NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD]):
    raise ValueError(
        "Neo4j settings are missing. Check NEO4J_URI, "
        "NEO4J_USERNAME, and NEO4J_PASSWORD in .env."
    )


# ============================================================
# STEP 3: Read the extracted JSON file
# ============================================================

# This file was created by your existing extract_entities.py.
INPUT_FILE = PROJECT_FOLDER / "extracted_graph_data.json"

if not INPUT_FILE.exists():
    raise FileNotFoundError(
        f"Extraction file not found: {INPUT_FILE}"
    )

# Open the JSON file using UTF-8 so Tamil Nadu scheme names
# and the rupee symbol are read correctly.
with INPUT_FILE.open("r", encoding="utf-8") as file:
    pages = json.load(file)

print("========== INPUT SUMMARY ==========")
print("Pages loaded:", len(pages))


# ============================================================
# STEP 4: Collect unique entities
# ============================================================

# The same scheme can appear on several PDF pages.
# We use its name and type as a key so repeated records
# normally become one node instead of many duplicate nodes.
#
# Amounts are also kept by name and type here. This is a
# starting point; identical amounts can have different meanings,
# so they may need a more specific identity later.

unique_entities = {}

for page in pages:
    page_number = page.get("page")
    source_file = page.get("source", "")

    for entity in page.get("entities", []):
        name = entity.get("name", "").strip()
        entity_type = entity.get("type", "Other").strip()

        # Ignore records that do not have a name.
        if not name:
            continue

        # Build a consistent identity key.
        entity_key = (
            name.casefold(),
            entity_type.casefold(),
        )

        # Create the entity record the first time we see it.
        if entity_key not in unique_entities:
            unique_entities[entity_key] = {
                "name": name,
                "type": entity_type,
                "details": entity.get("details", ""),
                "source_pages": [],
                "source_files": [],
            }

        stored_entity = unique_entities[entity_key]

        # Keep track of every page where this entity appears.
        if (
            page_number is not None
            and page_number not in stored_entity["source_pages"]
        ):
            stored_entity["source_pages"].append(page_number)

        # Preserve the PDF filename as source information.
        if (
            source_file
            and source_file not in stored_entity["source_files"]
        ):
            stored_entity["source_files"].append(source_file)


# ============================================================
# STEP 5: Prepare relationships
# ============================================================

# A relationship connects two entities.
# Example:
# Tamil Nadu Soil Fertility Mission
#       --[HAS_BUDGET]-->
# ₹122.51 crore
#
# We check that both endpoints appear among the entities
# extracted from that same page.

relationships = []
skipped_relationships = 0

for page in pages:
    # Build a set of entity names for this page.
    page_entity_names = {
        entity.get("name", "").strip().casefold()
        for entity in page.get("entities", [])
        if entity.get("name")
    }

    for relationship in page.get("relationships", []):
        source = relationship.get("source", "").strip()
        target = relationship.get("target", "").strip()
        relation_type = relationship.get("type", "").strip()

        # A relationship needs a source, a target, and a type.
        if not source or not target or not relation_type:
            skipped_relationships += 1
            continue

        # Skip relationships that point to entities not extracted
        # on this page. This prevents accidental broken links.
        if (
            source.casefold() not in page_entity_names
            or target.casefold() not in page_entity_names
        ):
            skipped_relationships += 1
            continue

        relationships.append({
            "source": source,
            "target": target,
            "type": relation_type,
            "details": relationship.get("details", ""),
            "page": page.get("page"),
            "source_file": page.get("source", ""),
        })


# ============================================================
# STEP 6: Display the import plan
# ============================================================

# These counts help us understand what will be imported.
print("\n========== IMPORT PLAN ==========")
print("Unique entity records:", len(unique_entities))
print("Relationships prepared:", len(relationships))
print("Relationships skipped:", skipped_relationships)

# Do not proceed if no usable entities were extracted.
if not unique_entities:
    raise ValueError("No valid entities found. Import cancelled.")


# ============================================================
# STEP 7: Define how entities are stored in Neo4j
# ============================================================

# Each entity will have the Neo4j label :Entity.
# Its name, type, details, and source pages are properties.
#
# MERGE means: find an existing node with this key, or create
# it if it does not exist.
#
# The key combines entity name and type to avoid merging
# different entity types that happen to share a name.

CREATE_ENTITY_QUERY = """
MERGE (n:Entity {key: $key})
SET n.name = $name,
    n.type = $type,
    n.details = $details,
    n.source_pages = $source_pages,
    n.source_files = $source_files
"""


# ============================================================
# STEP 8: Define how relationships are stored in Neo4j
# ============================================================

# We use the fixed relationship type :RELATED_TO.
# The extracted relationship name, such as HAS_BUDGET,
# is stored in the relationship's "type" property.
#
# This keeps the Cypher query simple and avoids constructing
# query text dynamically from model-generated relationship names.

CREATE_RELATIONSHIP_QUERY = """
MATCH (source:Entity {key: $source_key})
MATCH (target:Entity {key: $target_key})
MERGE (source)-[r:RELATED_TO {
    type: $relationship_type,
    page: $page
}]->(target)
SET r.details = $details,
    r.source_file = $source_file
"""


# ============================================================
# STEP 9: Build a reliable lookup for relationship endpoints
# ============================================================

# A relationship endpoint needs to resolve to the correct node.
# We use the case-insensitive entity name and type to identify it.
#
# If multiple entity types have the same name, the relationship
# endpoint in the current JSON does not tell us which type it
# means. In that case, we skip the ambiguous relationship rather
# than silently connecting it to the wrong node.

entities_by_name = {}

for entity_key, entity in unique_entities.items():
    name_key = entity["name"].casefold()

    entities_by_name.setdefault(name_key, []).append({
        "entity_key": entity_key,
        "entity": entity,
    })


# ============================================================
# STEP 10: Connect to Neo4j and import the graph
# ============================================================

# The driver manages the connection to AuraDB.
# The session sends Cypher queries to the database.
#
# This script does not delete existing nodes. MERGE helps
# make repeat runs safer, but it does not correct old data.

nodes_created_or_updated = 0
relationships_created_or_matched = 0
ambiguous_relationships = 0

with GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
) as driver:

    # Check the connection before writing anything.
    driver.verify_connectivity()
    print("\nConnected to Neo4j AuraDB.")

    with driver.session() as session:

        # STEP 10A: Import the entity nodes.
        for entity_key, entity in unique_entities.items():

            # Create a stable key for this entity.
            node_key = (
                entity["name"].casefold()
                + "|"
                + entity["type"].casefold()
            )

            session.execute_write(
                lambda tx, key=node_key, item=entity: tx.run(
                    CREATE_ENTITY_QUERY,
                    key=key,
                    name=item["name"],
                    type=item["type"],
                    details=item["details"],
                    source_pages=item["source_pages"],
                    source_files=item["source_files"],
                ).consume()
            )

            nodes_created_or_updated += 1

        print("Entity nodes imported:", nodes_created_or_updated)

        # STEP 10B: Import relationships between the nodes.
        for relationship in relationships:

            source_matches = entities_by_name.get(
                relationship["source"].casefold(), []
            )
            target_matches = entities_by_name.get(
                relationship["target"].casefold(), []
            )

            # The JSON provides endpoint names but not endpoint types.
            # If a name matches multiple types, skip it for safety.
            if len(source_matches) != 1 or len(target_matches) != 1:
                ambiguous_relationships += 1
                continue

            source_entity = source_matches[0]["entity"]
            target_entity = target_matches[0]["entity"]

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
                src=source_key, tgt=target_key: tx.run(
                    CREATE_RELATIONSHIP_QUERY,
                    source_key=src,
                    target_key=tgt,
                    relationship_type=rel["type"],
                    details=rel["details"],
                    page=rel["page"],
                    source_file=rel["source_file"],
                ).consume()
            )

            relationships_created_or_matched += 1

    print("\n========== IMPORT SUMMARY ==========")
    print("Entity records processed:", nodes_created_or_updated)
    print("Relationships processed:", relationships_created_or_matched)
    print("Ambiguous relationships skipped:", ambiguous_relationships)


# ============================================================
# STEP 11: Verify the saved graph
# ============================================================

# Reconnect and count nodes and relationships in Neo4j.
# These queries only read data; they do not modify the graph.

with GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
) as driver:

    with driver.session() as session:

        node_count = session.run(
            "MATCH (n:Entity) RETURN count(n) AS total"
        ).single()["total"]

        relationship_count = session.run(
            """
            MATCH (:Entity)-[r:RELATED_TO]->(:Entity)
            RETURN count(r) AS total
            """
        ).single()["total"]

        print("\n========== NEO4J VERIFICATION ==========")
        print("Total Entity nodes:", node_count)
        print("Total RELATED_TO relationships:", relationship_count)

print("\nKnowledge graph import finished.")
