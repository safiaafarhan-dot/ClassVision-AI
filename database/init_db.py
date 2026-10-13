"""
ClassVision AI - Database Initialization (Step 6)
-------------------------------------------------
Creates the attendance database and its tables.

    python database/init_db.py
    python database/init_db.py --path data/somewhere-else.db
    python database/init_db.py --show

Safe to run as often as you like. Every statement is CREATE ... IF NOT EXISTS,
so re-running verifies the schema and changes nothing.

It will NEVER drop a table, delete a row, or reset anything. If the schema
ever needs to change destructively, that will be a deliberate migration
script, not a flag on this one.

The database holds attendance decisions only - no images, no video, no
embeddings. Biometric data stays in the Step 3 store, which this file does not
touch and cannot reach.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ai"))

from database import DEFAULT_DATABASE_PATH, Database  # noqa: E402
from paths import enable_safe_console, relative  # noqa: E402


def describe(database):
    """Print what is actually in the database."""
    print("  path            : %s" % relative(database.path))
    print("  schema version  : %s" % database.schema_version())
    print("  foreign keys    : %s"
          % ("enforced" if database.foreign_keys_enabled() else "OFF"))

    tables = database.table_names()
    print("  tables          : %d" % len(tables))

    for table in tables:
        count = database.query_one(
            "SELECT COUNT(*) AS total FROM %s" % table)["total"]
        print("      %-28s %6d row(s)" % (table, count))


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Create or verify the ClassVision AI attendance database. "
                    "Never deletes data.")
    parser.add_argument("--path", default=None,
                        help="database file (default: data/classvision.db)")
    parser.add_argument("--show", action="store_true",
                        help="only report what is there; create nothing new")
    args = parser.parse_args()

    path = Path(args.path) if args.path else DEFAULT_DATABASE_PATH
    existed = path.exists()

    database = Database(path)

    try:
        database.connect()

        if args.show:
            if not existed:
                print("No database at %s yet." % relative(path))
                print("Create it with:  python database/init_db.py")
                return 1
            print("ClassVision AI attendance database\n")
            describe(database)
            return 0

        database.initialize()

        print("ClassVision AI attendance database %s\n"
              % ("verified" if existed else "created"))
        describe(database)

        if not existed:
            print("\nThis database stores attendance decisions only.")
            print("No images, video, or face embeddings are ever written here.")
        print("\nSafe to re-run. No existing data was modified.")
        return 0

    except Exception as error:
        print("ERROR: %s: %s" % (type(error).__name__, error))
        return 1

    finally:
        database.close()


if __name__ == "__main__":
    sys.exit(main())
