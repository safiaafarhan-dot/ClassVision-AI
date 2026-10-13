# ClassVision AI — Vision & Differentiators

> Status: design document. Steps 1-10 are implemented; everything below that is still intent.

## The problem with "AI attendance" projects

Most classroom attendance projects stop at one idea: point a camera at the room,
match faces against a folder of photos, write a name into a CSV. That is a face
matcher, not a platform. It answers one question — *was this person's face seen
once?* — and it answers it in the way that is easiest to fool and least useful
to a teacher.

ClassVision AI is built around a different question.

## The core idea: presence, not roll-call

Attendance is a single yes/no stamped at one moment. **Presence is a timeline.**

A student who is scanned at the door and leaves after ten minutes is marked
"present" by every roll-call system. ClassVision AI models each student as a
*presence interval* across the session — when they were first seen, how long
they were continuously in frame, when they left — and derives attendance from
that timeline according to a rule the institution controls (for example:
"present = in the room for at least 60% of the session").

This single change is what turns the project from a script into a platform,
because a timeline can be queried, aggregated, and reported on. A checkbox
cannot.

## The seven pillars

### 1. Presence timeline as the primary record
The raw record is `(student, entered_at, left_at)` intervals, not a boolean.
Attendance status is a *derived* value, recomputed whenever the policy changes.

### 2. Privacy-first by construction
- Raw classroom video is **never** stored. Frames are processed and discarded.
- Enrolled identities are stored as numeric embeddings, not photos.
- Retention windows are configurable and enforced automatically.
- The system is designed to be defensible to a privacy review, not retrofitted
  for one.

**As built in Step 3**, the identity architecture is:

```
camera frame ──► detect ──► quality gate ──► align ──► embed ──► store vector
     │                                                              │
     └──────────────── frame discarded, never written ──────────────┘
```

- **No image leaves the frame buffer.** The enrollment tool writes embeddings
  and metadata only; a test asserts no image file is ever created, and the
  store refuses metadata fields named `image`, `photo`, `face_image`, `crop`
  or `frame`.
- **Embeddings are treated as biometric data**, not as anonymous numbers. They
  identify a person and are handled accordingly: `data/` is gitignored,
  vectors are never printed to logs, and a test scans every module to enforce
  that.
- **Deletion is real.** Removing a student removes their metadata *and* their
  vector file; a test asserts nothing referencing them survives.
- **Model provenance travels with the data.** Every enrollment records the
  model name, version and SHA-256, so a future model change cannot silently
  produce incomparable embeddings.

**UNKNOWN is a first-class answer.** As built in Step 4, the recogniser never
forces a face onto the nearest enrolled student: below the threshold it
returns no identity at all. This matters for Pillar 5 — a system that always
guesses would fill a teacher's review queue with confident mislabels of
visitors, parents and passers-by, and would train them to stop reading it.

**Honest limits.** Embeddings are often described as irreversible. Treat that
as a defence in depth, not a guarantee: research has shown approximate face
images can sometimes be reconstructed from embeddings. They are therefore
handled as sensitive personal data, not as anonymised data. This prototype
also does **not** yet encrypt data at rest — see the README limitations.

### 3. Anti-proxy verification
The classic failure of face-based attendance is a phone screen held up to the
camera. Planned defences: passive liveness checks and temporal consistency
(a real student persists across frames and moves; a photo does not).

**As built in Step 9**, this pillar has its first real code - and its first
honest measurement of how far that code does not go.

The insight the implementation rests on is that a photograph and a face differ
in a way that survives being waved about. Normalise the five facial landmarks
for translation *and* scale, and rigid movement disappears: carrying a print
across the room or towards the camera changes nothing about the normalised
geometry. What remains is **non-rigid deformation**, and a flat object has
none. A real head, being a moving 3D object, never stops producing it.

That cue is supported by two texture cues and never trusted alone, and no cue
ever decides from one frame. Blinking is deliberately not used: it is easy to
replay, easy to miss, and a person who does not blink for two seconds is not a
photograph.

