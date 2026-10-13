# ClassVision AI — Build Roadmap

One step at a time. A step is only "done" when it runs and is verified on real
hardware. See `VISION.md` for why the project is shaped this way.

| Step | Name | Status |
|------|------|--------|
| 1 | Foundation + camera pipeline | **Done** |
| 2 | Face detection (find faces, no identity) | **Done** |
| 3 | Enrollment + embeddings | **Done** |
| 4 | Recognition (identity verification) | **Done** |
| 5 | Tracking + presence timeline | **Done** |
| 6 | Attendance rules + database | **Done** |
| 7 | Teacher dashboard + analytics | **Done** |
| 8 | Live classroom session + real-time dashboard | **Done** |
| 9 | Liveness / presentation attack detection | **Done** (research prototype) |
| 10 | Video authenticity analysis | **Done** (research baseline, off by default) |
| 11 | Reports + exports | **Done** |
| 12 | AI assistant over trusted analytics | **Done** (offline router, no model) |
| 13 | Hardening: offline sync, deployment, packaging | **Done** (never synced to a real server) |

> **Steps 8 and 9 were inserted, not substituted.** Reports and exports were
> originally Step 8 and are now Step 10; liveness was originally one bullet
> inside the final hardening step and is now Step 9 in its own right.
>
> The reason is ordering rather than preference. Steps 1-7 could produce
> attendance and display attendance, but nothing connected the camera to the
> database in one run - so exporting a report built from data no live session
> had ever produced would have polished the wrong end of the pipeline. And once
> a live session DID run, the most obvious thing wrong with it was that a
> photograph would have worked perfectly.
>
> Liveness is also too large to be one bullet. It needs its own state machine,
> its own stability layer, its own place in the attendance chain, and - above
> all - its own honest account of what it has not been shown to do.

---

## Step 1 — Foundation + camera pipeline ✅

Project structure, virtual environment, first dependencies, and a camera
diagnostics tool that reports resolution, live FPS and dropped frames.

**Why it matters:** every later step is bounded by what the camera can actually
deliver. Knowing the real frame rate now decides the detection budget later.

## Step 2 — Face detection ✅

Find *where* faces are in a frame. No identity. Output: bounding boxes with a
confidence score, drawn live, with detection cost measured per frame.

**Step 2 detects faces but does not identify people or mark attendance.**

**Detector chosen: YuNet** (`cv2.FaceDetectorYN`, model
`face_detection_yunet_2023mar.onnx`, 227 KB).

The decision was forced and then confirmed:

- `cv2.CascadeClassifier` **does not exist in OpenCV 5.0.0** — Haar cascades
  were removed, so the classic approach was unavailable, not merely inferior.
- YuNet ships inside the OpenCV already installed: **no new dependency**.
- It returns a confidence score per face and handles small, slightly angled
  faces — the classroom case.
- It pairs with `cv2.FaceRecognizerSF` (also built in) for Step 3, so
  recognition will not need a new dependency either.

**Measured on this machine:** 640×480 at 14.9 FPS, detection 10.2 ms average
(p95 15.4 ms), 0 dropped frames. 1280×720 costs 3× more per frame for no
detection benefit, so 640×480 stays the default.

**Three non-ASCII path bugs were found and fixed** (the project lives under a
folder named `文档`): `cv2.imwrite` fails silently, printing a path crashes
the console, and OpenCV cannot load a model by such a path. See the README.

## Step 3 — Enrollment + embeddings ✅

Register a student once and convert their face into a numeric embedding vector.
Store embeddings, never photos — this is where Pillar 2 (privacy) became real
code rather than an intention.

**Step 3 creates and stores biometric embeddings but does not perform
classroom attendance.** There is no recognition, no tracking, no sessions.

**Model: SFace** (`cv2.FaceRecognizerSF`,
`face_recognition_sface_2021dec.onnx`, 36.9 MB) — the recogniser designed to
pair with YuNet, built into the OpenCV already installed, so Step 3 added
**no new Python dependency**.

- **Embedding dimension: 128**, measured by running the model, not quoted.
- Faces are aligned to a 112×112 ArcFace template using YuNet's five
  landmarks before embedding. The alignment was validated as **pixel-identical
  to OpenCV's own `alignCrop`** (cosine 1.000000), confirming the landmark
  order and template are right.
- Every embedding is L2-normalised, so similarity is cosine in [-1, 1].
- Model name, version and **SHA-256 are stored with every enrollment**, so
  embeddings from different models can never be compared silently.

**Measured separation (diagnostic, not a production threshold):** same face
under rotation/brightness/blur/scale variation scored 0.9334–0.9857; different
people scored 0.0280–0.1269.

**Privacy:** no photograph is ever written. `data/` is gitignored, vectors are
never printed, and deleting a student deletes their vectors — all enforced by
tests, not by discipline.

## Step 4 — Recognition (identity verification) ✅

Match live faces against enrolled embeddings and report a named MATCH or
UNKNOWN.

**Step 4 performs identity verification only. It does not mark attendance or
calculate presence duration.**

- **Best-of-five**: a student's score is the highest of their five enrolled
  embeddings, so the closest pose wins. Averaging was rejected — a mean vector
  matches every pose slightly worse.
- **UNKNOWN is a real answer.** A face below the threshold returns
  `identity: null`, never the nearest guess.
- **Model provenance is enforced**: embeddings enrolled with a different model
  version are refused rather than silently compared.
- Matching is one matrix multiply against all stored vectors: **0.03 ms at 30
  students, 0.14 ms at 500**.

**Threshold: 0.45, provisional and unvalidated.** Step 3's synthetic 0.93 was
deliberately *not* used — those pairs were altered copies of one photo, and a
threshold that high would reject real matches. 0.45 sits above the SFace
authors' published 0.363, favouring fewer false accepts.
`ai/evaluate_threshold.py` measures a real threshold from labelled data; run on
the only public faces available it reported **NOT ENOUGH DATA**, which is the
honest current state.

**Measured:** align+embed 4.16 ms/face; 640×480 at 15 FPS with 0 dropped
frames. Projected ~7 FPS at 30 faces — CPU only, no GPU claimed.

**Tracking was NOT built.** `recognition_stability.py` is only an anti-flicker
filter (3 consecutive frames to confirm) that associates faces by nearest box
centre. Real multi-object tracking is Step 5's job.

## Step 5 — Tracking + presence timeline

**Done.** **Step 5 does not calculate attendance.**

Real multi-object tracking now exists, and frame-by-frame recognition has
become a temporal record.

**Tracker: written in-project, no new dependency.** Verified in this
environment first: ByteTrack/BoT-SORT/DeepSORT need torch or scipy+lap, and
OpenCV 5.0 removed `cv2.legacy`/CSRT/KCF, leaving only single-object trackers.
Since YuNet already detects every face every frame in ~13 ms, association was
the only missing piece — so `person_tracker.py` does tracking-by-detection in
~200 lines, keeping ByteTrack's two-pass idea (weak detections may *continue* a
track but never *create* one).

**Track and identity are separate concepts.** A returning person gets a new
track id but the same student id, which is precisely why attendance cannot be
read off track ids.

**Identity-switch protection:** setting an identity needs 2 agreeing
recognitions; *changing* one needs 4 **and** a +0.08 score margin. Refusals are
recorded as `IDENTITY_CHANGED_REJECTED` rather than happening silently.

**Temporary loss is not an exit.** A 15-frame grace period (~1 s at the
measured 15 FPS) separates "blinked" from "left". Reacquisition reuses the same
track and creates no second person.

**Measured — the performance payoff:** on a 300-frame sequence with one real
face, recognition calls fell from **300 to 11 (96.3% avoided)** and wall time
from 3.16 s to 1.92 s (**1.64×**). Tracking itself costs **0.039 ms/frame**
against 13.2 ms of detection. Live webcam: 640×480, **15.0 FPS, 0 dropped**,
p95 latency 16.0 ms.

**Not validated on real people** — nobody was available in front of the camera,
so crossing, occlusion and multi-person scenarios are covered by synthetic
tracker tests only.

