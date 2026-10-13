# ClassVision AI

AI-powered classroom attendance and presence monitoring platform.

---

## Project Purpose

Most classroom attendance tools answer one weak question: *was this face seen
once?* ClassVision AI is being built around a stronger one — **how long was each
student actually present?**

The core record is a *presence timeline* per student, not a checkbox. Attendance
is then derived from that timeline by a rule the institution controls. That one
design choice is what lets the project grow into a real classroom-management
platform with analytics and reporting, instead of a single-purpose script.

The full design intent — including the privacy model, anti-proxy approach, and
seat-map awareness — is in [`docs/VISION.md`](docs/VISION.md).
The build order is in [`docs/ROADMAP.md`](docs/ROADMAP.md).

> The project is being built step by step. **Steps 1-10 exist today:** a
> verified camera pipeline, face *detection*, privacy-preserving student
> *enrollment*, live *identity verification*, multi-person *tracking* with
> a *presence timeline*, configurable *attendance rules* persisted to
> SQLite, a local *teacher dashboard* with deterministic analytics, a
> *live classroom session* that runs the whole pipeline from the webcam and
> shows presence on that dashboard as it happens, and a *liveness / PAD*
> layer that asks whether a face came from a person in the room, and a
> *video authenticity* layer that asks whether the video came from this
> camera at all.
> Local password authentication IS implemented (PBKDF2 hashing, session cookies, SUPER_ADMIN / TEACHER roles enforced server-side); it has not been independently reviewed and is not evaluated production security. Attendance accuracy has NOT been
> validated on a real classroom population, and BOTH the liveness and
> authenticity layers are RESEARCH BASELINES that have NOT been evaluated
> against real presentation attacks or real manipulated video.

---

## Current Status — Steps 1-10 Complete, plus Accounts and the Student Registry

