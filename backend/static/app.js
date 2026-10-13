/*
  ClassVision AI - Dashboard (Step 7)

  Plain JavaScript, no framework, no build step, no external request. The page
  fetches JSON from the local backend and renders it.

  Two rules run through the whole file:

    1. Never invent a number. When the API returns null for a rate, that means
       "nothing to divide by" and the UI says so instead of printing 0%.
    2. Never rely on colour alone. Every status renders as a symbol plus its
       text, so the table survives greyscale printing and colour blindness.
*/

"use strict";

const STATUS_SYMBOL = {
  PRESENT: "✓",          // check
  LATE: "▲",             // triangle
  PARTIAL: "◐",          // half circle
  LEFT_EARLY: "◀",       // left triangle
  ABSENT: "✕",           // cross
  UNKNOWN: "?",
  MANUAL_OVERRIDE: "✎",  // pencil
  SCHEDULED: "○",
  ACTIVE: "●",
  COMPLETED: "✓",
  CANCELLED: "✕",
  /* live runtime + live presence states (Step 8) */
  IDLE: "○",
  STARTING: "◑",
  RUNNING: "●",
  STOPPING: "◑",
  FAILED: "✕",
  RECOGNIZED: "●",
  TEMPORARILY_LOST: "◑",
  SEEN_EARLIER: "○",
  /* face enrollment */
  ENROLLED: "✓",
  NOT_ENROLLED: "○",
  /* liveness states (Step 9) */
  LIVE: "●",
  SPOOF: "⚠",
  UNCERTAIN: "◑",
  /* authenticity states (Step 10) */
  AUTHENTIC: "◆",
  SUSPICIOUS: "⚠"
};

const STATUS_COLOR = {
  PRESENT: "var(--present)",
  LATE: "var(--late)",
  PARTIAL: "var(--partial)",
  LEFT_EARLY: "var(--early)",
  ABSENT: "var(--absent)",
  UNKNOWN: "var(--unknown)",
  MANUAL_OVERRIDE: "var(--unknown)"
};

const PAGE_SIZE = 25;

const state = {
  view: "overview",

  /* WHERE SOMEBODY HAS BEEN, and how to undo it.

     `viewStack` holds the views that were left to get to this one, most
     recent last, and Back pops it. `navSeq` counts how many history entries
     this application has pushed, so the browser's own Back button and the one
     in the top bar operate on the same stack instead of disagreeing - see
     pushNavStep and goBack. */
  viewStack: [],
  navSeq: 0,
  classId: "",
  date: "",
  sessionOffset: 0,
  studentOffset: 0,
  studentSearch: "",
  session: null,
  attendanceOffset: 0,
  attendanceStatus: "",
  attendanceSort: "student_id",
  attendanceDesc: false,
  /* Which of the four readings of the register is on screen. */
  reportMode: "records",
  record: null,
  liveTimer: null,
  enrollTimer: null,
  cameraTimer: null,
  liveSession: "",
  liveSessionList: [],
  /* The running sessions, and which one is enlarged. The focused session is
     identified by its id, so it survives a refresh as long as the backend
     still reports that session running - see renderLiveFocus. */
  liveActive: [],
  liveFocus: null,
  liveUpdatedAt: null,
  liveRunningSession: null,
  liveBusy: false,

  /* sign-in and the registry page */
  auth: { configured: false, authenticated: false, user: null,
          is_super_admin: false, unrestricted: true },
  authMode: "login",
  /* "staff" or "student" - which credential table the sign-in form is aimed
     at. Staff by default: it is the door most people using this are coming
     through, and a student is told which button to press. */
  authAudience: "staff",
  studentYear: "",
  studentClass: "",
  studentSection: "",
  /* The unfiltered registry count, fetched once and then remembered.

     null means "not asked yet", which is different from 0 - a school with no
     students has a real count of zero and must not be re-asked on every
     keystroke. Reset when a student is added or removed, because that is the
     only thing that changes it. */
  studentTotal: null,
  studentStatus: "",
  studentEnrolled: "",
  editing: null,
  removing: null,
  removingSession: null,
  removingBusy: false,
  editingUser: null,
  userAssignments: [],
  enrollAfterSave: false
};

/* ---------- helpers ---------- */

const $ = (id) => document.getElementById(id);

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else node.setAttribute(key, value);
    }
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/* A 401 means the session ended - expired, signed out in another tab, or the
   account was deactivated while this page was open. Showing the login screen
   is the only useful response; showing "Request failed (401)" thirty times is
   not. */
function handleUnauthorized() {
  state.auth = Object.assign({}, state.auth,
                             { authenticated: false, user: null });
  stopLivePolling();
  stopEnrollPolling();
  stopCameraPolling();
  showAuthScreen("Your session has ended. Sign in again.");
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (response.status === 401) {
    handleUnauthorized();
    throw new Error("Signed out.");
  }
  if (!response.ok) {
    let detail = "Request failed (" + response.status + ")";
    try { detail = (await response.json()).detail || detail; } catch (e) { /* keep default */ }
    const failure = new Error(detail);
    failure.status = response.status;
    throw failure;
  }
  return response.json();
}

async function apiSend(path, method, body) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body ? JSON.stringify(body) : undefined
  });
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    handleUnauthorized();
    throw new Error("Signed out.");
  }
  if (!response.ok) {
    let detail = "Request failed (" + response.status + ")";
    try { detail = (await response.json()).detail || detail; } catch (e) { /* keep default */ }
    const failure = new Error(detail);
    failure.status = response.status;
    throw failure;
  }
  /* A write to the registry changes how many students there are, so the
     remembered unfiltered count stops being true.

     Invalidated HERE, in the one funnel every non-GET request passes
     through, rather than in each handler that adds, removes, activates or
     deactivates a student - a cache that has to be remembered in nine places
     is a cache that goes stale in one of them. The next count asks again. */
  if (path.startsWith("/api/students")) state.studentTotal = null;

  return response.json();
}

/* "No data" and "zero" are different facts and must look different. */
/* =========================================================================
   COLOUR THEME

   Three states: follow the machine, always light, always dark. The choice is
   remembered in localStorage and applied as data-theme on <html>, which is
   what the two attribute-scoped palettes in styles.css key off.

   "Auto" REMOVES the attribute rather than resolving it to a value. That is
   the whole reason the media queries in the stylesheet are still there and
   still doing the work: a machine that switches to dark at sunset follows,
   with no listener and no re-render, and the page behaves correctly with this
   script disabled entirely.

   Nothing here is persisted on the server. A theme is a property of the
   machine somebody is sitting at, not of their account - the same teacher on
   the projector laptop and on their own screen wants different answers.
   ========================================================================= */

const THEME_KEY = "classvision-theme";
const THEMES = ["auto", "light", "dark"];

function storedTheme() {
  /* localStorage throws in a private window on some browsers rather than
     simply being empty, so every access is guarded. A theme nobody can save is
     a smaller problem than a dashboard that will not load. */
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    return THEMES.indexOf(saved) >= 0 ? saved : "auto";
  } catch (failure) {
    return "auto";
  }
}

/* The id prefixes of the two appearance switches. A switch that is not in the
   document is simply skipped, so the student portal - which has neither -
   costs nothing. */
const THEME_SWITCHES = ["theme-", "gate-theme-"];

function applyTheme(theme) {
  const chosen = THEMES.indexOf(theme) >= 0 ? theme : "auto";

  if (chosen === "auto") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", chosen);
  }

  try {
    window.localStorage.setItem(THEME_KEY, chosen);
  } catch (failure) {
    /* The theme still applies for this visit; it just will not be remembered. */
  }

  state.theme = chosen;

  /* Both switches. There are two - one in the rail and one on the sign-in
     card, because the rail is inside .app-shell and body.locked hides it, so
     the first screen anybody sees had no way to choose day or night. They are
     one choice with two controls, so both are told, and aria-checked is what
     is written: the appearance and the accessibility tree cannot disagree. */
  for (const prefix of THEME_SWITCHES) {
    for (const name of THEMES) {
      const button = $(prefix + name);
      if (button) button.setAttribute("aria-checked", String(name === chosen));
    }
  }

  /* The sign-in scene paints on a canvas and reads its colours once, so it has
     to be told. It is only running while the sign-in screen is up. */
  if (typeof repaintAuthScene === "function") repaintAuthScene();

  /* And the spatial layer's canvases, for the same reason. Only matters under
     prefers-reduced-motion, where each scene is one still frame: a running
     loop re-reads the palette every frame and picks a theme change up on its
     own. */
  if (window.Spatial) window.Spatial.repaint();
}

function bindTheme() {
  for (const prefix of THEME_SWITCHES) {
    for (const name of THEMES) {
      const button = $(prefix + name);
      if (button) button.addEventListener("click", () => applyTheme(name));
    }
  }
  applyTheme(storedTheme());
}

function percent(value) {
  return value === null || value === undefined ? null : (value * 100).toFixed(1) + "%";
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes >= 60) {
    return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
  }
  return minutes > 0 ? minutes + "m " + rest + "s" : rest + "s";
}

function clock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (isNaN(date)) return iso;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function statusPill(status) {
  if (!status) return el("span", { class: "muted", text: "—" });
  return el("span", { class: "pill pill-" + status, title: status }, [
    el("span", { class: "sym", "aria-hidden": "true", text: STATUS_SYMBOL[status] || "•" }),
    status.replace(/_/g, " ")
  ]);
}

function card(label, value, sub) {
  const isMissing = value === null || value === undefined;
  return el("li", { class: "card" }, [
    el("div", { class: "label", text: label }),
    el("div", {
      class: "value" + (isMissing ? " nodata" : ""),
      text: isMissing ? "No data" : String(value)
    }),
    sub ? el("div", { class: "sub", text: sub }) : null
  ]);
}

function banner(message, warn) {
  const node = $("banner");
  node.textContent = message || "";
  node.hidden = !message;
  node.classList.toggle("warn", Boolean(warn));
}

function setBusy(on) {
  document.body.setAttribute("aria-busy", on ? "true" : "false");
}

/* ---------- charts (hand-drawn, no library) ---------- */

function barChart(container, rows, total) {
  container.textContent = "";
  if (!total) {
    container.appendChild(el("p", { class: "muted small", text: "No records to chart." }));
    return;
  }
  for (const row of rows) {
    if (!row.count) continue;
    const share = row.count / total;
    container.appendChild(el("div", { class: "bar-row" }, [
      statusPill(row.status),
      el("div", { class: "bar-track" }, [
        el("div", {
          class: "bar-fill",
          style: "width:" + (share * 100).toFixed(1) + "%;background:" +
                 (STATUS_COLOR[row.status] || "var(--unknown)")
        })
      ]),
      el("div", { class: "bar-value", text: row.count + " (" + (share * 100).toFixed(0) + "%)" })
    ]));
  }
}

function historyChart(container, history) {
  container.textContent = "";

  const points = history.filter((entry) => entry.presence_ratio !== null &&
                                           entry.presence_ratio !== undefined);
  if (points.length < 2) {
    container.appendChild(el("p", { class: "muted small", text:
      points.length === 0
        ? "No evaluated sessions yet, so there is nothing to chart."
        : "Only one evaluated session — not enough for a trend." }));
    return;
  }

  const width = 640, height = 160, padLeft = 38, padBottom = 26, padTop = 12;
  const innerW = width - padLeft - 12, innerH = height - padTop - padBottom;
  const step = points.length > 1 ? innerW / (points.length - 1) : innerW;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    "Presence ratio across " + points.length + " sessions, from " +
    percent(points[0].presence_ratio) + " to " +
    percent(points[points.length - 1].presence_ratio));

  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };

  [0, 0.5, 1].forEach((fraction) => {
    const y = padTop + innerH - fraction * innerH;
    svg.appendChild(make("line", {
      x1: padLeft, y1: y, x2: width - 12, y2: y,
      stroke: "currentColor", "stroke-opacity": ".15"
    }));
    const label = make("text", {
      x: padLeft - 6, y: y + 4, "text-anchor": "end",
      "font-size": "10", fill: "currentColor", "fill-opacity": ".6"
    });
    label.textContent = (fraction * 100) + "%";
    svg.appendChild(label);
  });

  const coords = points.map((entry, index) => [
    padLeft + index * step,
    padTop + innerH - entry.presence_ratio * innerH
  ]);

  svg.appendChild(make("polyline", {
    points: coords.map((c) => c.join(",")).join(" "),
    fill: "none", stroke: "var(--accent)", "stroke-width": "2"
  }));

  coords.forEach((coord, index) => {
    const status = points[index].status;
    svg.appendChild(make("circle", {
      cx: coord[0], cy: coord[1], r: "4",
      fill: STATUS_COLOR[status] || "var(--accent)"
    }));
  });

  container.appendChild(svg);

  // A textual equivalent, so the chart is not the only way to read the data.
  const summary = points.map((entry) =>
    entry.session_date + ": " + percent(entry.presence_ratio) + " (" + entry.status + ")").join("; ");
  container.appendChild(el("p", { class: "muted small", text: "Values — " + summary }));
}

function renderTimeline(container, data) {
  container.textContent = "";

  if (!data.students.length) {
    container.appendChild(el("p", { class: "muted small", text:
      "No verified presence segments were recorded for this session." }));
    return;
  }

  const start = new Date(data.session_start).getTime();
  const end = new Date(data.session_end).getTime();
  const span = end - start;

  if (!span || isNaN(span)) {
    container.appendChild(el("p", { class: "muted small", text:
      "Session window unavailable, so a timeline cannot be drawn." }));
    return;
  }

  for (const student of data.students) {
    const track = el("div", { class: "timeline-track" });

    for (const segment of student.segments) {
      const left = ((new Date(segment.start_time).getTime() - start) / span) * 100;
      const width = ((new Date(segment.end_time).getTime() -
                      new Date(segment.start_time).getTime()) / span) * 100;
      track.appendChild(el("div", {
        class: "timeline-seg",
        style: "left:" + Math.max(0, left).toFixed(2) + "%;width:" +
               Math.max(0.4, width).toFixed(2) + "%",
        title: clock(segment.start_time) + " → " + clock(segment.end_time) +
               " (" + duration(segment.duration_seconds) + ")" +
               (segment.track_id !== null ? ", track " + segment.track_id : "")
      }));
    }

    const segmentText = student.segments.map((segment) =>
      clock(segment.start_time) + "–" + clock(segment.end_time)).join(", ");

    container.appendChild(el("div", { class: "timeline-row" }, [
      el("div", { class: "timeline-label" }, [
        el("strong", { text: student.student_id }),
        el("span", { class: "muted", text:
          duration(student.total_seconds) + " over " + student.segment_count +
          (student.segment_count === 1 ? " segment" : " segments") })
      ]),
      track,
      el("div", { class: "timeline-axis" }, [
        el("span", { text: clock(data.session_start) }),
        el("span", { text: clock(data.session_end) })
      ]),
      el("p", { class: "muted small", text: "Segments: " + segmentText })
    ]));
  }
}

/* ---------- overview ---------- */

/* =========================================================================
   TODAY, AND THE QUICK ACTIONS BESIDE IT

   One request, /api/analytics/today, already narrowed on the server to the
   signed-in account: a teacher's own classes and own students, a Super
   Admin's school. Nothing here re-derives that scope, and nothing here
   invents a figure - an attendance rate is shown only once a session has
   actually completed, because attendance is what the rule engine produces
   after a session is finalised and a lesson still running has none.
   ========================================================================= */

async function loadToday() {
  let today;
  try {
    today = await api("/api/analytics/today");
  } catch (failure) {
    /* The rest of the overview is still useful, so this panel says what
       happened instead of taking the page down with it. */
    $("today-scope").textContent = failure.message;
    $("today-cards").replaceChildren();
    $("today-sessions").replaceChildren();
    return;
  }

  state.today = today;

  $("today-date").textContent = today.session_date;

  const scope = today.scope || {};
  const assignments = ((scope.user || {}).assignments) || [];
  $("today-scope").textContent = scope.unrestricted
    ? "Every class in this installation."
    : "Your assigned classes: " +
      assignments.map(describeAssignment).join(", ") +
      (scope.may_view_all_students
        ? ". You may also view every student's record."
        : ".");

  const counts = today.session_counts;
  const attendance = today.attendance;

  $("today-cards").replaceChildren(
    card("Sessions today", counts.total,
         counts.scheduled + " scheduled · " + counts.active + " running · " +
         counts.completed + " completed"),
    card("Students in scope", today.students.active,
         today.students.total + " on the register"),
    /* "No data" rather than 0%: a morning with nothing finalised yet has no
       attendance rate, and printing 0% would read as a disaster. */
    card("Attendance today", attendance.has_data
           ? percent(attendance.attendance_rate) : null,
         attendance.has_data
           ? "over " + attendance.expected_students + " expectations in " +
             attendance.completed_sessions + " completed session(s)"
           : "no session has been finalised today"),
    card("Present today", attendance.has_data
           ? attendance.counts.PRESENT : null),
    card("Absent today", attendance.has_data
           ? attendance.counts.ABSENT : null)
  );

  renderTodayLive(today);
  renderTodaySessions(today);

  /* The stage, from the payload that is already here. */
  spatialToday(today);
}

/* What is running right now, read from the live runtime rather than from the
   sessions table, so this agrees with the Live classroom tab. Absent entirely
   when nothing is running - never the next scheduled lesson presented as a
   current one. */
function renderTodayLive(today) {
  const container = $("today-live");
  const live = today.live;

  if (!live) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }

  /* The button is held as a local rather than given an id and fetched back
     with $(). Every id $() reaches for has to exist in index.html - a test
     asserts exactly that - and an id that only exists once this panel has
     rendered would break it. */
  const open = el("button", { class: "btn btn-quiet", type: "button",
                              text: "Open live classroom" });
  open.addEventListener("click", () => switchView("live"));

  container.hidden = false;
  container.replaceChildren(
    el("div", { class: "today-live" }, [
      el("span", { class: "live-dot", "aria-hidden": "true" }),
      el("span", {}, [
        el("strong", { text: "Live now: " + live.class_id }),
        el("span", { class: "muted small", text:
          " " + live.session_id + " · " + (live.state || "unknown") +
          (live.fps === null || live.fps === undefined
            ? "" : " · " + Number(live.fps).toFixed(1) + " fps") })
      ]),
      open
    ]));
}

function renderTodaySessions(today) {
  const container = $("today-sessions");
  const sessions = today.sessions || [];

  if (!sessions.length) {
    container.replaceChildren(el("p", { class: "muted", text:
      "No session is scheduled for " + today.session_date +
      " in your classes. Use Create Session to add one." }));
    return;
  }

  container.replaceChildren(
    el("div", { class: "table-scroll" }, [
      el("table", { class: "data-table" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "Class" }),
            el("th", { scope: "col", text: "Start" }),
            el("th", { scope: "col", text: "End" }),
            el("th", { scope: "col", text: "Status" }),
            el("th", { scope: "col", class: "num", text: "Expected" }),
            el("th", { scope: "col", text: "" })
          ])
        ]),
        el("tbody", {}, sessions.map((session) => el("tr", {}, [
          el("td", { text: session.class_id }),
          /* timeOfDay renders the stored UTC instant in the browser's own
             timezone, which is the only reading of "09:00" a teacher has. */
          el("td", { text: timeOfDay(session.start_time) || "—" }),
          el("td", { text: timeOfDay(session.end_time) || "—" }),
          el("td", {}, [statusPill(session.status)]),
          el("td", { class: "num",
                     text: String(session.expected_students ?? "—") }),
          el("td", {}, [
            el("button", { class: "btn-link", type: "button", text: "Open",
                           "data-today-session": session.session_id })
          ])
        ])))
      ])
    ]));

  container.querySelectorAll("[data-today-session]").forEach((button) => {
    button.addEventListener("click", () => {
      switchView("sessions");
      openSession(button.getAttribute("data-today-session"));
    });
  });
}

/* =========================================================================
   ATTENDANCE REPORTS

   Filters into a query string, rows into a table, and the same query string
   onto a CSV link. The export and the table are built from the SAME filters
   by construction - reportQuery() is called by both - so an exported file
   cannot contain rows the page never showed.

   Nothing is computed here. Every figure under the table comes from the
   server's own summary over exactly the rows it returned.
   ========================================================================= */

function reportError(message) {
  const node = $("report-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function reportQuery() {
  const query = new URLSearchParams();
  const add = (key, value) => { if (value) query.set(key, value); };

  add("student_id", $("report-student").value.trim());
  add("class_id", $("report-class").value.trim());
  add("section", $("report-section").value.trim());
  add("date_from", $("report-from").value);
  add("date_to", $("report-to").value);
  add("status", $("report-status").value);

  if ($("report-unevaluated").checked) query.set("include_unevaluated", "true");

  return query;
}

async function loadReport() {
  reportError("");

  let report;
  try {
    report = await api("/api/reports/attendance?" + reportQuery().toString());
  } catch (failure) {
    reportError(failure.message);
    return;
  }

  const rows = report.items || [];

  $("report-empty").hidden = rows.length > 0;
  $("report-table-wrap").hidden = rows.length === 0;

  renderReportSummary(report);

  const body = $("report-table").querySelector("tbody");
  body.replaceChildren(...rows.map((row) => el("tr", {}, [
    el("td", { text: row.session_date || "—" }),
    el("td", { text: row.class_id || "—" }),
    el("td", { text: row.student_id }),
    el("td", { text: row.student_name || "—" }),
    el("td", { text: row.section || "—" }),
    /* A never-evaluated row says so, in words. It is NOT drawn as absent:
       nobody decided it was. */
    el("td", {}, [row.status
      ? statusPill(row.status)
      : el("span", { class: "muted", text: "not evaluated" })]),
    el("td", { class: "num", text: percent(row.presence_ratio) ?? "—" }),
    el("td", { class: "num", text: row.present_seconds === null
      ? "—" : duration(row.present_seconds) }),
    el("td", { text: row.is_override
      ? "yes (was " + (row.original_status || "?") + ")" : "no" }),
    el("td", { text: row.policy_version || "—" })
  ])));
}

function renderReportSummary(report) {
  const summary = report.summary || {};
  const container = $("report-summary");

  if (!summary.rows) {
    container.replaceChildren();
    return;
  }

  const cards = [
    card("Records", summary.rows),
    card("Students", summary.students),
    card("Sessions", summary.sessions),
    card("Attendance rate", percent(summary.attendance_rate),
         summary.satisfying_count + " of " + summary.rows + " satisfying")
  ];

  for (const status of ["PRESENT", "LATE", "PARTIAL", "LEFT_EARLY",
                        "ABSENT"]) {
    if (summary.counts[status]) cards.push(card(status, summary.counts[status]));
  }

  if (summary.unevaluated) {
    cards.push(card("Not evaluated", summary.unevaluated,
                    "expected, never decided"));
  }

  const children = [el("ul", { class: "cards small" }, cards)];

  /* A truncated report says so. A silently short export is worse than a
     refusal, because nobody notices it. */
  if (report.truncated) {
    children.push(el("p", { class: "form-error", text:
      "Showing the first " + report.returned + " of " + report.total +
      " matching records. Narrow the filters to see the rest — the CSV " +
      "export is limited to the same number." }));
  }

  container.replaceChildren(...children);

  /* The same distribution, standing in a glass box beside the table. */
  spatialReportCube({ counts: summary.counts });
}

/* The export.

   A real navigation rather than fetch-and-blob, so the browser's own download
   handling applies: the server sends Content-Disposition with a filename built
   from the filters, and the file never passes through JavaScript memory. */
function exportReport() {
  reportError("");
  window.location.href = "/api/reports/attendance.csv?" +
                         reportQuery().toString();
}

function clearReportFilters() {
  $("report-student").value = "";
  $("report-class").value = "";
  $("report-section").value = "";
  $("report-from").value = "";
  $("report-to").value = "";
  $("report-status").value = "";
  $("report-unevaluated").checked = false;
  loadReport();
}

/* Class suggestions come from the classes that actually have sessions, already
   narrowed by the server to the ones this account may see. */
async function fillReportClasses() {
  try {
    const classes = await api("/api/classes");
    $("report-classes").replaceChildren(...classes.map((item) =>
      el("option", { value: item.class_id })));
  } catch (failure) {
    /* free-text field; a missing suggestion list costs nothing */
  }
}

/* =========================================================================
   FOUR READINGS OF THE SAME STORED DECISIONS (Step 11)

   Records is the register. "By class" and "By term" are the analytics
   service's own figures, fetched as summaries rather than recalculated here -
   the same constraint the backend holds itself to, extended to the page: this
   file does not add, divide or average anything a teacher could then fail to
   reproduce from the Overview tab. Seat map is one session on one sheet.

   Only the pane somebody is looking at is fetched. Switching to "By term"
   does not quietly run three more reports.
   ========================================================================= */

const REPORT_MODES = ["records", "classes", "terms", "seatmap"];

function switchReportMode(mode) {
  if (!REPORT_MODES.includes(mode)) mode = "records";
  state.reportMode = mode;

  for (const each of REPORT_MODES) {
    const tab = $("report-mode-" + each);
    const pane = $("report-pane-" + each);
    if (tab) tab.setAttribute("aria-selected", each === mode ? "true" : "false");
    if (pane) pane.hidden = each !== mode;
  }

  loadReportMode();
}

/* The one loader refresh() and the mode switch both go through, so there is a
   single answer to "what does the Reports tab show". */
async function loadReportMode() {
  if (state.reportMode === "classes") return loadClassSummary();

  if (state.reportMode === "terms") {
    await fillTermOptions();
    return loadTermSummary();
  }

  if (state.reportMode === "seatmap") {
    await fillSeatmapSessions();
    return loadSeatMap();
  }

  return loadReport();
}

/* ---------- by class ---------- */

function classSummaryQuery() {
  const query = new URLSearchParams();
  const add = (key, value) => { if (value) query.set(key, value); };

  add("class_id", $("class-summary-class").value.trim());
  add("date_from", $("class-summary-from").value);
  add("date_to", $("class-summary-to").value);

  return query;
}

async function loadClassSummary() {
  const error = $("class-summary-error");
  error.hidden = true;

  let report;
  try {
    report = await api("/api/reports/classes?" +
                       classSummaryQuery().toString());
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    return;
  }

  const rows = report.items || [];

  $("class-summary-empty").hidden = rows.length > 0;
  $("class-summary-wrap").hidden = rows.length === 0;
  $("class-summary-note").textContent = report.note || "";

  renderClassSummaryTotals(report.totals, rows.length);

  const body = $("class-summary-table").querySelector("tbody");
  body.replaceChildren(...rows.map((row) => el("tr", {}, [
    el("td", { text: row.class_id || "—" }),
    el("td", { class: "num", text: String(row.completed_sessions ?? 0) }),
    el("td", { class: "num", text: String(row.student_count ?? 0) }),
    el("td", { class: "num", text: String(row.expected_students ?? 0) }),
    /* percent() returns null for null, and the dash means "nothing to divide
       by" - never 0%, which would read as everybody absent. */
    el("td", { class: "num", text: percent(row.attendance_rate) ?? "—" }),
    el("td", { class: "num",
               text: percent(row.average_presence_ratio) ?? "—" }),
    el("td", { class: "num", text: percent(row.late_rate) ?? "—" }),
    el("td", { class: "num", text: percent(row.partial_rate) ?? "—" }),
    el("td", { class: "num", text: percent(row.early_leave_rate) ?? "—" }),
    el("td", { class: "num", text: percent(row.absence_rate) ?? "—" }),
    el("td", { text: row.first_session || "—" }),
    el("td", { text: row.last_session || "—" })
  ])));
}

function renderClassSummaryTotals(totals, shown) {
  const container = $("class-summary-totals");

  if (!totals || !shown) {
    container.replaceChildren();
    return;
  }

  container.replaceChildren(el("ul", { class: "cards small" }, [
    card("Classes", totals.classes),
    card("Sessions", totals.completed_sessions),
    card("Expected", totals.expected_students),
    /* The server weights this by expected students. A mean of the per-class
       rates would weight a class of 4 like a class of 40. */
    card("Attendance rate", percent(totals.attendance_rate) ?? "—",
         "weighted by expected students")
  ]));
}

function clearClassSummary() {
  $("class-summary-class").value = "";
  $("class-summary-from").value = "";
  $("class-summary-to").value = "";
  loadClassSummary();
}

/* ---------- by term ---------- */

/* The dropdown lists the terms that exist, not the terms that have sessions:
   a term with no finalised lesson is a real choice, and choosing it must be
   able to answer "nothing yet" rather than hiding the term. */
async function fillTermOptions() {
  const select = $("term-summary-term");
  const chosen = select.value;

  try {
    const data = await api("/api/academic/sessions");
    const options = [el("option", { value: "", text: "All terms" })];

    for (const term of data.items || []) {
      const label = ((term.academic_year || "") + " " +
                     (term.semester || "")).trim() ||
                    term.academic_session_id;
      options.push(el("option", {
        value: term.academic_session_id,
        text: label + (term.status === "ACTIVE" ? " (current)" : "")
      }));
    }

    select.replaceChildren(...options);
    select.value = chosen;
  } catch (failure) {
    /* "All terms" is the default and still works without the list */
  }
}

async function loadTermSummary() {
  const error = $("term-summary-error");
  error.hidden = true;

  const query = new URLSearchParams();
  const term = $("term-summary-term").value;
  if (term) query.set("academic_session_id", term);

  let report;
  try {
    report = await api("/api/reports/terms?" + query.toString());
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    return;
  }

  const rows = report.items || [];

  $("term-summary-empty").hidden = rows.length > 0;
  $("term-summary-note").textContent = report.note || "";

  $("term-summary-list").replaceChildren(...rows.map(termCard));
}

function termCard(row) {
  const figures = [
    ["Attendance", percent(row.attendance_rate) ?? "no data"],
    ["Sessions", String(row.completed_sessions ?? 0)],
    ["Expected", String(row.expected_students ?? 0)],
    ["Evaluated", String(row.evaluated_students ?? 0)]
  ];

  if (row.first_session || row.last_session) {
    figures.push(["Dates", (row.first_session || "?") + " to " +
                           (row.last_session || "?")]);
  }

  const summary = el("summary", {}, [
    el("h3", { text: row.label || row.academic_session_id || "Unassigned" }),
    el("div", { class: "term-figures" }, figures.map(([label, value]) =>
      el("span", { text: label + ": " + value })))
  ]);

  /* A lesson that belongs to no term is a fact about the data, and the card
     says why rather than looking like a term with a strange name. */
  const children = [summary];

  if (row.unassigned) {
    children.push(el("p", { class: "muted small", text:
      "These lessons were recorded before academic terms existed. They " +
      "belong to no term and are not folded into one." }));
  } else if (row.term_status) {
    children.push(el("p", { class: "muted small", text:
      "Term status: " + row.term_status +
      (row.term_start ? ", " + row.term_start +
                        " to " + (row.term_end || "?") : "") }));
  }

  children.push(el("div", { class: "table-scroll" }, [
    el("table", { class: "mini-table" }, [
      el("caption", { class: "sr-only", text: "Classes in this term" }),
      el("thead", {}, [el("tr", {}, [
        el("th", { scope: "col", text: "Class" }),
        el("th", { scope: "col", text: "Sessions" }),
        el("th", { scope: "col", text: "Expected" }),
        el("th", { scope: "col", text: "Attendance" })
      ])]),
      el("tbody", {}, (row.classes || []).map((each) => el("tr", {}, [
        el("td", { text: each.class_id || "—" }),
        el("td", { class: "num", text: String(each.completed_sessions ?? 0) }),
        el("td", { class: "num", text: String(each.expected_students ?? 0) }),
        el("td", { class: "num",
                   text: percent(each.attendance_rate) ?? "—" })
      ])))
    ])
  ]));

  return el("details", { class: "panel term-card" }, children);
}

/* ---------- seat map ---------- */

/* Only finalised sessions. A session still running has no decided attendance,
   and a grid of blanks is not a register. */
async function fillSeatmapSessions() {
  const select = $("seatmap-session");
  const chosen = select.value;

  try {
    const data = await api("/api/sessions?status=COMPLETED&limit=100");
    const options = [el("option", { value: "",
                                    text: "Choose a finalised session" })];

    for (const session of data.items || []) {
      options.push(el("option", {
        value: session.session_id,
        text: session.session_date + "  " + session.class_id +
              (session.section ? " " + session.section : "")
      }));
    }

    select.replaceChildren(...options);
    select.value = chosen;
  } catch (failure) {
    /* the field stays as it was; the error surfaces when a grid is asked for */
  }
}

function seatmapColumns() {
  const raw = parseInt($("seatmap-columns").value, 10);
  if (!Number.isFinite(raw)) return 6;
  return Math.max(1, Math.min(raw, 12));
}

async function loadSeatMap() {
  const error = $("seatmap-error");
  error.hidden = true;

  const sessionId = $("seatmap-session").value;
  const grid = $("seatmap-grid");

  if (!sessionId) {
    $("seatmap-empty").hidden = false;
    grid.replaceChildren();
    $("seatmap-meta").replaceChildren();
    $("seatmap-note").textContent = "";
    return;
  }

  let report;
  try {
    report = await api("/api/reports/sessions/" +
                       encodeURIComponent(sessionId) + "/seatmap?columns=" +
                       seatmapColumns());
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    $("seatmap-empty").hidden = false;
    grid.replaceChildren();
    $("seatmap-meta").replaceChildren();
    return;
  }

  $("seatmap-empty").hidden = true;
  $("seatmap-note").textContent = report.note || "";

  $("seatmap-meta").replaceChildren(
    metaItem("Class", report.class_id || "—"),
    metaItem("Date", report.session_date || "—"),
    metaItem("Section", report.section || "—"),
    metaItem("Teacher", report.teacher || "—"),
    metaItem("Students", String(report.seats ?? 0)),
    /* Said in words, on the page, next to the grid - not only in the note. */
    metaItem("Order", report.ordering === "roll_number_then_name"
      ? "roll number, then name" : (report.ordering || "—")));

  grid.style.setProperty("--seat-columns", String(report.columns || 6));

  const cells = [];
  for (const row of report.grid || []) {
    for (const seat of row) cells.push(seatCell(seat));
  }
  grid.replaceChildren(...cells);
}

function seatCell(seat) {
  return el("div", { class: "seat seat-" + (seat.status || "NONE") }, [
    el("span", { class: "seat-roll",
                 text: seat.roll_number ? "Roll " + seat.roll_number
                                        : "No roll number" }),
    el("span", { class: "seat-name", text: seat.student_name || "—" }),
    el("span", { class: "seat-id", text: seat.student_id }),
    /* Expected and never decided is NOT absent, here as everywhere else. */
    seat.status ? statusPill(seat.status)
                : el("span", { class: "muted small", text: "not evaluated" })
  ]);
}

/* ---------- exports ---------- */

/*
  Every export is a real navigation rather than fetch-and-blob, so the
  browser's own download handling applies and the bytes never pass through
  JavaScript memory. Each one carries the SAME query as the pane it sits
  under, which is what makes "the file is what I was looking at" true by
  construction rather than by hope.
*/
function download(path, query) {
  const text = query ? query.toString() : "";
  window.location.href = path + (text ? "?" + text : "");
}

function exportReportPdf() {
  reportError("");
  download("/api/reports/attendance.pdf", reportQuery());
}

function exportClassSummaryPdf() {
  download("/api/reports/classes.pdf", classSummaryQuery());
}

function exportTermSummaryPdf() {
  const query = new URLSearchParams();
  const term = $("term-summary-term").value;
  if (term) query.set("academic_session_id", term);
  download("/api/reports/terms.pdf", query);
}

function exportSeatMapPdf() {
  const error = $("seatmap-error");
  const sessionId = $("seatmap-session").value;

  if (!sessionId) {
    error.textContent = "Choose a session first.";
    error.hidden = false;
    return;
  }

  error.hidden = true;
  download("/api/reports/sessions/" + encodeURIComponent(sessionId) +
           "/seatmap.pdf", new URLSearchParams({
             columns: String(seatmapColumns())
           }));
}

function bindReports() {
  $("report-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadReport();
  });
  $("report-clear").addEventListener("click", clearReportFilters);
  $("report-export").addEventListener("click", exportReport);
  $("report-export-pdf").addEventListener("click", exportReportPdf);

  for (const mode of REPORT_MODES) {
    const tab = $("report-mode-" + mode);
    if (tab) tab.addEventListener("click", () => switchReportMode(mode));
  }

  $("class-summary-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadClassSummary();
  });
  $("class-summary-clear").addEventListener("click", clearClassSummary);
  $("class-summary-pdf").addEventListener("click", exportClassSummaryPdf);

  $("term-summary-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadTermSummary();
  });
  $("term-summary-pdf").addEventListener("click", exportTermSummaryPdf);

  $("seatmap-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadSeatMap();
  });
  $("seatmap-pdf").addEventListener("click", exportSeatMapPdf);
}

