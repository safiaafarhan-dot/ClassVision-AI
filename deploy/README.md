# Running ClassVision AI on a real machine

Step 13. Three tools and a set of instructions, for the moment the project
stops being something that runs on a developer's laptop and starts being
something a school leaves switched on.

Nothing here is a product installer. This is a local appliance: it binds to
localhost, keeps its data in `data/`, and makes no request that leaves the
machine.

---

## Before a lesson: `preflight.py`

```
python deploy/preflight.py
python deploy/preflight.py --camera          # also open the camera
python deploy/preflight.py --camera-index 1
```

Every failure it catches has a diagnosis that is obvious afterwards and
invisible beforehand — a model that was never downloaded, a `data/` directory
that is read-only, a schema a version behind, a camera index nobody checked.
All of them arrive at the same moment, the start of a lesson in front of a
class, and all of them look like *the camera is broken*.

**A skipped check is never counted as a pass.** Each check reports `OK`,
`PROBLEM`, or `SKIPPED` **with the reason it was skipped**, and the exit code
is non-zero only for `PROBLEM`. A machine with no webcam attached is honest
about the camera rather than failing the whole run, and a successful run says
how many checks proved nothing.

It creates no file, writes no row, and downloads nothing. The one thing it does
change is the database schema — opening the database runs the additive
migration, which is deliberate: an upgrade that is going to fail should fail
here, with nobody waiting.

---

## Every day: `backup.py`

```
python deploy/backup.py                  # into backups/<timestamp>/
python deploy/backup.py --into D:/backups
python deploy/backup.py --staff          # include personnel documents
```

**Do not back this project up by copying `classvision.db` in the file
explorer.** The database runs in WAL mode, so at any moment some committed
transactions live in `classvision.db-wal` and not in `classvision.db`. A copy
of the `.db` alone opens cleanly and is missing the most recent lessons — on a
young database it does not even have the tables yet. That is the worst
possible failure, because it looks like a backup.
`tests/test_deployment.py` reproduces it deliberately.

This tool uses SQLite's own backup API, which takes a consistent snapshot of a
live database including everything in the WAL, without stopping the server. It
then **opens the copy** and prints its row counts beside the live ones. A
backup nobody has opened is a hypothesis.

What is copied:

| | |
|---|---|
| `data/classvision.db` | through the backup API, as one consistent file |
| `data/embeddings/` | the face vectors, as files |
| `data/staff/` | **only with `--staff`** |

Personnel documents are opt-in because backing up identity documents is a
different decision from backing up attendance, and the default does not make it
quietly. Nothing is uploaded anywhere; this writes to a directory you name on a
disk you chose, and the module imports no network client.

A backup never overwrites an existing one — the usual way to lose data to a
backup tool is for it to half-write over the only good copy.

### Restoring

Deliberately manual, and deliberately not automated by a script:

1. Stop the server.
2. Move the current `data/classvision.db`, `-wal` and `-shm` files aside.
   **Move, do not delete.** The file you are replacing may be the better one.
3. Copy `classvision.db` from the backup into `data/`.
4. Copy the backup's `embeddings/` over `data/embeddings/`.
5. Run `python deploy/preflight.py`. It brings the schema forward and tells you
   what it found.
6. Start the server and look at the Reports tab before telling anyone it worked.

A restore throws away whatever happened after the backup was taken. That is a
decision for a person, which is why there is no `--restore` flag.

**Step 2 is the one that fails silently if you skip it.** Replacing
`classvision.db` while the old `-wal` is still beside it does not fail, does
not warn, and does not restore: both files descend from the same database, so
the stale log is replayed over the pages you just put there and the
installation comes back holding exactly the rows you were rolling back.
`tests/test_restore.py` measures it both ways on one fixture - 8 records back
when only the `.db` is replaced, 4 when all three files are moved aside.

That suite performs these six steps end to end against a temporary
installation on every run, and reads the steps **out of this file, in order**,
so a procedure edited here and nowhere else fails the test rather than
quietly diverging from the one that has been shown to work. What it does not
do is rehearse a restore on your machine, from a backup carried in on another
disk. Do that once, before you need to.

---

## Attendance recorded while the network was down

There is nothing to configure, and nothing to do. Attendance is written to
local SQLite; the network is not in that path at all.

What the outbound queue (`/api/sync`) is for is getting a **copy** of that
attendance somewhere else. **Nothing in this project has ever synchronised to a
real server.** The default target is configured with nowhere to send anything
and delivers nothing, which is the honest behaviour of an installation that has
not been given an upstream.

So a queue with items in it means **nothing has been lost** — only that nothing
has been sent. Preflight reports it that way rather than as a fault.

If an upstream is ever built, the pieces it needs are already here and tested:
every item carries an idempotency key derived from what it *is*, so a retry
cannot deliver a term of attendance twice; sessions are presented before the
attendance that references them; a partial acknowledgement marks only what was
acknowledged; and an item that keeps failing becomes `ABANDONED` and **stays in
the table** with its last error, rather than being retried forever or thrown
away.

---

## Running it as a service on Windows

The server is `uvicorn backend.app:app`. For an appliance that should come back
after a power cut, the simplest reliable arrangement is a **Scheduled Task**
that runs at startup:

* Trigger: *At startup*
* Action: `C:\path\to\ClassVision-AI\.venv\Scripts\python.exe`
* Arguments: `-m uvicorn backend.app:app --host 127.0.0.1 --port 8000`
* Start in: `C:\path\to\ClassVision-AI`
* Run whether the user is logged on or not, and restart on failure.

Two things to get right:

**`--host 127.0.0.1`, not `0.0.0.0`.** The authentication here is real but has
not been independently reviewed, and this is not evaluated production security.
Binding it to the network is a decision nobody should make by leaving a default
alone.

**The working directory matters.** `data/`, `models/` and `backups/` are
resolved from the project root.

Add a second Scheduled Task for the backup, daily, out of lesson hours. A backup
that depends on somebody remembering is not a backup.

---

## The paths that will bite you

This project is developed in a directory whose path contains non-ASCII
characters, and the main `README.md` has a section on it. For a deployment,
put the project somewhere plain — `C:\ClassVision-AI` — and the whole class of
problem disappears.

`backend/static/*` must keep **LF** line endings: a test compares the served
HTML to the source file byte for byte, and a checkout that rewrote them to CRLF
fails it.

---

## What none of this makes true

Packaging and a backup do not make the pipeline validated. Every limitation the
main `README.md` records still stands: the recognition threshold is still
provisional, liveness is still a research prototype that has not been evaluated
against real presentation attacks, video authenticity is still a baseline that
is off by default, and **attendance accuracy has still not been validated on a
real classroom population**.

A machine that passes preflight is a machine whose files and database are in
order. It is not a machine whose attendance figures have been shown to be right.
