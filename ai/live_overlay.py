"""
ClassVision AI - Live Preview Overlay
--------------------------------------
Draws what the pipeline already worked out onto a copy of the frame, and
encodes it as JPEG for the dashboard.

Why the WHOLE frame
    A teacher watching the Live Classroom tab needs to see the room. A grid of
    face crops tells them nothing about who is at the back, whether the camera
    has drifted, or whether half the class is out of shot - which are exactly
    the things a person looks at a classroom to find out.

    So the preview is the complete camera frame, at the resolution the camera
    delivered, with boxes and labels drawn ON TOP. The inference pipeline goes
    on using aligned crops internally; that is its business and it never
    reaches the browser.

Why this adds no AI work
    Everything drawn here was already computed. The boxes come from the
    tracker, the names from recognition stability, the states from the
    liveness and authenticity managers. This module runs no detector, no
    embedder and no classifier - it draws rectangles and text, which is
    roughly a millisecond, and encodes a JPEG, which is a few more.

    If encoding ever became too expensive the honest fix is to publish every
    Nth frame, which PREVIEW_EVERY_N_FRAMES already allows, rather than to
    shrink or crop what the teacher can see.

Why nothing is stored
    The annotated frame exists as bytes in one slot of memory, is replaced by
    the next one, and is never written to disk, queued, or accumulated. The
    retention policy is the assignment: publishing frame N+1 drops frame N.

Colour is never the only signal
    Every box carries a text label, so the preview survives greyscale and
    colour blindness - the same rule the rest of the dashboard follows.
"""

import numpy as np

# Publish one preview out of every N processed frames. 1 means every frame.
# The camera runs at roughly 15 FPS on the development machine, so 1 is
# comfortable; this exists so a slower machine can halve the cost without
# touching anything a teacher sees.
PREVIEW_EVERY_N_FRAMES = 1

# JPEG quality. 70 is visually fine for a classroom overview at 640x480 and
# roughly a third the bytes of 95.
PREVIEW_JPEG_QUALITY = 70

# BGR, because OpenCV. Each pairs with a text label rather than replacing one.
COLOUR_CONFIRMED = (110, 200, 110)      # green  - recognised student
COLOUR_UNKNOWN = (150, 150, 150)        # grey   - tracked, not identified
COLOUR_SPOOF = (80, 80, 230)            # red    - liveness says presentation
COLOUR_UNCERTAIN = (70, 190, 240)       # amber  - not yet decided
COLOUR_TEXT = (255, 255, 255)
COLOUR_TEXT_SHADOW = (20, 20, 20)

BOX_THICKNESS = 2
FONT_SCALE = 0.48
FONT_THICKNESS = 1


def _cv2():
    """
    Imported lazily so this module can be reasoned about - and partly tested -
    without OpenCV present.
    """
    import cv2
    return cv2


def _colour_for(row):
    """
    One colour per row, chosen by the most important thing it has to say.

    Liveness first: a face the liveness layer is suspicious of matters more
    than whose face it is. Then identity. Then "tracked but unidentified",
    which is the ordinary state for anybody who has just walked in.
    """
    if row.get("liveness_state") == "SPOOF":
        return COLOUR_SPOOF
    if row.get("authenticity_state") == "SUSPICIOUS":
        return COLOUR_SPOOF
    if row.get("liveness_state") == "UNCERTAIN":
        return COLOUR_UNCERTAIN
    if row.get("student_id"):
        return COLOUR_CONFIRMED
    return COLOUR_UNKNOWN


def label_for(row):
    """
    The text drawn above a box.

    A track with no confirmed identity is labelled by its TRACK number, never
    by a guess. "Track 4" is a true statement about a person the system has
    not identified; a name would not be.
    """
    if row.get("student_id"):
        name = row.get("student_name") or row["student_id"]
        head = "%s (%s)" % (name, row["student_id"])
    else:
        head = "Track %s" % row.get("track_id", "?")

    marks = []

    liveness = row.get("liveness_state")
    if liveness and liveness != "UNKNOWN":
        marks.append(liveness)

    authenticity = row.get("authenticity_state")
    if authenticity and authenticity not in ("UNKNOWN", "AUTHENTIC"):
        marks.append(authenticity)

    return head + ("  [%s]" % " / ".join(marks) if marks else "")


def draw_overlay(frame, rows):
    """
    Return a NEW annotated frame. The original is never modified.

    Copying costs a memcpy of about 900 KB at 640x480, which is cheap, and it
    guarantees the pipeline is never handed a frame somebody has drawn on.
    """
    cv2 = _cv2()

    if frame is None:
        return None

    annotated = frame.copy()
    height, width = annotated.shape[:2]

    for row in rows or []:
        box = row.get("box")
        if not box:
            continue

        x, y, box_width, box_height = (int(value) for value in box)

        # Clamp to the frame. A tracker predicting a box slightly off the edge
        # is normal; drawing outside the array is not.
        left = max(0, min(x, width - 1))
        top = max(0, min(y, height - 1))
        right = max(0, min(x + box_width, width - 1))
        bottom = max(0, min(y + box_height, height - 1))

        if right <= left or bottom <= top:
            continue

        colour = _colour_for(row)
        cv2.rectangle(annotated, (left, top), (right, bottom), colour,
                      BOX_THICKNESS)

        text = label_for(row)
        (text_width, text_height), baseline = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, FONT_THICKNESS)

        # A filled strip behind the label, so a name stays readable against a
        # bright window or a white wall.
        strip_top = max(0, top - text_height - baseline - 4)
        cv2.rectangle(annotated, (left, strip_top),
                      (min(left + text_width + 8, width - 1), top),
                      colour, -1)

        cv2.putText(annotated, text, (left + 4, top - baseline - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, COLOUR_TEXT_SHADOW,
                    FONT_THICKNESS + 1, cv2.LINE_AA)
        cv2.putText(annotated, text, (left + 4, top - baseline - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, COLOUR_TEXT,
                    FONT_THICKNESS, cv2.LINE_AA)

    return annotated


def encode_jpeg(frame, quality=PREVIEW_JPEG_QUALITY):
    """
    JPEG bytes, or None.

    cv2.imencode is used rather than imwrite deliberately: it returns a buffer
    and touches no file, which is both the privacy property this project wants
    and the workaround for OpenCV's inability to handle the non-ASCII path
    this repository lives under.
    """
    cv2 = _cv2()

    if frame is None:
        return None

    ok, buffer = cv2.imencode(".jpg", frame,
                              [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return None

    return buffer.tobytes()


def render_preview(frame, rows, quality=PREVIEW_JPEG_QUALITY):
    """Annotate and encode in one step. Returns JPEG bytes, or None."""
    annotated = draw_overlay(frame, rows)
    if annotated is None:
        return None

    try:
        return encode_jpeg(annotated, quality)
    finally:
        # The annotated copy goes out of scope here whatever happened. Only
        # the encoded bytes travel any further.
        annotated = None


def placeholder(width=640, height=480, text="Camera not running"):
    """
    A frame to serve when there is nothing live to show.

    Better than a broken image icon, and better than serving the last real
    frame after the camera has been released - which would show a classroom
    that is no longer being watched.
    """
    cv2 = _cv2()

    frame = np.full((int(height), int(width), 3), 24, dtype=np.uint8)

    (text_width, text_height), _ = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 1)
    cv2.putText(frame, text,
                ((int(width) - text_width) // 2,
                 (int(height) + text_height) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (170, 170, 170), 1, cv2.LINE_AA)

    return encode_jpeg(frame)
