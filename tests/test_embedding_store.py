"""
Tests for ai/embedding_store.py and the privacy rules (Step 3).

Every test uses a temporary directory, so the real data/ folder is never
touched, read, or modified.

Run with:
    python tests/test_embedding_store.py
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from embedding_store import (  # noqa: E402
    EmbeddingStore,
    StudentNotFound,
    check_student_id,
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

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif",
                    ".tiff", ".gif", ".mp4", ".avi", ".mov"}


def temp_store():
    """A store in a throwaway directory."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_test_"))
    return EmbeddingStore(directory=directory), directory


def fake_embeddings(count=5, seed=0):
    """Unit-length vectors of the right shape. Not real biometric data."""
    generator = np.random.default_rng(seed)
    vectors = generator.normal(size=(count, EMBEDDING_DIM)).astype(np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def enroll(store, student_id="S001", name="Test Student"):
    store.save_student(
        student_id=student_id, name=name, roll_number="42",
        class_name="10", section="A",
        model_name=MODEL_INFO["model_name"],
        model_version=MODEL_INFO["model_version"])
    store.save_embedding(student_id, fake_embeddings(), MODEL_INFO)


# -- student ids -----------------------------------------------------------

def test_student_id_validation():
    for good in ("S001", "student-1", "a", "A_1-2", "9" * 64):
        check_student_id(good)

    for bad in ("", "../etc/passwd", "a/b", "with space", "x" * 65,
                "-leading", None, 42):
        try:
            check_student_id(bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError("student id %r should have been rejected" % (bad,))

    print("PASS  student ids are validated and path traversal is blocked")


# -- metadata --------------------------------------------------------------

def test_save_and_get_student():
    store, directory = temp_store()
    try:
        saved = store.save_student(
            student_id="S001", name="Asha R", roll_number="23",
            class_name="10", section="B",
            model_name="sface", model_version="2021dec")

        assert saved["student_id"] == "S001", "student_id not stored"
        assert saved["name"] == "Asha R", "name not stored"

        loaded = store.get_student("S001")
        for field in ("student_id", "name", "roll_number", "class_name",
                      "section", "embedding_id", "created_at",
                      "model_name", "model_version"):
            assert field in loaded, "required metadata field missing: " + field

        print("PASS  save_student and get_student keep all required fields")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_get_missing_student_raises():
    store, directory = temp_store()
    try:
        try:
            store.get_student("nobody")
        except StudentNotFound:
            print("PASS  get_student raises StudentNotFound for unknown ids")
            return
        raise AssertionError("an unknown id should raise StudentNotFound")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_list_students():
    store, directory = temp_store()
    try:
        assert store.list_students() == [], "a new store should be empty"

        for student_id in ("S003", "S001", "S002"):
            store.save_student(student_id, "Name " + student_id, "1", "10", "A",
                               "sface", "2021dec")

        listed = store.list_students()
        assert len(listed) == 3, "expected 3 students, got %d" % len(listed)
        ids = [s["student_id"] for s in listed]
        assert ids == sorted(ids), "list_students should be sorted by id"
        print("PASS  list_students returns every student, sorted")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_saving_twice_preserves_created_at():
    store, directory = temp_store()
    try:
        first = store.save_student("S001", "A", "1", "10", "A", "sface", "2021dec")
        second = store.save_student("S001", "A Renamed", "1", "10", "A",
                                    "sface", "2021dec")
        assert second["created_at"] == first["created_at"], (
            "created_at should not change when a record is updated")
        assert second["name"] == "A Renamed", "the update should apply"
        print("PASS  updating a student preserves the original created_at")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- embeddings ------------------------------------------------------------

def test_embedding_round_trip():
    store, directory = temp_store()
    try:
        enroll(store)
        original = fake_embeddings()
        store.save_embedding("S001", original, MODEL_INFO)

        loaded, info = store.load_embeddings("S001")

        assert loaded.shape == (5, EMBEDDING_DIM), (
            "expected (5, %d), got %s" % (EMBEDDING_DIM, loaded.shape))
        assert np.allclose(loaded, original, atol=1e-6), (
            "embeddings changed during the round trip")
        assert info["model_name"] == "sface", "model name not persisted"
        assert info["model_version"] == "2021dec", "model version not persisted"
        assert info["embedding_dim"] == EMBEDDING_DIM, "dimension not persisted"
        print("PASS  embeddings survive a save/load round trip unchanged")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_single_embedding_is_reshaped():
    store, directory = temp_store()
    try:
        enroll(store)
        store.save_embedding("S001", fake_embeddings(1)[0], MODEL_INFO)
        loaded, _ = store.load_embeddings("S001")
        assert loaded.shape == (1, EMBEDDING_DIM), (
            "a single vector should be stored as (1, %d), got %s"
            % (EMBEDDING_DIM, loaded.shape))
        print("PASS  a single embedding is stored as a one-row array")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_sample_count_is_recorded():
    store, directory = temp_store()
    try:
        enroll(store)
        store.save_embedding("S001", fake_embeddings(7), MODEL_INFO)
        assert store.get_student("S001")["sample_count"] == 7, (
            "sample_count should match the number of stored vectors")
        print("PASS  sample_count tracks how many vectors were stored")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_wrong_dimension_is_rejected():
    store, directory = temp_store()
    try:
        enroll(store)
        for bad, description in [
            (np.zeros((3, 64), dtype=np.float32), "the wrong dimension"),
            (np.full((2, EMBEDDING_DIM), np.nan, dtype=np.float32), "NaN values"),
        ]:
            try:
                store.save_embedding("S001", bad, MODEL_INFO)
            except ValueError:
                continue
            raise AssertionError("save_embedding should have rejected " + description)
        print("PASS  save_embedding rejects wrong dimensions and NaN values")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_load_missing_embeddings_raises():
    store, directory = temp_store()
    try:
        store.save_student("S001", "A", "1", "10", "A", "sface", "2021dec")
        try:
            store.load_embeddings("S001")
        except StudentNotFound:
            print("PASS  loading absent embeddings raises StudentNotFound")
            return
        raise AssertionError("missing embeddings should raise StudentNotFound")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- deletion --------------------------------------------------------------

def test_delete_removes_metadata_and_vectors():
    """The core privacy guarantee: deletion must leave nothing behind."""
    store, directory = temp_store()
    try:
        enroll(store)
        embedding_file = directory / "S001.npz"
        assert embedding_file.exists(), "test setup failed: no embedding file"

        assert store.delete_student("S001") is True, "delete should report success"

        assert not embedding_file.exists(), (
            "DELETED STUDENT STILL HAS EMBEDDINGS ON DISK")
        assert not store.has_student("S001"), "metadata was not removed"
        assert store.list_students() == [], "store should be empty after deletion"

        remaining = [p.name for p in directory.rglob("*") if p.is_file()]
        assert not any("S001" in n for n in remaining), (
            "files referencing the deleted student remain: %s" % remaining)

        print("PASS  deleting a student removes metadata AND embedding vectors")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_delete_unknown_student_is_safe():
    store, directory = temp_store()
    try:
        assert store.delete_student("ghost") is False, (
            "deleting an unknown student should return False, not raise")
        print("PASS  deleting an unknown student is safe")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- privacy ---------------------------------------------------------------

def test_no_images_are_written():
    store, directory = temp_store()
    try:
        enroll(store)
        written = [p for p in directory.rglob("*") if p.is_file()]
        for path in written:
            assert path.suffix.lower() not in IMAGE_EXTENSIONS, (
                "an image/video file was written into the store: %s" % path.name)
        print("PASS  no image or video files are written (%d files checked)"
              % len(written))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_store_refuses_image_metadata():
    """A raw photo must not be smuggled in through the metadata."""
    store, directory = temp_store()
    try:
        for field in ("image", "photo", "face_image", "crop", "frame"):
            try:
                store.save_student("S001", "A", "1", "10", "A", "sface",
                                   "2021dec", extra={field: "somedata"})
            except ValueError:
                continue
            raise AssertionError(
                "save_student should refuse the %r field" % field)
        print("PASS  the store refuses image-shaped metadata fields")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_metadata_file_contains_no_vectors():
    """students.json is human-readable, so it must never hold biometrics."""
    store, directory = temp_store()
    try:
        enroll(store)
        text = (directory / "students.json").read_text(encoding="utf-8")
        document = json.loads(text)

        record = document["students"]["S001"]
        for key, value in record.items():
            assert not isinstance(value, (list, dict)), (
                "metadata field %r holds a %s, which could hide a vector"
                % (key, type(value).__name__))

        # A vector would show up as a long run of comma-separated numbers.
        # Looking for that is precise; looking for the word "embedding" is not,
        # because "embedding_id" is a harmless label.
        number_runs = re.findall(r"(?:-?\d+\.\d+\s*,\s*){8,}", text)
        assert not number_runs, (
            "the metadata file contains a run of numbers that looks like a vector")

        print("PASS  the metadata file contains no embedding vectors")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_audit_reports_without_exposing_vectors():
    store, directory = temp_store()
    try:
        enroll(store)
        rows = store.audit()

        assert len(rows) == 1, "audit should report the one enrolled student"
        row = rows[0]
        assert row["sample_count"] == 5, "audit should report the sample count"
        assert row["embeddings_present"] is True, "audit should see the vectors"

        for key, value in row.items():
            assert not isinstance(value, (list, tuple, np.ndarray)), (
                "audit exposed an array in field %r" % key)

        print("PASS  audit reports enrollments without exposing vectors")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_data_directory_is_gitignored():
    """The real guarantee that biometrics never reach the repository."""
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    entries = [line.strip() for line in gitignore.splitlines()]

    assert "data/" in entries, "'data/' is not in .gitignore"
    assert "*.npz" in entries, "'*.npz' is not in .gitignore"
    print("PASS  data/ and *.npz are gitignored")


def test_source_files_never_print_vectors():
    """
    No module may print an embedding. Logs are a real leak path for
    biometric data, so this is enforced rather than left to discipline.
    """
    # Names that hold a vector rather than a number.
    vector_names = ("embedding", "embeddings", "vector", "vectors",
                    "feature", "features")

    # Wrapping a vector in any of these yields a harmless number.
    safe_wrappers = re.compile(
        r"\b(?:len|min|max|sum|abs|int|float|round|sorted|enumerate|"
        r"cosine_similarity)\s*\([^()]*\)")

    suspicious = []
    for path in sorted((PROJECT_ROOT / "ai").glob("*.py")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("print("):
                continue

            # Strip the message text first. The WORD "embedding" inside a
            # sentence is harmless; only a vector passed as a value matters.
            reduced = re.sub(r'"[^"]*"', '""', stripped)
            reduced = re.sub(r"'[^']*'", "''", reduced)

            # Remove safe wrapper calls, then look for a bare vector name.
            for _ in range(4):
                reduced = safe_wrappers.sub("SAFE", reduced)

            for name in vector_names:
                if re.search(r"\b%s\b" % name, reduced):
                    suspicious.append("%s:%d  %s" % (path.name, number, stripped))
                    break

    assert not suspicious, "a module appears to print a vector:\n  " + \
        "\n  ".join(suspicious)
    print("PASS  no ai/ module prints an embedding vector")


def main():
    enable_safe_console()

    tests = [
        test_student_id_validation,
        test_save_and_get_student,
        test_get_missing_student_raises,
        test_list_students,
        test_saving_twice_preserves_created_at,
        test_embedding_round_trip,
        test_single_embedding_is_reshaped,
        test_sample_count_is_recorded,
        test_wrong_dimension_is_rejected,
        test_load_missing_embeddings_raises,
        test_delete_removes_metadata_and_vectors,
        test_delete_unknown_student_is_safe,
        test_no_images_are_written,
        test_store_refuses_image_metadata,
        test_metadata_file_contains_no_vectors,
        test_audit_reports_without_exposing_vectors,
        test_data_directory_is_gitignored,
        test_source_files_never_print_vectors,
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