## Step 6 — Attendance rules + database

**Done.** **Step 6 introduces deterministic attendance rules and SQLite
persistence. Attendance accuracy has NOT been validated on a real classroom
population.**

Presence finally becomes attendance - through a policy, not a shortcut.

**Three layers, kept apart:** the presence timeline is the evidence,
`ai/attendance_rules.py` is the decision, `database/` is only storage. The rule
engine has no database and no camera, which is precisely what makes a policy
change replayable.

**Configurable policy**, with development defaults that are placeholders, not
any institution's rules: minimum ratio 0.75, late after 10 minutes, early-leave
in the last 5 minutes, 30-second loss grace. Invalid policies are rejected.

**States:** PRESENT, LATE, PARTIAL, LEFT_EARLY, ABSENT, UNKNOWN,
MANUAL_OVERRIDE, with a documented precedence for when several fit.

**Two rules that outrank convenience:**
- An unknown face never becomes attendance - a segment only opens at
  `IDENTITY_CONFIRMED`, and `count_unverified_presence=True` is *refused* by
  the policy validator.
- ABSENT requires a COMPLETED session. A live or interrupted session yields
  UNKNOWN. A camera that stopped early is not evidence of anyone's absence.

**Duration is computed from timestamps, never frame counts.** Overlapping
segments merge, so two tracks covering 09:00-09:20 and 09:10-09:40 are 40
minutes, not 50. One student with several track ids is one attendance record.

**Database: SQLite via the standard library - no new dependency.** Six tables,
foreign keys enforced per connection, CHECK constraints on every status and
number, parameterised statements throughout, explicit transactions.
`UNIQUE (session_id, student_id, policy_version)` provides idempotency and
policy history at once: re-running updates, a new policy adds. Presence
segments are persisted so attendance can be recomputed without re-running a
camera.

**Manual override** keeps a full audit trail and is never overwritten by
re-evaluation.

**Measured:** 120 events, 30 students - rule engine **0.42 ms**, database write
11.29 ms. Attendance calculation is not a bottleneck.

**Not validated on real people.** All results come from synthetic timelines
with invented students, and inherit every Step 4 recognition limitation.

## Step 7 — Teacher dashboard + analytics

**Done.** **Step 7 is a local teacher/admin dashboard and deterministic
analytics layer. Local password authentication IS implemented (PBKDF2 hashing, session cookies, SUPER_ADMIN / TEACHER roles enforced server-side); it has not been independently reviewed and is not evaluated production security. Attendance
accuracy has NOT been validated on a real classroom population.**

**FastAPI backend, 16 operations** (14 read-only, 2 for manual corrections),
serving a **dependency-free** dashboard: plain HTML, CSS and JavaScript,
54.8 KB total, hand-written SVG charts, zero external requests. A charting
library would have been the heaviest dependency in the project, for two charts.

**Layering held:** routes contain no SQL, the analytics service contains no
HTTP, and the attendance engine is untouched. Each layer can be reused by a
later reporting or assistant step.

**Analytics are deterministic and defined.** Only COMPLETED sessions count;
"satisfied the rule" is exactly `{PRESENT, LATE, LEFT_EARLY}`; the denominator
is *expectation*, not appearance. Historical records are never reinterpreted
under a newer policy.

**NO DATA is not REAL ZERO.** A rate with nothing to divide by returns `null`
and renders as "No data"; a student who genuinely attended nothing returns a
real `0.0`. Both are asserted by tests.

**"Needs review" is not anomaly detection** — students appear only when stated
arithmetic thresholds are exceeded, and the rule ships in the response.

**Privacy enforced three ways:** responses are built field by field, schemas
declare every permitted field, and tests walk all 14 read endpoints plus the
OpenAPI field names against a forbidden list. Both leak tests were verified to
catch a planted leak. GET requests provably modify nothing.

**Measured** (30 students, 20 sessions, 600 records): most endpoints 2.6-7.5 ms;
`analytics/attention` is 71 ms because it issues N+1 queries — reported as a
known limitation, not hidden. Raw queries under 0.1 ms.

**Accessibility and responsiveness were verified statically, not in a browser** —
no screenshots, no screen-reader pass, no measured contrast.

## Step 8 — Live classroom session + real-time dashboard

**Done.** **Step 8 runs the existing pipeline against a live camera. It adds no
model, changes no threshold, and decides no attendance. Attendance accuracy has
NOT been validated on a real classroom population.**

The camera, the pipeline, the database and the dashboard became one system.

**Nothing from Steps 1-7 was rewritten.** The frame loop is Step 5's
`TrackingSession`, reused; identity confirmation is still Step 5's
`IdentityManager`; presence segments are still built by Step 6's
`segments_from_events`; attendance is still decided by Step 6's rule engine.
Step 8 is orchestration, a state machine, four endpoints and one dashboard tab.

**Two lifecycles, deliberately not merged.** The lesson (`SCHEDULED -> ACTIVE ->
COMPLETED`) lives in SQLite; the camera loop (`IDLE -> STARTING -> RUNNING ->
STOPPING -> COMPLETED`, with `FAILED` reachable from every busy state) lives in
memory. "The camera crashed" and "the lesson ended" are different claims, and a
failed camera must never quietly complete a lesson. The stored session is moved
to `ACTIVE` only *after* the camera has actually opened, so a failed start
leaves the database untouched.

**The worker never touches SQLite.** One thread owns the camera and the
pipeline; every write happens on the request thread at `/stop`, in one
transaction. That removes the race instead of managing it. The cost is stated:
a process killed mid-session loses its evidence.

**The camera is released in a `finally` block, always** — after a clean stop,
after a crash, after a pipeline exception, and on application shutdown. Four
tests assert it, one per failure mode.

**Polling, not WebSockets.** One small JSON object per second did not justify a
second server protocol and a reconnect policy. Four endpoints: `/api/live/start`,
`/status`, `/presence`, `/stop`. `/stop` is idempotent.

**Live is not attendance, and the vocabulary enforces it.** Students appear as
`PRESENT`, `RECOGNIZED`, `TEMPORARILY_LOST` or `SEEN_EARLIER`. There is
deliberately **no live `ABSENT`** — absence is a conclusion about a finished
session. An unverified person is a *count*, never a name and never a guess.

**Measured (synthetic unless stated).** Live orchestration overhead **0.15 ms**
per frame against a 65 ms budget; recognition calls **96.7% avoided**, unchanged
from Step 5; `/api/live/status` **7.2 ms** avg while the camera ran;
`presence_view()` 0.20 ms after 5,000 frames. **On a real webcam through the
HTTP API: 640×480, 14.85 FPS, 0 dropped, 382 captures, camera released and
immediately re-openable.**

**Still not validated on real people.** That real-camera run had nobody in front
of it and nobody enrolled, so it exercised the plumbing, not recognition. No two
people have been in front of this camera at once. The threshold is still the
provisional 0.45. There is no liveness check, so a photograph is not detected.

**One honest rough edge:** stopping the camera early does not shorten the
session window, so an early stop makes everyone look `PARTIAL` or `LEFT_EARLY`.
That is the Step 6 rule behaving correctly, and it was left alone rather than
tuned to flatter a demo.

## Step 9 — Liveness / presentation attack detection

**Done, as a RESEARCH PROTOTYPE.** **Step 9 is NOT production-grade
presentation attack detection and is not a security control. Its behaviour on
real presentation attacks has NOT been established: accuracy, FAR, FRR, APCER,
BPCER and ACER are all "Not evaluated".**

Pillar 3 - anti-proxy verification - became code rather than intent. Only
partly, and the gap is stated rather than glossed.

**Nothing from Steps 1-8 was rewritten.** Detection, recognition, tracking,
recognition stability, the presence timeline and the attendance rule engine are
untouched. A test greps `ai/attendance_rules.py` for the words "liveness" and
"spoof" and fails if either appears - if the rule engine ever learns about
liveness, the layering has collapsed. The database schema still has exactly its
original six tables.

**Identity and liveness are two facts, never one.** A photograph of a student
IS that student's face, and recognition says so correctly; what must not follow
is that they were present. Neither layer can set, change or clear the other's
state, and a test asserts both directions.

