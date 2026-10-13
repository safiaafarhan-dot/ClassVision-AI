"""
Classroom view tests - the full frame, drawn on, never stored.

NO WEBCAM IS NEEDED. Frames are generated arrays; what is under test is what
the overlay draws, what the endpoints serve, and what reaches disk.

WHAT THIS VERIFIES
    That the preview is the COMPLETE frame at the camera's own resolution and
    not a crop; that the overlay adds no inference; that a frame is never
    written anywhere; that the feed needs a session and an account; and that
    releasing the camera stops the picture.

WHAT THIS DOES NOT VERIFY
    That a real webcam produces a usable classroom image. That needs a
    classroom and remains a manual check.

Run with:
    python tests/test_classroom_view.py
"""

import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

import numpy as np  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from live_overlay import (  # noqa: E402
    PREVIEW_JPEG_QUALITY,
    draw_overlay,
    encode_jpeg,
    label_for,
    render_preview,
)
from live_session import LiveRuntime  # noqa: E402

from backend.services import live_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

WIDTH, HEIGHT = 640, 480


def a_frame(width=WIDTH, height=HEIGHT):
    """A frame with structure, so cropping would be detectable."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = 90
    return frame


def rows():
    return [
        {"box": (60, 50, 90, 110), "track_id": 1, "student_id": "STU_VIEW_1",
         "student_name": "One Student", "liveness_state": "LIVE",
         "authenticity_state": "UNKNOWN"},
        {"box": (400, 200, 100, 120), "track_id": 2, "student_id": None,
         "student_name": None, "liveness_state": "UNKNOWN",
         "authenticity_state": "UNKNOWN"},
    ]


def decode(jpeg):
    import cv2
    return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)


# -- the overlay ------------------------------------------------------------

def test_the_preview_is_the_whole_frame_not_a_crop():
    """
    The point of the whole feature. A face crop would be a different size.
    """
    frame = a_frame()
    jpeg = render_preview(frame, rows())

    decoded = decode(jpeg)
    assert decoded.shape[:2] == (HEIGHT, WIDTH), (
        "the preview is %s, not the camera's %dx%d frame"
        % (decoded.shape[:2], WIDTH, HEIGHT))

    # And the far corners are still the frame's own content, so nothing was
    # cropped away to make room for the boxes.
    for corner in ((5, 5), (5, WIDTH - 6), (HEIGHT - 6, 5),
                   (HEIGHT - 6, WIDTH - 6)):
        original = frame[corner[0], corner[1]].astype(int)
        shown = decoded[corner[0], corner[1]].astype(int)
        assert abs(original - shown).max() < 40, (
            "corner %s changed from %s to %s - the frame was cropped or "
            "replaced" % (corner, original.tolist(), shown.tolist()))

    print("PASS  the preview is the complete camera frame, corners intact")


def test_the_overlay_never_modifies_the_frame_it_was_given():
    """
    The pipeline must never be handed a frame somebody has drawn on.
    """
    frame = a_frame()
    before = frame.copy()

    annotated = draw_overlay(frame, rows())

    assert np.array_equal(frame, before), (
        "draw_overlay modified the caller's frame in place")
    assert not np.array_equal(annotated, before), (
        "draw_overlay drew nothing at all")

    print("PASS  the overlay draws on a copy and leaves the original alone")


def test_an_unidentified_track_is_labelled_by_track_never_by_a_guess():
    identified = label_for(rows()[0])
    unidentified = label_for(rows()[1])

    assert "One Student" in identified and "STU_VIEW_1" in identified
    assert "LIVE" in identified

    assert "Track 2" in unidentified, unidentified
    assert "STU" not in unidentified, (
        "an unidentified track was labelled with a student id: %r"
        % unidentified)

    print("PASS  an unconfirmed track is labelled Track N, never with a name")


def test_the_overlay_survives_boxes_that_fall_outside_the_frame():
    """A tracker predicting off-frame is normal. Drawing off-array is not."""
    frame = a_frame()

    awkward = [
        {"box": (-50, -50, 100, 100), "track_id": 1},
        {"box": (WIDTH - 10, HEIGHT - 10, 200, 200), "track_id": 2},
        {"box": (0, 0, 0, 0), "track_id": 3},
        {"box": (WIDTH + 100, HEIGHT + 100, 50, 50), "track_id": 4},
        {"track_id": 5},
    ]

    jpeg = render_preview(frame, awkward)
    assert jpeg, "a box outside the frame produced no preview at all"
    assert decode(jpeg).shape[:2] == (HEIGHT, WIDTH)

    print("PASS  boxes outside the frame are clamped, not crashed on")


def test_the_preview_is_a_real_jpeg_and_costs_no_inference():
    jpeg = encode_jpeg(a_frame(), PREVIEW_JPEG_QUALITY)

    assert jpeg[:3] == b"\xff\xd8\xff", "not a JPEG"
    assert len(jpeg) < 200_000, "a 640x480 preview should not be %d bytes" % len(jpeg)

    # The overlay module must not reach for a model, a detector or a store.
    source = (PROJECT_ROOT / "ai" / "live_overlay.py").read_text(
        encoding="utf-8")
    for forbidden in ("YuNet", "SFace", "face_detector", "face_embedding",
                      "embedding_store", "FaceRecognizer"):
        assert forbidden not in source, (
            "the overlay imports %s - it must draw what is already known, "
            "never compute it again" % forbidden)

    print("PASS  the preview is a JPEG and the overlay runs no model")


# -- the runtime slot -------------------------------------------------------

def test_the_preview_is_one_slot_and_is_dropped_with_the_camera():
    runtime = LiveRuntime()

    assert runtime.latest_frame()[0] is None

    runtime.publish_frame(b"first")
    runtime.publish_frame(b"second")

    jpeg, index, taken_at = runtime.latest_frame()
    assert jpeg == b"second", "an old frame survived a newer one"
    assert index == 2, "the frame counter did not advance"
    assert taken_at is not None

    runtime.mark_camera_released()
    assert runtime.latest_frame()[0] is None, (
        "the dashboard could still show a room nothing is watching")

    print("PASS  one slot only, newest wins, and releasing the camera clears "
          "it")


# -- the endpoints ----------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_view_"))
    state.configure(directory / "view.db")
    live_service.reset_live_manager(live_service.LiveSessionManager())

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    live_service.reset_live_manager(None)
    client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def test_the_feed_refuses_rather_than_serving_a_stale_room():
    client, directory = make_client()
    try:
        for url in ("/api/live/frame.jpg",
                    "/api/students/enrollment/frame.jpg"):
            response = client.get(url)
            assert response.status_code == 503, (
                "%s served something with no session running (%d)"
                % (url, response.status_code))
            assert b"\xff\xd8\xff" not in response.content

        print("PASS  with nothing running the feed is 503, never a stale "
              "picture")
    finally:
        teardown(client, directory)


def test_the_feed_serves_the_published_frame_and_says_not_to_cache_it():
    client, directory = make_client()
    try:
        jpeg = render_preview(a_frame(), rows())
        live_service.get_live_manager().runtime.publish_frame(jpeg)

        response = client.get("/api/live/frame.jpg")

        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == jpeg
        assert "no-store" in response.headers.get("cache-control", ""), (
            "a live classroom view is cacheable")

        print("PASS  the feed serves the current frame and forbids caching")
    finally:
        teardown(client, directory)


def test_the_feed_requires_an_account_once_accounts_exist():
    client, directory = make_client()
    try:
        from backend.services.auth_service import AuthService

        AuthService(state.get_database()).bootstrap_super_admin(
            "view-owner", "View Owner", "a-long-enough-passphrase")

        live_service.get_live_manager().runtime.publish_frame(
            render_preview(a_frame(), rows()))

        from backend.app import app
        anonymous = TestClient(app)

        for url in ("/api/live/frame.jpg", "/api/live/stream.mjpg",
                    "/api/students/enrollment/frame.jpg"):
            assert anonymous.get(url).status_code == 401, (
                "%s served a classroom view to a signed-out caller" % url)

        anonymous.close()
        print("PASS  the classroom view is behind the same sign-in as "
              "everything else")
    finally:
        teardown(client, directory)


# -- privacy ----------------------------------------------------------------

def test_showing_the_classroom_writes_nothing_to_disk():
    """
    Displaying video is not recording it. Nothing here may reach the disk.
    """
    data = PROJECT_ROOT / "data"
    before = {path: path.stat().st_mtime for path in data.rglob("*")
              if path.is_file()} if data.exists() else {}

    client, directory = make_client()
    try:
        manager = live_service.get_live_manager()
        for _ in range(25):
            manager.runtime.publish_frame(render_preview(a_frame(), rows()))
            client.get("/api/live/frame.jpg")
    finally:
        teardown(client, directory)

    after = {path: path.stat().st_mtime for path in data.rglob("*")
             if path.is_file()} if data.exists() else {}

    assert set(after) == set(before), (
        "serving the classroom view created %s" % (set(after) - set(before)))
    for path, stamp in before.items():
        assert after.get(path) == stamp, "%s was modified" % path

    # A sweep for stray media anywhere in the project, because a frame
    # escaping to disk would not necessarily land under data/.
    #
    # data/staff/ is excluded: it is where a teacher's personnel photograph
    # is deliberately stored, and an image being there is the feature working
    # rather than a leak. It is NOT unguarded - the before/after comparison
    # above walks the whole of data/, this directory included, so a frame
    # written there during these 25 requests still fails this test. What is
    # excluded is only the standing presence of files somebody uploaded on
    # purpose.
    media = [path for path in PROJECT_ROOT.rglob("*")
             if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4",
                                        ".avi", ".bmp")
             and ".venv" not in str(path) and "datasets" not in str(path)
             and "models" not in str(path)
             and "staff" not in path.parts]
    assert not media, "an image file appeared: %s" % media

    print("PASS  25 frames served, nothing written to disk")


def test_no_json_response_carries_a_frame():
    """
    The picture has exactly one door, and the JSON API is not it.
    """
    client, directory = make_client()
    try:
        live_service.get_live_manager().runtime.publish_frame(
            render_preview(a_frame(), rows()))

        for url in ("/api/live/status", "/api/live/presence"):
            body = client.get(url).text.lower()
            for forbidden in ("jpeg", "jpg", "base64", "data:image", "frame_"):
                assert forbidden not in body, (
                    "%s mentions %r" % (url, forbidden))

        schema = client.get("/openapi.json").json()
        for name, model in schema["components"]["schemas"].items():
            for field in model.get("properties", {}):
                assert "image" not in field.lower(), (
                    "%s declares an image field %r" % (name, field))

        print("PASS  no JSON response and no schema carries a picture")
    finally:
        teardown(client, directory)


def test_the_overlay_draws_no_biometric_value():
    """
    A box and a name. Never a distance, a score or a vector.
    """
    labelled = label_for({
        "track_id": 7, "student_id": "STU_VIEW_9",
        "student_name": "Named Student", "liveness_state": "LIVE",
        "authenticity_state": "AUTHENTIC",
    })

    for forbidden in ("0.", "similarity", "distance", "score", "embedding",
                      "cosine"):
        assert forbidden not in labelled.lower(), (
            "the overlay label leaks %r: %r" % (forbidden, labelled))

    print("PASS  an overlay label carries an identity and a state, no numbers")


# -- the dashboard ----------------------------------------------------------

HTML = (PROJECT_ROOT / "backend" / "static" / "index.html").read_text(
    encoding="utf-8")
JS = (PROJECT_ROOT / "backend" / "static" / "app.js").read_text(
    encoding="utf-8")
CSS = (PROJECT_ROOT / "backend" / "static" / "styles.css").read_text(
    encoding="utf-8")


def test_the_dashboard_shows_the_room_without_cropping_or_stretching():
    assert 'id="live-view"' in HTML, "there is no classroom view"
    assert 'id="enroll-view"' in HTML, "the enrollment dialog shows no camera"

    assert "object-fit: contain" in CSS, (
        "the classroom view does not use contain - cover would crop the edges "
        "of the room, which is the part a teacher is checking")
    assert "object-fit: cover" not in CSS, (
        "something crops the classroom view")

    assert "aspect-ratio" in CSS, (
        "the video container has no aspect ratio, so the frame can stretch")

    print("PASS  the dashboard shows the whole room, contained, undistorted")


def test_the_stream_is_opened_once_and_torn_down_when_it_stops():
    """
    Re-assigning src on every poll would rebuild the stream once a second,
    which looks exactly like a broken camera.
    """
    assert "dataset.streaming" in JS, (
        "nothing tracks whether the stream is already open")
    assert "removeAttribute" in JS, (
        "the stream is never torn down, so a generator stays open on the "
        "server for a tab nobody is watching")
    assert "showClassroomView" in JS and "showEnrollmentView" in JS

    print("PASS  the stream opens once and is torn down when the session ends")


def test_the_page_says_the_classroom_view_is_not_recorded():
    assert "Nothing is recorded" in HTML, (
        "the page does not tell a teacher that the live view is not stored")
    assert "complete camera frame" in HTML.lower()

    print("PASS  the page states that the classroom view is not recorded")


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_the_preview_is_the_whole_frame_not_a_crop,
        test_the_overlay_never_modifies_the_frame_it_was_given,
        test_an_unidentified_track_is_labelled_by_track_never_by_a_guess,
        test_the_overlay_survives_boxes_that_fall_outside_the_frame,
        test_the_preview_is_a_real_jpeg_and_costs_no_inference,
        test_the_preview_is_one_slot_and_is_dropped_with_the_camera,
        test_the_feed_refuses_rather_than_serving_a_stale_room,
        test_the_feed_serves_the_published_frame_and_says_not_to_cache_it,
        test_the_feed_requires_an_account_once_accounts_exist,
        test_showing_the_classroom_writes_nothing_to_disk,
        test_no_json_response_carries_a_frame,
        test_the_overlay_draws_no_biometric_value,
        test_the_dashboard_shows_the_room_without_cropping_or_stretching,
        test_the_stream_is_opened_once_and_torn_down_when_it_stops,
        test_the_page_says_the_classroom_view_is_not_recorded,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                       # noqa: BLE001
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__,
                                        error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
