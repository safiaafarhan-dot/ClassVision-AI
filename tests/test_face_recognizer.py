"""
Tests for ai/face_recognizer.py (Step 4).

No webcam and no network needed. The gallery is built from controlled
vectors, so matching behaviour can be asserted exactly.

These test the DECISION LOGIC, not recognition accuracy. No accuracy figure is
claimed anywhere in this project.

Run with:
    python tests/test_face_recognizer.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from embedding_store import EmbeddingStore  # noqa: E402
from face_recognizer import (  # noqa: E402
    DEFAULT_RECOGNITION_THRESHOLD,
    STATUS_MATCH,
    STATUS_NO_ENROLLMENTS,
    STATUS_UNKNOWN,
    THRESHOLD_ENV_VAR,
    UPSTREAM_SFACE_COSINE_THRESHOLD,
    EnrolledGallery,
    FaceRecognizer,
    RecognitionResult,
    configured_threshold,
    validate_threshold,
)
from paths import enable_safe_console  # noqa: E402

EMBEDDING_DIM = 128

MODEL_INFO = {
    "model_name": "sface",
    "model_version": "2021dec",
    "model_file": "face_recognition_sface_2021dec.onnx",
    "model_sha256": "0" * 64,
    "embedding_dim": EMBEDDING_DIM,
    "normalization": "l2",
    "similarity": "cosine",
}


def unit(vector):
    vector = np.asarray(vector, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def basis(index):
    """A unit vector pointing along one axis. Orthogonal to every other."""
    vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vector[index] = 1.0
    return vector


def near(vector, similarity, axis=99):
    """
    Build a vector at a known cosine similarity to `vector`.

    Mixing in an orthogonal direction gives exact control, which lets the
    threshold behaviour be asserted precisely instead of approximately.
    """
    other = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    other[axis] = 1.0
    other = other - np.dot(other, vector) * vector
    other = other / np.linalg.norm(other)
    return unit(similarity * vector + np.sqrt(1.0 - similarity ** 2) * other)


def temp_store():
    directory = Path(tempfile.mkdtemp(prefix="classvision_rec_"))
    return EmbeddingStore(directory=directory), directory


def add_student(store, student_id, embeddings, name=None,
                model_name="sface", model_version="2021dec"):
    store.save_student(student_id, name or student_id, "1", "10", "A",
                       model_name, model_version)
    info = dict(MODEL_INFO)
    info["model_name"] = model_name
    info["model_version"] = model_version
    store.save_embedding(student_id, np.array(embeddings, dtype=np.float32), info)


class FakeEmbedder:
    """Stands in for SFace so tests need no model and no images."""

    def __init__(self, model_info=None):
        self._info = model_info or MODEL_INFO

    def model_info(self):
        return dict(self._info)

    def embed(self, aligned_face):
        return unit(np.ones(EMBEDDING_DIM, dtype=np.float32))


def recognizer_for(store, threshold=None, model_info=None):
    return FaceRecognizer(store=store, embedder=FakeEmbedder(model_info),
                          threshold=threshold)


# -- threshold configuration ----------------------------------------------

def test_validate_threshold():
    for good in (-1.0, 0.0, 0.363, 0.45, 1.0):
        validate_threshold(good)
    for bad in (1.5, -2.0, "abc"):
        try:
            validate_threshold(bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError("threshold %r should have been rejected" % (bad,))
    print("PASS  threshold validation accepts -1..1 and rejects the rest")


def test_default_threshold_is_not_the_synthetic_number():
    """
    Step 3's synthetic same-face scores were 0.93+. Using that as a threshold
    would reject nearly every real match, so this asserts we did not.
    """
    assert DEFAULT_RECOGNITION_THRESHOLD < 0.9, (
        "the default threshold looks like the synthetic diagnostic value")
    assert DEFAULT_RECOGNITION_THRESHOLD >= UPSTREAM_SFACE_COSINE_THRESHOLD, (
        "the default should not be looser than the model authors' value")
    print("PASS  default threshold %.2f sits above the authors' %.3f and well "
          "below the synthetic 0.93"
          % (DEFAULT_RECOGNITION_THRESHOLD, UPSTREAM_SFACE_COSINE_THRESHOLD))


def test_threshold_precedence():
    explicit = configured_threshold(0.7)
    assert explicit == 0.7, "an explicit threshold should win"

    previous = os.environ.get(THRESHOLD_ENV_VAR)
    try:
        os.environ[THRESHOLD_ENV_VAR] = "0.55"
        assert configured_threshold() == 0.55, "the environment variable should apply"
        assert configured_threshold(0.7) == 0.7, "explicit should beat environment"

        os.environ[THRESHOLD_ENV_VAR] = "not-a-number"
        try:
            configured_threshold()
        except ValueError:
            pass
        else:
            raise AssertionError("a bad environment value should raise")
    finally:
        if previous is None:
            os.environ.pop(THRESHOLD_ENV_VAR, None)
        else:
            os.environ[THRESHOLD_ENV_VAR] = previous

    assert configured_threshold() == DEFAULT_RECOGNITION_THRESHOLD, (
        "the default should apply when nothing is set")
    print("PASS  threshold precedence: explicit > environment > default")


# -- gallery ---------------------------------------------------------------

def test_empty_gallery():
    store, directory = temp_store()
    try:
        gallery = EnrolledGallery(store)
        assert gallery.is_empty, "a new store should produce an empty gallery"
        assert gallery.best_per_student(basis(0)) == [], (
            "an empty gallery should return no candidates")
        print("PASS  an empty gallery matches nothing and does not crash")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_best_of_five_uses_the_maximum():
    """A student's score must be their BEST embedding, not their average."""
    store, directory = temp_store()
    try:
        target = basis(0)
        embeddings = [near(target, s, axis=50 + i)
                      for i, s in enumerate([0.10, 0.20, 0.30, 0.40, 0.95])]
        add_student(store, "S001", embeddings)

        ranked = EnrolledGallery(store).best_per_student(target)
        _, _, score = ranked[0]

        assert abs(score - 0.95) < 0.01, (
            "expected the best-of-five score 0.95, got %.4f" % score)

        average_like = sum([0.10, 0.20, 0.30, 0.40, 0.95]) / 5.0
        assert score > average_like + 0.2, (
            "the score looks averaged (%.4f) rather than maximised" % score)

        print("PASS  best-of-five takes the maximum (%.4f), not the mean" % score)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_candidates_are_ranked():
    store, directory = temp_store()
    try:
        target = basis(0)
        add_student(store, "S001", [near(target, 0.90, axis=60)])
        add_student(store, "S002", [near(target, 0.70, axis=61)])
        add_student(store, "S003", [near(target, 0.50, axis=62)])

        ranked = EnrolledGallery(store).best_per_student(target)
        ids = [student_id for student_id, _, _ in ranked]
        scores = [score for _, _, score in ranked]

        assert ids == ["S001", "S002", "S003"], "candidates are not ranked: %s" % ids
        assert scores == sorted(scores, reverse=True), "scores are not descending"
        print("PASS  candidates are ranked best-first across %d students" % len(ids))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_malformed_stored_data_is_skipped():
    store, directory = temp_store()
    try:
        add_student(store, "GOOD", [basis(0)])

        # A student whose vectors are the wrong size.
        store.save_student("BAD", "Bad", "1", "10", "A", "sface", "2021dec")
        np.savez_compressed(
            directory / "BAD.npz",
            embeddings=np.zeros((2, 64), dtype=np.float32),
            student_id=np.array("BAD"), model_name=np.array("sface"),
            model_version=np.array("2021dec"), model_sha256=np.array(""),
            embedding_dim=np.array(64), normalization=np.array("l2"),
            saved_at=np.array("now"))

        # A student with no vector file at all.
        store.save_student("MISSING", "Missing", "1", "10", "A", "sface", "2021dec")

        gallery = EnrolledGallery(store)

        assert "GOOD" in gallery.student_ids, "the valid student was dropped"
        assert "BAD" not in gallery.student_ids, "malformed vectors were accepted"
        assert "MISSING" not in gallery.student_ids, "a student with no vectors was accepted"
        assert len(gallery.model_warnings) >= 2, (
            "problems should be reported, got %r" % gallery.model_warnings)
        print("PASS  malformed and missing data are skipped with warnings")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_model_mismatch_is_refused():
    """Step 3 recorded the model so Step 4 can refuse incompatible vectors."""
    store, directory = temp_store()
    try:
        add_student(store, "OLD", [basis(0)],
                    model_name="sface", model_version="2019old")
        add_student(store, "NEW", [basis(1)])

        recognizer = recognizer_for(store)

        assert "NEW" in recognizer.gallery.student_ids, "the current student was dropped"
        assert "OLD" not in recognizer.gallery.student_ids, (
            "a student enrolled with a different model was used anyway")
        assert any("re-enroll" in w for w in recognizer.warnings), (
            "the mismatch warning should tell the user what to do")
        print("PASS  embeddings from a different model version are refused")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_gallery_rejects_bad_query():
    store, directory = temp_store()
    try:
        add_student(store, "S001", [basis(0)])
        gallery = EnrolledGallery(store)

        for bad, description in [
            (np.zeros(64, dtype=np.float32), "the wrong length"),
            (np.zeros(EMBEDDING_DIM, dtype=np.float32), "a zero vector"),
        ]:
            try:
                gallery.best_per_student(bad)
            except ValueError:
                continue
            raise AssertionError("best_per_student should reject " + description)
        print("PASS  the gallery rejects malformed query embeddings")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- recognition decisions -------------------------------------------------