**No model, no weights, no new dependency.** OpenCV 5.0 ships no PAD model; no
downloadable model met this project's source/licence/SHA-256 bar without a
deep-learning runtime; and no PAD dataset with an identity-disjoint split was
available. Using classmates' faces without authorisation and consent was not an
option. So Step 9 is a documented deterministic baseline in plain numpy -
`classvision-heuristic-pad 0.9.0` - rather than a heuristic dressed up as a
model.

**Three temporal cues, never one frame.** Non-rigid landmark dynamics
(normalised for translation AND scale, so waving a photo does not look alive);
texture detail normalised by intensity variance (so it cannot degenerate into a
brightness rule - a test asserts it does not move when 60 is added to every
pixel); and texture stability over time. The geometry cue is **mandatory in
both directions**, because it is the one with a physical argument behind it.
Blinking is deliberately not used.

**UNCERTAIN is a real answer.** Too-small face, missing landmarks, unreadable
frame, crashed liveness layer - every one of those is UNCERTAIN, never LIVE.
Three agreeing verdicts confirm a state; **five** are needed to overturn one, so
a confirmed SPOOF cannot be cleared by briefly showing a real face. Oscillation
is reported as instability, never as a state.

**Blocked is not absent - and making that true forced a design change.** Step 6
marks an expected student with no evidence ABSENT, which is right when "no
evidence" means "never appeared". A student whose evidence was WITHHELD was
seen; the system just declined to trust it. Letting them fall through to ABSENT
would convert "we could not verify this" into "they were not here". So under
enforcement such a student is withheld from evaluation and gets NO record -
"the system cannot say" - and the stop summary names them. Held-back evidence is
still stored, because a teacher cannot review a decision whose evidence was
deleted.

**Enforcement is OFF by default.** The layer analyses, displays, and reports
what it would have blocked, and changes no attendance until
`CLASSVISION_LIVENESS_MODE=enforce`. An unevaluated baseline silently deleting a
real student's attendance would trade a known harm for an unmeasured benefit.

**Fails safe, loudly.** A detector that raises on every call keeps the session
RUNNING, reports 0 live tracks, flips `liveness_available` to false, and can
still be finalised. An error whose message contains a model path and an API
token leaks neither - only the exception type.

The final audit found the one hole in this: a track confirmed LIVE *before* the
checker died kept reporting LIVE and ELIGIBLE, because the stability layer
correctly refuses to drop a confirmed state on a single bad observation. Right
for a student who turned away; wrong for a checker that is dead. The failure is
now latched, every view reports UNCERTAIN while it is set - finalisation
included - and the latch clears only when a check actually runs and returns.

**Measured.** Liveness overhead **0.22 - 0.90 ms per frame** with 4 faces
(roughly 0.06 - 0.22 ms per face per frame, **0.3% - 1.4%** of the 65 ms
budget - a range because the figure moves with machine load between sessions,
and the liveness-off column moves with it), over a fixed 20,000 frames
with **recognition calls identical** in both columns - the check that Step 5's
scheduling was left alone. Cropping the face before converting to grayscale
took the per-measurement cost from 2.67 ms to 0.15 ms. **Real webcam: 14.87 FPS
in report mode, 14.98 in enforce, 0 dropped - no measurable regression from
Step 8's 14.85.**

**Calibration is anchored on real measurements where it could be.** The texture
thresholds come from 40 real webcam frames (sharp 0.684-2.614; twice-blurred
0.062-0.248), with a deliberate UNCERTAIN band between the two thresholds. The
motion thresholds come from synthetic sequences, because a photograph's zero
non-rigid deformation can be reproduced exactly. Calibrating on generated data
is circular; that is stated, and no accuracy figure is derived from it.

**The biggest known gap:** the motion cue assumes YuNet's landmark jitter on a
static image is smaller than a real face's deformation, and that has NOT been
measured on this camera, because no face - real or presented - was available. A
replayed video defeats the cue by design. Masks, 3D attacks, deepfakes and
adversarial attacks are not addressed. Realistic behaviour is expected to be
UNCERTAIN-dominant rather than SPOOF-dominant, which is exactly why the default
mode reports instead of acting.

## Step 10 — Video authenticity analysis

**Done, as a RESEARCH BASELINE, and OFF BY DEFAULT.** **Step 10 is NOT a
deepfake detector, NOT production-grade security, and has NEVER been evaluated
against real manipulated video. Accuracy, precision, recall, F1, ROC-AUC, FAR,
FRR, APCER, BPCER and ACER are all "Not evaluated".**

A third independent evidence source, asking the question neither of the others
can see: a printed photo is a liveness problem, but a face-swapped video of
somebody who really is moving passes liveness comfortably, because it IS a
moving, deforming, detailed face.

**No model, and the search is recorded rather than summarised.** The official
OpenCV model zoo carries 24 models and none is a forgery or authenticity
detector - so the one source meeting this project's provenance bar had nothing.
The DFDC ensembles need PyTorch and ~1.7 GB of weights; FaceForensics++
baselines are trained on an EULA-restricted corpus and are not freely
redistributable; the small models survive only as unvetted ONNX conversions
with no published hash. And all of them are trained on high-quality face-swap
corpora rather than 640x480 webcam video, so their published numbers would not
transfer and there is no dataset here on which to re-measure them. Step 10
therefore adds no model, no weights and no dependency.

**Three cues, one physical idea.** A real camera produces ONE coherent image:
face and background share a sensor, an exposure curve and a noise floor. A
region generated separately becomes statistically DECOUPLED from its own
surroundings. Every cue compares the face against a ring of context just
outside it - boundary sharpness, temporal coupling, chroma offset - and the
coupling cue is mandatory for a SUSPICIOUS verdict because it is the one with
a physical argument behind it.

**The window size was measured, not chosen.** On 400 real observations from
this machine's webcam, the SAME video gives coupling spanning -0.823..+0.988 at
an 8-sample window and +0.427..+0.892 at 60, with the median steady at +0.75
throughout. A short window does not measure a different thing badly - it
measures the right thing too noisily, and would have produced constant false
SUSPICIOUS verdicts on ordinary students. Hence 60 samples minimum, about 12
seconds to a first verdict.

**The most important limitation, found by measuring and not hidden.** Coupling
only carries information when the scene's illumination actually changes: a
varying scene reads ~+0.75, a static evenly lit one reads ~+0.05 - and a
genuinely decoupled region sits in the same place. So the suspicious threshold
sits at -0.35, only anti-correlated evidence can reach it, and in an ordinary
classroom this baseline will report UNCERTAIN for most people and rarely
report SUSPICIOUS at all. It is built to miss manipulations rather than accuse
students.

**Three facts, never merged.** Identity, liveness and authenticity are held in
separate objects, updated from separate code paths and shown in separate
columns. A test asserts every direction: a SUSPICIOUS verdict leaves a
CONFIRMED identity untouched, recognition never promotes authenticity, and one
track can be liveness=LIVE and authenticity=UNCERTAIN simultaneously.

**A second, independent evidence gate**, in front of the untouched Step 6 rule
engine. `BLOCKED_SUSPICIOUS` is named separately from Step 9's `BLOCKED_SPOOF`
because "held-up photograph" and "manipulated video" are different findings.
Blocked is still not absent: a withheld student gets NO record rather than a
confident ABSENT, and the evidence is still stored for review.

**A bug the second gate exposed, fixed and regression-tested:** before the fix,
switching authenticity to enforce made the report-only LIVENESS opinion start
filtering evidence too - turning on one gate silently enforced the other. Only
an enforcing gate may remove a segment.

**Off by default**, a stronger position than Step 9 took, because the
eligibility column is shared and a second gate switched on by default would
silently alter what Step 9 means - and because a layer with zero validation
would mark almost everything "unverified" and teach a teacher to ignore it.

