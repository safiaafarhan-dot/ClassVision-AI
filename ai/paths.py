"""
ClassVision AI - Project paths and console safety
-------------------------------------------------
Small shared helpers used by the scripts in ai/.

This project can live under a Windows path containing non-English characters
(for example a folder named "文档"). That causes two separate problems, and
both are handled here:

1. Writing files      - cv2.imwrite() fails silently on such paths.
                        See save_image() in camera_test.py for the fix.
2. Printing file paths - the Windows console uses the cp1252 encoding, which
                        cannot represent those characters, so a plain
                        print(path) raises UnicodeEncodeError and crashes the
                        script. enable_safe_console() fixes that.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AI_DIR = PROJECT_ROOT / "ai"
MODELS_DIR = PROJECT_ROOT / "models"
DATASETS_DIR = PROJECT_ROOT / "datasets"
DOCS_DIR = PROJECT_ROOT / "docs"


def enable_safe_console():
    """
    Stop the script from crashing when it prints an awkward character.

    Any character the console cannot display becomes "?" instead of raising
    UnicodeEncodeError. Call this once at the start of a script.
    """
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() exists on Python 3.7+. Guard anyway so this helper
        # can never be the thing that breaks a script.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def relative(path):
    """
    Show a path relative to the project root when possible.

    Printing "models/face.onnx" is clearer than printing the full path, and it
    avoids the non-English part of the path entirely.
    """
    path = Path(path)
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        # The path is outside the project, so show it in full.
        return str(path)
