"""
ClassVision AI - Face Embeddings (Step 3)
-----------------------------------------
Turns one aligned face image into an embedding: a fixed-length list of
numbers that describes the face.

What an embedding is, and is not
    It is NOT a photograph and it cannot be viewed. It is also not anonymous:
    an embedding identifies a person, so it is biometric data and is treated
    as sensitive throughout this project.

Model
    SFace, file face_recognition_sface_2021dec.onnx, from the OpenCV model
    zoo, run through cv2.FaceRecognizerSF. It is the recogniser designed to
    pair with the YuNet detector from Step 2, and it is built into the OpenCV
    already installed, so Step 3 adds no new dependency.

Measured facts (not assumed - see the README)
    embedding dimension : 128
    input               : 112x112 BGR uint8, aligned (see face_alignment.py)

Preprocessing
    The face must already be aligned to the 112x112 template. This module does
    not crop or resize; passing an unaligned face produces a valid-looking but
    much less reliable embedding, so align_face() is the required first step.

Normalization
    Every embedding is scaled to unit length (L2 norm = 1). Two benefits:
      - similarity becomes a plain dot product, which is fast and simple;
      - embeddings of the same face at different brightness or scale become
        directly comparable.

Similarity
    Cosine similarity, in the range -1.0 to 1.0. Higher means more similar.
    1.0 is identical. This module deliberately defines no "same person"
    threshold - choosing one is a Step 4 decision and must be made against
    real data.
"""

import hashlib

import cv2
import numpy as np

from face_alignment import ALIGNED_HEIGHT, ALIGNED_WIDTH, align_face
from paths import MODELS_DIR, relative

SFACE_MODEL_NAME = "face_recognition_sface_2021dec.onnx"

# Recorded with every enrolled student so a future model change can never
# cause embeddings from different models to be compared silently.
MODEL_NAME = "sface"
MODEL_VERSION = "2021dec"

# Confirmed by running the model, not taken from documentation.
EMBEDDING_DIM = 128


def cosine_similarity(first, second):
    """
    Similarity between two embeddings, from -1.0 to 1.0. Higher is closer.

    Works whether or not the inputs are already normalized.
    """
    first = np.asarray(first, dtype=np.float32).flatten()
    second = np.asarray(second, dtype=np.float32).flatten()

    if first.shape != second.shape:
        raise ValueError(
            "embeddings have different lengths: %d and %d"
            % (first.size, second.size))

    first_norm = float(np.linalg.norm(first))
    second_norm = float(np.linalg.norm(second))

    if first_norm == 0.0 or second_norm == 0.0:
        raise ValueError("cannot compare a zero-length embedding")

    return float(np.dot(first, second) / (first_norm * second_norm))


def normalize(embedding):
    """Scale an embedding to unit length so similarity is a dot product."""
    vector = np.asarray(embedding, dtype=np.float32).flatten()

    if vector.size != EMBEDDING_DIM:
        raise ValueError(
            "expected an embedding of %d numbers, got %d"
            % (EMBEDDING_DIM, vector.size))

    if not np.all(np.isfinite(vector)):
        raise ValueError("embedding contains NaN or infinite values")

    length = float(np.linalg.norm(vector))
    if length == 0.0:
        raise ValueError("embedding has zero length and cannot be normalized")

    return (vector / length).astype(np.float32)


def check_aligned_face(face):
    """The embedding model only accepts a correctly aligned 112x112 face."""
    if face is None:
        raise ValueError("aligned face is None")

    if not isinstance(face, np.ndarray):
        raise ValueError("aligned face must be a numpy array, got %s"
                         % type(face).__name__)

    if face.ndim != 3 or face.shape[2] != 3:
        raise ValueError("aligned face must be a colour image (h, w, 3), got %s"
                         % (face.shape,))

    if face.shape[0] != ALIGNED_HEIGHT or face.shape[1] != ALIGNED_WIDTH:
        raise ValueError(
            "aligned face must be %dx%d, got %dx%d - run align_face() first"
            % (ALIGNED_WIDTH, ALIGNED_HEIGHT, face.shape[1], face.shape[0]))

    if face.dtype != np.uint8:
        raise ValueError("aligned face must have dtype uint8, got %s" % face.dtype)


class SFaceEmbedder:
    """Produces a normalized 128-number embedding from an aligned face."""

    def __init__(self, model_path=None):
        self.model_path = model_path or (MODELS_DIR / SFACE_MODEL_NAME)

        if not self.model_path.exists():
            raise FileNotFoundError(
                "Face embedding model not found at %s\n"
                "Download it once with:  python ai/download_models.py"
                % relative(self.model_path))

        model_bytes = self.model_path.read_bytes()

        # Identifies the exact weights that produced an embedding. Stored with
        # every enrollment so a swapped model file is detectable.
        self.model_sha256 = hashlib.sha256(model_bytes).hexdigest()

        # Load from BYTES, never from a path: OpenCV cannot open paths
        # containing non-English characters on Windows, and this project can
        # live under such a path. Same fix as the Step 2 detector.
        buffer = np.frombuffer(model_bytes, dtype=np.uint8)

        previous_log_level = cv2.utils.logging.getLogLevel()
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        try:
            self._recognizer = cv2.FaceRecognizerSF.create(
                "onnx", buffer, np.empty(0, dtype=np.uint8))
        finally:
            cv2.utils.logging.setLogLevel(previous_log_level)

    def model_info(self):
        """
        Everything needed to know which model produced an embedding.

        Stored alongside every enrollment (see MODEL VERSIONING in the README).
        """
        return {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "model_file": SFACE_MODEL_NAME,
            "model_sha256": self.model_sha256,
            "embedding_dim": EMBEDDING_DIM,
            "aligned_size": [ALIGNED_WIDTH, ALIGNED_HEIGHT],
            "normalization": "l2",
            "similarity": "cosine",
        }

    def embed(self, aligned_face):
        """
        Aligned 112x112 face in, normalized 128-number embedding out.

        Returns a float32 array of length 128 with an L2 norm of 1.
        """
        check_aligned_face(aligned_face)

        # feature() returns shape (1, 128); flatten and normalize it.
        raw = self._recognizer.feature(aligned_face)

        return normalize(raw)

    def embed_detection(self, frame, detection):
        """Align a detected face and embed it, in one call."""
        aligned = align_face(frame, detection.landmarks)
        return self.embed(aligned)