**What Step 9 does NOT do, stated as plainly as the design intent above.** It
has never been shown a real photograph, screen, replayed video or mask. A
replayed video defeats the primary cue *by design*, because it contains real
facial deformation. No accuracy, FAR, FRR, APCER, BPCER or ACER is claimed -
they are "Not evaluated", not "pending". And the single largest unknown is that
the cue assumes the face detector's landmark jitter on a still image is smaller
than a real face's movement, which could not be measured because no face was
available to measure it with.

It is a research prototype, and it is labelled as one on the interface itself.

**As built in Step 10**, the pillar gained a second, independent layer asking a
question the first cannot see. A printed photograph is a liveness problem. A
face-swapped video of somebody who really is moving passes liveness
comfortably, because it *is* a moving, deforming, detailed face.

The idea this rests on is that a real camera produces **one coherent image**:
the face and the wall behind it share a sensor, an exposure curve and a noise
floor, so when the light changes they change together. A region generated
separately becomes statistically decoupled from its own surroundings, and that
can be measured without knowing anything about how it was generated.

**What Step 10 does NOT do, stated as plainly as the intent above.** It is not
a deepfake detector and has never been shown a real face swap. Its primary cue
only carries information when the scene's illumination actually changes - a
static, evenly lit room drives it to the same place a generated region sits, so
in an ordinary classroom it will report UNCERTAIN for most people and rarely
report SUSPICIOUS at all. It is deliberately built to miss manipulations rather
than accuse students, and it is **off by default** for the same reason.

Every accuracy metric is "Not evaluated", and that is meant literally.

### 4. Spatial awareness — the seat map
Detections are mapped onto a configurable classroom seating layout. This turns
the camera into a room sensor: occupancy density, empty-seat counts, and
per-zone presence — useful even where individual identification is not wanted.

### 5. Teacher-in-the-loop, always
Every automated mark is a *proposal* with a confidence score, not a verdict.
The teacher reviews, overrides, and confirms. Every override is logged, and the
system is auditable: for any mark you can ask "why?" and get an answer.

### 6. Offline-first edge operation
Classrooms have unreliable networks. The capture and inference layer runs fully
on-device and queues results locally, syncing when a connection returns. The
lesson never depends on the internet.

### 7. Analytics that create action
The end product is not a spreadsheet. It is early warning: attendance trend
lines per student, automatic flagging of at-risk patterns (declining
attendance, chronic lateness, partial-session departures), and reports
generated per class, per subject, and per term.

## What this is deliberately NOT

- Not a surveillance or behaviour-scoring system.
- Not an emotion-detection or "attention grading" tool — those are scientifically
  weak and ethically poor.
- Not a system that stores video of minors.

## Honest current state

Steps 1-9 are complete: camera pipeline, face **detection**,
privacy-preserving **enrollment**, live **identity verification**,
multi-person **tracking** with a **presence timeline**, configurable
**attendance rules** in SQLite, a local **teacher dashboard** with
deterministic analytics, a **live classroom session** that drives the whole
chain from the webcam and shows presence building up as it happens, and a
**liveness / presentation-attack** layer that asks whether a face came from a
person in the room, and a **video authenticity** layer that asks whether the
video came from this camera at all - the last two as research baselines, with
their limits written down.

Pillar 1 now reaches a person. A teacher can open a session, see who was
present and for how long, look at the presence timeline that produced each
result, ask "why?" of any record and get the stored evidence, and correct a
result with a reason that is kept forever alongside what the system originally
decided.

Pillar 5 is real in code rather than in prose: every decision carries its
numbers and its policy version, a correction never destroys the computed
result, and a recomputation never destroys a correction.

Two things the dashboard deliberately refuses to do. It will not show a
confident 0% for an empty database - a rate with nothing to divide by is
reported as "no data", and a test enforces the difference. And it will not call
anything an anomaly: the "needs review" list fires on stated arithmetic
thresholds that ship in the response, so a teacher can disagree with it.

