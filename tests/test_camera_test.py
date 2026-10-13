"""
Smoke tests for ai/camera_test.py (Step 1).

These do NOT need a webcam. They check that the module imports and that the
helper functions behave on a fake frame, so a broken file is caught early.

Run with:
    python tests/test_camera_test.py
"""

import sys
from pathlib import Path

import numpy as np

# Make the ai/ folder importable when running this file directly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

import camera_test  # noqa: E402
from paths import enable_safe_console  # noqa: E402


def make_fake_frame():
    """A plain black 480x640 colour image, the same shape a webcam gives us."""
    return np.zeros((480, 640, 3), dtype=np.uint8)


def test_module_has_expected_functions():
    for name in ("list_cameras", "open_camera", "draw_info_panel",
                 "save_snapshot", "run_camera_test", "main"):
        assert hasattr(camera_test, name), "missing function: " + name
    print("PASS  module exposes all expected functions")


def test_window_title():
    assert camera_test.WINDOW_TITLE == "ClassVision AI - Camera Test"
    print("PASS  window title is correct")


def test_draw_info_panel_changes_the_frame():
    frame = make_fake_frame()
    camera_test.draw_info_panel(frame, ["line one", "line two"])
    # The panel writes pixels, so the frame can no longer be all black.
    assert frame.sum() > 0, "info panel drew nothing"
    assert frame.shape == (480, 640, 3), "info panel changed the frame size"
    print("PASS  draw_info_panel writes onto the frame")


def test_save_snapshot_writes_a_file():
    path = camera_test.save_snapshot(make_fake_frame(), 999)
    try:
        assert path.exists(), "snapshot file was not created"
        assert path.stat().st_size > 0, "snapshot file is empty"
        print("PASS  save_snapshot wrote " + path.name)
    finally:
        path.unlink(missing_ok=True)


def main():
    enable_safe_console()
    tests = [
        test_module_has_expected_functions,
        test_window_title,
        test_draw_info_panel_changes_the_frame,
        test_save_snapshot_writes_a_file,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
