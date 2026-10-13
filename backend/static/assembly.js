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
    const details = Object.create(null);
    try {
      const roster = await api("/api/sessions/expected-students?class_name=" +
                               encodeURIComponent(session.class_id));
      /* Inactive students are read too: one who left after the session was
         scheduled is still on the frozen expected list, and a seat labelled
         with an id rather than a name is a seat nobody can act on. Being
         listed here is not being expected - the expected list is the
         session's, above. */
      for (const student of (roster.students || [])
                             .concat(roster.inactive || [])) {
        details[student.student_id] = student;
      }
    } catch (ignored) {
      /* ids it is */
    }

    /* Section and roll number travel with the name, because an assembly is
       organised by section and read out by roll - and both come off the same
       roster response the names did, so this costs no extra request. A
       student the lookup could not reach keeps their id and no section,
       which is drawn as "No section recorded" rather than guessed at. */
    A.roster = expected.map((id) => {
      const student = details[id] || null;
      return {
        id: id,
        name: (student && student.name) || id,
        roll: (student && student.roll_number) || "",
        section: (student && student.section) || "",
        className: (student && student.class_name) || ""
      };
    });
  }

  /* ======================================================================
     THE VIEW CONTROLS
     ----------------------------------------------------------------------
     Search, filter, arrangement, seat size and projector mode.

     THE ONE RULE THESE ALL OBEY: they change what is DRAWN, never what is
     COUNTED. The figures above the hall, the ring, the breakdown and the
     pipeline readings are always the whole session, whatever is typed in the
     search box - and the note under the hall says how many seats a filter is
     hiding, so a half-empty formation can never be mistaken for a half-empty
     hall.

     None of them issues a request either. Every one of them is arithmetic
     over the two payloads the poll already fetched.
  */
  const V = {
    search: "",
    filter: "",          /* "" | present | absent | late | uncertain */
    group: "rows",       /* rows | section | roll | name | state */
    seatSize: "normal",
    sort: "name",
    sortDesc: false,
    projector: false
  };

  /* The order states are listed in when the formation is grouped by state:
     worst news first, because the reason somebody groups by state is to find
     the people who are not here. */
  const STATE_ORDER = ["absent", "uncertain", "late", "present", "unknown"];

  function normalise(value) {
    return String(value === null || value === undefined ? "" : value)
      .toLowerCase();
  }

  /* Does this person match what was typed. Id, name and roll number, because
     those are the three things a register is read by. */
  function matchesSearch(person) {
    if (!V.search) return false;
    const needle = V.search;
    return normalise(person.name).includes(needle) ||
           normalise(person.id).includes(needle) ||
           normalise(person.roll).includes(needle);
  }

  function matchesFilter(state) {
    if (!V.filter) return true;
    if (V.filter === "absent") {
      /* "Not seen yet" is absent OR unknown: neither has been confirmed, and
         a formation that hid one of them would under-report the gap. */
      return state === "absent" || state === "unknown";
    }
    return state === V.filter;
  }

  /* Who is on screen, with their live state, in the order the toolbar asked
     for. One function, used by the formation, the register, the absentee list
     and both CSV exports, so none of the five can disagree with the others. */
  function people() {
    const byId = Object.create(null);
    for (const student of ((A.presence && A.presence.students) || [])) {
      byId[student.student_id] = student;
    }

    const rows = A.roster.map((person) => {
      const live = byId[person.id] || null;
      return {
        id: person.id,
        name: person.name,
        roll: person.roll || "",
        section: person.section || "",
        state: seatState(person.id, byId),
        live: live,
        first: live && live.first_seen ? live.first_seen : null,
        last: live && live.last_seen ? live.last_seen : null,
        /* 0 is a measured value for somebody just recognised; null is "the
           pipeline has not said". The two must not collapse into one. */
        duration: live ? live.duration_seconds : null,
        segments: live ? live.segment_count : null,
        liveness: live ? live.liveness_state : null,
        authenticity: live ? live.authenticity_state : null
      };
    });

    if (V.group === "section") {
      rows.sort((a, b) => (a.section || "￿").localeCompare(
        b.section || "￿") || a.name.localeCompare(b.name));
    } else if (V.group === "roll") {
      /* Roll numbers are school's own words, so they are compared as numbers
         when they are numbers and as text when they are not. */
      rows.sort((a, b) => {
        const left = Number(a.roll), right = Number(b.roll);
        if (Number.isFinite(left) && Number.isFinite(right)) {
          return left - right;
        }
        return String(a.roll).localeCompare(String(b.roll));
      });
    } else if (V.group === "name") {
      rows.sort((a, b) => a.name.localeCompare(b.name));
    } else if (V.group === "state") {
      rows.sort((a, b) =>
        STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state) ||
        a.name.localeCompare(b.name));
    }
    /* "rows" is the roster's own order, which is the session's expected list
       and therefore the order the school put them in. Left alone. */

    return rows;
  }

  /* ======================================================================
     WHAT THE PIPELINE REPORTED, AS WORDS
     ====================================================================== */

  /* A reading, or "No data". Never a zero nobody measured: "0s" against a
     student the pipeline has never mentioned is the most misleading cell this
     page could print. */
  function reading(value, format) {
    if (value === null || value === undefined) return "No data";
    return format ? format(value) : String(value);
  }

  /*
     THE ASSEMBLY'S OWN WORDS FOR A STATE.

     Deliberately NOT the register's attendance vocabulary, and deliberately
     not statusPill: "ABSENT" is a decision the rule engine makes at
     finalisation, and printing it beside a student the camera simply has not
     resolved yet would be this page claiming something it does not know.

     "Not seen yet" is the honest reading of both absent and unknown, and it
     is the phrase the breakdown beside the ring already uses - so one student
     is never described two ways on one page.
  */
  const LIVE_WORD = {
    present: "Present now",
    late: "Seen earlier",
    uncertain: "Held for review",
    absent: "Not seen yet",
    unknown: "Not seen yet"
  };

  function stateWord(state) {
    return LIVE_WORD[state] || String(state);
  }

  /* The same pill shape the rest of the console uses, carrying the state as a
     data attribute so the stylesheet can colour it from the one palette the
     seats and the constellation are drawn from. */
  function statePill(state) {
    const symbols = (window.Spatial && window.Spatial.stateSymbol) || {};
    return el("span", { class: "live-pill", "data-state": state,
                        title: stateWord(state) }, [
      el("span", { class: "sym", "aria-hidden": "true",
                   text: symbols[state] || "•" }),
      stateWord(state)
    ]);
  }

  function timeOnly(stamp) {
    const fn = appFn("timeOfDay");
    if (fn) {
      const out = fn(stamp);
      if (out) return out;
    }
    return stamp ? String(stamp) : null;
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

    const hall = $("assembly-hall");
    if (hall) hall.dataset.seatSize = V.seatSize;

    if (!A.roster.length) {
      container.replaceChildren(el("p", { class: "muted small", text:
        A.sessionId
          ? "This session has no expected students, so there is no formation " +
            "to draw. Check the class roster in Students."
          : "Choose an assembly session to see the hall." }));
      const empty = $("assembly-formation-note");
      if (empty) empty.textContent = "";
      const filtered = $("assembly-filter-note");
      if (filtered) filtered.textContent = "";
      return;
    }

    /* Everybody, in the order the toolbar asked for. The FILTER is applied
       below rather than here, because a hidden seat still has to be counted
       in the note that says how many are hidden. */
    const everybody = people();
    const shown = everybody.filter((person) => matchesFilter(person.state));

    const shape = formationShape(Math.max(1, shown.length));
    const rows = [];
    let matched = 0;

    /* Grouping by section draws one row per section rather than one row per
       sqrt of the roster: a section that spills past the row cap continues on
       the next row, so a section of ninety is still visibly one group. */
    const slices = [];
    if (V.group === "section") {
      let current = null;
      for (const person of shown) {
        const key = person.section || "";
        if (!current || current.key !== key ||
            current.people.length >= shape.perRow) {
          current = { key: key, people: [] };
          slices.push(current);
        }
        current.people.push(person);
      }
    } else {
      for (let index = 0; index < shown.length; index += shape.perRow) {
        slices.push({ key: null,
                      people: shown.slice(index, index + shape.perRow) });
      }
    }

    slices.forEach((slice, index) => {
      const row = el("div", { class: "hall-row" });
      /* Row 0 is the front of the hall, nearest the stage, furthest from the
         reader - so it takes the largest depth. */
      row.style.setProperty("--depth", String(slices.length - 1 - index));
      if (slice.key) row.dataset.group = slice.key;

      for (const person of slice.people) {
        const state = person.state;
        const word = stateWord(state);

        const seat = el("button", { type: "button", class: "hall-seat" });
        seat.dataset.state = state;
        seat.dataset.studentId = person.id;

        /* The seat is a small circle with no text in it, so the accessible
           name is the only name it has. It carries every fact the marker
           shows, because a screen reader cannot hover. */
        seat.setAttribute("aria-label", person.name + ": " + word +
          (person.section ? " (" + person.section + ")" : ""));

        /* Searching DIMS the hall and lights the matches rather than removing
           anybody: the shape of the formation is the information, and a
           formation that rearranged itself on every keystroke would destroy
           the thing somebody is looking at. */
        if (V.search) {
          const hit = matchesSearch(person);
          seat.dataset.match = hit ? "true" : "false";
          if (hit) matched += 1;
        }

        /* The holographic marker, in the DOM rather than generated on hover:
           it is real text that a screen reader reads from the label above,
           and CSS reveals it on hover and on focus alike. */
        const symbols = (window.Spatial && window.Spatial.stateSymbol) || {};
        const marker = el("span", { class: "holo-marker", "aria-hidden": "true" }, [
          el("span", { class: "holo-name",
                       text: (symbols[state] || "") + " " + person.name }),
          el("span", { class: "holo-state", text: word })
        ]);
        seat.appendChild(marker);

        /* Selecting a person opens the profile the register and the students
           page open. One destination, one code path. */
        seat.addEventListener("click", () => openProfile(person.id));

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
    });

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
        A.roster.length + " expected, in " + rows.length + " row" +
        (rows.length === 1 ? "" : "s") +
        (V.group === "section" ? ", grouped by section" : "") +
        ". Select a person to open their profile; hover or focus one to see " +
        "their name and state. Only the session's expected students have a " +
        "place in the formation.";
    }

    /*
       WHAT THE TOOLBAR IS HIDING, in words.

       The single most important line on this page when a filter is on. The
       figures above the hall are always the whole session; this says how many
       seats are not being drawn, so a formation with four people in it can
       never be read as a hall with four people in it.
    */
    const filterNote = $("assembly-filter-note");
    if (filterNote) {
      const hidden = A.roster.length - shown.length;
      const parts = [];
      if (V.filter) {
        parts.push("Showing " + shown.length + " of " + A.roster.length +
                   " seats" + (hidden ? " — " + hidden + " hidden by the " +
                   "filter" : "") + ". The figures above are the whole " +
                   "session, not the filter.");
      }
      if (V.search) {
        parts.push(matched + " match" + (matched === 1 ? "" : "es") +
                   " for “" + V.search + "”, lit in the hall.");
      }
      filterNote.textContent = parts.join(" ");
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

  /* ======================================================================
     BY SECTION
     ----------------------------------------------------------------------
     The reading a head of year asks for: which sections are in the hall, and
     which one is still in a corridor.

     Grouped in the browser from the roster and the presence payload that are
     already here - no request, and no figure that can disagree with the ring.
     A roster with no section recorded says so rather than inventing a group,
     because "(no section recorded)" is a data-entry problem somebody can fix
     and an invented group is one they cannot find.
  */
  function renderSections() {
    const container = $("assembly-sections");
    const note = $("assembly-sections-note");
    if (!container) return;

    const rows = people();
    if (!rows.length) {
      container.replaceChildren(el("p", { class: "muted small", text:
        "No expected roster, so there is nothing to group." }));
      if (note) note.textContent = "";
      return;
    }

    const groups = new Map();
    for (const person of rows) {
      const key = person.section || "—";
      if (!groups.has(key)) {
        groups.set(key, { present: 0, late: 0, uncertain: 0, total: 0 });
      }
      const group = groups.get(key);
      group.total += 1;
      if (group[person.state] !== undefined) group[person.state] += 1;
    }

    const named = Array.from(groups.keys()).filter((key) => key !== "—");
    named.sort((a, b) => a.localeCompare(b));
    if (groups.has("—")) named.push("—");

    container.replaceChildren(...named.map((key) => {
      const group = groups.get(key);
      const share = group.total ? (group.present / group.total) * 100 : 0;

      const bar = el("div", { class: "section-bar", role: "img",
        "aria-label": (key === "—" ? "No section recorded" : key) +
          ": " + group.present + " of " + group.total + " present" });
      bar.appendChild(el("span", { class: "section-bar-fill" }));
      bar.firstChild.style.setProperty("--share", share.toFixed(1) + "%");

      return el("div", { class: "section-row" }, [
        el("span", { class: "section-name",
                     text: key === "—" ? "No section recorded" : key }),
        bar,
        el("span", { class: "section-figure num",
                     text: group.present + " / " + group.total })
      ]);
    }));

    if (note) {
      note.textContent = named.length +
        (named.length === 1 ? " group" : " groups") +
        ". Present now, against the expected roster.";
    }
  }

  /* ======================================================================
     ARRIVALS
     ----------------------------------------------------------------------
     The last people the pipeline confirmed, most recent first. Straight off
     presence.first_seen, which is an OBSERVATION - the time this pipeline
     first resolved that identity, not a time anybody arrived at the hall and
     not an attendance status.
  */
  const ARRIVALS_SHOWN = 12;

  function renderArrivals() {
    const container = $("assembly-arrivals");
    const count = $("assembly-arrivals-count");
    if (!container) return;

    const seen = people()
      .filter((person) => person.first)
      .sort((a, b) => String(b.first).localeCompare(String(a.first)));

    if (!seen.length) {
      container.replaceChildren(el("li", { class: "muted small", text:
        running()
          ? "Nobody has been confirmed yet."
          : "No assembly is running, so nothing has been confirmed." }));
      if (count) count.textContent = "";
      return;
    }

    container.replaceChildren(...seen.slice(0, ARRIVALS_SHOWN)
      .map((person) => el("li", { class: "arrival" }, [
        el("span", { class: "arrival-when num",
                     text: timeOnly(person.first) || "—" }),
        el("span", { class: "arrival-who" }, [
          el("strong", { text: person.name }),
          el("span", { class: "muted small",
            text: person.section ? person.id + " · " + person.section
                                 : person.id })
        ]),
        el("span", { class: "arrival-state", "data-state": person.state,
                     text: stateWord(person.state) })
      ])));

    if (count) {
      count.textContent = seen.length > ARRIVALS_SHOWN
        ? "latest " + ARRIVALS_SHOWN + " of " + seen.length
        : seen.length + " confirmed";
    }
  }

  /* ======================================================================
     STILL MISSING
     ----------------------------------------------------------------------
     Names, not a count, because a count cannot be read out in a hall.

     NOT A LIST OF ABSENTEES, and the note under it says so: absence is
     decided by the rules at finalisation, and somebody at the back of a hall
     may simply not have been resolved yet.

     Copy and CSV are both built here out of what is already on the page. No
     export endpoint is called and nothing is written on the server.
  */
  function missingPeople() {
    return people().filter((person) =>
      person.state === "absent" || person.state === "unknown");
  }

  function renderMissing() {
    const container = $("assembly-missing");
    const count = $("assembly-missing-count");
    if (!container) return;

    const missing = missingPeople();
    const copy = $("assembly-missing-copy");
    const csv = $("assembly-missing-csv");
    if (copy) copy.disabled = !missing.length;
    if (csv) csv.disabled = !missing.length;

    if (!A.roster.length) {
      container.replaceChildren(el("p", { class: "muted small", text:
        "Choose an assembly session to see who is expected." }));
      if (count) count.textContent = "";
      return;
    }

    if (!missing.length) {
      container.replaceChildren(el("p", { class: "muted small", text:
        running()
          ? "Every expected student has been confirmed."
          : "Nothing is running, so nobody has been confirmed yet." }));
      if (count) count.textContent = running() ? "none" : "";
      return;
    }

    container.replaceChildren(el("ul", { class: "missing-list" },
      missing.map((person) => {
        const open = el("button", { type: "button", class: "btn-link",
                                    text: person.name });
        open.addEventListener("click", () => openProfile(person.id));
        return el("li", {}, [
          open,
          el("span", { class: "muted small",
            text: person.section ? person.id + " · " + person.section
                                 : person.id })
        ]);
      })));

    if (count) {
      count.textContent = missing.length + " of " + A.roster.length;
    }
  }

  /* The profile a name opens. The SAME profile the register and the student
     page open, through app.js, rather than a second one written here. */
  function openProfile(studentId) {
    const go = appFn("switchView");
    const open = appFn("openStudent");
    if (!go || !open) return;
    go("students");
    open(studentId).catch(() => {
      /* A teacher scoped away from this student gets a 404 from the server,
         which is the permission doing its job. */
    });
  }

  /* ======================================================================
     THE LIVE REGISTER
     ----------------------------------------------------------------------
     The formation shows shape; this shows evidence. One row per expected
     student with everything the pipeline reported about them, which is what
     somebody queried about an assembly a week later actually needs.

     Sorted by any column, filtered by the same toolbar as the hall, and
     exportable as a CSV built out of these very rows.
  */
  function sortedRegister() {
    const rows = people().filter((person) => {
      if (!matchesFilter(person.state)) return false;
      if (V.search && !matchesSearch(person)) return false;
      return true;
    });

    const by = V.sort;
    const direction = V.sortDesc ? -1 : 1;

    /* A missing value sorts last in both directions: a row the pipeline has
       said nothing about is not "the earliest arrival". */
    const compare = (left, right, pick, numeric) => {
      const a = pick(left), b = pick(right);
      const aMissing = a === null || a === undefined || a === "";
      const bMissing = b === null || b === undefined || b === "";
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      return (numeric ? (a - b) : String(a).localeCompare(String(b)))
             * direction;
    };

    const pickers = {
      name: [(p) => p.name, false],
      id: [(p) => p.id, false],
      roll: [(p) => p.roll, false],
      section: [(p) => p.section, false],
      state: [(p) => STATE_ORDER.indexOf(p.state), true],
      first: [(p) => p.first, false],
      duration: [(p) => p.duration, true]
    };

    const picker = pickers[by] || pickers.name;
    rows.sort((left, right) => compare(left, right, picker[0], picker[1]));
    return rows;
  }

  function renderRegister() {
    const table = $("assembly-register");
    if (!table) return;

    const body = table.querySelector("tbody");
    const rows = sortedRegister();
    const duration = appFn("duration");

    /* Which column is sorted, said in the markup rather than only in colour,
       so a screen reader is told the order the table is in. */
    table.querySelectorAll(".th-sort").forEach((button) => {
      const active = button.dataset.sort === V.sort;
      const header = button.closest("th");
      if (header) {
        if (active) {
          header.setAttribute("aria-sort",
                              V.sortDesc ? "descending" : "ascending");
        } else {
          header.removeAttribute("aria-sort");
        }
      }
      button.dataset.active = active ? "true" : "false";
      button.dataset.direction = active && V.sortDesc ? "desc" : "asc";
    });

    const count = $("assembly-register-count");
    if (count) {
      count.textContent = A.roster.length
        ? rows.length + " of " + A.roster.length + " shown"
        : "";
    }

    const csv = $("assembly-register-csv");
    if (csv) csv.disabled = !rows.length;

    if (!rows.length) {
      body.replaceChildren(el("tr", {}, [
        el("td", { colspan: "11", class: "muted", text: A.roster.length
          ? "No expected student matches the filters above."
          : "Choose an assembly session to see its expected roster." })
      ]));
      return;
    }

    body.replaceChildren(...rows.map((person) => {
      const open = el("button", { type: "button", class: "btn-link",
                                  text: "Open" });
      open.addEventListener("click", () => openProfile(person.id));

      return el("tr", {}, [
        el("td", { text: person.name }),
        el("td", { text: person.id }),
        el("td", { text: person.roll || "—" }),
        el("td", { text: person.section || "—" }),
        el("td", {}, [statePill(person.state)]),
        el("td", { class: "num", text: timeOnly(person.first) || "No data" }),
        el("td", { class: "num", text: duration
          ? reading(person.duration, (v) => duration(v))
          : reading(person.duration) }),
        el("td", { class: "num", text: reading(person.segments) }),
        el("td", { text: reading(person.liveness) }),
        el("td", { text: reading(person.authenticity) }),
        el("td", { class: "row-actions-cell" }, [
          el("div", { class: "row-actions" }, [open])
        ])
      ]);
    }));
  }

  /* ======================================================================
     TAKING IT OFF THE SCREEN
     ----------------------------------------------------------------------
     A clipboard list for a message, and a CSV for the office.

     BOTH ARE BUILT FROM THE ROWS ALREADY ON THE PAGE. No export endpoint is
     called, nothing is written on the server, and the file therefore cannot
     contain a row the screen did not show - which is the same guarantee the
     reports page makes about its own CSV, and for the same reason.

     Everything here is a LIVE READING, and the first line of the file says so
     in words, because a spreadsheet outlives the screen it came from.
  */

  /* RFC 4180 quoting: a name with a comma in it must not become two columns,
     and a field opening with =, + or - must not be executed by a spreadsheet
     that treats it as a formula. */
  function csvField(value) {
    let text = (value === null || value === undefined) ? "" : String(value);
    if (/^[=+\-@]/.test(text)) text = "'" + text;
    if (/[",\n\r]/.test(text)) text = '"' + text.replace(/"/g, '""') + '"';
    return text;
  }

  function csvRows(rows) {
    return rows.map((row) => row.map(csvField).join(",")).join("\r\n");
  }

  /* The file name says which assembly and when it was taken, because a folder
     of files called assembly.csv is a folder of one useful file. */
  function exportName(kind) {
    const session = (A.session && A.session.class_id) || A.sessionId || "assembly";
    const date = (A.session && A.session.session_date) ||
      new Date().toISOString().slice(0, 10);
    return (session + "-" + date + "-" + kind)
      .replace(/[^A-Za-z0-9._-]+/g, "-") + ".csv";
  }

  function download(name, text) {
    /* A Blob and an object URL, revoked straight afterwards: this page holds
       no file and leaves none behind. */
    const blob = new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function exportRegister() {
    const rows = sortedRegister();
    if (!rows.length) return;

    const durationFn = appFn("duration");
    const header = [
      ["ClassVision AI - assembly live register. A LIVE READING, not " +
       "finalised attendance: the rules apply duration and the late " +
       "threshold when the assembly is finalised."],
      ["Session", (A.session && A.session.session_id) || A.sessionId || ""],
      ["Class", (A.session && A.session.class_id) || ""],
      ["Date", (A.session && A.session.session_date) || ""],
      ["Taken at", new Date().toISOString()],
      []
    ];

    const table = [[
      "Student ID", "Name", "Roll", "Section", "State", "First seen",
      "Last seen", "Seen for", "Seen for (seconds)", "Stretches",
      "Liveness", "Authenticity"
    ]].concat(rows.map((person) => [
      person.id, person.name, person.roll, person.section,
      stateWord(person.state),
      person.first || "", person.last || "",
      durationFn ? reading(person.duration, (v) => durationFn(v))
                 : reading(person.duration),
      person.duration === null || person.duration === undefined
        ? "" : person.duration,
      person.segments === null || person.segments === undefined
        ? "" : person.segments,
      person.liveness || "", person.authenticity || ""
    ]));

    download(exportName("register"), csvRows(header.concat(table)));
  }

  function exportMissing() {
    const missing = missingPeople();
    if (!missing.length) return;

    const header = [
      ["ClassVision AI - expected and not yet confirmed. NOT A LIST OF " +
       "ABSENTEES: absence is decided by the attendance rules when the " +
       "assembly is finalised."],
      ["Session", (A.session && A.session.session_id) || A.sessionId || ""],
      ["Class", (A.session && A.session.class_id) || ""],
      ["Date", (A.session && A.session.session_date) || ""],
      ["Taken at", new Date().toISOString()],
      []
    ];

    const table = [["Student ID", "Name", "Roll", "Section"]]
      .concat(missing.map((person) =>
        [person.id, person.name, person.roll, person.section]));

    download(exportName("not-seen"), csvRows(header.concat(table)));
  }

  /*
     The missing list, as text, for a message.

     navigator.clipboard needs a secure context and this console is normally
     served over plain http on a local network, so the failure is expected
     rather than exceptional - and when it fails the names are SELECTED on the
     page instead, which is a working answer rather than an apology.
  */
  function copyMissing() {
    const missing = missingPeople();
    if (!missing.length) return;

    const text = missing
      .map((person) => person.name + " (" + person.id +
        (person.section ? ", " + person.section : "") + ")")
      .join("\n");

    const note = $("assembly-missing-note");
    const said = (message) => { if (note) note.textContent = message; };

    const selectInstead = () => {
      const list = $("assembly-missing");
      if (!list) return;
      const range = document.createRange();
      range.selectNodeContents(list);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      said("This browser would not give the page the clipboard - the names " +
           "are selected instead, so Ctrl+C copies them.");
    };

    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      selectInstead();
      return;
    }

    navigator.clipboard.writeText(text).then(
      () => said(missing.length + " name(s) copied. Expected and not yet " +
                 "confirmed - not absent."),
      selectInstead);
  }

  /* ======================================================================
     PROJECTOR MODE
     ----------------------------------------------------------------------
     The hall, full screen, for the wall at the back of the room.

     requestFullscreen on the hall element rather than a class on the body,
     for one reason that matters: the browser's own Escape gets somebody out
     of it. A CSS "full screen" has to invent its own way out and then teach
     it to people.
  */
  function toggleProjector() {
    const hall = $("assembly-hall");
    if (!hall) return;

    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
      return;
    }

    if (!hall.requestFullscreen) {
      error("This browser will not show an element full screen. Use the " +
            "browser's own full-screen key instead.");
      return;
    }
    hall.requestFullscreen().catch((failure) => error(failure.message));
  }

  function renderProjector() {
    const button = $("assembly-projector");
    const hall = $("assembly-hall");
    const on = !!(hall && document.fullscreenElement === hall);
    V.projector = on;

    if (hall) hall.dataset.projector = on ? "true" : "false";
    if (button) {
      button.setAttribute("aria-pressed", on ? "true" : "false");
      button.textContent = on ? "Leave projector mode" : "Projector mode";
    }
  }

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

    /* The summary takes the hall's place, so opening it is a navigation: it
       gets a step on the one stack Back pops, which is how the control in the
       top bar and the one in this panel's own head end up doing the same
       thing. Only on the transition - redrawing a summary that is already
       open is not a second navigation. */
    if (summary && panel.hidden) {
      const step = appFn("pushDetailStep");
      if (step) step();
    }

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
    renderSections();
    renderArrivals();
    renderMissing();
    renderRegister();
    renderStats();
    renderCamera();
    renderTimeline();
    renderControls();
    renderProjector();

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

    /* ---------- the view controls ----------

       Every one of these redraws from the payloads already in A, so not one
       of them waits on the network: a filter that had to ask the server
       before it could dim a seat would be unusable in a hall. */

    const search = $("assembly-search");
    if (search) {
      search.addEventListener("input", () => {
        V.search = search.value.trim().toLowerCase();
        draw();
      });
    }

    const filter = $("assembly-filter");
    if (filter) {
      filter.addEventListener("change", () => {
        V.filter = filter.value;
        draw();
      });
    }

    const group = $("assembly-group");
    if (group) {
      group.addEventListener("change", () => {
        V.group = group.value;
        draw();
      });
    }

    const seatSize = $("assembly-seat-size");
    if (seatSize) {
      seatSize.addEventListener("change", () => {
        V.seatSize = seatSize.value;
        draw();
      });
    }

    on("assembly-projector", () => toggleProjector());
    on("assembly-missing-copy", () => copyMissing());
    on("assembly-missing-csv", () => exportMissing());
    on("assembly-register-csv", () => exportRegister());

    /* Sorting the register. The header button carries the column, so there is
       one listener rather than eleven, and pressing the sorted column again
       reverses it - which is what every other table in this console does. */
    const register = $("assembly-register");
    if (register) {
      register.querySelectorAll(".th-sort").forEach((button) => {
        button.addEventListener("click", () => {
          const column = button.dataset.sort;
          if (V.sort === column) V.sortDesc = !V.sortDesc;
          else { V.sort = column; V.sortDesc = false; }
          renderRegister();
        });
      });
    }

    /* Projector mode is left through the browser's own Escape as well as
       through the button, so the button has to follow the browser rather than
       assume it is the only way out. */
    document.addEventListener("fullscreenchange", renderProjector);

    /*
       Two keys, and only while the assembly view is the one on screen.

       Deliberately NOT a shortcut for start, pause or end: those stop a
       camera and run the attendance rules, and a single keystroke is the
       wrong control for either.
    */
    document.addEventListener("keydown", (event) => {
      const assembly = $("view-assembly");
      if (!assembly || assembly.hidden) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      /* Never while somebody is typing - "f" belongs in the search box. */
      const active = document.activeElement;
      const tag = active ? active.tagName : "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (document.querySelector("dialog[open]")) return;

      if (event.key === "/") {
        event.preventDefault();
        if (search) search.focus();
      } else if (event.key === "f" || event.key === "F") {
        event.preventDefault();
        toggleProjector();
      }
    });

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
    },

    /*
       Put the summary away and give the hall back.

       Called by Back in app.js, which is the one control that closes a detail
       panel anywhere in this console. It forgets the FINALISATION ANSWER and
       nothing else: the session is still finalised, the attendance is still
       written, and both are still findable in Sessions and in Reports -
       closing a panel has never been allowed to undo a decision.
    */
    closeSummary: function () {
      A.summary = null;
      const panel = $("assembly-summary");
      if (panel) panel.hidden = true;
      draw();
    }
  };
})();
