mitya/
├── .env
├── requirements.txt
├── data/
│   ├── raw/                   # raw pdfs
│   ├── processed/             # generated csvs
│   └── vector_db/             # rag embeddings
├── src
│   ├── __init__.py
│   ├── config.py              # config loader
│   ├── ingestion.py           # pdf -> text
│   ├── generation.py          # text -> prompt
│   └── rag.py                 # vector store & retrieval
├── scripts/
│   ├── build_dataset.py       # create dataset
│   └── push_model.py          # push to Ollama
└── app.py                     # ui