**Measured.** Overhead **0.179 ms/frame** (0.3% of the 65 ms budget) over a
fixed 1,000 frames, with recognition AND liveness call counts identical in both
columns. **Real webcam: 640x480, 29.85 FPS, 0 dropped, 3,342 captures, camera
released, session SCHEDULED -> COMPLETED.** That run had nobody in front of the
camera, so it exercised the plumbing and not the cues.

**Not handled and not claimed:** sophisticated replay, 3D masks, adversarial
attacks, high-quality face swaps, real-time neural deepfakes. No real face swap
has ever been tested, not once.

## Dashboard student enrollment — added after Step 10

**Done.** Not a numbered step: a gap between finished ones.

The project had two stores keyed on the same `student_id` and no link between
them - face vectors under `data/embeddings/` read by recognition, student rows
in `data/classvision.db` read by the dashboard. `ai/enroll_student.py` wrote
vectors and no row, so a student the camera recognised perfectly well showed up
as "No students" on the Students page and could not be picked as an expected
student. Two correct halves, never joined.

**Enrollment now writes both**, from either path, and the Students page joins
them on `student_id`.

**No second recognition implementation.** Detection is YuNet, embedding is
SFace, the quality gate is `ai/face_quality.py` and the store is
`ai/embedding_store.py` - the same objects the CLI has always used. The capture
DECISION moved into `SampleCollector`, which both paths now call, so the two
cannot drift into holding students to different standards. The CLI keeps its
window, its pose prompts and its consistency report.

**A threading bug worth recording.** The capture runs on a worker thread, and
SQLite connections are bound to the thread that created them. Handing the
request thread's connection across raised on every enrollment, silently, and
left the student enrolled for recognition and missing from the dashboard -
which is precisely the bug the feature exists to fix. The worker now opens its
own connection, the same one-per-thread rule Step 7 applies to the request
threadpool.

**No schema change.** Roll, class and section already lived in the embedding
store's metadata; the six tables are untouched.

**Privacy unchanged, boundary restated.** The backend now reads the embedding
store's METADATA - roll, class, section, sample count. It still never reads a
vector, the browser never uploads or receives a frame, and a completed
enrollment writes exactly `students.json` and one `.npz`.

**One webcam, one holder.** Enrollment and the live classroom refuse while the
other holds the camera, with a message naming which to stop.

## Accounts + the student registry — done

Two pieces of groundwork the roadmap had been quietly assuming.

**SQLite became the single source of truth for who a student is.** Schema
version 2 adds `roll_number`, `class_name` and `section` to the `students`
table. Until then they lived only in the Step 3 embedding store, so a plain
`SELECT * FROM students` answered "who is this person?" with half an answer and
anybody inspecting the database had to know a second store existed. The
migration is additive — `ALTER TABLE ADD COLUMN`, then a backfill that fills
blanks only and never overwrites — and `database/migrate_students.py` reports
exactly what it did.

Attendance summaries on the Students page are **derived** from
`attendance_records` on every read, never copied onto the student row. A number
stored next to the registry would be wrong the moment a session is re-evaluated
or an override applied.

**Authentication and roles.** PBKDF2-HMAC-SHA256 passwords, HttpOnly session
cookies, and `SUPER_ADMIN` / `TEACHER` enforced by FastAPI dependencies before
any data is read — not by hiding buttons. Four more tables: `users`,
`user_class_assignments`, `user_sessions`, `audit_log`. No new dependency: the
hashing is `hashlib`, the tokens are `secrets`.

A database with no accounts is **unconfigured** — the API stays open and the
dashboard asks for the first Super Admin. That is the state every installation
predating this change is in, and the alternative, a shipped default password,
is worse in every way that matters. There is no default account anywhere.

It has not been independently reviewed and is not evaluated production
security. The dashboard footer says so.

**The AI pipeline is untouched.** YuNet, SFace, tracking, recognition
stability, liveness and the authenticity baseline behave exactly as they did,
and the recognition threshold is still 0.45.

## Multi-term platform: academic history, alerts, model registry — done

Not a numbered step. This is what turned a system that watches one classroom
into one an institution can run across terms, and it was specified separately
from the numbering below — the "Step 11" and "Step 12" headings further down are
still the original plan (reports/exports and the analytics assistant) and are
still unbuilt.

**A student is permanent; where they sit is not.** Schema version 3 adds
`academic_sessions` and `student_academic_history`, so `STU001` is registered
once and stays `STU001` for their whole time at the institution. Roll number,
class, section and semester become facts about a *term*. Promotion CREATES a
history row and leaves the previous one exactly as it was, which is what keeps
last year's attendance describing last year's section.

The registry still mirrors the current term's class, section and roll on the
student row. That is deliberate: every existing query, the Students page, the
session roster and the recognition gallery read that row, and making them all
join a history table would be a rewrite with no benefit. Promotion moves both in
one transaction so they cannot disagree.

**Promotion guesses nothing.** Where an administrator supplies no allocation the
previous value is carried forward unchanged, and the response says which fields
were carried forward, per student. A preview endpoint reports exactly what a bulk
promotion would do and writes nothing. For a whole cohort, a three-column CSV is
read with the standard library — a class list does not justify a spreadsheet
dependency — and a missing column means "carry forward", never "blank".

Student lifecycle gained `TRANSFERRED` and `GRADUATED` alongside `ACTIVE` and
`INACTIVE`. None of them lose a record, and only `ACTIVE` students are promoted:
putting somebody who left into this term's register would generate an absence for
a person who is not there.

**Parent notifications, with no hard-coded threshold.** Rules are rows in
`notification_rules` — a type, a threshold, a window, optionally scoped to a
class. `0.75` does not appear in the evaluation engine, and a test asserts the
engine compares against no literal fraction at all. Three suggested rules can be
installed on request; an installation that never asks has none and sends nothing.

Delivery is an interface with three implementations — one that records and
delivers nothing (the default, and the honest behaviour with no messaging
account), one that always fails, one that succeeds. **Nothing has ever sent a
real message.** A notification row records who, which rule, the measured value
and the outcome; it does not store the guardian's phone number or email, which
are looked up at send time. The same alert stays quiet for the same student for
seven days, and the suppression is reported rather than silent.

Guardian contact is readable and writable by `SUPER_ADMIN` only. A teacher sees
`null` — the same as "nothing on file", so the response cannot be used to probe
which students have a number — and gets a `403` for trying to write one, rather
than a silent drop. They keep the `notification_enabled` switch, which carries no
contact detail.

**A model registry, and an experimental training pipeline that cannot promote
itself.** SFace remains the production recogniser. `ai/recognition_training.py`
learns a classifier over the embeddings SFace already produces — not a network
from scratch, which this project has neither the identities nor the compute for —
and registers the result as `TESTING` with its measured numbers. Samples are
grouped by capture so five near-identical enrollment frames cannot be split
across train and test; where capture information is missing, the whole student
goes into one split.

Activation requires clearing a strict floor (≥0.98 accuracy/precision/recall,
≤0.005 false acceptance, ≥5 held-out identities, and `held_out: true`), where a
missing metric is a failure rather than a pass, AND the model id typed out.
Rollback to the baseline is one call with no gate — deliberately asymmetric,
because when something is going wrong in a live classroom the safe direction
should be the easy one.

**Session lifecycle, correction, and early end.** Whether a session's metadata
can be edited depends on one question: has it produced attendance? Before that it
is a plan. After that a change is a correction — recorded, attributed, reasoned —
and a historical register is never recomputed because a class or a time was
edited. "Needs correcting" is a separate column rather than a status, so a
session that ran and ended keeps saying `COMPLETED`.

Cancelling keeps the row and its whole register; deleting is refused whenever
anything references the session, and the refusal names what would be lost.
Cancelling a completed session is refused outright.

Ending a session early **never turns a student who was marked present into
absent.** Finalisation reads the presence and attendance records the session
produced rather than asking whether the scheduled end time was reached: 40
expected with 32 recorded present, ended at 10:35 instead of 11:00, is 32 present
and 8 absent. Finalising twice changes nothing.

**The Live Classroom shows the whole room.** The full camera frame at the
camera's own resolution, contained rather than cropped or stretched, with the
existing overlay drawn on the frame the pipeline already produced — no second
stream, no extra inference. No frame reaches disk; showing a classroom is not
recording one.

