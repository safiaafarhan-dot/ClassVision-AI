/* =========================================================================
   ASSEMBLY MODE
   =========================================================================

   A whole school in one room.

   THE ONE THING TO UNDERSTAND ABOUT THIS FILE: it contains no attendance
   logic whatsoever. Not a rule, not a threshold, not a decision. Assembly
   Mode is a VIEW onto machinery that already exists, and every endpoint it
   touches is an endpoint the live classroom console already touches:

     POST /api/live/start           start the pipeline on a session
     GET  /api/live/status          the runtime, once a second
     GET  /api/live/presence        who has been confirmed, once a second
     POST /api/live/break           pause
     POST /api/live/break/end       resume
     POST /api/live/stop            end, and finalise through the rules
     GET  /api/live/stream.mjpg     the frame the capture worker already has
     GET  /api/sessions/{id}        the session, and its expected roster
     GET  /api/sessions/expected-students
                                    the names behind those ids

   So an assembly is an ordinary session: scheduled in Sessions, bound by the
   same roster and the same permissions, finalised by the same rule engine,
   written to the same tables, and findable afterwards in the same sessions
   list and the same reports. Nothing on this page is a parallel register.

   What is different is the ROOM. A classroom is thirty people in rows you can
   read; an assembly is five hundred, and the useful view of five hundred is a
   formation seen from the stage, where the shape of the gaps is the
   information.

   WHAT IT DEPENDS ON, and the direction matters: this file uses app.js -
   api(), el(), card(), withBusyButton() and the rest - and app.js does not
   use this file except through window.Assembly, guarded. So a console served
   without assembly.js has an empty Assembly page and a completely working
   everything else, and that is the only coupling either way.
   ========================================================================= */

