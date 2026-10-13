"""
ClassVision AI - Local Embedding Store (Step 3)
-----------------------------------------------
Stores enrolled students and their face embeddings on disk.

This is deliberately the ONLY module that knows where data lives or what
format it is in. Camera code and AI code never touch files directly. Step 6
replaces this store with PostgreSQL by reimplementing this same small API,
and nothing else has to change.

Layout
    data/
      embeddings/
        students.json          metadata for every enrolled student
        <student_id>.npz       that student's embedding vectors

What is stored
    - student metadata (id, name, roll number, class, section, timestamps)
    - embedding vectors, as float32 numbers
    - which model produced them, so incompatible embeddings are never mixed

What is NOT stored
    - raw photographs, face crops, or any image of the student

Privacy
    Embeddings are biometric data. data/ is gitignored, vectors are never
    printed or logged, and deleting a student deletes their vectors. See the
    privacy section of the README for what this prototype does NOT do.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import numpy as np

from paths import PROJECT_ROOT, relative

DATA_DIR = PROJECT_ROOT / "data"
EMBEDDINGS_DIR = DATA_DIR / "embeddings"
STUDENTS_FILE = EMBEDDINGS_DIR / "students.json"

# Student ids become file names, so keep them boring and safe. This also
# blocks path-traversal ids such as "../../etc/passwd".
VALID_STUDENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

METADATA_FIELDS = (
    "student_id", "name", "roll_number", "class_name", "section",
    "embedding_id", "created_at", "model_name", "model_version",
)


class StudentNotFound(KeyError):
    """Raised when a student id is not in the store."""


def utc_now_iso():
    """Current time as an ISO-8601 string, so stored dates are unambiguous."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_student_id(student_id):
    """Reject ids that are unsafe as file names or simply empty."""
    if not isinstance(student_id, str) or not VALID_STUDENT_ID.match(student_id):
        raise ValueError(
            "invalid student_id %r - use letters, digits, underscore or "
            "hyphen only (max 64 characters)" % (student_id,))
    return student_id


def restrict_permissions(path):
    """
    Ask the operating system to keep biometric files to the current user.

    On Linux and macOS this is owner-only read/write. On Windows os.chmod
    cannot express that, so this is a best effort there - see the README
    limitations. Failing to set permissions must never lose the data.
    """
    try:
        os.chmod(path, 0o600 if path.is_file() else 0o700)
    except (OSError, NotImplementedError):
        pass