**Cameras and device health.** A `cameras` row says where a device is and when it
last answered, and nothing about anybody it has seen. Only values the hardware
actually reported are recorded: a heartbeat with no frame rate keeps the last
measured one rather than inventing a number, and a camera nobody has heard from
reads `unknown` rather than online.

**The AI pipeline is untouched.** YuNet, SFace, tracking, recognition stability,
liveness and the authenticity baseline behave exactly as they did, and the
recognition threshold is still 0.45.

**What is not built:** the dashboard pages for academic terms, promotion,
allocation import, alert rules, the model registry and camera health. All of it
is complete and tested through the service layer and the API; the browser UI for
those six areas is not. The Students and Sessions pages are.

## Step 11 — Reports + exports ✅

Scheduled and on-demand reports built on the Step 7 analytics functions:
per-class and per-term summaries, CSV and PDF export, and a seat-map view of a
session. The analytics service was written so these reuse its numbers rather
than recomputing them slightly differently.

**That reuse is the whole constraint, and it is asserted rather than intended.**
`test_report_exports.py` checks the per-class rate is EQUAL to the number
`AnalyticsService.class_analytics` returns for that class, and the per-term rate
equal to `day_summary` over that term's sessions. A summary that drifted from
the dashboard by a rounding step fails. The reason is not tidiness: a teacher
who can read two different attendance figures for one class, with no way to tell
which is real, has been given a reason to trust neither.

**"No data" survives every layer.** A class with no completed session in the
window reports `None`, and the PDF prints `-`. A printed `0.0%` reads as "every
student was absent" and would be a fabrication.

**The seat map is not a seating plan.** No seat position is stored anywhere in
this schema and none is inferred from the camera — the tracker works in image
space and the image is not calibrated to furniture. What it is: the session's
students in roll-number order, in a stable grid, so one printed sheet can be
carried round a room. The page says so in those words rather than only in a
server note, and a student expected and never evaluated appears with **no
status**, never as absent.

**The PDF writer is hand-written** — about 400 lines, no reportlab, no
dependency. `test_report_exports.py` parses the bytes back: header, xref table,
every offset, trailer, and the page's decompressed text. A character Helvetica
cannot draw is COUNTED and reported in a response header rather than silently
replaced. An export nobody can open is worse than no export.

**Exports are scoped.** An export is the easiest thing in a system to walk out
of a building with, so a teacher's file is checked for another class's names in
its DECOMPRESSED BYTES rather than by trusting a row count.

**The front end was finished separately, and the gap is worth recording.** The
backend was complete and tested while the Reports page had four mode buttons,
no panels behind them, no JavaScript bound to them, and one unclosed `<div>`.
A browser silently repairs that and the page still renders, which is why it
survived — nothing failed. `test_report_ui.py` now checks the markup balance
directly, along with the panes reading only endpoints that already existed and
the page computing no figure of its own.

## Step 12 — AI assistant over trusted analytics ✅

A natural-language layer that answers questions by CALLING the Step 7 analytics
functions, never by computing its own numbers or narrating from raw rows.

That constraint is the whole point: the assistant may phrase an answer, but the
figure it quotes must be one a teacher could reproduce from the dashboard. An
assistant that does its own arithmetic would be a second, unverifiable source
of truth about a student's attendance.

**It is enforced by parsing, not by intention.** `test_assistant.py` walks the
service's AST and locates every arithmetic operation in it. The only one
permitted is a single `* 100` inside `_percent`, which formats a ratio analytics
has already computed; any other `+`, `-`, `*` or `/` on a figure is a failure.
Every answer's figures are then compared to the analytics result **exactly** —
not rounded, not within a tolerance.

**There is no model here and no network call.** It is a question router: an
intent table of regular expressions over a small, closed vocabulary. It can be
read in one sitting, it gives the same answer twice, and it works on a machine
with no internet connection — which is the machine this project is built for.

The cost of that is stated rather than hidden: **a question phrased outside the
patterns is not answered.** There is no paraphrasing and no fuzzy intent
guessing. The assistant says it did not understand and publishes the list of
what it can be asked, because the failure mode of guessing is a confident
answer to a question nobody asked.

**Every answer carries a receipt** — the analytics function, the arguments it
was given, and the dashboard page that shows the same number. It is rendered
next to the answer rather than tucked inside a disclosure, because it is the
reason to believe the answer.

**What it refuses to do.** Invent a number: "no data" is given as "no data",
never as 0%. Answer about a student the caller may not see: the answer is
byte-identical to the one for a student who does not exist, so it cannot be
used to discover who is in somebody else's class. Guess between two students
whose names both match: it asks which. Predict, advise, or explain WHY
attendance is what it is.

One regression is guarded by name because it is the easiest to ship and the
hardest to notice: *"What is the weather like?"* matched the rate intent on
"what is" and came back as **"I have no student by that name"** — a confident
non-answer to a question that was never about a student.

## Step 13 — Hardening ✅

Offline queue with sync, packaging and deployment.

**The queue solves two different problems in two different ways.** Not lost:
queueing is a write to the same SQLite file the attendance is in, so there is no
in-memory buffer to lose on a power cut — and a classroom appliance loses power.
Not twice: every item carries an idempotency key derived from WHAT IT IS, and
the column is UNIQUE, so re-running the exporter after a crash updates the same
rows instead of delivering a term of attendance a second time.

**Nothing is ever marked sent that was not acknowledged.** A target hands back
the keys it ACCEPTED rather than a boolean, because a partial delivery is the
normal case over a bad link and all-or-nothing handling either loses records or
duplicates them. A target that raises leaves every item queued with its attempt
counted and the reason in words. An item that has failed too many times becomes
`ABANDONED` — still in the table, still readable, **never deleted** — and
retrying it is a deliberate act, because retrying on a timer would hide whatever
was wrong.

**NOTHING HAS EVER SYNCHRONISED TO A REAL SERVER.** The default target is
configured with nowhere to send anything and delivers nothing, which is the
honest behaviour of an installation that has not been given an upstream, and it
is the state every installation is in. The other two targets are test doubles —
one always fails, one always succeeds — in the same shape as the notification
providers. No HTTP client is imported anywhere in the module and a test asserts
it. So a full queue means nothing has been **lost**, only that nothing has been
**sent**, and preflight reports it that way rather than as a fault.

**No payload can carry a biometric.** Embeddings, crops, frames, tokens,
passwords and file paths are refused by key name at every depth — including
inside lists — and the refusal NAMES the key, because silently stripping one
would send a record that looks complete and is not.

**Packaging is two tools and a set of instructions**, not an installer. This is
a local appliance.

`deploy/preflight.py` answers whether this machine can run a lesson. Every
failure it catches has a diagnosis that is obvious afterwards and invisible
beforehand — a model never downloaded, a half-downloaded model that exists and
fails at load time, a read-only `data/`, a schema a version behind — and all of
them arrive at the start of a lesson looking like "the camera is broken". **A
skipped check is never a pass**: each reports OK, PROBLEM or SKIPPED *with the
reason*, the exit code is non-zero only for PROBLEM, and a successful run says
how many checks proved nothing.

`deploy/backup.py` exists because copying `classvision.db` in a file explorer
produces a file that opens cleanly and is missing the most recent lessons — the
database is in WAL mode, and on a young database the plain `.db` does not even
have the tables yet. That is the worst possible failure, because it looks like a
backup. `test_deployment.py` reproduces it deliberately. The tool uses SQLite's
own backup API and then **opens the copy** and compares its row counts to the
live ones, because a backup nobody has opened is a hypothesis. Personnel
documents are opt-in; a backup never overwrites one; and restoring is
deliberately manual, because it throws away whatever happened after the backup
was taken and that is a person's decision.

**What remains unaddressed, and is NOT claimed:** the liveness baseline has
still not been evaluated against a real PAD dataset with an identity-disjoint
split, and every failure mode the Step 9 list records is still unaddressed.
Multi-camera operation is still architectural rather than demonstrated. No
message has ever reached a parent and no record has ever reached an upstream
server. And **attendance accuracy has still not been validated on a real
classroom population** — a machine that passes preflight is a machine whose
files and database are in order, not one whose figures have been shown to be
right.