> Since Step 10 the project has become a single signed-in application: local
> password authentication with SUPER_ADMIN and TEACHER roles, and SQLite as the
> single source of truth for the student registry. See
> **[Signing In](#signing-in--accounts-roles-and-the-first-run)** and
> **[The Student Registry](#the-student-registry--sqlite-as-the-single-source-of-truth)**.
> The AI pipeline is untouched: YuNet, SFace, tracking, recognition stability,
> liveness and the authenticity baseline all behave exactly as they did.

### Step 1 — Foundation + Camera Pipeline ✅

Project structure, virtual environment, and `ai/camera_test.py`, a camera
diagnostics tool reporting resolution, live FPS, and dropped frames.

### Step 2 — Face Detection ✅

> **Step 2 detects faces but does not identify people or mark attendance.**

The system can now answer *"where are the faces in this frame?"* — and only
that. It draws a box and a confidence score around each face it finds.

- `ai/face_detector.py` — the detector, behind a swappable interface
- `ai/face_detection_test.py` — live webcam demo + benchmark mode
- `ai/download_models.py` — fetches the model (227 KB) with SHA-256 verification
- `ai/paths.py` — shared paths and console-safety helpers

### Step 3 - Enrollment + Face Embeddings

> **Step 3 creates and stores biometric embeddings but does not perform
> classroom attendance.**

A student can be registered: the camera captures several good views, each
becomes a 128-number embedding, and only those numbers are stored. No
photograph is ever written to disk.

- `ai/face_alignment.py` - aligns a face to 112x112 using YuNet's landmarks
- `ai/face_embedding.py` - SFace embeddings, L2-normalised
- `ai/face_quality.py` - explainable enrollment quality gate
- `ai/embedding_store.py` - local store; swapped for PostgreSQL in Step 6
- `ai/enroll_student.py` - the enrollment CLI

### Step 4 - Live Recognition + Identity Verification

> **Step 4 performs identity verification only. It does not mark attendance or
> calculate presence duration.**

A detected face is matched against the enrolled students and reported as a
named MATCH or as UNKNOWN. Multiple faces are handled independently.

- `ai/face_recognizer.py` - matching engine, best-of-five, UNKNOWN handling
- `ai/recognition_stability.py` - anti-flicker filter (placeholder for Step 5)
- `ai/recognition_test.py` - live webcam demo + benchmark
- `ai/evaluate_threshold.py` - measures a threshold from labelled data

### Step 5 - Tracking + Presence Timeline

> **Step 5 does not calculate attendance.**

Each person is followed across frames with a stable track id, recognised only
when it is worth doing, and recorded as a timeline of presence events.

- `ai/person_tracker.py` - ByteTrack-style tracking-by-detection, no new deps
- `ai/track_identity.py` - identity per track, switch protection, scheduling
- `ai/presence_timeline.py` - presence states and events
- `ai/tracking_test.py` - live demo + benchmark

### Step 6 - Attendance Rules + Database

> **Step 6 introduces deterministic attendance rules and SQLite persistence.**
> **Attendance accuracy has NOT been validated on a real classroom population.**

The presence timeline is turned into an explainable attendance decision by a
configurable policy, and stored with the evidence behind it.

- `ai/attendance_rules.py` - policy, duration maths, states, rule engine
- `ai/attendance_session.py` - session model and lifecycle
- `database/database.py` - SQLite schema and connection handling
- `database/repositories.py` - all SQL, and nowhere else
- `database/init_db.py` - safe, repeatable initialization

### Step 7 - Teacher Dashboard + Analytics

> **Step 7 is a local teacher/admin dashboard and deterministic analytics
> layer. Local password authentication IS implemented (PBKDF2 hashing, session cookies, SUPER_ADMIN / TEACHER roles enforced server-side); it has not been independently reviewed and is not evaluated production security.**

A FastAPI backend serves the attendance data to a dependency-free web
dashboard, with deterministic analytics, a presence timeline view, per-record
evidence, and manual corrections.

- `backend/app.py` - FastAPI application (16 operations; Step 8 adds 4)
- `backend/routes/` - HTTP surface only, no SQL
- `backend/services/` - data + analytics services
- `backend/schemas/` - explicit response models
- `backend/static/` - the dashboard (HTML/CSS/JS, 71.1 KB, no CDN)
- `backend/seed_demo.py` - fictional demo data, separate database only

### Step 8 - Live Classroom Session + Real-Time Dashboard

> **Step 8 runs the existing pipeline against a live camera. It adds no
> model, changes no threshold, and decides no attendance.**

The camera, the pipeline, the database and the dashboard are finally one
system. A teacher starts a session, watches presence build up live, and stops
it - at which point the Step 6 rule engine runs once and writes the result.

- `ai/live_camera.py` - open / read / release, with configuration
- `ai/live_session.py` - runtime state machine + background camera worker
- `ai/live_pipeline.py` - coordinates the Step 5 pipeline, shapes a safe view
- `backend/services/live_service.py` - lifecycle, refusals, finalisation
- `backend/routes/live.py` - four HTTP operations
- `ai/live_benchmark.py` - synthetic measurement of what Step 8 added

### Step 9 - Liveness / Presentation Attack Detection

> **Step 9 is a RESEARCH PROTOTYPE. It is not production-grade presentation
> attack detection and is not a security control. Its behaviour on real
> presentation attacks has NOT been established - accuracy, FAR, FRR, APCER,
> BPCER and ACER are all "Not evaluated".**

An independent evidence source that asks whether a tracked face came from a
person in the room. It never says who anybody is, and recognition never says
whether anybody is live.

- `ai/liveness_detector.py` - three temporal cues, plain numpy, no model
- `ai/liveness_stability.py` - per-track temporal confirmation
- `ai/attendance_evidence.py` - the eligibility layer in front of Step 6
- `ai/liveness_test.py` - calibration, benchmark and live webcam demo

Enforcement is **off by default**: the layer reports what it would have held
back and changes no attendance until `CLASSVISION_LIVENESS_MODE=enforce`.

### Step 10 - Video Authenticity Analysis

> **Step 10 is a RESEARCH BASELINE, not a deepfake detector. It has never
> been evaluated against real manipulated video - every accuracy metric is
> "Not evaluated" - and it is OFF by default.**

A third independent evidence source, asking whether a face region was captured
by this camera or produced by a different process. Identity, liveness and
authenticity are reported as three separate columns and can hold any
combination.

- `ai/authenticity_detector.py` - three temporal cues, plain numpy, no model
- `ai/authenticity_stability.py` - per-track temporal confirmation
- `ai/authenticity_test.py` - calibration, benchmark and live webcam demo
- the Step 9 evidence layer gained a second, independent gate

**Built since:** live video in the dashboard (Step 8), notifications
(Step 11/12), reports and exports with CSV and PDF (Step 11), the assistant over
analytics (Step 12), and the offline queue with packaging and backup (Step 13).

**Still not built, and not claimed:** multi-camera operation is architectural
rather than demonstrated, no message has ever reached a parent, no record has
ever reached an upstream server, and there is no cloud deployment. This is a
local appliance.

---

## Detector Choice — YuNet

| | |
|---|---|
| **Model** | `face_detection_yunet_2023mar.onnx` (YuNet, 2023-03) |
| **Size** | 227 KB |
| **API** | `cv2.FaceDetectorYN` — built into OpenCV 5.0.0 |
| **Extra dependencies** | **None** |
| **Source** | [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo) |

**Why this detector, and not the usual one:**

1. **Haar cascades were not an option.** `cv2.CascadeClassifier` **does not
   exist in OpenCV 5.0.0** — it was removed. Most tutorials still teach it, but
   it cannot run in this environment at all. This was verified, not assumed.
2. **No new dependency.** YuNet ships inside the OpenCV already installed.
   MediaPipe would have added a large dependency tree for no measured gain.
3. **Confidence scores per face**, which Step 5 will need to weigh evidence.
4. **Small and angled faces**, which is the classroom case — students sit far
   from the camera, not centred like a selfie.
5. **Step 3 stays dependency-free too:** OpenCV 5 also ships
   `cv2.FaceRecognizerSF` (SFace), YuNet's matching recogniser.

---

## How to Run Face Detection

### 1. Download the model (once)

```powershell
python ai/download_models.py
```

Downloads to `models/` and verifies the SHA-256. `models/` is gitignored, so
the file is never committed. Re-running just re-verifies and skips.

### 2. Run the live detector

```powershell
python ai/face_detection_test.py
```

**Controls**

| Key | Action |
|-----|--------|
| `Q` or `ESC` | quit cleanly |
| `H` | hide / show the info panel |
| `C` | hide / show confidence labels |

**Options**

```powershell
python ai/face_detection_test.py --threshold 0.5      # catch more distant faces
python ai/face_detection_test.py --width 1280 --height 720
python ai/face_detection_test.py --benchmark          # measure, no window
python ai/face_detection_test.py --help
```

### Expected output

A window titled **"ClassVision AI - Face Detection Test"** showing your webcam
feed with a green box and a confidence percentage around each face, and a panel
reading:

```
ClassVision AI - Face Detection (Step 2)
Faces: 1
FPS: 12.9   detect: 11.5 ms
640x480   no identification
```

No names are ever shown, because Step 2 does not know who anyone is.

### Run the tests (no webcam needed)

```powershell
python tests/run_all.py               # all 49 suites, 1100 tests
```

Or one suite at a time:

```powershell
python tests/test_camera_test.py      # Step 1 - 4 tests
python tests/test_face_detector.py    # Step 2 - 17 tests
python tests/test_face_alignment.py   # Step 3 - 12 tests
python tests/test_face_embedding.py   # Step 3 - 15 tests
python tests/test_embedding_store.py  # Step 3 - 18 tests
python tests/test_face_recognizer.py  # Step 4 - 18 tests
python tests/test_recognition_stability.py  # Step 4 - 16 tests
python tests/test_person_tracker.py    # Step 5 - 21 tests
python tests/test_track_identity.py    # Step 5 - 21 tests
python tests/test_presence_timeline.py # Step 5 - 20 tests
python tests/test_attendance_rules.py  # Step 6 - 36 tests
python tests/test_attendance_database.py    # Step 6 - 29 tests
python tests/test_attendance_end_to_end.py  # Step 6 - 10 tests
python tests/test_dashboard_analytics.py   # Step 7 - 18 tests
python tests/test_dashboard_api.py         # Step 7 - 25 tests
python tests/test_dashboard_frontend.py    # Step 7 - 18 tests
python tests/test_live_session.py          # Step 8 - 19 tests
python tests/test_live_api.py              # Step 8 - 28 tests
python tests/test_liveness_detector.py     # Step 9 - 23 tests
python tests/test_liveness_stability.py    # Step 9 - 25 tests
python tests/test_liveness_integration.py  # Step 9 - 24 tests
python tests/test_authenticity_detector.py    # Step 10 - 25 tests
python tests/test_authenticity_stability.py   # Step 10 - 25 tests
python tests/test_authenticity_integration.py # Step 10 - 27 tests
```

---

## Step 2 Performance — Measured on This Machine

Real numbers from `--benchmark` (8 s per resolution), not estimates:

| Resolution | FPS with detection | Detect avg | Detect p95 | Frames | Dropped |
|---|---|---|---|---|---|
| **640×480** | **14.9** | **10.2 ms** | 15.4 ms | 120 | 0 |
| 1280×720 | 10.0 | 31.7 ms | 43.0 ms | 80 | 0 |

Live run at 640×480: 12.9 FPS, 11.5 ms average detect, p50 12.3 ms / p95
15.1 ms, 81 frames, **0 dropped**.

**640×480 remains the default.** 720p costs about 3× the detection time per
frame and cuts the frame rate by a third. Detection is only ~10 ms of a ~67 ms
frame at 640×480, so the camera is still the bottleneck, not the model — there
is real headroom left for Steps 3–5.

### Threshold tuning

Detections at different confidence thresholds, on OpenCV sample photos plus
three non-face control images:

| Image | t=0.3 | t=0.5 | t=0.6 | t=0.7 | t=0.8 | t=0.9 |
|---|---|---|---|---|---|---|
| `lena.jpg` (1 clear face) | 1 | 1 | 1 | 1 | 1 | 1 |
| `messi5.jpg` (several people) | 12 | 3 | 1 | 1 | 1 | 1 |
| `basketball1.png` (several people) | 3 | 2 | 1 | 1 | 1 | 0 |
| black frame (no face) | 0 | 0 | 0 | 0 | 0 | 0 |
| random noise (no face) | 0 | 0 | 0 | 0 | 0 | 0 |
| `right.jpg` (no face) | 0 | 0 | 0 | 0 | 0 | 0 |

Reading: the non-face controls stayed at zero at **every** threshold, while
multi-person photos needed **0.5 or lower** to pick up smaller background
faces. 0.3 clearly over-detects. The default is **0.6**; try `--threshold 0.5`
for students sitting in back rows.

> These are detection *counts*, not an accuracy score. No accuracy percentage
> is claimed, because that requires a labelled dataset this project does not
> have.

---

## Step 2 Limitations

- **No identity.** It finds faces; it cannot tell people apart.
- **No accuracy figure.** Counts only — no labelled dataset, no benchmark score.
- **Not validated on a real classroom.** Verified on one webcam and three
  sample photos. Real classroom conditions — many faces, back rows, poor
  lighting — are untested.
- **Live multi-face and lighting conditions are unverified by me.** Nobody was
  in front of the camera during the automated runs (`frames with faces: 0`).
  Detection is proven on static photos; see the checklist below to confirm live.
- **Single camera, single frame.** No tracking, so the same face in consecutive
  frames is unrelated detections.
- **Frontal-ish faces only.** A face turned well away from the camera is missed.

### Please verify these yourself

I cannot sit in front of your webcam, so these remain unconfirmed. Run
`python ai/face_detection_test.py` and check:

- [ ] One face — a box tracks your face as you move
- [ ] Two or more people in frame — `Faces:` counts correctly
- [ ] Distance — step back; note where it stops detecting, retry `--threshold 0.5`
- [ ] Angles — turn your head slowly; note the angle where the box drops
- [ ] Low light — dim the lights and compare
- [ ] Partial face — cover part of your face with a hand

---

---

## Step 3 — How Enrollment Works

```
student -> camera -> face detection -> exactly one face required
        -> quality gate -> alignment (112x112) -> embedding (128 numbers)
        -> L2 normalisation -> stored as a vector
                                   |
        frame discarded, never written to disk
```

### Run it

```powershell
python ai/download_models.py          # once - adds the SFace model (36.9 MB)
python ai/enroll_student.py
```

You are asked for the student's details, then guided through five poses:

```
Student ID: S101
Name: Asha R
Roll number: 23
Class: 10
Section: A

>>> Look straight at the camera.
  Sample 1/5 captured.
>>> Turn your head slightly LEFT.
  Sample 2/5 captured.
...
Enrollment completed.
```

Options:

```powershell
python ai/enroll_student.py --samples 3
python ai/enroll_student.py --student-id S101 --name "Asha R" --roll 23 --class-name 10 --section A
python ai/enroll_student.py --replace          # overwrite an existing enrollment
```

### Review and delete enrollments

```powershell
python ai/embedding_store.py list              # never prints vectors
python ai/embedding_store.py delete S101       # removes metadata AND vectors
```

---

## Enrolling Students from the Dashboard

Face enrollment used to be command-line only, and that created a problem worth
describing because the fix is the interesting part.

### The problem: two stores, one key, no link

The project has always had two places a student exists:

```
data/embeddings/       student_id -> roll, class, section, face vectors
                       read by RECOGNITION  (Step 4's gallery)

data/classvision.db    student_id -> name, status, attendance
                       read by the DASHBOARD, sessions and attendance rules
```

They have always shared `student_id`. Nothing ever linked them.

So `python ai/enroll_student.py --student-id STU001 ...` wrote face vectors and
**no student row**. The camera could recognise that student perfectly well,
and the Students page said *"No students"*, and they could not be picked as an
expected student when scheduling a session. Two correct halves, no join.

**Enrollment now writes both**, from either path, and the Students page joins
them on `student_id`. A student enrolled from the command line appears on the
dashboard; a student enrolled from the dashboard is recognised by the camera.

### Add a student from the dashboard

```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> → **Students** → **+ Add Student**.

| Field | |
|---|---|
| Student ID | the stable identifier, e.g. `STU002`. Letters, digits, `_` and `-` only |
| Name | shown everywhere the student appears |
| Roll number | optional |
| Class | optional, e.g. `CSE(AI)` |
| Section | optional, e.g. `C` |
| Camera index | optional; defaults to `CLASSVISION_CAMERA_INDEX`, then `0` |

Then either:

- **Start Face Enrollment** — opens the camera and captures **5 samples**,
  the same number the command-line tool uses. Progress shows live:
  `Sample 1 / 5` … `Sample 5 / 5`. **Cancel enrollment** stops it and stores
  nothing.
- **Register without camera** — creates the student row only. They appear as
  **Not enrolled** and the camera cannot recognise them until face enrollment
  is completed. This is a real state, not a broken one.

On success the student list and the session roster both refresh, and the
student is immediately selectable under **Sessions → + Add Session → Expected
students**.

### What is captured, and what is stored

Five samples, each from a different pose, with the same rules the CLI applies —
they live in `ai/enrollment_rules.py` and **both paths import them**, so two
students enrolled on different days are held to the same standard:

| | |
|---|---|
| samples | 5 |
| consecutive good frames before a sample | 6 |
| cooldown between samples | 1.5 s |

**No photograph is taken or stored.** Each frame is detected (YuNet), quality-
checked, converted to a 128-number SFace embedding, and then dropped. What
reaches disk is:

```
data/embeddings/students.json   roll, class, section, sample count, model
data/embeddings/STU002.npz      the five embedding vectors
data/classvision.db             the student row: id, name, status
```

The browser never uploads a frame and never receives one. There is no image
element, no video element and no camera access in the dashboard at all — the
camera is read **server-side** and only counters and a state come back. A test
asserts each of those, and another asserts that a completed enrollment writes
exactly `students.json` and one `.npz` and no image of any kind.

### The Students page

| Column | |
|---|---|
| Student ID / Name | from the attendance database |
| Class / Roll | from the enrollment metadata |
| Face enrollment | **Enrolled** with a sample count, or **Not enrolled** |
| Status | ACTIVE / INACTIVE |

Three states are possible and all three are reported honestly:

- **registry row + enrollment** — enrolled and recognisable;
- **registry row, no enrollment** — shown as *Not enrolled*; the camera cannot
  recognise them;
- **enrollment, no registry row** — the Students page lists the database, so an
  orphan enrollment is **not** shown and **no student row is invented** for it.
  Nothing knows that person's status or when they were added, and guessing
  would be worse than saying nothing. Re-run the CLI enrollment to register it.

### The command line still works, unchanged

```powershell
python ai\enroll_student.py --student-id STU001 --name "Safia Farhan" --roll "001" --class-name "CSE(AI)" --section "C"
python ai\embedding_store.py list
```

The CLI keeps its camera window, its pose prompts and its consistency report.
What changed is that it now also registers the student in the attendance
database, and reports whether that succeeded:

```
  registry       : added to the attendance database
```

If the database cannot be reached the face enrollment is still saved and
recognition still works — the CLI says so and tells you to re-run rather than
discarding a capture the student just sat through.

### Troubleshooting camera enrollment

| What you see | What it means |
|---|---|
| *"A live classroom session is using the camera"* (409) | One webcam cannot serve a lesson and an enrollment at once. Stop the session from the Live classroom tab. |
| *"Camera N could not be opened"* (503) | Another application has it, or the index is wrong. Run `python ai/camera_test.py --list`. |
| *"No face detected - look at the camera"* | Capture is running and waiting. Move into frame and into the light. |
| *"More than one face"* | Enrollment needs exactly one person in frame. |
| *"STU002 is already enrolled"* (409) | Use a different id, or re-enrol to replace the existing samples. |
| Progress stuck at `0 / 5` | The quality gate is refusing every frame — usually too dark, too far away, or off-centre. |
| *"Enrollment timed out"* | Three minutes passed without five good samples. Nothing was stored. |

Nothing is stored until **all five** samples are captured. A cancelled, failed
or timed-out enrollment leaves no vectors and no half-written student.

### Then schedule a session

**Sessions → + Add Session**, pick the class, date and times, tick the newly
enrolled student under **Expected students**, and **Create session**. It is
then runnable from **Live classroom**, where the existing pipeline — YuNet,
tracking, SFace, recognition stability, presence timeline — recognises them by
the same `student_id`.

---

## Signing In — Accounts, Roles and the First Run

The dashboard used to say *"Authentication is not implemented."* That was true,
and saying so was better than a login box that protected nothing. It now has
one, and the disclaimer changed to describe what actually exists rather than
disappearing.

### What is actually protecting anything

| | |
|---|---|
| Passwords | PBKDF2-HMAC-SHA256, per-user random salt, 240,000 iterations. Standard library — **no new dependency**. |
| Sessions | 256-bit tokens from `secrets`. Only the SHA-256 is stored, so reading `user_sessions` does not let anybody sign in as somebody else. |
| Cookie | HttpOnly (no script can read it), SameSite=Lax, Secure when the request arrived over HTTPS. |
| Comparisons | `hmac.compare_digest`, so a wrong token takes as long to reject as a nearly-right one. |
| Roles | `SUPER_ADMIN` and `TEACHER`, enforced by FastAPI dependencies **before any data is read**. |

**What is not claimed.** This has not been penetration-tested or independently
reviewed. There is no two-factor authentication, no password-reset email, and
no rate limiting beyond a fixed delay on every failed attempt. It is real
authentication for a local classroom appliance. It is not evaluated production
security, and the dashboard says exactly that in its footer.

### The first run

A database with **no accounts in it is unconfigured**: the API stays open and
the dashboard shows a create-the-first-administrator form instead of a login
box.

That is the state every installation that predates this change is in. The
alternative — shipping a default administrator password — is worse in every
way that matters, because a default that nobody changes is the same as no
password at all. There is **no default account anywhere in this project**, and
a test asserts that `admin/admin` and friends are refused on a fresh database.

```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>, choose a username and a password of at least 10
characters, and you are signed in as the Super Admin. `POST /api/auth/setup`
then refuses forever: it can never mint a second administrator without
somebody signing in first.

### Super Admin — creating teachers

**Administration → + Add Teacher.** Username, name, password, role, and
optionally the classes they are responsible for.

```
Teacher:   s.kumar  (T Kumar)
Assigned:  CSE(AI) — Section C
```

- **Deactivate / Activate** — a deactivated account's open browser tab stops
  working at its *next request*, not whenever its token expires.
- **Delete account** — permanent. The audit trail survives it, because audit
  rows record a username as text rather than as a foreign key.
- **Change role** — a teacher can be promoted, an administrator demoted.

Two refusals you cannot talk the system out of: the **last active Super Admin**
cannot be deactivated, demoted or deleted, and **nobody can delete the account
they are signed in with**. An installation with no usable administrator can
only be repaired by editing the database by hand, which is the thing this whole
feature exists to stop people having to do.

### Teacher — what they can and cannot reach

| Teacher | Super Admin |
|---|---|
| Dashboard, Students, Sessions, Live classroom, Attendance, Analytics | all of that, plus **Administration** |
| Register, edit, enrol and **deactivate** students | create and manage teacher accounts, and **delete a student permanently** |
| Edit a session they scheduled wrongly | issue and revoke student portal logins |
| See a colleague's photograph | see and add staff personnel documents |
| Create, run and finalise sessions | assign classes and sections |
| See their assigned classes | see every class |

Hiding the Administration tab is a courtesy. The permission is the server's: a
teacher who calls `POST /api/users` by hand gets **403** and changes nothing,
and a test does exactly that for every administration endpoint.

### Class assignments

A teacher **with** assignments sees only those classes' students, sessions and
attendance. A teacher **with no** assignment is unrestricted within the teacher
role — somebody given an account but not yet a timetable should be able to
work, not stare at an empty dashboard and conclude the system is broken.

An assignment with no section covers every section of that class.

A student outside a teacher's classes returns **404, not 403** — a status code
that changed would let a teacher discover which student ids exist in a class
they may not see.

Sessions carry a free-text `class_id` (`CSE(AI)-C`, `cse(ai) c`, `CLASS-10A`)
while students carry `class_name` and `section` as separate columns. Both
spellings already existed in the data, so both sides are normalised —
uppercased and stripped to letters and digits — in one place,
`backend/services/authz.py`, so it can be replaced by a real class table later
without touching a route.

---

## The Student Registry — SQLite as the Single Source of Truth

### The problem this closes

Until schema version 2 a student row held an id, a name and a status. Their
roll number, class and section lived **only** in the Step 3 embedding store. So
opening `data/classvision.db` and running the obvious query answered *"who is
this person?"* with half an answer:

```
student_id | name         | status
STU001     | Safia Farhan | ACTIVE
```

Anyone inspecting the database had to know a second store existed, where it
was, and what format it used. That is a bad place for identifying metadata to
live.

**Version 2 moves it into the registry.** The same query now answers the
question completely:

```sql
SELECT student_id, name, roll_number, class_name, section, status
  FROM students
 ORDER BY student_id;
```

The embedding store keeps what it is actually for — the face vectors — and
answers exactly two further questions: does an enrollment exist for this id,
and how many samples does it hold.

### The migration

Three things, all additive:

1. **Schema.** `ALTER TABLE students ADD COLUMN roll_number / class_name /
   section`, each `TEXT NOT NULL DEFAULT ''`. `ADD COLUMN` is the one schema
   change SQLite makes without rewriting the table: existing rows keep their
   bytes and gain the default. Run automatically by `Database.initialize()`,
   so it happens on the next server start.
2. **Four new tables** for accounts: `users`, `user_class_assignments`,
   `user_sessions`, `audit_log`. Nothing existing is touched.
3. **Backfill.** `database/migrate_students.py` copies roll number, class and
   section out of existing enrollments and into the students they belong to.

```powershell
python database/migrate_students.py --dry-run   # report, write nothing
python database/migrate_students.py             # do it
```

It also runs on every server start, where it is a no-op once the database is
in step.

Two rules, both about not destroying anything:

- **SQLite wins.** A value already in the registry is never overwritten, even
  if the store disagrees. The registry is what a teacher edits, and an old
  enrollment record must not undo that edit.
- **Blanks only.** A column is written only when it is empty, so a second run
  does nothing — and so does a run after somebody corrects a class in the
  dashboard.

Nothing in the migration deletes a row, a column, a table, an attendance
record, a presence segment, an expected-student row or an embedding. The only
statements are `ALTER TABLE ADD COLUMN`, `INSERT` and a narrow `UPDATE`.

**Orphan enrollments** — a face enrollment with no registry row — are
*imported*, not invented: the store already holds that person's name, roll,
class and section, because the enrollment asked for them. A store record with
no name is **skipped and reported** rather than filled in with a placeholder.

### Every student, every path

The registry is general. There is no student-specific code anywhere in it, and
a test scans the registry modules for a literal student id in a comparison to
keep it that way.

| Path | What it writes |
|---|---|
| Dashboard **+ Add Student** | the full row: id, name, roll, class, section, status |
| Dashboard **Save & Enroll Face** | the full row, *then* the face samples |
| `ai/enroll_student.py` | the face samples, *then* the full row |
| Editing a student | the row, and the store's copy of the metadata to match |

Both enrollment paths share `ai/enrollment_rules.py`, `SampleCollector` and
`persist_enrollment` — there is one capture implementation and one persistence
implementation, not two.

```powershell
python ai\enroll_student.py --student-id STU002 --name "Rahul Kumar" `
    --roll 002 --class-name "CSE(AI)" --section C --samples 5
```

That student appears on the Students page immediately, with roll, class and
section filled in, because the CLI writes the same row the dashboard writes.

### The Students page

| Student ID | Name | Roll | Class | Section | Status | Enrollment | Samples | Attendance |
|---|---|---|---|---|---|---|---|---|

Filter by search (id, name **or roll number**), class, section, status and
whether a face enrollment exists. The class suggestions come from the classes
that actually exist in the registry — no list is hard-coded, and a class nobody
is in is not offered.

Each row offers **View**, **Edit**, **Enroll face** (when there is none) and
**Remove**.

### Attendance is derived, never duplicated

The summary on each row — sessions attended, present/absent counts, attendance
percentage — is computed from `attendance_records` on every read. It is **not**
stored on the student row, and a test asserts that no such column exists.

A number copied next to the registry would be wrong the moment a session is
re-evaluated under a new policy or an override is applied. Only the **latest**
record per (session, student) is counted, so a session re-evaluated under two
policies is not counted as two attendances.

A student nobody has expected yet has an attendance rate of **null**, not 0% —
"nothing to divide by" and "attended nothing" are different facts.

The whole page is two queries regardless of how many students it shows. Forty
students used to mean forty summary queries.

### Editing, deactivating and deleting

**Edit** changes name, roll number, class, section and status. The **student ID
is not editable** — it is the key that attendance records, presence segments,
expected-student rows and the face enrollment all hang from, and a field
several tables reference is not one a form may quietly rewrite. Editing
metadata never touches the face samples; a corrected spelling must not cost a
student their enrollment.

**Deactivate** is the safe option and the dialog offers it first. The student
keeps every attendance record, every presence segment and every
expected-student row; they simply leave the active roster. It is reversible.

**Delete permanently** is **`SUPER_ADMIN` only**, and refused by default when
anything references them. Every table pointing at `students(student_id)`
cascades, so deleting a student who has been in a lesson would silently delete
that lesson's attendance for them — precisely what a school must not be able to
do by clicking Remove. A teacher does not see the button at all, and the server
refuses the call regardless of what the browser drew. The refusal names what
would be lost:

```
STU001 has attendance history (3 attendance record(s), 5 presence
segment(s), expected in 3 session(s)). Deleting them would delete that
history too. Deactivate them instead, or confirm permanent deletion.
```

Pressing **Delete permanently** a second time confirms it. Their face samples
go with them — a vector file for a student who no longer exists is an orphaned
biometric record, and keeping one would be the opposite of what deletion means.

### `data/classvision.db`

One SQLite file, ten tables, and the only database this application has.

| Table | |
|---|---|
| `students` | the registry: who a student is |
| `attendance_sessions` | when a class met |
| `session_expected_students` | who was expected — absence is only meaningful against this |
| `attendance_records` | one decision per student per session per policy version |
| `presence_segments` | the evidence a decision was derived from |
| `schema_info` | schema version (**2**) |
| `users` | sign-in accounts, passwords as PBKDF2 hashes |
| `user_class_assignments` | which classes a teacher is responsible for |
| `user_sessions` | live sign-ins, tokens stored only as hashes |
| `audit_log` | what was done, by whom, when |

Inspect it with DB Browser for SQLite, or:

```powershell
python database/init_db.py --show
```

**It has never contained a face image, a face crop, a video frame or an
embedding vector, and it still does not.** Biometric data stays in
`data/embeddings/`, keyed by the same `student_id`.

### Privacy, unchanged

- No photograph is taken or stored, by either enrollment path.
- No endpoint returns an embedding, a face crop, a frame or a file path.
- The browser never uploads or receives an image — the camera is read
  server-side and only counters and a state come back. The Students page has no
  `<img>`, `<video>` or `<canvas>` at all.
- A password never reaches a log, an audit row, an error message or an API
  response, and neither does its hash.
- Everything runs locally. No cloud service, no external request, no LLM.

---

### If the dashboard shows a sign-in box and you have no password

There are **two different situations** here, and they have different answers.
Find out which one you are in first:

```powershell
python database/create_admin.py --list
```

That prints the accounts and no passwords. ClassVision AI ships no default
credentials — no `admin/admin`, no seeded account, nothing in the source — so
whatever it prints was created by a person.

#### Case 1 — it says "No accounts exist yet"

This installation has never been set up, so there is genuinely nothing to sign
in with. Two ways forward, and the first is normal:

**From the dashboard.** The screen reads *"Create your Super Admin account"* and
asks for a username and password of your choosing. Creating it signs you
straight in, and the setup route closes permanently afterwards.

**From the command line**, when a browser cannot help:

```powershell
python database/create_admin.py
```

It prompts for a username and a password (without echoing it), hashes the
password with PBKDF2, and creates a `SUPER_ADMIN`.

#### Case 2 — it lists an account, and you do not know its password

This is the one that actually traps people, and it is NOT a first-run problem.
An account exists, so the setup route is closed — correctly, because leaving a
"create an administrator" endpoint open on a configured system would be a
backdoor. And the password cannot be read back: it is a PBKDF2-HMAC-SHA256
hash with a per-user salt, and a hash does not go backwards.

So the password is **replaced**, not recovered:

```powershell
python database/create_admin.py --reset-password <username>
```

It asks for the new password twice, without echoing it, checks its strength,
and signs out every session that account had open. The sign-in screen itself
carries this command under *"Lost the only administrator password?"*, so you do
not have to remember where it lives.

Nothing about this is a privilege escalation: running it needs write access to
`data/classvision.db`, which is the same access as opening that file in any
SQLite tool. What it adds is doing the job with the project's own hashing
instead of inviting somebody to improvise with a hex editor.

> **If you see a plain "Sign in" box with no explanation on a database that has
> no accounts, the server is running older code than the page.** Static files
> are read from disk on every request, so a browser can load the current
> `index.html` while uvicorn still holds the Python it imported at start-up.
> Stop uvicorn and start it again. The dashboard now detects this case and
> says so instead of leaving a login form on screen.

---

## Ending a Lesson Early

Attendance is a **ratio**: time present divided by the length of the session.
That made ending early quietly expensive.

A lesson scheduled 10:00–11:00, stopped by the teacher at 10:35, used to
divide every student's presence by the full hour. A student who sat through
**every second the camera was running** scored 0.58 — and against the default
75% policy that is `PARTIAL`, not `PRESENT`. They had done nothing wrong.
Nobody was looking for the last 25 minutes.

**Finalisation now measures over the window that was actually observed:**

| Session stopped | Window attendance is measured over |
|---|---|
| before the scheduled end | scheduled start → **actual stop** |
| at the scheduled end | the scheduled window |
| after the scheduled end | the scheduled window (stopping late does not lengthen a lesson) |

The session row keeps its scheduled start and end — this shortens what is
*measured*, never what was *scheduled*, so no historical timestamp is
rewritten. The stop summary reports `ended_early`, `scheduled_end` and
`observed_end`, and the note says the window was shortened rather than hiding
it.

### Attendance already recorded is never taken away

A second, independent guard: finalising a session again — a double-clicked
Stop, a re-run after a crash, a second process — **cannot downgrade a student
out of an attending status**. Nothing about finalising is evidence that a
student was *not* there.

Only downgrades are blocked. A correction upwards from `ABSENT` to `PRESENT`
goes through, every first-time decision goes through, and a teacher's manual
override is protected a layer below in `AttendanceRepository.save_decision`.

### What the teacher sees

Pressing **Stop and finalise** before the scheduled end opens a confirmation:

```
End session early?

Session: SES-2026-05-11-1000-CSE-AI
Scheduled end: 11:00:00
Ending now at: 10:35:12

Students already marked present will remain present. Students not yet
marked present will be finalised according to the attendance rules.
```

Ending on time or late does not ask — that is the ordinary case.

---

## Step 3 — The Embedding Model

| | |
|---|---|
| **Model** | `face_recognition_sface_2021dec.onnx` (SFace) |
| **Size** | 36.9 MB |
| **API** | `cv2.FaceRecognizerSF` — built into OpenCV 5.0.0 |
| **Extra dependencies** | **None** |
| **Embedding dimension** | **128** (measured by running the model) |
| **Input** | 112x112 BGR, aligned |
| **Normalisation** | L2 (unit length) |
| **Similarity** | cosine, -1.0 to 1.0, higher is more similar |

**Alignment.** YuNet gives five landmarks per face. Their order was confirmed
by measuring a known photo, not by trusting documentation:

| index | landmark |
|---|---|
| 0 | left eye (as it appears in the image) |
| 1 | right eye |
| 2 | nose tip |
| 3 | left mouth corner |
| 4 | right mouth corner |

Those five points are mapped onto the standard ArcFace 112x112 template with a
similarity transform — rotation, uniform scale and translation only, so the
face is never stretched.

**Why alignment matters:** without it, a tilted head changes the embedding
almost as much as a different person would. Aligning first means the embedding
describes the face rather than the camera angle.

**Validation:** this alignment was checked against OpenCV's own `alignCrop`
and is **pixel-identical** (max difference 1, from rounding), with embedding
cosine similarity **1.000000**. That confirms both the landmark order and the
template are correct.

### Model versioning

Every enrollment records `model_name`, `model_version` and the model file's
**SHA-256**, both in the metadata and inside the vector file. If the model is
ever changed, embeddings produced by different models can be detected instead
of being silently compared — which would produce confident, wrong answers.

---

## Step 3 — Multi-Sample Enrollment

Five samples are captured per student, at different poses.

**Decision: store all five embeddings, rather than averaging them into one.**

Averaging is tempting because it stores less. It was rejected because the whole
point of capturing different poses is to record natural variation, and a mean
vector can land between poses and represent none of them well. Keeping the
samples lets Step 4 compare against the closest one.

This does not weaken privacy: five vectors of the same person identify exactly
the same person as one vector does. The privacy cost is in storing biometric
data at all, not in the number of samples — and no photographs are stored
either way.

Samples are only taken when the face passes every quality check for six
consecutive frames, with a 1.5 s pause between captures so two samples are
never taken from nearly the same moment.

### Quality gate

Enrollment is stricter than live detection, because a bad sample becomes the
permanent reference for that student:

| Check | Rule | Example message |
|---|---|---|
| Exactly one face | not 0, not 2+ | "Multiple faces detected (2) - only one person should be visible." |
| Confidence | >= 0.85 | "Face confidence too low (72%) - improve lighting and face the camera." |
| Size | >= 80x80 px | "Face too small (56x83, need 80x80) - move closer to the camera." |
| Aspect ratio | 0.5 to 2.0 | "Face box looks distorted - please try again." |
| Not at frame edge | 2 px margin | "Face is at the edge of the frame - center yourself." |
| Landmark geometry | eyes above mouth, etc. | "Face not clearly visible - ..." |

---

## Step 3 — Storage

```
data/                      <-- gitignored, never committed
  embeddings/
    students.json          metadata (readable, no vectors)
    S101.npz               that student's 128-number vectors
```

`students.json` holds the required metadata: `student_id`, `name`,
`roll_number`, `class_name`, `section`, `embedding_id`, `created_at`,
`model_name`, `model_version`, plus `sample_count` and model provenance.

Vectors live in `.npz`, a compact binary format meant for numeric arrays.

All storage goes through `ai/embedding_store.py` — `save_student`,
`get_student`, `list_students`, `delete_student`, `save_embedding`,
`load_embeddings`, `audit`. Camera and AI code never touch files directly, so
**Step 6 can replace this with PostgreSQL by reimplementing that one module.**

---

## Step 3 — Privacy

> **This prototype processes biometric information. Real deployment requires
> appropriate consent, institutional policy, access controls, retention rules,
> and applicable legal and privacy review.**

No claim is made that this system is legally compliant. It is not.

What is actually enforced, each one covered by a test:

| Guarantee | How it is enforced |
|---|---|
| Raw photos are never retained | Frames become embeddings and are discarded; a test asserts no image file is written |
| Photos cannot sneak into metadata | The store rejects fields named `image`, `photo`, `face_image`, `crop`, `frame` |
| Vectors never reach logs | A test scans every `ai/` module for a `print()` of a vector |
| Biometric data never reaches Git | `data/`, `*.npz`, `*.npy` are gitignored; a test asserts this |
| Deleting a student removes their biometrics | `delete_student()` removes metadata *and* the vector file; a test asserts no trace remains |
| Enrollments can be audited | `python ai/embedding_store.py list` reports who is enrolled, with counts and model provenance, and never shows a vector |

**Embeddings are treated as sensitive personal data, not anonymous numbers.**
They are often called irreversible; treat that as defence in depth rather than
a guarantee, since research has shown approximate faces can sometimes be
reconstructed from embeddings.

---

## Step 3 — Measured Results

### Embedding separation (diagnostic only)

Measured on OpenCV sample photos. The "same person" rows are one photo altered
to imitate capture variation:

| Comparison | Cosine similarity |
|---|---|
| same face, rotated +6 deg | 0.9854 |
| same face, rotated -6 deg | 0.9549 |
| same face, brighter | 0.9526 |
| same face, darker | 0.9857 |
| same face, blurred | 0.9761 |
| same face, scaled to 70% | 0.9334 |
| **lowest same-person** | **0.9334** |
| different person (A) | 0.1269 |
| different person (B) | 0.0280 |
| **highest different-person** | **0.1269** |

> This is a **development sanity check, not a recognition threshold.** It uses
> three identities and synthetic variation. A real threshold must be chosen in
> Step 4 against real data, and no accuracy percentage is claimed here.

### Quality gate on static images

| Image | Faces | Result |
|---|---|---|
| one clear face | 1 | ACCEPT |
| two faces side by side | 2 | REJECT — "Multiple faces detected (2)" |
| no face | 0 | REJECT — "No face detected" |
| black frame | 0 | REJECT — "No face detected" |
| face scaled to 40% | 1 | REJECT — "Face too small (56x83, need 80x80)" |

---

## Step 3 — Limitations

- **No recognition.** Embeddings exist, but nothing matches a live face
  against them. That is Step 4.
- **No at-rest encryption.** Vectors are stored as plain `.npz` files,
  protected only by gitignore and file permissions. Encrypting them needs a
  cryptography dependency and a key-management decision, which belongs with
  the Step 10 hardening work. **Do not put real student data in this prototype
  as it stands.**
- **File permissions are weak on Windows.** `os.chmod` cannot express
  owner-only access there, so that protection is effectively Linux/macOS only.
- **No liveness or anti-spoofing.** A printed photo held to the camera would
  enroll successfully.
- **No consent workflow.** There is no record of consent being given, which a
  real deployment requires.
- **Not validated on real students.** Verified with one webcam and OpenCV
  sample photos. No accuracy figure is claimed, because that needs a labelled
  dataset this project does not have.
- **Enrollment quality depends on the operator.** Nothing stops someone
  enrolling a student under the wrong id.

---

## Step 4 — How Recognition Works

> **Step 4 performs identity verification only. It does not mark attendance or
> calculate presence duration.**

```
camera frame -> YuNet detection -> align (112x112) -> SFace embedding (128)
             -> compare against every enrolled embedding
             -> best score per student  (best-of-five)
             -> >= threshold ? MATCH : UNKNOWN
             -> stability filter -> displayed label
```

### Run it

```powershell
python ai/recognition_test.py
```

| Key | Action |
|-----|--------|
| `Q` or `ESC` | quit cleanly |
| `H` | hide / show the info panel |
| `S` | toggle the stability filter |
| `R` | reload enrolled students from disk |

```powershell
python ai/recognition_test.py --threshold 0.5     # try a different threshold
python ai/recognition_test.py --no-stability      # raw per-frame result
python ai/recognition_test.py --confirm-frames 5  # stricter confirmation
python ai/recognition_test.py --benchmark         # measure, no window
```

On-screen output is identity only — a name or `Unknown`, plus a score. No
attendance status is displayed anywhere.

### Best-of-five matching

Each student was enrolled with five embeddings at slightly different poses. A
live face is compared against **all five**, and that student's score is the
**highest** of them — the closest pose wins.

Averaging the five was rejected: a mean vector sits between the poses and
matches each of them slightly worse, which throws away the very variation the
five samples were captured to record.

Matching is one matrix multiply against every stored vector at once, so cost
barely grows with class size (measured below).

### UNKNOWN is a real answer

The recogniser never forces a face onto the nearest student:

```
best candidate Rahul, similarity 0.91  ->  MATCH,   identity "S001"
best candidate Rahul, similarity 0.42  ->  UNKNOWN, identity null
no students enrolled                   ->  NO_ENROLLMENTS
```

A system that always returned its closest guess would confidently mislabel
every visitor, parent, and stranger who walked past the camera.

### Result schema

```python
{
  "identity": "S001",        # or None when UNKNOWN
  "name": "Rahul",           # or None
  "similarity": 0.9137,
  "status": "MATCH",         # MATCH | UNKNOWN | NO_ENROLLMENTS
  "threshold": 0.45,
  "box": (x, y, w, h),
  "detection_confidence": 0.98,
  "timestamp": 1758...
}
```

It carries a box, a timestamp and a stable student id, so a tracking layer in
Step 5 can consume it unchanged. It carries **no embedding**, so it is safe to
log.

---

## Step 4 — Threshold Methodology

This is the most consequential number in the system, so how it was chosen
matters more than the value itself.

### Three different kinds of number

| Term | What it means | Value here |
|---|---|---|
| **Diagnostic similarity** | Scores from a small controlled experiment. Useful for sanity checks; proves nothing about real performance. | 0.86–1.00 same face, 0.03–0.27 different people |
| **Development threshold** | A provisional value used so the system runs. Not validated. | **0.45 (current default)** |
| **Validated production threshold** | Chosen from a genuine-vs-impostor study on real, representative data. | **Does not exist for this project** |

### Why not 0.93?

Step 3 measured same-face similarities of 0.93+. **Those pairs were altered
copies of one photograph** — rotated, brightened, blurred. Two genuinely
different captures of the same person, on different days under different
light, score far lower.

A threshold near 0.93 would therefore reject almost every real match. The
synthetic number is the wrong kind of number for this job.

### Where 0.45 comes from

The model's authors calibrated SFace on a standard face-verification benchmark
and publish a cosine threshold of **0.363** ([OpenCV Model
Zoo](https://github.com/opencv/opencv_zoo)). That is *their* measurement on
*their* data, reported here as a reference point — not a claim about this
project.

0.45 sits deliberately above it, because in this application wrongly accepting
a face is worse than asking someone to try again.

> **0.45 is a provisional development threshold, not a validated production
> one.** It is configurable and expected to change once real data exists.

```powershell
python ai/recognition_test.py --threshold 0.5        # per run
$env:CLASSVISION_RECOGNITION_THRESHOLD = "0.5"       # per environment
```

### Measuring a real threshold

`ai/evaluate_threshold.py` exists so the threshold can be chosen from data
rather than argued about. Give it a folder of labelled images — one folder per
person, **two or more genuinely separate captures each**:

```
data/eval/rahul/1.jpg  2.jpg  3.jpg
data/eval/priya/1.jpg  2.jpg
```

```powershell
python ai/evaluate_threshold.py
```

It reports genuine and impostor score distributions, false accepts and false
rejects at a range of thresholds, the equal-error point, and the specific
closest-impostor and weakest-genuine pairs. It refuses to bless a threshold
when the dataset is too small.

**Run on the only public face data available to this project, it returned:**

```
identities        : 3
genuine pairs     : 2
impostor pairs    : 8

NOT ENOUGH DATA TO SET A PRODUCTION THRESHOLD.
```

That is the honest state of the threshold today.

---

## Step 4 — Stability Filter

Recognition runs independently per frame, so a face sitting near the threshold
flickers between a name and `Unknown`. The stability filter requires the same
identity in **three consecutive frames** before confirming it, and smooths the
displayed score over the last five.

```
frame 1  Rahul 0.81  ->  pending
frame 2  Rahul 0.88  ->  pending
frame 3  Rahul 0.91  ->  CONFIRMED: Rahul
frame 4  Rahul 0.92  ->  CONFIRMED: Rahul
```

A single bad frame does not drop a confirmed identity; a sustained run of
non-matches does.

> **This is not tracking and not attendance.** Faces are associated between
> frames by nearest box centre only. There is no motion model, no
> re-identification after occlusion, and no stable track ids — two people
> crossing can swap slots. It lives alone in `ai/recognition_stability.py` so
> Step 5 can replace it with real multi-object tracking.

---

## Step 4 — Measured Performance

All measurements are from this machine. **OpenCV is running on the CPU — no
GPU acceleration is configured or claimed.**

### Camera loop

| Resolution | FPS | Detect avg | Dropped |
|---|---|---|---|
| **640×480** | **15.0** | 11.3 ms | 0 |
| 1280×720 | 10.0 | 37.5 ms | 0 |

Live run at 640×480: 96 frames, 13.8 FPS, detect 13.7 ms avg (p95 16.6 ms),
**0 dropped frames**, clean exit on Q.

### Recognition cost per face

| Operation | Cost |
|---|---|
| Align + embed, per face | **4.16 ms** |
| Match against 1 student (5 vectors) | 0.026 ms |
| Match against 10 students (50 vectors) | 0.033 ms |
| Match against 30 students (150 vectors) | 0.031 ms |
| Match against 100 students (500 vectors) | 0.035 ms |
| Match against 500 students (2500 vectors) | 0.143 ms |

**Matching is essentially free; embedding dominates.** Class size barely
matters — the number of faces *in frame* is what costs.

### Projected cost by faces in frame (640×480)

| Faces | Recognition | Total/frame | Projected FPS |
|---|---|---|---|
| 1 | 4.2 ms | 15.5 ms | 64.5 |
| 5 | 20.8 ms | 32.1 ms | 31.1 |
| 10 | 41.6 ms | 52.9 ms | 18.9 |
| 20 | 83.2 ms | 94.5 ms | 10.6 |
| 30 | 124.8 ms | 136.1 ms | 7.3 |

The camera runs at ~15 FPS (66.7 ms/frame), so up to roughly **13 faces the
system is camera-bound**, not compute-bound. Beyond that, embedding becomes
the bottleneck: **a full classroom of 30 faces would drop to about 7 FPS.**

> This is a projection from a measured per-face cost, not a measured
> 30-face run. It is a concrete problem for Step 5: tracking should let the
> system embed a face once and follow it, rather than re-embedding every face
> in every frame.

### Recognition behaviour on controlled images

Using the real SFace model, one enrolled identity, threshold 0.45:

| Case | Status | Similarity |
|---|---|---|
| Enrolled person, held-out view | MATCH | 0.9837 |
| Enrolled person, original | MATCH | 1.0000 |
| Different person A | UNKNOWN | 0.1405 |
| Different person B | UNKNOWN | 0.0409 |

**Lighting** (same face, brightness shifted):

| Brightness | -70 | -45 | -20 | 0 | +20 | +45 | +70 |
|---|---|---|---|---|---|---|---|
| Similarity | 0.8681 | 0.9690 | 0.9898 | 1.0000 | 0.9988 | 0.8861 | 0.8577 |

All still MATCH.

**Rotation** (same face, rotated):

| Rotation | 0° | 5° | 10° | 15° | 20° | 30° | 45° |
|---|---|---|---|---|---|---|---|
| Similarity | 1.0000 | 0.9942 | 0.9849 | 0.8878 | 0.8729 | 0.7643 | face not detected |

Recognition degrades gracefully with rotation; **detection fails before
recognition does** — at 45° no face is found at all.

**Multiple faces in one frame:** a composed two-face image produced two
independent results — one MATCH (1.0000, the enrolled identity) and one
UNKNOWN (0.1405) — with no state shared between them, and the same answers
regardless of the order the faces were processed in.

> These "same person" rows are **altered copies of one photograph**, so the
> similarities are optimistic. They show the pipeline behaves sensibly; they
> are **not** an accuracy measurement and no accuracy figure is claimed.

---

## Step 4 — Limitations

- **The threshold is not validated.** 0.45 is provisional. Insufficient real
  data exists to justify a production value.
- **Never tested on real different people in front of the camera.** Every
  "same person" result comes from altered copies of one photo. Real-world
  false accept and false reject rates are **unknown**.
- **No liveness.** A printed photo held to the camera would be recognised.
- **Not tracking.** The stability filter is a placeholder; two people crossing
  can swap slots.
- **~7 FPS projected at 30 faces**, CPU only.
- **Detection fails before recognition does** — beyond about 30° of head
  rotation there is nothing to recognise.
- **Single camera, single frame.** No presence, no duration, no attendance.
- **No accuracy figure is claimed anywhere in this project.**

---

## Step 5 — Tracking + Presence Timeline

> **Step 5 does not calculate attendance.**
>
> It produces a *presence timeline* — a record of who was visible and when.
> Attendance will be derived from that timeline in a later step by applying
> configurable rules, so a school can change its policy without re-running the
> camera.

```
camera -> detection (every frame)
       -> tracking  (every frame, ~0.04 ms)
       -> recognition (ONLY when the identity manager asks)
       -> identity association + switch protection
       -> presence timeline -> events
```

### Run it

```powershell
python ai/tracking_test.py
python ai/tracking_test.py --timeline            # print events as they happen
python ai/tracking_test.py --recognition-interval 15
python ai/tracking_test.py --max-lost-frames 30  # longer grace period
python ai/tracking_test.py --benchmark --seconds 20
```

| Key | Action |
|-----|--------|
| `Q` or `ESC` | quit cleanly |
| `H` | hide / show the info panel |
| `T` | toggle the live console timeline |
| `R` | reload enrolled students |

On-screen label: `Track 7 | Safia | MATCH 0.82 | PRESENT`, or
`Track 12 | UNKNOWN | 0.31 | PRESENT`.

---

## Step 5 — Why the Tracker Was Written, Not Installed

This environment was checked before deciding:

| Option | Why not |
|---|---|
| ByteTrack / BoT-SORT | need `torch` or `scipy` + a linear-assignment package |
| DeepSORT | needs a separate re-identification model and deep-learning stack |
| `cv2.legacy` CSRT / KCF | **removed in OpenCV 5.0** — they do not exist here |
| `cv2.TrackerMIL` / Nano / ViT | **single-object** trackers: one instance per person, each doing its own visual search, some needing extra model downloads |

YuNet already detects every face in every frame in ~13 ms. When detections
are that good, association is the only missing piece — so `ai/person_tracker.py`
implements tracking-by-detection directly, the same family as SORT and
ByteTrack, in about 200 lines with **no new dependency**.

### The ByteTrack idea worth keeping

ByteTrack's insight is that *low-confidence detections are usually real people*
who are briefly blurred or half-turned — not noise. Discarding them breaks
tracks; using them only to **continue** existing tracks recovers those people
without inventing new ones. So association runs in two passes:

1. existing tracks ↔ **high**-confidence detections
2. still-unmatched tracks ↔ **low**-confidence detections
3. new tracks are created **only** from unmatched high-confidence detections

A weak detection can keep a person alive; it can never invent one.

### Determinism

Matching is greedy on overlap, with ties broken by track id and box position
rather than list position. Reversing the detector's output produces identical
tracks — asserted by a test. Detection order must never shuffle identities.

---

## Step 5 — Track vs Identity

A track and a student are deliberately different things, held in different
modules:

| | Track (`person_tracker.py`) | Identity (`track_identity.py`) |
|---|---|---|
| Means | "this box, followed across frames" | "this is student STU001" |
| Lifetime | ends when the person leaves | belongs to a person |
| On re-entry | **a new track id** | the same student |

`track_id = 17` and `student_id = STU001` are never treated as the same thing.
The same student gets a new track id every time they leave and return, which is
exactly why attendance cannot be derived from track ids — it must come from the
timeline.

### Identity-switch protection

A track must never quietly become a different student. When a confirmed track
is claimed by someone else, the switch is refused unless the evidence is both
**repeated** and **clearly stronger**:

| Guard | Default | Why |
|---|---|---|
| Confirmations to set an identity | 2 | one frame is weak evidence |
| Confirmations to **change** one | 4 | changing is more serious than setting |
| Score margin to change | +0.08 | noise near the threshold must never win |

Until both conditions are met the original identity holds and an
`IDENTITY_CHANGED_REJECTED` event is recorded, so refusals are auditable rather
than silent. A confirmed identity also survives an isolated weak frame — a
student turning away briefly does not lose their name.

If recognition is not confident, identity stays `UNKNOWN`. It is never forced.

---

## Step 5 — Recognition Efficiency

Embedding a face costs **4.16 ms** (measured in Step 4). Doing it for every
face on every frame is what limited a full classroom to ~7 FPS. A tracked face
does not need re-checking every frame — it is the same person it was 60 ms ago.

Recognition is requested only when it can change the answer:

- the track has no identity yet
- the identity is not yet confirmed
- the track was just **reacquired** (it might be a different person)
- the refresh interval elapsed (default every 30 frames, ~2 s)
- a possible identity change is being investigated

A track that is not currently visible is never sent for recognition — there is
no fresh image to embed.

### Measured: Step 4 pipeline vs Step 5 pipeline

A 300-frame sequence, one **real** face drifting across a 640×480 frame, same
machine, same models. *Synthetic motion — not a real classroom recording.*

| | Step 4 style (recognise every face, every frame) | Step 5 style (tracked) |
|---|---|---|
| Recognition/embedding calls | 300 | **11** |
| Calls per frame | 1.00 | **0.04** |
| Wall time | 3.16 s | **1.92 s** |
| Throughput | 95.1 FPS | **155.9 FPS** |

**96.3% of embedding calls avoided; 1.64× faster end to end.**

The speed-up is smaller than the call reduction because detection still runs
on every frame and now dominates the cost — which is the expected result, not a
disappointing one.

A second scenario (135 frames: present → hidden 8 frames → returns → leaves)
made **5** recognition calls where a Step 4 pipeline would have made 102 —
**95.1% avoided**.

---

## Step 5 — Temporary Loss vs Exit

The distinction the whole timeline depends on. A face vanishing for a few
frames must **not** become an exit.

```
visible
   |  face not detected
   v
TEMPORARILY_LOST  (inside the grace period)
   |                        \
   | seen again              \ grace period expires
   v                          v
REACQUIRED  -> PRESENT      EXITED
```

The grace period is `--max-lost-frames`, default **15 frames** (~1 s at the
measured 15 FPS). It is a tolerance for blinking, turning away, or someone
walking in front of the camera — **not** a classroom policy. How long an
absence has to be before it *means* something is a later decision, made from
the timeline rather than hard-coded here.

`TRACK_TEMPORARILY_LOST` fires once per episode, not once per frame.

---

## Step 5 — Presence Events

| Event | When |
|---|---|
| `TRACK_STARTED` | a new person appears |
| `IDENTITY_CONFIRMED` | recognition settled on a student |
| `UNKNOWN_PERSON` | visible, still unidentified after 2 attempts |
| `PRESENCE_CONTINUED` | heartbeat, every 60 frames by default |
| `TRACK_TEMPORARILY_LOST` | not visible, inside the grace period |
| `TRACK_REACQUIRED` | seen again before the grace period expired |
| `TRACK_EXITED` | the tracker gave up, or the session ended |
| `IDENTITY_CHANGED_REJECTED` | a switch was proposed and refused |
| `IDENTITY_CHANGED` | a switch was accepted after sustained evidence |

Every event carries `event_type`, `track_id`, `student_id`, `name`,
`similarity`, `state`, `frame_index` and a **UTC** timestamp.

**Time convention: UTC throughout, never local time**, so a timeline is
unambiguous across machines and survives daylight-saving changes. The frame
index is kept alongside it because it is exact, while wall-clock time can jump
if the system clock is adjusted mid-session.

### Real output from the leave-and-return scenario

```
[18:36:17.704] TRACK_STARTED              track=1
[18:36:17.716] IDENTITY_CONFIRMED         track=1  student=STU001 similarity=0.92
[18:36:17.998] PRESENCE_CONTINUED         track=1  student=STU001 similarity=0.92
[18:36:18.029] TRACK_TEMPORARILY_LOST     track=1  student=STU001 similarity=0.92
[18:36:18.085] TRACK_REACQUIRED           track=1  student=STU001 similarity=0.94
[18:36:18.293] PRESENCE_CONTINUED         track=1  student=STU001 similarity=0.86
[18:36:18.421] TRACK_TEMPORARILY_LOST     track=1  student=STU001 similarity=0.86
[18:36:18.517] TRACK_EXITED               track=1  student=STU001 (visible 102 frames)
```

One track, one person, two lost episodes, one exit — no duplicate person
created by the disappearance.

The session summary reports `visible_frames`, `lost_episodes` and
`duration_seconds` per track. It deliberately contains **no** `present` or
`attendance` field, and a test asserts that.

---

## Step 5 — Measured Performance

**CPU only — no GPU acceleration is configured or claimed.**

### Live webcam, 640×480

| | MEASURED |
|---|---|
| Resolution | 640×480 |
| Average FPS | **15.0** (benchmark), 14.8 (live) |
| Dropped frames | **0** |
| Detection | 13.2 ms avg |
| **Tracking** | **0.039 ms avg** |
| Total latency avg | 13.2 ms |
| Total latency p95 | 16.0 ms |
| Total latency max | 19.5 ms |
| Frames processed | 180 (benchmark), 112 (live) |

**Tracking is effectively free** — 0.039 ms against 13.2 ms of detection, about
0.3% of the frame budget. Adding tracking cost nothing and removed 96% of the
recognition work.

> **NOT TESTED: nobody was in front of the camera during these runs.**
> Face-frames seen: 0. Tracks created: 0. The FPS, latency and dropped-frame
> figures are real; the tracking and identity figures from the webcam are not,
> because there was nothing to track. The efficiency numbers above come from
> the simulated sequences, which used a real face but synthetic motion.

---

## Step 5 — Limitations

- **No real-person validation.** Nobody was available in front of the camera.
  Every multi-person, crossing, occlusion and re-entry scenario is verified by
  **synthetic tracker tests** only.
- **Re-entry creates a new track id.** A person who leaves and returns is a new
  track. Their identity is re-established by recognition, not carried over —
  deliberate, since carrying identity across a gap on weak evidence is exactly
  how the wrong student gets marked.
- **No appearance-based re-identification.** Association is geometric (overlap
  plus predicted motion). Two people crossing with heavily overlapping boxes
  can still swap tracks; the identity-switch guard limits the damage but does
  not eliminate it.
- **No Kalman filter.** Motion prediction is a smoothed constant-velocity
  estimate. Adequate at 15 FPS for walking speed; not for fast erratic motion.
- **Grace period and intervals are defaults, not policy.** 15 frames and 30
  frames were chosen against the measured ~15 FPS, not against classroom
  evidence.
- **Recognition quality is unchanged from Step 4** — the threshold is still
  provisional (0.45) and unvalidated, and no accuracy figure is claimed.
- **Still no attendance**, no presence *rules*, no database, no API, no
  dashboard.

---

## Step 6 — Attendance Rules + Database

> **Step 6 introduces deterministic attendance rules and SQLite persistence.**
>
> **Attendance accuracy has NOT been validated on a real classroom
> population.** Every result below comes from synthetic timelines.

```
Camera -> Detection -> Tracking -> Recognition -> Presence Timeline
                                                        |
                          +-----------------------------+
                          v
              +---------------------------+
              |     ATTENDANCE ENGINE     |
              |  policy - session rules   |
              |  duration calculation     |
              |  late / partial / early   |
              |  expected students        |
              |  absence finalisation     |
              +---------------------------+
                          |
                 Attendance Decisions
                          |
                   SQLite Database
```

Three responsibilities, three layers, deliberately not merged:

| Layer | Module | Role |
|---|---|---|
| Evidence | `ai/presence_timeline.py` (Step 5) | what was observed |
| Decision | `ai/attendance_rules.py` | what it means, under a policy |
| Storage | `database/` | keeping both |

The rule engine has no database and no camera. It takes segments and a policy
and returns decisions — which is exactly why a policy can change and history
can be recomputed without re-running anything.

### Run it

```powershell
python database/init_db.py          # create or verify the schema
python database/init_db.py --show   # report what is there
```

Safe to re-run: every statement is `CREATE ... IF NOT EXISTS`. It never drops a
table, deletes a row, or resets anything.

---

## Step 6 — Attendance States

| State | Meaning |
|---|---|
| `PRESENT` | met the duration and ratio requirements, on time, stayed |
| `LATE` | met the requirements, but first seen after the late threshold |
| `PARTIAL` | genuinely seen, but for less than the policy requires |
| `LEFT_EARLY` | met the requirements, on time, but last seen before the early-leave threshold |
| `ABSENT` | expected, no verified presence, **in a completed session** |
| `UNKNOWN` | cannot be decided yet — typically an expected student in a session still running |
| `MANUAL_OVERRIDE` | a permitted stored status for a human-determined result |

A student can be both late *and* leave early, so when more than one label fits,
a fixed precedence decides which is reported:

```
ABSENT  >  PARTIAL  >  LATE  >  LEFT_EARLY  >  PRESENT
```

Nothing is lost — `was_late` and `left_early` are always recorded in the
evidence regardless of which status won.

### Two rules that override convenience

**An unknown face never becomes attendance.** A presence segment only opens at
`IDENTITY_CONFIRMED`. A track that is never identified produces no segment, no
record, nothing. `count_unverified_presence=True` is not merely discouraged —
the policy validator *refuses* it.

**ABSENT requires a completed session.** A live, scheduled, or cancelled
session yields `UNKNOWN` for a missing student, never `ABSENT`. A camera that
stopped early is not evidence that anybody was away. Likewise `LEFT_EARLY` is
never decided while a session is still active.

---

## Step 6 — Attendance Policy

Every threshold is configurable. **These are development defaults, not any
institution's rules.**

| Setting | Default | Meaning |
|---|---|---|
| `version` | `"1.0"` | recorded with every decision |
| `minimum_presence_ratio` | 0.75 | fraction of the session required |
| `minimum_presence_seconds` | 0 | absolute floor, if wanted |
| `late_after_seconds` | 600 | 10 minutes past the start |
| `early_leave_before_seconds` | 300 | last 5 minutes of the session |
| `temporary_loss_grace_seconds` | 30 | how long a gap may be and still count as continuous |
| `allow_partial` | True | if False, insufficient presence is ABSENT |
| `timezone_name` | `"UTC"` | for display; all arithmetic is UTC |

> 0.75 is **not** a universal academic requirement. It is a placeholder so the
> engine runs out of the box. An invalid policy — a ratio above 1, a negative
> threshold, an empty version — is rejected rather than silently clamped.

---

## Step 6 — Presence Duration

Durations come from Step 5 timestamps, never from frame counts.

Reported per student: first verified presence, last verified presence, total
presence seconds, segment count, longest continuous segment, number of
loss periods, and total loss seconds.

**Overlaps are merged before counting.** Two tracks covering 09:00–09:20 and
09:10–09:40 are **40 minutes, not 50**. This matters because one student
routinely produces several track ids in a session.

Presence outside the session window is clipped away, and zero-length segments
are dropped.

### Temporary loss versus exit

```
IDENTITY_CONFIRMED          -> segment opens
TEMPORARILY_LOST            -> segment stays open (pending)
  REACQUIRED within grace   -> segment continues, unbroken
  REACQUIRED after grace    -> close at the moment of loss, open a new segment
TRACK_EXITED                -> close at the last actual sighting
  identity confirmed again  -> a NEW segment (same student, new track)
```

An exit credits presence up to when the person was **last actually seen**, not
to when the tracker gave up waiting.

### Multiple tracks, one student

```
track 7  -> STU001   09:00-09:20
track 15 -> STU001   09:21-09:45
                     ------------
STU001 total verified presence: 44 minutes
```

One student, two tracks, one attendance record. Track ids are recorded as
evidence; they are never treated as identity.

---

## Step 6 — Every Decision Explains Itself

```
student_id      : STU002
status          : LATE
present_seconds : 2760
session_seconds : 3600
presence_ratio  : 0.767
first_seen      : 09:14:06
reason          : "First verified presence at 09:14:06 occurred after the
                   late threshold of 09:10:00"
policy_version  : "1.0"
```

Every record carries the numbers that produced it and the policy version that
judged them. A result nobody can question is a result nobody can correct.

---

## Step 6 — Session Lifecycle

```
SCHEDULED ──► ACTIVE ──► COMPLETED
     │           │
     └───────────┴──────► CANCELLED
```

Illegal transitions are refused, not ignored. **A session is never completed
automatically** — closing the camera window and ending a lesson are different
claims, and only completion unlocks final absence decisions.

---

## Step 6 — Database

**SQLite, via the Python standard library — no new dependency.** SQLite
3.40.1. Everything is ordinary SQL, so a future move to PostgreSQL is a change
of driver and dialect, not a redesign.

Default location: `data/classvision.db`. `data/` is gitignored, and `*.db` is
now explicitly ignored too.

### Schema (6 tables)

| Table | Holds |
|---|---|
| `students` | `student_id`, `name`, `status`, timestamps |
| `attendance_sessions` | session, class, date, start/end, timezone, status, policy version |
| `session_expected_students` | who was expected — absence is meaningless without this |
| `attendance_records` | the decision, its evidence, its policy version, its override trail |
| `presence_segments` | the underlying evidence, so attendance can be recomputed |
| `schema_info` | schema version |

### Constraints that are actually enforced

- `PRAGMA foreign_keys = ON` **per connection** — SQLite leaves this off by
  default, so the declared foreign keys would otherwise be decorative. A test
  asserts an orphan insert is refused.
- `CHECK` on every status column, so an invalid status is refused by the
  database itself, not only by Python.
- `present_seconds >= 0`, `0 <= presence_ratio <= 1`, `segment_count >= 0`,
  `duration_seconds >= 0`, `end_time > start_time`.
- `UNIQUE (session_id, student_id, policy_version)` — the idempotency guard.
- A `CHECK` requiring that an override carries its full audit trail.
- Every statement is parameterised; no value is ever formatted into SQL.
- Explicit transactions that roll back on any exception. Integrity errors
  propagate — they are never swallowed.

---

## Step 6 — Policy Versioning and Re-evaluation

`UNIQUE (session_id, student_id, policy_version)` does two jobs at once:

- **Idempotency** — re-running an evaluation with the same policy *updates* the
  row. Running the whole pipeline three times produces one record per student.
- **History** — a new policy version writes a *new* row. The earlier result
  survives.

```
same stored evidence -> policy 1.0 -> LATE      (kept)
same stored evidence -> policy 2.0 -> PARTIAL   (added)
```

This is why `presence_segments` is persisted separately: attendance can be
recalculated from stored evidence without re-running a camera.

---

## Step 6 — Manual Override

A teacher correction is recorded, never applied invisibly:

| Column | Holds |
|---|---|
| `status` | the corrected status |
| `original_status` | what the system decided |
| `is_override` | 1 |
| `override_reason` | why |
| `overridden_by` | actor identifier, if known |
| `overridden_at` | when |

**Re-evaluation does not overwrite an override** — a human decision outranks a
recomputation. An override can also be cleared, restoring the computed status.
There is no path that changes a result without leaving a trail; the database
`CHECK` constraint refuses a half-recorded override.

No user-management system is built here. `overridden_by` is a plain nullable
string, which is all this step needs.

---

## Step 6 — Measured Performance

**MEASURED** — rule engine and database timed separately:

| | Value |
|---|---|
| Presence events | 120 |
| Students | 30 |
| Segments produced | 30 |
| **Rule engine** | **0.42 ms** |
| **Database write** | **11.29 ms** |
| **Total** | **11.70 ms** |

**Attendance calculation is not a bottleneck.** Evaluating a 30-student class
costs less than half a millisecond — roughly 3% of the total, with the database
write dominating. Compared with the camera pipeline (~13 ms per *frame*), the
entire attendance evaluation for a whole session costs less than one frame of
video.

**NOT TESTED:** classes larger than 30, sessions longer than one hour,
concurrent writers, or any real classroom data.

---

## Step 6 — Limitations

- **No real-classroom validation.** Every result comes from synthetic
  timelines with invented students. Attendance accuracy on real people is
  **unknown**, and inherits every Step 4 limitation — the recognition
  threshold is still the provisional, unvalidated 0.45.
- **Attendance is only as good as the recognition beneath it.** A missed
  identification becomes missing presence, which becomes a wrong status. The
  rules are deterministic; their input is not.
- **Defaults are placeholders**, not policy. 0.75, 10 minutes, 5 minutes and a
  30-second grace were chosen so the engine runs, not because any institution
  requires them.
- **Single-session evaluation.** No term aggregation, no trends, no reports.
- **No authentication.** `overridden_by` is a free-text string; anyone with
  file access can write to the database.
- **No encryption at rest** — same gap as Step 3.
- **SQLite single-writer.** Fine for one classroom on one machine; concurrent
  writers are untested.
- **Timezone support is minimal.** All arithmetic is UTC; the policy carries a
  timezone name for display only.
- Still no API, dashboard, notifications, analytics, reports, or AI assistant.

---

## Step 7 — Teacher/Admin Dashboard + Attendance Analytics

> **Step 7 is a local teacher/admin dashboard and deterministic analytics
> layer.**
>
> **Local password authentication IS implemented (PBKDF2 hashing, session cookies, SUPER_ADMIN / TEACHER roles enforced server-side); it has not been independently reviewed and is not evaluated production security.**
>
> **Attendance accuracy has NOT been validated on a real classroom
> population.**

```
SQLite database
      |
Repository layer (Step 6)
      |
Data service  +  Analytics service      <- deterministic, no SQL in routes
      |
FastAPI backend (16 operations)
      |
Dashboard (HTML / CSS / JS, no framework)
```

The dashboard reads. It makes no attendance decisions, touches no camera,
loads no model, and calls no external service. The one write it can perform is
a manual correction, which Step 6 already records with a full audit trail.

### Run it

```powershell
python database/init_db.py            # once, if the database does not exist
uvicorn backend.app:app --reload
```

Then open <http://127.0.0.1:8000/>. API documentation is at `/docs`.

To look at the dashboard before any real session exists, seed a **separate**
demo database:

```powershell
python backend/seed_demo.py --yes                     # writes data/classvision-demo.db
$env:CLASSVISION_DB="data/classvision-demo.db"
uvicorn backend.app:app --reload
```

The seeder refuses to run without `--yes`, and **refuses outright** to write to
the real database. Its data is fictional and labelled as such.

---

## Step 7 — Technology

| Layer | Choice | Why |
|---|---|---|
| Backend | **FastAPI 0.141.1** + uvicorn | Requested, installs cleanly on Python 3.10, gives typed schemas and OpenAPI for free |
| Database | SQLite (Step 6, stdlib) | unchanged |
| Frontend | **Plain HTML + CSS + JavaScript** | No framework, no build step, no CDN — the whole UI is 54.8 KB and works offline |
| Charts | Hand-written inline SVG | A charting library would have been the single heaviest dependency in the project, for two charts |

New dependencies: `fastapi`, `uvicorn`, `httpx` (test client only). Pinned in
`requirements.txt`.

**Connections are per-thread.** SQLite connections are bound to the thread that
created them, and FastAPI runs synchronous endpoints in a worker threadpool, so
a single shared connection fails on the second concurrent request. Each worker
thread keeps its own connection instead.

---

## Step 7 — API

16 operations: 14 read-only, 2 for manual correction.

| Method | Path |
|---|---|
| GET | `/api/health` |
| GET | `/api/classes` |
| GET | `/api/sessions` |
| GET | `/api/sessions/{session_id}` |
| GET | `/api/sessions/{session_id}/attendance` |
| GET | `/api/sessions/{session_id}/presence` |
| GET | `/api/students` |
| GET | `/api/students/{student_id}` |
| GET | `/api/students/{student_id}/attendance` |
| GET | `/api/analytics/overview` |
| GET | `/api/analytics/sessions/{session_id}` |
| GET | `/api/analytics/students/{student_id}` |
| GET | `/api/analytics/classes/{class_id}` |
| GET | `/api/analytics/attention` |
| POST | `/api/sessions/{session_id}/attendance/{student_id}/override` |
| DELETE | `/api/sessions/{session_id}/attendance/{student_id}/override` |

Errors are predictable and say nothing about internals:

| Code | Meaning |
|---|---|
| 400 | Known parameter, invalid value (unknown status, unsortable field) |
| 404 | Session, student or class does not exist |
| 422 | Malformed parameter (bad date, limit out of range, reason too short) |
| 500 | `{"detail": "Internal server error"}` — no traceback, no SQL, no path |

Filtering, sorting and pagination all happen **server-side**. Sort fields are
whitelisted, so a crafted `sort` value cannot reach SQL — a test fires
`sort=; DROP TABLE students` and asserts both a 400 and that the table
survives.

---

## Step 7 — Analytics Definitions

Every figure is arithmetic over stored rows. No model, no inference, no
generated prose.

**Only COMPLETED sessions count.** Scheduled, active and cancelled sessions are
excluded entirely — attendance is not final until a session is completed, and
analytics must not quietly contradict that Step 6 rule.

**"Satisfied the attendance rule" = `{PRESENT, LATE, LEFT_EARLY}`.** All three
met the policy's duration and ratio requirements; they differ only in when the
student arrived or left. `PARTIAL` and `ABSENT` did not.

```
                  completed sessions where the student's status is satisfying
attendance rate = ----------------------------------------------------------
                  completed sessions where that student was EXPECTED
```

The denominator is **expectation, not appearance**. A student expected and
never seen counts against the rate. Late, partial, early-leave and absence
rates use the same denominator.

**Multiple policy versions:** the record matching the *session's* policy
version wins; otherwise the most recent. The number of distinct versions is
always reported, so a mixed dataset is visible rather than hidden. Historical
records are never reinterpreted under a newer policy.

**Overrides** are counted by their *effective* status, and the override count
is reported separately so a corrected figure is never mistaken for a measured
one.

### NO DATA is not REAL ZERO

A rate with nothing to divide by returns **`null`**, not `0.0`, and the
dashboard renders it as "No data". A student who genuinely attended nothing
returns a real `0.0`. Both cases are asserted by tests, because showing a
confident 0% for an empty database is exactly the kind of quiet lie this
project is trying to avoid.

### "Needs review" is not anomaly detection

`/api/analytics/attention` lists a student only when, over at least 3 completed
sessions, a stated threshold is literally exceeded:

- attendance rate < 0.75, or
- late rate > 0.25, or
- absence rate > 0.25

The response includes the rule that produced the list and the numbers behind
each entry. No model is involved, and the API says so in the payload.

---

## Step 7 — Dashboard

Three views — Overview, Sessions, Students — with class and date filters.

- **Overview**: completed sessions, students in scope, attendance rate, and
  status counts; a status-distribution bar chart with its own data table; the
  "needs review" list; recent sessions.
- **Session detail**: metadata, policy version (and a warning when several
  exist), per-status counts, a filterable and sortable attendance table, and
  the **presence timeline**.
- **Student detail**: attendance rate, averages, status counts, a presence
  chart across sessions, and full history.

### Presence timeline

ClassVision's differentiating view: each student's verified presence drawn
against the session window, with gaps where they were not verified.

```
09:00 ─────────────────────────────────── 10:00
STU001  ████████████████████░░░░██████████
        present              gap    present
```

Each segment shows its start, end and duration on hover, and every segment is
also listed as text beneath the bar. Track references appear as evidence and
are explicitly labelled as technical references, **not identities**.

### Attendance explanation

Every row has a **"Why?"** button showing the stored evidence — first and last
verified presence, duration, session length, presence share, segment count,
times briefly lost, late and early-leave flags, and the policy version. These
are deterministic values read from the database, not generated explanations.

### Manual override

The same panel allows a correction: pick a status, give a reason (required),
optionally name yourself, confirm. Step 6 preserves the computed status, the
reason, the timestamp and the actor; re-evaluation never overwrites a
correction, and a correction can be undone.

Because there is no authentication, the actor field is an **unverified claim**,
and the UI says so.

---

## Step 7 — Accessibility

| Feature | Implementation |
|---|---|
| Landmarks | `header`, `nav`, `main`, `section`, `footer` |
| Skip link | first focusable element, targets `#main` |
| Focus | `:focus-visible` outline on every interactive element |
| Labels | all 9 form controls have associated `<label for>` |
| Tables | 36 `<th scope="col">` headers, 6 screen-reader captions |
| **Status without colour** | every status renders as **symbol + text** (`✓ PRESENT`, `▲ LATE`, `◐ PARTIAL`, `◀ LEFT EARLY`, `✕ ABSENT`) — readable in greyscale |
| Charts | `role="img"` with an `aria-label`, plus a textual list of every value |
| Live region | `role="status"` banner; `aria-busy` during loading |
| Dialog | native `<dialog>`, labelled, with `role="alert"` errors and a `fieldset`/`legend` |
| Dark mode | `prefers-color-scheme` token overrides |

> **Verified by static analysis, not by a browser.** Installing a browser
> automation stack for one step was not justified, so these properties are
> asserted against the served HTML/CSS/JS on every test run. Real screen-reader
> behaviour, real keyboard focus order and computed colour contrast were
> **not** measured.

---

## Step 7 — Responsive Design

Breakpoints at **900 px** and **600 px**. The layout is fluid: grids use
`minmax()`, the body has no fixed width, and the page is capped at 1400 px.

Wide tables scroll **inside their own container** (`overflow-x: auto`) with a
minimum width applied to the table, never to the page — which is the usual
cause of mobile horizontal overflow. A test asserts every table sits in a
scroll container and that no page-level `min-width` exists.

> **Verified structurally, not visually.** The CSS rules for 1920×1080,
> 1366×768, 768×1024 and 390×844 were written and asserted; the pages were
> **not** opened in a browser at those sizes. Visual confirmation remains
> outstanding.

---

## Step 7 — Measured Performance

Synthetic dataset: **30 students, 20 completed sessions, 600 attendance
records, 515 presence segments.** Built in 0.43 s.

| Endpoint | avg | p50 | p95 | max |
|---|---|---|---|---|
| `GET /api/health` | 2.62 ms | 2.54 | 3.00 | 3.15 |
| `GET /api/classes` | 2.68 ms | 2.63 | 3.07 | 3.50 |
| `GET /api/sessions` (25) | 3.10 ms | 3.06 | 3.66 | 3.80 |
| `GET /api/sessions/{id}` | 2.88 ms | 2.87 | 3.07 | 3.40 |
| `GET …/attendance` (30) | 3.32 ms | 3.22 | 4.15 | 4.31 |
| `GET …/presence` | 3.40 ms | 3.31 | 3.92 | 4.36 |
| `GET /api/students` (25) | 3.11 ms | 3.03 | 3.68 | 4.21 |
| `GET /api/analytics/overview` | 7.23 ms | 7.17 | 7.72 | 9.34 |
| `GET analytics/sessions/{id}` | 3.21 ms | 3.22 | 3.54 | 3.78 |
| `GET analytics/students/{id}` | 5.73 ms | 5.70 | 6.33 | 6.53 |
| `GET analytics/classes/{id}` | 7.49 ms | 7.40 | 8.17 | 9.03 |
| **`GET analytics/attention`** | **71.28 ms** | 71.26 | 75.07 | 75.07 |

Raw database queries: session attendance **0.092 ms**, completed-session join
**0.022 ms**. The database is not the constraint.

Frontend: **54.8 KB** total (HTML + CSS + JS), zero external requests, overview
payload **428 bytes**.

**The one slow endpoint is `attention`**, at roughly 10× the others. It calls
per-student analytics in a loop, so it issues N+1 queries. At 30 students it is
71 ms, which is tolerable for a local tool; at several hundred students it would
not be. It is listed as a known limitation rather than left to be discovered.

> Measured at this dataset size only. No claim is made about larger datasets,
> concurrent users, or production load.

---

## Step 7 — Privacy and Security Boundaries

**No endpoint can return** a face embedding, biometric vector, face crop,
camera frame, video, image, file path, database path, credential or secret.
This is enforced three ways:

1. API responses are built **field by field** in the service layer, so a new
   database column cannot leak through a refactor.
2. Response **schemas** declare every permitted field; nothing undeclared is
   serialised.
3. A test walks the JSON of **all 14 read endpoints** checking every key
   against a forbidden list, and a second test checks the **declared field
   names** in the OpenAPI schema. Both were verified to catch a planted leak.

The backend never opens the Step 3 embedding store. Biometric data and
attendance data remain in separate files.

**GET requests never modify data** — asserted by snapshotting every attendance
row before and after calling all 14 read endpoints.

**Authentication is implemented** — see
[Signing In](#signing-in--accounts-roles-and-the-first-run). Step 7 shipped without
it, deliberately: a fake login screen would have implied protection that did
not exist. What Step 7 did enforce, and still does: parameterised SQL
throughout, whitelisted sort columns, bounded and validated query parameters,
and error responses containing no traceback, SQL or file path. The server
binds to localhost.

---

## Step 7 — Limitations

- **No authentication or authorisation.** Anyone who can reach the port can
  read everything and record overrides. The `overridden_by` field is
  unverified free text. Do not expose this beyond localhost.
- **Attendance accuracy is still unvalidated** on real people, and inherits
  every Step 4 limitation — the recognition threshold remains the provisional,
  unvalidated 0.45.
- **Responsive and accessibility properties were verified statically**, not in
  a browser. No screenshots, no real screen-reader pass, no measured contrast.
- **`analytics/attention` issues N+1 queries** — 71 ms at 30 students, and it
  will degrade roughly linearly.
- **SQLite single-writer.** Fine for one teacher on one machine; concurrent
  writers are untested.
- **No date-range filtering** — a single exact date only.
- **No export** of any kind (no CSV, PDF or Excel) — that is a later step.
- **Session state cannot be changed from the dashboard.** Completing a session
  remains a deliberate operation outside the web layer, by design.
- Still no notifications, reports, AI assistant, live video, multi-camera, or
  cloud deployment.

## Step 8 — Live Classroom Session + Real-Time Dashboard

> **Step 8 runs the existing pipeline against a live camera and shows it on
> the existing dashboard. It adds no model, changes no threshold, and decides
> no attendance.** Attendance is still produced by the Step 6 rule engine,
> once, after the session has been explicitly stopped.
>
> **Attendance accuracy has NOT been validated on a real classroom
> population.** Authentication is still not implemented.

Until now the pipeline and the dashboard were two halves that never met: Steps
1-5 ran from a command-line demo window, and Step 7 read a database somebody
else had filled. Step 8 joins them, without collapsing any of the layers
between.

```
Webcam
  -> Live Session Manager      backend/services/live_service.py
  -> Camera source             ai/live_camera.py
  -> YuNet face detection      ai/face_detector.py          (Step 2, unchanged)
  -> Multi-person tracker      ai/person_tracker.py         (Step 5, unchanged)
  -> Recognition stability     ai/track_identity.py         (Step 5, unchanged)
  -> Presence timeline         ai/presence_timeline.py      (Step 5, unchanged)
  -> Attendance rules          ai/attendance_rules.py       (Step 6, unchanged)
  -> SQLite                    database/repositories.py     (Step 6, unchanged)
  -> FastAPI                   backend/routes/live.py
  -> Existing dashboard        backend/static/  (Live classroom tab)
```

**What is genuinely new is small, and that is the point:**

- `ai/live_camera.py` — open, read, release, with configuration
- `ai/live_session.py` — the runtime state machine and the worker thread
- `ai/live_pipeline.py` — coordinates the Step 5 pipeline and shapes a safe view
- `backend/services/live_service.py` — lifecycle, refusals, and finalisation
- `backend/routes/live.py` — four HTTP operations
- `ai/live_benchmark.py` — synthetic measurement of what Step 8 added

The frame loop itself is `TrackingSession` from Step 5, reused rather than
rewritten. Re-typing it would have created a second pipeline that could drift
from the tested one.

---

## Step 8 — Two Lifecycles, Kept Apart

There are two state machines, and merging them would have been the easiest
mistake to make.

**The lesson** (Step 6, stored in SQLite):

```
SCHEDULED -> ACTIVE -> COMPLETED
                    -> CANCELLED
```

**The camera loop** (Step 8, in memory only):

```
IDLE -> STARTING -> RUNNING -> STOPPING -> COMPLETED
             |          |          |
             +----------+----------+-------> FAILED
```

They are separate because *"the camera crashed"* and *"the lesson ended"* are
different claims. A `FAILED` camera never quietly completes a lesson, and a
completed lesson never depends on a process staying alive.

The stored session is only moved to `ACTIVE` **after** the camera has actually
opened. A start that fails leaves the database exactly as it was.

---

## Step 8 — Run It

```powershell
uvicorn backend.app:app --reload
```

Then open <http://127.0.0.1:8000/> and choose the **Live classroom** tab.
Interactive API docs are at <http://127.0.0.1:8000/docs>.

The dashboard does not create sessions — Step 7 already owns that, and a second
way to create them would be a second source of truth. Create one however you
already do (see `backend/seed_demo.py` for the shape), then:

1. pick a `SCHEDULED` or `ACTIVE` session in the Live classroom tab,
2. optionally set a camera index,
3. **Start camera**,
4. watch the live state,
5. **Stop and finalise**.

### Camera configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLASSVISION_CAMERA_INDEX` | `0` | which camera to open |
| `CLASSVISION_CAMERA_WIDTH` | `640` | requested width |
| `CLASSVISION_CAMERA_HEIGHT` | `480` | requested height |

```powershell
$env:CLASSVISION_CAMERA_INDEX="1"
uvicorn backend.app:app
```

A camera index in the API request wins over the environment variable, which
wins over the default.

**640×480 is the default because that is what was measured**, in Step 1 and
again in Step 5: ~15 FPS with 0 dropped frames. 1280×720 costs roughly three
times as much per frame for no detection benefit, so it is available by
configuration and is never assumed.

**Camera 0 is not assumed to be right.** It is the built-in webcam on some
machines and a virtual device on others. `python ai/camera_test.py --list`
reports which indexes actually work.

---

## Step 8 — Live API

Four operations, polled rather than pushed.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/live/start` | open the camera and begin processing |
| GET | `/api/live/status` | safe operational state, cheap to poll |
| GET | `/api/live/presence` | who has been verified, and where they are now |
| POST | `/api/live/stop` | stop, finalise attendance, return a summary |

**Why polling and not WebSockets.** The dashboard needs one small JSON object
about once a second. A WebSocket would add a connection lifecycle, a reconnect
policy and a second server protocol to maintain, in exchange for latency nobody
is waiting on. The dashboard already fetches JSON and has no build step.

### `/api/live/status`

```json
{
  "state": "RUNNING",
  "session_id": "SES-SMOKE",
  "class_id": "CLASS-SMOKE",
  "camera": "connected",
  "camera_index": 0,
  "elapsed_seconds": 19.4,
  "fps": 14.89,
  "processed_count": 250,
  "dropped_count": 0,
  "recognition_calls": 0,
  "active_tracks": 0,
  "recognized_students": 0,
  "unknown_tracks": 0,
  "worker_alive": true,
  "error": null
}
```

`IDLE` is a valid answer, not an error. `error` is always a short sentence
written to be read by a teacher — never a traceback and never a file path.

> **On the name `processed_count`.** It counts captures the pipeline processed.
> It is deliberately not called `processed_frames`: the project's privacy tests
> refuse any API field whose name contains `frame`, and weakening that check to
> accommodate a counter would have been the wrong trade. A separate test
> asserts the stronger property anyway — every value in a live response is a
> plain scalar, and every capture-related field is an integer.

### `/api/live/presence`

```json
{
  "session_id": "SES-SMOKE",
  "state": "RUNNING",
  "students": [
    {
      "student_id": "STU001",
      "student_name": "Student One",
      "identity_state": "CONFIRMED",
      "presence_state": "PRESENT",
      "first_seen": "2026-03-02T09:00:04.120+00:00",
      "last_seen": "2026-03-02T09:12:41.900+00:00",
      "duration_seconds": 757.8,
      "segment_count": 1
    }
  ],
  "unknown_tracks": 1,
  "recognized_students": 1,
  "note": "Provisional. These are live presence readings, not attendance..."
}
```

No similarity score is returned. The scores stay inside the identity manager,
where they are used to decide, and are never published as a per-person number a
teacher could misread as a confidence in a person.

### Live student states

These describe a person **operationally, while the lesson is running**. None of
them is an attendance status.

| State | Meaning |
|---|---|
| `PRESENT` | verified and visible right now |
| `RECOGNIZED` | verified, settling into the timeline |
| `TEMPORARILY_LOST` | verified, not visible right now, inside the tracker's grace period |
| `SEEN_EARLIER` | verified earlier in this session, not currently tracked |

`TRACKED` and `UNKNOWN` describe *tracks*, not students, so they never appear in
the per-student list. An unverified person is reported only as `unknown_tracks`,
a count.

**There is deliberately no live `ABSENT`.** Absence is a conclusion about a
finished session. A student who is not on camera at 09:20 has not left, and a
system that says otherwise trains a teacher to distrust it.

### `/api/live/stop`

Stops the camera, closes the timeline, marks the session `COMPLETED`, runs the
Step 6 rule engine, and writes the segments and decisions in **one**
transaction.

```json
{
  "session_id": "SES-SMOKE",
  "session_status": "COMPLETED",
  "state": "COMPLETED",
  "finalized": true,
  "already_finalized": false,
  "policy_version": "1.0",
  "expected_students": 1,
  "students_evaluated": 1,
  "segments_saved": 0,
  "segments_skipped_unenrolled": 0,
  "counts": {"ABSENT": 1},
  "elapsed_seconds": 28.6,
  "processed_count": 382
}
```

It is idempotent. A second call returns the same summary with
`already_finalized: true` and writes nothing, so a double-clicked button cannot
finalise a session twice.

### Refusals

| Situation | Response |
|---|---|
| a live session is already running | `409` |
| the session does not exist | `404` |
| the session is `COMPLETED` or `CANCELLED` | `409` |
| the camera cannot be opened | `503`, runtime `FAILED`, database unchanged |
| stop with nothing running | `409` |
| stop called twice | `200`, `already_finalized: true` |

---

## Step 8 — Concurrency

One worker thread owns the camera and the pipeline. Its contract is short:

- it is the only thing allowed to read the camera;
- **it never opens a database connection**;
- it never writes an image anywhere;
- it releases the camera in a `finally` block, whatever happened;
- any exception becomes runtime state `FAILED` with a safe message.

Every database write happens on the request thread, at `/stop`, inside one
transaction. That is what keeps SQLite out of a race without inventing a queue
or a second connection policy on top of the Step 7 thread-local connections.

The cost is stated rather than hidden: **presence evidence for a session whose
process is killed outright is lost**, because it was never written. A crash in
the *worker* is survivable — the runtime goes to `FAILED`, the camera is
released, and `/stop` still finalises whatever evidence was gathered.

The API stays responsive while the camera runs: measured below, and asserted by
a test that fires 22 requests during a live session.

---

## Step 8 — Dashboard

The Step 7 dashboard gained one tab. Nothing was redesigned, no framework was
introduced, and there is still no build step.

```
LIVE CLASSROOM

● Camera: connected   ● Session: RUNNING   ● Duration: 01:24   ● FPS: 14.8

Verified in this session
------------------------
STU001   Student One     PRESENT           09:00:04   09:12:41   12m 37s
STU002   Student Two     TEMPORARILY LOST  09:03:11   09:11:02    7m 51s

Currently unverified
--------------------
1  unverified person being tracked
```

It polls `/api/live/status` and `/api/live/presence` once a second, and **only
while the Live tab is open** — the camera keeps running when the teacher looks
at another tab; only the polling stops.

**The dashboard never fetches a picture.** There is no `<img>`, `<video>`,
`<canvas>`, `getUserMedia`, `EventSource` or `WebSocket` anywhere in it, and a
test enforces that. There is also no endpoint that could serve a frame even if
the page asked for one.

Every live state renders as a symbol plus its text, like every other status in
this dashboard, so nothing depends on colour alone.

---

## Step 8 — What Happens When You Press Stop

```
stop the camera
      |
      v
join the worker, release the camera (again - release is idempotent)
      |
      v
close the presence timeline  (anyone still on camera gets an exit event)
      |
      v
build presence segments      segments_from_events(), the Step 6 function
      |
      v
mark the session COMPLETED   because ABSENT requires a completed session
      |
      v
run the rule engine          AttendanceRuleEngine, unchanged
      |
      v
write segments + decisions   one transaction
```

The order is not negotiable. The session is completed *before* the engine runs,
because Step 6 refuses to conclude absence for a session that has not finished —
run it the other way round and every expected student would be `UNKNOWN`
forever.

**A manual correction is never overwritten.** Step 6's `save_decision` leaves
the override columns alone, and a Step 8 test proves the live path does not go
around it: correct a record after finalisation, re-run finalisation, and the
correction and its audit trail are still there.

**The policy version is preserved.** Attendance is written under the policy
version recorded on the session, not under whatever is current.

---

## Step 8 — Privacy Behaviour

Nothing in Step 8 changes the privacy model; it extends it to a live loop.

| Claim | How it is enforced |
|---|---|
| no classroom video is saved | the worker holds one frame, passes it to the pipeline, and drops the reference. Nothing writes an image file. |
| no face crop is saved | alignment happens in memory inside Step 3 code and is discarded |
| no embedding is exposed | the runtime has no field for one; the API schemas declare none |
| no similarity score is published | scores stay inside the identity manager |
| no frame reaches the API | `/status` and `/presence` are built field by field from a snapshot that has no image field |
| an unknown person never becomes attendance | a segment opens only at `IDENTITY_CONFIRMED`, unchanged from Step 6 |
| an unknown person is never named | the presence view lists only confirmed identities; unverified people are a count |
| errors leak nothing | failures are recorded as short sentences; a raw exception message never reaches the runtime |

Verified after a real camera session: no `.jpg`, `.png`, `.npy`, `.avi` or
`.mp4` was created anywhere in the project, and the camera was re-openable
immediately afterwards.

---

## Step 8 — Measured Performance

**Synthetic, on one machine, with generated frames and invented students.**
None of this is a claim about classroom performance or recognition accuracy.

```powershell
python ai/live_benchmark.py
```

### 1. Detection floor — real YuNet over generated frames

| Metric | Value |
|---|---|
| whole frame | 7.67 ms avg, 12.56 ms p95 |
| detection | 7.63 ms |
| tracking | 0.021 ms |
| recognition | 0.000 ms (no faces in generated frames) |

Generated noise contains no faces, so this is the **detection floor**, not a
populated classroom. Step 5 measured 13.2 ms with a real face in shot; the
difference is the work a real face creates, not a Step 8 improvement.

### 2. Live orchestration overhead — fakes everywhere else

| Metric | Value |
|---|---|
| whole frame, 4 synthetic faces | 0.27 ms avg, 0.62 ms p95 |
| recognition calls | 1,944 of 58,132 possible (**96.7% avoided**) |

With detection and recognition removed, what remains is what Step 8 itself
added: the worker loop, the runtime lock, the counters and the periodic
presence republish. **0.27 ms against a 65 ms frame budget** — the live layer is
not the bottleneck, and the Step 5 recognition scheduling is still in charge.

> **This figure moved between Step 8 and Step 9, and the reason is the
> benchmark, not the code.** Step 8 reported 0.15 ms here. Its fake detector
> emitted all-zero landmarks, which are degenerate geometry — harmless for
> Step 8, which never looked at them, but it meant the Step 9 liveness
> measurement rejected every sample before doing any work, and liveness looked
> free because only its failure path was being timed. The fixture now emits
> real landmark positions, which costs a little more inside the fake detector
> itself. Liveness is switched **off** for this section, so it still measures
> Step 8.

### 3. Presence view cost as a session ages

`presence_view()` rebuilds from the whole event list, so it was measured rather
than assumed:

| After | Events | Cost |
|---|---|---|
| 500 frames | 40 | 0.08 – 0.13 ms |
| 2,000 frames | 140 | 0.09 ms |
| 5,000 frames | 340 | 0.20 – 0.24 ms |

(Ranges are the spread across runs; these are sub-millisecond figures and the
run-to-run variance is comparable to the value.)

It grows, but slowly: the timeline emits a heartbeat every 60 frames rather
than an event per frame. It is republished every 15 frames, not every frame.

### 4. API latency while the camera runs

| Endpoint | avg | p95 |
|---|---|---|
| `/api/live/status` | 7.16 ms | 11.95 ms |
| `/api/live/presence` | 6.20 ms | 7.46 ms |

Measured against a camera paced at 15 FPS. The camera loop does not block
request handling.

### 5. Real camera, end to end

A real webcam, real YuNet and real SFace, driven entirely through the HTTP API:

| Metric | Value |
|---|---|
| resolution | 640×480 |
| measured FPS | **14.85** |
| captures processed | 382 |
| dropped | **0** |
| camera released after stop | yes — re-opened immediately afterwards |
| session status after stop | `COMPLETED` |

Nobody was in front of the camera and nobody was enrolled, so the session
finalised to `ABSENT` for its one expected student, with the reason recorded.
That is the correct answer, not a limitation — but it means **this run
exercised the plumbing, not recognition**.

---

## Step 8 — Limitations

- **Nothing here validates recognition.** No two people have been in front of
  this camera at once. Every multi-person result in the tests is synthetic, and
  the recognition threshold underneath it all is still the provisional,
  unvalidated **0.45**.
- **No liveness and no anti-spoofing.** A photograph held to the camera is not
  detected. That is Step 10.
- **Stopping early does not shorten the session window.** Attendance is
  calculated over the *scheduled* window, so stopping the camera thirty minutes
  into a one-hour session will make everyone look `PARTIAL` or `LEFT_EARLY`.
  This is the existing Step 6 rule behaving correctly, and changing it to
  flatter a demo would have been exactly the wrong fix. If a lesson genuinely
  ended early, the session's scheduled end is what needs correcting.
- **Evidence is written only at `/stop`.** A process killed mid-session loses
  the presence evidence it had gathered.
- **One camera, one session, one process.** No multi-camera, no multi-room, and
  the runtime dies with the process.
- **No policy store.** Finalisation uses the development default thresholds
  under the session's recorded policy version, because no table of policies
  exists yet.
- **A live session cannot be cancelled** — only stopped and finalised, or left
  `FAILED`.
- **The recovery path after a wedged worker is a restart.** If the worker does
  not stop within 10 seconds the runtime says so plainly rather than pretending
  the stop was clean.
- **The dashboard was verified statically**, as in Step 7 — no browser
  automation, no screenshots, no screen-reader pass.
- Still no authentication. Still localhost only.

---

## Step 9 — Liveness / Presentation Attack Detection

> **STEP 9 IS A RESEARCH PROTOTYPE. It is NOT production-grade presentation
> attack detection, and it is not a security control.**
>
> Its behaviour on real presentation attacks has **not been established**. No
> photograph, screen, replayed video or mask has ever been shown to this
> system. **Accuracy, FAR, FRR, APCER, BPCER and ACER: Not evaluated.**
>
> Enforcement is **off by default** for exactly that reason.

Steps 1-8 could answer *who* and *for how long*. Neither question notices a
phone held up to the camera. Step 9 adds the missing one: **did this come from
a person who was actually in the room?**

### The one rule the whole step rests on

```
recognised  does NOT imply live
live        does NOT imply recognised
```

A photograph of a student **is** that student's face, and recognition will say
so — correctly. What must not follow is that the student was present. And a
face that looks alive belongs to nobody until recognition says otherwise.

The two facts are produced by different code, held in different objects, and
shown in different columns. Nothing in the recognition path can set a liveness
state, and nothing in the liveness path can set, change or clear an identity —
a test asserts each direction.

```
Camera -> Detection -> Tracking
                          |
              +-----------+-----------+
              |                       |
        Face Recognition        Liveness / PAD
        (Steps 3-5)             (Step 9)
              |                       |
     Recognition Stability    Liveness Stability
        (Step 5)                 (Step 9)
              |                       |
              +-----------+-----------+
                          |
                 Presence Timeline    (Step 5, unchanged)
                          |
                 Attendance Evidence  (Step 9 - the eligibility layer)
                          |
                 Attendance Rules     (Step 6, UNCHANGED)
```

---

## Step 9 — What It Is Built From

**No model, no weights, no download, no new dependency.** Checked before
deciding:

- OpenCV 5.0 ships no presentation-attack model, so there was nothing already
  present to reuse;
- this project downloads model weights only with a documented source, licence
  and SHA-256. No PAD model met that bar without pulling in a deep-learning
  runtime the project deliberately does not have;
- a trained detector needs a PAD dataset with an **identity-disjoint** split.
  None was available, and using classmates' faces without authorisation and
  consent is not acceptable.

So Step 9 is a **documented deterministic baseline**, written in plain numpy,
whose limitations can be written down — rather than a heuristic dressed up as a
model.

| | |
|---|---|
| Baseline | `classvision-heuristic-pad` |
| Version | `0.9.0` |
| Weights / model file | **none** |
| Dependencies added | **none** (numpy only) |
| Trained | no |
| Evaluated on a PAD dataset | **no** |
| Cost | ~0.17 ms per checked face (measured) |

### The three cues

Every cue is measured over a **window of observations**. There is no
single-frame rule anywhere, and no cue is trusted alone.

**A. Non-rigid landmark dynamics — the primary cue.** The five landmarks are
normalised for translation **and** scale, then the variation of that normalised
configuration is measured across the window.

This is what separates a face from a photograph *of* a face. A real head is a
moving 3D object: eyes, nose and mouth shift relative to each other constantly,
even when somebody sits still. A flat print or a phone screen does not — and
because translation and scale are normalised away, that stays true when the
attacker **moves** the photo. Waving a print produces rigid motion with
near-zero internal deformation, which is exactly what this measures.

**B. Texture detail.** High-frequency energy in the face region, normalised by
the region's own intensity variance. Recapturing a print or a display loses
fine detail relative to overall contrast. The normalisation is deliberate: it
stops this degenerating into a brightness rule. A test asserts the cue does not
move when 60 is added to every pixel, or when contrast is scaled by 1.8.

**C. Texture stability over time.** How much cue B fluctuates. A replayed still
image gives an almost constant measurement; a real face under real light does
not.

**Blinking is deliberately not used.** It is easy to replay, easy to miss, and
a person who does not blink during the window is not a photograph.

### The decision rule

Cue A is **mandatory in both directions**; the texture cues can only support
it. That is deliberate — cue A has a physical argument behind it, while the
texture cues are a *model* of detail loss rather than a measurement of real
prints and screens. Letting texture decide alone would produce the two worst
errors available: calling a blurry, distant, very still student a SPOOF, and
calling a sharp photograph LIVE.

```
fewer than 6 usable observations   -> UNCERTAIN
nothing measurable                 -> UNCERTAIN
geometry deforming AND detailed    -> LIVE
geometry static AND (detail low OR texture frozen)
                                   -> SPOOF
anything else                      -> UNCERTAIN
```

---

## Step 9 — Four States, and UNCERTAIN Is a Real One

| State | Meaning |
|---|---|
| `UNKNOWN` | nothing has been assessed for this track yet |
| `LIVE` | two cues agree this looks like a physical person |
| `SPOOF` | two cues agree this looks like a presented image |
| `UNCERTAIN` | not enough evidence, conflicting evidence, or no analysis |

**Insufficient evidence never becomes LIVE.** Not when the face is too small,
not when landmarks are missing, not when the frame is unreadable, not when the
liveness layer itself has crashed. Every one of those paths is a test.

### Temporal stability

A single verdict decides nothing. `ai/liveness_stability.py` does for liveness
what `ai/track_identity.py` does for identity:

| | |
|---|---|
| to **set** a state | 3 consecutive agreeing verdicts |
| to **overturn** one | 5 consecutive agreeing verdicts |

The asymmetry mirrors Step 5's identity-switch protection (2 to set, 4 to
switch) and exists for the same reason — plus one specific to security: **a
confirmed SPOOF must not be clearable by holding up a photograph and then
briefly showing a real face.** That is precisely the sequence an attacker would
try, and a test runs it.

```
LIVE  LIVE  LIVE                  -> confirmed LIVE
SPOOF SPOOF SPOOF                 -> confirmed SPOOF
LIVE  SPOOF LIVE  SPOOF ...       -> UNCERTAIN, flagged unstable, never confirmed
```

**Oscillation is not a state.** A track that flips repeatedly without ever
confirming is reported UNCERTAIN and flagged; if it had already confirmed a
state, that state holds. Eligibility follows the *confirmed* state, so it
cannot flicker.

**An UNCERTAIN observation breaks a run in progress but never tears down a
confirmed state.** A student who turned away has not become a photograph.

### Liveness belongs to the track, not the person

```
Track 1 -> STU001  -> LIVE       -> counts
Track 2 -> STU002  -> UNCERTAIN  -> held for review
Track 3 -> UNKNOWN -> SPOOF      -> no attendance, and nobody is named
```

Every track has its own window, its own candidate and its own confirmation
count. Two tracks never share a buffer — a test confirms that ten confirmations
on one track confirm nothing on another. When a track exits, its runtime state
is released; its final verdict is archived, because finalisation runs long
after every track has gone.

A student who leaves and returns gets a **new track id** (Step 5 is explicit
that track ids are not identities), and that new track proves its liveness
again from scratch.

---

## Step 9 — What Happens to Attendance

`ai/attendance_rules.py` is **not modified**. A test greps it for the words
"liveness" and "spoof" and fails if either appears — if the rule engine ever
learns about liveness, the layering has collapsed.

Instead, `ai/attendance_evidence.py` sits in front of it:

```
segments (Step 6) -> eligibility (Step 9) -> rule engine (Step 6)
```

| Identity | Liveness | Eligibility |
|---|---|---|
| CONFIRMED | confirmed LIVE | `ELIGIBLE` |
| CONFIRMED | SPOOF | `BLOCKED_SPOOF` |
| CONFIRMED | UNCERTAIN / UNKNOWN | `BLOCKED_UNCERTAIN` |
| UNKNOWN | anything | no segment exists at all |

### Blocked is not absent — and making that true took a design change

Step 6 marks an expected student with no evidence `ABSENT` once a session
completes. That is correct when "no evidence" means "never appeared". But a
student whose evidence was *withheld* is not a student who never appeared: they
were seen, and the system declined to trust what it saw. Letting them fall
through to `ABSENT` would convert **"we could not verify this"** into **"they
were not here"** — a different and much stronger claim.

So under enforcement, a student whose evidence is **entirely** withheld is also
withheld from evaluation and receives **no attendance record** for that
session, rather than a confident wrong one. "The system cannot say" is an
honest outcome; Step 7's analytics already count sessions without a record, and
the stop summary names every student it happened to.

A student with **some** eligible evidence is evaluated normally on that
evidence — only the untrusted part is dropped. One student seen genuinely and
later presented as a photograph keeps the genuine presence and loses only the
presented part, because liveness is judged per track.

**Held-back evidence is still stored.** A teacher cannot review a decision
whose evidence was deleted.

### Spoof handling

When spoof evidence is confirmed, the system does **not** delete the student,
mark them absent, end the session, change their identity, or touch the
recognition threshold. It holds their evidence back, says so, and leaves the
decision to a person.

---

## Step 9 — Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLASSVISION_LIVENESS_MODE` | `report` | `off`, `report` or `enforce` |
| `CLASSVISION_LIVENESS_INTERVAL` | `3` | frames between checks, per track |

```powershell
$env:CLASSVISION_LIVENESS_MODE="enforce"
uvicorn backend.app:app
```

| Mode | Analysis | Shown on the dashboard | Filters attendance |
|---|---|---|---|
| `off` | no | no | no — Step 8 behaviour exactly |
| `report` | yes | yes | **no** — reports what it *would* have blocked |
| `enforce` | yes | yes | yes |

**`report` is the default, and that is a deliberate safety choice.** This
baseline has never been evaluated against a real attack. Letting it silently
delete a real student's attendance would trade a known harm for an unmeasured
benefit. Enforcement is one environment variable away and is fully tested.

The sampling interval is not only a saving. At the measured ~15 FPS consecutive
frames are 66 ms apart and a face barely moves between them; spacing the
samples out is what makes the motion cue measurable at all.

---

## Step 9 — Live API and Dashboard

No new API. The four Step 8 operations were extended, because liveness is not a
separate session — it is another fact about the one that is running.

`/api/live/status` gained `liveness_mode`, `liveness_available`,
`baseline_version`, `live_tracks`, `spoof_tracks`, `uncertain_tracks`,
`liveness_checks`.

`/api/live/presence` gained a `liveness_state` column per student and a
**track-level** view:

```json
{
  "track_id": 7,
  "student_id": "STU001",
  "identity_state": "CONFIRMED",
  "liveness_state": "LIVE",
  "liveness_confirmed": true,
  "presence_state": "PRESENT",
  "attendance_eligibility": "ELIGIBLE"
}
```

`/api/live/stop` gained a `liveness` block reporting what the eligibility layer
did, including `withheld_students`.

**No liveness score is published, under any name.** The cues are uncalibrated
measurements. A number printed beside a person's name would be read as a
confidence *about that person*, which it is not. Cue values are reachable from
`ai/liveness_test.py --calibrate` and from tests, and from nowhere else.

The dashboard's Live Classroom tab gained a Liveness column, a "Counts" column,
and a Tracked people table listing everyone — identified or not — with identity
and liveness side by side. Eligibility renders as "Counts" or "**Held for
review**", never as anything resembling an attendance status.

```
Student   Identity   Live state   Liveness      Counts
------------------------------------------------------------
STU001    Alice      PRESENT      ● LIVE        Counts
STU002    Rahul      PRESENT      ⚠ UNCERTAIN   Held for review

Tracked people
Track  Identity   Liveness      Presence   Counts
------------------------------------------------------------
1      STU001     ● LIVE        PRESENT    Counts
3      ? UNKNOWN  ⚠ SPOOF       TRACKED    Held for review
```

Every state renders as a symbol **plus its text**, so nothing depends on
colour. No image, video, canvas, `getUserMedia`, `EventSource` or `WebSocket`
was added — a test enforces that, and there is no endpoint that could serve a
frame even if the page asked.

---

## Step 9 — Failure Safety

> **"Liveness unavailable" must never mean "everybody is live".**

The whole liveness block in the pipeline is wrapped. If it raises — a broken
model, a corrupt frame, anything — the session **keeps running**, the failure
is **latched**, and every view reports UNCERTAIN until a check actually
succeeds again. `liveness_available` turns false on the dashboard.

**The latch is the part that needed a second look, and the Step 9 audit found
why.** Recording an unavailable observation is not enough on its own: the
stability layer deliberately refuses to tear down an already-confirmed state on
one bad observation, which is right for a student who briefly turned away and
*wrong* for a checker that has died. Before the fix, a track confirmed LIVE and
then orphaned by a broken detector kept reporting LIVE and ELIGIBLE — exactly
the failure the rule above forbids. Now a failed layer reports UNCERTAIN for
every track, including ones it had confirmed a moment earlier, and the
finalisation view agrees, so nothing a dead checker once said can become
attendance.

The latch clears **only when a check actually runs and returns**. Liveness
samples every third frame, so two frames in three complete the check pass
without measuring anything; treating those as recovery would clear the failure
almost immediately and hand the LIVE verdict straight back. One transient
failure still does not poison the rest of the lesson — a working detector
clears it on its next real check.

Four tests cover this: a detector that raises on *every* call keeps the session
`RUNNING`, reports **0** live tracks and can still be finalised; a confirmed
LIVE verdict is **withdrawn** when the checker dies, with 0 eligible segments
reaching the rule engine; the latch holds across every frame of a failure and
clears only on a real successful check; and an exception whose message contains
a model path and an API token leaks neither — only the exception **type**
survives.

---

## Step 9 — Measured Performance

**Synthetic unless stated.** `python ai/live_benchmark.py`

### Liveness off vs on — same frames, same machine, one process

20,000 frames, 4 synthetic faces, 96 px each, checked every 3rd frame:

| | liveness off | liveness on |
|---|---|---|
| frame latency avg | 0.060 ms | 0.293 ms |
| frame latency p95 | 0.071 ms | 0.758 ms |
| detection | 0.030 ms | 0.033 ms |
| tracking | 0.022 ms | 0.026 ms |
| **recognition calls** | **2672** | **2672** |
| recognition calls / frame | 0.1336 | 0.1336 |
| liveness checks | 0 | 26,668 |
| dropped frames | 0 | 0 |

**Liveness overhead: 0.22 – 0.90 ms per frame with 4 faces — roughly
0.06 – 0.22 ms per face per frame, and 0.3% – 1.4% of the 65 ms frame budget**
Step 1 measured.

A **range**, because that is what was measured. Within any one session five
consecutive runs cluster tightly (0.781 – 0.838 ms in one such sample), but the
figure moves between sessions as the machine's load and power state change —
and the *liveness-off* column moves with it (0.060 – 0.237 ms across the same
runs). These are sub-millisecond measurements on a busy laptop; quoting a
single decimal would imply a precision the method does not have. The conclusion
is the same at either end of the range: liveness is not the bottleneck, and
detection at ~13 ms still dominates the frame.

The comparison uses a **fixed frame count**, not a fixed duration. Time-boxing
would let the slower column process fewer frames and therefore make fewer
recognition calls, which looks exactly like liveness having changed recognition
scheduling when it has not. **Recognition calls are identical in both columns**,
which is the check that Step 5's scheduling was left alone.

### Cost per checked face, by face size

| Face size | observe | assess | total |
|---|---|---|---|
| 64 px | 0.084 ms | 0.073 ms | 0.156 ms |
| 96 px | — | — | ~0.174 ms |
| 120 px | 0.108 ms | 0.058 ms | 0.166 ms |
| 200 px | 0.359 ms | 0.064 ms | 0.423 ms |

The measurement crops the face **before** converting to grayscale. Converting a
whole 640×480 frame first cost 2.67 ms; cropping first brought it to 0.15 ms —
an 11× reduction for a one-line reordering.

### Real webcam, end to end through the HTTP API

| | Step 8 baseline | Step 9 report | Step 9 enforce |
|---|---|---|---|
| resolution | 640×480 | 640×480 | 640×480 |
| measured FPS | 14.85 | **14.87** | **14.98** |
| dropped frames | 0 | **0** | **0** |
| camera released | yes | yes | yes |

**No measurable regression.** Note what this run does *not* show: nobody was in
front of the camera and nobody was enrolled, so it exercised the plumbing, not
the cues.

---

## Step 9 — Calibration and Evaluation

```powershell
python ai/liveness_test.py --calibrate
```

### Where the thresholds came from

The **texture** thresholds are anchored on **real measurements**.
`texture_detail()` was measured over 40 real frames from this machine's webcam
at 640×480, on a 120×120 region:

| | range | median |
|---|---|---|
| real, sharp | 0.684 – 2.614 | 1.263 |
| one blur pass | 0.111 – 0.406 | 0.202 |
| two blur passes | 0.062 – 0.248 | 0.114 |
| three blur passes | 0.042 – 0.183 | 0.079 |

A blur pass costs roughly 6× of this statistic. `DETAIL_LIVE_MIN = 0.25` sits
below the quietest real sharp frame; `DETAIL_SPOOF_MAX = 0.10` sits below a
twice-blurred one. The gap between them is a deliberate **UNCERTAIN band** — a
cue forced to choose for every value would have no way to say "I cannot tell".

The **motion** thresholds come from synthetic sequences, because a photograph's
defining property — zero non-rigid deformation under any amount of waving — can
be reproduced exactly. They are set conservatively so that "very still real
person" lands in UNCERTAIN rather than SPOOF: wrongly accusing a student is the
more damaging error.

### Measured cue separation on synthetic sequences

| sequence | motion | detail | variation | verdict |
|---|---|---|---|---|
| live | 0.064 – 0.074 | 0.50 – 0.52 | 0.62 – 0.64 | LIVE ×12 |
| spoof (static image, moved) | 0.000 | 0.106 – 0.108 | 0.013 – 0.026 | SPOOF ×12 |
| very still real face | 0.005 – 0.006 | 0.50 – 0.52 | 0.62 – 0.64 | **UNCERTAIN** ×12 |

The synthetic spoof carries **sensor noise on top of the frozen image**,
because a camera pointed at a photograph still produces noise. Without it the
texture-stability cue would separate the two perfectly and for the wrong
reason — it would be detecting a digitally identical frame, which is not the
attack being modelled.

### Evaluation

```
Dataset:     none
Accuracy:    Not evaluated
Precision:   Not evaluated
Recall:      Not evaluated
F1:          Not evaluated
ROC-AUC:     Not evaluated
FAR / FRR:   Not evaluated
APCER / BPCER / ACER:  Not evaluated
```

**Not "pending". Not "approximately". Not evaluated.** No PAD dataset was
available or licensed, no real attack has been presented to the system, and the
calibration above is circular — the sequences were generated by the same file
that measures them. That circularity is stated rather than hidden, and it is
why no accuracy figure is derived from it.

An honest evaluation would need a PAD dataset with an **identity-disjoint**
split (never the same person's frames on both sides of the split), a documented
attack taxonomy, and a protocol fixed before the numbers are looked at. None of
that exists here yet.

---

## Step 9 — Privacy

Nothing changes in the privacy model; Step 9 lives inside it.

| Claim | How it is enforced |
|---|---|
| no frame is stored | the measurement is taken and the frame reference dropped |
| no face crop is stored | the crop is a numpy *view*, used and discarded within one function |
| no embedding enters liveness | liveness never sees one; it takes a frame and a box |
| no embedding is logged | `LivenessSample.__slots__` is `frame_index, shape, detail, usable, reason` — there is nowhere to put one, and a test proves assigning one raises |
| no cue value is exposed | the verdict has no score field; the API has no score field |
| no model path leaks | a test raises an error containing a path and a token and greps every response |
| no new database table | a test asserts the schema still has exactly its original 6 tables and contains no liveness column |

What a track actually retains: **five normalised (x, y) points and two
floating-point numbers per observation**, for at most 12 observations.

Verified after real camera sessions: no `.jpg`, `.png`, `.npy`, `.avi` or
`.mp4` was created anywhere in the project.

---

## Step 9 — Known Failure Modes

These are the ways this baseline is expected to be wrong. They are listed
because a security control whose failure modes are unknown is worse than none.

1. **Detector landmark noise is the biggest unknown.** The motion cue assumes
   YuNet's landmark jitter on a static image is smaller than a real face's
   deformation. That has **not been measured on this camera**, because no face
   — real or presented — was available. If the jitter is larger than assumed, a
   real photograph could read as LIVE. This is the single most important gap.

2. **A photograph rotated in depth may deform projectively.** Tilting a print
   changes the apparent landmark geometry. Cue A could read that as
   deformation.

3. **A replayed video of a person defeats cue A by design.** It contains real
   facial deformation, because it is a recording of real facial deformation.
   Nothing here addresses it.

4. **Masks, prosthetics and 3D attacks** are not addressed at all.

5. **Deepfakes are explicitly out of scope** — that is a different problem and
   is not attempted.

6. **Adversarial attacks** — an attacker who reads this file knows the cues,
   the thresholds and the window length. Nothing here is robust to somebody
   optimising against it.

7. **The texture thresholds are the least transferable numbers** in Step 9.
   They were anchored on real camera imagery but on a *scene*, not on a face,
   and print grain and display moiré are modelled as blur rather than measured.

8. **Realistic behaviour is UNCERTAIN-dominant, not SPOOF-dominant.** The
   thresholds are set conservatively to avoid falsely accusing students, so the
   likely real-world outcome is that most tracks are undecided. Under
   enforcement that means evidence is held back rather than attacks being
   caught, which is why `report` is the default.

9. **A liveness verdict under enforcement is not persisted.** The stop summary
   and live API report it, but no audit row is written, because that would need
   a schema change Step 9 chose not to make.

10. **None of this is validated on real people.** No two people have been in
    front of this camera at once, in Step 9 any more than in Step 8.

**Not claimed anywhere:** resistance to sophisticated deepfakes, advanced
replay attacks, high-quality masks or adversarial attacks. Not claimed:
classroom liveness performance, or any accuracy figure whatsoever.

---

## Step 10 — Video Authenticity Analysis

> **STEP 10 IS A RESEARCH BASELINE. It is NOT a deepfake detector, it is NOT
> production-grade security, and it has NEVER been evaluated against real
> manipulated video.**
>
> **Accuracy, Precision, Recall, F1, ROC-AUC, FAR, FRR, APCER, BPCER and
> ACER: Not evaluated.** Not "pending". Not "approximately". Not evaluated.
>
> It is **off by default**.

Steps 1–8 answer *who* and *for how long*. Step 9 asks whether a face came
from a person rather than a held-up object. Step 10 asks a third question that
neither of the others can see:

> **Did this video come from this camera — or from a generator?**

A printed photograph is a liveness problem. A face-swapped video of a person
who really is moving passes liveness comfortably, because it *is* a moving,
deforming, detailed face. That gap is what Step 10 exists to look at.

### Three questions, three layers, never merged

```
Identity      (Steps 3-5)   who does this face resemble?
Liveness      (Step 9)      is this a flat presented object?
Authenticity  (Step 10)     was this region generated rather than captured?
```

```
recognised  does NOT imply live
live        does NOT imply recognised
authentic   does NOT imply either
suspicious  does NOT imply spoof
```

Measured on the real pipeline, all four combinations coexist:

| Sequence | Identity | Liveness | Authenticity | Eligibility |
|---|---|---|---|---|
| coupled + known | CONFIRMED | LIVE | AUTHENTIC | ELIGIBLE |
| inverted + known | CONFIRMED | UNCERTAIN | SUSPICIOUS | BLOCKED_SUSPICIOUS |
| decoupled + known | CONFIRMED | **LIVE** | **UNCERTAIN** | BLOCKED_UNVERIFIED_SOURCE |
| coupled + unknown | UNKNOWN | LIVE | AUTHENTIC | *(no segment exists)* |

Row 3 is the point of the whole step: a track that liveness is happy with and
authenticity is not. Row 4 is the security rule holding: an unknown person
produces no presence segment, so nothing downstream can turn them into
attendance however authentic they look.

---

## Step 10 — Model Research, and Why There Is No Model

The search was run before any code was written, and the result is recorded so
it can be re-examined when it changes.

**The official OpenCV model zoo carries 24 models. None is a forgery,
authenticity or anti-spoofing detector.** That is the one source that already
meets this project's provenance bar — a pinned URL and a pinned SHA-256, the
same standard YuNet and SFace are held to — and it had nothing to offer.

Every alternative failed at least one hard requirement:

| Candidate | Blocker |
|---|---|
| DFDC winner (EfficientNet-B7 ensemble) | needs PyTorch (~2 GB); ~1.7 GB of weights; hopeless against a 65 ms frame budget |
| FaceForensics++ XceptionNet | FF++ requires a signed EULA with TUM; derived weights are not freely redistributable |
| MesoNet | the original is Keras; the ONNX conversions live on unvetted repos with no published hash — a "random downloaded checkpoint" |
| CViT / EfficientViT deepfake | PyTorch, 100 MB+, research-grade provenance |
| Deepware and similar | proprietary / service-backed |

And one objection outlives all of them: **every credible detector is trained
on FF++, DFDC or Celeb-DF** — corpora of high-quality face-swap *video*, not
640×480 webcam feeds of a classroom. Cross-dataset and cross-compression
generalisation for these models is known to collapse, so their published
benchmarks would not transfer — and there is no dataset here on which to
re-measure them honestly.

Installing a 2 GB deep-learning runtime to host a model whose accuracy would
be unknown, unmeasurable and probably irrelevant is not a trade worth making.

**So Step 10 adds no model, no weights, no download and no dependency.** It is
plain numpy, and it is labelled a baseline because that is what it is.

| | |
|---|---|
| Baseline | `classvision-authenticity-research-baseline` |
| Version | `0.10.0` |
| Model file / weights | **none** |
| Model SHA-256 | **N/A — there is no model** |
| Dependencies added | **none** |
| Trained | no |
| Dataset | none |
| Evaluated | **no** |

---

## Step 10 — How It Works

### The physical idea

A real camera produces **one coherent image**. The face and the wall behind it
share a sensor, an exposure curve, a noise floor and a compressor. When the
light changes, both change together.

A face swapped or synthesised into the frame is produced by a *different
process* from its own surroundings. The two become statistically **decoupled**,
and that decoupling is measurable without knowing anything about how the face
was generated.

Every cue compares the face against a ring of context just outside it. None
looks at the face alone, because a face on its own says nothing about whether
it belongs to the frame it is in.

**A. Boundary sharpness ratio.** Fine detail inside the face against fine
detail in the ring. Generators suppress high frequencies.

**B. Temporal coupling — the primary cue.** How strongly face brightness
tracks context brightness, frame to frame. Under one camera they move
together. A region generated per-frame drifts on its own schedule. **A
SUSPICIOUS verdict requires this cue**; the texture cues may only support it.

**C. Chroma offset.** Colour difference between face and ring, where blending
seams and colour-transfer errors show up.

### A deliberate weakness, recorded rather than hidden

A true **pixel-level** residual between consecutive frames would be a stronger
cue B than the statistical one used here. It is not used, because it would mean
holding face **pixels** from one frame to the next — and the guarantee that
nothing retains an image is worth more than the extra sensitivity.

What a track keeps is **six floating-point numbers and two flags per
observation**. `AuthenticitySample` has `__slots__`, so there is nowhere to put
a picture and an attempt to attach one raises.

### The four states

| State | Meaning |
|---|---|
| `UNKNOWN` | nothing assessed for this track yet |
| `AUTHENTIC` | the face region behaves like part of this camera's image |
| `SUSPICIOUS` | the face region behaves as if produced separately from its surroundings — **not** a finding that a deepfake was used |
| `UNCERTAIN` | not enough evidence, conflicting evidence, or no analysis |

`INSUFFICIENT_EVIDENCE` is a **reason**, not a state. It appears in the
explanation attached to an UNCERTAIN verdict, because a teacher needs to know
the difference between "we looked and cannot tell" and "we have not looked
enough yet" — while everything downstream needs both to behave identically,
which they do.

### Temporal stability

A single verdict decides nothing. `ai/authenticity_stability.py` does for
authenticity what Step 5 does for identity and Step 9 for liveness:

| | |
|---|---|
| to **set** a state | 3 consecutive agreeing verdicts |
| to **overturn** one | 5 consecutive agreeing verdicts |

```
AUTHENTIC  AUTHENTIC  AUTHENTIC           -> confirmed AUTHENTIC
SUSPICIOUS SUSPICIOUS SUSPICIOUS          -> confirmed SUSPICIOUS
AUTHENTIC  SUSPICIOUS AUTHENTIC ...       -> UNCERTAIN, flagged unstable
```

Oscillation is not a state. An UNCERTAIN observation breaks a run in progress
but never tears down a confirmed state.

**Track-level, always.** Every track has its own window, candidate and
confirmation count. Ten confirmations on one track confirm nothing on another,
and a returning person gets a new track id that must prove itself again.

---

## Step 10 — Calibration

```powershell
python ai/authenticity_test.py --calibrate
```

### The window size was measured, not chosen

Cue B is a correlation over consecutive differences, and a correlation from a
short series is mostly noise. Measured on **400 real observations** from this
machine's webcam with a face present, the *same* real video gives:

| Window | Coupling seen across the run |
|---|---|
| 8 samples | −0.823 … +0.988 — unusable; a real face looks decoupled |
| 14 | −0.268 … +0.940 |
| 20 | +0.003 … +0.929 |
| 30 | +0.161 … +0.924 |
| 40 | +0.233 … +0.887 |
| **60** | **+0.427 … +0.892 — stable** |

The median sits at about **+0.75** at every window length; only the *spread*
changes. A short window does not measure a different thing badly — it measures
the right thing too noisily, and at 8 samples it would have produced constant
false SUSPICIOUS verdicts on ordinary students.

So: **60 samples minimum, 80-sample window, every 3rd frame ≈ 12 seconds**
before a first verdict. That is slow, and it is the honest price of an estimate
that means anything. Until then the track is UNCERTAIN.

### Thresholds

| Threshold | Value | Where it came from |
|---|---|---|
| `COUPLING_AUTHENTIC_MIN` | `+0.35` | below the weakest real 60-sample reading (+0.427) |
| `COUPLING_SUSPICIOUS_MAX` | `−0.35` | past 3 SD of an uncorrelated signal |
| `SHARPNESS_AUTHENTIC_MIN` | `0.18` | model of generator behaviour — **not** anchored on real data |
| `SHARPNESS_SUSPICIOUS_MAX` | `0.06` | as above |
| `CHROMA_SUSPICIOUS_MIN` | `26.0` | as above |

### Measured separation

| Sequence | Coupling | Verdict |
|---|---|---|
| coupled | +0.9998 … +0.9999 | AUTHENTIC ×10 |
| inverted | −0.9999 … −0.9998 | SUSPICIOUS ×10 |
| decoupled | −0.041 … +0.046 | **UNCERTAIN ×10** |
| smooth but coupled | +0.9998 … +0.9999 | **UNCERTAIN ×10** |

### What the cue cannot do — the most important limitation

**Coupling only carries information when the scene's illumination actually
changes.** Two runs on this machine's webcam, same code:

```
scene with varying illumination   coupling ~ +0.75
static, evenly lit scene          coupling ~ +0.05
```

In a static room, face and context are both dominated by independent sensor
noise, so the correlation collapses towards zero — **which is exactly where a
genuinely decoupled region also sits.** The cue degenerates and the two cases
become indistinguishable.

That is why `COUPLING_SUSPICIOUS_MAX` is at −0.35 and not at 0. Only
genuinely **anti-correlated** evidence can reach it. The consequences, stated
plainly:

- a merely **independent** face region is **not** flagged — only an inverted one;
- in an ordinary, evenly lit classroom this baseline will report **UNCERTAIN
  for most people** and will **rarely report SUSPICIOUS at all**;
- it is built to miss manipulations rather than accuse students, because a
  false SUSPICIOUS harms a real person and a missed one leaves the system
  exactly as safe as it was before Step 10 existed.

---

## Step 10 — Attendance Integration

`ai/attendance_rules.py` is **not modified**. A test greps it for
"authenticity", "deepfake", "suspicious" and "manipulat" and fails if any
appears.

```
segments (Step 6)
      -> liveness gate      (Step 9)
      -> authenticity gate  (Step 10)
      -> rule engine        (Step 6, untouched)
```

Two **independent** gates, each with its own mode. A blocking result from
either blocks; the narrower finding wins when both fire.

| Identity | Authenticity | Eligibility |
|---|---|---|
| CONFIRMED | confirmed AUTHENTIC | `ELIGIBLE` |
| CONFIRMED | SUSPICIOUS | `BLOCKED_SUSPICIOUS` |
| CONFIRMED | UNCERTAIN / UNKNOWN | `BLOCKED_UNVERIFIED_SOURCE` |
| UNKNOWN | anything | *no segment exists at all* |

`BLOCKED_SUSPICIOUS` is named separately from Step 9's `BLOCKED_SPOOF` on
purpose: "this looked like a held-up photograph" and "this looked like
manipulated video" are different findings, and a teacher reviewing them needs
to know which one fired.

### A gate in report mode has an opinion and no power

Found while wiring the second gate, and worth stating because it would have
been a quiet disaster: before the fix, switching authenticity to `enforce`
made the **report-only liveness opinion start filtering evidence too**. Turning
on one gate silently enforced the other.

Only a gate that is *enforcing* may remove a segment. A regression test asserts
it in both pairings.

### Blocked is not absent

Step 9 established this and Step 10 preserves it: a student whose evidence is
entirely withheld is also withheld from *evaluation*, and receives **no
attendance record** rather than a confident `ABSENT`. "The system cannot say"
is an honest outcome; "they were not here" is a much stronger claim and a
false one. Held-back evidence is still **stored**, because a teacher cannot
review a decision whose evidence was deleted.

---

## Step 10 — Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLASSVISION_AUTHENTICITY_MODE` | **`off`** | `off`, `report` or `enforce` |
| `CLASSVISION_AUTHENTICITY_INTERVAL` | `3` | frames between checks, per track |

```powershell
$env:CLASSVISION_AUTHENTICITY_MODE="report"
uvicorn backend.app:app
```

**Off by default — a stronger position than Step 9 took**, for two reasons:

1. **Step 9's default must not change.** The eligibility column is shared, so a
   second gate switched on by default would start reporting every student's
   evidence as unverified-source the moment Step 10 landed, silently altering
   what Step 9 means.
2. **This baseline has no validation at all.** Liveness at least had a physical
   argument and real-camera anchoring. A gate with zero validation, on by
   default, would mark almost everything "unverified" and teach a teacher to
   ignore the column — worse than not having it.

Unchanged by Step 10: `CLASSVISION_RECOGNITION_THRESHOLD` is still **0.45**
(asserted by a test), and `CLASSVISION_LIVENESS_MODE` still defaults to
`report`.

---

## Step 10 — API and Dashboard

No new API. The four Step 8 operations were extended, because authenticity is
not a separate session — it is another fact about the one that is running.

`/api/live/status` gained `authenticity_mode`, `authenticity_available`,
`authenticity_baseline_version`, `authentic_tracks`, `suspicious_tracks`,
`unverified_source_tracks`, `authenticity_checks`.

`/api/live/presence` gained an `authenticity_state` column per student and per
track:

```json
{
  "track_id": 1,
  "student_id": "STU001",
  "identity_state": "CONFIRMED",
  "liveness_state": "LIVE",
  "authenticity_state": "UNCERTAIN",
  "attendance_eligibility": "BLOCKED_UNVERIFIED_SOURCE"
}
```

**No authenticity score is published, under any name.** The cues are
uncalibrated. A number printed beside somebody's name would be read as a
probability that *this person* is fake, which it is not. Cue values are
reachable from `ai/authenticity_test.py --calibrate` and from tests, and
nowhere else.

The Live Classroom tab gained an **Authenticity** column in both tables, a
status card, and a research notice. Every state renders as a **symbol plus its
text**, so nothing depends on colour. The dashboard never claims a deepfake was
detected.

---

## Step 10 — Failure Safety

> **"Authenticity unavailable" must never mean "everybody is authentic".**

If the layer raises, the session **keeps running**, the failure is **latched**,
and every view reports UNCERTAIN until a check actually succeeds again.

The latch is the part that matters, and Step 10 was built with it rather than
waiting to repeat Step 9's audit finding. Recording an unavailable observation
is not enough on its own: the stability layer correctly refuses to drop a
confirmed state on one bad observation — right for a student who moved, wrong
for a checker that has died. So a confirmed AUTHENTIC verdict is **withdrawn**
when the checker dies, finalisation included.

The latch clears **only when a check actually runs and returns**. Two frames in
three complete the pass without measuring anything; treating those as recovery
would hand the AUTHENTIC verdict straight back.

The two layers fail **independently**: a test breaks liveness and asserts
authenticity carries on unaffected.

---

## Step 10 — Measured Performance

**Synthetic unless stated.** Fixed 1,000 frames, one tracked face, same
process, liveness in `report` for both columns:

| | without Step 10 | with Step 10 |
|---|---|---|
| frame latency avg | 0.088 ms | 0.267 ms |
| frame latency p95 | 0.219 ms | 0.761 ms |
| frame latency max | 5.981 ms | 1.843 ms |
| **recognition calls** | **35** | **35** |
| **liveness checks** | **334** | **334** |
| authenticity checks | 0 | 334 |
| dropped frames | 0 | 0 |

**Step 10 overhead: 0.179 ms per frame — 0.3% of the 65 ms frame budget.**

Recognition and liveness call counts are **identical** in both columns, which
is the check that Step 10 changed neither.

### Cost per checked face

| | avg | p95 |
|---|---|---|
| observe (measure one frame) | 0.836 ms | 1.016 ms |
| assess (verdict from window) | 0.241 ms | 0.269 ms |
| combined | 1.076 ms | 1.287 ms |

### Real webcam, end to end through the HTTP API

| | value |
|---|---|
| resolution | 640×480 |
| measured FPS | **29.85** |
| captures processed | 3,342 |
| dropped | **0** |
| liveness checks / authenticity checks | 195 / 195 |
| camera released after stop | yes |
| session lifecycle | `SCHEDULED → COMPLETED` |

Nobody was in front of the camera and nobody was enrolled, so this run
exercised the **plumbing, not the cues**.

---

## Step 10 — Evaluation

```
Dataset:     none
Training:    NO

Accuracy:    NOT EVALUATED
Precision:   NOT EVALUATED
Recall:      NOT EVALUATED
F1:          NOT EVALUATED
ROC-AUC:     NOT EVALUATED
FAR:         NOT EVALUATED
FRR:         NOT EVALUATED
APCER:       NOT EVALUATED
BPCER:       NOT EVALUATED
ACER:        NOT EVALUATED
```

No PAD or deepfake dataset was available or licensed, no manipulated video has
ever been presented to this system, and the synthetic calibration is circular —
the sequences were generated by the same file that measures them. That
circularity is stated rather than hidden, and it is why **no accuracy figure is
derived from it**.

An honest evaluation would need a deepfake corpus with an **identity-disjoint**
split (never the same person on both sides), a documented attack taxonomy, a
fixed seed and a protocol agreed before the numbers are looked at. None of that
exists here.

---

## Step 10 — Classroom Reality

What was actually exercised, and what was not:

| Condition | Status |
|---|---|
| normal webcam video, 640×480 | exercised on a real camera |
| static / evenly lit scene | exercised — **cue degenerates, reports UNCERTAIN** |
| varying illumination | exercised — coupling ≈ +0.75 |
| multiple people | exercised in synthetic tests (two tracks, independent verdicts) |
| grayscale input | exercised |
| tiny faces, faces filling the frame, off-frame boxes | exercised — all UNCERTAIN |
| different head poses, movement | **not evaluated** |
| glasses, partial occlusion | **not evaluated** |
| low-quality webcam, compression | **not evaluated** |
| screen replay | **not evaluated** |
| printed image presentation | **not evaluated** (this is Step 9's question) |

**Explicitly NOT handled, and not claimed:**

- sophisticated replay attacks
- 3D masks
- adversarial attacks
- high-quality face swaps
- real-time neural deepfakes
- anything produced by somebody who has read this file and knows the cues,
  the thresholds and the window length

---

## Step 10 — Known Limitations

1. **The coupling cue degenerates in a static, evenly lit room** — the most
   important limitation. Face and context both reduce to sensor noise, the
   correlation collapses toward zero, and a generated region sits in the same
   place. Realistic behaviour is UNCERTAIN-dominant.
2. **A merely decoupled region is not flagged.** Only anti-correlated evidence
   reaches SUSPICIOUS.
3. **12 seconds to a first verdict** at 15 FPS. Shorter is measurably worse
   than useless.
4. **The texture and chroma thresholds are not anchored on real data.** They
   are models of generator behaviour, which is why neither may decide alone.
5. **The statistical coupling is weaker than a pixel residual would be** — a
   deliberate trade for the no-images-retained guarantee.
6. **A real face swap has never been tested.** Not once.
7. **Authenticity verdicts are not persisted.** Reported live and in the stop
   summary; no audit row, because that would need a schema change Step 10
   chose not to make.
8. **None of this is validated on real people**, in Step 10 any more than in
   Steps 8 and 9.

**Not claimed anywhere:** 100% deepfake detection, that deepfakes cannot bypass
ClassVision, AI-proof attendance, spoof-proof anything, or any accuracy figure
whatsoever.

---

## Step 11 — Student Registry, Academic History and Academic Sessions

Steps 1–10 built a pipeline that can watch one classroom and produce a register.
Step 11 makes that usable by an institution across more than one term.

The distinction the whole step rests on:

```
students                   who a person is        permanent
student_academic_history   where they were        one row per term
academic_sessions          the terms themselves   2026-27 semester 6, ...
```

**`STU001` is Safia Farhan, and stays `STU001`.** Her roll number, class,
section and semester are facts about a *period*, and they change. Before this,
the registry held only the current values, so promoting a student overwrote
where they used to be — and last year's attendance then appeared to belong to
this year's section. That is wrong in a way that is hard to notice and
impossible to undo.

A student is therefore registered **once**, for their whole time at the
institution. Nothing about them has to be retyped at the start of a term.

### The registry mirrors the current term, on purpose

`students` keeps the *current* `class_name`, `section` and `roll_number` as a
mirror of the `is_current` history row. That is not duplication for its own
sake: every existing query, the Students page, the session roster and the
recognition gallery read the student row, and making them all join a history
table to answer "what section is this student in" would be a rewrite with no
benefit. `promote()` moves both together, in one transaction, so they cannot
disagree.

### Student lifecycle

| Status | Means | Promoted by a bulk operation? |
|---|---|---|
| `ACTIVE` | at the institution | yes |
| `INACTIVE` | not currently attending | no |
| `TRANSFERRED` | moved away | no |
| `GRADUATED` | finished | no |

Deactivation is the default; deletion is the exception. Every one of these keeps
every attendance record, presence segment and expected-student row they ever
earned. A student who transferred out last term is **not** promoted into this
term's register, because doing so would generate an absence for somebody who is
not there.

---

## Step 11 — Promotion, and What It Refuses to Guess

Starting a new term is two deliberate steps, never one:

```
Academic session 2026-27 sem 6  (ACTIVE)
        |
        |  create 2026-27 sem 7          <- touches no student
        v
Preview promotion                        <- writes nothing
        |
        |  promote                        <- creates history
        v
New row per student: is_current = 1
Previous row:        is_current = 0, otherwise UNCHANGED
```

A promotion **creates** a row. It never updates one in place and never deletes
one, so the attendance attached to the old term still points at the sessions it
was always attached to.

### It does not invent a roll number

A system that decides "roll 21 becomes roll 21 in the new section" will be wrong
for most of a class, and the wrongness lands on a register. So:

* allocation is supplied by the administrator, per student, or imported;
* where it is **not** supplied the previous value is **carried forward
  unchanged**;
* and the response says which fields were carried forward, per student:

```json
{
  "student_id": "STU_TEST_001",
  "from": { "class_name": "MECH", "section": "A", "roll_number": "R-11" },
  "to":   { "class_name": "MECH", "section": "B", "roll_number": "R-11" },
  "carried_forward": ["class_name", "roll_number"],
  "already_enrolled": false
}
```

`POST /api/academic/promotion/preview` returns exactly that, for everybody
affected, and writes nothing. The dashboard shows it as a confirmation summary,
because a bulk change across forty students should be read before it is run.

### Bulk allocation by CSV

For a whole cohort, `POST /api/academic/promotion/allocation` reads the `.csv` a
school office actually produces:

```
student_id,class_name,section,roll_number
STU001,CSE(AI),C,001
STU002,CSE(AI),C,002
```

Parsed with the standard library's `csv` module rather than pandas or openpyxl:
a class list is three columns, and adding a spreadsheet dependency to read three
columns would be the wrong trade. Unknown columns are ignored. **A missing
column means "carry the previous value forward", never "blank it."** A row with
no student id, or a repeated student id, is reported by row number rather than
silently dropped:

```json
{ "students": 2,
  "problems": ["row 3 has no student id", "row 4 repeats STU_TEST_001"] }
```

### Promotion scope

| Request | Who moves |
|---|---|
| `student_ids: [...]` | exactly those, if `ACTIVE` |
| `class_name: "MECH"` | every `ACTIVE` student in that class |
| `class_name` + `section` | narrowed further |
| neither | every `ACTIVE` student |

Promoting into a term twice **corrects the row it already owns** rather than
adding a second one — there is a `UNIQUE` constraint saying a student is in a
section once, not twice.

---

## Step 11 — Face Enrollment Is Unchanged

The existing pipeline is reused exactly as it was:

```
Camera -> YuNet -> quality gate -> SampleCollector -> SFace -> embedding store
```

`student_id` is the identity key shared by the registry and the embedding store,
and it always was. Step 11 adds no second enrollment path, stores no photograph
in the registry, keeps embeddings out of the student metadata table, and exposes
no embedding through any API. A student's face samples survive every rename,
every section change and every promotion, because none of those touch the id
they hang from.

---

## Step 11 — Experimental Recognition Training

**SFace remains the production recogniser.** Nothing in this section changes
what the camera recognises.

```
python ai/recognition_training.py --report              # dataset only
python ai/recognition_training.py --train --model-id knn-v1
```

What it does *not* do is train a face-recognition network from scratch. Doing
that properly needs hundreds of thousands of identities, weeks of compute and a
dataset this project does not have; doing it improperly produces something that
looks like it works on the twelve people it saw and fails on the thirteenth.
What it does instead is learn a **classifier over the embeddings SFace already
produces**, which is the honest thing to do with a few hundred samples.

### Leakage, which is the whole difficulty

Five enrollment samples of one person, captured over about eight seconds, are
nearly the same photograph. Split them at random across train and test and the
test set measures whether the model can recognise a picture it has already seen
— which it can, at 100%, while telling you nothing.

So samples are grouped by **capture**, and a capture goes entirely into one
split. Where capture information is unavailable the whole **student** goes into
one split, which measures something harder and more useful: whether the model
generalises to a person it was not trained on. `split_dataset()` refuses to
split a student across sets, and a test asserts that it refuses.

Class imbalance is **reported and weighted**, never silently corrected by
duplicating or discarding samples.

### The model registry

| Status | Means |
|---|---|
| `BASELINE` | SFace. Always present; nothing can remove it. |
| `TRAINING` | a run is in progress; no metrics yet |
| `TESTING` | trained, with metrics on data the author chose. Most experiments should stay here. |
| `VALIDATED` | measured on a held-out set, cleared the gate. Eligible for production; not in it. |
| `ACTIVE` | in production. Exactly one model is, ever. |
| `RETIRED` | was active. Kept so a rollback has something to roll back to. |

Registering a trained model puts it in `TESTING` and changes nothing.

### The activation gate

A recogniser that is slightly worse than the one it replaced does not announce
itself. It marks a handful of students absent who were present, spread over
weeks, and by the time anybody notices, the register is wrong and nobody knows
which entries to trust. So the gate is a floor, and refusing is the default:

| Requirement | Floor |
|---|---|
| accuracy / precision / recall | ≥ 0.98 each |
| false acceptance rate | ≤ 0.005 |
| false rejection rate | ≤ 0.05 |
| test identities | ≥ 5 |
| test samples | ≥ 50 |
| `held_out: true` | required |

**A missing metric is a failure, not a pass.** A model that did not measure its
false-acceptance rate has not shown it is safe, and treating silence as success
is how a worse recogniser gets promoted.

Activation then needs the model id **typed out** — the same protection a
permanent deletion gets. Rollback is deliberately asymmetric: promoting an
experiment takes a validation and a typed confirmation, and undoing it takes one
call with no gate at all. When something is going wrong in a live classroom, the
safe direction should be the easy one.

```
POST /api/models/knn-v1/validate            {"metrics": {...}}
POST /api/models/knn-v1/activate?confirm=knn-v1
POST /api/models/rollback                   # always available
```

---

## Step 11 — Parent and Guardian Notifications

### 75% is a row, not a constant

The string `0.75` appears nowhere in the evaluation engine — a test asserts that
the engine compares against no hard-coded fraction at all. A rule is a row in
`notification_rules` with a type, a threshold and a window, and a school that
wants 60%, or wants alerts for one class only, edits a row.

| Rule type | Fires when |
|---|---|
| `attendance_below` | the attended share of completed sessions is under `threshold` |
| `consecutive_absence` | the run of absences *at the end* reaches `threshold` |
| `periodic_report` | there is anything to report inside `window_days` |

`POST /api/alerts/rules/defaults` installs three common rules **when an
administrator asks**. An installation that never asks has no rules and sends
nothing: there is no threshold anywhere in this project that fires on its own.

Everything a rule acts on comes from the existing attendance tables. It reads a
register; it never computes one. A student with **no completed sessions has no
attendance rate**, and alerting on one would be inventing it.

### Delivery is an interface, not an SMS account

Sending a real message needs a provider, an account and credentials this project
does not have and should not require in order to run its tests. So:

| Provider | Behaviour |
|---|---|
| `RecordingProvider` | the default. Records what it would have sent and delivers nothing, with that as the recorded reason. |
| `FailingProvider` | always fails, so the failure path is real code |
| `CollectingProvider` | succeeds and keeps what it was given |

Plugging in a real provider is implementing `send()`. Nothing else changes, and
nothing ever pretends a message arrived.

### What a notification row does not hold

A notification records **who** it was about, **which** rule fired, the measured
value, and whether delivery succeeded. It does **not** store the guardian's
phone number or email — those live on the student row and are looked up at send
time. A delivery log that accumulates contact details is a breach waiting to be
copied out. A test checks both the table and the API response for the fixture's
number and address.

### Cooldown

Without one, a nightly run would send a parent the same "attendance below
threshold" message every night until the child's average recovered, which is how
people learn to ignore alerts. The same rule stays quiet for the same student for
`DEFAULT_COOLDOWN_DAYS` (7), and the suppression is **reported** rather than
silent.

### Who may see a guardian's contact

| | `SUPER_ADMIN` | `TEACHER` |
|---|---|---|
| read `parent_name` / `parent_phone` / `parent_email` | yes | **redacted to `null`** |
| write them | yes | `403` |
| read and write `notification_enabled` | yes | yes |

A redacted field and an empty field both come back as `null`, deliberately: from
outside, "no number on file" and "you may not see it" look the same, so the
response cannot be used to probe which students have a number recorded. A
teacher can still register and edit students — that is their job — and they can
still switch alerts off for a family that has asked not to be messaged, because
that switch carries no contact detail. Supplying a contact field they may not
write is a `403` rather than a silent drop: quietly ignoring half a form is how
a school ends up believing it has phone numbers it does not.

---

## Step 12 — Classroom Session Lifecycle, Correction and Early End

### Editing a session depends on one question

```
SCHEDULED -> ACTIVE -> COMPLETED      the ordinary life of a lesson
SCHEDULED -> CANCELLED                it never happened
ACTIVE    -> CANCELLED                it was abandoned
```

A teacher creates Tuesday's session for 10:00 when it should have been 11:00.
What happens next depends entirely on whether the session has already produced
attendance:

| | Then |
|---|---|
| no attendance yet | it is just a plan. Edit it and move on. |
| attendance exists | the register is a record of something observed. It is not editable by changing the plan it hung from. |

Changing `session_date`, `start_time`, `end_time` or `timezone` changes the
**window attendance is measured over**, so doing it to a session that already
has evidence requires an explicit correction rather than an edit. Nothing
silently recalculates a historical register because a class, section or time was
edited.

**"Needs correcting" is not a status.** A session that ran and ended is
`COMPLETED`, and noticing afterwards that its time was wrong does not change
what happened — so `correction_state` is a separate column
(`NONE` / `REQUIRED` / `DONE`) and the status keeps telling the truth about the
lesson. It also means the status `CHECK` constraint never has to change, which
would have meant rebuilding the table holding every attendance record.

### Cancel is not delete

`session_expected_students`, `attendance_records` and `presence_segments` all
cascade from `attendance_sessions(session_id)`. Deleting a session that ran
deletes the lesson's whole register, for everybody, with no trace it existed.
Cancelling leaves the row, marks it `CANCELLED`, and records who did it, when,
and why. A cancelled session cannot be started and the live classroom will not
run it.

Cancelling a `COMPLETED` session is **refused**: it would make a register that
exists belong to a lesson that did not happen. Flag it for correction instead.

### Hard deletion exists, and is fenced

A session created by mistake five minutes ago is clutter rather than history, so
`DELETE /api/sessions/{id}` exists. It is refused whenever anything references
the session, and the refusal names what would be lost:

> Session SES-… has 32 attendance record(s) and 87 presence segment(s). Deleting
> it would delete them too. Cancel it instead — a cancelled session keeps its
> history and stops behaving like a lesson.

It is also refused while the session is running, and otherwise needs the session
id **typed out**. The audit row records the session id as text rather than a
foreign key, so deleting a session never rewrites the history of who did what.

### Removing a session from the dashboard

Every row on the Sessions page carries **Remove**, next to Open. It opens a
dialog showing the session id, date, class, section, teacher, scheduled times
and status, and three counts — attendance records, presence segments, expected
students — so what is at stake is on screen before anything is chosen.

**Which buttons appear is decided by the server, not the browser.**
`GET /api/sessions/{id}/details` returns `available_actions`, computed from the
session's status and correction state, and the dialog renders exactly that set:

| Status | Offered in the dialog |
|---|---|
| `SCHEDULED` | Cancel session · Delete permanently |
| `ACTIVE` | Cancel session (stop it first to delete) |
| `COMPLETED` | neither — the dialog says so and points at correction |
| `CANCELLED` | neither — its history is kept deliberately |

That indirection is the point. A browser deciding a session looked safe to
delete would be a second copy of the policy, free to drift from the one the API
enforces — and the drift would only ever be discovered by destroying a register.
A test asserts the dialog derives nothing from `status` itself.

**Delete permanently** first calls `DELETE /api/sessions/{id}` with no
confirmation. The server answers either *"it would destroy attendance"* (409) or
*"type its session id exactly"* (400), and both become the same next step: the
reason is shown and a field appears asking for the id. Only then is `?confirm=`
sent. A second click while a request is in flight is ignored.

**Cancel session** takes an optional reason, and records who cancelled it and
when. Cancelling twice keeps the first reason and the first timestamp.

Afterwards the sessions list, the class filter, the overview and the session
roster all reload, so counts and analytics cannot show a session that is gone.

### Removing a student from the dashboard

Unchanged by the above, and worth stating alongside it. Every row on the
Students page carries **Remove**, opening a dialog with the student's name, id
and class, their attendance-record / expected-session / face-sample counts, and
two choices:

- **Deactivate** — keeps every attendance record, presence segment and
  expected-student row, and takes them out of the active roster. The normal
  answer when somebody leaves.
- **Delete permanently** — `SUPER_ADMIN` only, and refused by default when
  anything references them, with the refusal naming what would be lost. Going
  through with it needs the student id typed out, and takes their face samples
  with them: a vector file for a student who no longer exists is an orphaned
  biometric record. A teacher is shown a line explaining the absent button
  rather than a gap, but the refusal that matters is the server's.

### Who may delete what

| Operation | Role |
|---|---|
| `DELETE /api/sessions/{id}` | `SUPER_ADMIN` only |
| `POST /api/sessions/{id}/cancel` | any signed-in user, within their class scope |
| `DELETE /api/students/{id}` | `SUPER_ADMIN` only |
| `DELETE /api/portal/accounts/{id}` | `SUPER_ADMIN` — removes a password, **not** a student |
| `DELETE /api/users/{id}` | `SUPER_ADMIN`, and never the last one or yourself |
| `DELETE /api/cameras/{id}`, `/api/alerts/rules/{id}` | `SUPER_ADMIN` |

Permanent **student** deletion was widened to a Super Admin after the CRUD
audit found the asymmetry: a session deletion cascades into three tables and
was already `SUPER_ADMIN` only, while a student deletion cascades into six —
attendance, presence segments, expected-student rows, academic history,
notifications and the face samples — and was open to any signed-in account.
**Deactivate is unchanged and remains a teacher's**; it is the operation they
actually need when somebody leaves, and it loses nothing.

A session or student outside a teacher's class scope answers **404**, not 403 —
`require_class_scope` hides existence rather than confirming it. Student
deletion is the exception and deliberately so: the role guard runs as a
dependency, *before* the row is read, so a teacher gets the same **403**
whether the id exists, sits in their own class, or was invented — nothing
about the answer distinguishes them. One known gap:
the two 404s carry different *sentences* (`"Session SES-X does not exist."` for a
genuinely absent one, `"Session not found."` for one in another class), so the
wording still distinguishes them even though the status code does not. It is
asserted in `tests/test_session_lifecycle.py` rather than left unnoticed.

### What deletion actually removes

Both deletions rely on the schema rather than on hand-written cleanup. With
`PRAGMA foreign_keys = ON` (set in `Database.connect()`), these cascade:

```
attendance_sessions(session_id) ←CASCADE← session_expected_students
                                ←CASCADE← attendance_records
                                ←CASCADE← presence_segments

students(student_id)  ←CASCADE← those same three
                      ←CASCADE← student_academic_history
                      ←CASCADE← notifications
```

Each service wraps its delete in a transaction, so a failure part-way leaves
nothing behind. **That cascade is exactly why both refuse by default** — it is
what makes deleting a session or a student silently take a register with it.

There is no "delete everything" control anywhere in this project, and no
endpoint that removes more than the one resource it names.

### Duplicate and overlap protection

`GET /api/sessions/{id}/conflicts` reports sessions that overlap on class,
section, teacher or time, so the dashboard can warn before creating one. Two
sessions for the same class on **different** dates or times are legitimate and
are not blocked.

### Early manual end — present stays present

The rule, stated plainly: **ending a session early must never turn a student who
was already marked present into absent.**

Finalisation derives the answer from the evidence, not from the clock:

```
Expected students
      |
      v
Presence and attendance records produced during the session
      |
      +-- was this student recorded present?  --> yes: PRESENT stands
      |
      +-- never recorded present               --> ABSENT, by the existing rules
```

Concretely: 40 expected, 32 recorded present, teacher ends at 10:35 instead of
11:00 → **present 32, absent 8.** Not present 0, absent 40.

* the observed window is what attendance is measured over, and the summary says
  the window was shortened;
* the session keeps its **scheduled** times, and records `actual_start` /
  `actual_end` separately;
* a manual override survives finalisation;
* finalising twice changes nothing — no duplicate records, no duplicate audit
  rows, no `PRESENT` → `ABSENT` regression;
* ending exactly on time and ending late use the identical code path;
* presence evidence is stored even for students who end up absent, so the
  decision can be re-examined.

---

## Step 12 — The Full Classroom View

The Live Classroom panel shows the **complete camera frame**, at the camera's own
resolution — not face crops, not bounding-box regions, not a zoomed-in face. A
teacher should be able to look at the screen and understand what is happening
across the whole room while the AI pipeline runs behind it.

```
+-----------------------------------------------+----------------+
| LIVE CLASSROOM                                | LIVE STATUS    |
|                                               |                |
|   [ FULL CLASSROOM CAMERA FRAME ]             | Present: 32    |
|                                               | Absent:  8     |
|        +-------+                              | Unknown: 2     |
|        | Safia |  <- overlay                   | Spoof:   0     |
|        +-------+                              |                |
+-----------------------------------------------+----------------+
```

* `GET /api/live/frame.jpg` — the current frame, no caching
* `GET /api/live/stream.mjpg` — the same, as a stream

Both need a live session and an account. With nothing running, the feed is a
`503` rather than a stale picture. The view is presented `contain`-style so the
aspect ratio is preserved and nothing is cropped or stretched, and it resizes for
desktop and tablet.

The overlay is drawn on the frame the pipeline already produced — it adds **no
second camera stream and no extra inference**, and it carries an identity and a
state, never a similarity score or a cue value. Releasing the camera stops the
picture.

**Showing the classroom is not recording it.** No frame reaches disk: a test
serves 25 frames and asserts nothing was written. No JSON response and no schema
carries a picture, and the page says so to the people in the room.

---

## Step 12 — Cameras and Device Health

`cameras` is operational only. A row says where a device is and when it last
delivered a frame; it says nothing about anybody the camera has ever seen — a
test checks the column list and the API response for identities.

```
PUT  /api/cameras/CAM-01            {"label": "Front", "classroom": "Lab 2"}
POST /api/cameras/CAM-01/heartbeat  {"fps": 12.5, "latency_ms": 40}
GET  /api/cameras
```

**Only values the hardware actually reported are recorded.** A camera that
reports no frame rate keeps its last measured one rather than being credited
with a number nobody measured, and a camera nobody has heard from reads
`unknown` rather than online. A heartbeat for an unregistered camera is a `404` —
it does not conjure a camera into existence. An offline heartbeat records the
error; a successful one clears it.

`GET /api/cameras` reports the live runtime alongside the rows, from the running
pipeline rather than from what was last written down, so the health view reflects
what is actually happening. Multiple physical cameras are not required for the
current demo; the architecture is extensible to them.

---

## Step 12 — Audit Log and Authorization

Every administrative action writes an audit row: `student.create`,
`student.update`, `academic.session_create`, `academic.session_activate`,
`academic.promote`, `model.registered`, `model.validated`, `model.activated`,
`model.rollback`, `notification.rule_saved`, `notification.raised`,
`session.cancelled`, `session.deleted`, and the correction events. Each holds
who, what, when, the subject and a short detail — **no password, no embedding,
no contact detail, nothing biometric.** A failure to write an audit row never
fails the operation that succeeded.

| | `SUPER_ADMIN` | `TEACHER` |
|---|---|---|
| students, enrollment, sessions, attendance, analytics | yes | within assigned classes |
| accounts, class assignments | yes | no |
| academic terms, promotion, allocation import | yes | no |
| alert rules, evaluation, retry | yes | no |
| model validate / activate / rollback | yes | no |
| camera register / delete | yes | no |
| camera heartbeat, alert history | yes | yes |
| guardian contact | read + write | redacted, `403` on write |

All of it is enforced on the server, before any data is read. Hiding a tab or a
fieldset from a teacher is a courtesy, not the permission — calling an endpoint
by hand gets them exactly as far as clicking would have.

---

## Step 11/12 — Database Migration (schema version 3)

Six tables added, all additive, plus columns on two existing tables:

| Table | Holds |
|---|---|
| `academic_sessions` | a year and a semester within it |
| `student_academic_history` | one row per student per term |
| `notification_rules` | configurable alert rules |
| `notifications` | the delivery log |
| `model_registry` | recognition models and which is in production |
| `cameras` | devices and their last heartbeat |

New columns on `students`: `parent_name`, `parent_phone`, `parent_email`,
`notification_enabled`.

New columns on `attendance_sessions`: `academic_session_id`, `section`,
`teacher`, `actual_start`, `actual_end`, `cancelled_at`, `cancelled_by`,
`cancellation_reason`, `correction_state`, `correction_reason`, `correction_at`,
`correction_by`.

`ALTER TABLE … ADD COLUMN` is the one schema change SQLite performs without
rewriting the table: existing rows keep their bytes and gain the default.
**No statement in `initialize()` or `migrate()` drops a table, deletes a row, or
resets anything**, and that is not an accident — a database holding a term's
attendance must never be recoverable-from rather than simply correct.

`migrate()` skips a table that is not present rather than failing on it, so it is
safe to run against a genuinely old file as well as a current one. A
version-1 database gains all seven student columns at once and loses nothing; a
current database comes through untouched, and running it twice is a no-op.

### The order matters, and it is the one thing that broke

`initialize()` runs the table script, **then** `migrate()`, **then** the
indexes — in that order, and the indexes are a separate script for exactly that
reason. Three of them index a column version 3 added (`academic_session_id` on
`attendance_sessions`, `class_name` on `students`). On a new database the
`CREATE TABLE` includes those columns, so an index on them is fine. On an
existing version-2 database the table already exists, `CREATE TABLE IF NOT
EXISTS` does nothing, and the column does not arrive until `migrate()` runs — so
creating the index in the same script failed with

```
ERROR: OperationalError: no such column: academic_session_id
```

and took the whole upgrade with it. **Every real upgrade hit that and no
fresh-database test could**, which is why
`test_a_version_two_database_upgrades_to_the_current_schema` builds a genuine
version-2 file with rows in it and upgrades that.

### Verified on the real database

The production `data/classvision.db` was at version 2 and has been upgraded. A
row-by-row comparison against a pre-migration snapshot found **one** changed
value — `schema_info.schema_version`, `2` → `3`. Everything else is byte for
byte what it was:

| | Before | After |
|---|---|---|
| tables | 10 | 16 |
| `students` | 1 (`STU001`, Safia Farhan, CSE(AI) C, ACTIVE) | unchanged, + 4 contact columns defaulting to `''` / on |
| `attendance_sessions` | 3 rows, 10 columns | 3 rows unchanged, 22 columns |
| `attendance_records` | 3 | 3, statuses unchanged |
| `presence_segments` | 5 | 5 |
| `session_expected_students` | 3 | 3 |
| `users` / `user_sessions` / `audit_log` | 1 / 1 / 5 | 1 / 1 / 5 |

`STU001`'s face samples were not touched: `data/embeddings/STU001.npz` has the
same size and modification time as before the upgrade, and no migration path
opens the embedding store.

Filling the new registry columns in for students enrolled before the change is a
separate step, `python database/migrate_students.py` — it needs the embedding
store, and the database layer must not know the embedding store exists.

---

## Step 11/12 — API

Existing endpoints are unchanged. Added:

### Academic

| Method | Path | Role |
|---|---|---|
| `GET` | `/api/academic/sessions` | any signed-in user |
| `POST` | `/api/academic/sessions` | `SUPER_ADMIN` |
| `POST` | `/api/academic/sessions/{id}/activate` | `SUPER_ADMIN` |
| `GET` | `/api/academic/sessions/{id}/students` | any, class-scoped |
| `POST` | `/api/academic/promotion/preview` | `SUPER_ADMIN` |
| `POST` | `/api/academic/promotion` | `SUPER_ADMIN` |
| `POST` | `/api/academic/promotion/allocation` | `SUPER_ADMIN` |
| `GET` | `/api/academic/students/{id}/history` | any, class-scoped |
| `POST` | `/api/academic/students/{id}/enrol` | any, class-scoped |

### Alerts and notifications

| Method | Path | Role |
|---|---|---|
| `GET` | `/api/alerts/rules` | any signed-in user |
| `PUT` | `/api/alerts/rules/{id}` | `SUPER_ADMIN` |
| `POST` | `/api/alerts/rules/defaults` | `SUPER_ADMIN` |
| `DELETE` | `/api/alerts/rules/{id}` | `SUPER_ADMIN` |
| `POST` | `/api/alerts/evaluate` | `SUPER_ADMIN` |
| `GET` | `/api/alerts/notifications` | any, class-scoped |
| `POST` | `/api/alerts/notifications/{id}/retry` | `SUPER_ADMIN` |

### Models and cameras

| Method | Path | Role |
|---|---|---|
| `GET` | `/api/models` | any signed-in user |
| `POST` | `/api/models/{id}/validate` | `SUPER_ADMIN` |
| `POST` | `/api/models/{id}/activate?confirm={id}` | `SUPER_ADMIN` |
| `POST` | `/api/models/rollback` | `SUPER_ADMIN` |
| `GET` | `/api/cameras` | any signed-in user |
| `PUT` | `/api/cameras/{id}` | `SUPER_ADMIN` |
| `POST` | `/api/cameras/{id}/heartbeat` | any signed-in user |
| `DELETE` | `/api/cameras/{id}` | `SUPER_ADMIN` |

### Session lifecycle

| Method | Path |
|---|---|
| `GET` | `/api/sessions/{id}/details` |
| `PATCH` | `/api/sessions/{id}` |
| `POST` | `/api/sessions/{id}/cancel` |
| `POST` | `/api/sessions/{id}/correction` |
| `POST` | `/api/sessions/{id}/correction/resolve` |
| `DELETE` | `/api/sessions/{id}` |
| `GET` | `/api/sessions/{id}/conflicts` |

`GET /api/students` gained `parent_name`, `parent_phone`, `parent_email` and
`notification_enabled`; the first three are `null` for a teacher. `POST` and
`PATCH` accept them, `SUPER_ADMIN` only.

**No endpoint added here returns an embedding, a face crop, an image, a model
weight or a filesystem path.** A test checks each of them against a list of
forbidden patterns.

---

## Step 11/12 — Service Layer and CLI

Business logic lives in services, not in route handlers, and the CLI and the
dashboard go through the same ones:

```
Dashboard ---+
             +--> StudentService / AcademicService / SessionService  --> SQLite
CLI        --+    NotificationService / ModelRegistry
```

| Command | Does |
|---|---|
| `python database/init_db.py` | create or verify the schema |
| `python database/init_db.py --show` | report what is there |
| `python database/migrate_students.py` | fill registry columns from the embedding store |
| `python database/create_admin.py` | create the first Super Admin, or reset a password |
| `python ai/enroll_student.py` | face enrollment, writing both the registry row and the samples |
| `python ai/recognition_training.py --report` | dataset summary, no training |
| `python ai/recognition_training.py --train --model-id knn-v1` | train, evaluate, register as `TESTING` |

The CLI and the dashboard write identically shaped registry rows, and a test
asserts they do.

---

## Step 11/12 — Testing

```
python tests/run_all.py
```

The runner reports a per-suite count and a project total, and
`tests/test_classroom_view.py` — which existed but was never registered — is now
in it. Two suites were added for this work:

| Suite | Tests | Covers |
|---|---|---|
| `tests/test_academic_sessions.py` | 26 | terms, academic history, promotion, CSV allocation, historical attendance |
| `tests/test_operations.py` | 39 | alert rules, notifications, guardian-contact protection, model registry, camera health |

**32 suites, 708 tests, all passing.**

`tests/schema_baseline.py` holds the one authoritative list of tables the
project is allowed to have. Several suites assert that *their own* step added no
table, and they each used to carry a private copy of that list — so every
legitimate schema addition broke all of them at once, which is exactly the
situation where somebody eventually edits the assertion instead of thinking about
it. The list is written out by hand and is deliberately **not** imported from
`database/database.py`: if it were, the schema would be defining its own
expectation and the guard would prove nothing.

No webcam, model, network, SMS account or dataset is needed for any of it. Every
suite uses its own temporary SQLite file and, where relevant, its own temporary
embedding store; `data/classvision.db` and `data/embeddings` are never opened,
and a test asserts that too.

---

## Step 11/12 — Known Limitations

1. **No message is ever actually delivered.** The default provider records and
   sends nothing, which is the honest behaviour of an installation with no
   messaging account. Nothing here has been tested against a real SMS or email
   gateway.
2. **The custom recognition pipeline has never beaten SFace**, and is not
   expected to on this amount of data. It exists to measure, and its model is
   experimental until validated and explicitly activated.
3. **The activation gate's floors are judgement, not measurement.** They are
   deliberately strict; they are not derived from this project's data.
4. **Class scope is matched on normalised free text**, not a class table. A
   session carries a free-text `class_id` and the registry carries `class_name`
   plus `section`; both are reduced to letters and digits and compared. It is a
   small, reversible accommodation of the data that already exists, applied in
   one place.
5. **Attendance is still not validated on a real classroom population**, in Step
   11 or 12 any more than in Steps 8, 9 and 10.
6. **Multi-camera operation is architectural, not demonstrated.** One physical
   camera has been used.
7. **The dashboard pages for all six of these now exist.** Terms, promotion
   and allocation import are the Academic tab (`test_academic_ui.py`); the
   model registry is a panel on Administration; alert rules and camera health
   were built earlier. What this item used to say - that the browser UI was not
   built - is no longer true, and the suites that cover each page are named in
   the testing section.

**Not claimed anywhere:** that an alert reached a parent, that a custom model is
better than SFace, that multi-camera classrooms have been tested, or any
attendance accuracy figure.

---

---

## Step 13 — Student Portal, Staff Records and the Redesigned Interface

Four things arrived together: a student can sign in and see their own
attendance, a teacher's account carries a photograph and personnel documents,
absence warnings can actually be emailed to a parent, and the dashboard has a
visual design.

### The student portal

A student signs in with a username and password the Super Admin issues, and
sees one page: their attendance percentage, the counts behind it, and every
session they were expected at.

**It is a second front door, not a third role.** Student credentials live in
their own `student_accounts` table, not in `users`, and that separation is the
security property rather than a filing preference:

- Every staff permission in `backend/services/authz.py` begins by resolving a
  row in `users`. A portal credential lives in a table those guards never
  read, so there is no sequence of edits to role handling that turns a
  fifteen-year-old's login into a teacher's.
- `users.role` carries a SQL `CHECK` constraint. SQLite cannot widen one
  without rebuilding the table, and no migration in this project drops a table
  a term of attendance points at. The same reasoning already governs
  `correction_state`.

The two doors use different cookies (`classvision_session` and
`classvision_portal`), and a test calls every staff endpoint by hand with a
valid portal session and every portal endpoint with a valid staff cookie.
Both get **401**.

**No portal endpoint takes a student id.** There is no
`/api/portal/students/{id}`, because an endpoint that accepts an id is an
endpoint somebody tries their friend's id in. The student comes from the
session token, and a test asserts that no path under `/api/portal/me`
contains a parameter.

**The issued password opens one door.** An account is created with
`must_change` set, and until it has been replaced the portal answers **403**
to everything except "who am I" — which the change-password form needs in
order to greet somebody by name. Changing the password ends every session for
that student, this one included.

| Operation | Role |
|---|---|
| `POST /api/portal/login`, `/api/portal/me/*` | the student themselves |
| `POST /api/portal/accounts` (issue) | `SUPER_ADMIN` |
| `POST /api/portal/accounts/{id}/status` (suspend / restore) | `SUPER_ADMIN` |
| `POST /api/portal/accounts/{id}/password` (reset) | `SUPER_ADMIN` |
| `DELETE /api/portal/accounts/{id}` (remove the login) | `SUPER_ADMIN` |

**Suspend, not remove, is the first answer.** A suspended account keeps its
row, its username and its history and can be restored. Removing it deletes a
password — and nothing else. Worth saying plainly because *delete* means two
very different things here: `DELETE /api/students/{id}` destroys a person's
attendance history, and `DELETE /api/portal/accounts/{id}` destroys a
credential. A test asserts the student, their registry row and their
attendance are all untouched by the second.

### Staff photographs and documents

A teacher's account can carry a photograph and any number of documents.

**This is not enrollment and never becomes it.**
`backend/services/staff_files_service.py` imports nothing from `ai/` and
nothing from the embedding store, so a change that made a staff photograph
biometric would have to add an import — visible in review in a way a quiet
behaviour change is not. No embedding is computed, the recognition pipeline
never opens these files, and a teacher with a photograph on file is not
recognisable to any camera.

The browser's own camera *is* used here, via `getUserMedia`, and that is the
opposite of student enrollment — where the camera is opened server-side and a
frame never reaches the browser. The difference is deliberate:

| | Student enrollment | Staff photograph |
|---|---|---|
| Camera | server-side, `ai/enrollment_session.py` | the browser's, `getUserMedia` |
| What is stored | an embedding vector under `data/embeddings/` | a JPEG under `data/staff/` |
| Read by the AI pipeline | yes, it is the identity | never |
| Reaches the browser | never | it is taken there |

`tests/test_student_enrollment.py` still bans `getUserMedia`, `<canvas>`,
`FileReader` and `toDataURL` outright — scoped to the student-registry section
of `app.js`, which is the code that ban was written about.

**Uploads are checked against their bytes, not their claim.** A file sent as
`image/jpeg` that does not begin with JPEG's signature is refused, so a script
cannot be stored and served back as a photograph. SVG is refused at any size:
it is a document format that executes script, and no ID photo has to be one.
Documents are always served `Content-Disposition: attachment`, so a PDF never
runs in the dashboard's origin.

Bytes live on disk and never in SQLite, so a copy of `classvision.db` taken
for debugging carries nobody's photograph or identity document. Stored
filenames are random, so a URL cannot be guessed from a person's name, and
`read_file()` validates the generated shape rather than merely cleaning the
string — a tampered `stored_name` cannot read an arbitrary file.

| Operation | Role |
|---|---|
| `GET /api/users/{id}/photo` | any signed-in staff member |
| `PUT`/`DELETE /api/users/{id}/photo` | `SUPER_ADMIN` |
| `GET`/`POST`/`DELETE /api/users/{id}/documents…` | `SUPER_ADMIN` |

A colleague may see who works at the school. A contract is not a face, so the
documents stay administrative.

### Absence warnings that actually send

Two things were missing: a rule counting absent **days**, and a delivery layer.

**`consecutive_absent_days` is a new rule type, separate from
`consecutive_absence`.** The existing rule counts absent *sessions* in a row.
Where a class meets twice a day those are different numbers, and a letter home
saying "ten days" when a child missed ten periods over five days overstates
the absence by a factor of the timetable. A day counts as absent only when
*every* completed session that day was an absence, so attending one period
breaks the run. Days with no session — a weekend, a holiday — neither count
nor break it.

`absent-10-days` is now one of the four suggested rules, alongside the
existing 75% and three-session rules. Thresholds are rows, so a school that
wants 15 days changes a number.

**Delivery is SMTP, configured by environment and never by database.** An SMTP
password in a table is an SMTP password in every backup and every screenshot
of the admin page.

```
CLASSVISION_SMTP_HOST       smtp.gmail.com
CLASSVISION_SMTP_PORT       587
CLASSVISION_SMTP_USER       the account that authenticates
CLASSVISION_SMTP_PASSWORD   an app password, not the login password
CLASSVISION_SMTP_FROM       what a parent sees in From:
CLASSVISION_SMTP_SECURITY   starttls (default) | ssl | none
```

**Nothing is required.** With no host configured the existing
`RecordingProvider` is used, alerts are raised and visible, and nothing is
sent. `GET /api/alerts/delivery` reports the host, port and sender — and
whether a password is set, never any part of it — and the Administration page
says out loud when alerts are being recorded and not delivered. A school that
has not finished setting up email should not be quietly believing parents have
been told.

### Editing a session that was scheduled wrongly

`PATCH /api/sessions/{id}` already existed; it now has a form. The dialog
exists to express one rule:

- **Metadata** — a mistyped section, the wrong teacher — is corrected freely,
  even on a session that ran. None of it changes what *present* meant.
- **The window** — the date and times — decides what was measured. Changing it
  on a session that already has attendance is refused until a correction is
  confirmed, and even then **no record is recomputed**. The register stands
  and the session is flagged `CORRECTION_REQUIRED`. Re-deriving a past
  register from an edited plan would be inventing a different lesson.

Which of the two applies is decided by the **server**. The browser sends the
change without confirmation first and shows the checkbox only on a 409, so
there is no second copy of the policy in the page to drift from the real one.

### The visual design

Dark glassmorphism with real depth: translucent surfaces over a slowly
drifting gradient mesh, cards that lift towards the viewer on a shared
perspective, and a rotating wireframe globe behind the sign-in card.

**It is still hand-written and still offline.** No three.js, no WebGL, no CDN,
no build step and no dependency. The globe is 260 points on a Fibonacci
sphere, two rotation matrices written out longhand and one perspective divide,
drawn on a 2D canvas — about forty lines. A test asserts the page references
no external host and that `THREE` and `WebGLRenderer` appear nowhere.

**All of the motion can be switched off.** This replaced a blanket
"no animation anywhere" assertion that the tests used to carry. That was the
right default for a tool with no visual design; once the project asked for
one, the honest guard is not *nothing moves* but *nothing moves for somebody
who asked it not to*. `prefers-reduced-motion: reduce` stops every animation —
including the drifting background, switched off by name rather than merely
sped up, because a shortened animation is still an animation. The canvas
consults the same preference before starting its loop and draws one still
frame instead, and the loop stops itself when the sign-in screen goes away
rather than burning a core behind a signed-in dashboard.

Three narrower bans survive where they matter: the live classroom view, the
enrollment progress display and the scheduling dialog must not animate,
because each is read by somebody making a decision about a real camera.

Light mode is a genuine second palette rather than an inversion — glass over
white needs more opacity and less blur to hold the same contrast.

### Schema version 4

Additive, like every migration before it. Three new tables and three new
columns; **no existing constraint changed**, which is the whole reason the
portal has its own credential table.

```
student_accounts   one portal login per student, keyed on student_id
student_sessions   live portal sign-ins, token hashes only
staff_documents    what personnel files exist, by name and size

users.photo_name   the filename of a staff portrait, never the bytes
users.phone        staff contact
users.email        staff contact
```

`users.photo_name` is the one column named against the
`no biometric columns` guard in `tests/test_attendance_end_to_end.py`, listed
there by its exact name with the reason. That test was **strengthened** at the
same time: no column in any table may be declared `BLOB`, which is a stricter
check than the name list and is what actually stops an image reaching SQLite.

### Testing

```
python tests/test_student_portal.py     18 tests
python tests/run_all.py                 1313 tests across 59 suites
```

The portal suite redirects `staff_files_service.STAFF_DIR` at a temporary
directory as well as the database, and a test asserts that neither
`data/classvision.db` nor `data/staff/` was touched by the run.

---

---

## Roadmap Steps 11, 12 and 13 — Reports, the Assistant, and Hardening

> **Numbering note.** The headings above use the README's own running count, in
> which "Step 11" is the student registry and "Step 13" is the portal. The three
> steps in THIS section are `docs/ROADMAP.md`'s Steps 11, 12 and 13 — reports
> and exports, the assistant over analytics, and hardening — which are the last
> three entries in that file's table. They are grouped here rather than renamed,
> because renaming the sections above would break every reference to them.

### Reports and exports (roadmap Step 11)

Four readings of the same stored decisions, on one page: the register itself,
one row per class, one row per academic term, and one session as a printable
grid. Plus CSV and PDF for each.

**Not one rate is computed twice.** `class_summary` asks
`AnalyticsService.class_analytics`; `term_summary` asks `day_summary`; both hand
back what they were told. `test_report_exports.py` asserts the per-class rate is
EQUAL to the number analytics returns for that class — not close to it. A
summary that drifted by a rounding step fails, because a teacher who can read
two different figures for one class with no way to tell which is real has been
given a reason to trust neither.

**The PDF writer is hand-written**, about 400 lines, no reportlab and no new
dependency. The tests parse the bytes back — header, xref table, every offset,
trailer — and decompress the page text. A character Helvetica cannot draw is
**counted** and reported in a response header rather than silently replaced. An
export nobody can open is worse than no export, and four of these files were
also opened in a real reader by hand.

**The seat map is not a seating plan.** No seat, desk or coordinate is stored
anywhere in this schema, and none is inferred from the camera: the tracker works
in image space and the image is not calibrated to furniture. Inventing an x and
y from a bounding box would produce a picture a teacher would reasonably read as
"where the student sat". What it is instead: the session's students in
roll-number order, in a stable grid, so one printed sheet can go round a room.
The page says that in those words, and the ordering is sorted numerically where
a roll number is a number — a register ordered 1, 10, 11, 2 is the kind of small
wrongness that makes somebody stop trusting a printout.

**A student expected and never evaluated has no status**, in the grid and in the
PDF, never "absent". Nobody decided they were absent.

**Exports are scoped in the bytes.** An export is the easiest thing in a system
to walk out of a building with, so a teacher's PDF is checked for another
class's names in its DECOMPRESSED content rather than by trusting a row count.

### The Academic page and the model registry

The browser UI for the two areas the limitations list used to say were missing.

**Academic terms and promotion.** Creating a term touches nobody and promotes
nobody; a new term sits `PLANNED` until somebody activates it, and activating
one CLOSES the previous rather than deleting it.

Promotion cannot be run without being previewed, and **what runs is the payload
the preview was built from**, sent verbatim — not a second one reassembled from
the form afterwards, which could differ from what was read. The preview names,
per student and in words, every field being **carried forward**, because where
the administrator supplied nothing the previous value is kept rather than
invented.

The page forms no opinion about who is promotable. `class_name` and `section` on
the promotion API are WHO, not WHERE, so a typed destination is expanded into a
per-student allocation over exactly the students the server's own preview
returned — and a per-student allocation from the CSV always wins over the typed
one, because a class list the office produced is more specific than a blanket
instruction. The CSV is parsed by the server, never in the browser: a second
parser could disagree about which rows were usable, and the office would be told
two different things. Rows the parser skipped are **named**.

**The recognition model registry**, as a panel on Administration. The asymmetry
is the point and it is carried into the UI rather than evened out: activating a
model needs a validation against the measured floor AND the model id typed out,
because it is the only action on this dashboard that changes what the camera
recognises. Rolling back to SFace takes one click and no gate — when something
is going wrong in a live classroom, the safe direction should be the easy one.
A metric nobody measured reads **"not measured"**, never a dash that could be
mistaken for zero and never a pass.

### The assistant (roadmap Step 12)

A question box that answers by CALLING the analytics functions.

**Why it may not do its own arithmetic.** A teacher reading "Asha attended 71%
of sessions" must be able to open the Students page, click Asha, and see 71%. An
assistant that computed its own figure would be a second, unverifiable source of
truth about a child's attendance, and when the two disagreed nothing would tell
anybody which was wrong.

So `test_assistant.py` walks the service's **AST** and locates every arithmetic
operation in it. The only one permitted is a single `* 100` inside `_percent`,
which formats a ratio analytics has already computed. Every answer's figures are
then compared to the analytics result exactly — not rounded, not within a
tolerance.

**There is no model here and no network call.** It is a question router: an
intent table of regular expressions over a small, closed vocabulary. It reads in
one sitting, gives the same answer twice, and works with no internet connection
— which is the machine this project is built for. A test asserts the module
imports no HTTP client and names no URL.

The cost is stated rather than hidden: **a question phrased outside the patterns
is not answered.** No paraphrasing, no fuzzy intent guessing. It says it did not
understand and publishes the list of what it can be asked, because the failure
mode of guessing is a confident answer to a question nobody asked. Every
published example is itself asserted to be understood — a capability nobody can
trigger is a lie.

**Every answer carries a receipt**: the analytics function, the arguments it was
given, and the dashboard page showing the same number. It is rendered next to
the answer, not inside a disclosure, because it is the reason to believe it.

**What it refuses.** "No data" is given as "no data", never 0%. A student the
caller may not see produces an answer byte-identical to the one for a student
who does not exist, so it cannot be used to find out who is in somebody else's
class. Two students called Asha produce a question, not a guess. It does not
predict, advise, or explain WHY attendance is what it is.

One regression is guarded by name because it was the easiest to ship and hardest
to notice: *"What is the weather like?"* matched the rate intent on "what is"
and came back as "I have no student by that name" — a confident non-answer to a
question that was never about a student.

### Hardening (roadmap Step 13)

**Schema version 5 adds one table, `sync_queue`, and changes nothing else.**
There is no `ALTER` for it: a new table arrives through `CREATE TABLE IF NOT
EXISTS`, which is why upgrading to it cannot fail on a database already holding
a term of attendance.

**The queue solves two problems in two ways.** *Not lost:* queueing is a write
to the same SQLite file the attendance is in, so there is no in-memory buffer to
lose on a power cut — and a classroom appliance loses power. *Not twice:* every
item carries an idempotency key derived from WHAT IT IS, and the column is
UNIQUE, so re-running the exporter after a crash updates the same rows instead
of delivering a term of attendance a second time.

**A payload is a copy, not a pointer.** It is taken when the item is queued, so
what eventually leaves is what was true then; a correction queues its own item
under the same key rather than silently rewriting history in flight. A test
corrects a record after queueing and asserts the queued payload did not move.

**Nothing is marked sent that was not acknowledged.** A target returns the keys
it ACCEPTED rather than a boolean, because a partial delivery is the normal case
over a bad link and all-or-nothing handling either loses records or duplicates
them. A target that raises leaves every item queued with its attempt counted and
the reason in words. After too many refusals an item becomes `ABANDONED` —
**still in the table, still readable, never deleted** — and retrying it is a
deliberate act, because retrying on a timer would hide whatever was wrong.
`purge_delivered()` is asserted to touch nothing that is not `DELIVERED`.

**Sessions are presented before the attendance that references them**, because a
receiver given a decision for a session it has never heard of must either reject
it or invent the session.

**Nothing has ever synchronised to a real server.** The default target is
configured with nowhere to send anything and delivers nothing — the honest
behaviour of an installation that has not been given an upstream, and the state
every installation is in. The other two targets are test doubles, in the same
shape as the notification providers. So **a full queue means nothing has been
lost, only that nothing has been sent**, and preflight reports it that way
rather than as a fault.

**No payload can carry a biometric.** Embeddings, crops, frames, tokens,
passwords and file paths are refused by key name at every depth, including
inside lists, and the refusal NAMES the key — silently stripping one would send
a record that looks complete and is not.

**Packaging is two tools and a set of instructions**, in `deploy/`. See
`deploy/README.md` for the Scheduled Task arrangement and the restore procedure.

`deploy/preflight.py` answers whether this machine can run a lesson. Every
failure it catches has a diagnosis that is obvious afterwards and invisible
beforehand — a model never downloaded, a half-downloaded model that exists and
fails at load time, a read-only `data/`, a schema a version behind — and they
all arrive at the start of a lesson looking like "the camera is broken". **A
skipped check is never a pass:** each reports OK, PROBLEM or SKIPPED *with the
reason*, the exit code is non-zero only for PROBLEM, and a successful run says
how many checks proved nothing. It creates no file and leaves its write probe
nowhere.

`deploy/backup.py` exists because copying `classvision.db` in a file explorer
produces a file that opens cleanly and is missing the most recent lessons — the
database is in WAL mode, and on a young one the plain `.db` does not even have
the tables. That is the worst possible failure, because it looks like a backup;
`test_deployment.py` reproduces it deliberately. This uses SQLite's own backup
API, then **opens the copy** and compares its row counts to the live ones,
because a backup nobody has opened is a hypothesis. Personnel documents are
opt-in, a backup never overwrites one, and restoring is deliberately manual
because it discards whatever happened after the backup was taken.

### Testing

```
python tests/test_report_exports.py     23 tests   summaries, seat map, PDF
python tests/test_report_ui.py          31 tests   the four report panes
python tests/test_academic_ui.py        30 tests   terms, promotion, registry
python tests/test_assistant.py          36 tests   the assistant
python tests/test_offline_sync.py       31 tests   the outbound queue
python tests/test_deployment.py         22 tests   preflight and backup
python tests/test_restore.py            10 tests   the restore, rehearsed
python tests/run_all.py               1313 tests across 59 suites
```

Every suite redirects the database at a temporary file, and each asserts that
`data/classvision.db` was not touched by the run.

### Known limitations of these three steps

1. **Nothing has ever been synchronised to a real server**, and no upstream
   implementation exists. The queue, the idempotency keys, the ordering and the
   partial-acknowledgement handling are all tested against test doubles.
2. **No PDF has been checked for typography.** Structure and content are
   asserted; four files were opened by hand in a real reader.
3. **The assistant understands a closed list of phrasings.** A teacher who
   words a question differently gets "I did not understand" rather than an
   answer. That is the deliberate trade for having no model and no network call,
   and it is a real limitation rather than a quirk.
4. **No suite in `run_all.py` drives a browser**, and that is deliberate:
   the automated run has to work on a machine with no browser, no webcam and
   no network. Every page assertion in `test_report_ui.py`,
   `test_academic_ui.py` and the assistant's page tests still reads the
   dashboard's own source files, and in those suites the DOM is never built.
   What has changed is that the DOM is no longer *never* built:
   `tests/browser_check.py` is an opt-in tool that drives the real
   application in real Chrome and measures the rendered page. See **The
   interface, rendered** below for what it measured and the two defects it
   found. It needs Chrome, so it is run by hand.
5. **A restore has now been performed end to end**, by
   `tests/test_restore.py`, which carries out the six documented steps against
   a real temporary installation and opens the result. There is still no
   `--restore` flag: putting a backup back throws away whatever happened
   after it was taken, which is a human decision, and the function that
   performs the steps lives in the test rather than in `deploy/`. What is
   still untrue is that anybody has rehearsed a restore on a school's machine
   with a backup carried in from another disk.
6. **The liveness baseline is still not evaluated against a real PAD dataset**
   with an identity-disjoint split, and every Step 9 failure mode is still
   unaddressed. Packaging did not change that.
7. **Attendance accuracy is still not validated on a real classroom
   population.** A machine that passes preflight is a machine whose files and
   database are in order — not one whose figures have been shown to be right.

**Not claimed anywhere:** that a record reached an upstream server, that a
message reached a parent, that multi-camera classrooms have been tested, that a
restore has been rehearsed **on a school's own machine**, or any attendance
accuracy figure.

## The interface, rendered — and a restore performed

Two limitations above were true for the same reason: the automated suite is
built to need no browser and no decisions, so nobody had ever watched the
dashboard build a DOM, and nobody had ever put a backup back. Neither was
going to stop being true on its own.

### `tests/browser_check.py`

**Opt-in, and not part of `run_all.py`.** It needs Google Chrome installed,
and the automated run has to keep working on a machine that has none — the
same reason `--camera` is opt-in in preflight.

```
python tests/browser_check.py
python tests/browser_check.py --into C:/somewhere/shots
```

It starts the real application on a thread against a **temporary** database it
seeds — `data/classvision.db` is never opened — launches headless Chrome with
a throwaway profile, and drives the page over the DevTools protocol: typing
into the sign-in form, pressing Enter, clicking controls at their measured
coordinates, reading back computed styles and geometry, and writing a
screenshot of each view. The WebSocket client is about a hundred lines written
out by hand in the same file, because the project has no browser-automation
dependency and this was not worth adding one for.

The ten-classroom grid is drawn by pushing **ten synthetic runtimes** into the
live registry directly, the same device `test_multi_session_console.py` uses.
That is what makes the check possible on a laptop with one webcam, and it is
also its limit: **no camera is opened**, every panel's video area is empty by
construction, and nothing here is evidence that ten simultaneous devices work.
What is being checked is the interface, not the hardware.

**Measured in a 1600×1000 window, seventeen checks, all passing:**

| | |
|---|---|
| Sign-in page | 1,740 DOM nodes, the form painted |
| After signing in | 11 navigation items, 11 views mounted |
| Entry screen | 1582×904 at z-index 60, crossed by Enter |
| Status row | 6 tiles; Schema reads `READY`, the API's own word |
| Sideways scroll | none at 1600×1000, 1024×768 or 820×1180 |
| Theme | light `rgb(238,242,248)`, dark `rgb(5,7,13)`, auto follows the system |
| Contrast, dashboard figures | lowest of six measured **13.45:1** |
| Contrast, console panels | lowest of six measured **5.37:1** at 11px |
| Reduced motion | no element reports a running animation |
| Ten classrooms | **10 panels, 10 distinct classes, 3 columns** |
| Per-panel roster | at most 6 names, "10 verified", "+4 more in the focused view" |
| Bulk finish | one control, "Finish all sessions", at ten panels |
| Requests | 43, every one to 127.0.0.1, none 4xx or 5xx |
| Script errors | none, in any view visited |

The contrast figures are the ones the static `test_glass_contrast.py` could
not produce: they are computed from the colours **Chrome resolved** for text
and for whatever is actually painted behind it, walking up to the first opaque
ancestor, rather than from the tokens the stylesheet asked for.

The column line is worth reading twice. `gridColumns(10)` returns 4 and the
laid-out grid has 3 — which is correct, and is what that function's own
comment says it is for: the count is a **maximum**, and `auto-fit` picks the
real number from the width. A source-reading test can confirm the 4. Only a
laid-out page can confirm the 4 is a ceiling rather than a promise.

### What only a rendered page showed

**The status row printed a version it did not have.** `/api/health` answers
`ready`, `missing` or `unknown` — a state. The Schema tile rendered
`"v" + health.schema`, so it read **vready** on every dashboard this project
has ever shown. No suite could catch it: the string is assembled at runtime
from a value that appears nowhere in the source. The tile now reports the
API's word, and a schema that is not ready is a `warn` rather than an `idle`
fact, because it is the one condition on that row that stops attendance being
written.

**Every page load asked for a file that was not there.** Browsers request
`/favicon.ico` whether or not anybody wrote one; this appliance had not, so it
came back 404 and Chrome logged an error on a page where nothing was wrong.
There is now `backend/static/favicon.svg` — drawn in this project, served from
this server, with no data URI and no second host, because
`test_live_api.py` forbids an image embedded in the page and that rule is
right. The static-directory manifest in `test_dashboard_frontend.py` knows
about the seventh file and holds it to the same offline checks as the other
six.

**And one finding that was the check's fault, kept because of how convincing
it was.** `spatial.js` puts a deliberate entry scene in front of the console
after sign-in, crossable by Enter, Escape, its own button, or a click on the
scene. Rendered, that is a full-viewport overlay at z-index 60 with
`pointer-events: auto` over every control in the rail. The check's first click
— on the "Light" theme button — was swallowed by that door and crossed it, and
the reading that followed said the theme switch was broken. It was not.
`click()` now hit-tests with `elementFromPoint` and **refuses** to click a
control that something else is covering, naming what is in the way; and the
door became a check of its own, since "the console is reachable once the door
is crossed" can only be asserted in a browser.

### `tests/test_restore.py`

Ten tests, in `run_all.py`, no browser and no network needed. The six steps of
`deploy/README.md` are performed against a real temporary tree — a real
WAL-mode database, a real backup taken through `deploy/backup.py`, real
embedding files — and the restored installation is opened and queried.

**There is still no `--restore` flag,** and a test asserts that nothing in
`deploy/` grew one. A restore discards whatever happened after the backup was
taken; that is a decision for a person, so the function performing the steps
lives in the test, which is the only place a restore should happen without
somebody agreeing to it.

**The headline is step 2, and leaving it out fails silently.** Copying the
backup over `classvision.db` while the old `-wal` is still beside it does not
fail, does not warn, and does not restore: both files descend from the same
database, so the stale log's salt still matches, SQLite replays it over the
pages just written, and the installation comes back holding the very rows the
restore existed to roll back. Measured both ways on one fixture: **8 records
back when only the `.db` is replaced, 4 when all three files are moved aside.**
That is why the deploy notes say to move the `-wal` and `-shm` too.

Also verified: that a restore discards the lessons recorded after the backup,
by name; that "move, do not delete" leaves the superseded database openable
and complete, so the restore itself can be undone; that the embedding store
and the database come back to the **same moment**, since a vector belonging to
a student the restored tables have never heard of would be a face recognised
for nobody; that restoring a file-explorer copy produces an installation that
is short, opens cleanly and passes preflight — which is exactly what makes
that kind of backup dangerous; and that preflight brings a restored schema-1
database forward to version 7.

The six steps are read out of `deploy/README.md` and asserted **in order**, so
the procedure a school will type and the procedure that has been shown to work
cannot drift apart.

### What these two still do not make true

No camera was opened, and no browser has seen a real classroom frame. No
restore has been rehearsed on a school's machine with a backup carried in from
another disk — a temporary tree on a developer's laptop is evidence that the
steps are right and in the right order, not that anybody has lived through
one. And neither touches the figures: **attendance accuracy is still not
validated on a real classroom population.**

---

## Technology Currently Used

| Tool | Version | Why |
|------|---------|-----|
| Python | 3.10.11 | Best wheel coverage for computer-vision / ML packages |
| opencv-python | 5.0.0.93 | Camera access and image processing |
| numpy | 2.2.6 | Array maths (OpenCV depends on it) |
| pandas | 2.3.3 | Tabular data, used for records in later steps |

**Models** (downloaded, not committed; both verified by SHA-256):

| Model | Purpose | Size | Step |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | find where faces are | 227 KB | 2 |
| `face_recognition_sface_2021dec.onnx` | face -> 128-number embedding | 36.9 MB | 3 |

Steps 1-3 added **no Python dependency beyond the original three**. Both
models run through OpenCV's built-in `cv2.FaceDetectorYN` and
`cv2.FaceRecognizerSF`.

> Python 3.11 / 3.12 were not installed on this machine. 3.10 was chosen over the
> also-installed 3.14 because many CV/ML libraries do not yet ship 3.14 wheels.

---

## Folder Structure

```
ClassVision-AI/
|
+-- ai/
|   +-- camera_test.py          # Step 1: camera diagnostics
|   +-- paths.py                # shared paths + console safety
|   +-- download_models.py      # fetch + verify both models
|   +-- face_detector.py        # Step 2: detection (swappable interface)
|   +-- face_detection_test.py  # Step 2: live demo + benchmark
|   +-- face_alignment.py       # Step 3: landmarks -> 112x112 aligned face
|   +-- face_embedding.py       # Step 3: aligned face -> 128-number vector
|   +-- face_quality.py         # Step 3: enrollment quality gate
|   +-- embedding_store.py      # Step 3: local store (PostgreSQL in Step 6)
|   +-- enroll_student.py       # Step 3: enrollment CLI
|   +-- face_recognizer.py      # Step 4: matching engine
|   +-- recognition_stability.py# Step 4: anti-flicker (Step 5 replaces)
|   +-- recognition_test.py     # Step 4: live demo + benchmark
|   +-- evaluate_threshold.py   # Step 4: threshold from data
|   +-- person_tracker.py       # Step 5: multi-person tracking
|   +-- track_identity.py       # Step 5: identity per track
|   +-- presence_timeline.py    # Step 5: presence states + events
|   +-- tracking_test.py        # Step 5: live demo + benchmark
|   +-- attendance_rules.py     # Step 6: policy + rule engine
|   +-- attendance_session.py   # Step 6: session model
|   +-- live_camera.py          # Step 8: open / read / release the camera
|   +-- live_session.py         # Step 8: runtime state machine + worker
|   +-- live_pipeline.py        # Step 8: coordinates the Step 5 pipeline
|   +-- live_benchmark.py       # Step 8: synthetic live-session benchmark
|   +-- liveness_detector.py    # Step 9: PAD cues (no model, numpy only)
|   +-- liveness_stability.py   # Step 9: per-track temporal confirmation
|   +-- attendance_evidence.py  # Step 9: eligibility in front of Step 6
|   +-- liveness_test.py        # Step 9: calibration + benchmark + demo
|   +-- authenticity_detector.py   # Step 10: authenticity cues (no model)
|   +-- authenticity_stability.py  # Step 10: per-track confirmation
|   +-- authenticity_test.py       # Step 10: calibration + benchmark + demo
|   +-- enrollment_rules.py     # capture rules shared by CLI + dashboard
|   +-- enrollment_session.py   # headless enrollment capture + persistence
|   +-- model_registry.py       # which recogniser is in production
|   +-- recognition_training.py # Step 11: experimental training, isolated
+-- database/                   # Step 6: persistence layer
|   +-- database.py             # schema (v5) + connection + migration
|   +-- repositories.py         # all SQL lives here
|   +-- init_db.py              # safe, repeatable initialization
|   +-- migrate_students.py     # fill registry columns from the store
|   +-- create_admin.py         # first Super Admin / password reset
+-- data/                       # gitignored, never committed
|   +-- embeddings/             # BIOMETRIC DATA (Step 3)
|   |   +-- students.json
|   |   +-- <student_id>.npz
|   +-- classvision.db          # attendance only - no biometrics
|   +-- classvision-demo.db     # fictional demo data (optional)
+-- backend/                    # the dashboard backend
|   +-- app.py                  # FastAPI application
|   +-- routes/                 # HTTP surface (health, sessions, students,
|   |                           #   analytics, live, auth, users, academic,
|   |                           #   operations, portal, reports, assistant,
|   |                           #   sync)
|   +-- services/               # data, analytics, live, auth, authz,
|   |                           #   enrollment, academic, session,
|   |                           #   notification, portal, staff files,
|   |                           #   report, pdf writer, assistant, sync
|   +-- schemas/                # explicit response models
|   +-- static/                 # the dashboard (HTML/CSS/JS, hand-written)
|   +-- seed_demo.py            # fictional demo data, separate database
+-- deploy/                     # roadmap Step 13: running it for real
|   +-- preflight.py            # is this machine ready to run a lesson
|   +-- backup.py               # consistent WAL-safe backup, then open it
|   +-- README.md               # service setup, restore, what not to claim
+-- frontend/                   # (empty) future web UI
+-- database/                   # (empty) future DB files / migrations
+-- datasets/                   # gitignored
+-- models/                     # downloaded models (gitignored)
|   +-- face_detection_yunet_2023mar.onnx
|   +-- face_recognition_sface_2021dec.onnx
+-- docs/
|   +-- VISION.md               # what makes this project different
|   +-- ROADMAP.md              # the 13-step build order
+-- tests/
|   +-- run_all.py              # runs every suite
|   +-- test_camera_test.py     # Step 1  (4 tests)
|   +-- test_face_detector.py   # Step 2  (17 tests)
|   +-- test_face_alignment.py  # Step 3  (12 tests)
|   +-- test_face_embedding.py  # Step 3  (15 tests)
|   +-- test_embedding_store.py # Step 3  (18 tests)
|   +-- test_face_recognizer.py # Step 4  (18 tests)
|   +-- test_recognition_stability.py  # Step 4 (16 tests)
|   +-- test_person_tracker.py  # Step 5  (21 tests)
|   +-- test_track_identity.py  # Step 5  (21 tests)
|   +-- test_presence_timeline.py      # Step 5 (20 tests)
|   +-- test_attendance_rules.py       # Step 6 (36 tests)
|   +-- test_attendance_database.py    # Step 6 (29 tests)
|   +-- test_attendance_end_to_end.py  # Step 6 (10 tests)
|   +-- test_dashboard_analytics.py    # Step 7 (18 tests)
|   +-- test_dashboard_api.py          # Step 7 (25 tests)
|   +-- test_dashboard_frontend.py     # Step 7 (18 tests)
|   +-- test_live_session.py           # Step 8 (19 tests)
|   +-- test_live_api.py               # Step 8 (28 tests)
|   +-- test_liveness_detector.py      # Step 9 (23 tests)
|   +-- test_liveness_stability.py     # Step 9 (25 tests)
|   +-- test_liveness_integration.py   # Step 9 (24 tests)
|   +-- test_session_creation.py       # scheduling (23 tests)
|   +-- test_authenticity_detector.py    # Step 10 (25 tests)
|   +-- test_authenticity_stability.py   # Step 10 (25 tests)
|   +-- test_authenticity_integration.py # Step 10 (27 tests)
|   +-- test_student_enrollment.py     # dashboard enrollment (30 tests)
|   +-- test_student_registry.py       # registry (34 tests)
|   +-- test_auth_rbac.py              # accounts + RBAC (33 tests)
|   +-- test_session_finalization.py   # early end (14 tests)
|   +-- test_classroom_view.py         # full classroom view (15 tests)
|   +-- test_academic_sessions.py      # academic service (26 tests)
|   +-- test_operations.py             # alerts + model registry (39 tests)
|   +-- test_attendance_reports.py     # the register + CSV (17 tests)
|   +-- test_report_exports.py         # summaries, seat map, PDF (23 tests)
|   +-- test_report_ui.py              # the four report panes (31 tests)
|   +-- test_academic_ui.py            # terms, promotion, models (30 tests)
|   +-- test_assistant.py              # the assistant (36 tests)
|   +-- test_offline_sync.py           # the outbound queue (31 tests)
|   +-- test_deployment.py             # preflight and backup (22 tests)
|   +-- schema_baseline.py             # the allowed table list
|   +-- (and the portal, camera, lifecycle and theme suites - see run_all.py
|        for the authoritative list of all 49)
+-- .venv/                      # virtual environment (gitignored)
+-- .gitignore
+-- requirements.txt
+-- README.md
```

---

## Installation

Open a terminal **inside the `ClassVision-AI` folder**.

### 1. Create the virtual environment

```powershell
py -3.10 -m venv .venv
```

### 2. Activate the virtual environment

**Windows — PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
```

**Windows — Command Prompt (cmd):**

```cmd
.venv\Scripts\activate.bat
```

**Git Bash / macOS / Linux:**

```bash
source .venv/Scripts/activate      # Windows Git Bash
source .venv/bin/activate          # macOS / Linux
```

When it works, your prompt starts with `(.venv)`.

> If PowerShell blocks the activate script, run this once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 3. Install the dependencies

```powershell
pip install -r requirements.txt
```

---

## How to Run the Camera Test

With the virtual environment activated:

```powershell
python ai/camera_test.py
```

**Controls**

| Key | Action |
|-----|--------|
| `Q` or `ESC` | quit cleanly |
| `S` | save a snapshot to `datasets/snapshots/` |
| `H` | hide / show the info panel |

**Options**

```powershell
python ai/camera_test.py --list                          # show available cameras
python ai/camera_test.py --camera 1                      # use a different camera
python ai/camera_test.py --width 1280 --height 720       # request a resolution
python ai/camera_test.py --help
```

### Run the smoke tests (no webcam needed)

```powershell
python tests/test_camera_test.py
```

---

## Expected Result

- A window opens titled **"ClassVision AI - Camera Test"**.
- You see your live webcam feed with an info panel showing resolution, live FPS,
  frame count, dropped frames, and elapsed time.
- The terminal prints `Camera 0 opened successfully at 1280x720.`
- Pressing **Q** (with the video window focused) closes it.
- The terminal prints a summary and `Camera released and windows destroyed.`

### Measured baseline on this machine

Step 1 was verified on the development webcam. These are the real numbers, and
they set the compute budget for Step 2:

| Resolution | Measured FPS | Frame time budget |
|------------|--------------|-------------------|
| 1280×720 | ~8 FPS | ~125 ms/frame |
| 640×480 | ~15.5 FPS | ~65 ms/frame |

Dropped frames: 0 in both cases.

**Takeaway:** this camera is the bottleneck, not the CPU. Face detection in
Step 2 should target 640×480 capture, where there is roughly double the frame
budget to work with.

### If the camera does not open

The script prints `ERROR: Could not open camera 0.` Common causes:

1. Another app (Zoom, Teams, the Windows Camera app) is using the webcam — close it.
2. Camera access is off in **Windows Settings → Privacy & security → Camera**.
3. No webcam connected, or the driver is missing.
4. The camera is on a different index — run `python ai/camera_test.py --list`.

---

## Note for this machine — non-ASCII path issues

This project sits under a path containing non-English characters (`文档`).
That broke **three** different things, each with a different fix. All three are
handled in the code, but keep them in mind when writing new code:

| What breaks | Symptom | Fix used |
|---|---|---|
| `cv2.imwrite()` | returns without writing; **no error** | `cv2.imencode()` + `Path.write_bytes()` — see `save_image()` in `camera_test.py` |
| `print(path)` | `UnicodeEncodeError`, script crashes | `enable_safe_console()` in `ai/paths.py`; prefer `relative(path)` |
| Loading a model by path | `Can't read ONNX file` on a valid file | read the bytes in Python, pass the buffer to OpenCV — see `YuNetFaceDetector.__init__` |

The rule: **never hand a file path to OpenCV.** Read or write bytes in Python
and pass the buffer. This works on every platform, so it is not a workaround
so much as the more portable habit.
