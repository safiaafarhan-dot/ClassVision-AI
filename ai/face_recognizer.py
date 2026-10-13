"""
ClassVision AI - Face Recognition (Step 4)
------------------------------------------
Answers one question per detected face:

    "Does this face match one of the enrolled students?"

    Step 4 performs identity verification only. It does not mark attendance
    or calculate presence duration.

How a face is matched
    detected face -> align (Step 3) -> 128-number embedding (Step 3)
                  -> compare against every stored embedding
                  -> keep the BEST score per student
                  -> MATCH if that score clears the threshold, else UNKNOWN

Best-of-five
    Each student was enrolled with five embeddings covering slightly different
    poses. A live face is compared against all of them and the student's score
    is the HIGHEST of the five - the closest pose wins. Averaging the five
    would blur the poses together and match every one of them slightly worse.

UNKNOWN is a real answer
    The recogniser never forces a face onto the nearest student. If the best
    score does not clear the threshold, the result is UNKNOWN. A system that
    always returns its closest guess would confidently mislabel every visitor,
    parent and stranger who walked past the camera.

Privacy
    Results carry a student id, a display name, a score and a timestamp.
    They never carry an embedding, and no frame or face crop is ever saved.
"""

import os
import time

import numpy as np

from embedding_store import EmbeddingStore
from face_alignment import align_face
from face_embedding import EMBEDDING_DIM, SFaceEmbedder

# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING THE NUMBER.
#
# The model's authors calibrated SFace on a standard face-verification
# benchmark and published a cosine threshold of 0.363. That is their
# measurement, on their data - not ours.
#
# Step 3 measured same-face similarities of 0.93+, but those pairs were
# ALTERED COPIES OF ONE PHOTO (rotated, brightened, blurred). Two genuinely
# different captures of the same person - different day, light, expression -
# score far lower. Setting the threshold near 0.93 would therefore reject
# almost every real match. It is the wrong number for this job.
#
# This project has NOT been able to validate a threshold on real data: the
# available public sample images contain only one usable identity, which is
# not enough to measure genuine-versus-impostor behaviour. See
# ai/evaluate_threshold.py, which exists so a real threshold can be measured
# once real enrollment data is available.
#
# Until then 0.45 is used: above the authors' 0.363, because wrongly accepting
# a face is worse here than asking someone to try again.
#
# This is a PROVISIONAL DEVELOPMENT THRESHOLD, not a validated production one.
DEFAULT_RECOGNITION_THRESHOLD = 0.45

# The model authors' published operating point, kept for reference.
UPSTREAM_SFACE_COSINE_THRESHOLD = 0.363

# Lets the threshold be changed without editing code.
THRESHOLD_ENV_VAR = "CLASSVISION_RECOGNITION_THRESHOLD"

# Result statuses.
STATUS_MATCH = "MATCH"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NO_ENROLLMENTS = "NO_ENROLLMENTS"


def configured_threshold(explicit=None):
    """
    Work out which threshold to use.

    Order of preference: an explicit value, then the environment variable,
    then the provisional default.
    """
    if explicit is not None:
        return validate_threshold(explicit)

    from_environment = os.environ.get(THRESHOLD_ENV_VAR)
    if from_environment:
        try:
            return validate_threshold(float(from_environment))
        except ValueError as error:
            raise ValueError(
                "%s is set to %r, which is not a usable threshold: %s"
                % (THRESHOLD_ENV_VAR, from_environment, error))

    return DEFAULT_RECOGNITION_THRESHOLD


def validate_threshold(value):
    """A cosine threshold only makes sense between -1 and 1."""
    number = float(value)
    if not (-1.0 <= number <= 1.0):
        raise ValueError(
            "threshold must be between -1.0 and 1.0, got %r" % (value,))
    return number


class RecognitionResult:
    """
    What the recogniser concluded about one detected face.

    Deliberately carries no embedding. It is shaped so a later tracking layer
    can consume it without changes: it already carries the box, the time and a
    stable student id.
    """

    __slots__ = ("identity", "name", "similarity", "status", "threshold",
                 "box", "detection_confidence", "timestamp", "candidates")

    def __init__(self, identity=None, name=None, similarity=0.0,
                 status=STATUS_UNKNOWN, threshold=DEFAULT_RECOGNITION_THRESHOLD,
                 box=None, detection_confidence=0.0, timestamp=None,
                 candidates=None):
        self.identity = identity
        self.name = name
        self.similarity = float(similarity)
        self.status = status
        self.threshold = float(threshold)
        self.box = tuple(box) if box is not None else None
        self.detection_confidence = float(detection_confidence)
        self.timestamp = time.time() if timestamp is None else float(timestamp)

        # The runners-up, for diagnostics. Scores only, never vectors.
        self.candidates = candidates or []

    @property
    def is_match(self):
        return self.status == STATUS_MATCH

    @property
    def label(self):
        """The short text to show on screen."""
        if self.status == STATUS_MATCH:
            return self.name or self.identity
        if self.status == STATUS_NO_ENROLLMENTS:
            return "No students enrolled"
        return "Unknown"

    def to_dict(self):
        """A plain dictionary, safe to log. Contains no biometric vector."""
        return {
            "identity": self.identity,
            "name": self.name,
            "similarity": round(self.similarity, 4),
            "status": self.status,
            "threshold": self.threshold,
            "box": self.box,
            "detection_confidence": round(self.detection_confidence, 4),
            "timestamp": self.timestamp,
        }

    def __repr__(self):
        return "RecognitionResult(identity=%r, similarity=%.4f, status=%r)" % (
            self.identity, self.similarity, self.status)


