# src/ingestion.py

import pypdfium2
import pytesseract
from langchain.text_splitter import RecursiveCharacterTextSplitter
from src.config import Config

pytesseract.pytesseract.tesseract_cmd = Config.TESSERACT_PATH

def pdf_to_text(pdf_path: str) -> str:
    '''
    converts PDF to images, then ocr-s them to text
    '''
    try:
        pdf_document = pypdfium2.PdfDocument(pdf_path)
        texts = []

        print(f"Processing {len(pdf_document)} pages in {pdf_path}...")

        for i, page in enumerate(pdf_document):
            # render page to image
            pil_image = page.render().to_pil()
            # extract text
            text = pytesseract.image_to_string(pil_image)
            texts.append(text)

        return "\n".join(texts)
    except Exception as e:
        raise RuntimeError(f"Failed to process PDF: {e}")

def get_text_chunks(text: str, chunk_size = 1600, chunk_overlap = 100) -> list[str]:
    '''
    splits text recursively
    '''
    splitter = RecursiveCharacterTextSplitter(
        chunk_size = chunk_size,
        chunk_overlap = chunk_overlap,
        length_function = len
    )

    return splitter.split_text(text)