async function loadOverview() {
  const query = new URLSearchParams();
  if (state.classId) query.set("class_id", state.classId);
  if (state.date) query.set("session_date", state.date);

  const overview = await api("/api/analytics/overview?" + query.toString());

  $("overview-empty").hidden = overview.has_data;
  $("overview-body").hidden = !overview.has_data;

  $("overview-scope").textContent = overview.has_data
    ? "Based on " + overview.completed_sessions + " completed session" +
      (overview.completed_sessions === 1 ? "" : "s") +
      (state.classId ? " in " + state.classId : "") +
      (state.date ? " on " + state.date : "") +
      ". Scheduled, active and cancelled sessions are excluded."
    : "";

  if (!overview.has_data) return;

  if (overview.has_multiple_policy_versions) {
    banner("This dataset contains more than one attendance policy version (" +
           overview.policy_versions.join(", ") +
           "). Records are shown under the policy that produced them.", true);
  } else {
    banner("");
  }

  const counts = overview.counts;
  $("summary-cards").replaceChildren(
    card("Completed sessions", overview.completed_sessions,
         overview.total_sessions + " total in database"),
    card("Students in scope", overview.students_in_scope,
         overview.total_students + " enrolled"),
    card("Attendance rate", percent(overview.attendance_rate),
         overview.sample_size ? "over " + overview.sample_size + " expectations" : "no expectations recorded"),
    card("Present", counts.PRESENT),
    card("Late", counts.LATE),
    card("Partial", counts.PARTIAL),
    card("Absent", counts.ABSENT)
  );

  const rows = ["PRESENT", "LATE", "PARTIAL", "LEFT_EARLY", "ABSENT", "UNKNOWN", "MANUAL_OVERRIDE"]
    .map((status) => ({ status: status, count: counts[status] || 0 }));
  const total = rows.reduce((sum, row) => sum + row.count, 0);

  barChart($("status-chart"), rows, total);

  const body = $("status-table").querySelector("tbody");
  body.replaceChildren(...rows.filter((row) => row.count > 0).map((row) =>
    el("tr", {}, [
      el("td", {}, [statusPill(row.status)]),
      el("td", { class: "num", text: String(row.count) }),
      el("td", { class: "num", text: total ? ((row.count / total) * 100).toFixed(1) + "%" : "—" })
    ])));

  await loadAttention();
}

async function loadAttention() {
  const query = new URLSearchParams();
  if (state.classId) query.set("class_id", state.classId);

  const data = await api("/api/analytics/attention?" + query.toString());
  const rule = data.rule;

  $("attention-rule").textContent =
    "Listed when, over at least " + rule.minimum_sessions +
    " completed sessions: attendance below " + (rule.attendance_rate_below * 100) +
    "%, or late in over " + (rule.late_rate_above * 100) +
    "%, or absent in over " + (rule.absence_rate_above * 100) +
    "%. Fixed thresholds — not anomaly detection, no model involved.";

  const container = $("attention-body");
  container.textContent = "";

  if (!data.indicators.length) {
    container.appendChild(el("p", { class: "muted small", text:
      "No student meets these thresholds in the current data." }));
    return;
  }

  for (const item of data.indicators) {
    container.appendChild(el("div", { class: "bar-row", style: "grid-template-columns:1fr" }, [
      el("div", {}, [
        el("button", {
          class: "btn-link", type: "button", "data-student": item.student_id,
          text: item.student_id + " — " + item.name
        }),
        el("div", { class: "muted small", text:
          item.reasons.join("; ") + " (" + item.sessions_considered + " sessions)" })
      ])
    ]));
  }

  container.querySelectorAll("[data-student]").forEach((button) => {
    button.addEventListener("click", () => {
      switchView("students");
      openStudent(button.getAttribute("data-student"));
    });
  });
}

/* ---------- sessions ---------- */

/* =========================================================================
   THE CLASSES LIST, AND DELETING ONE

   A class is DERIVED. There is no classes table: GET /api/classes builds the
   list from the sessions that exist, so a class is alive for exactly as long
   as something refers to it.

   Deleting one therefore deletes its sessions, through the endpoint that
   already does that - DELETE /api/sessions/{id}, Super Admin, confirmation
   typed back, and 409 whenever anything references the session. Those
   protections are the point and none of them is bypassed: a class that
   produced attendance is refused, and the refusal is reported with a count of
   what was kept.
   ========================================================================= */

function classesError(message) {
  const node = $("classes-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function loadClassList() {
  classesError("");

  let classes;
  try {
    classes = await api("/api/classes");
  } catch (failure) {
    classesError(explainFailure(failure, "load classes"));
    return;
  }

  $("classes-empty").hidden = classes.length > 0;
  $("classes-wrap").hidden = classes.length === 0;

  const count = $("classes-count");
  if (count) {
    count.textContent = classes.length
      ? classes.length + (classes.length === 1 ? " class" : " classes")
      : "";
  }

  const body = $("classes-table").querySelector("tbody");
  body.replaceChildren(...classes.map((item) => {
    const open = el("button", { class: "btn-link", type: "button",
                                text: "Open" });
    open.addEventListener("click", () => openClassSessions(item.class_id));

    const remove = el("button", { class: "btn-link", type: "button",
                                  text: "Delete" });
    remove.addEventListener("click", () => deleteClass(item, remove));

    return el("tr", {}, [
      el("td", { text: item.class_id || "\u2014" }),
      el("td", { class: "num", text: String(item.session_count ?? 0) }),
      el("td", { class: "num", text: String(item.completed_sessions ?? 0) }),
      el("td", { text: item.first_session || "\u2014" }),
      el("td", { text: item.last_session || "\u2014" }),
      el("td", { class: "row-actions-cell" }, [
        el("div", { class: "row-actions" }, [open, remove])
      ])
    ]);
  }));
}

/* Open = show this class's sessions. The filter already exists and is what the
   rest of the page is driven by, so this sets it rather than inventing a second
   way to narrow the list. */
function openClassSessions(classId) {
  state.classId = classId;
  state.sessionOffset = 0;
  $("filter-class").value = classId;
  /* Classes is its own view now, so "Open" has somewhere to go: the session
     register, narrowed to this class by the filter that already existed.
     switchView refreshes, so this does not ask twice. */
  switchView("sessions");
}

/*
  Delete a class, by deleting the sessions that make it one.

  The confirmation says exactly that before anything is sent. "Delete class"
  sounds like it removes a class; what it actually removes is sessions, and a
  session that produced attendance is kept.
*/
async function deleteClass(item, button) {
  classesError("");

  const total = item.session_count ?? 0;
  const completed = item.completed_sessions ?? 0;

  const confirmed = window.confirm(
    "Delete " + item.class_id + "?\n\n"
    + "This class is not a record of its own - it exists because "
    + total + " session(s) refer to it. Deleting it deletes those sessions.\n\n"
    + (completed
       ? completed + " of them have been completed and hold attendance. Those "
         + "will be REFUSED and kept, because deleting one would delete its "
         + "register too.\n\n"
       : "None of them has produced attendance yet.\n\n")
    + "Continue?");

  if (!confirmed) return;

  let sessions;
  try {
    sessions = await api("/api/sessions?class_id="
                         + encodeURIComponent(item.class_id)
                         + "&limit=200&offset=0");
  } catch (failure) {
    classesError(explainFailure(failure, "list class sessions"));
    return;
  }

  const ids = (sessions.items || []).map((session) => session.session_id);

  await withBusyButton(button, { busy: "Deleting...", done: "Done" },
    async () => {
      const kept = [];
      let deleted = 0;

      for (const sessionId of ids) {
        try {
          /* The id typed back is the endpoint's own confirmation requirement.
             Sending it here is not a bypass - the dialog above is what asked
             the person, and the endpoint still refuses anything referenced. */
          await apiSend("/api/sessions/" + encodeURIComponent(sessionId)
                        + "?confirm=" + encodeURIComponent(sessionId),
                        "DELETE", null);
          deleted += 1;
        } catch (failure) {
          kept.push({ sessionId: sessionId, why: failure });
        }
      }

      if (kept.length) {
        /* The backend's own refusal, which names what would have been lost. */
        classesError(
          deleted + " session(s) deleted. " + kept.length + " kept: "
          + explainFailure(kept[0].why, "delete session"));
      } else if (deleted) {
        banner(item.class_id + ": " + deleted + " session(s) deleted.");
      } else {
        classesError("Nothing was deleted.");
      }

      await loadClassList();
      await loadSessions().catch(() => {});
      await loadClasses().catch(() => {});
      return deleted;
    },
    (failure) => classesError(explainFailure(failure, "delete class")));
}

async function loadSessions() {
  const query = new URLSearchParams({
    limit: String(PAGE_SIZE), offset: String(state.sessionOffset)
  });
  if (state.classId) query.set("class_id", state.classId);
  if (state.date) query.set("session_date", state.date);

  const data = await api("/api/sessions?" + query.toString());

  $("sessions-empty").hidden = data.total > 0;
  $("sessions-list-wrap").hidden = data.total === 0;

  const body = $("sessions-table").querySelector("tbody");
  body.replaceChildren(...data.items.map((session) =>
    el("tr", {}, [
      el("td", { text: session.session_date }),
      el("td", { text: session.class_id }),
      el("td", { text: clock(session.start_time) }),
      el("td", { text: clock(session.end_time) }),
      el("td", {}, [statusPill(session.status)]),
      el("td", { class: "num", text: String(session.expected_students ?? "—") }),
      el("td", { text: session.policy_version }),
      el("td", { class: "row-actions-cell" }, [
        el("div", { class: "row-actions" }, [
          el("button", { class: "btn-link", type: "button", text: "Open",
                         "data-session": session.session_id }),
          el("button", { class: "btn-link", type: "button", text: "Edit",
                         "data-edit-session": session.session_id }),
          el("button", { class: "btn-link", type: "button", text: "Remove",
                         "data-remove-session": session.session_id })
        ])
      ])
    ])));

  body.querySelectorAll("[data-session]").forEach((button) => {
    button.addEventListener("click", () => openSession(button.getAttribute("data-session")));
  });

  body.querySelectorAll("[data-edit-session]").forEach((button) => {
    button.addEventListener("click", () =>
      openSessionEdit(button.getAttribute("data-edit-session")));
  });

  body.querySelectorAll("[data-remove-session]").forEach((button) => {
    button.addEventListener("click", () =>
      openSessionRemoveDialog(button.getAttribute("data-remove-session")));
  });

  renderPager($("sessions-pager"), data, (offset) => {
    state.sessionOffset = offset;
    loadSessions();
  });
}

function renderPager(container, data, onChange) {
  container.textContent = "";
  if (data.total <= data.limit) {
    if (data.total) {
      container.appendChild(el("span", { text: data.total + " record" + (data.total === 1 ? "" : "s") }));
    }
    return;
  }

  const from = data.offset + 1;
  const to = Math.min(data.offset + data.limit, data.total);

  const previous = el("button", { class: "btn", type: "button", text: "Previous" });
  previous.disabled = data.offset === 0;
  previous.addEventListener("click", () => onChange(Math.max(0, data.offset - data.limit)));

  const next = el("button", { class: "btn", type: "button", text: "Next" });
  next.disabled = to >= data.total;
  next.addEventListener("click", () => onChange(data.offset + data.limit));

  container.append(previous, el("span", { text: from + "–" + to + " of " + data.total }), next);
}

async function openSession(sessionId) {
  state.session = sessionId;
  state.attendanceOffset = 0;

  const [session, analytics] = await Promise.all([
    api("/api/sessions/" + encodeURIComponent(sessionId)),
    api("/api/analytics/sessions/" + encodeURIComponent(sessionId))
  ]);

  /* Pushed before the panel is shown, so the step exists by the time Back
     is rendered - and pushed here rather than at each call site, so every
     route into a session pushes exactly one. */
  if ($("session-detail").hidden) pushDetailStep();
  $("session-detail").hidden = false;
  $("session-detail-heading").textContent = session.class_id + " — " + session.session_date;

  const meta = $("session-meta");
  meta.replaceChildren(
    metaItem("Session", session.session_id),
    metaItem("Class", session.class_id),
    metaItem("Date", session.session_date),
    metaItem("Start", clock(session.start_time)),
    metaItem("End", clock(session.end_time)),
    metaItem("Timezone", session.timezone),
    metaItemNode("Status", statusPill(session.status)),
    metaItem("Policy version", session.policy_version +
      (session.policy_versions_present.length > 1
        ? " (records exist under: " + session.policy_versions_present.join(", ") + ")"
        : ""))
  );

  const counts = analytics.counts;
  $("session-cards").replaceChildren(
    card("Expected", analytics.expected_students),
    card("Evaluated", analytics.evaluated_students),
    card("Attendance", percent(analytics.attendance_rate),
         analytics.is_final ? "final" : "not final — session " + analytics.status.toLowerCase()),
    card("Present", counts.PRESENT),
    card("Late", counts.LATE),
    card("Partial", counts.PARTIAL),
    card("Left early", counts.LEFT_EARLY),
    card("Absent", counts.ABSENT),
    card("Avg presence", percent(analytics.average_presence_ratio)),
    card("Verified in presence data", analytics.verified_students,
         analytics.presence_segments + " segments")
  );

  await loadAttendance();

  const presence = await api("/api/sessions/" + encodeURIComponent(sessionId) + "/presence");
  renderTimeline($("timeline"), presence);

  $("session-detail").scrollIntoView({ block: "start" });
  $("session-detail-heading").focus?.();
}

function metaItem(label, value) {
  return el("div", {}, [el("dt", { text: label }), el("dd", { text: String(value ?? "—") })]);
}

function metaItemNode(label, node) {
  return el("div", {}, [el("dt", { text: label }), el("dd", {}, [node])]);
}

async function loadAttendance() {
  if (!state.session) return;

  const query = new URLSearchParams({
    limit: String(PAGE_SIZE), offset: String(state.attendanceOffset),
    sort: state.attendanceSort, descending: String(state.attendanceDesc)
  });
  if (state.attendanceStatus) query.set("status", state.attendanceStatus);

  const data = await api("/api/sessions/" + encodeURIComponent(state.session) +
                         "/attendance?" + query.toString());

  const body = $("attendance-table").querySelector("tbody");

  if (!data.items.length) {
    body.replaceChildren(el("tr", {}, [
      el("td", { colspan: "9", class: "muted", text:
        data.total === 0 && !state.attendanceStatus
          ? "No attendance records for this session yet."
          : "No records match this filter." })
    ]));
    renderPager($("attendance-pager"), data, () => {});
    return;
  }

  body.replaceChildren(...data.items.map((record) =>
    el("tr", {}, [
      el("td", { text: record.student_id }),
      el("td", { text: record.student_name || "—" }),
      el("td", {}, [
        statusPill(record.status),
        record.is_override ? el("span", { class: "muted small", text: " (corrected)" }) : null,
        /* A backend boolean, rendered. Open "Why?" for the three figures
           behind it. */
        record.within_grace
          ? el("span", { class: "muted small", text: " (within relaxation)" })
          : null
      ]),
      el("td", { text: clock(record.first_seen) }),
      el("td", { text: clock(record.last_seen) }),
      el("td", { class: "num", text: duration(record.present_seconds) }),
      el("td", { class: "num", text: percent(record.presence_ratio) ?? "—" }),
      el("td", { text: record.policy_version }),
      el("td", {}, [el("button", {
        class: "btn-link", type: "button",
        "data-record": record.student_id, text: "Why?"
      })])
    ])));

  body.querySelectorAll("[data-record]").forEach((button) => {
    button.addEventListener("click", () => {
      const studentId = button.getAttribute("data-record");
      openRecord(data.items.find((item) => item.student_id === studentId));
    });
  });

  renderPager($("attendance-pager"), data, (offset) => {
    state.attendanceOffset = offset;
    loadAttendance();
  });
}

/* ---------- evidence + override dialog ---------- */

function openRecord(record) {
  if (!record) return;
  state.record = record;

  const evidence = $("record-evidence");
  const rows = [
    ["Student", record.student_id + (record.student_name ? " — " + record.student_name : "")],
    ["Status", record.status],
    ["Reason", record.reason],
    ["First verified presence", clock(record.first_seen)],
    ["Last verified presence", clock(record.last_seen)],
    ["Verified presence", duration(record.present_seconds)],
    ["Session length", duration(record.session_seconds)],
    ["Presence share", percent(record.presence_ratio) ?? "No data"],
    /* The duration-based relaxation, as the backend computed it.

       Three separate figures on purpose. "Time missing" is the measurement
       and is never reduced by the allowance - a student who missed five
       minutes reads as having missed five minutes, which is the point of
       the rule being a qualification and not an adjustment. "Allowed
       relaxation" is what a class of this length permits. "Within the
       allowance" is the comparison between them, and the Reason row above
       says whether it is what granted the status.

       Every one of these arrives in the response. There is no grace
       arithmetic in this file, and there must not be: the decision is the
       backend's and this only shows it. */
    ["Time missing", duration(record.missing_seconds)],
    ["Allowed relaxation", record.grace_minutes
      ? duration(record.grace_seconds)
      : "none (a class this short gets no relaxation)"],
    ["Within the allowance", record.within_grace ? "yes" : "no"],
    ["Presence segments", String(record.segment_count)],
    ["Times briefly lost", String(record.loss_periods)],
    ["Arrived late", record.was_late ? "yes" : "no"],
    ["Left early", record.left_early ? "yes" : "no"],
    ["Policy version", record.policy_version]
  ];

  if (record.is_override) {
    rows.push(["Originally computed", record.original_status || "—"]);
    rows.push(["Correction reason", record.override_reason || "—"]);
    rows.push(["Corrected by", record.overridden_by || "not recorded"]);
    rows.push(["Corrected at", record.overridden_at || "—"]);
  }

  evidence.replaceChildren(el("dl", { class: "evidence" },
    rows.map(([label, value]) => el("div", { class: "evidence-row" }, [
      el("dt", { text: label }), el("dd", { text: String(value) })
    ]))));

  $("override-status").value = record.status;
  $("override-reason").value = "";
  $("override-error").hidden = true;
  $("override-clear").hidden = !record.is_override;

  $("record-dialog").showModal();
}

async function applyOverride() {
  const record = state.record;
  const reason = $("override-reason").value.trim();
  const error = $("override-error");

  if (reason.length < 3) {
    error.textContent = "A reason is required so the correction can be audited.";
    error.hidden = false;
    $("override-reason").focus();
    return;
  }

  try {
    await apiSend(
      "/api/sessions/" + encodeURIComponent(record.session_id) +
      "/attendance/" + encodeURIComponent(record.student_id) + "/override",
      "POST",
      {
        status: $("override-status").value,
        reason: reason,
        actor: $("override-actor").value.trim() || null,
        policy_version: record.policy_version
      });

    $("record-dialog").close();
    await loadAttendance();
    await loadOverview();
    banner("Correction recorded for " + record.student_id +
           ". The original computed status is preserved.");
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  }
}

async function clearOverride() {
  const record = state.record;
  try {
    await apiSend(
      "/api/sessions/" + encodeURIComponent(record.session_id) +
      "/attendance/" + encodeURIComponent(record.student_id) +
      "/override?policy_version=" + encodeURIComponent(record.policy_version),
      "DELETE");
    $("record-dialog").close();
    await loadAttendance();
    banner("Correction removed. The computed status has been restored.");
  } catch (failure) {
    const error = $("override-error");
    error.textContent = failure.message;
    error.hidden = false;
  }
}

/* ---------- students ---------- */

/* Every column on this page comes from one request. The registry fields are
   SQLite columns, `enrolled` and the sample count come from the embedding
   store's metadata, and the attendance summary is derived from the attendance
   tables - none of it is stored twice, and none of it is invented here. */

function attendanceCell(summary) {
  if (!summary || !summary.expected_sessions) {
    return el("td", { class: "attendance-cell muted",
                      text: summary && summary.recorded_sessions
                        ? summary.recorded_sessions + " recorded"
                        : "—" });
  }

  const rate = percent(summary.attendance_rate);
  return el("td", { class: "attendance-cell" }, [
    el("span", { text: rate === null ? "No data" : rate }),
    el("div", { class: "muted",
                text: summary.sessions_attended + " of " +
                      summary.expected_sessions + " sessions" })
  ]);
}

function studentRow(student) {
  const actions = el("td", { class: "row-actions-cell" }, [
    el("div", { class: "row-actions" }, [
      el("button", { class: "btn-link", type: "button", text: "View",
                     "data-view-student": student.student_id }),
      el("button", { class: "btn-link", type: "button", text: "Edit",
                     "data-edit-student": student.student_id }),
      student.enrolled ? null
        : el("button", { class: "btn-link", type: "button",
                         text: "Enroll face",
                         "data-enroll-student": student.student_id }),
      el("button", { class: "btn-link", type: "button", text: "Remove",
                     "data-remove-student": student.student_id })
    ])
  ]);

  /* The whole row opens the performance profile.

     The View button stays, and deliberately: a row that is only clickable is
     undiscoverable, and it is the thing a keyboard reaches with Tab. This
     adds a second way in for the people who tried clicking the name first,
     which is most of them. data-row-student is read by loadStudents, and the
     handler ignores a click that landed on one of the action buttons so
     "Remove" does not also open the profile behind the dialog. */
  return el("tr", { class: student.status === "INACTIVE"
                     ? "student-row student-inactive" : "student-row",
                    "data-row-student": student.student_id,
                    title: "Open " + student.name + "'s performance" }, [
    el("td", { text: student.student_id }),
    el("td", { text: student.name }),
    el("td", { text: student.roll_number || "—" }),
    /* Year, class and section together are what tell two students with the
       same name apart at a glance, which is the reason the year column is
       here rather than only in the filter. */
    el("td", { text: student.academic_year || "—" }),
    el("td", { text: student.class_name || "—" }),
    el("td", { text: student.section || "—" }),
    el("td", {}, [statusPill(student.status)]),
    el("td", {}, [statusPill(student.enrolled ? "ENROLLED" : "NOT_ENROLLED")]),
    el("td", { class: "num", text: String(student.sample_count || 0) }),
    attendanceCell(student.attendance),
    actions
  ]);
}

async function loadStudents() {
  const query = new URLSearchParams({
    limit: String(PAGE_SIZE), offset: String(state.studentOffset)
  });
  if (state.studentSearch) query.set("search", state.studentSearch);
  if (state.studentYear) query.set("academic_year", state.studentYear);
  if (state.studentClass) query.set("class_name", state.studentClass);
  if (state.studentSection) query.set("section", state.studentSection);
  if (state.studentStatus) query.set("status", state.studentStatus);
  if (state.studentEnrolled) query.set("enrolled", state.studentEnrolled);

  const data = await api("/api/students?" + query.toString());

  $("students-empty").hidden = data.total > 0;
  $("students-list-wrap").hidden = data.total === 0;
  renderStudentCount(data.total);
  renderStudentEmptyState();

  const body = $("students-table").querySelector("tbody");
  body.replaceChildren(...data.items.map(studentRow));

  /* The same page, as a formation above the table. */
  spatialRoster(data);

  const bind = (attribute, handler) => {
    body.querySelectorAll("[" + attribute + "]").forEach((button) => {
      button.addEventListener("click",
        () => handler(button.getAttribute(attribute)));
    });
  };

  bind("data-view-student", openStudent);
  bind("data-edit-student", openStudentEdit);

  /* Clicking anywhere on the row that is not already a control. `closest`
     is what keeps the action buttons working: a click on "Remove" has the
     row as an ancestor, and without this check it would fire both. */
  body.querySelectorAll("[data-row-student]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, select")) return;
      openStudent(row.getAttribute("data-row-student"));
    });
  });

  bind("data-enroll-student", enrollExistingStudent);
  bind("data-remove-student", openStudentRemove);

  /* The options for all three filters, from the registry rather than from
     this page. Not awaited: the table is already rendered and the datalists
     are a convenience, so a slow second request must not hold the rows up. */
  loadStudentFilterOptions();

  renderPager($("students-pager"), data, (offset) => {
    state.studentOffset = offset;
    loadStudents();
  });
}

/* Why the table is empty.

   "No students" and "no students matching these filters" are different
   facts and must read differently: the first invites registering somebody,
   and the second is almost always a filter the reader can widen. The
   heading changes too, because that is the line people actually read. */
function renderStudentEmptyState() {
  const chosen = [
    state.studentYear && "Year " + state.studentYear,
    state.studentClass && "Class " + state.studentClass,
    state.studentSection && "Section " + state.studentSection
  ].filter(Boolean);

  const filtering = Boolean(chosen.length || state.studentSearch
                            || state.studentStatus || state.studentEnrolled);

  const heading = $("students-empty").querySelector("h2");
  const reason = $("students-empty-reason");

  if (!filtering) {
    heading.textContent = "No students";
    /* Deliberately not the wording the markup uses.

       tests/test_live_api.py isolates the live-classroom code by slicing
       this file between two literal strings, and the closing one is a phrase
       about scheduling. An earlier second copy of it moves the slice and the
       test then reads code it was never looking at - so the wording here
       stays clear of it, and this comment does too. */
    reason.textContent =
      "No student is registered yet. Use + Add Student to register one and "
      + "capture their face samples, then add them to a class roster.";
    return;
  }

  heading.textContent = "No students match these filters";
  reason.textContent = chosen.length
    ? "No registered student is in " + chosen.join(" · ")
    + ". Widen or clear the filters to see more."
    : "No registered student matches the current filters. Widen or clear "
    + "them to see more.";
}

/* How many students this view is showing, and of how many.

   Both numbers are the API's `total`. The filtered one came back with the
   page that was just rendered; the unfiltered one is fetched once per visit
   with limit=1, which returns ONE student row and the full count - the count
   is a COUNT(*) in SQL and does not depend on how many rows were asked for.

   So the line below costs one row, not five thousand. Nothing here adds
   anything up. */
async function renderStudentCount(filtered) {
  const node = $("students-count");
  const filtering = Boolean(state.studentYear || state.studentClass
                            || state.studentSection || state.studentSearch
                            || state.studentStatus || state.studentEnrolled);

  if (state.studentTotal === null) {
    try {
      const all = await api("/api/students?limit=1&offset=0");
      state.studentTotal = all.total;
    } catch (ignored) {
      /* The count is a nicety. A failure here must not blank the table that
         has already been drawn, so the line simply states what it knows. */
    }
  }

  const total = state.studentTotal;

  if (!filtering) {
    node.textContent = total === null
      ? plural(filtered, "student")
      : plural(total, "student") + " registered";
    return;
  }

  node.textContent = total === null
    ? plural(filtered, "student") + " match these filters"
    : plural(filtered, "student") + " of " + total.toLocaleString()
    + " match these filters";
}

function plural(count, word) {
  return (count || 0).toLocaleString() + " " + word + (count === 1 ? "" : "s");
}

/* The years, classes and sections registered students are actually in.

   One request, answered by three GROUP BYs over the registry - so the lists
   are the school's own words and a year nobody is in is never offered. Each
   list is narrowed by the OTHER two selections, which is what makes choosing
   a year and a class leave only the sections that pair really has.

   Failure is silent and deliberately so: a datalist is a suggestion, and a
   filter typed by hand works identically without one. */
async function loadStudentFilterOptions() {
  const query = new URLSearchParams();
  if (state.studentYear) query.set("academic_year", state.studentYear);
  if (state.studentClass) query.set("class_name", state.studentClass);
  if (state.studentSection) query.set("section", state.studentSection);

  let options;
  try {
    options = await api("/api/students/filters?" + query.toString());
  } catch (ignored) {
    return;
  }

  fillDatalist("student-years", options.years);
  fillDatalist("student-classes", options.classes);
  fillDatalist("student-sections", options.sections);
}

function fillDatalist(id, values) {
  const node = $(id);
  if (!node) return;
  node.replaceChildren(
    ...(values || []).map((value) => el("option", { value: value })));
}

function readStudentFilters() {
  state.studentSearch = $("student-search").value.trim();
  state.studentYear = $("student-filter-year").value.trim();
  state.studentClass = $("student-filter-class").value.trim();
  state.studentSection = $("student-filter-section").value.trim();
  state.studentStatus = $("student-filter-status").value;
  state.studentEnrolled = $("student-filter-enrolled").value;
  state.studentOffset = 0;
}

/* A student's attendance trend, in words.

   The arithmetic is done on the server (analytics_service._attendance_trend)
   so this only chooses a label and a colour. Nothing is computed here that
   the table below the summary does not also show, which is the rule the whole
   performance profile follows: every figure on it can be recounted by hand
   from the rows printed underneath it. */
const TREND_WORDS = {
  IMPROVING: { label: "Improving", tone: "trend-up" },
  DECLINING: { label: "Declining", tone: "trend-down" },
  STEADY: { label: "Steady", tone: "trend-flat" },
  NO_DATA: { label: "Not enough sessions", tone: "trend-none" }
};

function trendSummary(trend) {
  const shape = TREND_WORDS[(trend && trend.direction) || "NO_DATA"]
                || TREND_WORDS.NO_DATA;

  const parts = [
    el("strong", { class: "trend-label " + shape.tone, text: shape.label })
  ];

  if (trend && trend.recent_rate !== null && trend.earlier_rate !== null) {
    parts.push(el("span", { class: "muted small", text:
      " " + percent(trend.earlier_rate) + " → " +
      percent(trend.recent_rate) }));
  }

  parts.push(el("p", { class: "muted small", text:
    (trend && trend.note) || TREND_WORDS.NO_DATA.label }));

  return el("div", { class: "trend" }, parts);
}

async function openStudent(studentId) {
  /* Two requests, in parallel. The analytics endpoint knows the rates and the
     registry fields; face-enrollment state lives in the Step 3 embedding
     store's metadata and reaches the browser only through /api/students. Both
     are scoped to the caller's classes on the server, so a student outside
     them answers 404 from either one. */
  const [analytics, record] = await Promise.all([
    api("/api/analytics/students/" + encodeURIComponent(studentId)),
    api("/api/students/" + encodeURIComponent(studentId))
  ]);

  if ($("student-detail").hidden) pushDetailStep();
  $("student-detail").hidden = false;
  $("student-detail-heading").textContent = analytics.name + " (" + analytics.student_id + ")";

  /* Who they are, then how they have been doing. The registry fields come
     from the same response as the rates - see the comment above the return in
     analytics_service.student_analytics - so the profile never shows a class
     that disagrees with the row it was opened from. A blank one says "not
recorded" rather than being quietly omitted. */
  const orDash = (value) => value || "—";

  $("student-meta").replaceChildren(
    metaItem("Student ID", analytics.student_id),
    metaItem("Name", analytics.name),
    metaItem("Roll number", orDash(analytics.roll_number)),
    metaItem("Class", orDash(analytics.class_name)),
    metaItem("Section", orDash(analytics.section)),
    metaItem("Record status", analytics.status),
    metaItem("Face enrollment", record.enrolled
      ? "Enrolled (" + (record.sample_count || 0) + " samples)"
      : "Not enrolled"),
    metaItem("Completed sessions expected", analytics.expected_sessions)
  );

  const counts = analytics.counts;
  $("student-cards").replaceChildren(
    card("Attendance rate", percent(analytics.attendance_rate),
         analytics.expected_sessions
           ? "over " + analytics.expected_sessions + " sessions"
           : "no completed sessions"),
    card("Total sessions", analytics.expected_sessions),
    card("Sessions attended", analytics.satisfying_count),
    card("Sessions absent", analytics.absent_sessions ?? counts.ABSENT),
    card("Avg presence", percent(analytics.average_presence_ratio)),
    card("Avg duration", analytics.average_present_seconds === null
      ? null : duration(analytics.average_present_seconds)),
    card("Present", counts.PRESENT),
    card("Late", counts.LATE),
    card("Partial", counts.PARTIAL),
    card("Left early", counts.LEFT_EARLY),
    card("Absent", counts.ABSENT)
  );

  $("student-trend").replaceChildren(trendSummary(analytics.trend));

  historyChart($("student-chart"), analytics.history);

  const body = $("student-history").querySelector("tbody");
  if (!analytics.history.length) {
    body.replaceChildren(el("tr", {}, [
      el("td", { colspan: "6", class: "muted", text:
        "This student has not been expected in any completed session yet." })
    ]));
  } else {
    body.replaceChildren(...analytics.history.map((entry) =>
      el("tr", {}, [
        el("td", { text: entry.session_date }),
        el("td", { text: entry.class_id }),
        el("td", {}, [entry.status ? statusPill(entry.status)
          : el("span", { class: "muted", text: "not evaluated" })]),
        el("td", { class: "num", text: percent(entry.presence_ratio) ?? "—" }),
        el("td", { class: "num", text: duration(entry.present_seconds) }),
        el("td", { text: entry.policy_version || "—" })
      ])));
  }

  $("student-detail").scrollIntoView({ block: "start" });
}

/* ---------- view switching ---------- */

/*
   EVERY VIEW, AND EXACTLY ONE OF THEM.

   The list below is the whole application. It grew when Administration was
   broken up: an account list, a credential list, an alerting rule set, a
   model registry and a log were five panels on one page, and they share
   nothing but the permission that opens them. Classes left the session
   register for the same reason - "what is scheduled" and "what classes exist"
   were never the same question.

   Keyed by the data-view on the rail item, so adding a destination means
   adding a button and a line here and nothing else.
*/
const VIEWS = ["overview", "live", "assembly", "reports", "students",
               "sessions", "classes", "assistant", "cameras", "academic",
               "models", "admin", "logins", "alerts", "audit"];

/* The views only a Super Admin may open. The server refuses every one of
   these regardless; hiding them is a courtesy, not the permission. */
const ADMIN_VIEWS = ["academic", "models", "admin", "logins", "alerts",
                     "audit"];

/*
   The panels that open INSIDE a view rather than replacing it.

   Opening one is a navigation - it is the thing somebody wants to get back
   out of - so Back closes the panel before it changes the view. Without this,
   Back out of a student profile would leave the profile on screen and switch
   the tab underneath it.
*/
const DETAIL_PANELS = {
  sessions: ["session-detail"],
  students: ["student-detail"],
  live: ["live-summary"],
  assembly: ["assembly-summary"]
};

function switchView(view, isBack) {
  if (!VIEWS.includes(view)) return;

  /* The step being left, remembered before anything changes. Not remembered
     when this IS a back navigation, or the stack would never empty. */
  if (!isBack && view !== state.view) {
    state.viewStack.push(state.view);
    pushNavStep();
  }

  state.view = view;

  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    if (active) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });

  /* The top bar names the view. Taken from the navigation item's own label
     rather than from a second table of names, which could disagree with the
     rail about what a page is called. */
  const current = document.querySelector('.tab[data-view="' + view + '"]');
  if (current) $("topbar-view").textContent = current.textContent.trim();

  /* On a small screen the rail is a drawer over the content; choosing a
     destination is the moment it should get out of the way. */
  closeSidebar();

  $("view-overview").hidden = view !== "overview";
  $("view-sessions").hidden = view !== "sessions";
  $("view-classes").hidden = view !== "classes";
  $("view-students").hidden = view !== "students";
  $("view-live").hidden = view !== "live";
  $("view-assembly").hidden = view !== "assembly";
  $("view-reports").hidden = view !== "reports";
  $("view-cameras").hidden = view !== "cameras";
  $("view-assistant").hidden = view !== "assistant";
  $("view-academic").hidden = view !== "academic";
  $("view-admin").hidden = view !== "admin";
  $("view-logins").hidden = view !== "logins";
  $("view-alerts").hidden = view !== "alerts";
  $("view-models").hidden = view !== "models";
  $("view-audit").hidden = view !== "audit";

  /* A detail panel belongs to the view it was opened in. Leaving the view
     closes it, so coming back does not land on somebody else's profile. */
  Object.keys(DETAIL_PANELS).forEach((owner) => {
    if (owner === view) return;
    DETAIL_PANELS[owner].forEach((id) => {
      const panel = $(id);
      if (panel) panel.hidden = true;
    });
  });

  /* The camera keeps running when the teacher looks elsewhere; only the
     polling stops, because nothing is on screen to update. */
  if (view === "live") {
    startLivePolling();
  } else {
    stopLivePolling();
    /* The camera keeps running - the lesson is still happening - but there is
       no reason to keep pulling pictures nobody is looking at. */
    showClassroomView(false);
  }

  /* Same rule for camera health: poll only while somebody is looking at it. */
  if (view === "cameras") startCameraPolling();
  else stopCameraPolling();

  /* And for Assembly Mode, which polls the same two live endpoints the live
     console does. Leaving the page stops the polling and nothing else: the
     camera keeps running, because the assembly is still happening. */
  if (window.Assembly) {
    if (view === "assembly") window.Assembly.enter();
    else window.Assembly.leave();
  }

  /* Generated portal passwords are shown once and are not left on screen for
     the next person who walks past the machine. Leaving the view clears them;
     they cannot be read back from the server, which is the whole reason the
     office was told to write them down. */
  if (view !== "logins") clearIssuedLogins();

  /* The cinematic move: the view being left falls back into the room and the
     one being opened comes forward out of the glass. 420ms, and switched off
     entirely under prefers-reduced-motion - a tool somebody navigates two
     hundred times a day cannot charge them a second each time. */
  if (window.Spatial) window.Spatial.view(view);

  renderBack();
  refresh();
}

