"""
ClassVision AI - Dashboard Backend (Step 7)
-------------------------------------------
    Step 7 is a local teacher/admin dashboard and deterministic analytics
    layer.

    Local password authentication IS implemented (PBKDF2 hashing,
    session cookies, SUPER_ADMIN / TEACHER roles enforced server-side).
    It has NOT been independently reviewed and is not evaluated
    production security.

    Attendance accuracy has NOT been validated on a real classroom population.

This package serves read-mostly views of data that Steps 1-6 already produced.
It makes no attendance decisions of its own: the rule engine decides, the
database stores, and this layer reports.

Import path note
    ai/ and database/ are plain module directories, not installed packages,
    so they are put on sys.path here - before the project root.

    Order matters. The project root contains a directory called "database",
    which Python would otherwise treat as a namespace package and import
    INSTEAD of database/database.py. Putting database/ first makes
    "import database" resolve to the module, which is what every existing
    Step 6 module already expects.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AI_DIR = PROJECT_ROOT / "ai"
DATABASE_DIR = PROJECT_ROOT / "database"

for directory in (DATABASE_DIR, AI_DIR):
    path = str(directory)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

__all__ = ["PROJECT_ROOT", "AI_DIR", "DATABASE_DIR"]