(function () {
  "use strict";

  /* The same clock the live console runs on. Not a faster one: the status
     endpoint reports what the pipeline has done, and asking twice as often
     does not make it report twice as much. */
  const POLL_MS = 1000;
  const ACTIVE_STATES = ["STARTING", "RUNNING", "STOPPING"];

  const $ = (id) => document.getElementById(id);

  /*
     Everything this view knows, and it is deliberately little.

     No attendance figure is cached here. `status` and `presence` are the last
     answers from the server and are replaced wholesale on every poll, so the
     page can never show a figure the server has stopped reporting. The only
     things that persist are the roster (which is per session, fetched once)
     and the stage timestamps (which are observations, labelled as such).
  */
  const A = {
    sessionId: null,
    session: null,       /* the SessionDetail, for class, times and roster */
    roster: [],          /* [{ id, name }] - the session's expected students */
    rosterFor: null,     /* which session the roster above belongs to */
    status: null,
    presence: null,
    summary: null,       /* the finalisation answer, once ended */
    timer: null,
    polling: false,
    streaming: false,

    /* When each pipeline stage was first OBSERVED by this page. Two of the
       six come from the server (started_at, stopped_at); the middle four are
       the first poll at which the pipeline reported the thing, and the note
       under the timeline says so rather than passing them off as server
       events. */
    stamps: {},

    /* Last drawn state per student, so a newly recognised person can be given
       the one-shot detection pulse. A state word per id and nothing else -
       no name, no evidence, and it does not outlive the page. */
    seen: Object.create(null)
  };

  function error(message) {
    const node = $("assembly-error");
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  function note(message) {
    const node = $("assembly-note");
    if (node) node.textContent = message || "";
  }

  /* app.js owns these. Reached through window at CALL time rather than bound
     at load time, because this script is parsed before app.js is. */
  function appFn(name) {
    return typeof window[name] === "function" ? window[name] : null;
  }

  function running() {
    return !!(A.status && ACTIVE_STATES.indexOf(A.status.state) >= 0);
  }

  /* ======================================================================
     THE ROSTER
     ----------------------------------------------------------------------
     Who is expected, with their names.

     The SESSION is the authority on who is expected - session.expected_students
     is the list that was frozen when the session was created, and it is the
     list the attendance rules will be applied to. The class roster is read
     only to put names against those ids.

     Which way round that is matters. Reading the class roster and treating it
     as the expected list would quietly include a student enrolled after the
     assembly was scheduled, and then report them absent from something they
     were never expected at.
  */
  async function loadRoster(sessionId) {
    if (A.rosterFor === sessionId && A.roster.length) return;

    const api = appFn("api");
    if (!api) return;

    A.roster = [];
    A.rosterFor = sessionId;

    const session = await api("/api/sessions/" + encodeURIComponent(sessionId));
    A.session = session;

    const expected = session.expected_students || [];
    if (!expected.length) return;

    /* Names. Allowed to fail: a formation of ids is still a formation, and a
       roster lookup that 403s for a scoped teacher must not empty the hall. */
    const names = Object.create(null);
    try {
      const roster = await api("/api/sessions/expected-students?class_name=" +
                               encodeURIComponent(session.class_id));
      for (const student of roster.students || []) {
        names[student.student_id] = student.name;
      }
    } catch (ignored) {
      /* ids it is */
    }

    A.roster = expected.map((id) => ({ id: id, name: names[id] || id }));
  }

  /* ======================================================================
     THE FORMATION
     ----------------------------------------------------------------------
     The expected roster, arranged in rows in front of a stage.

     The arrangement adapts to the real number of people, which is the whole
     requirement: ten students make two short rows and five hundred make
     twenty long ones, and neither is a special case. Rows nearest the stage
     are furthest from the reader, because the reader is standing at the back
     of the hall.

     ONLY THE EXPECTED ROSTER IS DRAWN. Somebody recognised who was not
     expected is real and is reported - as a figure, with a sentence - but
     they are not given a seat, because a seat in this formation means "this
     person was expected here" and inventing one would make the formation
     disagree with the session it is drawn from.
  */
  function formationShape(count) {
    /* Wider than deep, and capped so a row never needs its own scrollbar. */
    const perRow = Math.min(26, Math.max(6, Math.ceil(Math.sqrt(count * 2.2))));
    return { perRow: perRow, rows: Math.max(1, Math.ceil(count / perRow)) };
  }

  function seatState(studentId, byId) {
    const student = byId[studentId];
    if (!student) return "absent";

    const map = appFn("orbStateForStudent");
    return map ? map(student) : "present";
  }

  function renderFormation() {
    const container = $("assembly-rows");
    if (!container) return;

    const spatial = window.Spatial;
    const words = (spatial && spatial.stateWord) || {};
    const symbols = (spatial && spatial.stateSymbol) || {};

    if (!A.roster.length) {
      container.replaceChildren(el("p", { class: "muted small", text:
        A.sessionId
          ? "This session has no expected students, so there is no formation " +
            "to draw. Check the class roster in Students."
          : "Choose an assembly session to see the hall." }));
      return;
    }

    /* Who has been seen, keyed by id. Presence only ever carries CONFIRMED
       identities, so nobody in here is a guess. */
    const byId = Object.create(null);
    for (const student of ((A.presence && A.presence.students) || [])) {
      byId[student.student_id] = student;
    }

    const shape = formationShape(A.roster.length);
    const rows = [];

    for (let index = 0; index < shape.rows; index += 1) {
      const row = el("div", { class: "hall-row" });
      /* Row 0 is the front of the hall, nearest the stage, furthest from the
         reader - so it takes the largest depth. */
      row.style.setProperty("--depth", String(shape.rows - 1 - index));

      const slice = A.roster.slice(index * shape.perRow,
                                   (index + 1) * shape.perRow);

      for (const person of slice) {
        const state = seatState(person.id, byId);
        const word = words[state] || state.toUpperCase();

        const seat = el("button", { type: "button", class: "hall-seat" });
        seat.dataset.state = state;
        seat.dataset.studentId = person.id;

        /* The seat is a 22px circle with no text in it, so the accessible
           name is the only name it has. It carries all three facts the
           marker shows, because a screen reader cannot hover. */
        seat.setAttribute("aria-label", person.name + ": " + word);

        /* The holographic marker, in the DOM rather than generated on hover:
           it is real text that a screen reader reads from the label above,
           and CSS reveals it on hover and on focus alike. */
        const marker = el("span", { class: "holo-marker", "aria-hidden": "true" }, [
          el("span", { class: "holo-name",
                       text: (symbols[state] || "") + " " + person.name }),
          el("span", { class: "holo-state", text: word })
        ]);
        seat.appendChild(marker);

        /* The detection beat: only when this person was not present a moment
           ago and is now. Once, for 900ms. */
        const before = A.seen[person.id];
        A.seen[person.id] = state;
        if (before && before !== state && state === "present") {
          seat.dataset.justSeen = "true";
          window.setTimeout(() => { delete seat.dataset.justSeen; }, 1000);
        }

        row.appendChild(seat);
      }

      rows.push(row);
    }

    container.replaceChildren(...rows);

    const who = $("assembly-stage-who");
    if (who) {
      who.textContent = A.session
        ? A.session.class_id + " · " + A.session.session_date
        : "—";
    }

    const shapeNote = $("assembly-formation-note");
    if (shapeNote) {
      shapeNote.textContent =
        A.roster.length + " expected, in " + shape.rows + " row" +
        (shape.rows === 1 ? "" : "s") + " of up to " + shape.perRow +
        ". Hover or focus a person to see their name and state. Only the " +
        "session's expected students have a place in the formation.";
    }
  }

  /* ======================================================================
     THE FIGURES
     ----------------------------------------------------------------------
     Six counts and a rate, every one of them derived from two payloads the
     server sent and nothing else.

     The rate is the one worth being careful about. It is present over
     expected, computed here, and it is labelled A LIVE READING - because the
     finalised attendance is not this number: the rule engine runs at
     finalisation and applies duration, grace and the late threshold, so a
     student who is present on the timeline right now can still finalise as
     LATE or PARTIAL. Calling this "attendance" without that word would be
     the one dishonest figure on the page.
  */
  function tally() {
    const byId = Object.create(null);
    for (const student of ((A.presence && A.presence.students) || [])) {
      byId[student.student_id] = student;
    }

    const counts = { present: 0, late: 0, uncertain: 0, absent: 0 };
    for (const person of A.roster) {
      const state = seatState(person.id, byId);
      if (counts[state] === undefined) counts.absent += 1;
      else counts[state] += 1;
    }

    /* Confirmed identities that are not on this session's expected list.
       Reported, never seated, and never counted into the rate. */
    const expected = Object.create(null);
    for (const person of A.roster) expected[person.id] = true;
    const unexpected = Object.keys(byId)
      .filter((id) => !expected[id]).length;

    return {
      total: A.roster.length,
      present: counts.present,
      late: counts.late,
      uncertain: counts.uncertain,
      absent: counts.absent,
      /* People the camera is following whose identity was never confirmed.
         A COUNT OF TRACKS, not of students, and it is never a name: this is
         the figure the live console calls "unverified now". */
      unidentified: (A.status && A.status.unknown_tracks) || 0,
      unexpected: unexpected,
      rate: A.roster.length
        ? (counts.present / A.roster.length) * 100 : null
    };
  }

  function renderFigures() {
    const totals = tally();
    const container = $("assembly-cards");
    const card = appFn("card");
    if (!container || !card) return;

    container.replaceChildren(
      card("Total students", totals.total, "on this session's roster"),
      card("Present", totals.present, "confirmed in the hall now"),
      card("Absent", totals.absent, "expected, not yet seen"),
      card("Late", totals.late, "seen earlier, not visible now"),
      card("Held for review", totals.uncertain,
           "seen, evidence not trustworthy yet"),
      card("Unidentified people", totals.unidentified,
           "tracked, never named")
    );

    /* The ring and its breakdown. */
    if (window.Spatial) {
      window.Spatial.ring("assembly-ring", totals.rate, "Live attendance");
    }

    const breakdown = $("assembly-breakdown");
    if (breakdown) {
      breakdown.replaceChildren(...[
        ["Present", totals.present],
        ["Absent", totals.absent],
        ["Late", totals.late],
        ["Held for review", totals.uncertain],
        ["Unidentified", totals.unidentified]
      ].map(([label, value]) => el("li", {}, [
        el("span", { text: label }),
        el("span", { class: "att-count", text: String(value) })
      ])));

      breakdown.appendChild(el("li", {}, [
        el("span", { class: "muted small", text:
          totals.unexpected
            ? totals.unexpected + " confirmed student(s) are not on this " +
              "session's expected list, so they have no place in the " +
              "formation and are not counted in the rate."
            : "A live reading. The finalised attendance is computed by the " +
              "rules when the assembly ends, and applies duration and the " +
              "late threshold." })
      ]));
    }
  }

  /* ======================================================================
     THE PIPELINE READINGS
     ----------------------------------------------------------------------
     Straight off the status endpoint, in the units it reported them in.
     A field it did not send reads "No data" - never a zero, because "0 FPS"
     for a camera nobody has heard from is the most misleading thing this
     panel could print.
  */
  function renderStats() {
    const container = $("assembly-stats");
    const systemModule = appFn("systemModule");
    if (!container || !systemModule) return;

    const status = A.status;
    if (!status) {
      container.replaceChildren(systemModule("Pipeline", "idle", "NOT STARTED"));
      return;
    }

    const live = running();
    const level = live ? "ok" : "idle";
    const totals = tally();

    const reading = (value, format) =>
      (value === null || value === undefined)
        ? "No data" : (format ? format(value) : String(value));

    container.replaceChildren(
      systemModule("Pipeline", level, String(status.state || "IDLE")),
      systemModule("Camera", status.camera === "connected" ? "ok" : "idle",
                   String(status.camera || "unknown").toUpperCase()),
      systemModule("Camera FPS", level,
                   live ? reading(status.fps, (v) => Number(v).toFixed(1))
                        : "No data"),
      systemModule("Detected now", level, reading(status.active_tracks)),
      systemModule("Recognised", level, reading(status.recognized_students)),
      systemModule("Present", level, String(totals.present)),
      systemModule("Absent", level, String(totals.absent)),
      systemModule("Unidentified",
                   (status.unknown_tracks || 0) > 0 ? "warn" : level,
                   reading(status.unknown_tracks)),
      systemModule("Attendance rate", level,
                   totals.rate === null ? "No data"
                                        : totals.rate.toFixed(1) + "%",
                   "live reading, not finalised"),
      systemModule("Recognition", level, live ? "ACTIVE" : "IDLE",
                   reading(status.recognition_calls) + " calls"),
      systemModule("Liveness", status.liveness_available ? level : "idle",
                   status.liveness_available
                     ? String(status.liveness_mode || "off").toUpperCase()
                     : "UNAVAILABLE"),
      systemModule("Authenticity", status.authenticity_available ? level : "idle",
                   status.authenticity_available
                     ? String(status.authenticity_mode || "off").toUpperCase()
                     : "UNAVAILABLE")
    );
  }

  /* ======================================================================
     THE CAMERA
     ----------------------------------------------------------------------
     The same stream endpoint, the same capture worker, the same server-drawn
     overlays as the live classroom. This opens no second camera and triggers
     no extra inference, and - exactly as on the live page - the connection is
     dropped the moment nothing is looking at it, rather than leaving a
     generator open on the server for a hidden tab.
  */
  function renderCamera() {
    const frame = $("assembly-view");
    const idle = $("assembly-idle");
    const shell = $("assembly-cam");
    if (!frame || !shell) return;

    const live = running() && A.sessionId;
    const streamUrl = appFn("classroomStreamUrl");

    if (live && !A.streaming && streamUrl) {
      frame.src = streamUrl(A.sessionId);
      A.streaming = true;
      frame.hidden = false;
      if (idle) idle.hidden = true;
    }

    if (!live && A.streaming) {
      frame.removeAttribute("src");
      A.streaming = false;
      frame.hidden = true;
      if (idle) idle.hidden = false;
    }

    shell.dataset.live = live ? "true" : "false";

    /* The sweep over the formation runs while identities are being resolved
       and stops when the pipeline does - it is the pipeline state, not a
       decoration somebody has to remember to switch off. */
    if (window.Spatial) window.Spatial.scanning($("assembly-hall"), !!live);

    const status = A.status || {};
    const totals = tally();

    if (window.Spatial) {
      window.Spatial.cameraRail($("assembly-cam-rail"), [
        { key: "Live", value: live ? (status.on_break ? "BREAK" : "YES") : "NO" },
        { key: "Camera", value: String(status.camera || "").toUpperCase() },
        { key: "FPS", value: live && status.fps !== undefined
            ? Number(status.fps).toFixed(1) : null },
        { key: "Detected", value: status.active_tracks },
        { key: "Recognised", value: status.recognized_students },
        { key: "Present", value: totals.present },
        { key: "Authenticity", value: status.authenticity_available
            ? String(status.authenticity_mode || "off").toUpperCase()
            : "UNAVAILABLE" }
      ]);
    }
  }

  /* ======================================================================
     THE TIMELINE
     ----------------------------------------------------------------------
     Six stages as a lit path.

     Two of the timestamps are the SERVER'S: started_at and stopped_at come
     off the status endpoint. The middle four are the first poll at which the
     pipeline reported the thing, which is an observation by this page and is
     not the same as a server event - a tab that was in the background for a
     minute observed it late. The note under the path says exactly that,
     because a time presented without that qualification would be a time the
     page does not actually know.
  */
  const STAGES = [
    { key: "started",   label: "Assembly started" },
    { key: "camera",    label: "Camera connected" },
    { key: "detected",  label: "Students detected" },
    { key: "identity",  label: "Identity processing" },
    { key: "recorded",  label: "Attendance recorded" },
    { key: "completed", label: "Assembly completed" }
  ];

  function observeStages() {
    const status = A.status;
    if (!status) return;
    const clock = appFn("clock");
    const now = () => (clock ? clock(new Date().toISOString()) : "");

    /* The two the server timestamps. */
    if (status.started_at && !A.stamps.started) {
      A.stamps.started = clock ? clock(status.started_at) : status.started_at;
    }
    if (status.stopped_at && !A.stamps.completed) {
      A.stamps.completed = clock ? clock(status.stopped_at) : status.stopped_at;
    }

    /* The four this page observes. */
    if (status.camera === "connected" && !A.stamps.camera) {
      A.stamps.camera = now();
    }
    if ((status.active_tracks || 0) > 0 && !A.stamps.detected) {
      A.stamps.detected = now();
    }
    if ((status.recognition_calls || 0) > 0 && !A.stamps.identity) {
      A.stamps.identity = now();
    }
    if (A.summary && A.summary.finalized && !A.stamps.recorded) {
      A.stamps.recorded = now();
    }
  }

  function renderTimeline() {
    const container = $("assembly-spine");
    if (!container || !window.Spatial) return;

    /* The stage that is happening is the first one with no time yet, as long
       as something is running at all. */
    let currentIndex = -1;
    if (running()) {
      for (let index = 0; index < STAGES.length; index += 1) {
        if (!A.stamps[STAGES[index].key]) { currentIndex = index; break; }
      }
    }

    window.Spatial.spine(container, STAGES.map((stage, index) => ({
      label: stage.label,
      time: A.stamps[stage.key] || "",
      done: !!A.stamps[stage.key],
      current: index === currentIndex
    })));

    container.appendChild(el("p", { class: "muted small", text:
      "Assembly started and completed are the server's own timestamps. The " +
      "four stages between them are the first poll at which the pipeline " +
      "reported them, observed by this page - not server events." }));
  }

  /* ======================================================================
     COMPLETION
     ----------------------------------------------------------------------
     The formation settles and the summary takes its place.

     The figures here are the FINALISED ones out of /api/live/stop, which are
     not the live counts above: the rules ran, and they apply duration, grace
     and the late threshold. Showing the live tally as the outcome would be
     the obvious mistake, so the two are labelled differently and sourced
     differently.
  */
  function renderSummary() {
    const panel = $("assembly-summary");
    if (!panel) return;

    const summary = A.summary;
    panel.hidden = !summary;
    if (!summary) return;

    const card = appFn("card");
    const duration = appFn("duration");
    const hall = $("assembly-hall");

    /* The cinematic settle: one animation on the hall, and only when
       something is actually finishing. */
    if (hall && window.Spatial && !window.Spatial.reducedMotion()) {
      hall.dataset.settling = "true";
      window.setTimeout(() => { delete hall.dataset.settling; }, 1400);
    }

    const meta = $("assembly-summary-meta");
    if (meta) {
      const rows = [
        ["Session", summary.session_id],
        ["Status", summary.session_status],
        ["Policy version", summary.policy_version],
        ["Expected students", String(summary.expected_students)],
        ["Evaluated", String(summary.students_evaluated)],
        ["Duration", A.status && duration
          ? duration(A.status.elapsed_seconds) : "—"]
      ];
      meta.replaceChildren(...rows.map(([label, value]) => el("div", null, [
        el("dt", { text: label }),
        el("dd", { text: value === null || value === undefined
                     ? "—" : String(value) })
      ])));
    }

    const cards = $("assembly-summary-cards");
    if (cards && card) {
      const counts = summary.counts || {};
      cards.replaceChildren(
        card("Present", counts.PRESENT),
        card("Late", counts.LATE),
        card("Partial", counts.PARTIAL),
        card("Absent", counts.ABSENT),
        card("Attendance", summary.attendance_rate === null ||
                           summary.attendance_rate === undefined
               ? null : (summary.attendance_rate * 100).toFixed(1) + "%")
      );
    }

    const summaryNote = $("assembly-summary-note");
    if (summaryNote) {
      summaryNote.textContent = summary.already_finalized
        ? "This assembly had already been finalised, so nothing was changed."
        : "These are the finalised figures: the attendance rules ran when the " +
          "assembly ended and applied duration, grace and the late " +
          "threshold, so they need not match the live counts above. The " +
          "assembly is now an ordinary completed session and appears in " +
          "Sessions and in Reports.";
    }
  }

  /* ======================================================================
     THE CONTROLS
     ====================================================================== */

  function renderControls() {
    const live = running();
    const chosen = !!($("assembly-session") && $("assembly-session").value);
    const onBreak = !!(A.status && A.status.on_break);

    const set = (id, disabled) => {
      const button = $(id);
      if (button) button.disabled = !!disabled;
    };

    set("assembly-start", live || !chosen || A.busy);
    set("assembly-pause", !live || onBreak || A.busy);
    set("assembly-resume", !live || !onBreak || A.busy);
    set("assembly-end", !live || A.busy);
    /* Finalise is the same call as End. It is offered separately because the
       word somebody looks for after an assembly has stopped is "finalise",
       and finalising twice is already safe server-side - it answers
       already_finalized and writes nothing. */
    set("assembly-finalize", !chosen || A.busy);
    set("assembly-delete", live || !chosen || A.busy);

    const flag = $("assembly-live");
    if (flag) flag.hidden = !live;

    const elapsed = $("assembly-elapsed");
    const elapsedClock = appFn("elapsedClock");
    if (elapsed) {
      elapsed.textContent = live && A.status && elapsedClock
        ? elapsedClock(A.status.elapsed_seconds) : "";
    }

    const select = $("assembly-session");
    const camera = $("assembly-camera");
    if (select) select.disabled = live;
    if (camera) camera.disabled = live;

    if (live) {
      note(onBreak
        ? "On a break. Students are not marked absent for a break - the " +
          "interval is excluded from both sides of the attendance ratio."
        : "Running. Ending the assembly stops the camera and runs the " +
          "attendance rules for this session.");
    } else if (A.summary) {
      note("Finalised. The assembly is now an ordinary completed session.");
    } else if (chosen) {
      note("Ready. Starting opens the camera and begins detection, tracking, " +
           "recognition and the liveness and authenticity checks.");
    } else {
      note("No session chosen. Schedule the assembly in Sessions first, then " +
           "pick it here.");
    }
  }

  async function start(button) {
    const api = appFn("apiSend");
    const busy = appFn("withBusyButton");
    const explain = appFn("explainFailure");
    if (!api || !busy) return;

    const select = $("assembly-session");
    const camera = $("assembly-camera");
    if (!select || !select.value) {
      error("Choose an assembly session first.");
      return;
    }

    error("");
    A.sessionId = select.value;

    const payload = { session_id: select.value };
    /* A registered device if one was chosen; otherwise the server's own
       default, which is what the live console does too. No camera index is
       invented here. */
    if (camera && camera.value) payload.camera_id = camera.value;

    A.busy = true;
    renderControls();
    try {
      await busy(button, { busy: "Starting...", done: "Started" },
                 () => api("/api/live/start", "POST", payload),
                 (failure) => error(explain
                   ? explain(failure, "start the assembly") : failure.message));
    } catch (ignored) {
      return;
    } finally {
      A.busy = false;
    }

    A.summary = null;
    A.stamps = {};
    A.seen = Object.create(null);
    await poll();
  }

  async function pause(button, resume) {
    const api = appFn("apiSend");
    const busy = appFn("withBusyButton");
    const explain = appFn("explainFailure");
    if (!api || !busy || !A.sessionId) return;

    error("");
    const path = resume
      ? "/api/live/break/end?session_id="
      : "/api/live/break?session_id=";

    A.busy = true;
    renderControls();
    try {
      await busy(button,
                 { busy: resume ? "Resuming..." : "Pausing...",
                   done: resume ? "Resumed" : "Paused" },
                 () => api(path + encodeURIComponent(A.sessionId), "POST"),
                 (failure) => error(explain
                   ? explain(failure, resume ? "resume" : "pause")
                   : failure.message));
    } catch (ignored) {
      return;
    } finally {
      A.busy = false;
    }

    await poll();
  }

  /*
     Ending, and finalising, which are the same server call.

     The confirmation is deliberately NOT a new dialog. finishSession() in
     app.js already asks, in the words this product uses, and already handles
     the already-finalised answer and the banner - so this reuses it rather
     than growing a second confirmation that could drift from the first.
  */
  async function end(button, label) {
    const api = appFn("apiSend");
    const busy = appFn("withBusyButton");
    const explain = appFn("explainFailure");
    const banner = appFn("banner");
    if (!api || !busy || !A.sessionId) {
      error("No assembly is selected.");
      return;
    }

    const name = (A.session && A.session.class_id) || A.sessionId;
    const confirmed = window.confirm(
      (label || "End") + " the assembly for " + name + "?\n\n" +
      "This stops the camera and runs the attendance rules for this session. " +
      "Other running classes are not affected.");
    if (!confirmed) return;

    error("");
    A.busy = true;
    renderControls();

    let summary;
    try {
      summary = await busy(button,
        { busy: "Finalising...", done: "Completed" },
        () => api("/api/live/stop?session_id="
                  + encodeURIComponent(A.sessionId), "POST"),
        (failure) => error(explain
          ? explain(failure, "finalise the assembly") : failure.message));
    } catch (ignored) {
      return;
    } finally {
      A.busy = false;
    }

    if (!summary) return;

    A.summary = summary;
    if (banner) {
      banner(summary.already_finalized
        ? "That assembly had already been finalised. Nothing was changed."
        : "Assembly " + name + " completed: " + summary.students_evaluated +
          " of " + summary.expected_students + " student(s) evaluated.");
    }

    await poll();
    renderSummary();
  }

  /*
     Delete.

     Handed straight to the existing session removal dialog, which is the one
     place in this product that knows the difference between cancelling a
     session and deleting one, what each does to the attendance already
     recorded, and what has to be typed to confirm it. A second delete
     confirmation written here would be a worse copy of that.
  */
  function remove() {
    const open = appFn("openSessionRemoveDialog");
    if (!A.sessionId) {
      error("No assembly is selected.");
      return;
    }
    if (!open) {
      error("The session removal dialog is unavailable.");
      return;
    }
    error("");
    open(A.sessionId);
  }

  /* ======================================================================
     POLLING
     ====================================================================== */

  async function poll() {
    const api = appFn("api");
    const select = $("assembly-session");
    if (!api) return;

    const sessionId = A.sessionId ||
      (select && select.value ? select.value : null);
    if (!sessionId) {
      A.status = null;
      A.presence = null;
      draw();
      return;
    }

    A.sessionId = sessionId;

    try {
      A.status = await api("/api/live/status?session_id=" +
                           encodeURIComponent(sessionId));
    } catch (failure) {
      /* A session that is not running answers with an idle state rather than
         an error, so a failure here is a real one and is worth saying. */
      A.status = null;
      error(failure.message);
    }

    /* Presence only while something is running. Asking for the presence of a
       session with no runtime is a request for nothing. */
    if (running()) {
      try {
        A.presence = await api("/api/live/presence?session_id=" +
                               encodeURIComponent(sessionId));
      } catch (ignored) {
        /* The formation keeps its last state; the next poll retries. */
      }
    } else {
      A.presence = null;
    }

    observeStages();
    draw();
  }

  function draw() {
    renderFormation();
    renderFigures();
    renderStats();
    renderCamera();
    renderTimeline();
    renderControls();

    /* The core, when the assembly view is the one on screen. */
    if (window.Spatial) {
      const live = running();
      const totals = tally();
      window.Spatial.core(
        live ? "live" : A.summary ? "settling" : "idle",
        live ? (A.status.on_break ? "Break" : "Assembly") : "Idle",
        live ? totals.present + " of " + totals.total + " present"
             : A.summary ? "Assembly completed" : "No assembly running");
    }
  }

  function startPolling() {
    stopPolling();
    A.polling = true;
    A.timer = window.setInterval(() => {
      poll().catch(() => { /* the next tick retries */ });
    }, POLL_MS);
  }

  function stopPolling() {
    A.polling = false;
    if (A.timer) {
      window.clearInterval(A.timer);
      A.timer = null;
    }
  }

  /* ======================================================================
     LOADING THE VIEW
     ====================================================================== */

  /*
     The sessions that can be run as an assembly, and the cameras to run them
     on.

     Both lists are the SAME lists the live console offers, read from the same
     endpoints, so a session schedulable there is schedulable here and a device
     registered in Camera health is immediately selectable in both. There is
     no separate notion of an assembly session or of an assembly camera.
  */
  async function fillSessions() {
    const api = appFn("api");
    const select = $("assembly-session");
    if (!api || !select) return;

    const runnable = [];
    for (const status of ["ACTIVE", "SCHEDULED"]) {
      const query = new URLSearchParams({ status: status, limit: "50",
                                          offset: "0" });
      const page = await api("/api/sessions?" + query.toString());
      runnable.push(...(page.items || []));
    }

    const chosen = A.sessionId || select.value;

    select.replaceChildren(...(runnable.length
      ? runnable.map((session) => el("option", {
          value: session.session_id,
          text: session.session_date + " — " + session.class_id +
                " (" + session.status + ")"
        }))
      : [el("option", { value: "",
                        text: "No scheduled or active session" })]));

    if (chosen && runnable.some((item) => item.session_id === chosen)) {
      select.value = chosen;
    }
    A.sessionId = select.value || null;
  }

  async function fillCameras() {
    const api = appFn("api");
    const select = $("assembly-camera");
    if (!api || !select) return;

    let cameras = [];
    try {
      cameras = (await api("/api/cameras")).items || [];
    } catch (ignored) {
      /* The server's default camera still works, which is what the empty
         option below means. */
    }

    const chosen = select.value;
    select.replaceChildren(
      el("option", { value: "", text: "Server default" }),
      /* The same name the live console gives a device, in the same order of
         preference: the room it is in, then its label, then its id. Two
         pages naming one camera two different ways is how an operator ends
         up starting an assembly in the wrong hall. */
      ...cameras.map((camera) => el("option", {
        value: camera.camera_id,
        text: (camera.classroom || camera.label || camera.camera_id) +
              " — " + camera.camera_id
      })));
    if (chosen) select.value = chosen;
  }

  async function load() {
    error("");

    try {
      await fillSessions();
    } catch (failure) {
      error(failure.message);
    }
    await fillCameras();

    /* An assembly already running when the page is opened. /api/live/sessions
       is the runtime's own list, so this finds a session started before this
       browser was, which is the case that matters after a reload. */
    const api = appFn("api");
    if (api) {
      try {
        const live = await api("/api/live/sessions");
        const items = live.items || [];
        const select = $("assembly-session");
        /* Only adopt a running session that is actually in the dropdown: the
           live console may be running a classroom lesson, and that is not
           this page's assembly. */
        const match = items.find((entry) => select &&
          Array.from(select.options).some((option) =>
            option.value === entry.session_id));
        if (match) {
          A.sessionId = match.session_id;
          if (select) select.value = match.session_id;
        }
      } catch (ignored) {
        /* An older server has no runtime list. */
      }
    }

    if (A.sessionId) {
      try {
        await loadRoster(A.sessionId);
      } catch (failure) {
        error(failure.message);
      }
    }

    await poll();
    renderSummary();
  }

  /* ======================================================================
     WIRING
     ====================================================================== */

  let wired = false;

  function wire() {
    if (wired) return;
    wired = true;

    const on = (id, handler) => {
      const node = $(id);
      if (node) node.addEventListener("click", (event) =>
        handler(event.currentTarget));
    };

    on("assembly-start", (button) => start(button));
    on("assembly-pause", (button) => pause(button, false));
    on("assembly-resume", (button) => pause(button, true));
    on("assembly-end", (button) => end(button, "End"));
    on("assembly-finalize", (button) => end(button, "Finalise"));
    on("assembly-summary-finalize", (button) => end(button, "Finalise"));
    on("assembly-delete", () => remove());

    on("assembly-open-attendance", () => {
      const go = appFn("switchView");
      const open = appFn("openSession");
      if (go) go("sessions");
      if (open && A.sessionId) open(A.sessionId);
    });

    on("assembly-open-report", () => {
      const go = appFn("switchView");
      if (go) go("reports");
    });

    /* A form with a select in it submits on Enter, and an unhandled submit
       reloads the page and discards everything this view holds. */
    const form = $("assembly-controls");
    if (form) form.addEventListener("submit", (event) => event.preventDefault());

    const select = $("assembly-session");
    if (select) {
      select.addEventListener("change", async () => {
        A.sessionId = select.value || null;
        A.summary = null;
        A.stamps = {};
        A.seen = Object.create(null);
        A.roster = [];
        A.rosterFor = null;
        if (A.sessionId) {
          try { await loadRoster(A.sessionId); }
          catch (failure) { error(failure.message); }
        }
        await poll();
      });
    }
  }

  /* ======================================================================
     THE PUBLIC SURFACE
     ----------------------------------------------------------------------
     Three functions, called by switchView() and refresh() in app.js.

     leave() stops the POLLING and nothing else. The camera keeps running,
     because the assembly is still happening - exactly the rule the live
     console follows when a teacher looks at another tab.
     ====================================================================== */

  window.Assembly = {
    load: async function () {
      wire();
      await load();
    },

    enter: function () {
      wire();
      startPolling();
    },

    leave: function () {
      stopPolling();
      /* Drop the stream rather than leave a generator open on the server for
         a view nobody is looking at. */
      if (A.streaming) {
        const frame = $("assembly-view");
        if (frame) {
          frame.removeAttribute("src");
          frame.hidden = true;
        }
        A.streaming = false;
        const idle = $("assembly-idle");
        if (idle) idle.hidden = false;
      }
    }
  };
})();