---

## Duration-based attendance grace — added after Step 14

A longer class forgives more absence than a short one, because the same five
minutes mean different things in a half-hour tutorial and a three-hour lab.

| Total class duration | Grace |
|---|---|
| 30 minutes or less | 0 minutes |
| more than 30, up to 60 | 5 minutes |
| more than 60, up to 90 | 10 minutes |
| more than 90 | 15 minutes |

The bands live in `ai/attendance_rules.py` and nowhere else, as a **policy
setting** like every other threshold in that module. `grace_minutes_for_duration`
and `grace_seconds_for_duration` are the only implementations; the service
layer and the API import them rather than restating the table, and the
interface holds no copy of it at all.

**The grace period is a qualification, not an adjustment.** It is consulted
once, after the ordinary rules have finished, and it can only ever turn a
withheld PRESENT into a granted one:

```
ordinary rules decide  ->  PRESENT?  ->  return it unchanged
                       ->  not PRESENT, and 0 < missing <= grace
                                     ->  PRESENT, with a reason naming both figures
```

Running it **second** is what makes it a relaxation rather than a shortcut.
Asked first it claimed the credit for every PRESENT in the register, and a
student who missed two minutes of an hour read as having needed grace they
never used — which is how the first version of this was caught: it changed
the reason text on `test_present`, a test that had nothing to do with grace.

Nothing else moves. `present_seconds`, the presence ratio, `first_seen`,
`last_seen`, the segments, the loss periods and the `was_late` / `left_early`
flags are identical with the bands on and off — a suite asserts that across
twelve fields by evaluating the same evidence twice. A student present for 55
of 60 minutes was present for 55 and missing for 5, and the record goes on
saying so after the five minutes have been forgiven.

`missing_seconds` is `session_seconds - present_seconds` and never
`duration - grace`.

**No migration.** Both figures follow from `session_seconds` and
`present_seconds`, which `attendance_records` has held since Step 6, so the
API derives them at read time and no existing database needs upgrading. A
test asserts the absence of the columns, so adding one later has to be a
decision rather than a drift.

The window is the **observed** window, as it already was: a session finalised
after the camera stopped early measures both the absence and the allowance
against the time somebody was actually looking, so the two always share a
denominator.

---

## Student identity: names are not unique, identities are

Three children called Rahul Kumar is an ordinary register. Four defects stood
between that and two people being able to sign in as each other.

**The registration race.** `create_student` checked availability and then
called `upsert`. Two concurrent registrations for one `student_id` both read
"available", and the second became an UPDATE — one student's name written
over another's, leaving one row where there should be two and handing the
newcomer the first student's entire attendance history, because `student_id`
is what `attendance_records`, `presence_segments`, `session_expected_students`
and the embedding store all reference. The path now uses a strict
`StudentRepository.insert()`, the PRIMARY KEY is the final arbiter, and its
`IntegrityError` becomes the same 409 the pre-check raises. `upsert` is
untouched and still serves the enrollment path, where re-running a camera on
a registered student is a legitimate update.

**Case-insensitive lookup against case-sensitive uniqueness.**
`UserRepository.get_by_username` resolves a staff account with `COLLATE
NOCASE` — deliberately, because somebody typing their own name in a different
case is not a different person — while `users.username UNIQUE` is
case-sensitive. "rahul" and "Rahul" therefore both fitted in the table and the
lookup had two rows to choose from. That is ambiguous authentication, and
`IDENTITY_INDEXES` closes it with a unique index on `lower(username)` for both
credential tables.

Those two indexes are **not** in `INDEXES` or `SCHEMA`, and that is the one
interesting thing about them: unlike every other statement in `database.py`, a
UNIQUE index can fail on a database that is otherwise perfectly fine, because
an existing file may already hold the pair it forbids. Creating it in the same
`executescript` as the rest would take the whole upgrade down on exactly the
installations that most need to be told. So `enforce_identity_uniqueness()`
asks first, skips the index when the data does not satisfy it, and **returns**
the duplicate instead. Nothing is deleted, merged or renamed: which of two
people owns a name is a question about people. `deploy/preflight.py` is what
reports it.

**A lost race was a 500.** Nothing anywhere caught `sqlite3.IntegrityError`,
so the constraint doing its job arrived as a server error. Staff creation and
portal account issue now translate it into the conflict they already raise
from their pre-check, and the account that was created first is left exactly
as it is.

**Cross-table username collision** was checked case-sensitively, which would
have let a pupil take "Rahul" while the teacher "rahul" existed.

What was already right, and is now pinned by tests rather than by reading:
`students.name` carries no UNIQUE constraint; attendance is keyed
`UNIQUE (session_id, student_id, policy_version)` and foreign-keyed to the
registry by id; the embedding store names its files after `student_id`, so two
same-named students cannot share a face record; a portal username defaults to
the Student ID, which is unique by construction; passwords are PBKDF2 with a
per-account salt, so two students may share one password and still be two
accounts; and the duplicate check is a primary-key lookup rather than a sweep,
which is what makes 5,000 students a non-event — a suite registers five
thousand students who all share one name and asserts five thousand identities.

---

## Year + class + section filtering — schema version 7

The registry could already be filtered by class and by section. It could not
be filtered by year, and the reason turned out to be one missing column
rather than a missing concept.

**The year already existed.** `student_academic_history.academic_year` has
held it since the multi-term work, and that table's own comment states the
pattern it established: roll number, class and section live on the student row
as the CURRENT values "because that is what every existing query reads", while
history records each term separately so the previous one survives a promotion.
`academic_year` was the one field left out of that mirror.

So version 7 **finishes the mirror** rather than starting a new one. No new
concept, no renamed field, and `student_academic_history` remains the
authority — `academic_service.enrol()` writes the year onto the student row in
the same transaction it already used for the other three, so the registry
cannot drift from the history it mirrors.

Choosing the column over a join is a scale decision: a `LEFT JOIN
student_academic_history ON ... AND is_current = 1` on every page of the
Students view is a join nobody needs in order to ask a question about the
present.

**Migration.** One `ALTER TABLE students ADD COLUMN academic_year TEXT NOT
NULL DEFAULT ''`, through the additive path versions 2 and 3 already use, plus
**one derived UPDATE** — the only one in `migrate()`. `backfill_academic_year()`
copies the year off each student's `is_current` history row, which is the same
row their class and section were already mirrored from, so a school that has
been running terms does not re-enter data the database is holding. It invents
nothing: a student with no current enrolment keeps `''`. It writes only where
the column is still blank, so re-running it cannot overwrite a year somebody
has since corrected by hand.

`''` is a real value and is treated as one. A student registered before anybody
created a term has no year: they appear unfiltered, they appear under their
class, and they appear under **no** year — and `''` is never offered as a
filter option, because an empty year in the filter means "all years", which is
what Clear writes.

**Filtering is three independent ANDed clauses**, which is what makes all eight
combinations work without eight code paths. All three are `COLLATE NOCASE`,
matching how class and section already behaved.

**Paging moved into SQL, and this is the part that was actually slow.**
`list_students` used to read every matching row, build a dictionary per row,
and then slice out a page of twenty-five. Two of its filters genuinely cannot
be expressed in SQL — `enrolled` is a question for the embedding store, and a
teacher's class scope is a question for authz — and when either is in play the
rows still have to be read in full. But when neither is, which is the ordinary
case for an administrator browsing the registry, the database now does the
counting and the paging. At five thousand students that is twenty-five rows
instead of five thousand rows, five thousand dictionaries, and four thousand
nine hundred and seventy-five of them discarded.

`_principal_restricts()` is what decides. It returns False for the three
callers who remove nothing — no principal, an unrestricted one, and a teacher
holding the `can_view_all_students` grant — because `may_see_student()` answers
True for every row in all three cases, so skipping the Python pass cannot
change an answer.

