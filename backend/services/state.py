"""
ClassVision AI - Backend State (Step 7)
---------------------------------------
Holds the database connections the API uses.

One connection PER THREAD, not one shared connection
    SQLite connections are bound to the thread that created them, and FastAPI
    runs synchronous endpoints in a worker threadpool. A single shared
    connection therefore fails the moment a second thread serves a request:

        sqlite3.ProgrammingError: SQLite objects created in a thread can only
        be used in that same thread

    Rather than disable that check and add locking, each worker thread gets its
    own connection, created on first use and reused afterwards. The threadpool
    is small and long-lived, so this settles at a handful of connections
    against one local file. WAL mode (set in database.py) handles concurrent
    readers well.

Kept in its own module so tests can redirect the entire application at a
temporary database with one call, and so no route ever opens its own
connection.
"""

import os
import threading
from pathlib import Path

from database import DEFAULT_DATABASE_PATH, Database

# Lets the backend be pointed at another database file without editing code -
# used to view the demo data without touching the real database.
DATABASE_ENV_VAR = "CLASSVISION_DB"

from backend.services.analytics_service import AnalyticsService
from backend.services.data_service import DataService

_local = threading.local()
_database_path = None
_schema_ready = False
_lock = threading.Lock()


def configure(path=None):
    """
    Point the backend at a database. Call before serving requests.

    Tests use this to redirect the application at a temporary file, which is
    why nothing else may hold its own connection.
    """
    global _database_path, _schema_ready

    close_database()

    with _lock:
        _database_path = path
        _schema_ready = False

    return get_database()


def current_path():
    """
    Which database file to use.

    An explicit configure() wins (tests), then the CLASSVISION_DB environment
    variable, then the project default.
    """
    if _database_path is not None:
        return _database_path

    from_environment = os.environ.get(DATABASE_ENV_VAR)
    if from_environment:
        return Path(from_environment)

    return DEFAULT_DATABASE_PATH


def get_database():
    """This thread's connection, opened on first use."""
    global _schema_ready

    database = getattr(_local, "database", None)
    expected = current_path()

    # A reconfigure (in tests) must not leave a thread holding the old file.
    if database is not None and database.path != expected:
        database.close()
        database = None

    if database is None:
        database = Database(expected)
        database.connect()

        # Create the schema once per process, so the dashboard starts cleanly
        # against a fresh checkout instead of failing on a missing table. This
        # never deletes or modifies existing data.
        with _lock:
            if not _schema_ready:
                database.initialize()
                _schema_ready = True

        _local.database = database

    return database


def close_database():
    """Close this thread's connection, if it has one."""
    database = getattr(_local, "database", None)
    if database is not None:
        database.close()
        _local.database = None


def get_data_service():
    return DataService(get_database())


def get_analytics_service():
    return AnalyticsService(get_database())