**What is still not true.** Attendance accuracy on real people is unknown. The
recognition threshold underneath everything remains provisional and
unvalidated, no two people have yet been in front of this camera at once, and
every attendance figure so far comes from synthetic timelines with invented
students. A polished dashboard makes uncertain numbers *look* authoritative,
which is precisely why the limitations are stated on the interface itself.

There is also no authentication. The dashboard is a localhost tool, and no
login screen was added, because a fake one would imply a protection that does
not exist.

Steps 9 and 10 added a second and a third question the camera must answer, and
refused to let either contaminate the others.

    recognised  does NOT imply live
    live        does NOT imply recognised
    authentic   does NOT imply either
    suspicious  does NOT imply spoof

A photograph of a student IS that student's face, and recognition says so
correctly. What must not follow is that they were present - and the only way to
guarantee that is to keep the two facts in separate objects, updated by separate
code, displayed in separate columns, with neither able to set the other. Every
tempting shortcut here is the same shortcut: treating a match as proof of
presence, which is precisely the failure Pillar 3 exists to prevent.

That separation forced a decision this project would rather not have had to
make. Under enforcement, a student whose evidence is held back looks - to a rule
engine that has always been right about this - exactly like a student who never
came. Marking them ABSENT would turn *"we could not verify this"* into *"they
were not here"*, a much stronger claim and a false one. So such a student gets
**no attendance record at all**, and is named for review. "The system cannot
say" is an uncomfortable output and an honest one, and Pillar 5 is the reason it
is acceptable: a teacher decides.

For the same reason, enforcement is **off by default**. A liveness baseline
nobody has evaluated against a real attack should not be silently deleting a
real student's attendance. It reports what it would have done, and a human turns
it on.

Step 8 is where Pillar 2 stopped being a claim about storage and became a claim
about a running loop. The worker holds one frame, hands it to the pipeline and
drops the reference; nothing writes an image, no endpoint could serve one, and
the dashboard contains no `<img>`, `<video>`, `<canvas>` or `getUserMedia` at
all. A test enforces each of those, and a real camera session was checked
afterwards: no image, no vector and no video file was created anywhere.

It also forced a distinction the project had only written down before. While a
lesson is running, a student is `PRESENT`, `RECOGNIZED`, `TEMPORARILY_LOST` or
`SEEN_EARLIER` - and never `ABSENT`. Absence is a conclusion about a *finished*
session, and a live screen that guesses at it would be the fastest way to teach
a teacher to ignore the system. An unverified person, likewise, is a count on
screen and nothing else: never a name, never a nearest guess, and never anyone's
attendance.

Step 9 extended that privacy claim into the liveness layer rather than making
an exception for it. What a tracked person's liveness state actually retains is
five normalised points and two floating-point numbers per observation, for at
most twelve observations - held in an object whose `__slots__` make an image
field impossible rather than merely absent. No cue value reaches the API either:
the numbers are uncalibrated, and one printed beside a person's name would be
read as a confidence *about that person*, which it is not.

**What Steps 8 and 9 did not establish.** The real-camera runs that proved the
loop had nobody in front of them and nobody enrolled - they exercised the
plumbing, not recognition and not the liveness cues. No two people have yet been
in front of this camera at once. Pillar 3 is now partly built rather than
unbuilt, but a photograph held to the lens has still never actually been tried,
and a replayed video would defeat the primary cue by design.

Step 10 also produced the clearest example yet of measurement changing a
design. The authenticity window was going to be short; measuring the same real
video at several window lengths showed the estimator swinging from -0.82 to
+0.99 at eight samples while its median never moved. The cue was not weak - the
estimate was under-sampled, and a shorter window would have quietly accused
still students of being synthetic. The window is now long, the first verdict
takes twelve seconds, and that is the honest price.

Still design intent: reports and exports, the seat map, evaluating the liveness
baseline against a real PAD dataset and the authenticity baseline against a real
deepfake corpus - both with identity-disjoint splits - multi-camera, offline
sync, and the assistant that will one day query these same analytics functions
rather than inventing its own.

See `ROADMAP.md` for the build order.