A new index, `(academic_year, class_name, section)`, serves the year-leading
combinations. `idx_students_class` stays and still serves class, class+section
and a section-only scan. Section-only still scans deliberately: an index whose
leading column is a single letter shared by a third of the school would be read
and then discarded.

**`/api/students/filters`** answers what the three dropdowns should offer,
read from the registry so the lists are the school's own words. Each list is
narrowed by the other two selections and never by its own — 3rd Year + CSE
leaves Section offering A and B only, while Year still shows every year,
because a list narrowed by its own current value collapses to one option the
moment somebody uses it. It is ONE `GROUP BY` returning one row per distinct
year/class/section triple, so it is bounded by the timetable and not by the
roll, and the class-scope check is applied to those few dozen rows through the
same `may_see_student()` that filters the students — a dropdown must not offer
a class whose students the next request would refuse.

That route is declared **before** `/students/{student_id}`, because FastAPI
matches in order and "filters" would otherwise arrive as a student id. A test
asserts it.

The table gained a **Year** column, which matters most where names repeat:
year, class and section together are what tell two students called Rahul Kumar
apart at a glance. The count line reports "82 of 5,247", both figures being the
API's own `total` — the unfiltered one from a `limit=1` request, because the
count is a `COUNT(*)` and does not depend on how many rows were asked for, so
the line costs one row rather than five thousand. It is invalidated inside
`apiSend`, the one funnel every write passes through, rather than in each of the
handlers that add, remove, activate or deactivate a student.

The empty state distinguishes "no students" from "no students matching these
filters" and names the filters that produced it, because the second is almost
always a filter the reader can widen.

---

## Multi-class console: per-panel rosters and bulk finish

An audit came first, and most of this requirement turned out to be Step 14
already. What the console had: one manager per session in `LiveSessionRegistry`
with its own pipeline, timeline and counts; `GET /api/live/sessions` returning
one entry per session plus labelled totals; a grid whose column count comes
from `gridColumns(n)` and whose panel count comes from the entry count with no
cap; per-panel Focus / 10-min break / Finish, each carrying its own
`session_id`; focus mode with a switcher and a back button that drops focus
automatically when the focused session ends; one polling timer, started and
stopped by the view; and a session whose camera died staying visible as FAILED
rather than disappearing.

Three things were genuinely missing.

**Every panel now carries its own PRESENT NOW list.** Previously only the
focused panel named anybody. The obvious implementation — one
`/api/live/presence` request per panel — is ten requests a second with ten
classrooms, so `roster_preview()` rides along on the answer the panels already
ask for: one request draws the whole grid, however many classrooms are in it.

The roster is four fields: a name, the two states already on the panel's
telemetry, and the student id the rest of the interface keys off. No duration,
no track id, no cue value, no eligibility verdict, no embedding, no image — a
test asserts the exact field set, so a fifth field has to be a decision. It is
read from the runtime's published snapshot like every other live answer, so the
request thread never touches an object the worker is mutating.

Capped at six by `PANEL_ROSTER_LIMIT`, with `present_now_total` carrying the
real figure: a tile showing six of thirty says thirty and offers the focused
view for the rest. The cap lives in one place and the interface takes it rather
than deciding it — a test asserts the renderer contains no slice of its own.

The renderer handles no session id at all, and that is the isolation argument:
a panel can only render the array that arrived inside its own entry, so there
is no id for it to get wrong. A test asserts the absence.

**Finish all sessions.** The same per-session `/api/live/stop` the panel's own
Finish calls, once per session, scoped by that session's id, sequentially —
`Promise.all` would race a row of attendance writes. No endpoint was added, so
no finalisation rule is different here from the rule a single Finish follows. It
confirms first and the confirmation **names** every class, because a bulk
action whose scope you cannot read before agreeing to it is not a confirmation.
One class failing to stop names itself and leaves the others finished. It
appears only from two running classes upwards: with one, it is a second button
for what Finish already does, and two buttons for one irreversible action is how
the wrong one gets pressed.

**The module docstring was wrong.** `live_service.py` still opened with "One
session at a time — one camera, one lesson, one runtime", written before the
registry existed and contradicted by the registry's own comments twenty lines
below. It now describes what the code does: one runtime per session, as many
sessions as there are cameras, a second `/start` for an already-running session
still refused, and two sessions pointed at one physical device resolved
honestly — the second worker fails, its runtime goes to FAILED with the
camera's own reason, and the panel stays on the console saying so.

### What was verified, and what was not

`tests/test_multi_session_console.py` (19 tests) covers roster isolation, the
cap, the field set, the single-request contract, bulk finish, and panel counts
asserted individually at 1, 2, 3, 4, 5, 8, 10 and 15 — because "no maximum" is
a claim worth testing at a number nobody would hard-code.

Beyond the suite, the real application was run with **ten synthetic live
sessions** (runtimes driven directly, no camera opened) and driven over HTTP:
ten entries with ten distinct camera indices, ten separate rosters of the right
sizes, the cap visible as 6/7, 6/8, 6/9 and 6/10, and a break started on one
class leaving the other nine untouched.

**Not verified:** no browser rendered the grid — the Chrome extension was not
connected — so the layout at ten panels was not seen. No physical camera was
opened, and nothing here is evidence that ten simultaneous devices work.
Finalisation over HTTP could not be exercised with a synthetic runtime either:
`stop()` refuses a session the manager never started, which is correct, so
finish isolation rests on the automated suite.

---

## The spatial layer and Assembly Mode — built, and until now unwritten

Two features were built, tested and shipped without an entry here. This is
that entry, written from the code and from what the browser check rendered.

### The spatial layer — `spatial.css`, `spatial.js`

The 3D half of the console: the entry scene, the drifting background, the AI
core, the student constellation, the glass-cube charts and the move between
views.

**There is no three.js, no WebGL, no GSAP and no CDN.** That is the same
promise the sign-in screen makes in writing — this interface runs on a
classroom machine with no network — so the 3D is about ninety lines of
arithmetic: a Fibonacci point distribution, two rotation matrices written out
longhand and one divide for the perspective, drawn on a 2D canvas. Everything
else is CSS transforms, which the compositor does for free. `app.js` already
contained exactly this engine for the sign-in globe; this file is that idea
applied to the whole product.

**It is additive, and that is enforced rather than intended.** `app.js` owns
every figure; the layer is handed values and calls no endpoint, holds no
attendance state and invents nothing. Every call into it goes through
`window.Spatial` and is guarded, so deleting the three tags leaves the flat,
fully working console that was there before. `tests/test_spatial_interface.py`
(30 tests) asserts the guard, the absence of any external host or vendored
library, and that **nothing decorative is drawn behind a figure** — no blur,
no gradient and no animation on a selector a measured value is painted on,
which is the rule `styles.css` already kept for its own panels and which has
to hold for a layer whose whole job is putting glass in front of things.

**Four things stop it running**, and all four matter: `prefers-reduced-motion`
starts no loop at all and draws each scene once as a still frame, because a
blank rectangle looks like a broken page; a viewport under 1080px gets no
background or core loop, since CSS can drop a perspective but cannot stop a
`requestAnimationFrame`; a hidden tab stops rather than crawls; and each loop
checks that its own canvas is still connected and visible, so a dashboard left
on the students page is not painting a core nobody can see.

Rendered, the layer costs nothing a reader pays for: with reduced motion
emulated, **no element reports a running animation**, and the lowest measured
contrast on the dashboard's own figures is **13.45:1**.

The entry scene is the one part of the layer that is load-bearing for
interaction rather than decoration. It stands in front of the console after
sign-in — once per page load, crossable by Enter, Escape, its own button or a
click on the scene — and rendered, that is a full-viewport overlay at z-index
60 with `pointer-events: auto`. It is deliberate, it is documented in
`spatial.js`, and it is now also asserted in a browser, because an overlay
that swallows a click is not visible in a source file. See **The interface,
rendered** for the way that lesson was learned.

### Assembly Mode — `assembly.js`

A whole school in one room. A classroom is thirty people in rows you can read;
an assembly is five hundred, and the useful view of five hundred is a
formation seen from the stage, where the shape of the gaps is the information.

