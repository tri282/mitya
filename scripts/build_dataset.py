# scripts/build_dataset.py

import sys
import os
import pandas as pd
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.ingestion import pdf_to_text, get_text_chunks
from src.generation import Generator
from src.config import Config

def main(pdf_filename, output_csv_name):
    pdf_path = os.path.join(Config.RAW_DATA_DIR, pdf_filename)
    csv_path = os.path.join(Config.PROCESSED_DATA_DIR, output_csv_name)

    print(f"--- Starting Pipeline ---")
    print(f"Input: {pdf_path}")
    print(f"Output: {csv_path}")

    # ingestion
    print("Extracting text from PDF (OCR)...")
    raw_text = pdf_to_text(pdf_path)
    chunks = get_text_chunks(raw_text)
    print(f"Generated {len(chunks)} text chunks.")

    # generation
    generator = LLMGenerator()
    data_rows = []

    print("Generating Q&A pairs...")
    for i, chunk in enumerate(chunks):
        print(f"Processing chunk {i+1}/{len(chunks)}...")
        question, answer = generator.generate_qa_pair(chunk)

        if question and answer:
            # append to csv
            df = pd.DataFrame({'input': [question], 'output': [answer]})
            mode = 'a' if os.path.exists(csv_path) else 'w'
            header = False if os.path.exists(csv_path) else True
            df.to_csv(csv_path, mode = mode, header = header, index = False)

    print("Done!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required = True, help = "Name of PDF in data/raw/")
    parser.add_argument("--out", default="training_data.csv", help = "Name of output CSV")
    args = parser.parse_args()

    main(args.pdf, args.out)