def test_known_face_matches():
    store, directory = temp_store()
    try:
        target = basis(0)
        add_student(store, "S001", [target], name="Rahul")
        recognizer = recognizer_for(store, threshold=0.45)

        result = recognizer.identify(target)

        assert result.status == STATUS_MATCH, "a perfect match should MATCH"
        assert result.identity == "S001", "wrong identity: %r" % result.identity
        assert result.name == "Rahul", "the display name should be returned"
        assert result.similarity > 0.99, "similarity should be ~1.0"
        print("PASS  a known face returns MATCH (%.4f)" % result.similarity)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_unknown_face_is_not_forced_onto_a_student():
    """The most important behaviour in Step 4."""
    store, directory = temp_store()
    try:
        target = basis(0)
        add_student(store, "S001", [target], name="Rahul")
        recognizer = recognizer_for(store, threshold=0.45)

        stranger = near(target, 0.42, axis=70)
        result = recognizer.identify(stranger)

        assert result.status == STATUS_UNKNOWN, (
            "a below-threshold face must be UNKNOWN, got %s" % result.status)
        assert result.identity is None, (
            "UNKNOWN must not carry an identity, got %r" % result.identity)
        assert result.name is None, "UNKNOWN must not carry a name"
        assert 0.40 < result.similarity < 0.45, (
            "the score should still be reported, got %.4f" % result.similarity)
        print("PASS  a stranger returns UNKNOWN (%.4f) with no identity attached"
              % result.similarity)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_threshold_boundary():
    store, directory = temp_store()
    try:
        target = basis(0)
        add_student(store, "S001", [target])
        recognizer = recognizer_for(store, threshold=0.60)

        just_above = recognizer.identify(near(target, 0.62, axis=71))
        just_below = recognizer.identify(near(target, 0.58, axis=71))

        assert just_above.status == STATUS_MATCH, "0.62 should match at 0.60"
        assert just_below.status == STATUS_UNKNOWN, "0.58 should not match at 0.60"
        print("PASS  the threshold boundary behaves exactly (0.62 match, 0.58 unknown)")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_no_enrollments_status():
    store, directory = temp_store()
    try:
        recognizer = recognizer_for(store)
        result = recognizer.identify(basis(0))

        assert result.status == STATUS_NO_ENROLLMENTS, (
            "an empty store should report NO_ENROLLMENTS, got %s" % result.status)
        assert result.identity is None, "there is nobody to identify"
        print("PASS  an empty store reports NO_ENROLLMENTS rather than UNKNOWN")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_multiple_students_are_independent():
    store, directory = temp_store()
    try:
        rahul, priya = basis(0), basis(1)
        add_student(store, "S001", [rahul], name="Rahul")
        add_student(store, "S002", [priya], name="Priya")
        recognizer = recognizer_for(store, threshold=0.45)

        stranger = basis(2)
        results = [recognizer.identify(v) for v in (rahul, priya, stranger)]

        assert results[0].identity == "S001", "face 1 should be Rahul"
        assert results[1].identity == "S002", "face 2 should be Priya"
        assert results[2].status == STATUS_UNKNOWN, "face 3 should be UNKNOWN"

        # Running them in a different order must not change anything.
        again = [recognizer.identify(v) for v in (stranger, priya, rahul)]
        assert again[2].identity == "S001", "order changed the result"
        assert again[1].identity == "S002", "order changed the result"
        assert again[0].status == STATUS_UNKNOWN, "order changed the result"

        print("PASS  three faces resolve independently, in any order")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_recognize_all_returns_one_result_per_face():
    store, directory = temp_store()
    try:
        add_student(store, "S001", [unit(np.ones(EMBEDDING_DIM, dtype=np.float32))])
        recognizer = recognizer_for(store, threshold=0.45)

        class FakeDetection:
            def __init__(self, x):
                self.x, self.y, self.width, self.height = x, 0, 50, 50
                self.confidence = 0.99
                self.landmarks = np.array(
                    [[38.3, 51.7], [73.5, 51.5], [56.0, 71.7],
                     [41.5, 92.4], [70.7, 92.2]], dtype=np.float32)

            def as_tuple(self):
                return (self.x, self.y, self.width, self.height)

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = [FakeDetection(10), FakeDetection(200), FakeDetection(400)]
        results = recognizer.recognize_all(frame, detections)

        assert len(results) == 3, "expected one result per face, got %d" % len(results)
        boxes = [r.box for r in results]
        assert len(set(boxes)) == 3, "results should carry their own boxes: %s" % boxes
        print("PASS  recognize_all returns one independent result per face")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- result schema and privacy --------------------------------------------