class EnrolledGallery:
    """
    Every enrolled student's embeddings, held in one matrix for fast search.

    Stacking all vectors together means one matrix multiply compares a face
    against every student at once, instead of looping student by student.
    """

    def __init__(self, store=None):
        self.store = store or EmbeddingStore()
        self.student_ids = []
        self.names = {}
        self.matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        self.owner_index = np.zeros((0,), dtype=np.int32)
        self.model_warnings = []
        self.load()

    @property
    def is_empty(self):
        return self.matrix.shape[0] == 0

    @property
    def student_count(self):
        return len(self.student_ids)

    @property
    def embedding_count(self):
        return int(self.matrix.shape[0])

    def load(self, expected_model=None):
        """
        Read every student's vectors from the store.

        expected_model, when given, is the current embedder's model_info().
        Any student enrolled with a different model is reported, because
        comparing embeddings across models produces confident nonsense.
        """
        vectors = []
        owners = []
        self.student_ids = []
        self.names = {}
        self.model_warnings = []

        for student in self.store.list_students():
            student_id = student["student_id"]

            try:
                stored, info = self.store.load_embeddings(student_id)
            except Exception as error:
                self.model_warnings.append(
                    "%s: embeddings could not be read (%s)"
                    % (student_id, type(error).__name__))
                continue

            if stored.ndim != 2 or stored.shape[1] != EMBEDDING_DIM:
                self.model_warnings.append(
                    "%s: stored embeddings have the wrong shape %s - skipped"
                    % (student_id, (stored.shape,)))
                continue

            if expected_model is not None:
                if (info.get("model_name") != expected_model["model_name"]
                        or info.get("model_version") != expected_model["model_version"]):
                    self.model_warnings.append(
                        "%s: enrolled with %s/%s but the current model is %s/%s "
                        "- skipped, re-enroll this student"
                        % (student_id, info.get("model_name"),
                           info.get("model_version"),
                           expected_model["model_name"],
                           expected_model["model_version"]))
                    continue

            index = len(self.student_ids)
            self.student_ids.append(student_id)
            self.names[student_id] = student.get("name", student_id)

            vectors.append(stored)
            owners.append(np.full(stored.shape[0], index, dtype=np.int32))

        if vectors:
            self.matrix = np.vstack(vectors).astype(np.float32)
            self.owner_index = np.concatenate(owners)

            # Stored vectors are already normalized, but normalizing again is
            # cheap insurance: it keeps the dot product a true cosine even if
            # a vector was written by an older or hand-edited file.
            norms = np.linalg.norm(self.matrix, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            self.matrix = self.matrix / norms
        else:
            self.matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
            self.owner_index = np.zeros((0,), dtype=np.int32)

        return self

    def best_per_student(self, embedding):
        """
        Score every student against one face.

        Returns a list of (student_id, name, similarity), best first. Each
        student's score is the highest of their stored embeddings - the
        best-of-five rule.
        """
        if self.is_empty:
            return []

        query = np.asarray(embedding, dtype=np.float32).flatten()
        if query.size != EMBEDDING_DIM:
            raise ValueError(
                "embedding must have %d numbers, got %d"
                % (EMBEDDING_DIM, query.size))

        length = float(np.linalg.norm(query))
        if length == 0.0:
            raise ValueError("cannot match a zero-length embedding")
        query = query / length

        # Both sides are unit length, so the dot product is the cosine.
        similarities = self.matrix @ query

        best = np.full(len(self.student_ids), -1.0, dtype=np.float32)
        np.maximum.at(best, self.owner_index, similarities)

        order = np.argsort(-best)
        return [(self.student_ids[i], self.names[self.student_ids[i]],
                 float(best[i])) for i in order]


class FaceRecognizer:
    """
    Turns a detected face into a recognition decision.

    Knows nothing about cameras, attendance, tracking or storage formats.
    """

    def __init__(self, store=None, embedder=None, threshold=None, gallery=None):
        self.embedder = embedder or SFaceEmbedder()
        self.threshold = configured_threshold(threshold)
        self.model_info = self.embedder.model_info()

        self.gallery = gallery if gallery is not None else EnrolledGallery(store)
        # Re-load with the model check now that the embedder is known.
        self.gallery.load(expected_model=self.model_info)

    @property
    def warnings(self):
        return list(self.gallery.model_warnings)

    def reload(self):
        """Pick up students enrolled since this recogniser was created."""
        self.gallery.load(expected_model=self.model_info)
        return self

    def identify(self, embedding, box=None, detection_confidence=0.0):
        """Match one embedding against the gallery."""
        if self.gallery.is_empty:
            return RecognitionResult(
                status=STATUS_NO_ENROLLMENTS, threshold=self.threshold,
                box=box, detection_confidence=detection_confidence)

        ranked = self.gallery.best_per_student(embedding)
        best_id, best_name, best_score = ranked[0]

        matched = best_score >= self.threshold

        return RecognitionResult(
            identity=best_id if matched else None,
            name=best_name if matched else None,
            similarity=best_score,
            status=STATUS_MATCH if matched else STATUS_UNKNOWN,
            threshold=self.threshold,
            box=box,
            detection_confidence=detection_confidence,
            candidates=[(sid, round(score, 4)) for sid, _, score in ranked[:3]],
        )

    def recognize(self, frame, detection):
        """Align, embed and identify one detected face."""
        aligned = align_face(frame, detection.landmarks)
        embedding = self.embedder.embed(aligned)

        return self.identify(
            embedding,
            box=detection.as_tuple(),
            detection_confidence=detection.confidence)

    def recognize_all(self, frame, detections):
        """
        Recognise every detected face in a frame.

        Each face is handled independently - no state is shared between them,
        so one face can never influence another's result.
        """
        return [self.recognize(frame, detection) for detection in detections]
