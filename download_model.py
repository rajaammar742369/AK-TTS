"""Download the voice-cloning model once:  python download_model.py"""
import sys

import cloning

if cloning.model_ready():
    print("Model already present in", cloning.MODEL_DIR)
    sys.exit(0)


def show(done, total):
    mb = done / 1048576
    print(f"\r  {mb:7.1f} MB" + (f" / {total / 1048576:.1f} MB" if total else ""), end="", flush=True)


try:
    cloning.download_model(show)
    print("\nDone:", cloning.MODEL_DIR)
except cloning.CloneUnavailable as exc:
    print("\n" + str(exc))
    sys.exit(1)