/* ---------- going back ---------- */

/*
   ONE BACK CONTROL, AND ONE STACK BEHIND IT.

   The complaint this answers is the simplest one a tool can attract: you open
   something and there is no way out of it. There is now, in one place, on
   every page, and it undoes the LAST THING THAT HAPPENED rather than guessing
   at a hierarchy:

     a detail panel is open   ->  close it, stay on the page
     otherwise                ->  go back to the view you came from

   So opening a student from the dashboard takes two presses to get home,
   because two things happened. That is the property that makes a back button
   trustworthy: it is never a shortcut to somewhere you have not been.

   Three controls drive it and they cannot disagree, because all three end up
   here: the button in the top bar, Alt+Left, and the browser's own Back -
   which reaches this through popstate over the entries pushNavStep leaves.

   `navSeq` is how many entries this page has pushed. Without it, the forward
   button would be read as another back and undo a second step.
*/

function pushNavStep() {
  state.navSeq += 1;
  try {
    window.history.pushState({ cv: state.navSeq }, "");
  } catch (ignored) {
    /* A browser that refuses pushState still has a working Back button in the
       top bar: the in-page stack above is the authority, and this is only how
       the hardware button reaches it. */
  }
}

/* The detail panel currently open in this view, if there is one. */
function openDetailPanel() {
  for (const id of (DETAIL_PANELS[state.view] || [])) {
    const panel = $(id);
    if (panel && !panel.hidden) return id;
  }
  return null;
}

/* Is there anything to go back to. Drives whether the control is shown at
   all: a disabled back button is a promise the page cannot keep. */
function canGoBack() {
  return !!(openDetailPanel() || state.viewStack.length);
}

/* The name of a view, taken from its own rail item. One table of names, the
   one already on screen - a second one could disagree with it. */
function viewLabel(view) {
  const tab = document.querySelector('.tab[data-view="' + view + '"]');
  return tab ? tab.textContent.trim() : "";
}

function renderBack() {
  const button = $("topbar-back");
  if (!button) return;

  button.hidden = !canGoBack();
  if (button.hidden) return;

  /* The label says WHERE IT GOES, because "Back" on its own is a guess and
     the whole point of this control is that nobody has to guess. */
  const detail = openDetailPanel();
  const previous = state.viewStack[state.viewStack.length - 1];
  const where = detail ? "" : viewLabel(previous);

  const label = button.querySelector(".topbar-back-label");
  if (label) label.textContent = where ? "Back to " + where : "Back";

  button.title = detail
    ? "Close this and stay on the page (Alt+Left)"
    : (where ? "Back to " + where + " (Alt+Left)" : "Back (Alt+Left)");
}

/*
   Undo one step, and nothing else.

   Closing a detail panel returns focus to the list it was opened from -
   hiding a panel and leaving a keyboard user at the end of the document is
   how a back control becomes useless to somebody who cannot see the page.
*/
function undoOneStep() {
  const detail = openDetailPanel();
  if (detail) {
    closeDetailPanel(detail);
    renderBack();
    return true;
  }

  const previous = state.viewStack.pop();
  if (previous === undefined) {
    renderBack();
    return false;
  }

  switchView(previous, true);
  return true;
}

/* Each detail panel, and what has to be forgotten when it closes. The close
   buttons inside the panels call this too, so there is one way to shut one. */
function closeDetailPanel(id) {
  const panel = $(id);
  if (!panel) return;
  panel.hidden = true;

  if (id === "session-detail") {
    state.session = null;
    returnToList("sessions-table");
  } else if (id === "student-detail") {
    returnToList("students-table");
  } else if (id === "live-summary") {
    returnToList("live-strip");
  } else if (id === "assembly-summary") {
    /* The hall comes back. Assembly Mode owns the summary, so closing it is
       told to the module rather than done to its DOM from here - which is the
       same direction every other call between these two files runs in. */
    if (window.Assembly && window.Assembly.closeSummary) {
      window.Assembly.closeSummary();
    }
    returnToList("assembly-rows");
  }
}

/*
   A detail panel was opened: that is a navigation, so it gets a step.

   Called by openSession and openStudent rather than by their callers, so
   every route into a profile - a table row, the dashboard, a constellation,
   the assembly summary - pushes exactly one step.
*/
function pushDetailStep() {
  pushNavStep();
  renderBack();
}

function goBack() {
  /* Through the history stack where there is one, so the browser's own Back
     button and this one stay in step. popstate below does the actual undo. */
  if (state.navSeq > 0) {
    window.history.back();
    return;
  }
  undoOneStep();
}

function bindBackNavigation() {
  const button = $("topbar-back");
  if (button) button.addEventListener("click", goBack);

  window.addEventListener("popstate", (event) => {
    const to = (event.state && typeof event.state.cv === "number")
      ? event.state.cv : 0;

    if (to < state.navSeq) {
      /* Back, possibly several entries at once if somebody held the button. */
      let steps = state.navSeq - to;
      state.navSeq = to;
      /* Bounded: a stack that has run out stops rather than spinning. */
      while (steps > 0 && undoOneStep()) steps -= 1;
    } else if (to > state.navSeq) {
      /* Forward. There is nothing to redo - the step it would redo was undone
         by a real navigation - so the counter is brought back into step and
         the page is left where it is. */
      state.navSeq = to;
    }

    renderBack();
  });

  /* Alt+Left, which is what the keyboard shortcut for Back is on every
     platform this runs on. Ignored while a dialog is open, because a dialog's
     own Escape is the way out of a dialog. */
  document.addEventListener("keydown", (event) => {
    if (event.altKey && event.key === "ArrowLeft") {
      if (document.querySelector("dialog[open]")) return;
      event.preventDefault();
      goBack();
    }
  });
}

/* ---------- the navigation rail ---------- */

/*
  On a wide screen the rail is always there and these two functions do nothing
  anybody can see. Below 1080px it is off-canvas and this is what brings it in.

  The state lives in a data attribute rather than a class because the CSS keys
  off [data-open="true"], and aria-expanded is updated on the button so a
  screen reader is told whether the drawer is open rather than having to infer
  it from a transform.
*/
function openSidebar() {
  $("sidebar").dataset.open = "true";
  $("sidebar-toggle").setAttribute("aria-expanded", "true");
}

function closeSidebar() {
  $("sidebar").dataset.open = "false";
  $("sidebar-toggle").setAttribute("aria-expanded", "false");
}

function toggleSidebar() {
  if ($("sidebar").dataset.open === "true") closeSidebar();
  else openSidebar();
}

/* ---------- the rail, narrowed ---------- */

/*
  Icons only, or icons and labels.

  A SEPARATE CONCERN FROM THE DRAWER ABOVE, and the two must not be confused.
  `data-open` is the phone's off-canvas drawer and is driven by the top bar's
  hamburger; `data-rail` on the body is the desktop width and is driven by the
  chevron inside the rail. Below 1080px the rail is already a drawer, the
  collapse control is not shown, and this attribute styles nothing - which is
  why the stylesheet scopes every collapsed rule to the wide breakpoint.

  The choice is remembered, because somebody who wants a narrow rail wants it
  on every page and after every reload. Wrapped in try/catch for the reason
  the theme code gives: localStorage throws rather than returning nothing in a
  private window on some browsers, and a preference that cannot be saved must
  not stop the page loading.
*/
const RAIL_KEY = "classvision-rail";

function storedRail() {
  try {
    return window.localStorage.getItem(RAIL_KEY) === "collapsed"
      ? "collapsed" : "expanded";
  } catch (ignored) {
    return "expanded";
  }
}

function applyRail(mode) {
  const collapsed = mode === "collapsed";

  document.body.dataset.rail = collapsed ? "collapsed" : "expanded";

  const button = $("sidebar-collapse");
  if (button) {
    /* aria-expanded is the state, so the accessibility tree and the width
       cannot disagree. The visible label follows it. */
    button.setAttribute("aria-expanded", String(!collapsed));
    button.title = collapsed
      ? "Expand the navigation" : "Collapse the navigation";
    const label = button.querySelector(".sr-only");
    if (label) {
      label.textContent = collapsed
        ? "Expand the navigation" : "Collapse the navigation";
    }
  }

  try {
    window.localStorage.setItem(RAIL_KEY, collapsed ? "collapsed" : "expanded");
  } catch (ignored) {
    /* It applies for this visit; it just will not be remembered. */
  }
}

function toggleRail() {
  applyRail(document.body.dataset.rail === "collapsed"
            ? "expanded" : "collapsed");
}

function bindRail() {
  applyRail(storedRail());

  const button = $("sidebar-collapse");
  if (button) button.addEventListener("click", toggleRail);

  /* The rail's own sign-out, next to the name it signs out. The SAME function
     the top bar's button calls - one code path, so the two cannot drift about
     what signing out does. */
  const out = $("sidebar-signout");
  if (out) out.addEventListener("click", signOut);
}

async function refresh() {
  setBusy(true);

  /* The flag is global, so it is refreshed on every navigation rather than
     only while the live page is open. One request, and it fails quietly: a
     flag that cannot be read must not stop a page loading. */
  if (state.view !== "live") {
    api("/api/live/sessions")
      .then((body) => renderSessionFlag(body.items || []))
      .catch(() => { /* the flag keeps its last state */ });
  }
  try {
    if (state.view === "overview") {
      /* Today first, and independently: it is the panel a teacher opens the
         page for, and it must appear whether or not the overview below it has
         any completed session to summarise. */
      await loadToday();
      await loadOverview();
      /* The stage carries a status row, and it is the same four readings the
         camera health page shows. Its own concern and its own failure: a
         subsystem that will not answer must not take the overview down. */
      loadSystemStatus().catch(() => {});
    }
    else if (state.view === "sessions") await loadSessions();
    /* Classes is a view of its own now, so the class list is loaded by the
       page it is on rather than alongside a session register that never
       needed it. */
    else if (state.view === "classes") await loadClassList();
    else if (state.view === "students") await loadStudents();
    else if (state.view === "live") {
      /* The device list before the status, so the dropdown is populated by the
         time renderLiveStatus decides whether to disable it. Failing to read
         the camera registry must not take the live tab down - the index box
         still works, which is the case this project started from. */
      try { await loadLiveCameras(); } catch (failure) { /* index still works */ }
      await loadLive();
    }
    else if (state.view === "reports") {
      await fillReportClasses();
      await loadReportMode();
    }
    else if (state.view === "assembly") {
      /* Guarded, like every other call into the optional layer: a
         console served without assembly.js shows an empty Assembly
         page rather than failing refresh() for every other view. */
      if (window.Assembly) await window.Assembly.load();
    }
    else if (state.view === "cameras") await loadCameras();
    else if (state.view === "assistant") await loadAssistant();
    else if (state.view === "academic") await loadAcademic();

    /* Administration was five panels on one page. Each is now a view, and
       each loads only itself: an alert rule that will not load can no longer
       take an account list down with it, because they are not on the same
       page any more. */
    else if (state.view === "admin") await loadTeachers();
    else if (state.view === "logins") await loadPortalAccounts();
    else if (state.view === "alerts") {
      await loadAlertRules();
      /* Both are their own concern: how alerts are delivered and what has
         already been raised are two different questions, and neither failing
         should hide the rules. */
      loadAlertDelivery().catch(() => {});
      loadNotifications().catch(() => {});
    }
    else if (state.view === "models") {
      /* The registry reports its own failure in its own panel rather than
         silently: which recogniser is in production is exactly the kind of
         thing somebody must not be left guessing about. */
      await loadModels().catch((failure) => modelError(failure.message));
    }
    else if (state.view === "audit") await loadAudit();
  } catch (failure) {
    banner("Could not load data: " + failure.message, true);
  } finally {
    setBusy(false);
  }
}

/*
  Ask the backend which camera indexes answered.

  The probe is the backend's - it opens each device and reads one frame, which
  is the only way to tell a working camera from one another application is
  holding. This fills the shared datalist and reports what was found in words.

  Refused while a live session is running, and that refusal is shown as the
  reason rather than as a failure: probing would interrupt the lesson.
*/
async function detectCameras(button, reportId) {
  const target = reportId ? $(reportId) : null;
  if (target) target.textContent = "";

  try {
    await withBusyButton(button, { busy: "Detecting...", done: "Detected" },
      async () => {
        const body = await api("/api/cameras/available");
        const items = body.items || [];
        const usable = items.filter((entry) => entry.available);

        $("available-cameras").replaceChildren(
          ...usable.map((entry) => el("option", {
            value: String(entry.index),
            text: entry.index + " (" + entry.width + "x" + entry.height + ")"
          })));

        if (target) target.textContent = describeCameraScan(items, usable);
        return body;
      },
      (failure) => {
        if (target) {
          target.textContent = explainFailure(failure, "detect cameras");
        }
      });
  } catch (failure) {
    /* reported through the handler above */
  }
}

/* An index that opened and gave nothing is named as IN USE, not as absent.
   Those are different faults: one needs a cable, the other needs whatever is
   holding the camera closed. */
function describeCameraScan(items, usable) {
  const held = items.filter((entry) => entry.opened && !entry.available)
    .map((entry) => entry.index);

  if (usable.length) {
    return "Available: " + usable.map((entry) => entry.index).join(", ")
      + (held.length ? ".  Busy (opened, no frame): " + held.join(", ") : "");
  }

  if (held.length) {
    return "Busy: index " + held.join(", ") + " opened but delivered no "
         + "frame, which usually means another application is holding it.";
  }

  /* No camera opened at all. A device that is absent and one this process is
     not permitted to use both fail to open, and OpenCV does not tell them
     apart - so this says both rather than picking one and being wrong. */
  return "No camera detected on indexes 0-4. If a camera is connected, this "
       + "process may not be permitted to use it - check the operating "
       + "system's camera privacy setting.";
}

/*
  Put the reader back where they came from.

  Scrolls the list into view and focuses its first action, so Back works the
  same for a pointer and for a keyboard.
*/
function returnToList(tableId) {
  const table = $(tableId);
  if (!table) return;

  table.scrollIntoView({ block: "nearest", behavior: "auto" });
  const first = table.querySelector("button");
  if (first) first.focus({ preventScroll: true });
}

async function loadClasses() {
  try {
    const classes = await api("/api/classes");
    const select = $("filter-class");
    for (const item of classes) {
      select.appendChild(el("option", {
        value: item.class_id,
        text: item.class_id + " (" + item.completed_sessions + " completed)"
      }));
    }
  } catch (failure) {
    /* the filter simply stays at "All classes" */
  }
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    renderBackendStatus(health.status === "ok" ? "ok" : "warn",
                        health.status === "ok"
                          ? "Backend online, schema " + health.schema
                          : "Backend " + health.status);
    if (health.status !== "ok") {
      banner("Backend reports status “" + health.status + "” (database: " +
             health.database + ", schema: " + health.schema + ").", true);
    }
  } catch (failure) {
    renderBackendStatus("bad", "Backend unreachable");
    banner("Cannot reach the backend. Is it running? " + failure.message, true);
  }
}

/*
  One dot and one line in the rail: is the backend answering.

  The word is the signal and the colour agrees with it, which is the rule every
  status in this interface follows. Until /api/health has answered once this
  reads "Checking backend" in the neutral colour - an installation whose
  backend has not replied yet is not green.
*/
function renderBackendStatus(level, text) {
  const line = $("sidebar-status");
  if (!line) return;

  line.classList.remove("status-ok", "status-warn", "status-bad",
                        "status-idle");
  line.classList.add("status-" + (
    { ok: "ok", warn: "warn", bad: "bad" }[level] || "idle"));

  $("sidebar-status-text").textContent = text;

  /* The overview hero shows the same fact. One source, two places. */
  const hero = $("hero-backend");
  if (hero) hero.textContent = text;
}

/*
  Which recogniser is in production, in the hero.

  Read once after sign-in rather than on every refresh: the active model does
  not change while somebody is looking at the overview, and when it does it
  changes because they went to the registry and activated it.

  It fails quietly. A hero that cannot name the model is a cosmetic loss; a
  dashboard that will not load because of one is not.
*/
async function loadRuntimeIdentity() {
  const target = $("hero-model");
  if (!target) return;

  try {
    const registry = await api("/api/models");
    const active = registry.active;
    target.textContent = active
      ? (active.model_name || active.model_id) + " " + (active.version || "")
      : "No active model";
  } catch (failure) {
    target.textContent = "Recogniser unknown";
  }
}

/* =========================================================================
   THE MULTI-CLASS LIVE CONSOLE

   One panel per session the BACKEND reports running. The count is whatever
   GET /api/live/sessions returns - one, three, eight, fifteen - and there is
   no maximum anywhere in this file. A session that finishes stops being
   reported and its panel goes; a session that starts appears on the next poll.

   THE ISOLATION RULE

   Every panel is built from exactly one entry of that response, and every
   control it carries sends that entry's own session_id. A panel never reads a
   figure from another entry, never shares a frame URL, and never shares a
   presence list. The frame src carries ?session_id= for the same reason: the
   backend resolves it to that session's own manager, so one classroom's camera
   cannot appear in another classroom's panel.

   ONE POLL, NOT N

   The page already polled /api/live/status every second. It now polls
   /api/live/sessions instead - ONE request that carries every running
   session's runtime, including its break state. Adding a timer per panel would
   multiply requests by the number of classrooms for no extra information.

   The per-session presence lists are fetched only for the panels that are
   showing a list: the grid panels show counts, and the focused panel shows the
   names. That keeps the request count flat as classes are added.

   WHAT THE PANELS DO NOT DO

   Open a camera. Run detection. Decide attendance. All of that is the
   backend's, in a worker per session; these panels display what it reports.
   ========================================================================= */

/* Chosen from the SESSION COUNT, not hard-coded. The column count is a
   maximum - the grid's own auto-fit decides the real number from the viewport,
   which is what makes it responsive without a breakpoint per count. */
function gridColumns(count) {
  if (count <= 1) return 1;
  if (count <= 2) return 2;
  if (count <= 6) return 3;
  if (count <= 12) return 4;
  return 5;
}

function liveSessionLabel(entry) {
  const name = entry.class_id || entry.session_id || "Session";
  return entry.section ? name + " - " + entry.section : name;
}

/* The camera a panel is watching, in words. "No data" when the backend did not
   report an index, never a guessed zero. */
function cameraLabel(entry) {
  if (entry.camera_index === null || entry.camera_index === undefined) {
    return "No data";
  }
  return "Camera " + String(entry.camera_index).padStart(2, "0");
}

async function loadActiveLiveSessions() {
  let body;
  try {
    body = await api("/api/live/sessions");
  } catch (failure) {
    liveError(failure.message);
    return;
  }

  const entries = body.items || [];
  state.liveActive = entries;
  state.liveUpdatedAt = new Date();

  renderLiveSummaryBar(body.totals || {}, entries.length);
  renderGlobalLiveActions(entries);
  renderSessionFlag(entries);

  /* A focused session that is no longer running drops focus and returns to
     the grid, rather than leaving a panel describing a lesson that ended. */
  if (state.liveFocus
      && !entries.some((entry) => entry.session_id === state.liveFocus)) {
    state.liveFocus = null;
  }

  if (state.liveFocus) renderLiveFocus(entries);
  else renderLiveGrid(entries);

  renderLiveUpdated();
}

/*
  The flag, from the same answer the panels are built from.

  Never optimistic: the default is "no active session", and it only says LIVE
  because the backend reported a running session.
*/
function renderSessionFlag(entries) {
  const flag = $("session-flag");
  if (!flag) return;

  const running = entries.length;
  const onBreak = entries.filter((entry) => entry.on_break).length;

  flag.classList.toggle("is-live", running > 0 && onBreak < running);
  flag.classList.toggle("is-break", running > 0 && onBreak === running);

  $("session-flag-text").textContent =
    running === 0 ? "No active session"
    : running === 1
      ? (onBreak ? "Session on break" : "Live session")
      : running + " live sessions" + (onBreak ? " (" + onBreak + " on break)"
                                             : "");
}

function renderLiveSummaryBar(totals, count) {
  const bar = $("live-summary-bar");
  if (!bar) return;

  const modules = [
    systemModule("Active classes", count ? "ok" : "idle", String(count)),
    systemModule("Students recognised", "idle",
                 String(totals.recognized_students ?? 0)),
    systemModule("Tracked now", "idle", String(totals.active_tracks ?? 0)),
    systemModule("Cameras connected", totals.cameras_connected ? "ok" : "idle",
                 String(totals.cameras_connected ?? 0)),
    systemModule("On break", totals.on_break ? "warn" : "idle",
                 String(totals.on_break ?? 0)),
    systemModule("Failed", totals.failed ? "bad" : "ok",
                 String(totals.failed ?? 0))
  ];

  bar.replaceChildren(...modules);
}

/*
  The bulk action, and when it is offered.

  Only from two classrooms upwards: with one running class "Finish all" is a
  second button for what the panel's own Finish already does, and two buttons
  for one irreversible action is how the wrong one gets pressed.
*/
function renderGlobalLiveActions(entries) {
  const bar = $("live-global-actions");
  if (!bar) return;

  const running = entries.filter((entry) => entry.session_id);
  bar.hidden = running.length < 2;

  $("live-finish-all-note").textContent = bar.hidden ? ""
    : "Stops " + running.length + " cameras and runs the attendance rules "
      + "for each class separately.";
}

/*
  Finish every running class, one at a time.

  Each stop is the SAME per-session call the panel's own Finish button makes,
  so no finalisation rule is different here and nothing is batched inside the
  backend. Sequential rather than parallel on purpose: finalisation writes
  attendance, and a row of simultaneous writes is a queue nobody asked for.

  Scoped by id, one id at a time, read from the entries the backend just
  reported - so this cannot reach a session that is not running, and a class
  that starts while the loop is part-way through is simply not in the list
  the operator agreed to.

  One failure does not abandon the rest: the classes that could be finished
  are finished, and the ones that could not are named.
*/
async function finishAllLiveSessions(button) {
  liveError("");

  const running = (state.liveActive || []).filter((entry) => entry.session_id);
  if (running.length < 2) return;

  /* The confirmation NAMES every class it is about to finish. A bulk action
     whose scope you cannot read before agreeing to it is not a confirmation,
     so the list is the message rather than a count in it. */
  const labels = running.map(liveSessionLabel);
  const confirmed = window.confirm(
    ["You are about to finish:", ""]
      .concat(labels.map((label) => "  - " + label))
      .concat([
        "",
        "Each class is stopped separately and its attendance rules run on "
        + "its own evidence. This cannot be undone. Continue?"
      ])
      .join("\n"));

  if (!confirmed) return;

  const failures = [];

  await withBusyButton(button, { busy: "Finishing...", done: "Finished" },
    async () => {
      for (const entry of running) {
        try {
          await apiSend("/api/live/stop?session_id="
                        + encodeURIComponent(entry.session_id), "POST");
        } catch (failure) {
          failures.push(liveSessionLabel(entry) + " (" + failure.message + ")");
        }
      }
    },
    () => { /* per-class failures are reported below, not as one message */ });

  if (failures.length) {
    liveError("These classes were not finished: " + failures.join("; ")
              + ". The others were.");
  }

  await loadLive().catch(() => { /* the next poll retries */ });
  await loadSessions().catch(() => { /* the sessions view retries */ });
}

function renderLiveUpdated() {
  const node = $("live-updated");
  if (!node || !state.liveUpdatedAt) return;
  node.textContent = "Last updated: "
    + state.liveUpdatedAt.toTimeString().slice(0, 8);
}

/* ---------- the grid ---------- */

function renderLiveGrid(entries) {
  const grid = $("live-grid");
  const empty = $("live-grid-empty");
  const focus = $("live-focus");
  if (!grid) return;

  focus.hidden = true;
  grid.hidden = entries.length === 0;
  empty.hidden = entries.length > 0;

  /* The column count comes from how many classes are running. */
  grid.style.setProperty("--live-columns", String(gridColumns(entries.length)));

  /* Panels are REUSED by session id rather than rebuilt. Replacing the DOM
     every second would restart every <img> stream and tear the picture once a
     second in every panel. */
  const existing = new Map();
  for (const panel of grid.children) {
    if (panel.dataset.session) existing.set(panel.dataset.session, panel);
  }

  const ordered = [];
  for (const entry of entries) {
    let panel = existing.get(entry.session_id);
    if (panel) {
      updateLivePanel(panel, entry);
      existing.delete(entry.session_id);
    } else {
      panel = buildLivePanel(entry);
    }
    ordered.push(panel);
  }

  /* Whatever is left belonged to a session that is no longer running. Its
     stream is stopped explicitly: an <img> removed from the document can keep
     a motion-JPEG connection open until the browser notices. */
  for (const panel of existing.values()) releaseLivePanel(panel);

  grid.replaceChildren(...ordered);
}

function buildLivePanel(entry) {
  const panel = el("article", { class: "live-panel" });
  panel.dataset.session = entry.session_id;

  const stage = el("div", { class: "live-panel-stage" });
  const image = el("img", {
    alt: "Live view of " + liveSessionLabel(entry),
    class: "live-panel-frame"
  });
  /* Scoped to THIS session. The backend resolves session_id to that session's
     own manager, which is what keeps one room's camera out of another's
     panel. */
  image.src = classroomStreamUrl(entry.session_id);
  stage.appendChild(image);
  stage.appendChild(el("div", { class: "live-panel-idle",
                                "aria-hidden": "true" }));

  panel.appendChild(el("header", { class: "live-panel-head" }, [
    el("span", { class: "live-panel-state" }),
    el("span", { class: "live-panel-name", text: liveSessionLabel(entry) }),
    el("span", { class: "live-panel-camera", text: cameraLabel(entry) })
  ]));
  panel.appendChild(stage);
  panel.appendChild(el("dl", { class: "live-panel-telemetry" }));
  panel.appendChild(el("section", { class: "live-panel-roster" }, [
    el("span", { class: "live-panel-roster-head" }, [
      el("span", { class: "telemetry-key", text: "Present now" }),
      el("span", { class: "live-panel-roster-count muted small" })
    ]),
    el("ul", { class: "live-panel-roster-list" })
  ]));
  panel.appendChild(el("div", { class: "live-panel-actions" }));

  updateLivePanel(panel, entry);
  return panel;
}

function releaseLivePanel(panel) {
  const image = panel.querySelector(".live-panel-frame");
  /* Blank the src before dropping the node: that is what actually closes a
     motion-JPEG connection. */
  if (image) image.removeAttribute("src");
}

function updateLivePanel(panel, entry) {
  const onBreak = Boolean(entry.on_break);
  const running = entry.state === "RUNNING";

  panel.classList.toggle("is-break", onBreak);
  panel.classList.toggle("is-failed", entry.state === "FAILED");

  const stateCell = panel.querySelector(".live-panel-state");
  stateCell.replaceChildren(
    el("span", { class: onBreak ? "status-dot" : "live-dot",
                 "aria-hidden": "true" }),
    el("span", { class: "live-panel-word",
                 text: onBreak ? "BREAK"
                       : running ? "LIVE" : entry.state })
  );

  panel.querySelector(".live-panel-name").textContent =
    liveSessionLabel(entry);
  panel.querySelector(".live-panel-camera").textContent = cameraLabel(entry);

  /* Every reading below is this entry's own. The break countdown is the
     SERVER's remaining_seconds - the page displays it and asks again rather
     than deciding for itself when ten minutes are up. */
  /* Class, section and session id are all shown: the id is what every control
     on this panel is scoped by, so an operator can see which session they are
     about to act on rather than trusting the heading. */
  const rows = [
    ["Session", entry.session_id || "No data"],
    ["Section", entry.section || "not recorded"],
    ["Status", entry.state || "No data"],
    ["Detected", String(entry.active_tracks ?? 0)],
    ["Recognised", String(entry.recognized_students ?? 0)],
    ["Unverified", String(entry.unknown_tracks ?? 0)],
    ["Camera", entry.camera || "No data"],
    ["Duration", elapsedClock(entry.elapsed_seconds || 0)],
    ["FPS", running ? (entry.fps || 0).toFixed(1) : "No data"]
  ];

  /* Authenticity is reported only when the layer is doing something. A column
     of "off" in every panel is noise, and a missing column is not a claim. */
  if (entry.authenticity_available === false) {
    rows.push(["Authenticity", "unavailable"]);
  } else if (entry.authenticity_mode && entry.authenticity_mode !== "off") {
    rows.push(["Authentic source", String(entry.authentic_tracks ?? 0)]);
  }

  if (entry.liveness_available === false) {
    rows.push(["Liveness", "unavailable"]);
  } else if (entry.liveness_mode && entry.liveness_mode !== "off") {
    rows.push(["Live faces", String(entry.live_tracks ?? 0)]);
  }

  if (onBreak) {
    rows.push(["Break ends in",
               elapsedClock(entry.break_remaining_seconds || 0)]);
  }

  panel.querySelector(".live-panel-telemetry").replaceChildren(
    ...rows.flatMap(([label, value]) => [
      el("dt", { class: "telemetry-key", text: label }),
      el("dd", { class: "telemetry-val", text: value })
    ]));

  updateLivePanelRoster(panel, entry);
  renderLivePanelActions(panel, entry);
}

/*
  PRESENT NOW, for this panel's class and no other.

  The names come from entry.present_now, which the backend attached to THIS
  session's entry - so there is no request here, no session id to get wrong,
  and no possibility of CSE-A's students appearing under CSE-B: a panel can
  only render the array that arrived inside its own entry.

  Capped by the backend rather than here. The count line states the full
  figure, so a tile showing six names out of thirty says thirty, and the
  focused view is where the whole list lives.

  Every state is the backend's own word. "Present now" is a recognition
  reading and not attendance, which is why the heading is the same phrase the
  focused view uses underneath its warning to that effect.
*/
function updateLivePanelRoster(panel, entry) {
  const list = panel.querySelector(".live-panel-roster-list");
  const count = panel.querySelector(".live-panel-roster-count");
  if (!list) return;

  const roster = entry.present_now || [];
  const total = entry.present_now_total ?? roster.length;

  count.textContent = total ? total + " verified" : "nobody yet";

  if (!roster.length) {
    list.replaceChildren(el("li", { class: "muted small",
      text: "No student verified yet." }));
    return;
  }

  const rows = roster.map((student) => el("li", { class: "live-panel-face" }, [
    el("span", { class: "live-panel-face-name",
                 text: student.student_name || student.student_id }),
    el("span", { class: "live-panel-face-state",
                 text: livePresenceWord(student) })
  ]));

  /* The tile says how many it is not showing rather than silently truncating
     a class of thirty to six. */
  if (total > roster.length) {
    rows.push(el("li", { class: "muted small",
      text: "+" + (total - roster.length) + " more in the focused view" }));
  }

  list.replaceChildren(...rows);
}

/* The one word a tile has room for: the liveness reading when the layer is
   running, and the presence state otherwise. Never invented - an absent
   field reads as "no data" rather than as a verdict. */
function livePresenceWord(student) {
  if (student.liveness_state && student.liveness_state !== "UNKNOWN") {
    return student.liveness_state;
  }
  return student.presence_state || "No data";
}

/*
  Per-panel controls. Each one carries THIS entry's session id, so Finish on
  CSE-A cannot reach CSE-B - the id travels with the button, not with a
  "currently selected session" variable that could be stale.
*/
function renderLivePanelActions(panel, entry) {
  const actions = panel.querySelector(".live-panel-actions");
  const sessionId = entry.session_id;
  const onBreak = Boolean(entry.on_break);

  const focus = el("button", { class: "btn btn-quiet", type: "button",
                               text: "Focus" });
  focus.addEventListener("click", () => withBusyButton(
    focus, { busy: "Opening...", done: "Open" },
    async () => focusLiveSession(sessionId),
    (failure) => liveError(explainFailure(failure, "focus session"))));

  const breakButton = el("button", {
    class: "btn btn-quiet", type: "button",
    text: onBreak ? "End break" : "10-min break"
  });
  breakButton.addEventListener("click", () => (onBreak
    ? endSessionBreak(sessionId, breakButton)
    : startSessionBreak(sessionId, breakButton)));

  const finish = el("button", { class: "btn btn-danger", type: "button",
                                text: "Finish" });
  finish.addEventListener("click", () => finishSession(sessionId, finish,
                                                      liveSessionLabel(entry)));

  actions.replaceChildren(focus, breakButton, finish);
}

/* ---------- focus mode ---------- */

function focusLiveSession(sessionId) {
  state.liveFocus = sessionId;
  renderLiveFocus(state.liveActive || []);
  loadLive().catch(() => { /* the next poll retries */ });
}

function leaveLiveFocus() {
  state.liveFocus = null;
  renderLiveGrid(state.liveActive || []);
}

function renderLiveFocus(entries) {
  const entry = entries.find(
    (candidate) => candidate.session_id === state.liveFocus);

  if (!entry) {
    leaveLiveFocus();
    return;
  }

  $("live-grid").hidden = true;
  $("live-grid-empty").hidden = true;
  $("live-focus").hidden = false;

  $("live-focus-heading").textContent = liveSessionLabel(entry);
  $("live-focus-eyebrow").textContent =
    (entry.on_break ? "On break" : entry.state) + " · " + entry.session_id;

  /* The focused panel is a full panel, built by the same function as a grid
     panel so the two cannot drift apart in what they report. */
  const body = $("live-focus-body");
  let panel = body.querySelector(".live-panel");

  if (!panel || panel.dataset.session !== entry.session_id) {
    if (panel) releaseLivePanel(panel);
    panel = buildLivePanel(entry);
    panel.classList.add("live-panel-focused");
    body.replaceChildren(panel);
  } else {
    updateLivePanel(panel, entry);
  }

  renderFocusSwitcher(entries, entry.session_id);

  /* The names for this class. Fetched scoped, and allowed to fail: a presence
     list that will not load must not blank the camera above it. */
  loadFocusPresence(entry.session_id).catch(() => {});
}

/*
  PRESENT NOW for the focused class.

  Scoped: /api/live/presence?session_id= answers from that session's own
  manager, so these names belong to this classroom and no other.

  Only the focused class is fetched. A list per grid panel would multiply
  requests by the number of classrooms to show names nobody is reading at that
  size, which is why the grid panels show counts instead.
*/
async function loadFocusPresence(sessionId) {
  const list = $("present-now-list");
  const count = $("present-now-count");
  if (!list) return;

  let presence;
  try {
    presence = await api("/api/live/presence?session_id="
                         + encodeURIComponent(sessionId));
  } catch (failure) {
    count.textContent = explainFailure(failure, "presence");
    return;
  }

  const students = presence.students || [];
  count.textContent = students.length
    ? students.length + " verified"
    : "nobody verified yet";

  if (!students.length) {
    list.replaceChildren(el("li", { class: "muted small",
      text: "No student has been verified in this session yet. An "
            + "unverified person is counted, never named." }));
    return;
  }

  list.replaceChildren(...students.map((student) => {
    /* The row's edge follows the backend's own eligibility verdict: held back
       for review is amber, not visible right now is grey, and neither of them
       means absent. */
    const held = student.attendance_eligibility
      && student.attendance_eligibility !== "ELIGIBLE"
      && student.attendance_eligibility !== "NOT_CHECKED";
    const lost = student.presence_state === "TEMPORARILY_LOST"
      || student.presence_state === "SEEN_EARLIER";

    const row = el("li", {
      class: "present-row" + (held ? " is-held" : lost ? " is-lost" : "")
    }, [
      el("span", { class: "present-name",
                   text: student.student_name || student.student_id }),
      el("span", { class: "present-id", text: student.student_id }),
      statusPill(student.presence_state),
      statusPill(student.liveness_state)
    ]);

    if (student.first_seen || student.last_seen) {
      row.appendChild(el("span", { class: "present-seen",
        text: (student.first_seen ? clock(student.first_seen) : "?")
              + " \u2192 "
              + (student.last_seen ? clock(student.last_seen) : "?") }));
    }

    return row;
  }));
}

/* The other running classes, as a switcher. Not hidden: moving between
   classrooms should be one click, never a navigation. */