**The one thing to understand about it: there is no second attendance
system.** The file contains no rule, no threshold and no attendance decision.
Every endpoint it touches is one the live console already touches —
`/api/live/start`, `/status`, `/presence`, `/break`, `/break/end`, `/stop`,
`/stream.mjpg`, and the session's own expected roster — and
`tests/test_assembly_mode.py` (21 tests) holds `assembly.js` to that
**allowlist**, so a parallel register cannot appear quietly.

So an assembly is an ordinary session afterwards: scheduled in Sessions, bound
by the same expected roster and the same permissions, finalised by the same
rule engine, written to the same tables, and found later in the same sessions
list and the same reports.

Two details that are easy to get wrong and are tested:

- **The formation is drawn from the SESSION's expected students**, not from
  the class register. They are different lists the moment somebody is enrolled
  after the assembly was scheduled.
- **The live reading is labelled as a live reading.** The finalised figure is
  not that number: duration, grace and the late threshold are applied by the
  rules at finalisation, and a page that showed the two as one figure would be
  wrong twice a lesson.

The formation is operable rather than decorative — real buttons, real names,
reachable by keyboard — and every state is shown as a symbol and a word rather
than as a colour.

### What neither suite could verify, and now does

Both files say it plainly in their own headers: *no DOM is built and nothing
is painted; what is checked is the contract the source keeps, not the pixels
it produces.* That is still true of those suites. `tests/browser_check.py`
renders both — the entry scene, the console behind it, the theme in both
palettes, and the Assembly view — and the measurements are in the next
section.

---

## The interface, rendered — and a restore performed

Two of this project's standing limitations were that nobody had ever watched
the dashboard build a DOM, and that nobody had ever put a backup back. Both
were true for the same reason: each needs something the automated suite is
deliberately built without — a browser in the first case, a decision in the
second. Neither was going to stop being true on its own.

### `tests/browser_check.py` — Chrome, driven

Not part of `tests/run_all.py`, on purpose. It needs Google Chrome installed,
and the suite has to keep running on a machine that has no browser, no webcam
and no network, which is the same reason `--camera` is opt-in in preflight.

It starts the real application on a thread against a **temporary** database it
seeds, launches headless Chrome with a throwaway profile, and drives the page
over the DevTools protocol: it types into the sign-in form, presses keys,
clicks controls at their measured coordinates, reads back computed styles, and
writes a screenshot of each view. The WebSocket client is about a hundred
lines written out by hand in the same file — the project has no
browser-automation dependency and this was not worth adding one for.

The ten-classroom grid is drawn by pushing **ten synthetic runtimes** into the
live registry directly, exactly as `test_multi_session_console.py` does. That
is what makes the check possible on a laptop with one webcam, and it is also
its honest limit: no camera is opened, every panel's video area is empty by
construction, and nothing here is evidence that ten simultaneous devices work.

**Measured, in a 1600×1000 window:**

| | |
|---|---|
| Sign-in page | 1,740 DOM nodes, form painted |
| After signing in | 11 navigation items, 11 views mounted |
| Entry screen | 1582×904 at z-index 60, crossed by Enter |
| Sideways scroll | none at 1600×1000, 1024×768 or 820×1180 |
| Theme | light `rgb(238,242,248)`, dark `rgb(5,7,13)`, auto follows the system |
| Contrast, dashboard figures | lowest of six measured 13.45:1 |
| Contrast, console panels | lowest of six measured 5.37:1 at 11px |
| Reduced motion | no element reports a running animation |
| Ten classrooms | **10 panels, 10 distinct classes, 3 columns of the 4 `--live-columns` allows** |
| Per-panel roster | at most 6 names, "10 verified", "+4 more in the focused view" |
| Bulk finish | one control, "Finish all sessions", at ten panels |
| Requests | 43, every one to 127.0.0.1, none 4xx or 5xx |
| Script errors | none, in any view visited |

The column line is the one worth reading twice. `gridColumns(10)` returns 4,
and the laid-out grid has 3 — which is correct and is what the function's own
comment says it is for: the count is a **maximum** and `auto-fit` picks the
real number from the width. A source-reading test can confirm the 4. Only a
laid-out page can confirm that the 4 is a ceiling rather than a promise.

### Three things that only a rendered page showed

**The status row printed a version it did not have.** `/api/health` answers
`ready`, `missing` or `unknown` — a state. The Schema tile rendered `"v" +
health.schema` and so read **vready** on every dashboard this project has ever
shown. No suite could see it: the string is assembled at runtime out of a
value the source does not contain. The tile now reports the API's own word,
and a schema that is not ready is a `warn` rather than an `idle` fact, because
it is the one condition on that row that stops attendance being written.

**Every page load asked for a file that did not exist.** Browsers request
`/favicon.ico` whether or not anybody wrote one; this appliance had not, so
the request came back 404 and Chrome logged an error on a page where nothing
was wrong. There is now a `backend/static/favicon.svg` — drawn here, served
from here, no data URI and no second host, because
`tests/test_live_api.py` forbids an image embedded in the page and that rule
is right. The static-directory manifest in `tests/test_dashboard_frontend.py`
knows about the seventh file and holds it to the same checks as the other six.

**The first click after signing in never reaches the console.** This one was
not a defect — it was the check being wrong, and it is recorded because of how
convincing the wrong answer was. `spatial.js` puts a deliberate entry scene in
front of the console after sign-in, crossable by Enter, Escape, its own button
or a click on the scene. Rendered, that is a full-viewport overlay at z-index
60 with `pointer-events: auto` over every control in the rail. The check's
click on "Light" was swallowed by the door, crossed it, and the next reading
said the theme switch was broken. `click()` now hit-tests with
`elementFromPoint` and **refuses** to click a control something else is
covering, naming what is in the way; and the door is a check in its own right,
because "the console is reachable once the door is crossed" is worth asserting
in the only place it can be.

### `tests/test_restore.py` — the backup, put back

Ten tests, in `run_all.py`, no browser and no network. The six steps of
`deploy/README.md` are performed against a real temporary tree — a real
WAL-mode database, a real backup taken through `deploy/backup.py`, real
embedding files — and the restored installation is opened and queried
afterwards.

**There is still no `--restore` flag.** A restore throws away whatever
happened after the backup was taken, and that is a decision for a person. The
function that performs the steps lives in the test, which is the only place a
restore should happen without somebody agreeing to it, and a test asserts that
nothing in `deploy/` grew one.

**The headline is step 2, and it fails silently.** Copying the backup over
`classvision.db` while the old `-wal` is still beside it does not fail, does
not warn, and does not restore: both files descend from the same database, so
the stale log's salt still matches, SQLite replays it over the pages that were
just put there, and the installation comes back holding the very rows the
restore existed to discard. Measured both ways on one fixture: **8 records
back when only the `.db` is replaced, 4 when the three files are moved aside.**
That is why the notes say move the `-wal` and `-shm` too, and it is the
mistake a careful person makes anyway.

Also verified: that the restore discards what came after the backup, by name;
that "move, do not delete" leaves the superseded database openable and
complete, so the restore can be undone; that the embedding store and the
database come back to the **same moment**, since a vector belonging to a
student the tables have never heard of would be recognised by nobody; that a
file-explorer copy restores an installation which is short and which opens
cleanly and passes preflight, this being exactly what makes it dangerous; and
that preflight brings a restored schema-1 database forward to version 7.

The six documented steps are read out of `deploy/README.md` and asserted in
order, so the procedure a school will type and the procedure that has been
shown to work cannot drift apart.

### What is still not verified

No camera was opened and no browser saw a real classroom frame. A restore has
not been rehearsed on a school's machine, carrying a backup in from another
disk — this is a temporary tree on a developer's laptop, which is evidence
that the steps are right and in the right order, not that anybody has lived
through one. And neither of these touches the figures: attendance accuracy is
still not validated on a real classroom population.

---

## Rule for every step

1. Build the smallest version that runs.
2. Verify it on real hardware.
3. Write down the measured numbers.
4. Only then move on.
