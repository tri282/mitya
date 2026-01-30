# src/config.py

import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    TESSERACT_PATH = os.getenv("TESSERACT_PATH", "/usr/bin/tesseract")
    MODEL_NAME = os.getenv("MODEL_NAME", "gemini-1.5-flash")
    LLM_BACKEND = os.getenv("LLM_BACKEND", "gemini") # 'gemini' or 'ollama'

    # paths
    DATA_DIR = os.path.join(os.getcwd(), "data")
    RAW_DATA_DIR = os.path.join(DATA_DIR, "raw")
    PROCESSED_DATA_DIR = os.path.join(DATA_DIR, "processed")

# validation
if not Config.GOOGLE_API_KEY and Config.LLM_BACKEND == 'gemini':
    print("WARNING: GOOGLE_API_KEY is missing in .env")
