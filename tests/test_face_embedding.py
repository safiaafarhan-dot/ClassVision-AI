"""
Tests for ai/face_embedding.py (Step 3).

Needs the SFace model in models/ but no webcam and no network.

These check behaviour, shape, and normalization. They do NOT measure
recognition accuracy - that needs a labelled dataset this project does not
have, and no accuracy figure is claimed anywhere.

Run with:
    python tests/test_face_embedding.py
"""

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

import face_embedding  # noqa: E402
from face_embedding import (  # noqa: E402
    EMBEDDING_DIM,
    MODEL_NAME,
    MODEL_VERSION,
    SFaceEmbedder,
    check_aligned_face,
    cosine_similarity,
    normalize,
)
from paths import enable_safe_console  # noqa: E402

MODEL_PATH = PROJECT_ROOT / "models" / face_embedding.SFACE_MODEL_NAME


def fake_aligned_face(seed=0):
    """A 112x112 image with structure. Not a real face - shape testing only."""
    generator = np.random.default_rng(seed)
    return generator.integers(0, 256, (112, 112, 3), dtype=np.uint8)


# -- normalization and similarity (no model needed) ------------------------

def test_normalize_produces_unit_length():
    vector = np.arange(1, EMBEDDING_DIM + 1, dtype=np.float32)
    result = normalize(vector)
    length = float(np.linalg.norm(result))
    assert abs(length - 1.0) < 1e-5, "normalized length should be 1.0, got %f" % length
    assert result.dtype == np.float32, "embeddings should stay float32"
    print("PASS  normalize produces a unit-length float32 vector")


def test_normalize_is_consistent():
    """Scaling a vector must not change its normalized form."""
    vector = np.random.default_rng(5).normal(size=EMBEDDING_DIM).astype(np.float32)
    once = normalize(vector)
    scaled = normalize(vector * 17.3)
    twice = normalize(once)

    assert np.allclose(once, scaled, atol=1e-5), (
        "scaling the input changed the normalized result")
    assert np.allclose(once, twice, atol=1e-6), (
        "normalizing twice changed the result")
    print("PASS  normalization is consistent and idempotent")


def test_normalize_rejects_bad_input():
    bad = [
        (np.zeros(EMBEDDING_DIM, dtype=np.float32), "an all-zero vector"),
        (np.zeros(64, dtype=np.float32), "the wrong length"),
        (np.full(EMBEDDING_DIM, np.nan, dtype=np.float32), "NaN values"),
    ]
    for value, description in bad:
        try:
            normalize(value)
        except ValueError:
            continue
        raise AssertionError("normalize should have rejected " + description)
    print("PASS  normalize rejects all %d invalid inputs" % len(bad))


def test_cosine_similarity_known_values():
    a = np.zeros(EMBEDDING_DIM, dtype=np.float32); a[0] = 1.0
    b = np.zeros(EMBEDDING_DIM, dtype=np.float32); b[1] = 1.0

    assert abs(cosine_similarity(a, a) - 1.0) < 1e-6, "a vector must match itself"
    assert abs(cosine_similarity(a, b)) < 1e-6, "orthogonal vectors should score 0"
    assert abs(cosine_similarity(a, -a) + 1.0) < 1e-6, "opposite vectors score -1"
    print("PASS  cosine similarity returns 1.0 / 0.0 / -1.0 as expected")


def test_cosine_similarity_rejects_mismatched_sizes():
    try:
        cosine_similarity(np.ones(128, np.float32), np.ones(64, np.float32))
    except ValueError:
        print("PASS  cosine similarity rejects mismatched lengths")
        return
    raise AssertionError("mismatched lengths should raise ValueError")


# -- aligned-face validation ----------------------------------------------

def test_check_aligned_face_rejects_wrong_input():
    bad = [
        (None, "None"),
        (np.zeros((112, 112), dtype=np.uint8), "a greyscale image"),
        (np.zeros((64, 64, 3), dtype=np.uint8), "the wrong size"),
        (np.zeros((112, 112, 3), dtype=np.float32), "a float image"),
    ]
    for value, description in bad:
        try:
            check_aligned_face(value)
        except ValueError:
            continue
        raise AssertionError("check_aligned_face should have rejected " + description)
    print("PASS  check_aligned_face rejects all %d invalid inputs" % len(bad))