class EmbeddingStore:
    """A small local store for enrolled students and their embeddings."""

    def __init__(self, directory=None):
        # Tests pass a temporary directory so they never touch real data.
        self.directory = EMBEDDINGS_DIR if directory is None else directory
        self.students_file = self.directory / "students.json"

    # -- internal helpers --------------------------------------------------

    def _ensure_directory(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.directory)

    def _read_all(self):
        """Read the metadata file. Missing or empty means no students yet."""
        if not self.students_file.exists():
            return {}

        text = self.students_file.read_text(encoding="utf-8").strip()
        if not text:
            return {}

        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(
                "student metadata file is corrupted (%s): %s"
                % (relative(self.students_file), error))

        return data.get("students", {})

    def _write_all(self, students):
        """
        Write the metadata file.

        Writes to a temporary file first and then replaces the real one, so an
        interrupted write cannot leave a half-written file behind.
        """
        self._ensure_directory()

        document = {
            "format_version": 1,
            "updated_at": utc_now_iso(),
            "students": students,
        }

        temporary = self.students_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.students_file)
        restrict_permissions(self.students_file)

    def _embedding_file(self, student_id):
        return self.directory / ("%s.npz" % check_student_id(student_id))

    # -- student metadata --------------------------------------------------

    def save_student(self, student_id, name, roll_number, class_name, section,
                     model_name, model_version, embedding_id=None,
                     created_at=None, extra=None):
        """
        Create or update a student record. Returns the stored metadata.

        No image is accepted or stored by this method, by design.
        """
        check_student_id(student_id)

        students = self._read_all()
        existing = students.get(student_id, {})

        record = {
            "student_id": student_id,
            "name": str(name),
            "roll_number": str(roll_number),
            "class_name": str(class_name),
            "section": str(section),
            "embedding_id": embedding_id or ("%s-embeddings" % student_id),
            "created_at": created_at or existing.get("created_at") or utc_now_iso(),
            "updated_at": utc_now_iso(),
            "model_name": model_name,
            "model_version": model_version,
            "sample_count": existing.get("sample_count", 0),
        }

        if extra:
            # Used for model provenance (file name, sha256, dimension).
            # Rejects anything image-shaped so a photo cannot sneak in here.
            for key, value in extra.items():
                if key in ("image", "photo", "face_image", "crop", "frame"):
                    raise ValueError(
                        "refusing to store %r - raw images are never stored" % key)
                record[key] = value

        students[student_id] = record
        self._write_all(students)

        return record

    def get_student(self, student_id):
        """Return one student's metadata. Raises StudentNotFound if absent."""
        check_student_id(student_id)
        students = self._read_all()

        if student_id not in students:
            raise StudentNotFound("no enrolled student with id %r" % student_id)

        return students[student_id]

    def has_student(self, student_id):
        return check_student_id(student_id) in self._read_all()

    def list_students(self):
        """All student metadata, sorted by id. Never includes embeddings."""
        students = self._read_all()
        return [students[key] for key in sorted(students)]

    def delete_student(self, student_id):
        """
        Remove a student completely: metadata AND embedding vectors.

        Returns True if something was deleted. This is the mechanism behind
        the "a student can be removed" privacy requirement, so it must never
        leave the vectors behind.
        """
        check_student_id(student_id)
        students = self._read_all()

        existed = student_id in students
        if existed:
            del students[student_id]
            self._write_all(students)

        embedding_file = self._embedding_file(student_id)
        if embedding_file.exists():
            embedding_file.unlink()
            existed = True

        return existed

    # -- embeddings --------------------------------------------------------

    def save_embedding(self, student_id, embeddings, model_info):
        """
        Store one student's embedding vectors.

        embeddings  - a sequence of vectors, or a single vector
        model_info  - the dict from SFaceEmbedder.model_info()

        Stored as .npz, a compact binary format built for numeric arrays.
        """
        check_student_id(student_id)

        vectors = np.asarray(embeddings, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        if vectors.ndim != 2:
            raise ValueError(
                "embeddings must be a 2D array of vectors, got shape %s"
                % (vectors.shape,))

        expected_dim = int(model_info["embedding_dim"])
        if vectors.shape[1] != expected_dim:
            raise ValueError(
                "embeddings must have %d numbers each, got %d"
                % (expected_dim, vectors.shape[1]))

        if not np.all(np.isfinite(vectors)):
            raise ValueError("embeddings contain NaN or infinite values")

        self._ensure_directory()
        path = self._embedding_file(student_id)

        np.savez_compressed(
            path,
            embeddings=vectors,
            student_id=np.array(student_id),
            model_name=np.array(model_info["model_name"]),
            model_version=np.array(model_info["model_version"]),
            model_sha256=np.array(model_info.get("model_sha256", "")),
            embedding_dim=np.array(expected_dim),
            normalization=np.array(model_info.get("normalization", "l2")),
            saved_at=np.array(utc_now_iso()),
        )
        restrict_permissions(path)

        # Keep the metadata in step with what was actually written.
        students = self._read_all()
        if student_id in students:
            students[student_id]["sample_count"] = int(vectors.shape[0])
            students[student_id]["updated_at"] = utc_now_iso()
            self._write_all(students)

        return path

    def load_embeddings(self, student_id):
        """
        Load one student's vectors.

        Returns (vectors, info) where vectors has shape (samples, dimension)
        and info records which model produced them.
        """
        check_student_id(student_id)
        path = self._embedding_file(student_id)

        if not path.exists():
            raise StudentNotFound(
                "no stored embeddings for student id %r" % student_id)

        with np.load(path, allow_pickle=False) as archive:
            vectors = archive["embeddings"].astype(np.float32)
            info = {
                "model_name": str(archive["model_name"]),
                "model_version": str(archive["model_version"]),
                "model_sha256": str(archive["model_sha256"]),
                "embedding_dim": int(archive["embedding_dim"]),
                "normalization": str(archive["normalization"]),
                "saved_at": str(archive["saved_at"]),
            }

        return vectors, info

    # -- auditing ----------------------------------------------------------

    def audit(self):
        """
        A summary of what is stored, for review.

        Deliberately reports counts and model provenance only. It never
        returns or prints a vector.
        """
        rows = []
        for student in self.list_students():
            path = self._embedding_file(student["student_id"])
            rows.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "class_name": student.get("class_name", ""),
                "section": student.get("section", ""),
                "created_at": student.get("created_at", ""),
                "model": "%s/%s" % (student.get("model_name", "?"),
                                    student.get("model_version", "?")),
                "sample_count": student.get("sample_count", 0),
                "embeddings_present": path.exists(),
            })
        return rows


def main():
    """Small command line view of the store: list, audit, or delete."""
    from paths import enable_safe_console
    enable_safe_console()

    store = EmbeddingStore()
    command = sys.argv[1] if len(sys.argv) > 1 else "list"

    if command in ("list", "audit"):
        rows = store.audit()
        if not rows:
            print("No students enrolled yet.")
            print("Enroll one with:  python ai/enroll_student.py")
            return 0

        print("%-12s %-20s %-10s %-8s %-16s %s" %
              ("STUDENT_ID", "NAME", "CLASS", "SAMPLES", "MODEL", "ENROLLED"))
        print("-" * 86)
        for row in rows:
            print("%-12s %-20s %-10s %-8d %-16s %s" % (
                row["student_id"][:12], row["name"][:20],
                ("%s-%s" % (row["class_name"], row["section"]))[:10],
                row["sample_count"], row["model"][:16], row["created_at"]))
        print("\n%d student(s) enrolled. Embedding vectors are never displayed."
              % len(rows))
        return 0

    if command == "delete":
        if len(sys.argv) < 3:
            print("Usage: python ai/embedding_store.py delete <student_id>")
            return 1
        student_id = sys.argv[2]
        if store.delete_student(student_id):
            print("Deleted student %r and their stored embeddings." % student_id)
            return 0
        print("No student found with id %r." % student_id)
        return 1

    print("Usage: python ai/embedding_store.py [list|audit|delete <student_id>]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
