"""Downloads the htdemucs ONNX model used by the Demixr app (same file/URL)."""
import os
import sys
import urllib.request

URL = "https://huggingface.co/StemSplitio/htdemucs-onnx/resolve/main/htdemucs_fp16weights.onnx"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "htdemucs_fp16weights.onnx")


def main():
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    if os.path.exists(DEST):
        print("Model already downloaded:", DEST)
        return
    tmp = DEST + ".part"

    def hook(blocks, size, total):
        if total > 0:
            sys.stdout.write(f"\r{min(100, blocks * size * 100 // total)}%  ")
            sys.stdout.flush()

    urllib.request.urlretrieve(URL, tmp, hook)
    os.replace(tmp, DEST)
    print("\nDone:", DEST)


if __name__ == "__main__":
    main()
