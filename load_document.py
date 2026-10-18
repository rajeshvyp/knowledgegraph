
from pathlib import Path
from pypdf import PdfReader
from langchain_core.documents import Document

# Locate the new agriculture schemes PDF
pdf_path = Path("data") / "AgricultureScheme Full.pdf"

if not pdf_path.exists():
    raise FileNotFoundError(
        f"PDF not found: {pdf_path.resolve()}"
    )

# Read the PDF
reader = PdfReader(str(pdf_path))
documents = []

for page_number, page in enumerate(reader.pages, start=1):
    text = page.extract_text() or ""

    if text.strip():
        documents.append(
            Document(
                page_content=text,
                metadata={
                    "source": pdf_path.name,
                    "page": page_number,
                },
            )
        )

print("PDF:", pdf_path.name)
print("Total PDF pages:", len(reader.pages))
print("Documents created:", len(documents))

if documents:
    print("\nFirst document metadata:")
    print(documents[0].metadata)

    print("\nFirst page preview:")
    print(documents[0].page_content[:1000])
