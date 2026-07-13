# scripts/train_model.py

import sys
import os
import argparse

from src.config import Config

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.training import MityaTrainer
from src.config import Config

def main(csv_file):
    csv_path = os.path.join(Config.PROCESSED_DATA_DIR, csv_file)

    trainer = MityaTrainer()

    # load data
    dataset = trainer.prepare_data(csv_path)

    # train
    trainer.train(dataset)

    # export
    save_path = os.path.join(Config.DATA_DIR, "mitya-2.0")
    trainer.export_model(save_path)

    print("Training pipeline complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default = "training_data.csv", help = "Name of CSV in data/processed/")

    args = parser.parse_args()
    main(args.csv)