def test_result_schema():
    result = RecognitionResult(identity="S001", name="Rahul", similarity=0.91,
                               status=STATUS_MATCH, threshold=0.45,
                               box=(10, 20, 30, 40), detection_confidence=0.98)

    assert result.is_match, "is_match should be True for a MATCH"
    assert result.label == "Rahul", "the label should be the display name"

    data = result.to_dict()
    for field in ("identity", "name", "similarity", "status", "threshold",
                  "box", "detection_confidence", "timestamp"):
        assert field in data, "to_dict is missing %r" % field

    assert data["similarity"] == 0.91, "similarity should survive to_dict"
    print("PASS  the result schema carries everything a tracking layer needs")


def test_unknown_result_label():
    unknown = RecognitionResult(status=STATUS_UNKNOWN, similarity=0.42)
    assert unknown.label == "Unknown", "an unknown face should be labelled Unknown"
    assert not unknown.is_match, "is_match should be False"
    assert unknown.to_dict()["identity"] is None, "identity must stay None"
    print("PASS  an UNKNOWN result is labelled and serialised correctly")


def test_result_carries_no_biometric_vector():
    """A result may be logged, so it must never contain an embedding."""
    result = RecognitionResult(identity="S001", name="Rahul", similarity=0.91,
                               status=STATUS_MATCH, box=(1, 2, 3, 4))

    for forbidden in ("embedding", "embeddings", "vector", "features",
                      "face_image", "crop", "frame"):
        assert not hasattr(result, forbidden), (
            "RecognitionResult exposes %r" % forbidden)

    for key, value in result.to_dict().items():
        if key == "box":
            continue
        assert not isinstance(value, (list, tuple, np.ndarray)), (
            "to_dict field %r contains an array" % key)

    print("PASS  a recognition result carries no biometric vector")


def main():
    enable_safe_console()

    tests = [
        test_validate_threshold,
        test_default_threshold_is_not_the_synthetic_number,
        test_threshold_precedence,
        test_empty_gallery,
        test_best_of_five_uses_the_maximum,
        test_candidates_are_ranked,
        test_malformed_stored_data_is_skipped,
        test_model_mismatch_is_refused,
        test_gallery_rejects_bad_query,
        test_known_face_matches,
        test_unknown_face_is_not_forced_onto_a_student,
        test_threshold_boundary,
        test_no_enrollments_status,
        test_multiple_students_are_independent,
        test_recognize_all_returns_one_result_per_face,
        test_result_schema,
        test_unknown_result_label,
        test_result_carries_no_biometric_vector,
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
