"""
ClassVision AI - Model Downloader (Step 2)
------------------------------------------
Downloads the models this project needs into models/.

    YuNet  (Step 2) - finds WHERE faces are in a frame.
    SFace  (Step 3) - turns one aligned face into an embedding vector.

Both are official OpenCV model-zoo models and both are verified by SHA-256
before being saved, so a corrupted or substituted download is never used.

models/ is gitignored, so the file is never committed. Run this once after
cloning the project:

    python ai/download_models.py

Re-running is cheap: an already-correct file is verified and skipped.
"""

import hashlib
import sys
import urllib.request

from paths import MODELS_DIR, enable_safe_console, relative

# YuNet face detector, from the official OpenCV model zoo.
#
# The zoo stores models with Git LFS, so the normal raw.githubusercontent.com
# URL returns a small text pointer instead of the model. The media.* host
# serves the real file.
YUNET = {
    "name": "face_detection_yunet_2023mar.onnx",
    "url": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "size": 232589,
    "step": 2,
    "purpose": "finds WHERE faces are",
}

# SFace face recogniser, also from the OpenCV model zoo. Step 3 uses it to turn
# an aligned face into an embedding (a numeric vector). It is the recogniser
# designed to pair with YuNet, so the two work together without extra glue.
SFACE = {
    "name": "face_recognition_sface_2021dec.onnx",
    "url": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
    "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    "size": 38696353,
    "step": 3,
    "purpose": "turns an aligned face into an embedding",
}

ALL_MODELS = [YUNET, SFACE]


def sha256_of(path):
    """Return the SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_already_correct(path, expected_sha256):
    """True if the file exists and its contents match the expected hash."""
    return path.exists() and sha256_of(path) == expected_sha256


def download(model):
    """Download one model and verify it. Returns True on success."""
    path = MODELS_DIR / model["name"]

    if is_already_correct(path, model["sha256"]):
        print("Already downloaded and verified: %s" % model["name"])
        return True

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading %s (%.0f KB)..." % (model["name"], model["size"] / 1024))

    try:
        with urllib.request.urlopen(model["url"], timeout=60) as response:
            data = response.read()
    except Exception as error:
        print("ERROR: download failed: %s: %s" % (type(error).__name__, error))
        print("Check your internet connection and try again.")
        return False

    actual = hashlib.sha256(data).hexdigest()
    if actual != model["sha256"]:
        # A wrong hash usually means we got an error page or an LFS pointer,
        # not the model. Never write a file we cannot trust.
        print("ERROR: checksum mismatch - refusing to save the file.")
        print("  expected %s" % model["sha256"])
        print("  actual   %s" % actual)
        print("  bytes received: %d" % len(data))
        return False

    # write_bytes() handles non-English paths correctly, which matters because
    # this project can live under a folder like "文档".
    path.write_bytes(data)
    print("Saved and verified: %s" % relative(path))
    return True


def model_path(name=None):
    """Where a model file lives. Used by the detector to find its weights."""
    return MODELS_DIR / (name or YUNET["name"])


def main():
    enable_safe_console()
    print("ClassVision AI - downloading models\n")

    failed = []
    for model in ALL_MODELS:
        print("Step %d model - %s" % (model["step"], model["purpose"]))
        if not download(model):
            failed.append(model["name"])
        print("")

    if failed:
        print("Download failed for: %s" % ", ".join(failed))
        print("Those features cannot run until the download succeeds.")
        return 1

    print("All models ready. You can now run:")
    print("    python ai/face_detection_test.py     (Step 2)")
    print("    python ai/enroll_student.py          (Step 3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