def test_wrong_size_error_is_actionable():
    try:
        check_aligned_face(np.zeros((64, 64, 3), dtype=np.uint8))
    except ValueError as error:
        assert "align_face" in str(error), "the error should say how to fix it"
        print("PASS  wrong-size error tells the user to run align_face()")


# -- the model itself ------------------------------------------------------

def test_embedder_initializes():
    embedder = SFaceEmbedder()
    assert embedder.model_path.exists(), "model file should exist"
    assert len(embedder.model_sha256) == 64, "model sha256 should be recorded"
    print("PASS  embedder initializes and records its model checksum")


def test_missing_model_raises_clear_error():
    try:
        SFaceEmbedder(model_path=PROJECT_ROOT / "models" / "missing.onnx")
    except FileNotFoundError as error:
        assert "download_models" in str(error), "error should say how to fix it"
        print("PASS  missing model raises a clear, actionable error")
        return
    raise AssertionError("a missing model should raise FileNotFoundError")


def test_embedding_shape_and_normalization():
    embedder = SFaceEmbedder()
    embedding = embedder.embed(fake_aligned_face())

    assert embedding.shape == (EMBEDDING_DIM,), (
        "embedding should be %d numbers, got %s" % (EMBEDDING_DIM, embedding.shape))
    assert embedding.dtype == np.float32, "embedding should be float32"

    length = float(np.linalg.norm(embedding))
    assert abs(length - 1.0) < 1e-5, "embedding should be unit length, got %f" % length
    print("PASS  embed returns a unit-length %d-number float32 vector" % EMBEDDING_DIM)


def test_embedding_is_deterministic():
    embedder = SFaceEmbedder()
    face = fake_aligned_face(seed=11)
    first = embedder.embed(face)
    second = embedder.embed(face)
    assert np.allclose(first, second, atol=1e-6), (
        "the same face produced different embeddings")
    print("PASS  the same input always produces the same embedding")


def test_different_inputs_differ():
    embedder = SFaceEmbedder()
    a = embedder.embed(fake_aligned_face(seed=1))
    b = embedder.embed(fake_aligned_face(seed=2))
    assert not np.allclose(a, b, atol=1e-4), (
        "different inputs produced identical embeddings")
    print("PASS  different inputs produce different embeddings")


def test_embed_rejects_invalid_input():
    embedder = SFaceEmbedder()
    for bad in (None, np.zeros((50, 50, 3), dtype=np.uint8)):
        try:
            embedder.embed(bad)
        except ValueError:
            continue
        raise AssertionError("embed should reject invalid input")
    print("PASS  embed fails safely on invalid input")


def test_model_info_is_complete():
    info = SFaceEmbedder().model_info()
    required = ("model_name", "model_version", "model_file", "model_sha256",
                "embedding_dim", "normalization", "similarity")
    for key in required:
        assert key in info, "model_info is missing %r" % key

    assert info["model_name"] == MODEL_NAME, "model name mismatch"
    assert info["model_version"] == MODEL_VERSION, "model version mismatch"
    assert info["embedding_dim"] == EMBEDDING_DIM, "dimension mismatch"
    print("PASS  model_info records everything needed for versioning")


def test_declared_dimension_matches_reality():
    """Guards against the documented dimension drifting from the model."""
    actual = SFaceEmbedder().embed(fake_aligned_face()).size
    assert actual == EMBEDDING_DIM, (
        "EMBEDDING_DIM says %d but the model produced %d" % (EMBEDDING_DIM, actual))
    print("PASS  declared EMBEDDING_DIM (%d) matches the real model output"
          % EMBEDDING_DIM)


def main():
    enable_safe_console()

    if not MODEL_PATH.exists():
        print("MODEL MISSING: %s" % face_embedding.SFACE_MODEL_NAME)
        print("Run this first:  python ai/download_models.py")
        return 1

    tests = [
        test_normalize_produces_unit_length,
        test_normalize_is_consistent,
        test_normalize_rejects_bad_input,
        test_cosine_similarity_known_values,
        test_cosine_similarity_rejects_mismatched_sizes,
        test_check_aligned_face_rejects_wrong_input,
        test_wrong_size_error_is_actionable,
        test_embedder_initializes,
        test_missing_model_raises_clear_error,
        test_embedding_shape_and_normalization,
        test_embedding_is_deterministic,
        test_different_inputs_differ,
        test_embed_rejects_invalid_input,
        test_model_info_is_complete,
        test_declared_dimension_matches_reality,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__, error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