function renderFocusSwitcher(entries, focusedId) {
  const switcher = $("live-focus-switcher");
  const others = entries.filter((entry) => entry.session_id !== focusedId);

  if (!others.length) {
    switcher.replaceChildren();
    return;
  }

  switcher.replaceChildren(
    el("span", { class: "panel-eyebrow",
                 text: "Other active classes (" + others.length + ")" }),
    el("div", { class: "focus-switcher-row" }, others.map((entry) => {
      const button = el("button", {
        class: "btn btn-quiet focus-chip" + (entry.on_break ? " is-break" : ""),
        type: "button"
      });
      button.appendChild(el("span", {
        class: entry.on_break ? "status-dot" : "live-dot",
        "aria-hidden": "true"
      }));
      button.appendChild(el("span", { text: liveSessionLabel(entry) }));
      button.addEventListener("click",
                              () => focusLiveSession(entry.session_id));
      return button;
    })));
}

/* =========================================================================
   SESSION ACTIONS, AND WHAT A BUTTON LOOKS LIKE WHILE IT WORKS

   Every action below is scoped by session_id and every one of them goes
   through withBusyButton, which does four things no action should have to
   repeat: disables the control so a second click cannot send a second
   request, says what it is doing, says when it worked, and turns a status code
   into a sentence.
   ========================================================================= */

/*
  A status code is not an error message.

  "HTTP 409" tells a teacher nothing. These are the refusals this API actually
  issues, in the words of the thing that was refused. The technical detail is
  still logged to the console for whoever is debugging it.
*/
function explainFailure(failure, context) {
  const status = failure && failure.status;
  const detail = (failure && failure.message) || "";

  /* The backend's own sentence is usually the best one: these services are
     written to explain themselves. It is used whenever there is one. */
  if (detail && !/^Request failed/.test(detail)) {
    if (window.console) console.debug("[ClassVision]", context, status, detail);
    return detail;
  }

  if (window.console) console.debug("[ClassVision]", context, status, failure);

  /* Keyed on WHAT WAS BEING DONE as well as the status: a 503 starting a
     session is a camera problem, and saying so is more use than "a resource
     is unavailable". */
  const live = /session|break|camera|focus|finish/.test(context || "");

  if (status === 401) {
    return "Your session has expired. Please sign in again.";
  }
  if (status === 403) {
    return "This account is not allowed to do that.";
  }
  if (status === 404) {
    return live
      ? "This classroom session is no longer available."
      : "That is no longer there. Refresh and try again.";
  }
  if (status === 409) {
    return live
      ? "This session is no longer active."
      : "That has already been done, or cannot be done in the current state.";
  }
  if (status === 422) {
    return "Something in that request was not valid.";
  }
  if (status === 503) {
    return "Camera could not be opened. Check that it is connected, "
         + "permitted, and not being used by another application.";
  }
  if (status >= 500) {
    return "The ClassVision server could not complete that. Its log has the "
         + "detail.";
  }
  /* No status at all means the request never arrived. */
  if (!status) {
    return "Unable to reach the ClassVision server. Please try again.";
  }
  return "That could not be completed.";
}

/*
  Run an action with the button reporting its own progress.

  Normal -> "Working..." (disabled) -> "Done" for a moment -> Normal, or the
  error surfaced through the caller's own reporter. The disable is the part
  that matters most: it is what stops a double-clicked Finish sending two
  finalisations.
*/
async function withBusyButton(button, labels, work, onError) {
  if (!button || button.disabled) return undefined;

  const original = button.textContent;
  button.disabled = true;
  button.dataset.busy = "true";
  button.textContent = labels.busy || "Working...";

  try {
    const result = await work();
    button.textContent = labels.done || "Done";
    window.setTimeout(() => {
      button.textContent = original;
      delete button.dataset.busy;
      button.disabled = false;
    }, 1200);
    return result;
  } catch (failure) {
    button.textContent = original;
    delete button.dataset.busy;
    button.disabled = false;
    if (onError) onError(failure);
    throw failure;
  }
}

/* ---------- breaks, per session ---------- */

async function startSessionBreak(sessionId, button) {
  liveError("");

  try {
    await withBusyButton(button, { busy: "Starting...", done: "On break" },
      () => apiSend("/api/live/break?session_id="
                    + encodeURIComponent(sessionId), "POST"),
      (failure) => liveError(explainFailure(failure, "start break")));
  } catch (failure) {
    return;
  }

  /* The authoritative state comes back on the next poll, not from this
     response - the server owns the clock. */
  await loadLive().catch(() => {});
}

async function endSessionBreak(sessionId, button) {
  liveError("");

  try {
    await withBusyButton(button, { busy: "Ending...", done: "Resumed" },
      () => apiSend("/api/live/break/end?session_id="
                    + encodeURIComponent(sessionId), "POST"),
      (failure) => liveError(explainFailure(failure, "end break")));
  } catch (failure) {
    return;
  }

  await loadLive().catch(() => {});
}

/* ---------- finishing one session ---------- */

/*
  Finish ONE class.

  The session id travels with the button that was clicked, so Finish on CSE-A
  finishes CSE-A whatever else is running. The confirmation names the class for
  the same reason: the mistake worth preventing is finalising the wrong room.

  Finalising twice is already safe server-side - it answers
  already_finalized: true and writes nothing - and the button disables itself
  anyway, so a double click cannot even ask.
*/
async function finishSession(sessionId, button, label) {
  liveError("");

  const confirmed = window.confirm(
    "Finish " + (label || sessionId) + "?\n\n"
    + "This stops the camera and runs the attendance rules for this class. "
    + "Other running classes are not affected.");

  if (!confirmed) return;

  let summary;
  try {
    summary = await withBusyButton(button,
      { busy: "Finishing...", done: "Completed" },
      () => apiSend("/api/live/stop?session_id="
                    + encodeURIComponent(sessionId), "POST"),
      (failure) => liveError(explainFailure(failure, "finish session")));
  } catch (failure) {
    return;
  }

  if (summary) {
    renderLiveSummary(summary);
    banner(summary.already_finalized
      ? "That session had already been finalised. Nothing was changed."
      : "Session " + (label || sessionId) + " completed: "
        + summary.students_evaluated + " of " + summary.expected_students
        + " student(s) evaluated.");
  }

  /* The class is gone from the live list on the next poll. Reports and the
     overview now have a completed session to show, so they are refreshed too. */
  state.liveFocus = null;
  await loadLive().catch(() => {});
  await loadLiveSessions().catch(() => {});
}

/* ---------- live classroom (Step 8) ---------- */

/*
  The live view polls two small JSON endpoints once a second. It draws no
  video: the camera feed never leaves the backend process, and there is no
  endpoint that could serve a picture even if this file asked for one.

  Three distinctions the rendering is careful to keep:
    - a person the camera has not verified is a COUNT, never a name;
    - "not visible right now" is TEMPORARILY LOST or SEEN EARLIER, not absent;
    - the durations are provisional until the session is finalised.
*/

const ELIGIBILITY_LABEL = {
  ELIGIBLE: "Counts",
  BLOCKED_SPOOF: "Held for review",
  BLOCKED_UNCERTAIN: "Held for review",
  BLOCKED_SUSPICIOUS: "Held for review",
  BLOCKED_UNVERIFIED_SOURCE: "Held for review",
  NOT_CHECKED: "Not checked"
};

const LIVE_POLL_MS = 1000;
const LIVE_ACTIVE_STATES = ["STARTING", "RUNNING", "STOPPING"];

function liveError(message) {
  const node = $("live-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function liveItem(label, value, status) {
  return el("span", { class: "live-item" }, [
    el("span", {
      class: "sym" + (status ? " live-" + status : ""),
      "aria-hidden": "true", text: STATUS_SYMBOL[status] || "•"
    }),
    el("span", { class: "live-label", text: label }),
    el("span", { class: "live-value", text: value })
  ]);
}

function elapsedClock(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  const pad = (value) => (value < 10 ? "0" + value : String(value));
  if (minutes >= 60) {
    return pad(Math.floor(minutes / 60)) + ":" + pad(minutes % 60) + ":" + pad(rest);
  }
  return pad(minutes) + ":" + pad(rest);
}

async function loadLiveSessions() {
  const select = $("live-session");
  const runnable = [];

  for (const status of ["ACTIVE", "SCHEDULED"]) {
    const query = new URLSearchParams({ status: status, limit: "50", offset: "0" });
    if (state.classId) query.set("class_id", state.classId);
    const page = await api("/api/sessions?" + query.toString());
    runnable.push(...page.items);
  }

  const chosen = state.liveSession || select.value;

  select.replaceChildren(...(runnable.length
    ? runnable.map((session) => el("option", {
        value: session.session_id,
        text: session.session_date + " — " + session.class_id + " (" + session.status + ")"
      }))
    : [el("option", { value: "", text: "No scheduled or active session" })]));

  if (chosen && runnable.some((session) => session.session_id === chosen)) {
    select.value = chosen;
  }
  state.liveSession = select.value;

  /* Kept so ending a session can tell whether it is ending EARLY. The live
     status reports what the camera is doing; only the session knows when the
     lesson was supposed to finish. */
  state.liveSessionList = runnable;
}

/* The registered devices a session can be run on.

   Read from /api/cameras, the same list the Camera health tab shows, so a
   device registered there is immediately selectable here and there is no
   second notion of which cameras exist.

   A device's health is shown beside its name when it has reported any, and the
   words "no data yet" when it has not. Nothing is inferred from silence: a
   camera that has never sent a heartbeat is still selectable, because a device
   registered five minutes ago on a machine with no heartbeat agent is a
   perfectly working camera that simply has not been asked to report. Refusing
   to offer it would be a judgement the data does not support. */
async function loadLiveCameras() {
  const select = $("live-camera-device");
  const chosen = state.liveCamera || select.value;

  let cameras = [];
  try {
    cameras = (await api("/api/cameras")).items || [];
  } catch (failure) {
    /* The index box still works, so this tab is not broken by a camera
       registry that could not be read. */
    $("live-camera-note").textContent =
      "Registered devices could not be read (" + failure.message +
      "). The camera index below still works.";
    return;
  }

  const options = [el("option", { value: "", text: "Default camera" })];

  for (const camera of cameras) {
    const where = camera.classroom || camera.label || camera.camera_id;
    const health = camera.last_heartbeat
      ? camera.status + (camera.last_fps === null ||
                         camera.last_fps === undefined
                         ? "" : ", " + camera.last_fps + " fps")
      : "no data yet";

    options.push(el("option", {
      value: camera.camera_id,
      text: where + " \u2014 " + camera.camera_id +
            (camera.camera_index === null || camera.camera_index === undefined
              ? "" : " (index " + camera.camera_index + ")") +
            " \u00b7 " + health
    }));
  }

  select.replaceChildren(...options);

  if (chosen && cameras.some((camera) => camera.camera_id === chosen)) {
    select.value = chosen;
  }
  state.liveCamera = select.value;

  $("live-camera-note").textContent = cameras.length
    ? "Pick the room. The device's own camera index is used unless an index " +
      "is typed below. A device that has not reported its health yet is still " +
      "selectable \u2014 silence is not a fault."
    : "No camera device is registered, so the machine's default camera is " +
      "used. Register a device on the Camera health tab to pick a room by " +
      "name.";
}

function scheduledSession(sessionId) {
  return (state.liveSessionList || []).find(
    (session) => session.session_id === sessionId) || null;
}

/*
  The classroom view.

  An <img> pointed at a motion-JPEG endpoint. No player, no library, no
  WebRTC, no build step - the browser has decoded multipart/x-mixed-replace
  since forever, and on a machine talking to itself the bandwidth it costs is
  not a cost.

  The src is set ONCE when a session starts and cleared when it stops.
  Re-assigning it on every poll would tear the stream down and rebuild it
  once a second, which looks exactly like a broken camera.
*/
/*
  The ONE place in this page that opens the classroom stream.

  Deliberately one function, and a test asserts the URL appears exactly once:
  a second place that opened a stream would be a second camera connection
  nobody had accounted for.

  `sessionId` scopes it to one classroom - the backend resolves it to that
  session's own manager, which is what keeps one room's camera out of another
  room's panel. The timestamp is cache-busting: a browser that has seen this
  URL will happily replay the finished stream from the previous session.
*/
function classroomStreamUrl(sessionId) {
  const base = "/api/live/stream.mjpg?t=" + Date.now();
  return sessionId
    ? base + "&session_id=" + encodeURIComponent(sessionId)
    : base;
}

function showClassroomView(running) {
  const view = $("live-view");

  if (running && !view.dataset.streaming) {
    view.src = classroomStreamUrl(state.liveRunningSession);
    view.dataset.streaming = "1";
    view.hidden = false;
    $("classroom-idle").hidden = true;
  }

  if (!running && view.dataset.streaming) {
    /* Drop the connection rather than leaving a generator open on the server
       for a tab nobody is looking at. */
    view.removeAttribute("src");
    delete view.dataset.streaming;
    view.hidden = true;
    $("classroom-idle").hidden = false;
  }
}

function renderLiveStatus(status) {
  const running = LIVE_ACTIVE_STATES.indexOf(status.state) >= 0;

  state.liveRunningSession = status.session_id || null;
  showClassroomView(running);

  $("live-start").disabled = running || state.liveBusy;
  $("live-stop").disabled = !running && status.state !== "FAILED";
  $("live-session").disabled = running;
  $("live-camera").disabled = running;
  /* The device cannot be swapped under a running pipeline. Changing it mid
     lesson would mean the frames after the change are from a different room
     while the presence evidence says one session. */
  $("live-camera-device").disabled = running;

  $("live-strip").replaceChildren(
    liveItem("Camera", status.camera, status.state),
    liveItem("Session", status.state, status.state),
    liveItem("Duration", elapsedClock(status.elapsed_seconds), status.state),
    liveItem("FPS", running ? status.fps.toFixed(1) : "—", status.state),
    liveItem("Liveness", status.liveness_available
      ? status.liveness_mode
      : "unavailable", status.state),
    liveItem("Authenticity", status.authenticity_available
      ? status.authenticity_mode
      : "unavailable", status.state)
  );

  $("live-cards").replaceChildren(
    card("Verified students", status.recognized_students,
         "confirmed identities so far"),
    card("Tracked now", status.active_tracks, "people the camera can see"),
    card("Unverified now", status.unknown_tracks, "counted, never named"),
    card("Confirmed live", status.live_tracks,
         "tracks that passed the liveness check"),
    card("Needs review", status.spoof_tracks + status.uncertain_tracks,
         status.spoof_tracks + " with attack evidence, " +
         status.uncertain_tracks + " undecided"),
    card("Authentic source", status.authentic_tracks,
         status.suspicious_tracks + " flagged, " +
         status.unverified_source_tracks + " unverified"),
    card("Captures processed", status.processed_count,
         status.dropped_count + " not delivered by the camera")
  );

  if (status.error) liveError(status.error);
  else if (!state.liveBusy) liveError("");

  /* The core, the frame furniture and the telemetry rail, from the same
     status object the strip above was drawn from. */
  spatialLiveStatus(status);
}

function eligibilityCell(value) {
  /* "Held for review" rather than anything resembling an attendance status.
     Blocked evidence is not a claim that anybody was away. */
  return el("span", {
    class: "eligibility eligibility-" + (value || "NOT_CHECKED"),
    title: value || "NOT_CHECKED",
    text: ELIGIBILITY_LABEL[value] || "Not checked"
  });
}

function renderLivePresence(presence) {
  const body = $("live-students-table").querySelector("tbody");

  if (!presence.students.length) {
    body.replaceChildren(el("tr", {}, [
      el("td", { colspan: "9", class: "muted",
                 text: "No student has been verified yet in this session." })
    ]));
  } else {
    body.replaceChildren(...presence.students.map((student) =>
      el("tr", {}, [
        el("td", { text: student.student_id }),
        el("td", { text: student.student_name || "—" }),
        el("td", {}, [statusPill(student.presence_state)]),
        el("td", {}, [statusPill(student.liveness_state),
                      student.liveness_confirmed
                        ? null
                        : el("span", { class: "muted small", text: " unconfirmed" })]),
        el("td", {}, [statusPill(student.authenticity_state),
                      student.authenticity_confirmed
                        ? null
                        : el("span", { class: "muted small", text: " unconfirmed" })]),
        el("td", {}, [eligibilityCell(student.attendance_eligibility)]),
        el("td", { text: clock(student.first_seen) }),
        el("td", { text: clock(student.last_seen) }),
        el("td", { class: "num", text: duration(student.duration_seconds) })
      ])));
  }

  renderLiveTracks(presence);

  /* The formation, and the authenticity sphere. Both from this payload: the
     formation from the confirmed students, the sphere from the tracks - and
     they are separate facts, which is why they are read separately. */
  spatialPresence(presence);
  spatialAuthenticity(presence.tracks || []);
}

function renderLiveTracks(presence) {
  const tracks = presence.tracks || [];
  const body = $("live-tracks-table").querySelector("tbody");

  if (!tracks.length) {
    body.replaceChildren(el("tr", {}, [
      el("td", { colspan: "6", class: "muted",
                 text: "The camera is not tracking anybody right now." })
    ]));
  } else {
    body.replaceChildren(...tracks.map((track) =>
      el("tr", {}, [
        el("td", { text: String(track.track_id) }),
        /* An unidentified person is shown as UNKNOWN. No name is invented,
           and no nearest guess is offered. */
        el("td", {}, [
          track.student_id
            ? el("span", {}, [
                el("strong", { text: track.student_id }),
                el("span", { class: "muted", text: " " + (track.student_name || "") })
              ])
            : statusPill("UNKNOWN")
        ]),
        el("td", {}, [statusPill(track.liveness_state)]),
        el("td", {}, [statusPill(track.authenticity_state)]),
        el("td", {}, [statusPill(track.presence_state)]),
        el("td", {}, [eligibilityCell(track.attendance_eligibility)])
      ])));
  }

  const count = presence.unknown_tracks;
  $("live-unverified").replaceChildren(
    el("div", { class: "unverified-count", text: String(count) }),
    el("div", { class: "muted", text: count === 1
      ? "person being tracked without a confirmed identity"
      : "people being tracked without a confirmed identity" }));

  $("live-liveness-note").textContent = presence.liveness_available === false
    ? "The liveness check is unavailable, so every track is reported as " +
      "undecided. Nothing is assumed to be live."
    : (presence.liveness_note || "");

  /* Authenticity carries a longer notice than liveness, because the leap it
     has to prevent - reading SUSPICIOUS as "a deepfake was detected" - is a
     bigger one. */
  $("live-authenticity-note").textContent =
    presence.authenticity_mode === "off"
      ? ""
      : (presence.authenticity_available === false
          ? "Authenticity analysis is unavailable, so every track is " +
            "reported as undecided. Nothing is assumed to be authentic."
          : (presence.authenticity_note || ""));
}

async function loadLive() {
  /*
     One poll, two jobs.

     /api/live/sessions carries every running session's runtime in a single
     request, which is what the panels are built from - a timer per panel would
     multiply requests by the number of classrooms for no extra information.

     The status and presence calls below are the SINGLE-session view the
     existing controls use, and the focused panel's presence list. They are
     unscoped on purpose: with one class running that is the class, which is
     what /api/live/status has always meant.
  */
  await loadActiveLiveSessions();

  const status = await api("/api/live/status");
  renderLiveStatus(status);
  renderLivePresence(await api("/api/live/presence"));
}

function startLivePolling() {
  stopLivePolling();
  state.liveTimer = window.setInterval(() => {
    loadLive().catch((failure) => liveError(failure.message));
  }, LIVE_POLL_MS);
}

function stopLivePolling() {
  if (state.liveTimer !== null) {
    window.clearInterval(state.liveTimer);
    state.liveTimer = null;
  }
}

async function startLive() {
  const sessionId = $("live-session").value;
  if (!sessionId) {
    liveError("Select a scheduled or active session first.");
    return;
  }

  const cameraText = $("live-camera").value.trim();
  const deviceId = $("live-camera-device").value;

  const body = { session_id: sessionId };

  /* Both may be sent. The server uses camera_index when it is given and the
     registered device's own index otherwise - see LiveStartRequest - so a
     typed index is an override rather than a conflict. */
  if (deviceId) body.camera_id = deviceId;
  if (cameraText !== "") body.camera_index = Number(cameraText);

  state.liveCamera = deviceId;

  state.liveBusy = true;
  liveError("");
  setBusy(true);

  try {
    $("live-summary").hidden = true;
    renderLiveStatus(await apiSend("/api/live/start", "POST", body));
    state.liveSession = sessionId;
  } catch (failure) {
    liveError(failure.message);
  } finally {
    state.liveBusy = false;
    setBusy(false);
    loadLive().catch(() => { /* the next poll retries */ });
  }
}

/*
  Ending a lesson before the bell.

  The confirmation is not ceremony: ending early used to cost attendance,
  because a presence RATIO measured against the full scheduled hour makes a
  student who sat through all 35 minutes of a 35-minute lesson look 58%
  present. It no longer does - finalisation measures over the observed window
  - and the dialog says so, because a teacher about to press Stop 25 minutes
  early is entitled to know what happens to the register.
*/
async function stopLive() {
  const session = scheduledSession(state.liveRunningSession ||
                                   state.liveSession);

  /* Only ask when it really is early. Ending on time or late is the ordinary
     case and does not deserve a dialog. */
  if (session && session.end_time &&
      new Date(session.end_time) > new Date()) {
    return openEarlyEndDialog(session);
  }

  return finishLive();
}

function openEarlyEndDialog(session) {
  $("early-end-meta").replaceChildren(
    metaItem("Session", session.session_id || "—"),
    metaItem("Class", session.class_id || "—"),
    metaItem("Scheduled end", clock(session.end_time)),
    metaItem("Ending now at", clock(new Date().toISOString())));

  $("early-end-dialog").showModal();
}

async function finishLive() {
  state.liveBusy = true;
  setBusy(true);

  try {
    renderLiveSummary(await apiSend("/api/live/stop", "POST", null));
    await loadLiveSessions();
  } catch (failure) {
    liveError(failure.message);
  } finally {
    state.liveBusy = false;
    setBusy(false);
    loadLive().catch(() => { /* the next poll retries */ });
  }
}

function renderLiveSummary(summary) {
  const counts = Object.keys(summary.counts).sort()
    .map((status) => status.replace(/_/g, " ") + " " + summary.counts[status])
    .join(", ");

  $("live-summary-meta").replaceChildren(
    el("dt", { text: "Session" }), el("dd", { text: summary.session_id }),
    el("dt", { text: "Session status" }), el("dd", {}, [statusPill(summary.session_status)]),
    el("dt", { text: "Policy version" }), el("dd", { text: summary.policy_version }),
    el("dt", { text: "Students evaluated" }), el("dd", { text: String(summary.students_evaluated) }),
    el("dt", { text: "Presence segments stored" }), el("dd", { text: String(summary.segments_saved) }),
    el("dt", { text: "Result" }), el("dd", { text: counts || "no attendance records" })
  );

  $("live-summary-note").textContent =
    (summary.already_finalized
      ? "This session had already been finalised, so nothing was written again. "
      : "") + summary.note;

  $("live-summary").hidden = false;
}

/* ---------- schedule a session ---------- */

/*
  The dashboard could always READ sessions; it could not create one, which
  left the Live classroom tab with nothing to run.

  Two things this deliberately does not do. It does not invent a second
  session system - it posts to the same /api/sessions the Sessions table
  reads, so a session created here is the same object Live classroom, the
  analytics and the attendance engine already understand. And it does not
  calculate anything: scheduling records an intention, and attendance is
  still produced once, by the rule engine, after the session has been run.
*/

function sessionError(message) {
  const node = $("new-session-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function selectedStudents() {
  return Array.from(
    document.querySelectorAll("#new-session-students input:checked")
  ).map((box) => box.value);
}

function updateRosterCount() {
  const chosen = selectedStudents().length;
  const total = document.querySelectorAll("#new-session-students input").length;
  $("roster-count").textContent = total
    ? chosen + " of " + total + " selected"
    : "";
}

async function loadRoster() {
  const container = $("new-session-students");
  container.replaceChildren(el("p", { class: "muted small", text: "Loading…" }));

  let students = [];
  try {
    const page = await api("/api/students?status=ACTIVE&limit=200&offset=0");
    students = page.items;
  } catch (failure) {
    container.replaceChildren(el("p", { class: "form-error",
      text: "Could not load the student list: " + failure.message }));
    return;
  }

  if (!students.length) {
    container.replaceChildren(el("p", { class: "muted small", text:
      "No students are registered yet. Enrol a student before scheduling a " +
      "session, so there is somebody to expect." }));
    updateRosterCount();
    return;
  }

  /* A checkbox per student, each with a real label bound to it - a
     multi-select would be smaller markup and considerably worse to use. */
  container.replaceChildren(...students.map((student) => {
    const id = "roster-" + student.student_id;
    return el("div", { class: "roster-row" }, [
      el("input", { type: "checkbox", id: id, value: student.student_id }),
      el("label", { for: id }, [
        el("strong", { text: student.student_id }),
        el("span", { class: "muted", text: " " + (student.name || "") })
      ])
    ]);
  }));

  container.querySelectorAll("input").forEach((box) => {
    box.addEventListener("change", updateRosterCount);
  });
  updateRosterCount();
}

/* The registered classes, for the scheduling form's suggestions.

   From the STUDENT register rather than from existing sessions, because this
   field picks who is expected and a class nobody is registered in has nobody
   to expect. loadKnownClasses below does the other one - the class identifiers
   sessions have already been recorded against. */
async function loadRegisteredClasses() {
  try {
    const page = await api("/api/students?status=ACTIVE&limit=200&offset=0");
    const names = [...new Set((page.items || [])
      .map((student) => student.class_name).filter(Boolean))].sort();
    $("registered-classes").replaceChildren(
      ...names.map((name) => el("option", { value: name })));
  } catch (failure) {
    /* free-text field; a missing suggestion list costs nothing */
  }
}

/* How many students the server WOULD expect for the chosen class and section.

   A preview of the server's own answer, from the server - not a second
   calculation in the browser that could disagree with it. It also ticks the
   matching roster boxes, so the list underneath always shows exactly who is
   about to be expected and can still be adjusted by hand.

   The inactive count is reported separately on purpose: "nobody is in MECH-A"
   and "seven people are in MECH-A and none of them is active" are different
   problems and need different fixing. */
async function previewExpectedStudents() {
  const className = $("new-session-class-name").value.trim();
  const section = $("new-session-section").value.trim();
  const note = $("new-session-expected");

  if (!className) {
    note.textContent = "";
    note.className = "roster-summary";
    return;
  }

  /* Keep the session identifier in step with the class, unless the teacher has
     typed over it. */
  const identifier = $("new-session-class");
  if (!identifier.dataset.edited) {
    identifier.value = className + (section ? "-" + section : "");
  }

  const query = new URLSearchParams({ class_name: className });
  if (section) query.set("section", section);

  let roster;
  try {
    roster = await api("/api/sessions/expected-students?" + query.toString());
  } catch (failure) {
    note.textContent = failure.message;
    note.className = "roster-summary warn";
    return;
  }

  const where = className + (section ? "-" + section : "");

  if (!roster.expected_count) {
    note.className = "roster-summary warn";
    note.textContent = roster.inactive_count
      ? "No active students in " + where + ". There are " +
        roster.inactive_count + " inactive student(s) there, and an inactive " +
        "student is not expected at a lesson."
      : "No students are registered in " + where + ", so there is nobody to " +
        "expect.";
  } else {
    note.className = "roster-summary ok";
    note.textContent = roster.expected_count +
      " active student(s) will be expected in " + where +
      (roster.inactive_count
        ? " (" + roster.inactive_count + " inactive, not expected)" : "") + ".";
  }

  /* Tick exactly those students in the roster below. */
  const wanted = new Set(roster.expected_students || []);
  $("new-session-students").querySelectorAll("input").forEach((box) => {
    box.checked = wanted.has(box.value);
  });
  updateRosterCount();
}

async function loadKnownClasses() {
  try {
    const classes = await api("/api/classes");
    $("known-classes").replaceChildren(...classes.map((item) =>
      el("option", { value: item.class_id })));
  } catch (failure) {
    /* The field is free text, so a missing suggestion list costs nothing. */
  }
}

async function openSessionDialog() {
  sessionError("");
  $("session-form").reset();

  /* Default to today and the next whole hour: the common case is "a lesson
     later today", and an empty date field makes that three keystrokes. */
  const now = new Date();
  const start = new Date(now.getTime());
  start.setMinutes(0, 0, 0);
  start.setHours(now.getHours() + 1);
  const end = new Date(start.getTime() + 60 * 60 * 1000);

  const pad = (value) => (value < 10 ? "0" + value : String(value));
  const asDate = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  const asTime = (d) => pad(d.getHours()) + ":" + pad(d.getMinutes());

  $("new-session-date").value = asDate(start);
  $("new-session-start").value = asTime(start);
  $("new-session-end").value = asTime(end);

  $("new-session-expected").textContent = "";
  $("new-session-expected").className = "roster-summary";
  delete $("new-session-class").dataset.edited;

  if (state.classId) {
    $("new-session-class").value = state.classId;
    /* Pre-filled from the dashboard filter, so it is not ours to overwrite
       when a class is chosen. */
    $("new-session-class").dataset.edited = "1";
  }

  await loadKnownClasses();
  await loadRegisteredClasses();
  await loadRoster();

  $("session-dialog").showModal();
}

/* Local date + local time -> an ISO instant with an offset.

   The conversion happens here, in the browser, because only the browser knows
   what the teacher's "09:00" means. The API refuses a timestamp with no
   offset rather than guessing one, so an ambiguous time never reaches it. */
function localToIso(dateValue, timeValue) {
  const [year, month, day] = dateValue.split("-").map(Number);
  const [hour, minute] = timeValue.split(":").map(Number);
  const moment = new Date(year, month - 1, day, hour, minute, 0, 0);
  return isNaN(moment.getTime()) ? null : moment.toISOString();
}

async function createSession() {
  const classId = $("new-session-class").value.trim();
  const className = $("new-session-class-name").value.trim();
  const section = $("new-session-section").value.trim();
  const date = $("new-session-date").value;
  const startTime = $("new-session-start").value;
  const endTime = $("new-session-end").value;
  const students = selectedStudents();

  if (!classId) return sessionError("Enter a class identifier.");
  if (!date) return sessionError("Choose a date.");
  if (!startTime) return sessionError("Choose a start time.");
  if (!endTime) return sessionError("Choose an end time.");

  /* Either a ticked list or a class the server can take one from. Naming
     neither is the one combination that cannot produce a session, and the
     server refuses it too - this only says so sooner. */
  if (!students.length && !className) {
    return sessionError(
      "Choose a registered class, or tick at least one expected student. " +
      "Absence is only meaningful against a list of who was expected.");
  }

  const start = localToIso(date, startTime);
  let end = localToIso(date, endTime);
  if (!start || !end) return sessionError("That date and time could not be read.");

  /* An end before the start on the same day means the lesson runs past
     midnight, which is the only reading that makes sense. */
  if (new Date(end) <= new Date(start)) {
    end = new Date(new Date(end).getTime() + 24 * 60 * 60 * 1000).toISOString();
  }

  sessionError("");
  $("session-create").disabled = true;
  setBusy(true);

  try {
    /* The ticked list wins when there is one, so a teacher who adjusted it by
       hand gets what they chose. With none, class_name and section are sent and
       the SERVER resolves the active students - which is the version that
       cannot be a stale copy of the register. */
    const created = await apiSend("/api/sessions", "POST", {
      class_id: classId,
      start_time: start,
      end_time: end,
      expected_students: students.length ? students : null,
      class_name: className || null,
      section: section || null
    });

    $("session-dialog").close();

    /* Show it where the teacher is looking, and make it runnable straight
       away without a page reload. */
    state.sessionOffset = 0;
    await loadSessions();
    await loadClasses();
    state.liveSession = created.session_id;
    try { await loadLiveSessions(); } catch (failure) { /* the live tab retries */ }

    /* On the session DETAIL, expected_students is the list of ids - it is only
       the session LIST that summarises it as a count. Reading it as a number
       printed "E-A1,E-A2,E-A3 expected student(s)". */
    const expectedCount = Array.isArray(created.expected_students)
    ? created.expected_students.length
    : created.expected_students;

    banner("Session " + created.session_id + " scheduled for " +
           created.session_date + " with " + expectedCount +
           " expected student(s). Open the Live classroom tab to run it.");
  } catch (failure) {
    sessionError(failure.message);
  } finally {
    $("session-create").disabled = false;
    setBusy(false);
  }
}

/* ---------- add, edit and remove a student ---------- */

/*
   Enrollment runs on the SERVER, against the camera attached to the machine
   running it. The browser sends the student's details, then polls counters.

   It never uploads a frame and never receives one. Nothing below reaches for
   a media element or a browser camera API, and no endpoint could serve a
   picture if this file asked. What comes back is a state, two numbers and a
   sentence - and a test asserts that by searching this section for the names
   of those APIs, which is why they are described rather than spelled here.

   "Save & Enroll Face" registers the student FIRST and then starts the camera.
   That ordering is deliberate: a cancelled or failed capture then leaves a
   registered student who is simply shown as not enrolled - true, visible, and
   finishable with one more click - rather than losing everything the teacher
   just typed.
*/

const ENROLL_POLL_MS = 700;
const ENROLL_BUSY_STATES = ["STARTING", "CAPTURING", "SAVING"];

const ENROLL_SYMBOL = {
  IDLE: "○", STARTING: "◑", CAPTURING: "●", SAVING: "◑",
  COMPLETED: "✓", FAILED: "✕", CANCELLED: "✕"
};

function studentError(message) {
  const node = $("new-student-error");
  node.textContent = message || "";
  node.hidden = !message;
}

/* A teacher is not shown a guardian's contact and cannot write one, so the
   whole group is hidden for them rather than shown full of nulls. The server
   enforces this too - hiding a fieldset is a courtesy, not a permission. */
function showParentFields(visible) {
  ["new-student-parent-group", "edit-student-parent-group"].forEach((id) => {
    const group = $(id);
    if (group) group.hidden = !visible;
  });
}

function studentDetails() {
  const details = {
    student_id: $("new-student-id").value.trim(),
    name: $("new-student-name").value.trim(),
    roll_number: $("new-student-roll").value.trim(),
    academic_year: $("new-student-year").value.trim(),
    class_name: $("new-student-class").value.trim(),
    section: $("new-student-section").value.trim(),
    notification_enabled: $("new-student-notify").checked
  };

  /* Guardian contact is optional, and only a Super Admin may write it. An
     empty field is omitted rather than sent as "", so a blank box never blanks
     something that was already recorded. */
  if (state.auth.is_super_admin) {
    ["name", "phone", "email"].forEach((part) => {
      const value = $("new-student-parent-" + part).value.trim();
      if (value) details["parent_" + part] = value;
    });
  }

  return details;
}

/* The five registry fields are all required, because a half-filled student is
   what made the registry unusable in the first place. */
function missingStudentField(details) {
  if (!details.student_id) return "Enter a student ID.";
  if (!details.name) return "Enter the student's name.";
  if (!details.roll_number) return "Enter a roll number.";
  if (!details.class_name) return "Enter a class.";
  if (!details.section) return "Enter a section.";
  return null;
}

function openStudentDialog() {
  studentError("");
  showEnrollmentView(false);
  $("student-form").reset();
  $("student-dialog-title").textContent = "Add a student";
  $("new-student-id").readOnly = false;
  $("enroll-progress").hidden = true;
  $("student-cancel-enroll").hidden = true;
  $("student-enroll").disabled = false;
  $("student-register-only").disabled = false;
  $("student-register-only").hidden = false;
  state.enrollAfterSave = false;
  $("new-student-notify").checked = true;
  showParentFields(state.auth.is_super_admin);
  $("student-dialog").showModal();
}

/* Enrolling somebody who is already registered: same dialog, same code path,
   with the registry fields filled in and locked to what the registry says. */
async function enrollExistingStudent(studentId) {
  let student;
  try {
    student = await api("/api/students/" + encodeURIComponent(studentId));
  } catch (failure) {
    return banner(failure.message, true);
  }

  openStudentDialog();
  $("student-dialog-title").textContent = "Enroll " + student.name;
  $("new-student-id").value = student.student_id;
  $("new-student-id").readOnly = true;
  $("new-student-name").value = student.name;
  $("new-student-roll").value = student.roll_number || "";
  $("new-student-year").value = student.academic_year || "";
  $("new-student-class").value = student.class_name || "";
  $("new-student-section").value = student.section || "";
  $("student-register-only").hidden = true;
}

/* The same arrangement for enrollment: set the stream up once when capture
   begins, tear it down when it ends. */
function showEnrollmentView(busy) {
  const view = $("enroll-view");
  const panel = $("enroll-camera");

  if (busy && !view.dataset.streaming) {
    view.src = "/api/students/enrollment/stream.mjpg?t=" + Date.now();
    view.dataset.streaming = "1";
    view.hidden = false;
    $("enroll-camera-idle").hidden = true;
    panel.hidden = false;
  }

  if (!busy && view.dataset.streaming) {
    view.removeAttribute("src");
    delete view.dataset.streaming;
    view.hidden = true;
    $("enroll-camera-idle").hidden = false;
    panel.hidden = true;
  }
}

function renderEnrollment(status) {
  const busy = ENROLL_BUSY_STATES.indexOf(status.state) >= 0;
  const progress = $("enroll-progress");

  showEnrollmentView(busy);

  progress.hidden = status.state === "IDLE";
  progress.className = "enroll-progress enroll-" + status.state;

  $("enroll-symbol").textContent = ENROLL_SYMBOL[status.state] || "•";
  $("enroll-state").textContent = status.state.charAt(0) +
  status.state.slice(1).toLowerCase();

  $("enroll-count").textContent = status.required
  ? "Sample " + status.captured + " / " + status.required
  : "";

  const share = status.required
  ? Math.min(100, (status.captured / status.required) * 100)
  : 0;
  $("enroll-bar").style.width = share.toFixed(0) + "%";

  $("enroll-message").textContent = status.message || "";

  $("student-enroll").disabled = busy;
  $("student-register-only").disabled = busy;
  $("student-cancel-enroll").hidden = !busy;

  if (status.state === "FAILED" && status.error) studentError(status.error);
}

function stopEnrollPolling() {
  if (state.enrollTimer !== null) {
    window.clearInterval(state.enrollTimer);
    state.enrollTimer = null;
  }
}

async function pollEnrollment() {
  let status;
  try {
    status = await api("/api/students/enrollment/status");
  } catch (failure) {
    stopEnrollPolling();
    return studentError(failure.message);
  }

  renderEnrollment(status);

  if (ENROLL_BUSY_STATES.indexOf(status.state) >= 0) return;

  stopEnrollPolling();

  /* The student changed in both stores, so every list that shows students -
     including the session dialog's roster - is stale. */
  await refreshAfterEnrollment();

  if (status.state === "COMPLETED") {
    banner(status.message || "Student enrolled successfully.");
    closeStudentDialog();
  }
}

async function refreshAfterEnrollment() {
  state.studentOffset = 0;
  try { await loadStudents(); } catch (failure) { /* the view retries */ }
  try { await loadRoster(); } catch (failure) { /* the dialog retries */ }
}

/* Save & Enroll Face. Two requests, in an order that cannot lose data:
   register, then capture. */
async function startEnrollment() {
  const details = studentDetails();

  const missing = missingStudentField(details);
  if (missing) return studentError(missing);

  const cameraText = $("new-student-camera").value.trim();

  studentError("");
  $("student-enroll").disabled = true;
  setBusy(true);

  try {
    await ensureRegistered(details);

    const request = Object.assign({}, details);
    if (cameraText !== "") request.camera_index = Number(cameraText);

    renderEnrollment(await apiSend("/api/students/enrollment/start", "POST",
                                   request));
    stopEnrollPolling();
    state.enrollTimer = window.setInterval(pollEnrollment, ENROLL_POLL_MS);
  } catch (failure) {
    studentError(failure.message);
    $("student-enroll").disabled = false;
    /* The registration may well have succeeded even though the camera did
       not, so the list is refreshed either way. */
    try { await loadStudents(); } catch (ignored) { /* the view retries */ }
  } finally {
    setBusy(false);
  }
}

/* Registers the student, and treats "already exists" as success - this runs
   before every camera enrollment, including re-enrolling somebody who has
   been on the roll for a term. */
async function ensureRegistered(details) {
  try {
    await apiSend("/api/students", "POST", details);
  } catch (failure) {
    if (!/already exists/i.test(failure.message)) throw failure;
  }
}

async function cancelEnrollment() {
  $("student-cancel-enroll").disabled = true;
  try {
    renderEnrollment(await apiSend("/api/students/enrollment/cancel", "POST",
                                   null));
    banner("Enrollment cancelled. No face samples were stored.", true);
  } catch (failure) {
    studentError(failure.message);
  } finally {
    $("student-cancel-enroll").disabled = false;
    stopEnrollPolling();
    try { await loadStudents(); } catch (ignored) { /* the view retries */ }
  }
}

/* Closing the dialog must also stop the camera. Leaving a capture running
   against a webcam nobody is watching is how the live classroom ends up
   unable to start. */
function closeStudentDialog() {
  stopEnrollPolling();
  showEnrollmentView(false);
  const dialog = $("student-dialog");
  if (dialog.open) dialog.close();
}

async function closeStudentDialogAndCancel() {
  const running = !$("student-cancel-enroll").hidden;
  closeStudentDialog();
  if (running) {
    try { await apiSend("/api/students/enrollment/cancel", "POST", null); }
    catch (failure) { /* nothing was stored either way */ }
    try { await loadStudents(); } catch (ignored) { /* the view retries */ }
  }
}

async function registerStudentOnly() {
  const details = studentDetails();

  const missing = missingStudentField(details);
  if (missing) return studentError(missing);

  studentError("");
  $("student-register-only").disabled = true;
  setBusy(true);

  try {
    const created = await apiSend("/api/students", "POST", details);
    await refreshAfterEnrollment();
    closeStudentDialog();
    banner(created.student_id + " registered. They cannot be recognised by " +
           "the camera until face enrollment is completed.", true);
  } catch (failure) {
    studentError(failure.message);
  } finally {
    $("student-register-only").disabled = false;
    setBusy(false);
  }
}

/* ---------- edit ---------- */

function editError(message) {
  const node = $("edit-student-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function openStudentEdit(studentId) {
  let student;
  try {
    student = await api("/api/students/" + encodeURIComponent(studentId));
  } catch (failure) {
    return banner(failure.message, true);
  }

  state.editing = student;
  editError("");

  $("student-edit-title").textContent = "Edit " + student.name;
  $("edit-student-id").value = student.student_id;
  $("edit-student-name").value = student.name;
  $("edit-student-roll").value = student.roll_number || "";
  $("edit-student-year").value = student.academic_year || "";
  $("edit-student-class").value = student.class_name || "";
  $("edit-student-section").value = student.section || "";
  $("edit-student-status").value = student.status;

  /* null means either nothing is on file or this caller may not see it. Both
     show as empty, and for a teacher the group is hidden entirely. */
  $("edit-student-parent-name").value = student.parent_name || "";
  $("edit-student-parent-phone").value = student.parent_phone || "";
  $("edit-student-parent-email").value = student.parent_email || "";
  $("edit-student-notify").checked = student.notification_enabled !== false;
  showParentFields(state.auth.is_super_admin);

  $("edit-student-enrollment").textContent = student.enrolled
  ? "Face enrollment: " + student.sample_count + " samples (" +
  (student.model_name || "model") + " " + (student.model_version || "") +
  "). Editing these details does not touch them."
  : "Face enrollment: none. This student cannot be recognised by the " +
  "camera yet.";

  $("edit-student-enroll").textContent = student.enrolled
  ? "Re-enroll face" : "Enroll face";

  $("student-edit-dialog").showModal();
}

async function saveStudentEdit() {
  if (!state.editing) return;

  const body = {
    name: $("edit-student-name").value.trim(),
    roll_number: $("edit-student-roll").value.trim(),
    academic_year: $("edit-student-year").value.trim(),
    class_name: $("edit-student-class").value.trim(),
    section: $("edit-student-section").value.trim(),
    status: $("edit-student-status").value,
    notification_enabled: $("edit-student-notify").checked
  };

  /* Only a Super Admin sends contact fields at all. Sending them as a teacher
     would be a 403, and sending an emptied box as one would be asking to blank
     a number this caller was never shown. */
  if (state.auth.is_super_admin) {
    body.parent_name = $("edit-student-parent-name").value.trim();
    body.parent_phone = $("edit-student-parent-phone").value.trim();
    body.parent_email = $("edit-student-parent-email").value.trim();
  }

  if (!body.name) return editError("Enter the student's name.");

  editError("");
  setBusy(true);

  try {
    const updated = await apiSend(
      "/api/students/" + encodeURIComponent(state.editing.student_id),
      "PATCH", body);
    $("student-edit-dialog").close();
    banner(updated.student_id + " updated.");
    await loadStudents();
    try { await loadRoster(); } catch (ignored) { /* the dialog retries */ }
  } catch (failure) {
    editError(failure.message);
  } finally {
    setBusy(false);
  }
}

/* ---------- deactivate or delete ---------- */

function removeError(message) {
  const node = $("student-remove-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function openStudentRemove(studentId) {
  let student;
  try {
    student = await api("/api/students/" + encodeURIComponent(studentId));
  } catch (failure) {
    return banner(failure.message, true);
  }

  state.removing = student;
  removeError("");

  $("student-remove-who").textContent =
  student.name + " (" + student.student_id + ")" +
  (student.class_name ? " — " + student.class_name +
   (student.section ? "-" + student.section : "") : "");

  const summary = student.attendance || {};
  $("student-remove-impact").replaceChildren(
    el("dl", { class: "evidence-row" }, [
      el("dt", { text: "Attendance records" }),
      el("dd", { text: String(summary.recorded_sessions || 0) })
    ]),
    el("dl", { class: "evidence-row" }, [
      el("dt", { text: "Expected in sessions" }),
      el("dd", { text: String(summary.expected_sessions || 0) })
    ]),
    el("dl", { class: "evidence-row" }, [
      el("dt", { text: "Face samples" }),
      el("dd", { text: student.enrolled
                ? String(student.sample_count) : "none" })
    ]));

  $("student-deactivate").hidden = student.status === "INACTIVE";

  /*
     Permanent deletion is Super Admin only, and the SERVER is what enforces
     that - DELETE /api/students/{id} depends on require_super_admin, so a
     teacher calling it by hand gets 403 whether or not this button is here.
     Hiding it is a courtesy, not a permission: it stops a teacher being shown
     a control that can only ever fail, and it says why.

     Deactivate is deliberately left alone. It is the operation a class teacher
     actually needs when somebody leaves, it loses nothing, and taking it away
     would turn a narrowing of one destructive endpoint into a loss of everyday
     classroom work.
  */
  const mayDelete = state.auth.is_super_admin;
  $("student-delete").hidden = !mayDelete;
  $("student-delete-note").hidden = mayDelete;
  $("student-delete-explainer").hidden = !mayDelete;

  $("student-remove-dialog").showModal();
}

async function deactivateStudent() {
  if (!state.removing) return;
  setBusy(true);
  try {
    await apiSend("/api/students/" +
                  encodeURIComponent(state.removing.student_id) + "/status?status=INACTIVE",
                  "POST", null);
    $("student-remove-dialog").close();
    banner(state.removing.student_id +
           " deactivated. Their attendance history is unchanged.");
    await loadStudents();
  } catch (failure) {
    removeError(failure.message);
  } finally {
    setBusy(false);
  }
}

/*
   Delete refuses by default when there is history to lose, and the refusal
   says exactly what would go. Going through with it requires typing the
   student's id.

   A button alone was not enough. One click on a control already labelled
   "Delete permanently" destroyed a real student's term of attendance during
   development - the roll, five face samples, three attendance records and five
   presence segments, all of which cascade from students(student_id). Typing
   the id is the ordinary protection for an irreversible destruction, and a
   stray double-click cannot produce it.
*/
async function deleteStudent() {
  if (!state.removing) return;

  const studentId = state.removing.student_id;
  const typed = $("student-delete-confirm").value.trim();
  const needsConfirmation = !$("student-delete-confirm-field").hidden;

  if (needsConfirmation && typed !== studentId) {
    return removeError("Type " + studentId + " exactly to confirm permanent " +
                       "deletion, or choose Deactivate to keep the history.");
  }

  setBusy(true);

  try {
    await apiSend("/api/students/" + encodeURIComponent(studentId) +
                  (needsConfirmation
                   ? "?confirm=" + encodeURIComponent(typed) : ""),
                  "DELETE", null);
    $("student-remove-dialog").close();
    banner(studentId + " deleted, along with any face samples.", true);
    await loadStudents();
    try { await loadRoster(); } catch (ignored) { /* the dialog retries */ }
  } catch (failure) {
    if (!needsConfirmation && /history/i.test(failure.message)) {
      /* There IS history. Show what would be lost and ask for the id. */
      removeError(failure.message);
      $("student-delete-id").textContent = studentId;
      $("student-delete-confirm-field").hidden = false;
      $("student-delete").textContent = "Delete permanently";
      $("student-delete-confirm").focus();
    } else {
      removeError(failure.message);
    }
  } finally {
    setBusy(false);
  }
}

/* ---------- cancel or delete a session ---------- */
/*
   Two different operations behind one Remove button, and the difference is the
   whole point.

   Cancel   keeps the row and everything attached to it. The lesson stops
   behaving like a lesson. Nothing is lost.
   Delete   removes the row, and session_expected_students,
   attendance_records and presence_segments all cascade from it -
   so deleting a session that ran takes its register with it.

   WHICH of the two is offered is NOT decided here. The server computes
   available_actions from the session's status and correction state, and this
   code renders exactly that set. A browser deciding a session is safe to
   delete would be a second copy of the policy, free to disagree with the one
   the API actually enforces - and the disagreement would only ever be found
   out by destroying a term of attendance.

   The typed id is the same protection the student dialog uses, for the same
   reason: a button labelled "Delete permanently" is one stray double-click
   away from an irreversible loss, and a typed identifier is not.
*/

function sessionRemoveError(message) {
  const node = $("session-remove-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function evidenceRow(label, value) {
  return el("dl", { class: "evidence-row" }, [
    el("dt", { text: label }),
    el("dd", { text: String(value) })
  ]);
}

function metaRow(label, value) {
  return el("div", null, [
    el("dt", { text: label }),
    el("dd", { text: value === null || value === undefined || value === ""
              ? "—" : String(value) })
  ]);
}

async function openSessionRemoveDialog(sessionId) {
  let details;
  try {
    details = await api("/api/sessions/" + encodeURIComponent(sessionId) +
                        "/details");
  } catch (failure) {
    return banner(failure.message, true);
  }

  state.removingSession = details;
  sessionRemoveError("");

  $("session-remove-who").textContent = details.session_id;

  $("session-remove-meta").replaceChildren(
    metaRow("Date", details.session_date),
    metaRow("Class", details.class_id),
    metaRow("Section", details.section),
    metaRow("Teacher", details.teacher),
    metaRow("Scheduled", clock(details.start_time) + " – " +
            clock(details.end_time)),
    metaRow("Status", details.status));

  const evidence = details.evidence || {};
  $("session-remove-impact").replaceChildren(
    evidenceRow("Attendance records", evidence.attendance_records || 0),
    evidenceRow("Presence segments", evidence.presence_segments || 0),
    evidenceRow("Expected students", evidence.expected_students || 0));

  /* The server's own list, rendered as-is. */
  const actions = details.available_actions || [];
  $("session-cancel-action").hidden = !actions.includes("cancel");
  $("session-delete").hidden = !actions.includes("delete");
  $("session-cancel-reason-field").hidden = !actions.includes("cancel");

  /* A session the server offers neither action for still opens, and says so,
     rather than presenting an empty dialog. */
  if (!actions.includes("cancel") && !actions.includes("delete")) {
    sessionRemoveError(
      "This session is " + details.status + " and can no longer be cancelled " +
      "or deleted. Its history is kept deliberately.");
  }

  /* Reset the confirmation between openings, so a previous session's typed id
     can never be reused against a different one. */
  $("session-delete-confirm-field").hidden = true;
  $("session-delete-confirm").value = "";
  $("session-delete").textContent = "Delete permanently";

  $("session-remove-dialog").showModal();
}

async function cancelSession() {
  if (!state.removingSession || state.removingBusy) return;

  const sessionId = state.removingSession.session_id;
  const reason = $("session-cancel-reason").value.trim();

  state.removingBusy = true;
  setBusy(true);

  try {
    await apiSend("/api/sessions/" + encodeURIComponent(sessionId) + "/cancel",
                  "POST", { reason: reason || null });
    $("session-remove-dialog").close();
    banner(sessionId + " cancelled. Its records are unchanged.");
    await refreshAfterSessionChange();
  } catch (failure) {
    sessionRemoveError(failure.message);
  } finally {
    state.removingBusy = false;
    setBusy(false);
  }
}

async function deleteSession() {
  if (!state.removingSession || state.removingBusy) return;

  const sessionId = state.removingSession.session_id;
  const typed = $("session-delete-confirm").value.trim();
  const needsConfirmation = !$("session-delete-confirm-field").hidden;

  /* The server demands the typed id too - this only saves a round trip and
     puts the message next to the field. */
  if (needsConfirmation && typed !== sessionId) {
    return sessionRemoveError(
      "Type " + sessionId + " exactly to confirm permanent deletion, or " +
      "choose Cancel session to keep the records.");
  }

  state.removingBusy = true;
  setBusy(true);

  try {
    await apiSend("/api/sessions/" + encodeURIComponent(sessionId) +
                  (needsConfirmation
                   ? "?confirm=" + encodeURIComponent(typed) : ""),
                  "DELETE", null);
    $("session-remove-dialog").close();
    banner(sessionId + " deleted permanently.", true);
    await refreshAfterSessionChange();
  } catch (failure) {
    /* The first attempt carries no confirmation on purpose: the server
       answers either "it would destroy attendance" (409) or "type the id"
       (400), and both are turned into the same next step - show what is at
       stake and ask for the id. */
    if (!needsConfirmation) {
      sessionRemoveError(failure.message);
      $("session-delete-id").textContent = sessionId;
      $("session-delete-confirm-field").hidden = false;
      $("session-delete-confirm").focus();
    } else {
      sessionRemoveError(failure.message);
    }
  } finally {
    state.removingBusy = false;
    setBusy(false);
  }
}

/* A deleted or cancelled session changes the list, the counts on the overview
   and every analytic derived from it, so all of them are reloaded rather than
   patched in place. */
async function refreshAfterSessionChange() {
  await loadSessions();
  try {
    await loadClasses();
  } catch (ignored) { /* the filter keeps its current options */ }
  try {
    await loadOverview();
  } catch (ignored) { /* the overview retries on its next visit */ }
  try {
    await loadRoster();
  } catch (ignored) { /* the scheduling dialog retries */ }
}

/* ---------- sign in ---------- */

function authError(message) {
  const node = $("auth-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function showAuthScreen(message) {
  document.body.classList.add("locked");
  $("auth-screen").hidden = false;
  startAuthScene();

  const first = state.authMode === "setup";
  $("auth-title").textContent = first
  ? "Create your Super Admin account" : "Sign in";
  $("auth-subtitle").textContent = first ? "First run" : "Sign in";
  $("auth-intro").textContent = first
  ? "This installation has no accounts yet, so there is nothing to sign in " +
  "with. Choose your own username and password below - ClassVision AI " +
  "ships no default credentials, which is why this step exists. You are " +
  "signed in immediately afterwards."
  : "Use the account your administrator created for you.";
  $("auth-submit").textContent = first ? "Create administrator" : "Sign in";
  $("auth-name-field").hidden = !first;
  $("auth-display-name").required = first;
  $("auth-password-hint").hidden = !first;
  /* Only on the sign-in screen. During first run there is no password to have
     lost, and offering a reset there would be confusing rather than helpful. */
  $("auth-recover").hidden = first;
  $("auth-recover").open = false;
  $("auth-password").autocomplete = first ? "new-password" : "current-password";

  authError(message || "");
  $("auth-username").focus();
}

function hideAuthScreen() {
  document.body.classList.remove("locked");
  $("auth-screen").hidden = true;
  stopAuthScene();
  announceConsole();
}

/*
   The console arrives.

   Removing body.locked is what actually repaints the interface from the
   gate's violet to the console's cyan - that is CSS and it needs no help.
   This adds the one-beat entrance on top of it, and then takes the class
   back off, because an animation class left on the body would replay every
   time one of these elements is re-shown for the rest of the session.
*/
function announceConsole() {
  const body = document.body;
  if (prefersReducedMotion()) return;

  body.classList.add("booting");
  window.setTimeout(() => body.classList.remove("booting"), 900);
}

/*
   The sign-in card leans towards the pointer.

   Two angles written into custom properties; the transform that reads them
   is in the stylesheet, which is where the decision about how far to lean
   belongs. Capped at a few degrees: the card holds a form somebody is typing
   into, and a label that swings is a label that is briefly unreadable.

   Not bound at all under prefers-reduced-motion - no listener, not a
   listener that does nothing - and cleared on leave so the card settles
   square rather than wherever the pointer left it.
*/
function bindGateLean() {
  const card = document.querySelector(".auth-card");
  if (!card || prefersReducedMotion()) return;
  if (!window.matchMedia || !window.matchMedia("(hover: hover)").matches) return;

  const LIMIT = 4.5;

  card.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") return;
    const box = card.getBoundingClientRect();
    if (!box.width || !box.height) return;

    const fromCentreX = (event.clientX - box.left) / box.width - 0.5;
    const fromCentreY = (event.clientY - box.top) / box.height - 0.5;

    card.style.setProperty("--lean-y", (fromCentreX * LIMIT * 2).toFixed(2) + "deg");
    card.style.setProperty("--lean-x", (-fromCentreY * LIMIT * 2).toFixed(2) + "deg");
  });

  card.addEventListener("pointerleave", () => {
    card.style.removeProperty("--lean-x");
    card.style.removeProperty("--lean-y");
  });
}

/*
   Initials for the rail's avatar.

   Deliberately not a photograph. Staff photos exist in this product and are
   shown on the administration page, but the rail is on screen all day in a
   room full of students, and putting somebody's face there permanently is a
   privacy decision nobody asked for.

   Two letters from two words, or the first two of a single word. Upper-cased
   here rather than in CSS, because text-transform would also shout a name
   that was already an acronym.
*/
function initials(name) {
  const words = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "\u2014";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/*
   WHICH RAIL ITEMS THIS ACCOUNT MAY OPEN.

   One list - ADMIN_VIEWS - drives the tabs, the group heading above them and
   the redirect in renderAccount, so a destination cannot be hidden in one of
   the three and left reachable in the others. Every one of these is refused
   by the server as well; this is tidiness, not a permission.

   The group label is hidden with its items, because a heading over nothing is
   how a rail ends up with a gap in it.
*/
function renderAdminNav() {
  const allowed = state.auth.configured && state.auth.is_super_admin;

  ADMIN_VIEWS.forEach((view) => {
    const tab = document.querySelector('.tab[data-view="' + view + '"]');
    if (tab) tab.hidden = !allowed;
  });

  const group = $("nav-group-admin-wrap");
  if (group) group.hidden = !allowed;
}

function renderAccount() {
  const user = state.auth.user;
  const account = $("account");

  account.hidden = !user;
  renderAdminNav();

  if (!user) {
    $("sidebar-user-name").textContent = "Not signed in";
    $("sidebar-user-role").textContent = "Teacher / Admin";
    $("sidebar-user-avatar").textContent = "\u2014";
    return;
  }

  $("account-name").textContent = user.display_name || user.username;
  $("account-role").textContent =
  (user.role === "SUPER_ADMIN" ? "Super Admin" : "Teacher") +
  (user.assignments && user.assignments.length
   ? " · " + user.assignments.map(describeAssignment).join(", ")
   : "");
  const who = user.display_name || user.username;
  $("sidebar-user-name").textContent = who;
  $("sidebar-user-role").textContent =
  user.role === "SUPER_ADMIN" ? "Super Admin" : "Teacher";
  $("sidebar-user-avatar").textContent = initials(who);

  /* A teacher who lands on a hidden tab would otherwise see an empty page. */
  if (ADMIN_VIEWS.includes(state.view) && !state.auth.is_super_admin) {
    switchView("overview");
  }
}

function describeAssignment(assignment) {
  return assignment.class_name +
  (assignment.section ? "-" + assignment.section : "");
}

async function submitAuth(event) {
  event.preventDefault();

  const username = $("auth-username").value.trim();
  const password = $("auth-password").value;
  const displayName = $("auth-display-name").value.trim();

  if (!username || !password) return authError("Enter a username and password.");

  authError("");
  $("auth-submit").disabled = true;

  /* The student door. A portal credential is not in `users`, so it goes to
     its own endpoint - trying it against the staff one would refuse a
     perfectly good password. */
  if (state.authAudience === "student") {
    try {
      await portalSignIn(username, password);
    } catch (failure) {
      authError(failure.message);
    } finally {
      $("auth-submit").disabled = false;
    }
    return;
  }

  try {
    const user = state.authMode === "setup"
    ? await apiSend("/api/auth/setup", "POST",
                    { username, display_name: displayName || username,
                     password })
    : await apiSend("/api/auth/login", "POST", { username, password });

    $("auth-password").value = "";
    state.auth = { configured: true, authenticated: true, user,
                  is_super_admin: user.role === "SUPER_ADMIN",
                  unrestricted: user.role === "SUPER_ADMIN" ||
                  !(user.assignments || []).length };
    hideAuthScreen();
    renderAccount();
    await startDashboard();
  } catch (failure) {
    authError(failure.message);
  } finally {
    $("auth-submit").disabled = false;
  }
}

async function signOut() {
  try { await apiSend("/api/auth/logout", "POST", null); }
  catch (failure) { /* the cookie is cleared regardless */ }

  stopLivePolling();
  stopEnrollPolling();
  /* Was missing: the camera poll kept firing after sign-out, took a 401, and
     bounced through handleUnauthorized a second time. */
  stopCameraPolling();

  state.auth = Object.assign({}, state.auth,
                             { authenticated: false, user: null });
  state.authMode = "login";
  clearFetchedData();
  renderAccount();
  showAuthScreen("Signed out.");
}

/*
  Empty every region holding data this account fetched.

  The server is the boundary and it refuses the next request either way, so
  this is not what makes sign-out secure. It is what makes it HONEST: without
  it the previous teacher's register is still sitting in the DOM behind the
  sign-in screen, and dismissing that screen - or simply pressing Escape -
  shows a class list to whoever is at the keyboard now.

  Swept generically rather than by listing panels, because a list of panels
  goes stale the first time somebody adds one and the thing left behind is a
  student's name.
*/
function clearFetchedData() {
  const main = document.querySelector("main:not(#portal-main)");
  if (!main) return;

  main.querySelectorAll("tbody").forEach((body) => body.replaceChildren());
  main.querySelectorAll(".cards, .metric-grid, .status-grid, .meta, "
                        + "#report-summary, #class-summary-totals, "
                        + "#term-summary-list, #seatmap-grid, "
                        + "#promotion-summary, #assistant-answer, "
                        + "#today-sessions, #live-strip, #attention-body, "
                        + "#timeline, #student-chart, #cameras-cards")
      .forEach((region) => region.replaceChildren());

  /* Collapse the detail panels too - they hold one person each. Taken from
     DETAIL_PANELS so a panel added there cannot be forgotten here. */
  Object.keys(DETAIL_PANELS).forEach((owner) => {
    DETAIL_PANELS[owner].forEach((id) => {
      const panel = $(id);
      if (panel) panel.hidden = true;
    });
  });

  /* And the trail that led to them: a signed-out session has no "back". */
  state.viewStack = [];
  renderBack();

  state.session = null;
  state.record = null;
  state.editing = null;
}

async function changeOwnPassword() {
  const node = $("password-error");
  node.hidden = true;

  try {
    await apiSend("/api/auth/password", "POST", {
      current_password: $("password-current").value,
      new_password: $("password-new").value
    });
    $("password-dialog").close();
    $("password-form").reset();
    state.authMode = "login";
    state.auth = Object.assign({}, state.auth,
                               { authenticated: false, user: null });
    renderAccount();
    showAuthScreen("Password changed. Sign in with the new one.");
  } catch (failure) {
    node.textContent = failure.message;
    node.hidden = false;
  }
}

/* ---------- camera health ---------- */
/*
   One GET of /api/cameras, rendered three ways: a headline for the device that
   matters right now, the live pipeline's own view of itself, and the full table
   of registered devices.

   Everything shown here comes from that one response. This page opens no
   camera, requests no frame and computes no health of its own - a browser
   deciding whether a classroom camera is well would be a second, disagreeing
   source of truth. Blanks stay blank: a device that reported no frame rate is
   shown with a dash, never with a plausible number.
*/

const CAMERA_POLL_MS = 7000;

/* The states the backend actually writes. `unknown` is the column default;
   `online` and `offline` come from a heartbeat. `degraded` is accepted in case
   a device reports it, but nothing in this project invents one. */
const CAMERA_SYMBOL = {
  online: "●",     // filled circle
  offline: "✕",    // cross
  degraded: "▲",   // triangle
  unknown: "?"
};

const DASH = "—";

function cameraStatusPill(status) {
  const raw = (status || "").trim();
  if (!raw) return el("span", { class: "muted", text: DASH });

  const key = raw.toLowerCase();
  const known = Object.prototype.hasOwnProperty.call(CAMERA_SYMBOL, key);

  /* The symbol matters: status must never be carried by colour alone. */
  return el("span", {
    class: "pill pill-cam-" + (known ? key : "unknown"),
    title: "Reported status: " + raw
  }, [
    el("span", { class: "sym", "aria-hidden": "true",
                text: CAMERA_SYMBOL[key] || "•" }),
    raw.toUpperCase()
  ]);
}

/* A value the hardware may simply not have reported. */
function orDash(value, suffix) {
  if (value === null || value === undefined || value === "") return DASH;
  return suffix ? value + suffix : String(value);
}

function stamp(iso) {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (isNaN(date)) return iso;
  return date.toLocaleString();
}

function camerasState(which, detail) {
  $("cameras-loading").hidden = which !== "loading";
  $("cameras-error").hidden = which !== "error";
  $("cameras-body").hidden = which !== "ready";
  if (which === "error") $("cameras-error-detail").textContent = detail || "";
}

/* The device a reader most likely means: the one the live pipeline is using if
   a session is running, otherwise the first registered device. */
function headlineCamera(body) {
  const items = body.items || [];
  const runningIndex = body.live ? body.live.camera_index : null;

  if (runningIndex !== null && runningIndex !== undefined) {
    const match = items.find((item) => item.camera_index === runningIndex);
    if (match) return match;
  }
  return items[0] || null;
}

function renderCameraHeadline(body) {
  const camera = headlineCamera(body);
  const host = $("cameras-headline");

  if (!camera) {
    host.replaceChildren(el("p", {
      class: "muted",
      text: "No camera device is registered, so there is no health to report."
    }));
    return;
  }

  const where = [
    camera.classroom ? "Classroom " + camera.classroom : null,
    camera.camera_index === null || camera.camera_index === undefined
    ? null : "camera index " + camera.camera_index,
    "device " + camera.camera_id
  ].filter(Boolean).join(" · ");

  host.replaceChildren(
    el("div", { class: "cam-headline-status" }, [
      cameraStatusPill(camera.status),
      el("strong", { class: "cam-headline-name",
                    text: camera.label || camera.camera_id })
    ]),
    el("p", { class: "muted small", text: where })
  );

  if (camera.last_error) {
    host.appendChild(el("p", { class: "form-error", role: "alert",
                              text: "Reported error: " + camera.last_error }));
  }
}

function renderCameraCards(body) {
  const camera = headlineCamera(body) || {};
  const fps = camera.last_fps;
  const latency = camera.last_latency_ms;

  $("cameras-cards").replaceChildren(
    card("Last heartbeat",
         camera.last_heartbeat ? stamp(camera.last_heartbeat) : null),
    card("Frame rate",
         fps === null || fps === undefined ? null : fps + " FPS"),
    card("Latency",
         latency === null || latency === undefined ? null : latency + " ms"),
    card("Live pipeline", (body.live && body.live.state) || null)
  );
}

function renderCameraRuntime(body) {
  const live = body.live || {};
  const rows = [
    ["Pipeline state", orDash(live.state)],
    ["Camera", orDash(live.camera)],
    ["Camera index", live.camera_index === null ||
     live.camera_index === undefined ? DASH : String(live.camera_index)],
    ["Frame rate", live.fps === null || live.fps === undefined
     ? DASH : live.fps + " FPS"],
    ["Frames processed", orDash(live.processed)],
    ["Frames dropped", orDash(live.dropped)],
    ["Last event", stamp(live.last_event_at)]
  ];

  $("cameras-runtime").replaceChildren(...rows.map(([label, value]) =>
                                                   el("div", null, [
                                                     el("dt", { text: label }),
                                                     el("dd", { text: value })
                                                   ])));
}

/* A health value the device has not reported.

   The words "No data", not a dash and never a zero. A dash reads as "nothing
   to say"; a zero reads as a measurement, and "0 fps" is the single most
   misleading thing this table could print about a camera nobody has heard
   from. A registered device with no heartbeat has no health, and the cell says
   exactly that. */
function reported(value, format) {
  if (value === null || value === undefined || value === "") {
    return el("span", { class: "muted nodata", text: "No data" });
  }
  return document.createTextNode(format ? format(value) : String(value));
}

function renderCameraTable(body) {
  const items = body.items || [];
  const rows = $("cameras-table").querySelector("tbody");
  const admin = state.auth.is_super_admin;

  $("cameras-empty").hidden = items.length > 0;
  $("cameras-table-wrap").hidden = items.length === 0;

  rows.replaceChildren(...items.map((item) => {
    /* Registration detail that is simply ABSENT keeps a dash: a device with no
       label recorded is different from a device that has not reported its
       frame rate, and using the same words for both would flatten the
       distinction this table exists to make. */
    const actions = el("td", { class: "row-actions-cell" }, [
      admin
      ? el("div", { class: "row-actions" }, [
        el("button", { class: "btn-link", type: "button", text: "Edit",
                      "data-edit-camera": item.camera_id }),
        el("button", { class: "btn-link", type: "button", text: "Remove",
                      "data-remove-camera": item.camera_id })
      ])
      /* A teacher may LOOK at every device and change none of them, and the
         server enforces that with require_super_admin on both routes - this
         only stops offering a button that would be refused. */
      : el("span", { class: "muted small", text: "View only" })
    ]);

    return el("tr", null, [
      el("td", null, [el("code", { text: item.camera_id })]),
      el("td", { text: item.label || DASH }),
      el("td", { text: item.classroom || DASH }),
      el("td", { text: item.camera_index === null ||
                item.camera_index === undefined ? DASH : String(item.camera_index) }),
      el("td", null, [item.last_heartbeat
                      ? cameraStatusPill(item.status)
                      : el("span", { class: "muted nodata", text: "No data" })]),
      el("td", null, [reported(item.last_heartbeat, stamp)]),
      el("td", null, [reported(item.last_fps)]),
      el("td", null, [reported(item.last_latency_ms,
                               (value) => value + " ms")]),
      el("td", null, [reported(item.pipeline_state)]),
      el("td", { class: "small", text: stamp(item.created_at) +
                (item.registered_by ? " by " + item.registered_by : "") }),
      el("td", { class: item.last_error ? "cam-error" : "muted",
                text: item.last_error || DASH }),
      actions
    ]);
  }));

  const bind = (attribute, handler) => {
    rows.querySelectorAll("[" + attribute + "]").forEach((button) => {
      button.addEventListener("click",
                              () => handler(button.getAttribute(attribute)));
    });
  };

  bind("data-edit-camera", (id) => openCameraDialog(
    items.find((item) => item.camera_id === id) || null));
  bind("data-remove-camera", removeCamera);

  /* Registering is Super Admin only. Hidden here and refused by the server. */
  $("camera-register").hidden = !(state.auth.configured && admin);
}

/* ---------- registering a camera device ---------- */

function cameraError(message) {
  const node = $("camera-error");
  node.textContent = message || "";
  node.hidden = !message;
}

/* One dialog for both registering and correcting.

   Re-registering an existing device id is an UPDATE on the server that
   deliberately leaves its health alone, so "Edit" is the same request as
   "Register" with the id filled in - there is no second endpoint and no second
   code path here either. */
function openCameraDialog(camera) {
  cameraError("");

  $("camera-dialog-title").textContent = camera
  ? "Edit " + camera.camera_id : "Register a camera";
  $("camera-save").textContent = camera ? "Save" : "Register";

  $("camera-id").value = camera ? camera.camera_id : "";
  $("camera-id").readOnly = Boolean(camera);
  $("camera-label").value = camera ? (camera.label || "") : "";
  $("camera-classroom").value = camera ? (camera.classroom || "") : "";
  $("camera-index").value = camera && camera.camera_index !== null &&
  camera.camera_index !== undefined ? String(camera.camera_index) : "";

  $("camera-dialog").showModal();
}

async function saveCamera() {
  const cameraId = $("camera-id").value.trim();
  if (!cameraId) return cameraError("Enter a device ID.");

  const rawIndex = $("camera-index").value.trim();
  let index = null;
  if (rawIndex !== "") {
    index = Number(rawIndex);
    if (!Number.isInteger(index) || index < 0 || index > 64) {
      return cameraError("The camera index must be a whole number "
                         + "between 0 and 64.");
    }
  }

  setBusy(true);
  try {
    await apiSend("/api/cameras/" + encodeURIComponent(cameraId), "PUT", {
      label: $("camera-label").value.trim(),
      classroom: $("camera-classroom").value.trim(),
      camera_index: index
    });

    $("camera-dialog").close();
    banner("Camera " + cameraId + " registered. Its health stays \u201cNo " +
           "data\u201d until the device sends a heartbeat.");

    await loadCameras();
    /* The Live classroom tab picks a camera from this same list. */
    try { await loadLiveCameras(); } catch (failure) { /* that tab retries */ }
  } catch (failure) {
    cameraError(failure.message);
  } finally {
    setBusy(false);
  }
}

/* Removing a device destroys no attendance and no image.

   A camera row is operational only - it records where a device is and when it
   last delivered a frame, and nothing about any person it has ever seen. That
   is why this is a plain confirm and the student and session deletions are
   typed-id dialogs. */
async function removeCamera(cameraId) {
  if (!window.confirm(
    "Remove camera " + cameraId + " from the register?\n\nNo " +
    "attendance, presence record or image is affected \u2014 a camera " +
    "row records only where a device is and when it last answered. It " +
    "can be registered again at any time.")) {
      return;
    }

  setBusy(true);
  try {
    await apiSend("/api/cameras/" + encodeURIComponent(cameraId), "DELETE",
                  null);
    banner("Camera " + cameraId + " removed from the register. No attendance " +
           "was affected.");
    await loadCameras();
    try { await loadLiveCameras(); } catch (failure) { /* that tab retries */ }
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function loadCameras() {
  /* Only the FIRST load shows the spinner. A poll that replaced the table with
     "Checking camera health..." every seven seconds would be unreadable. */
  if ($("cameras-body").hidden && $("cameras-error").hidden) {
    camerasState("loading");
  }

  try {
    const body = await api("/api/cameras");

    renderCameraHeadline(body);
    renderCameraCards(body);
    renderCameraRuntime(body);
    renderCameraTable(body);
    spatialCameras(body);
    $("cameras-note").textContent = body.note || "";

    camerasState("ready");

    /* The subsystem console is a separate concern with separate sources, and
       it fails on its own: one subsystem that will not answer must not hide
       the camera table above it. */
    loadSystemStatus().catch(() => {});
  } catch (failure) {
    /* A 401 has already shown the sign-in screen. Everything else belongs
       here, including the 403 a role without access would receive. */
    camerasState("error", failure.status === 403
                 ? "This account does not have access to camera health."
                 : failure.message);
    throw failure;
  }
}

function startCameraPolling() {
  /* Stop first, so navigating away and back can never leave two timers
     running against the same endpoint. */
  stopCameraPolling();
  state.cameraTimer = window.setInterval(() => {
    loadCameras().catch(() => {
      /* The error state is already on screen; the next poll retries. */
    });
  }, CAMERA_POLL_MS);
}

function stopCameraPolling() {
  if (state.cameraTimer !== null) {
    window.clearInterval(state.cameraTimer);
    state.cameraTimer = null;
  }
}

/* The Refresh and Try again buttons. Swallows the rejection because
   loadCameras has already rendered the error state. */
async function refreshCameras() {
  try {
    await loadCameras();
  } catch (failure) {
    /* already shown */
  }
}

/* ---------- administration ---------- */

function userError(message) {
  const node = $("user-error");
  node.textContent = message || "";
  node.hidden = !message;
}

/*
   THE ACCOUNT LIST, AND NOTHING ELSE.

   This used to be loadAdministration and it fetched six unrelated things,
   because the page it filled held six unrelated panels. Those panels are
   views now and each one loads itself from refresh(); what is left here is
   the thing the page is actually called after.
*/
async function loadTeachers() {
  const users = await api("/api/users");

  const body = $("users-table").querySelector("tbody");
  body.replaceChildren(...users.map((user) =>
                                    el("tr", {}, [
                                      el("td", null, [
                                        el("div", { class: "account" }, [
                                          staffPhotoNode(user, true),
                                          el("span", { text: user.username })
                                        ])
                                      ]),
                                      el("td", { text: user.display_name }),
                                      el("td", {}, [statusPill(user.role)]),
                                      el("td", { text: (user.assignments || []).length
                                                ? user.assignments.map(describeAssignment).join(", ")
                                                : "every class" }),
                                      el("td", {}, [statusPill(user.status)]),
                                      el("td", { text: user.last_login_at ? clock(user.last_login_at) : "—" }),
                                      el("td", { class: "row-actions-cell" }, [
                                        el("div", { class: "row-actions" }, [
                                          el("button", { class: "btn-link", type: "button", text: "Edit",
                                                        "data-user": String(user.user_id) }),
                                          el("button", { class: "btn-link", type: "button",
                                                        text: user.status === "ACTIVE"
                                                        ? "Deactivate" : "Activate",
                                                        "data-toggle-user": String(user.user_id),
                                                        "data-next": user.status === "ACTIVE"
                                                        ? "INACTIVE" : "ACTIVE" })
                                        ])
                                      ])
                                    ])));

  body.querySelectorAll("[data-user]").forEach((button) => {
    button.addEventListener("click", () => openUserDialog(
      users.find((user) =>
                 String(user.user_id) === button.getAttribute("data-user"))));
  });

  body.querySelectorAll("[data-toggle-user]").forEach((button) => {
    button.addEventListener("click", () => setUserStatus(
      button.getAttribute("data-toggle-user"),
      button.getAttribute("data-next")));
  });

}

/* The log. Its own page and its own request: nothing else on screen depends
   on it, and nothing it depends on is on screen. */
async function loadAudit() {
  const audit = await api("/api/users/audit?limit=50").catch(() => []);

  const auditBody = $("audit-table").querySelector("tbody");
  auditBody.replaceChildren(...(audit.length ? audit : []).map((entry) =>
                                                               el("tr", {}, [
                                                                 el("td", { text: entry.at }),
                                                                 el("td", { text: entry.actor || "—" }),
                                                                 el("td", { text: entry.action }),
                                                                 el("td", { text: entry.subject || "—" }),
                                                                 el("td", { text: entry.detail || "—" })
                                                               ])));

  if (!audit.length) {
    auditBody.replaceChildren(el("tr", {}, [
      el("td", { colspan: "5", class: "muted",
                text: "Nothing has been recorded yet." })
    ]));
  }
}

function renderAssignments() {
  const container = $("user-assignments");

  if (!state.userAssignments.length) {
    container.replaceChildren(el("p", { class: "muted small", text:
                                       "No assignment — this account sees every class." }));
    return;
  }

  container.replaceChildren(...state.userAssignments.map((assignment, index) =>
                                                         el("div", { class: "assignment-row" }, [
                                                           el("span", { text: describeAssignment(assignment) }),
                                                           el("button", { class: "btn-link", type: "button", text: "Remove",
                                                                         "data-assignment": String(index) })
                                                         ])));

  container.querySelectorAll("[data-assignment]").forEach((button) => {
    button.addEventListener("click", () => {
      state.userAssignments.splice(
        Number(button.getAttribute("data-assignment")), 1);
      renderAssignments();
    });
  });
}

function openUserDialog(user) {
  state.editingUser = user || null;
  state.userAssignments = user ? (user.assignments || []).slice() : [];
  userError("");
  $("user-form").reset();

  $("user-dialog-title").textContent = user
  ? "Edit " + user.username : "Add a teacher";
  $("user-username").value = user ? user.username : "";
  $("user-username").readOnly = Boolean(user);
  $("user-display-name").value = user ? user.display_name : "";
  $("user-role").value = user ? user.role : "TEACHER";
  $("user-status").value = user ? user.status : "ACTIVE";
  $("user-password").value = "";
  $("user-password-note").textContent = user
  ? "Leave blank to keep the current password. Setting one signs this " +
  "account out everywhere."
  : "At least 10 characters. Stored only as a PBKDF2 hash — it is never " +
  "shown again and cannot be recovered, only replaced.";
  $("user-save").textContent = user ? "Save changes" : "Create account";
  $("user-delete").hidden = !user;

  renderAssignments();

  /* The photograph and the documents belong to an account that EXISTS. On a
     new one the capture still works and the image is uploaded straight after
     the account is created; documents wait, because they need a user_id to
     hang from. */
  capture.dataUrl = null;
  stopCapture();
  if ($("capture-preview")) {
    const preview = $("capture-preview");
    if (user && user.photo_name) {
      preview.src = "/api/users/" + user.user_id + "/photo?v=" +
      encodeURIComponent(user.updated_at || "");
      preview.hidden = false;
      $("capture-idle").hidden = true;
    } else {
      preview.removeAttribute("src");
      preview.hidden = true;
      $("capture-idle").hidden = false;
    }
    $("capture-start").textContent = "Use camera";
  }
  loadStaffDocuments(user ? user.user_id : null);

  $("user-dialog").showModal();
}

function addAssignment() {
  const className = $("user-class").value.trim();
  if (!className) return userError("Enter a class to assign.");

  state.userAssignments.push({
    class_name: className,
    section: $("user-section").value.trim()
  });

  $("user-class").value = "";
  $("user-section").value = "";
  userError("");
  renderAssignments();
}

async function saveUser() {
  const password = $("user-password").value;
  const body = {
    display_name: $("user-display-name").value.trim(),
    role: $("user-role").value,
    status: $("user-status").value,
    assignments: state.userAssignments
  };

  if (!body.display_name) return userError("Enter a name.");

  userError("");
  setBusy(true);

  try {
    if (state.editingUser) {
      if (password) body.password = password;
      await apiSend("/api/users/" + state.editingUser.user_id, "PATCH", body);
      await uploadStaffPhoto(state.editingUser.user_id);
      banner(state.editingUser.username + " updated.");
    } else {
      body.username = $("user-username").value.trim();
      body.password = password;
      if (!body.username) return userError("Enter a username.");
      if (!password) return userError("Enter a password for this account.");
      const created = await apiSend("/api/users", "POST", body);
      /* The account first, then the photograph. A failed upload must leave a
         usable account rather than rolling back somebody's credentials. */
      await uploadStaffPhoto(created.user_id);
      banner(body.username + " can now sign in.");
    }
    $("user-dialog").close();
    await loadTeachers();
  } catch (failure) {
    userError(failure.message);
  } finally {
    setBusy(false);
  }
}

async function setUserStatus(userId, status) {
  try {
    await apiSend("/api/users/" + userId, "PATCH", { status });
    await loadTeachers();
  } catch (failure) {
    banner(failure.message, true);
  }
}

async function deleteUser() {
  if (!state.editingUser) return;
  try {
    await apiSend("/api/users/" + state.editingUser.user_id, "DELETE", null);
    $("user-dialog").close();
    banner(state.editingUser.username + " deleted.", true);
    await loadTeachers();
  } catch (failure) {
    userError(failure.message);
  }
}

/* =========================================================================
   THE ASSISTANT (Step 12)

   A question box and an answer, and the answer always comes with its receipt:
   which analytics function produced the figure, what arguments it was given,
   and which page of this dashboard shows the same number.

   The receipt is not decoration. A teacher who cannot check a figure about a
   child's attendance has to take it on trust, and an assistant that is taken
   on trust is a second source of truth. So "where to check this" is rendered
   next to every answer rather than hidden behind a details element.

   This file formats nothing numeric. The sentence arrives from the server
   already written, and the figures are shown as the server sent them - which
   is the same rule the rest of this dashboard follows, applied to a layer
   that would be especially easy to quietly round.
   ========================================================================= */

function assistantError(message) {
  const node = $("assistant-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function loadAssistant() {
  assistantError("");

  /* The capability list is fetched once per visit rather than cached forever:
     it is small, and a stale list of what the assistant can answer is a list
     that lies. */
  let capabilities;
  try {
    capabilities = await api("/api/assistant/capabilities");
  } catch (failure) {
    assistantError(failure.message);
    return;
  }

  renderAssistantExamples(capabilities);
}

function renderAssistantExamples(capabilities) {
  const examples = $("assistant-examples");

  examples.replaceChildren(...(capabilities.items || []).map((item) => {
    const ask = el("button", { class: "btn-link", type: "button",
                               text: item.question });
    ask.addEventListener("click", () => {
      $("assistant-question").value = item.question;
      askAssistant();
    });

    return el("li", {}, [
      ask,
      /* Each example says which analytics function answering it would call,
         so the list doubles as the map of what this layer is allowed to do. */
      el("span", { class: "muted small",
                   text: " — " + (item.calls || []).join(", ") +
                         " · " + item.where })
    ]);
  }));

  $("assistant-limits").replaceChildren(
    ...(capabilities.limits || []).map((limit) =>
      el("li", { text: limit })));
}

async function askAssistant() {
  assistantError("");

  const question = $("assistant-question").value.trim();
  const container = $("assistant-answer");

  if (!question) {
    container.replaceChildren();
    assistantError("Type a question first.");
    return;
  }

  setBusy(true);

  let answer;
  try {
    answer = await apiSend("/api/assistant/ask", "POST", { question });
  } catch (failure) {
    container.replaceChildren();
    assistantError(failure.message);
    return;
  } finally {
    setBusy(false);
  }

  renderAssistantAnswer(answer);
}

function renderAssistantAnswer(answer) {
  const children = [];

  /* A question that was not understood is shown AS not understood, with the
     list of what can be asked - not as a half-answer. */
  children.push(el("p", {
    class: "assistant-text" + (answer.understood ? "" : " assistant-unknown"),
    text: answer.answer
  }));

  if (!answer.understood && (answer.suggestions || []).length) {
    children.push(el("p", { class: "muted small", text: "Try one of these:" }));
    children.push(el("ul", { class: "example-list" },
      answer.suggestions.map((suggestion) => {
        const ask = el("button", { class: "btn-link", type: "button",
                                   text: suggestion });
        ask.addEventListener("click", () => {
          $("assistant-question").value = suggestion;
          askAssistant();
        });
        return el("li", {}, [ask]);
      })));
  }

  /* The receipt. One row per analytics call, naming the function, what it was
     asked, and where the same number is shown. */
  if ((answer.sources || []).length) {
    children.push(el("h3", { class: "assistant-receipt-heading",
                             text: "Where this came from" }));

    children.push(el("ul", { class: "assistant-sources" },
      answer.sources.map((source) => el("li", {}, [
        el("code", { text: source.function }),
        el("span", { class: "muted small",
                     text: " " + describeArguments(source.arguments) }),
        el("span", { class: "assistant-where", text: source.where })
      ]))));
  }

  if (answer.note) {
    children.push(el("p", { class: "muted small", text: answer.note }));
  }

  $("assistant-answer").replaceChildren(...children);
}

function describeArguments(args) {
  const entries = Object.entries(args || {});
  if (!entries.length) return "(no arguments - everything in your scope)";
  return "(" + entries.map(([key, value]) => key + ": " + value).join(", ") +
         ")";
}

function clearAssistant() {
  $("assistant-question").value = "";
  $("assistant-answer").replaceChildren();
  assistantError("");
}

function bindAssistant() {
  $("assistant-form").addEventListener("submit", (event) => {
    event.preventDefault();
    askAssistant();
  });
  $("assistant-clear").addEventListener("click", clearAssistant);
}

/* =========================================================================
   ACADEMIC TERMS AND PROMOTION

   The distinction the whole page rests on: a student is permanent, and their
   class, section and roll number are facts about a PERIOD. Promoting creates
   a period; it never edits the one before it. That is why last term's
   attendance still describes the class a student was actually in.

   Two things this page is careful never to do:

     1. Promote without showing what would change first. A bulk change across
        forty students is read before it is run, and the preview names every
        field being CARRIED FORWARD - because where nothing was supplied, the
        previous value is kept rather than invented.

     2. Imply that creating a term moves anybody. It does not. A new term sits
        PLANNED until somebody activates it, and promotion is a separate act.
   ========================================================================= */

const TERM_STATUS_SYMBOL = { ACTIVE: "●", PLANNED: "○", CLOSED: "✓" };

const academicState = {
  terms: [],
  current: null,
  /* What the allocation file gave us, per student. Held here rather than
     re-parsed, so the preview and the promotion run against the SAME
     allocation rather than two readings of one file. */
  allocations: null,
  /* The exact payload the preview was built from. runPromotion() sends this
     and not a freshly assembled one, which is what makes "the promotion does
     what the preview showed" true by construction rather than by hope. */
  previewed: null,
  preview: null
};

function academicError(message) {
  const node = $("academic-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function loadAcademic() {
  academicError("");

  const data = await api("/api/academic/sessions");
  const terms = data.items || [];

  academicState.terms = terms;
  academicState.current = data.current || null;

  $("terms-empty").hidden = terms.length > 0;
  $("terms-wrap").hidden = terms.length === 0;

  renderTerms(terms);
  fillPromotionTerms(terms);
}

function renderTerms(terms) {
  const body = $("terms-table").querySelector("tbody");

  body.replaceChildren(...terms.map((term) => {
    const row = el("tr", {}, [
      el("td", { text: term.academic_year || "—" }),
      el("td", { text: term.semester || "—" }),
      el("td", { text: term.start_date || "—" }),
      el("td", { text: term.end_date || "—" }),
      el("td", {}, [termStatusPill(term.status)]),
      /* Filled in by a second request per term; until it answers, the cell
         says nothing rather than showing a zero nobody counted. */
      el("td", { class: "num", "data-term-count": term.academic_session_id,
                 text: "…" }),
      el("td", { class: "row-actions-cell" }, [
        el("div", { class: "row-actions" }, [
          term.status === "ACTIVE"
            ? el("span", { class: "muted small", text: "current" })
            : el("button", { class: "btn-link", type: "button",
                             text: "Make current",
                             "data-activate": term.academic_session_id })
        ])
      ])
    ]);
    return row;
  }));

  body.querySelectorAll("[data-activate]").forEach((button) => {
    button.addEventListener("click", () =>
      activateTerm(button.getAttribute("data-activate")));
  });

  for (const term of terms) countTermStudents(term.academic_session_id);
}

function termStatusPill(status) {
  const symbol = TERM_STATUS_SYMBOL[status] || "?";
  return el("span", { class: "pill pill-" + status }, [
    el("span", { class: "sym", "aria-hidden": "true", text: symbol }),
    el("span", { text: status || "UNKNOWN" })
  ]);
}

/* How many students are enrolled in a term is a different question from how
   many exist, and the answer comes from the server rather than from counting
   anything on this page. */
async function countTermStudents(termId) {
  const cell = $("terms-table").querySelector(
    '[data-term-count="' + CSS.escape(termId) + '"]');
  if (!cell) return;

  try {
    const data = await api("/api/academic/sessions/" +
                           encodeURIComponent(termId) + "/students");
    cell.textContent = String(data.total ?? 0);
  } catch (failure) {
    /* "No data" rather than 0: nobody counted. */
    cell.textContent = "—";
  }
}

async function activateTerm(termId) {
  academicError("");

  try {
    await apiSend("/api/academic/sessions/" + encodeURIComponent(termId) +
                  "/activate", "POST");
  } catch (failure) {
    academicError(failure.message);
    return;
  }

  banner("Term " + termId + " is now the current one. The previous term is " +
         "closed; nothing it recorded has changed.");
  await loadAcademic();
}

function openTermDialog() {
  academicError("");
  $("term-year").value = "";
  $("term-semester").value = "";
  $("term-start").value = "";
  $("term-end").value = "";
  $("term-error").hidden = true;
  $("term-dialog").showModal();
}

async function createTerm() {
  const error = $("term-error");
  error.hidden = true;

  const payload = {
    academic_year: $("term-year").value.trim(),
    semester: $("term-semester").value.trim(),
    start_date: $("term-start").value || null,
    end_date: $("term-end").value || null,
    status: "PLANNED"
  };

  if (!payload.academic_year) {
    error.textContent = "An academic year is required.";
    error.hidden = false;
    return;
  }

  try {
    await apiSend("/api/academic/sessions", "POST", payload);
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    return;
  }

  $("term-dialog").close();
  banner("Term created. It is PLANNED: nobody has been moved into it, and " +
         "nothing has been promoted.");
  await loadAcademic();
}

/* ---------- promotion ---------- */

function fillPromotionTerms(terms) {
  const select = $("promotion-term");
  const chosen = select.value;

  select.replaceChildren(
    el("option", { value: "", text: "Choose a term" }),
    ...terms.map((term) => el("option", {
      value: term.academic_session_id,
      text: (((term.academic_year || "") + " " + (term.semester || ""))
             .trim() || term.academic_session_id) +
            (term.status === "ACTIVE" ? " (current)" : "")
    })));

  select.value = chosen;
}

function promotionFilters() {
  /* class_name and section here are WHO is promoted, not where they land.
     The destination only ever comes from an allocation - see
     targetedPayload(). */
  return {
    academic_session_id: $("promotion-term").value,
    class_name: $("promotion-from-class").value.trim() || null,
    section: $("promotion-from-section").value.trim() || null
  };
}

/*
  The typed destination, expanded over the students a promotion would touch.

  A blank "new class" is not a blank class - it means carry the current one
  forward, which is what supplying no allocation for that field tells the
  server. A per-student allocation from the file always WINS over the typed
  one, because a class list the office produced is more specific than a
  blanket instruction.
*/
function targetedPayload(studentIds) {
  const payload = promotionFilters();
  const toClass = $("promotion-to-class").value.trim();
  const toSection = $("promotion-to-section").value.trim();
  const allocations = {};

  for (const [studentId, allocation] of
       Object.entries(academicState.allocations || {})) {
    allocations[studentId] = Object.assign({}, allocation);
  }

  if (toClass || toSection) {
    for (const studentId of studentIds || []) {
      const existing = allocations[studentId] || {};
      if (toClass && existing.class_name === undefined) {
        existing.class_name = toClass;
      }
      if (toSection && existing.section === undefined) {
        existing.section = toSection;
      }
      allocations[studentId] = existing;
    }
  }

  if (Object.keys(allocations).length) payload.allocations = allocations;
  return payload;
}

async function readAllocationFile(input) {
  const state_node = $("promotion-allocation-state");
  const file = input.files && input.files[0];

  if (!file) {
    academicState.allocations = null;
    state_node.textContent = "";
    return;
  }

  let text;
  try {
    text = await file.text();
  } catch (failure) {
    state_node.textContent = "That file could not be read.";
    return;
  }

  let parsed;
  try {
    parsed = await apiSend("/api/academic/promotion/allocation", "POST",
                           { csv: text });
  } catch (failure) {
    academicState.allocations = null;
    state_node.textContent = failure.message;
    return;
  }

  academicState.allocations = parsed.allocations || {};

  /* Problems are REPORTED, not silently dropped. A row the parser skipped is
     a student who will not be allocated, and the office needs to know which. */
  const problems = parsed.problems || [];
  state_node.textContent =
    parsed.students + " student(s) read from the file." +
    (problems.length ? "  Skipped: " + problems.join("; ") : "");
}

async function previewPromotion() {
  academicError("");

  if (!$("promotion-term").value) {
    academicError("Choose the term to promote into.");
    return;
  }

  /* Two passes, on purpose. The first asks the server WHO a promotion would
     touch; the second asks what it would do to exactly those students with
     the typed destination applied. Working the candidate list out here would
     be this page forming its own opinion about who is ACTIVE. */
  let preview;
  try {
    preview = await apiSend("/api/academic/promotion/preview", "POST",
                            targetedPayload([]));

    const studentIds = (preview.students || []).map((row) => row.student_id);
    const payload = targetedPayload(studentIds);

    if (payload.allocations) {
      preview = await apiSend("/api/academic/promotion/preview", "POST",
                              payload);
    }

    academicState.previewed = payload;
  } catch (failure) {
    academicError(failure.message);
    return;
  }

  academicState.preview = preview;
  renderPromotionPreview(preview);
}

function renderPromotionPreview(preview) {
  const rows = preview.students || [];

  $("promotion-preview-empty").hidden = rows.length > 0;
  $("promotion-wrap").hidden = rows.length === 0;
  $("promotion-actions").hidden = rows.length === 0;

  const cards = [
    card("Students", preview.total),
    card("Already in this term", preview.already_enrolled,
         "promoting again changes nothing for them")
  ];

  $("promotion-summary").replaceChildren(
    el("ul", { class: "cards small" }, cards));

  const body = $("promotion-table").querySelector("tbody");
  body.replaceChildren(...rows.map((row) => el("tr", {}, [
    el("td", { text: row.student_id }),
    el("td", { text: row.name || "—" }),
    el("td", { text: describePlacement(row.from) }),
    el("td", { text: describePlacement(row.to) }),
    /* Said out loud, per student, rather than left to be inferred from two
       columns that happen to match. */
    el("td", { class: "muted small",
               text: (row.carried_forward || []).length
                 ? row.carried_forward.join(", ").replace(/_/g, " ")
                 : "nothing" }),
    el("td", { text: row.already_enrolled ? "yes" : "no" })
  ])));
}

function describePlacement(placement) {
  if (!placement) return "—";
  const parts = [placement.class_name || "—"];
  if (placement.section) parts.push(placement.section);
  if (placement.roll_number) parts.push("roll " + placement.roll_number);
  return parts.join(" · ");
}

async function runPromotion() {
  academicError("");

  if (!academicState.preview || !academicState.previewed) {
    academicError("Preview the promotion before running it.");
    return;
  }

  let result;
  try {
    /* The payload the preview was built from, verbatim. */
    result = await apiSend("/api/academic/promotion", "POST",
                           academicState.previewed);
  } catch (failure) {
    academicError(failure.message);
    return;
  }

  banner(result.count + " student(s) promoted into " +
         result.academic_session_id + ". Their previous term is unchanged — " +
         "the attendance recorded against it still describes the class they " +
         "were in when it was taken.");

  academicState.preview = null;
  academicState.previewed = null;
  $("promotion-wrap").hidden = true;
  $("promotion-actions").hidden = true;
  $("promotion-summary").replaceChildren();
  $("promotion-preview-empty").hidden = false;

  await loadAcademic();
}

function clearPromotion() {
  $("promotion-from-class").value = "";
  $("promotion-from-section").value = "";
  $("promotion-to-class").value = "";
  $("promotion-to-section").value = "";
  $("promotion-allocation").value = "";
  $("promotion-allocation-state").textContent = "";
  academicState.allocations = null;
  academicState.preview = null;
  academicState.previewed = null;
  $("promotion-wrap").hidden = true;
  $("promotion-actions").hidden = true;
  $("promotion-summary").replaceChildren();
  $("promotion-preview-empty").hidden = false;
  academicError("");
}

function bindAcademic() {
  $("add-term").addEventListener("click", openTermDialog);
  $("term-save").addEventListener("click", createTerm);
  $("term-cancel").addEventListener("click", () => $("term-dialog").close());

  $("promotion-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    previewPromotion();
  });
  $("promotion-clear").addEventListener("click", clearPromotion);
  $("promotion-run").addEventListener("click", runPromotion);
  $("promotion-allocation").addEventListener("change", (event) =>
    readAllocationFile(event.target));
}

/* =========================================================================
   THE RECOGNITION MODEL REGISTRY

   One table and three buttons, and the asymmetry between them is the point.

   Activating a model is the only action on this dashboard that changes what
   the camera recognises, so it takes a validation against a measured floor
   AND the model id typed out. Rolling back to SFace takes one click and no
   gate at all: when something is going wrong in a live classroom, the safe
   direction should be the easy one.

   Every number in the table was MEASURED by whatever trained the model. A
   metric that was never measured shows as "not measured", never as a dash
   that could be read as zero and never as a pass.
   ========================================================================= */

const MODEL_METRICS = ["accuracy", "precision", "recall",
                       "false_acceptance_rate"];

function modelError(message) {
  const node = $("models-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function loadModels() {
  modelError("");

  const data = await api("/api/models");
  const models = data.models || [];

  renderActiveModel(data.active);
  $("models-note").textContent = data.note || "";
  $("models-gate").textContent = describeGate(data.gate);

  const body = $("models-table").querySelector("tbody");
  body.replaceChildren(...models.map((model) => modelRow(model)));

  body.querySelectorAll("[data-validate]").forEach((button) => {
    button.addEventListener("click", () =>
      validateModel(button.getAttribute("data-validate")));
  });
  body.querySelectorAll("[data-activate-model]").forEach((button) => {
    button.addEventListener("click", () =>
      activateModel(button.getAttribute("data-activate-model")));
  });
}

function renderActiveModel(active) {
  const text = $("models-active-text");

  if (!active) {
    text.textContent = "No model is recorded as active.";
    return;
  }

  text.textContent = "In production: " + (active.model_name || active.model_id) +
                     " " + (active.version || "") +
                     " (" + active.kind + ")";
}

function describeGate(gate) {
  if (!gate) return "";

  return "Activation requires accuracy, precision and recall of at least " +
         percent(gate.accuracy) + ", a false acceptance rate no higher than " +
         percent(gate.false_acceptance_rate) + ", at least " +
         gate.minimum_test_identities + " held-out identities and " +
         gate.minimum_test_samples + " samples. A metric that was not " +
         "measured is a failure, not a pass.";
}

function modelRow(model) {
  const metrics = model.metrics || {};

  const cells = [
    el("td", { text: model.model_name || model.model_id }),
    el("td", { text: model.version || "—" }),
    el("td", { text: model.kind || "—" }),
    el("td", {}, [modelStatusPill(model.status)])
  ];

  for (const name of MODEL_METRICS) {
    cells.push(el("td", { class: "num" }, [metricCell(metrics[name])]));
  }

  cells.push(el("td", { class: "num" }, [metricCell(metrics.test_identities,
                                                   true)]));
  /* Whether the evaluation used identities the training never saw is the one
     fact that decides whether the other numbers mean anything. */
  cells.push(el("td", { text: metrics.held_out === true ? "yes"
                             : metrics.held_out === false ? "no"
                             : "not reported" }));

  cells.push(el("td", { class: "row-actions-cell" }, [
    el("div", { class: "row-actions" }, modelActions(model))
  ]));

  return el("tr", {}, cells);
}

function metricCell(value, whole) {
  if (value === null || value === undefined) {
    return el("span", { class: "muted small", text: "not measured" });
  }
  return el("span", { text: whole ? String(value) : percent(value) });
}

function modelActions(model) {
  if (model.kind === "baseline") {
    return [el("span", { class: "muted small", text: "the baseline" })];
  }

  const actions = [];

  if (model.status === "TESTING" || model.status === "TRAINING") {
    actions.push(el("button", { class: "btn-link", type: "button",
                                text: "Validate",
                                "data-validate": model.model_id }));
  }

  if (model.status === "VALIDATED") {
    actions.push(el("button", { class: "btn-link", type: "button",
                                text: "Activate",
                                "data-activate-model": model.model_id }));
  }

  if (model.status === "ACTIVE") {
    actions.push(el("span", { class: "muted small", text: "in production" }));
  }

  if (!actions.length) {
    actions.push(el("span", { class: "muted small", text: "—" }));
  }

  return actions;
}

function modelStatusPill(status) {
  return el("span", { class: "pill pill-model-" + (status || "UNKNOWN") }, [
    el("span", { class: "sym", "aria-hidden": "true",
                 text: status === "ACTIVE" ? "●"
                       : status === "VALIDATED" ? "◆"
                       : status === "RETIRED" ? "✕" : "○" }),
    el("span", { text: status || "UNKNOWN" })
  ]);
}

async function validateModel(modelId) {
  modelError("");

  try {
    await apiSend("/api/models/" + encodeURIComponent(modelId) +
                  "/validate", "POST");
  } catch (failure) {
    /* The gate's refusal names every reason. It is shown in full rather than
       summarised: "did not pass" tells whoever trained it nothing. */
    modelError(failure.message);
    await loadModels();
    return;
  }

  banner("Model " + modelId + " is VALIDATED. It is still not in production " +
         "— activation is a separate, deliberate act.");
  await loadModels();
}

async function activateModel(modelId) {
  modelError("");

  /* The same protection a permanent deletion gets. Promoting an unproven
     recogniser corrupts a register in exactly the same way. */
  const typed = window.prompt(
    "Activating " + modelId + " changes what the camera recognises.\n\n" +
    "Type the model id exactly to confirm:");

  if (typed === null) return;

  if (typed.trim() !== modelId) {
    modelError("The model id did not match. Nothing was activated.");
    return;
  }

  try {
    await apiSend("/api/models/" + encodeURIComponent(modelId) +
                  "/activate?confirm=" + encodeURIComponent(typed.trim()),
                  "POST");
  } catch (failure) {
    modelError(failure.message);
    return;
  }

  banner("Model " + modelId + " is in production. Roll back to the SFace " +
         "baseline at any time — that takes one click and no gate.");
  await loadModels();
}

async function rollbackModel() {
  modelError("");

  try {
    await apiSend("/api/models/rollback", "POST");
  } catch (failure) {
    modelError(failure.message);
    return;
  }

  banner("The SFace baseline is back in production.");
  await loadModels();
}

function bindModels() {
  $("models-rollback").addEventListener("click", rollbackModel);
}

/* =========================================================================
   SYSTEM HEALTH

   An engineering console: one module per subsystem, each showing the last
   thing that subsystem reported about ITSELF.

   Three rules, and they are the same three the rest of this interface follows:

     1. Nothing is assumed healthy. A module whose source has not answered
        reads UNKNOWN in the neutral colour, not green.

     2. Every state is a WORD. The colour agrees with it and is never the only
        signal.

     3. Nothing is derived. A queue with items waiting is reported as items
        waiting - not as a fault - because queued attendance is safe in the
        database and has not been lost.
   ========================================================================= */

const SYSTEM_STATES = { ok: "ONLINE", warn: "DEGRADED", bad: "OFFLINE",
                        idle: "UNKNOWN" };

function systemModule(name, level, value, detail) {
  return el("li", { class: "status-module status-" + level }, [
    el("span", { class: "status-name", text: name }),
    el("span", { class: "status-state" }, [
      el("span", { class: "status-dot", "aria-hidden": "true" }),
      el("span", { text: value || SYSTEM_STATES[level] })
    ]),
    detail ? el("span", { class: "metric-sub", text: detail }) : null
  ]);
}

async function loadSystemStatus() {
  const container = $("system-status");
  if (!container) return;

  const modules = [];

  /* API, database and schema all come from one answer. */
  try {
    const health = await api("/api/health");
    const ok = health.status === "ok";
    modules.push(systemModule("API", ok ? "ok" : "warn",
                              ok ? "ONLINE" : String(health.status).toUpperCase()));
    modules.push(systemModule("Database", health.database ? "ok" : "idle",
                              health.database ? "ONLINE" : "UNKNOWN",
                              health.database || ""));
    /* The API answers "ready", "missing" or "unknown" - a STATE, not a
       version number. This tile used to print "v" + that word and render
       "vready", which is the kind of thing only a rendered page shows.
       A schema that is not ready is also not an idle fact: it is the one
       condition on this row that stops attendance being written. */
    const schema = String(health.schema || "unknown").toUpperCase();
    modules.push(systemModule("Schema", schema === "READY" ? "ok" : "warn",
                              schema, "migrated on start"));
  } catch (failure) {
    modules.push(systemModule("API", "bad", "OFFLINE", failure.message));
    modules.push(systemModule("Database", "idle"));
    modules.push(systemModule("Schema", "idle"));
  }

  /* The camera fleet, and the pipeline, from the list this page already
     reads. A registry with nothing in it is NOT a fault - live sessions work
     without a registered device - so it reads UNKNOWN with the reason. */
  try {
    const body = await api("/api/cameras");
    const devices = body.items || [];
    const runtime = body.live || {};

    if (!devices.length) {
      modules.push(systemModule("Camera", "idle", "NONE REGISTERED",
                                "sessions still run"));
    } else {
      const states = devices.map((device) => device.status);
      const level = states.includes("offline") ? "bad"
        : states.includes("degraded") ? "warn"
          : states.includes("online") ? "ok" : "idle";
      modules.push(systemModule("Camera", level,
                                level === "idle" ? "UNKNOWN"
                                  : SYSTEM_STATES[level],
                                devices.length + " registered"));
    }

    const state = runtime.state || "IDLE";
    modules.push(systemModule(
      "Pipeline",
      state === "RUNNING" ? "ok" : state === "FAILED" ? "bad"
        : state === "IDLE" ? "idle" : "warn",
      state));
  } catch (failure) {
    modules.push(systemModule("Camera", "idle"));
    modules.push(systemModule("Pipeline", "idle"));
  }

  /* The outbound queue. Waiting items are reported, never called a fault. */
  try {
    const sync = await api("/api/sync/status");
    const waiting = sync.waiting || 0;
    modules.push(systemModule(
      "Sync queue", waiting ? "warn" : "ok",
      waiting ? waiting + " WAITING" : "EMPTY",
      sync.upstream && !sync.upstream.configured
        ? "no upstream configured" : ""));
  } catch (failure) {
    modules.push(systemModule("Sync queue", "idle"));
  }

  container.replaceChildren(...modules);

  /* The stage's own status row, on the overview.

     The same NODES, cloned - not a second request. Two requests for the same
     four readings can return two different answers, and two contradictory
     status rows on one screen is worse than one row in one place. */
  const stage = $("stage-system");
  if (stage && modules.length) {
    stage.replaceChildren(...modules.slice(0, 4)
                          .map((node) => node.cloneNode(true)));
    $("stage-system-note").textContent = "Live system readings";
  }
}

/* ---------- wiring ---------- */

document.addEventListener("DOMContentLoaded", async () => {
  /* First, so the switch shows the right state and the attribute agrees with
     what the inline script in <head> already applied. That script prevents the
     flash; this one owns the behaviour from here on. */
  bindTheme();

  /* The rail's remembered width, before anything is painted into it. */
  bindRail();

  /* The spatial layer. After the theme, because every canvas in it reads the
     palette out of the computed style and the theme has to be settled first;
     before anything fetches, because the background and the cursor are part
     of the page rather than part of the data.

     Guarded, like every call into it: a console with no spatial.js is a flat
     console, not a broken one. */
  if (window.Spatial) window.Spatial.init();

  wirePremiumFeatures();

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  });

  $("sidebar-toggle").addEventListener("click", toggleSidebar);
  $("classes-refresh").addEventListener("click", () => loadClassList());
  $("new-student-detect").addEventListener(
    "click", (event) => detectCameras(event.currentTarget,
                                      "new-student-camera-found"));
  $("live-detect").addEventListener(
    "click", (event) => detectCameras(event.currentTarget,
                                      "live-camera-note"));
  $("live-focus-back").addEventListener("click", leaveLiveFocus);

  const finishAll = $("live-finish-all");
  finishAll.addEventListener("click", () => finishAllLiveSessions(finishAll));
  /* The flag is also the way to the live page - the thing somebody wants when
     they notice a session is running. */
  $("session-flag").addEventListener("click", () => switchView("live"));

  /* Back: the button in the top bar, Alt+Left and the browser's own button,
     all through one stack. */
  bindBackNavigation();

  /*
     Escape, in the order somebody expects it.

     A dialog's own Escape is handled by the dialog and is left alone. Below
     that: the drawer if it is open, then a detail panel if one is. It does
     NOT change view - Escape that navigates is a surprise, and Alt+Left is
     the control for that.
  */
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (document.querySelector("dialog[open]")) return;

    if ($("sidebar").dataset.open === "true") {
      closeSidebar();
      return;
    }

    const detail = openDetailPanel();
    if (detail) goBack();
  });

  $("filter-class").addEventListener("change", (event) => {
    state.classId = event.target.value;
    state.sessionOffset = 0;
    refresh();
  });

  $("filter-date").addEventListener("change", (event) => {
    state.date = event.target.value;
    state.sessionOffset = 0;
    refresh();
  });

  /* The filters are applied on change, so submitting adds nothing - but a
     form with a date field in it submits on Enter, and an unhandled submit
     reloads the page and discards every bit of state the dashboard holds. */
  $("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    refresh();
  });

  $("filter-clear").addEventListener("click", () => {
    state.classId = "";
    state.date = "";
    $("filter-class").value = "";
    $("filter-date").value = "";
    state.sessionOffset = 0;
    refresh();
  });

  /*
     Back, not Close.

     Focus moves to the list the panel was opened from. A button that only
     hides a panel leaves a keyboard user at the end of the document with no
     indication of where they now are.

     Deliberately NOT history.back(): this is one page, and on the first view
     somebody opens, history.back() leaves the application entirely.
  */
  $("close-session").addEventListener("click", goBack);
  $("close-student").addEventListener("click", goBack);
  $("close-live-summary").addEventListener("click", goBack);
  $("close-assembly-summary").addEventListener("click", goBack);

  $("att-status").addEventListener("change", (event) => {
    state.attendanceStatus = event.target.value;
    state.attendanceOffset = 0;
    loadAttendance();
  });

  $("att-sort").addEventListener("change", (event) => {
    state.attendanceSort = event.target.value;
    state.attendanceOffset = 0;
    loadAttendance();
  });

  $("att-desc").addEventListener("change", (event) => {
    state.attendanceDesc = event.target.checked;
    state.attendanceOffset = 0;
    loadAttendance();
  });

  $("student-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    readStudentFilters();
    loadStudents();
  });

  $("student-filter-clear").addEventListener("click", () => {
    $("student-search").value = "";
    $("student-filter-year").value = "";
    $("student-filter-class").value = "";
    $("student-filter-section").value = "";
    $("student-filter-status").value = "";
    $("student-filter-enrolled").value = "";
    readStudentFilters();
    loadStudents();
  });

  /* Changing a filter reloads, so the three dropdowns stay in step with each
     other without anybody pressing Apply. Apply still works, and still has
     to: a year typed by hand rather than picked fires no change event until
     the field is left. */
  for (const id of ["student-filter-year", "student-filter-class",
                    "student-filter-section", "student-filter-status",
                    "student-filter-enrolled"]) {
    $(id).addEventListener("change", () => {
      readStudentFilters();
      loadStudents();
    });
  }

  $("add-student").addEventListener("click", openStudentDialog);
  $("student-enroll").addEventListener("click", startEnrollment);
  $("student-register-only").addEventListener("click", registerStudentOnly);
  $("student-cancel-enroll").addEventListener("click", cancelEnrollment);
  $("student-close").addEventListener("click", closeStudentDialogAndCancel);

  /* Escape closes a dialog without going through any button, so the camera is
     released on the dialog's own close event rather than on a click. */
  $("student-dialog").addEventListener("close", () => {
    stopEnrollPolling();
    if (!$("student-cancel-enroll").hidden) {
      apiSend("/api/students/enrollment/cancel", "POST", null)
      .then(() => loadStudents())
      .catch(() => { /* nothing was stored either way */ });
    }
  });

  $("student-form").addEventListener("submit", (event) => {
    event.preventDefault();
  });

  $("edit-student-save").addEventListener("click", saveStudentEdit);
  $("edit-student-close").addEventListener("click",
                                           () => $("student-edit-dialog").close());
  $("student-edit-form").addEventListener("submit",
                                          (event) => event.preventDefault());

  $("edit-student-enroll").addEventListener("click", () => {
    const student = state.editing;
    $("student-edit-dialog").close();
    if (student) enrollExistingStudent(student.student_id);
  });

  $("edit-student-remove").addEventListener("click", () => {
    const student = state.editing;
    $("student-edit-dialog").close();
    if (student) openStudentRemove(student.student_id);
  });

  $("student-deactivate").addEventListener("click", deactivateStudent);
  $("student-delete").addEventListener("click", deleteStudent);
  $("student-remove-close").addEventListener("click",
                                             () => $("student-remove-dialog").close());
  $("student-remove-dialog").addEventListener("close", () => {
    $("student-delete").textContent = "Delete permanently";
    $("student-delete-confirm").value = "";
    $("student-delete-confirm-field").hidden = true;
  });

  $("camera-register").addEventListener("click", () => openCameraDialog(null));
  $("camera-save").addEventListener("click", saveCamera);

  $("cameras-refresh").addEventListener("click", refreshCameras);
  $("cameras-retry").addEventListener("click", refreshCameras);
  $("audit-refresh").addEventListener("click", () => loadAudit());

  $("auth-form").addEventListener("submit", submitAuth);
  $("account-logout").addEventListener("click", signOut);
  $("account-password").addEventListener("click",
                                         () => $("password-dialog").showModal());
  $("password-save").addEventListener("click", changeOwnPassword);
  $("password-close").addEventListener("click",
                                       () => $("password-dialog").close());
  $("password-form").addEventListener("submit",
                                      (event) => event.preventDefault());

  $("add-user").addEventListener("click", () => openUserDialog(null));
  bindReports();
  bindAssistant();
  bindAcademic();
  bindModels();
  $("user-add-class").addEventListener("click", addAssignment);
  $("user-save").addEventListener("click", saveUser);
  $("user-delete").addEventListener("click", deleteUser);
  $("user-close").addEventListener("click", () => $("user-dialog").close());
  $("user-form").addEventListener("submit", (event) => event.preventDefault());

  $("add-session").addEventListener("click", openSessionDialog);

  $("session-cancel-action").addEventListener("click", cancelSession);
  $("session-delete").addEventListener("click", deleteSession);
  $("session-remove-close").addEventListener("click",
                                             () => $("session-remove-dialog").close());
  $("session-remove-dialog").addEventListener("close", () => {
    $("session-delete-confirm").value = "";
    $("session-delete-confirm-field").hidden = true;
    state.removingSession = null;
  });
  $("session-create").addEventListener("click", createSession);

  /* Choosing a class previews who would be expected and ticks them. "change"
     rather than "input" so the request fires once the field is settled rather
     than on every keystroke. */
  $("new-session-class-name").addEventListener("change",
                                               previewExpectedStudents);
  $("new-session-section").addEventListener("change",
                                            previewExpectedStudents);

  /* Typing over the session identifier means it is no longer ours to fill in
     from the class. */
  $("new-session-class").addEventListener("input", (event) => {
    event.target.dataset.edited = "1";
  });

  $("roster-all").addEventListener("click", () => {
    document.querySelectorAll("#new-session-students input")
    .forEach((box) => { box.checked = true; });
    updateRosterCount();
  });

  $("roster-none").addEventListener("click", () => {
    document.querySelectorAll("#new-session-students input")
    .forEach((box) => { box.checked = false; });
    updateRosterCount();
  });

  $("live-start").addEventListener("click", startLive);
  $("live-stop").addEventListener("click", stopLive);

  $("early-end-confirm").addEventListener("click", async () => {
    $("early-end-dialog").close();
    await finishLive();
  });
  $("early-end-cancel").addEventListener("click",
                                         () => $("early-end-dialog").close());

  $("live-session").addEventListener("change", (event) => {
    state.liveSession = event.target.value;
  });

  $("live-controls").addEventListener("submit", (event) => event.preventDefault());

  $("override-apply").addEventListener("click", applyOverride);
  $("override-clear").addEventListener("click", clearOverride);

  await boot();
});

/*
   Decide between three screens before fetching anything:

   no accounts exist   -> create the first administrator
   accounts, signed out -> sign in
   signed in            -> the dashboard

   Nothing is requested until that is settled, so a signed-out browser never
   holds a single row of attendance data.
*/
async function boot() {
  let auth;
  try {
    auth = await api("/api/auth/state");
  } catch (failure) {
    /*
       Two very different failures, and telling them apart is what stops this
       page from becoming a dead end.

       404 means the SERVER is older than this page - the browser fetched the
       new index.html and app.js off disk while uvicorn is still running the
       Python it imported at start-up. That server has no accounts and no
       sign-in, so the honest thing is to let the dashboard through and say
       loudly what to do about it.

       Anything else means the backend is not answering at all, and pretending
       otherwise would be worse than saying so.
    */
    if (failure.status === 404) {
      hideAuthScreen();
      banner("This page is newer than the server running it: /api/auth/state " +
             "is missing, so accounts and sign-in are unavailable. Stop " +
             "uvicorn and start it again to load the current code.", true);
      await startDashboard();
      return;
    }

    /* No answer at all. Lock the screen rather than show an empty dashboard
       that looks like an institution with no students in it. */
    state.authMode = "login";
    showAuthScreen();
    $("auth-title").textContent = "Cannot reach ClassVision AI";
    $("auth-intro").textContent =
    "The dashboard could not contact the backend. Start it with " +
    "python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000, " +
    "then reload this page.";
    $("auth-form").hidden = true;
    authError(failure.message);
    return;
  }

  /*
     A student may already be signed in on this browser.

     Checked BEFORE the staff state is acted on, because the two cookies are
     independent and a machine in a library can hold either. /api/portal/state
     never requires a session, so asking costs one request and answers
     honestly when nobody is signed in.
  */
  try {
    const portal = await api("/api/portal/state");
    if (portal.authenticated) {
      portalState.student = portal.student;
      $("auth-form").hidden = false;
      return enterPortal();
    }
  } catch (ignored) {
    /* An older server has no portal. The staff dashboard still works, which
       is the point of not making this fatal. */
  }

  $("auth-form").hidden = false;
  state.auth = auth;
  renderAccount();

  /* No accounts exist: this installation has never been set up. Ask for the
     first Super Admin rather than for a password nobody has been given. */
  if (!auth.configured) {
    state.authMode = "setup";
    return showAuthScreen();
  }

  if (!auth.authenticated) {
    state.authMode = "login";
    return showAuthScreen();
  }

  hideAuthScreen();
  await startDashboard();
}

async function startDashboard() {
  hideAuthScreen();

  /* The threshold, over the console while the console loads.

     Deliberately NOT awaited. Everything below carries on fetching behind the
     scene, so by the time somebody has read the three lines beside the sphere
     the dashboard is already populated - the entry scene costs no loading
     time, it occupies some. */
  if (window.Spatial) window.Spatial.showEntry();

  await checkHealth();
  await loadClasses();
  /* Which recogniser is in production, for the hero. Once per sign-in, and it
     is allowed to fail: a hero that cannot name the model is cosmetic. */
  loadRuntimeIdentity().catch(() => {});
  try { await loadLiveSessions(); } catch (failure) { /* the live tab retries */ }
  await refresh();

  /* Tell the spatial layer which view is on screen.

     The first view nobody navigated to. switchView() is what normally hands
     the layer the current view, and it only runs when somebody clicks a tab -
     so on a fresh load the AI core's canvas was never started and the centre
     of the stage was an empty shell. This is the one call switchView() cannot
     make, because the first view was never switched to. */
  if (window.Spatial) window.Spatial.view(state.view);

  /* Nowhere to go back to yet, and the control says so by not being there. */
  renderBack();
}


/* =========================================================================
   THE SIGN-IN SCENE
   -------------------------------------------------------------------------
   A rotating wireframe globe on a 2D canvas.

   There is no three.js, no WebGL and no CDN. The points are generated on a
   Fibonacci sphere, rotated with two rotation matrices written out longhand,
   and projected with one divide. That is the whole 3D engine, it is about
   forty lines, and it keeps the promise the rest of this project makes: the
   dashboard loads and runs on a classroom machine with no network.

   It stops itself in three situations, and each one matters:

   prefers-reduced-motion   never starts. Somebody who asked for less
   motion is not served by a slower globe.
   the sign-in screen hides once signed in, the canvas is gone; a loop
   still running would burn a core all day.
   the tab goes to the      requestAnimationFrame already throttles in a
   background               hidden tab, and the visibility check makes it
   stop rather than crawl.
   ========================================================================= */

const AUTH_SCENE = {
  frame: null,
  points: null,
  stars: null,
  angle: 0,
  running: false
};

function prefersReducedMotion() {
  return window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/*
   Points spread evenly over a sphere.

   A naive two-loop lat/long grid bunches them at the poles and looks like a
   net rather than a globe. The golden-angle spiral gives an even covering for
   any count, which is why it is worth the two lines of arithmetic.
*/
function spherePoints(count) {
  const points = [];
  const golden = Math.PI * (3 - Math.sqrt(5));

  for (let index = 0; index < count; index += 1) {
    const y = 1 - (index / (count - 1)) * 2;
    const radius = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * index;
    points.push([Math.cos(theta) * radius, y, Math.sin(theta) * radius]);
  }
  return points;
}

/*
   A drift of stars, behind the lattice.

   Stored in a unit square and scaled at paint time, so a resize does not
   rebuild them and the field does not jump. The third number is a depth: it
   sets the size, the brightness AND how fast the star drifts, which is the
   whole of the parallax.
*/
function starField(count) {
  const stars = [];
  for (let index = 0; index < count; index += 1) {
    stars.push([Math.random(), Math.random(), Math.random()]);
  }
  return stars;
}

function buildScene() {
  AUTH_SCENE.points = AUTH_SCENE.points || spherePoints(260);
  AUTH_SCENE.stars = AUTH_SCENE.stars || starField(130);
}

/*
   The palette, read out of the page instead of written in here as well.

   The scene used to be two hard-coded blues, which meant it was drawn for
   the dark theme and for the dark theme only: the same pale violet dots on
   the light palette's near-white ground were a smudge. Reading the computed
   values means there is ONE definition of the gate's colours - the
   stylesheet - and the canvas follows a theme change for free.
*/
function sceneColour(style, name, fallback) {
  const raw = (style.getPropertyValue(name) || "").trim() || fallback;

  if (raw.charAt(0) === "#") {
    const hex = raw.length === 4
    ? raw.charAt(1) + raw.charAt(1) + raw.charAt(2) + raw.charAt(2) +
    raw.charAt(3) + raw.charAt(3)
    : raw.slice(1, 7);
    return [parseInt(hex.slice(0, 2), 16),
            parseInt(hex.slice(2, 4), 16),
            parseInt(hex.slice(4, 6), 16)];
  }

  /* rgb() or rgba(). Only the three channels are wanted; the alpha this file
     draws with is decided per mark, not taken from the token. */
  const parts = raw.replace(/[^0-9.,]/g, "").split(",");
  return [Number(parts[0]) || 0, Number(parts[1]) || 0, Number(parts[2]) || 0];
}

function scenePalette() {
  const style = window.getComputedStyle(document.body);
  const ground = sceneColour(style, "--bg", "#0a0514");

  /* Luminance, the cheap weighted version. It answers one question - is this
     a bright room or a dark one - and the answer only changes how much alpha
     the marks need, so three decimal places of colour science would be spent
     on nothing. */
  const luminance =
  (ground[0] * 0.299 + ground[1] * 0.587 + ground[2] * 0.114) / 255;

  return {
    primary: sceneColour(style, "--gate", "#b57cff"),
    secondary: sceneColour(style, "--gate-2", "#ff63c6"),
    /* Ink on white needs more alpha to register than light on black does. */
    weight: luminance > 0.5 ? 1.8 : 1
  };
}

function rgba(channels, alpha) {
  return "rgba(" + channels[0] + ", " + channels[1] + ", " + channels[2] +
  ", " + alpha.toFixed(3) + ")";
}

function drawAuthScene(canvas, context) {
  const width = canvas.width;
  const height = canvas.height;
  /* Larger than it used to be and fainter with it, so the lattice reads as
     the room the gate is in rather than as a second object competing with the
     cube the stylesheet draws. */
  const scale = Math.min(width, height) * 0.46;
  const centreX = width * 0.5;
  const centreY = height * 0.5;
  const palette = scenePalette();
  const weight = palette.weight;

  context.clearRect(0, 0, width, height);

  /* ---- the stars ---- */

  const drift = AUTH_SCENE.angle;
  for (const [x, y, depth] of AUTH_SCENE.stars) {
    /* Each star drifts right at its own depth's rate and wraps. Nearer stars
       are bigger, brighter and faster, which is the only cue needed. */
    const across = (x + drift * (0.08 + depth * 0.22)) % 1;
    context.fillStyle = rgba(depth > 0.78 ? palette.secondary : palette.primary,
                             (0.1 + depth * 0.34) * weight);
    context.beginPath();
    context.arc(across * width, y * height, 0.5 + depth * 1.4, 0, Math.PI * 2);
    context.fill();
  }

  /* ---- the lattice ---- */

  const angle = AUTH_SCENE.angle;
  const sinY = Math.sin(angle);
  const cosY = Math.cos(angle);
  /* A fixed tilt, so the poles are visible and it reads as a globe rather
     than a spinning disc. */
  const sinX = Math.sin(0.42);
  const cosX = Math.cos(0.42);

  const projected = [];

  for (const [x, y, z] of AUTH_SCENE.points) {
    /* Rotate about Y, then about X. Written out rather than multiplied as
       matrices because two rotations do not justify a matrix class. */
    const x1 = x * cosY - z * sinY;
    const z1 = x * sinY + z * cosY;
    const y2 = y * cosX - z1 * sinX;
    const z2 = y * sinX + z1 * cosX;

    /* One-point perspective. Nearer points spread out and brighten. */
    const depth = 1 / (2.4 - z2);
    projected.push({
      x: centreX + x1 * scale * depth * 2.4,
      y: centreY + y2 * scale * depth * 2.4,
      z: z2
    });
  }

  /* Edges between points that are near each other in 3D. Drawn before the
     dots so the dots sit on top. */
  context.lineWidth = 1;
  for (let a = 0; a < projected.length; a += 1) {
    for (let b = a + 1; b < a + 5 && b < projected.length; b += 1) {
      const dx = projected[a].x - projected[b].x;
      const dy = projected[a].y - projected[b].y;
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance > scale * 0.55) continue;

      const near = (projected[a].z + projected[b].z) * 0.5;
      context.strokeStyle = rgba(palette.primary,
                                 (0.035 + Math.max(0, near) * 0.1) * weight);
      context.beginPath();
      context.moveTo(projected[a].x, projected[a].y);
      context.lineTo(projected[b].x, projected[b].y);
      context.stroke();
    }
  }

  for (const point of projected) {
    const near = (point.z + 1) * 0.5;
    /* The nearest tenth of the lattice takes the second hue. It is the only
       thing in the scene that is not one colour, and it is what stops a
       quarter of a million pixels of violet reading as a flat wash. */
    context.fillStyle = rgba(near > 0.9 ? palette.secondary : palette.primary,
                             (0.1 + near * 0.4) * weight);
    context.beginPath();
    context.arc(point.x, point.y, 0.6 + near * 1.5, 0, Math.PI * 2);
    context.fill();
  }
}

function resizeAuthCanvas(canvas) {
  /* Draw at device resolution so the dots are not soft on a retina screen,
     but cap the ratio: a 3x buffer on a large monitor is four times the
     pixels for no visible gain. */
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
}

function startAuthScene() {
  bindGateLean();

  const canvas = $("auth-canvas");
  if (!canvas || !canvas.getContext) return;

  if (prefersReducedMotion()) {
    /* One still frame. The scene is part of the design, and a blank
       rectangle where it should be looks like a broken page - but nothing
       moves. */
    buildScene();
    resizeAuthCanvas(canvas);
    drawAuthScene(canvas, canvas.getContext("2d"));
    return;
  }

  if (AUTH_SCENE.running) return;
  AUTH_SCENE.running = true;
  buildScene();

  const context = canvas.getContext("2d");
  resizeAuthCanvas(canvas);

  const step = () => {
    /* The screen went away, or we signed in. Stop rather than draw into
       something nobody can see. */
    if (!AUTH_SCENE.running || $("auth-screen").hidden) {
      AUTH_SCENE.running = false;
      AUTH_SCENE.frame = null;
      return;
    }

    if (!document.hidden) {
      AUTH_SCENE.angle += 0.0022;
      drawAuthScene(canvas, context);
    }
    AUTH_SCENE.frame = window.requestAnimationFrame(step);
  };

  AUTH_SCENE.frame = window.requestAnimationFrame(step);

  window.addEventListener("resize", () => {
    if (AUTH_SCENE.running) resizeAuthCanvas(canvas);
  });
}

/* Redraw the sign-in scene after a theme change.

   Only matters under prefers-reduced-motion, where the scene is a single still
   frame drawn once - the animated version repaints sixty times a second and
   picks up any change on its own. Doing nothing when the screen is not up is
   the point: this must not resurrect a scene that stopped itself. */
function repaintAuthScene() {
  const canvas = $("auth-canvas");
  if (!canvas || !canvas.getContext) return;
  if (!canvas.isConnected || $("auth-screen").hidden) return;
  if (!prefersReducedMotion()) return;

  buildScene();
  resizeAuthCanvas(canvas);
  drawAuthScene(canvas, canvas.getContext("2d"));
}

function stopAuthScene() {
  AUTH_SCENE.running = false;
  if (AUTH_SCENE.frame) {
    window.cancelAnimationFrame(AUTH_SCENE.frame);
    AUTH_SCENE.frame = null;
  }
}


/* =========================================================================
   THE STUDENT PORTAL
   -------------------------------------------------------------------------
   A student signing in to see their own attendance.

   Everything here calls /api/portal/*, which resolves the student from a
   SEPARATE cookie against a SEPARATE table. No function below sends a
   student id anywhere, because none of the endpoints accept one - there is
   no URL in this section a curious sixteen-year-old could edit.
   ========================================================================= */

const portalState = { student: null, offset: 0, limit: 25, total: 0 };

function portalError(message) {
  const node = $("portal-password-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function portalSignIn(username, password) {
  const student = await apiSend("/api/portal/login", "POST",
                                { username, password });
  portalState.student = student;
  stopAuthScene();
  await enterPortal();
}

/*
   Show the portal, or the change-password gate in front of it.

   The gate is the SERVER'S rule, not this page's: /api/portal/me/attendance
   answers 403 until the handed-out password has been replaced. This only puts
   the dialog in front of it so somebody sees a form rather than an error.
*/
async function enterPortal() {
  document.body.classList.remove("locked");
  $("auth-screen").hidden = true;
  $("portal-main").hidden = false;

  /* The staff chrome is not merely hidden - it holds no data, because none
     was fetched. A student's browser never called a staff endpoint.

     Hidden as ONE unit: the rail, the top bar and the dashboard are all inside
     .app-shell, and hiding them individually is how a student ends up looking
     at an Administration link. */
  const shell = document.querySelector(".app-shell");
  if (shell) shell.hidden = true;

  if (portalState.student && portalState.student.must_change) {
    portalError("");
    $("portal-password-dialog").showModal();
    return;
  }

  await loadPortal();
}

async function loadPortal() {
  let performance;
  try {
    performance = await api("/api/portal/me/performance");
  } catch (failure) {
    return portalFailure(failure);
  }

  const student = performance.student || {};
  const attendance = performance.attendance || {};

  $("portal-who").textContent = [
    student.name,
    student.roll_number ? "Roll " + student.roll_number : null,
    student.class_name
    ? student.class_name + (student.section ? "-" + student.section : "")
    : null
  ].filter(Boolean).join(" · ");

  /* A rate of null is "no sessions yet", which is a different fact from 0%
     and must not be drawn as an empty ring with a confident number in it. */
  const rate = attendance.attendance_rate;
  const ring = $("portal-ring");
  const value = $("portal-ring-value");

  if (rate === null || rate === undefined) {
    ring.style.setProperty("--pct", 0);
    value.textContent = "—";
    $("portal-summary").textContent =
    "You have not been expected at any completed session yet, so there is " +
    "no attendance rate to show.";
  } else {
    const percentage = Math.round(rate * 1000) / 10;
    ring.style.setProperty("--pct", percentage);
    value.textContent = percentage + "%";
    $("portal-summary").textContent =
    "Attended " + (attendance.attended_sessions || 0) + " of " +
    (attendance.expected_sessions || 0) + " sessions you were expected at.";
  }

  $("portal-cards").replaceChildren(
    card("Expected", attendance.expected_sessions ?? 0),
    card("Attended", attendance.attended_sessions ?? 0),
    card("Recorded", attendance.recorded_sessions ?? 0),
    card("Attendance", rate === null || rate === undefined
         ? null : percent(rate)));

  await loadPortalHistory();
}

async function loadPortalHistory() {
  let page;
  try {
    page = await api("/api/portal/me/attendance?limit=" + portalState.limit +
                     "&offset=" + portalState.offset);
  } catch (failure) {
    return portalFailure(failure);
  }

  portalState.total = page.total || 0;
  const body = $("portal-history").querySelector("tbody");

  if (!page.items || !page.items.length) {
    body.replaceChildren(el("tr", null, [
      el("td", { colspan: "4", class: "muted",
                text: "Nothing recorded yet." })
    ]));
  } else {
    body.replaceChildren(...page.items.map((row) => el("tr", null, [
      el("td", { text: row.session_date || "—" }),
      el("td", { text: row.class_id || "—" }),
      el("td", null, [statusPill(row.status)]),
      el("td", { class: "muted small", text: row.reason || "—" })
    ])));
  }

  renderPortalPager();
}

function renderPortalPager() {
  const pager = $("portal-pager");
  const shown = Math.min(portalState.offset + portalState.limit,
                         portalState.total);

  pager.replaceChildren(
    el("span", { text: portalState.total
                ? "Showing " + (portalState.offset + 1) + "–" + shown + " of " +
                portalState.total
                : "No sessions" }));

  if (portalState.offset > 0) {
    const previous = el("button", { class: "btn", type: "button",
                                   text: "Previous" });
    previous.addEventListener("click", () => {
      portalState.offset = Math.max(0, portalState.offset - portalState.limit);
      loadPortalHistory();
    });
    pager.appendChild(previous);
  }

  if (shown < portalState.total) {
    const next = el("button", { class: "btn", type: "button", text: "Next" });
    next.addEventListener("click", () => {
      portalState.offset += portalState.limit;
      loadPortalHistory();
    });
    pager.appendChild(next);
  }
}

function portalFailure(failure) {
  /* A 403 here means the password gate: the server refused because the
     handed-out password is still in place. Anything else is a real error. */
  if (failure.status === 403) {
    portalError("");
    return $("portal-password-dialog").showModal();
  }
  if (failure.status === 401) return portalSignOut();
  banner(failure.message, true);
}

async function savePortalPassword() {
  const current = $("portal-current-password").value;
  const next = $("portal-new-password").value;

  if (!current || !next) {
    return portalError("Fill in both boxes.");
  }

  setBusy(true);
  try {
    await apiSend("/api/portal/me/password", "POST", {
      current_password: current, new_password: next });
    $("portal-password-dialog").close();
    /* The server dropped every portal session, this one included. Sending
       them back to sign-in is the honest next step, not a bug. */
    window.location.reload();
  } catch (failure) {
    portalError(failure.message);
  } finally {
    setBusy(false);
  }
}

async function portalSignOut() {
  try { await apiSend("/api/portal/logout", "POST", null); }
  catch (ignored) { /* signing out locally matters more than the round trip */ }
  window.location.reload();
}


/* =========================================================================
   EDIT A SESSION THAT WAS SCHEDULED WRONGLY
   -------------------------------------------------------------------------
   PATCH /api/sessions/{id} has existed since the lifecycle work; this is the
   form for it.

   The rule the dialog exists to express: a session that has produced no
   evidence can be corrected freely, and one that HAS is a different
   conversation. Changing the window of a session that already has attendance
   needs confirm_correction, keeps every record exactly as it is, and flags
   the session for review. Nothing is recomputed - re-deriving a past
   register from an edited plan would be inventing a lesson that did not
   happen.

   Which of those two cases applies is decided by the SERVER. This sends the
   change without confirmation first and shows the checkbox only when it is
   refused with a 409, so the browser never has to hold a second copy of the
   policy that could drift from the real one.
   ========================================================================= */

let editingSession = null;

function sessionEditError(message) {
  const node = $("session-edit-error");
  node.textContent = message || "";
  node.hidden = !message;
}

/*
   An instant -> "14:30" in the reader's own timezone, for <input type="time">.

   Parsed rather than sliced. The stored value carries an offset, so the
   characters after the T are the time in whatever zone it was written in -
   slicing them shows a teacher in one timezone the wall clock of another, and
   saving the form back would then silently move the lesson.
*/
function timeOfDay(iso) {
  if (!iso) return "";
  const moment = new Date(iso);
  if (isNaN(moment.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  return pad(moment.getHours()) + ":" + pad(moment.getMinutes());
}

async function openSessionEdit(sessionId) {
  let details;
  try {
    details = await api("/api/sessions/" + encodeURIComponent(sessionId) +
                        "/details");
  } catch (failure) {
    return banner(failure.message, true);
  }

  editingSession = details;
  sessionEditError("");

  $("session-edit-who").textContent =
  details.session_id + " · " + (details.status || "");

  $("session-edit-class").value = details.class_id || "";
  $("session-edit-section").value = details.section || "";
  $("session-edit-teacher").value = details.teacher || "";
  $("session-edit-date").value = details.session_date || "";
  $("session-edit-start").value = timeOfDay(details.start_time);
  $("session-edit-end").value = timeOfDay(details.end_time);

  /* Reset between openings, so a confirmation ticked for one session can
     never be carried into another. */
  $("session-edit-confirm-field").hidden = true;
  $("session-edit-confirm").checked = false;

  $("session-edit-dialog").showModal();
}

function sessionEditPayload() {
  const date = $("session-edit-date").value;
  const start = $("session-edit-start").value;
  const end = $("session-edit-end").value;

  const payload = {
    class_id: $("session-edit-class").value.trim(),
    section: $("session-edit-section").value.trim(),
    teacher: $("session-edit-teacher").value.trim()
  };

  if (date) payload.session_date = date;

  /*
     The API takes full timestamps WITH an offset, and refuses one without:
     a naive "09:00" is a different moment in two schools and the server will
     not guess which was meant.

     localToIso is the same helper the scheduling dialog uses. Reusing it
     rather than concatenating strings is what makes an edited session land in
     the same instant a newly created one would - and the first version of
     this function did concatenate, which the API rejected outright.
  */
  if (date && start) {
    const iso = localToIso(date, start);
    if (iso) payload.start_time = iso;
  }
  if (date && end) {
    const iso = localToIso(date, end);
    if (iso) payload.end_time = iso;
  }

  return payload;
}

async function saveSessionEdit() {
  if (!editingSession) return;

  const sessionId = editingSession.session_id;
  const confirmed = $("session-edit-confirm").checked;

  setBusy(true);
  try {
    await apiSend("/api/sessions/" + encodeURIComponent(sessionId) +
                  (confirmed ? "?confirm_correction=true" : ""),
                  "PATCH", sessionEditPayload());
    $("session-edit-dialog").close();
    banner(sessionId + " updated.");
    await refreshAfterSessionChange();
  } catch (failure) {
    /* 409 is the server saying "this one has attendance". Show the
       confirmation and what it means, rather than the raw refusal. */
    if (failure.status === 409 && $("session-edit-confirm-field").hidden) {
      $("session-edit-confirm-field").hidden = false;
      sessionEditError(failure.message);
    } else {
      sessionEditError(failure.message);
    }
  } finally {
    setBusy(false);
  }
}


/* =========================================================================
   ABSENCE ALERTS
   -------------------------------------------------------------------------
   The rules a school sets, and whether anything can actually be sent.

   The delivery line at the top is the important part. Without it a school
   sees alerts appearing in a table and reasonably concludes parents are
   being told - when by default nothing leaves the building at all. It says
   so out loud, in the same place the rules are edited.
   ========================================================================= */

const RULE_LABEL = {
  attendance_below: "Attendance below",
  consecutive_absence: "Absent sessions in a row",
  consecutive_absent_days: "Absent days in a row",
  periodic_report: "Periodic report"
};

async function loadAlertDelivery() {
  let delivery;
  try {
    delivery = await api("/api/alerts/delivery");
  } catch (failure) {
    return;
  }

  const text = $("alerts-delivery-text");
  const line = $("alerts-delivery");

  if (delivery.configured) {
    text.textContent =
    "Email is configured: alerts are sent from " + delivery.sender +
    " via " + delivery.host + ":" + delivery.port +
    (delivery.password_set ? "" : " (no password set - sending may fail).");
    line.querySelector(".sym").textContent = "✓";
  } else {
    text.textContent =
    "No mail server is configured, so alerts are recorded here and " +
    "nothing is sent to anybody. Set CLASSVISION_SMTP_HOST and " +
    "CLASSVISION_SMTP_FROM in the environment to turn delivery on.";
    line.querySelector(".sym").textContent = "!";
  }
}

async function loadAlertRules() {
  let rules;
  try {
    rules = (await api("/api/alerts/rules")).items || [];
  } catch (failure) {
    return banner(failure.message, true);
  }

  const body = $("alert-rules-table").querySelector("tbody");

  if (!rules.length) {
    body.replaceChildren(el("tr", null, [
      el("td", { colspan: "6", class: "muted",
                text: "No rules yet. Nothing is evaluated and nobody is " +
                "contacted until one is added." })
    ]));
    return;
  }

  body.replaceChildren(...rules.map((rule) => {
    const scope = [rule.class_name, rule.section].filter(Boolean).join("-");

    const toggle = el("button", { class: "btn", type: "button",
                                 text: rule.enabled ? "Disable" : "Enable" });
    toggle.addEventListener("click", () => saveRule(rule,
                                                    { enabled: !rule.enabled }));

    const remove = el("button", { class: "btn btn-danger", type: "button",
                                 text: "Remove" });
    remove.addEventListener("click", () => removeRule(rule.rule_id));

    return el("tr", null, [
      el("td", null, [
        el("strong", { text: rule.name || rule.rule_id }),
        el("div", { class: "muted small", text: rule.message || "" })
      ]),
      el("td", { class: "small",
                text: RULE_LABEL[rule.rule_type] || rule.rule_type }),
      el("td", { class: "num", text: rule.threshold === null ||
                rule.threshold === undefined
                ? (rule.window_days ? rule.window_days + " days" : "—")
                : (rule.rule_type === "attendance_below"
                   ? Math.round(rule.threshold * 100) + "%"
                   : String(rule.threshold)) }),
      el("td", { class: "small muted", text: scope || "Every class" }),
      el("td", null, [
        el("span", { class: "pill pill-" + (rule.enabled ? "ACTIVE" : "INACTIVE"),
                    text: rule.enabled ? "Enabled" : "Disabled" })
      ]),
      el("td", { class: "row-actions" }, [toggle, remove])
    ]);
  }));
}

async function saveRule(rule, changes) {
  setBusy(true);
  try {
    await apiSend("/api/alerts/rules/" + encodeURIComponent(rule.rule_id),
                  "PUT", Object.assign({
                    name: rule.name,
                    rule_type: rule.rule_type,
                    threshold: rule.threshold,
                    window_days: rule.window_days,
                    class_name: rule.class_name,
                    section: rule.section,
                    message: rule.message,
                    enabled: rule.enabled
                  }, changes));
    await loadAlertRules();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function removeRule(ruleId) {
  setBusy(true);
  try {
    await apiSend("/api/alerts/rules/" + encodeURIComponent(ruleId),
                  "DELETE", null);
    banner(ruleId + " removed. No history was deleted - a rule is not a record.");
    await loadAlertRules();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function runAlerts(dryRun) {
  setBusy(true);
  try {
    const result = await apiSend("/api/alerts/evaluate", "POST",
                                 { dry_run: Boolean(dryRun) });
    const raised = (result.raised || []).length;

    banner(dryRun
           ? "Preview: " + raised + " alert(s) would be raised. Nothing was " +
           "recorded and nobody was contacted."
           : raised + " alert(s) raised. See the delivery column for what " +
           "actually left the building.");

    if (!dryRun) await loadNotifications();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function loadNotifications() {
  let items;
  try {
    items = (await api("/api/alerts/notifications?limit=50")).items || [];
  } catch (failure) {
    return;
  }

  const body = $("notifications-table").querySelector("tbody");

  if (!items.length) {
    body.replaceChildren(el("tr", null, [
      el("td", { colspan: "5", class: "muted", text: "Nothing raised yet." })
    ]));
    return;
  }

  body.replaceChildren(...items.map((row) => el("tr", null, [
    el("td", { class: "small", text: (row.generated_at || "").slice(0, 16)
              .replace("T", " ") }),
    el("td", { text: row.student_id || "—" }),
    el("td", { class: "small", text: row.rule_id || "—" }),
    el("td", { class: "num", text: row.measured_value === null ||
              row.measured_value === undefined
              ? "—" : String(row.measured_value) }),
    el("td", null, [
      el("span", { class: "pill pill-" +
                  (row.delivery_status === "SENT" ? "PRESENT"
                   : row.delivery_status === "FAILED" ? "ABSENT" : "UNKNOWN"),
                  title: row.failure_reason || "",
                  text: row.delivery_status || "—" })
    ])
  ])));
}

async function addDefaultRules() {
  setBusy(true);
  try {
    const result = await apiSend("/api/alerts/rules/defaults", "POST", null);
    banner(result.count
           ? "Added " + result.count + " suggested rule(s). Review the thresholds " +
           "before running them - they are common numbers, not this school's."
           : "The suggested rules are already present.");
    await loadAlertRules();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}


/* =========================================================================
   STUDENT PORTAL ACCOUNTS (administration)
   ========================================================================= */

function portalAccountError(message) {
  const node = $("portal-account-error");
  node.textContent = message || "";
  node.hidden = !message;
}

async function loadPortalAccounts() {
  let accounts;
  try {
    accounts = await api("/api/portal/accounts");
  } catch (failure) {
    return;
  }

  const body = $("portal-accounts-table").querySelector("tbody");

  if (!accounts.length) {
    body.replaceChildren(el("tr", null, [
      el("td", { colspan: "6", class: "muted",
                text: "No student has a portal login yet." })
    ]));
    return;
  }

  body.replaceChildren(...accounts.map((account) => {
    const active = account.status === "ACTIVE";

    const suspend = el("button", { class: "btn", type: "button",
                                  text: active ? "Suspend" : "Restore" });
    suspend.addEventListener("click", () => setPortalAccountStatus(
      account.student_id, active ? "INACTIVE" : "ACTIVE"));

    const reset = el("button", { class: "btn", type: "button",
                                text: "Reset password" });
    reset.addEventListener("click", () => resetPortalPassword(account));

    const revoke = el("button", { class: "btn btn-danger", type: "button",
                                 text: "Remove" });
    revoke.addEventListener("click", () => revokePortalAccount(account));

    return el("tr", null, [
      el("td", null, [
        el("strong", { text: account.name || account.student_id }),
        el("div", { class: "muted small", text: account.student_id })
      ]),
      el("td", { class: "small", text: [account.class_name, account.section]
                .filter(Boolean).join("-") || "—" }),
      el("td", { text: account.username }),
      el("td", null, [
        el("span", { class: "pill pill-" + (active ? "ACTIVE" : "INACTIVE"),
                    text: active ? "Active" : "Suspended" })
      ]),
      el("td", { class: "small muted",
                text: (account.last_login_at || "never").slice(0, 16)
                .replace("T", " ") }),
      el("td", { class: "row-actions" }, [suspend, reset, revoke])
    ]);
  }));
}

async function setPortalAccountStatus(studentId, status) {
  setBusy(true);
  try {
    await apiSend("/api/portal/accounts/" + encodeURIComponent(studentId) +
                  "/status?status=" + status, "POST", null);
    banner(studentId + " portal access " +
           (status === "ACTIVE" ? "restored." : "suspended. Their attendance " +
            "history is unchanged."));
    await loadPortalAccounts();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function resetPortalPassword(account) {
  const password = window.prompt(
    "New password for " + (account.name || account.student_id) +
    "\n\nAt least 10 characters. They will be asked to change it when they " +
    "next sign in, and every open session of theirs is ended now.");

  if (!password) return;

  setBusy(true);
  try {
    await apiSend("/api/portal/accounts/" +
                  encodeURIComponent(account.student_id) + "/password",
                  "POST", { password });
    banner("Password reset for " + account.username +
           ". Give it to them directly - it cannot be read back.");
    await loadPortalAccounts();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function revokePortalAccount(account) {
  /* Deliberately spelling out what is and is not destroyed. "Delete" means
     two very different things in this project and this is the harmless one. */
  const ok = window.confirm(
    "Remove the portal login for " + (account.name || account.student_id) +
    "?\n\nTheir attendance history, registry row and face samples are NOT " +
    "touched - only the password is removed. Suspending is reversible; this " +
    "is not, and a new login would have to be issued.");

  if (!ok) return;

  setBusy(true);
  try {
    await apiSend("/api/portal/accounts/" +
                  encodeURIComponent(account.student_id), "DELETE", null);
    banner("Portal login removed. No attendance record was affected.");
    await loadPortalAccounts();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}

async function openPortalAccountDialog() {
  portalAccountError("");
  $("portal-account-username").value = "";
  $("portal-account-password").value = "";
  $("portal-account-student").onchange = null;

  const select = $("portal-account-student");
  select.replaceChildren(el("option", { value: "", text: "Loading…" }));

  try {
    const page = await api("/api/students?limit=200&status=ACTIVE");
    const existing = new Set((await api("/api/portal/accounts"))
                             .map((account) => account.student_id));

    const options = (page.items || [])
    .filter((student) => !existing.has(student.student_id))
    /* Name, Student ID, and the class when there is one.

       Names are not unique and are not meant to be - three children called
       Rahul Kumar is an ordinary register. The Student ID is the identity
       and is always shown; the class is what actually lets an office tell
       two same-named students apart at a glance. */
    .map((student) => el("option", {
      value: student.student_id,
      text: student.name + " — " + student.student_id
      + (student.class_name
         ? " — " + student.class_name
         + (student.section ? "-" + student.section : "")
         : "")
    }));

    select.replaceChildren(...(options.length ? options : [
      el("option", { value: "",
                    text: "Every active student already has a login" })
    ]));

    /* The username follows the chosen student unless somebody types over it.
       The Student ID is the default the server uses when none is sent, so
       showing it here means the field agrees with what would happen if it
       were left alone. */
    const fillUsername = () => {
      const field = $("portal-account-username");
      if (!field.dataset.edited) field.value = select.value;
    };
    select.onchange = fillUsername;
    $("portal-account-username").addEventListener("input", (event) => {
      event.target.dataset.edited = "1";
    });
    fillUsername();
  } catch (failure) {
    portalAccountError(failure.message);
  }

  $("portal-account-dialog").showModal();
}

async function savePortalAccount() {
  const studentId = $("portal-account-student").value;
  const username = $("portal-account-username").value.trim();
  const password = $("portal-account-password").value;

  if (!studentId) return portalAccountError("Choose a student.");

  /* Both fields may be left blank now. The server defaults the username to
     the Student ID and generates the password, and sending null rather than
     "" is what reaches that default instead of failing validation on an empty
     string. */
  setBusy(true);
  try {
    const account = await apiSend("/api/portal/accounts", "POST", {
      student_id: studentId,
      username: username || null,
      password: password || null
    });
    $("portal-account-dialog").close();

    if (account.password) {
      showIssuedLogins([account]);
      banner("Portal login issued for " + (account.name || studentId) +
             " as " + account.username + ". The password is shown below " +
             "once - it cannot be read back, only reset.");
    } else {
      banner("Portal login issued for " + (account.name || studentId) +
             " as " + account.username + ". Write the password down now - it " +
             "cannot be read back.");
    }
    await loadPortalAccounts();
  } catch (failure) {
    portalAccountError(failure.message);
  } finally {
    setBusy(false);
  }
}

/* Generated first passwords, shown exactly once.

   Rendered into the page rather than a dialog on purpose: this is a list the
   office has to be able to read off or print, and a modal that closes takes
   credentials nobody wrote down with it. It is cleared when the Administration
   tab is left - see switchView - so a class's logins are not left on screen. */
function showIssuedLogins(accounts) {
  const container = $("portal-issued");

  container.replaceChildren(
    el("h3", { text: "New logins (" + accounts.length + ")" }),
    el("p", { class: "muted small", text:
             "Shown once. Each password is stored only as a PBKDF2 hash and cannot " +
             "be read back — a lost one is reset, never recovered." }),
    el("div", { class: "table-scroll" }, [
      el("table", { class: "data-table" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "Student" }),
            el("th", { scope: "col", text: "Student ID / username" }),
            el("th", { scope: "col", text: "First password" })
          ])
        ]),
        el("tbody", {}, accounts.map((account) => el("tr", {}, [
          el("td", { text: account.name || "—" }),
          el("td", { text: account.username }),
          el("td", { class: "credential", text: account.password })
        ])))
      ])
    ]),
    el("button", { class: "btn btn-quiet", type: "button",
                  text: "Hide these", id: "portal-issued-hide" })
  );

  container.hidden = false;
  container.querySelector("#portal-issued-hide")
  .addEventListener("click", clearIssuedLogins);
  container.scrollIntoView({ block: "nearest" });
}

function clearIssuedLogins() {
  const container = $("portal-issued");
  container.replaceChildren();
  container.hidden = true;
}

/* Give every registered student who has not got a login one.

   Confirmed first, because it is a bulk write and the number of accounts it
   creates is the thing somebody wants to know before pressing it. It resets
   nothing: a student who already has a login is skipped by the server. */
async function issueMissingPortalLogins() {
  if (!window.confirm(
    "Issue a portal login to every registered student who has not got " +
    "one?\n\nThe username will be their Student ID and the password " +
    "will be generated. Students who already have a login are skipped " +
    "and their passwords are not changed.")) {
      return;
    }

  setBusy(true);
  try {
    const result = await apiSend("/api/portal/accounts/issue-missing",
                                 "POST", null);
    const issued = result.issued || [];
    const skipped = result.skipped || [];

    if (issued.length) {
      showIssuedLogins(issued);
    } else {
      clearIssuedLogins();
    }

    banner(issued.length
           ? "Issued " + issued.length + " login(s)" +
           (skipped.length ? ", skipped " + skipped.length : "") +
           ". The passwords are shown below once."
           : "Every registered student already has a portal login. Nothing was " +
           "changed.");

    await loadPortalAccounts();
  } catch (failure) {
    banner(failure.message, true);
  } finally {
    setBusy(false);
  }
}


/* =========================================================================
   TEACHER PHOTOGRAPH AND DOCUMENTS
   -------------------------------------------------------------------------
   The personnel side of a staff account.

   This is NOT enrollment. No embedding is computed, the recognition pipeline
   never opens these files, and a teacher with a photograph on file is not
   recognisable to a camera. It is the picture on an ID card and the
   documents a school office keeps, and the server enforces the same thing -
   backend/services/staff_files_service.py imports nothing from ai/.

   The camera here is the BROWSER'S, via getUserMedia, and it is used only to
   take one still. That is the opposite of the student enrollment camera,
   which is opened server-side and never sends a frame to the browser. The
   difference is deliberate: a student's face is biometric data that must not
   travel, and a staff ID photo is a photograph somebody is choosing to have
   taken.
   ========================================================================= */

const capture = { stream: null, userId: null, dataUrl: null };

async function startCapture() {
  stopCapture();

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return userError("This browser will not give the page a camera. Choose " +
                     "an image file instead.");
  }

  try {
    capture.stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 } },
      audio: false
    });
  } catch (failure) {
    return userError("The camera could not be opened: " + failure.message);
  }

  const video = $("capture-video");
  video.srcObject = capture.stream;
  video.hidden = false;
  $("capture-preview").hidden = true;
  await video.play().catch(() => { /* autoplay refusal is not fatal */ });

  $("capture-take").hidden = false;
  $("capture-start").textContent = "Restart camera";
}

function stopCapture() {
  if (capture.stream) {
    capture.stream.getTracks().forEach((track) => track.stop());
    capture.stream = null;
  }
  const video = $("capture-video");
  if (video) {
    video.srcObject = null;
    video.hidden = true;
  }
  const take = $("capture-take");
  if (take) take.hidden = true;
}

/*
   One still, as a JPEG data URL.

   JPEG at 0.86 rather than PNG: a 640x480 PNG portrait is roughly 600 KB and
   the same picture as JPEG is about 60, and this goes through a JSON body.
*/
function takePhoto() {
  const video = $("capture-video");
  if (!video || !video.videoWidth) return;

  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);

  capture.dataUrl = canvas.toDataURL("image/jpeg", 0.86);

  const preview = $("capture-preview");
  preview.src = capture.dataUrl;
  preview.hidden = false;
  video.hidden = true;

  stopCapture();
  $("capture-start").textContent = "Retake";
  userError("");
}

/* An uploaded file, for a machine with no camera or a photo taken on a
   phone. Read to a data URL so both paths hand the server the same shape. */
function readPhotoFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = () => {
    capture.dataUrl = String(reader.result);
    const preview = $("capture-preview");
    preview.src = capture.dataUrl;
    preview.hidden = false;
    $("capture-video").hidden = true;
    stopCapture();
    userError("");
  };
  reader.onerror = () => userError("That file could not be read.");
  reader.readAsDataURL(file);
}

async function uploadStaffPhoto(userId) {
  if (!capture.dataUrl) return;
  await apiSend("/api/users/" + userId + "/photo", "PUT",
                { image: capture.dataUrl });
  capture.dataUrl = null;
}

function staffPhotoNode(user, small) {
  /*
     A cache-busting suffix on the photo URL.

     Without it, replacing a teacher's photograph leaves the old one on screen
     until a hard refresh, because the response is deliberately cacheable for
     five minutes. updated_at changes whenever the row does, so it is a
     version that costs no extra request.
  */
  const node = el("div", {
    class: "staff-photo" + (small ? " small" : "") +
    (user.photo_name ? "" : " staff-photo-empty"),
    role: "img",
    "aria-label": user.photo_name
    ? "Photograph of " + (user.display_name || user.username)
    : "No photograph on file"
  }, user.photo_name ? null : [document.createTextNode("No photo")]);

  if (user.photo_name) {
    node.style.backgroundImage = "url(/api/users/" + user.user_id +
    "/photo?v=" + encodeURIComponent(user.updated_at || "") + ")";
  }
  return node;
}

async function loadStaffDocuments(userId) {
  const list = $("user-documents");
  if (!list) return;

  if (!userId) {
    list.replaceChildren(el("p", { class: "muted small",
                                  text: "Documents can be added once the account exists." }));
    return;
  }

  let items;
  try {
    items = (await api("/api/users/" + userId + "/documents")).items || [];
  } catch (failure) {
    return list.replaceChildren(el("p", { class: "muted small",
                                         text: failure.message }));
  }

  if (!items.length) {
    return list.replaceChildren(el("p", { class: "muted small",
                                         text: "Nothing on file." }));
  }

  list.replaceChildren(...items.map((doc) => {
    const open = el("a", { class: "btn", text: "Download",
                          href: "/api/users/" + userId + "/documents/" +
                          doc.document_id });
    const remove = el("button", { class: "btn btn-danger", type: "button",
                                 text: "Remove" });
    remove.addEventListener("click", async () => {
      setBusy(true);
      try {
        await apiSend("/api/users/" + userId + "/documents/" + doc.document_id,
                      "DELETE", null);
        await loadStaffDocuments(userId);
      } catch (failure) {
        userError(failure.message);
      } finally {
        setBusy(false);
      }
    });

    return el("div", { class: "doc-row" }, [
      el("span", { class: "doc-name", text: doc.label
                  ? doc.label + " — " + doc.filename : doc.filename }),
      el("span", { class: "doc-size",
                  text: Math.max(1, Math.round(doc.byte_size / 1024)) + " KB" }),
      open, remove
    ]);
  }));
}

function addStaffDocument(userId, input) {
  const file = input.files && input.files[0];
  if (!file || !userId) return;

  const reader = new FileReader();
  reader.onload = async () => {
    setBusy(true);
    try {
      await apiSend("/api/users/" + userId + "/documents", "POST", {
        file: String(reader.result),
        filename: file.name,
        label: ($("user-document-label") || {}).value || ""
      });
      input.value = "";
      if ($("user-document-label")) $("user-document-label").value = "";
      await loadStaffDocuments(userId);
    } catch (failure) {
      userError(failure.message);
    } finally {
      setBusy(false);
    }
  };
  reader.onerror = () => userError("That file could not be read.");
  reader.readAsDataURL(file);
}


/* =========================================================================
   WIRING
   ========================================================================= */

function wirePremiumFeatures() {
  /* -- the sign-in switch -------------------------------------------- */

  const setAuthAudience = (audience) => {
    state.authAudience = audience;
    const staff = audience === "staff";

    $("auth-as-staff").setAttribute("aria-selected", String(staff));
    $("auth-as-student").setAttribute("aria-selected", String(!staff));

    /* A student never creates their own account - the school issues it - so
       the first-run and recovery affordances belong to staff only. */
    if (!staff) {
      $("auth-title").textContent = "Student sign in";
      $("auth-subtitle").textContent = "Student portal";
      $("auth-intro").textContent =
      "Use the username and password the school gave you. You will see " +
      "your own attendance and nothing else.";
      $("auth-submit").textContent = "Sign in";
      $("auth-name-field").hidden = true;
      $("auth-display-name").required = false;
      $("auth-password-hint").hidden = true;
      $("auth-recover").hidden = true;
    } else {
      showAuthScreen();
    }
    authError("");
  };

  $("auth-as-staff").addEventListener("click", () => setAuthAudience("staff"));
  $("auth-as-student").addEventListener("click",
                                        () => setAuthAudience("student"));

  /* -- the portal ----------------------------------------------------- */

  $("portal-password-save").addEventListener("click", savePortalPassword);
  $("portal-password-out").addEventListener("click", portalSignOut);

  /* -- editing a session ---------------------------------------------- */

  $("session-edit-save").addEventListener("click", saveSessionEdit);
  $("session-edit-close").addEventListener("click",
                                           () => $("session-edit-dialog").close());

  /* -- alerts ---------------------------------------------------------- */

  $("alerts-defaults").addEventListener("click", addDefaultRules);
  $("alerts-dry-run").addEventListener("click", () => runAlerts(true));
  $("alerts-run").addEventListener("click", () => runAlerts(false));

  /* -- student portal accounts ----------------------------------------- */

  $("add-portal-account").addEventListener("click", openPortalAccountDialog);
  $("portal-issue-missing").addEventListener("click",
                                             issueMissingPortalLogins);
  $("portal-account-save").addEventListener("click", savePortalAccount);
  $("portal-account-close").addEventListener("click",
                                             () => $("portal-account-dialog").close());

  /* -- the teacher camera ---------------------------------------------- */

  if ($("capture-start")) {
    $("capture-start").addEventListener("click", startCapture);
    $("capture-take").addEventListener("click", takePhoto);
    $("capture-file").addEventListener("change",
                                       (event) => readPhotoFile(event.target));

    /* Releasing the camera when the dialog closes matters more than any
       button: Escape closes a <dialog> without going through a handler, and
       a webcam left on is a light left on in somebody's office. */
    $("user-dialog").addEventListener("close", () => {
      stopCapture();
      capture.dataUrl = null;
    });
  }

  if ($("user-document-file")) {
    $("user-document-file").addEventListener("change", (event) =>
                                             addStaffDocument(state.editingUser && state.editingUser.user_id,
                                                              event.target));
  }
}


/* =========================================================================
   THE SPATIAL LAYER, WIRED TO REAL DATA
   -------------------------------------------------------------------------
   The bridge between this file and spatial.js.

   Everything here takes a payload app.js ALREADY FETCHED for some other part
   of the page and hands the shape of it to the 3D layer. Not one function
   below makes a request, and not one invents a figure: the ring gets the
   attendance rate out of /api/analytics/today, the core gets its state out of
   /api/live/status, and the formation gets its people out of
   /api/live/presence. If the backend did not say it, it is not drawn - a
   morning with nothing finalised shows "No data" in the ring, which is what
   the KPI module beside it shows for the same reason.

   Every function starts by checking for window.Spatial and returns if it is
   not there. That is what makes spatial.js optional: delete the script tag
   and these become eight no-ops, and the console is the flat tool it was.
   ========================================================================= */

/* The vocabulary the formation uses, mapped from the vocabulary the presence
   timeline uses.

   These are NOT the same words and must not be confused. The server reports
   PRESENT, RECOGNIZED, TEMPORARILY_LOST and SEEN_EARLIER - all of which mean
   "this person has been seen during this session" and none of which is an
   attendance status. The formation draws four visual states, and this is the
   only place the two vocabularies meet. */
const PRESENCE_TO_ORB = {
  PRESENT: "present",
  RECOGNIZED: "present",
  /* Seen, not visible this instant. Drawn as present, because they are in the
     room: a student who turned their head is not late and is certainly not
     absent, and drawing them differently every time the tracker loses them
     for a second would make the formation flicker. */
  TEMPORARILY_LOST: "present",
  SEEN_EARLIER: "late"
};

function orbStateForStudent(student) {
  /* Evidence held for review is its own state, and it outranks presence.

     A student whose evidence is BLOCKED has been seen, but the attendance
     rules will not be allowed to act on what was seen - so drawing them as a
     confident PRESENT would overstate what the system knows. UNCERTAIN is the
     honest word, and it is not a claim of absence either. */
  const eligibility = student.attendance_eligibility || "NOT_CHECKED";
  if (eligibility === "BLOCKED_SPOOF" || eligibility === "BLOCKED_UNCERTAIN") {
    return "uncertain";
  }
  return PRESENCE_TO_ORB[student.presence_state] || "unknown";
}

/* ---------- the overview stage ---------- */

/*
   The ring, the breakdown and the day as a path.

   Called from loadToday(), which is where the payload comes from, and which
   runs whether or not any session has been finalised - the panel this feeds
   is the first thing on the page and must not be empty on the first morning
   of term.
*/
function spatialToday(today) {
  if (!window.Spatial || !today) return;

  const attendance = today.attendance || {};
  const counts = attendance.counts || {};
  const sessions = today.session_counts || {};

  /* A fraction on the wire, a percentage on the ring. `has_data` is false
     until something has actually completed, and the ring says "No data"
     rather than sweeping to zero - a 0% ring on a morning with one lesson
     still running reads as a catastrophe. */
  window.Spatial.ring("overview-ring",
                      attendance.has_data
                        ? attendance.attendance_rate * 100 : null,
                      "Attendance today");

  const breakdown = $("overview-breakdown");
  if (breakdown) {
    if (!attendance.has_data) {
      breakdown.replaceChildren(el("li", {}, [
        el("span", { class: "muted small", text:
          "No session has been finalised today, so there is no breakdown yet." })
      ]));
    } else {
      breakdown.replaceChildren(...[
        ["Present", counts.PRESENT],
        ["Late", counts.LATE],
        ["Partial", counts.PARTIAL],
        ["Absent", counts.ABSENT]
      ].map(([label, value]) => el("li", {}, [
        el("span", { text: label }),
        el("span", { class: "att-count",
                     text: value === null || value === undefined
                       ? "—" : String(value) })
      ])));
    }
  }

  /* The figures around the core. The same numbers the Today panel below
     reports, in the same words. */
  const figures = $("core-figures");
  if (figures) {
    figures.replaceChildren(
      card("Sessions today", sessions.total,
           (sessions.active || 0) + " running"),
      card("Students in scope", (today.students || {}).active),
      card("Present today", attendance.has_data ? counts.PRESENT : null),
      card("Absent today", attendance.has_data ? counts.ABSENT : null)
    );
  }

  /* The day as a lit path. Each node is a real scheduled session: done when
     it has completed, current while it is running, and with the time it was
     scheduled for rendered in the browser's own timezone. */
  const spine = $("overview-spine");
  if (spine) {
    window.Spatial.spine(spine, (today.sessions || []).map((session) => ({
      label: session.class_id +
             (session.section ? " · " + session.section : ""),
      time: timeOfDay(session.start_time) || "",
      done: session.status === "COMPLETED",
      current: session.status === "ACTIVE"
    })));
  }

  /* The formation, when nothing is running.

     An empty pane where a roomful of people should be looks exactly like a
     class nobody turned up to, which is the one reading it must never
     invite. The sentence says which of the two it actually is. The live
     presence poll overwrites this the moment a session starts. */
  const room = $("overview-constellation");
  if (room && !state.liveRunningSession) {
    window.Spatial.constellation(room, [], {
      empty: (sessions.active || 0) > 0
        ? "A session is running. Open Live classroom to watch the room fill."
        : "No session is running, so there is nobody in the room to show. "
          + "The formation fills as students are recognised."
    });
    const count = $("stage-students-count");
    if (count) count.textContent = "";
  }

  /* The core, when nothing is on the live page to say otherwise. A session
     running is reported by renderLiveStatus on the live page's own clock;
     this is the overview's answer when that clock is not running. */
  if (state.view === "overview" && !state.liveRunningSession) {
    const running = Number(sessions.active) || 0;
    window.Spatial.core(running > 0 ? "live" : "idle",
                        running > 0 ? "Live" : "Idle",
                        running > 0
                          ? running + " session" + (running === 1 ? "" : "s") +
                            " running"
                          : "No session running");
  }
}

/* ---------- the live pipeline ---------- */

/*
   The core, the camera frame furniture and the telemetry rail.

   Called from renderLiveStatus(), on the live page's one-second clock, with
   the same status object the strip and the KPI modules are drawn from. Every
   value below is one the pipeline reported.
*/
function spatialLiveStatus(status) {
  if (!window.Spatial || !status) return;

  const running = LIVE_ACTIVE_STATES.indexOf(status.state) >= 0;

  /* The core: alive while the pipeline is, settling for a beat once it has
     stopped, idle after that. The WORD under it is the authority; the
     brightness and the rotation are a second signal on top of it. */
  window.Spatial.core(running ? "live"
                      : status.state === "COMPLETED" ? "settling" : "idle",
                      running ? (status.on_break ? "Break" : "Live")
                              : String(status.state || "Idle"),
                      running
                        ? status.recognized_students +
                          " verified · " + status.active_tracks + " tracked"
                        : "No session running");

  /* The scanline over the frame, and the sweep over the formation. Both are
     data attributes carrying the pipeline state, so neither can be left
     running over a stopped camera. */
  const classroom = $("classroom");
  if (classroom) classroom.dataset.live = running ? "true" : "false";
  window.Spatial.scanning($("overview-room"), running);

  /* The rail around the frame. "—" for anything the pipeline did not send,
     never a zero: "0 FPS" for a camera nobody has heard from is the single
     most misleading thing this rail could print. */
  window.Spatial.cameraRail($("live-cam-rail"), [
    { key: "Live", value: running ? (status.on_break ? "BREAK" : "YES") : "NO" },
    { key: "Camera", value: (status.camera || "").toUpperCase() },
    { key: "FPS", value: running ? status.fps.toFixed(1) : null },
    { key: "Students", value: status.recognized_students },
    { key: "Tracked", value: status.active_tracks },
    { key: "Authenticity", value: status.authenticity_available
        ? String(status.authenticity_mode || "").toUpperCase() : "UNAVAILABLE" }
  ]);
}

/*
   The formation.

   Called from renderLivePresence(), with the presence payload the roster
   table is drawn from. Only CONFIRMED identities appear in presence.students,
   which is why nobody in this formation is a guess - an unverified person is
   counted in the tracks table and never named anywhere.
*/
function spatialPresence(presence) {
  if (!window.Spatial) return;

  const container = $("overview-constellation");
  const count = $("stage-students-count");
  if (!container) return;

  const students = (presence && presence.students) || [];

  window.Spatial.constellation(container, students.map((student) => ({
    id: student.student_id,
    /* Presence never carries a name for anybody unconfirmed, so a missing
       name here means the server withheld it and the formation says so
       rather than filling in an id. */
    name: student.student_name || "Name withheld",
    state: orbStateForStudent(student),
    note: "seen for " + duration(student.duration_seconds)
  })));

  if (count) {
    count.textContent = students.length
      ? students.length + " confirmed in the room"
      : "";
  }
}

/* ---------- camera health, as objects ---------- */

/*
   The camera model and the authenticity sphere.

   AUTHENTICITY IS NOT LIVENESS AND NOT IDENTITY, and this is the one place a
   spatial redesign could quietly merge them. It does not: the sphere reports
   the authenticity states the backend actually has - AUTHENTIC, SUSPICIOUS,
   UNCERTAIN, UNKNOWN - and not the liveness vocabulary of LIVE and SPOOF,
   which belongs to a different question about a different thing. A confirmed
   student can have an unverified source; an authentic source names nobody.
*/
function spatialCameras(body) {
  if (!window.Spatial || !body) return;

  const live = body.live || {};
  const running = live.state === "RUNNING" || live.state === "STARTING";

  const model = $("cam-model");
  if (model) model.dataset.state = running ? "active" : "idle";

  const note = $("cam-model-note");
  if (note) {
    note.textContent = running
      ? "Reporting from the running pipeline"
      : "No pipeline running";
  }

  const status = $("cam-model-status");
  if (status) {
    const level = running ? "ok" : "idle";
    status.replaceChildren(
      systemModule("Camera", live.camera === "connected" ? "ok" : "idle",
                   String(live.camera || "unknown").toUpperCase()),
      systemModule("FPS", level,
                   live.fps === null || live.fps === undefined
                     ? "No data" : Number(live.fps).toFixed(1)),
      systemModule("Processed", level,
                   live.processed === null || live.processed === undefined
                     ? "No data" : String(live.processed)),
      systemModule("Dropped",
                   (live.dropped || 0) > 0 ? "warn" : level,
                   live.dropped === null || live.dropped === undefined
                     ? "No data" : String(live.dropped)),
      systemModule("Detection", level, running ? "ACTIVE" : "IDLE"),
      systemModule("Recognition", level, running ? "ACTIVE" : "IDLE")
    );
  }

  /* With no pipeline running there is no authenticity evidence, and the
     sphere says UNKNOWN rather than implying everything is fine. */
  if (!running) spatialAuthenticity(null);
}

/*
   The sphere's state.

   `tracks` is the live tracks array when there is one. The rule is the one the
   rest of the product uses for evidence: the WORST state present wins, because
   a hall where one source is suspicious is not an authentic hall, and
   averaging evidence would hide the one track somebody needs to look at.
*/
function spatialAuthenticity(tracks) {
  const sphere = $("auth-sphere");
  const word = $("auth-sphere-word");
  const note = $("auth-sphere-note");
  if (!sphere || !word) return;

  const list = tracks || [];
  const states = list.map((track) => track.authenticity_state || "UNKNOWN");

  let verdict = "UNKNOWN";
  let sentence =
    "No session is running, so no authenticity evidence is being produced.";

  if (states.indexOf("SUSPICIOUS") >= 0) {
    verdict = "SUSPICIOUS";
    sentence = "At least one track has attack evidence against its source. " +
               "That blocks attendance for the track; it is not a claim " +
               "about anybody being absent.";
  } else if (states.indexOf("UNCERTAIN") >= 0) {
    verdict = "UNCERTAIN";
    sentence = "The source of at least one track is undecided. Undecided is " +
               "not a verdict, and it is not absence.";
  } else if (states.indexOf("AUTHENTIC") >= 0) {
    verdict = "AUTHENTIC";
    sentence = "Every track with a decided source is authentic. An authentic " +
               "source names nobody - identity is a separate fact.";
  } else if (list.length) {
    sentence = "Tracks are being followed, but none has a decided source yet.";
  }

  word.textContent = verdict;
  /* The colour is the third signal, after the word and the sentence. */
  sphere.dataset.state = verdict.toLowerCase();
  sphere.setAttribute("aria-label", "Authenticity state: " + verdict);
  if (note) note.textContent = sentence;
}

/* ---------- the register, as a formation ---------- */

/*
   Which of these three the camera is in, for one student.

   Inactive first: somebody who has left is not expected at anything, and
   whether their samples are still on file does not change that.
*/
function rosterState(student) {
  if (student.status !== "ACTIVE") return "inactive";
  return student.enrolled ? "enrolled" : "pending";
}

/*
   The students page's constellation.

   Drawn from the payload loadStudents() already fetched - the same page, the
   same filters, the same order as the table underneath. No second request, so
   the formation and the table cannot disagree about who is in this class.

   THE STATES ARE ABOUT FACE SAMPLES AND NOTHING ELSE. `enrolled` means the
   store holds samples for this person, which is what makes recognition
   possible; it is not a claim that they are present, were present, or are
   expected anywhere. The register has no presence outside a session, and this
   formation never pretends otherwise - which is why the note under it says so
   in words rather than leaving a green bead to be misread.
*/
function spatialRoster(data) {
  if (!window.Spatial) return;

  const pane = $("roster-pane");
  const container = $("roster-constellation");
  if (!pane || !container) return;

  const items = (data && data.items) || [];
  pane.hidden = items.length === 0;
  if (!items.length) return;

  const counts = { enrolled: 0, pending: 0, inactive: 0 };

  window.Spatial.constellation(container, items.map((student) => {
    const state = rosterState(student);
    counts[state] += 1;
    return {
      id: student.student_id,
      name: student.name,
      state: state,
      note: student.class_name
        ? student.class_name + (student.section ? " " + student.section : "")
        : ""
    };
  }), {
    /* Selecting a person opens the profile the table rows open. One
       destination, one code path. */
    onPick: openStudent,
    empty: "No student matches this view."
  });

  $("roster-count").textContent = items.length + " of " + data.total +
    (data.total === 1 ? " student" : " students");

  $("roster-note").textContent =
    counts.enrolled + " enrolled, " + counts.pending + " with no face " +
    "samples yet, " + counts.inactive + " inactive. This is the register, " +
    "not attendance: nobody here is present or absent, and a student can " +
    "only be recognised once their samples are stored. Select a person to " +
    "open their profile.";
}

/* ---------- the analytics cube ---------- */

/*
   The status distribution, standing in a glass box.

   The same rows the records table below it is built from, so there is no
   second query and no chance of the two disagreeing. Hidden entirely when the
   report found nothing, because an empty cube is a box with a floor in it.
*/
function spatialReportCube(report) {
  if (!window.Spatial) return;

  const panel = $("report-cube-panel");
  const cube = $("report-cube");
  if (!panel || !cube) return;

  const counts = (report && report.counts) || null;
  if (!counts) { panel.hidden = true; return; }

  const rows = ["PRESENT", "LATE", "PARTIAL", "LEFT_EARLY", "ABSENT",
                "UNKNOWN", "MANUAL_OVERRIDE"]
    .map((status) => ({ label: status.replace(/_/g, " "),
                        value: counts[status] || 0 }))
    .filter((row) => row.value > 0);

  panel.hidden = rows.length === 0;
  if (!rows.length) return;

  window.Spatial.cube(cube, rows, {
    label: "Attendance status distribution for this report",
    empty: "This report matched no records."
  });

  const note = $("report-cube-note");
  if (note) {
    const total = rows.reduce((sum, row) => sum + row.value, 0);
    note.textContent = total + " record" + (total === 1 ? "" : "s");
  }
}
