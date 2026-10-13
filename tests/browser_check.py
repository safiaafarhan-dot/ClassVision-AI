"""
The browser check: the interface, rendered.

THIS IS NOT PART OF `tests/run_all.py`, DELIBERATELY. It needs Google Chrome
installed, and the automated suite must keep running on a machine that has no
browser, no webcam and no network - the same reason `--camera` is opt-in in
preflight. Run it by hand, read the numbers, look at the screenshots.

    python tests/browser_check.py
    python tests/browser_check.py --into C:/tmp/shots --keep-open

WHY IT EXISTS

    Every UI suite in this project asserts against `backend/static/*` as TEXT.
    They are honest about it - "NO BROWSER IS NEEDED", "the DOM is never
    built" - and they catch a great deal: a renderer that slices a list it was
    told not to slice, a figure painted on a blurred selector, an endpoint a
    view must not call. What they cannot catch is anything that only exists
    once a layout engine has run: a grid that wraps at ten panels, a glass
    panel whose real computed contrast is lower than the token it was
    calculated from, a page that scrolls sideways on a tablet, a script error
    that leaves a view blank.

    So this drives the real application in real Chrome over the DevTools
    protocol: it signs in by typing into the form, clicks the navigation,
    reads back computed styles and measured geometry, and writes a screenshot
    of each view.

HOW IT IS WIRED, AND WHAT THAT COSTS

    The server runs IN THIS PROCESS, on a thread, against a TEMPORARY
    database seeded here - never `data/classvision.db`. That is what lets the
    ten-classroom grid be rendered at all: ten synthetic runtimes are pushed
    into the live registry directly, exactly as `test_multi_session_console.py`
    does, so ten panels can be drawn without ten physical cameras.

    The consequence is the honest limit of the whole exercise: no camera is
    opened, so every panel's video frame is absent by construction, and
    nothing here is evidence that ten simultaneous devices work. What is being
    checked is the interface, not the hardware.

    Chrome runs headless with its own throwaway profile. The WebSocket client
    below is about a hundred lines written out by hand, because the project
    has no browser-automation dependency and this tool was not worth adding
    one for.

WHAT IT CHECKS, each reported with the figure it measured

    1. The sign-in page builds, and the form is really there.
    2. Signing in by typing and clicking reaches the dashboard.
    3. Nothing scrolls sideways - at 1600x1000, at 1024x768 and at 820x1180.
    4. The theme switch repaints: light and dark produce different, opaque
       backgrounds, and `auto` follows the emulated system preference.
    5. Figures are legible where they are actually painted: the contrast of
       rendered text against its rendered background, computed from the
       colours Chrome resolved, not from the tokens the CSS asked for.
    6. Reduced motion is obeyed by the running page, not only by the
       stylesheet: with the preference emulated, no element reports a running
       animation.
    7. Ten classrooms draw ten panels, in the column count `gridColumns`
       promises, each panel naming its own students and saying 6 of 10.
    8. Assembly mode draws a formation from the session's expected students.
    9. Not one request left the machine.
    10. No script error, in any view.

Exit code is non-zero if any check fails. Every check that could not run says
so and is not counted as a pass.
"""

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from paths import enable_safe_console  # noqa: E402

USERNAME = "browsercheck"
PASSWORD = "Brows3r-Check-9!"

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expanduser(
        r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


# -- a WebSocket client, because the project has no browser dependency ------

class Wire:
    """
    The smallest client that can talk to Chrome: RFC 6455 text frames.

    Only what the DevTools protocol needs - a client handshake, masked text
    frames out, fragmented frames in (a screenshot arrives as one very large
    one), and a pong for every ping so a long check is not disconnected.
    """

    def __init__(self, url, timeout=30):
        parsed = urlparse(url)
        port = parsed.port or 80
        self.socket = socket.create_connection((parsed.hostname, port),
                                               timeout=timeout)
        self.socket.settimeout(timeout)
        self.buffer = b""

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        handshake = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, parsed.hostname, port, key))
        self.socket.sendall(handshake.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise ConnectionError("Chrome closed the connection during "
                                      "the WebSocket handshake.")
            header += chunk

        head, _, rest = header.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("Chrome refused the upgrade: %s"
                                  % head.split(b"\r\n")[0])
        self.buffer = rest

    def _exactly(self, count):
        while len(self.buffer) < count:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("Chrome closed the connection.")
            self.buffer += chunk
        taken, self.buffer = self.buffer[:count], self.buffer[count:]
        return taken

    def _frame(self):
        first, second = self._exactly(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exactly(8))[0]

        # The server never masks; if it did, this would be the place.
        payload = self._exactly(length) if length else b""
        return final, opcode, payload

    def send(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4]
                       for index, byte in enumerate(payload))

        header = bytearray([0x81])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", size)

        self.socket.sendall(bytes(header) + mask + masked)

    def receive(self):
        """One complete text message, reassembled, pings answered."""
        pieces = []
        while True:
            final, opcode, payload = self._frame()

            if opcode == 0x9:                             # ping
                self.socket.sendall(b"\x8a" + bytes([0x80 | len(payload)])
                                    + os.urandom(4) + payload
                                    if payload else b"\x8a\x80"
                                    + os.urandom(4))
                continue
            if opcode == 0xA:                             # pong
                continue
            if opcode == 0x8:                             # close
                raise ConnectionError("Chrome closed the WebSocket.")

            pieces.append(payload)
            if final:
                return b"".join(pieces).decode("utf-8", "replace")

    def close(self):
        try:
            self.socket.close()
        except Exception:                                 # noqa: BLE001
            pass


class Browser:
    """A page in headless Chrome, driven over the DevTools protocol."""

    def __init__(self, executable, port, width, height, profile):
        self.port = port
        self.width = width
        self.height = height
        self.console_errors = []
        self.page_errors = []
        self.requests = []
        self.failed_responses = []
        self.aborted = []
        self._urls = {}
        self._next_id = 0
        self._events = []

        self.process = subprocess.Popen(
            [executable,
             "--headless=new",
             "--remote-debugging-port=%d" % port,
             "--user-data-dir=%s" % profile,
             "--window-size=%d,%d" % (width, height),
             "--hide-scrollbars",
             "--disable-gpu",
             "--no-first-run",
             "--no-default-browser-check",
             "--disable-extensions",
             "--disable-background-networking",
             "--disable-component-update",
             "--disable-sync",
             "--metrics-recording-only",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        target = self._await_target()
        self.wire = Wire(target)

        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Log.enable")
        self.call("Network.enable")

    def _await_target(self, timeout=25):
        """The page target's WebSocket url, once Chrome is listening."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port,
                        timeout=2) as answer:
                    targets = json.loads(answer.read().decode("utf-8"))
                for target in targets:
                    if target.get("type") == "page" and \
                            target.get("webSocketDebuggerUrl"):
                        return target["webSocketDebuggerUrl"]
            except (urllib.error.URLError, OSError, ValueError) as error:
                last = error
            time.sleep(0.25)

        raise RuntimeError("Chrome never opened a debugging port on %d (%s)"
                           % (self.port, last))

    # -- protocol ----------------------------------------------------------

    def call(self, method, params=None, timeout=40):
        self._next_id += 1
        identifier = self._next_id
        self.wire.send(json.dumps({"id": identifier, "method": method,
                                   "params": params or {}}))

        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.wire.receive())
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError("%s failed: %s"
                                       % (method, message["error"]))
                return message.get("result", {})
            self._record(message)

        raise TimeoutError("%s did not answer in %gs" % (method, timeout))

    def _record(self, message):
        method = message.get("method")
        params = message.get("params", {})
        self._events.append(message)

        if method == "Runtime.consoleAPICalled" and \
                params.get("type") in ("error", "assert"):
            self.console_errors.append(self._console_text(params))
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            self.page_errors.append(
                detail.get("text", "") + " " +
                str(detail.get("exception", {}).get("description", ""))[:400])
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") == "error":
                self.console_errors.append(
                    "%s %s" % (entry.get("source"), entry.get("text")))
        elif method == "Network.requestWillBeSent":
            url = params.get("request", {}).get("url", "")
            self.requests.append(url)
            self._urls[params.get("requestId")] = url
        elif method == "Network.responseReceived":
            answer = params.get("response", {})
            if (answer.get("status") or 0) >= 400:
                self.failed_responses.append(
                    "%s %s" % (answer.get("status"), answer.get("url", "")))
        elif method == "Network.loadingFailed":
            reason = params.get("errorText", "")
            url = self._urls.get(params.get("requestId"), "unknown url")
            # A cancelled request is the client's doing, not the server's:
            # leaving the live view closes the frame stream mid-flight, and
            # that is the view behaving correctly.
            if params.get("canceled") or reason == "net::ERR_ABORTED":
                self.aborted.append(url)
            else:
                self.failed_responses.append("%s %s" % (reason, url))

    @staticmethod
    def _console_text(params):
        parts = []
        for argument in params.get("args", []):
            parts.append(str(argument.get("value",
                                          argument.get("description", ""))))
        return " ".join(parts)[:400]

    def drain(self, seconds):
        """Let the page run, keeping events, without issuing a command."""
        deadline = time.time() + seconds
        original = self.wire.socket.gettimeout()
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            self.wire.socket.settimeout(max(0.05, min(left, 1.0)))
            try:
                self._record(json.loads(self.wire.receive()))
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
        self.wire.socket.settimeout(original)

    # -- driving -----------------------------------------------------------

    def navigate(self, url, settle=1.5):
        self.call("Page.navigate", {"url": url})
        self.drain(settle)

    def evaluate(self, expression, timeout=40):
        result = self.call("Runtime.evaluate", {
            "expression": "(function(){%s})()" % expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)

        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise RuntimeError(
                "the page raised: %s"
                % (detail.get("exception", {}).get("description")
                   or detail.get("text")))
        return result.get("result", {}).get("value")

    def wait_until(self, expression, timeout=15, interval=0.25):
        """Poll a JavaScript condition, returning what it finally produced."""
        deadline = time.time() + timeout
        value = None
        while time.time() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            self.drain(interval)
        return value

    def click(self, selector, require_reachable=True):
        """
        A real mouse press on a control, refused if something covers it.

        The hit test is the part worth having: a click dispatched at the
        coordinates of a button that an overlay is sitting on top of goes to
        the overlay, the page does something else entirely, and the check
        that follows reads a value it cannot explain. This says so instead.
        """
        box = self.evaluate(
            "const node = document.querySelector(%s);"
            "if (!node) return null;"
            "node.scrollIntoView({block: 'center'});"
            "const box = node.getBoundingClientRect();"
            "const x = box.left + box.width / 2;"
            "const y = box.top + box.height / 2;"
            "const top = document.elementFromPoint(x, y);"
            "return {x: x, y: y, w: box.width, h: box.height,"
            " hit: top ? (top.id || top.tagName.toLowerCase() + '.' +"
            "   (top.className || '').toString().split(' ')[0]) : null,"
            " reaches: !!(top && (top === node || node.contains(top)"
            "   || top.contains(node)))};" % json.dumps(selector))

        if not box or not box["w"] or not box["h"]:
            raise RuntimeError("%s is not on the page to be clicked"
                               % selector)

        if require_reachable and not box["reaches"]:
            raise RuntimeError(
                "%s is on the page but a click at its centre would land on "
                "%s instead" % (selector, box["hit"]))

        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": kind, "x": box["x"], "y": box["y"],
                "button": "left", "clickCount": 1, "buttons": 1,
            })
        self.drain(0.35)

    def type_into(self, selector, text):
        self.click(selector)
        self.call("Input.insertText", {"text": text})
        self.drain(0.1)

    def emulate(self, features):
        self.call("Emulation.setEmulatedMedia", {"features": features})
        self.drain(0.3)

    def resize(self, width, height):
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height,
            "deviceScaleFactor": 1, "mobile": False})
        self.drain(0.6)
        self.width, self.height = width, height

    def screenshot(self, path):
        answer = self.call("Page.captureScreenshot",
                           {"format": "png", "captureBeyondViewport": False},
                           timeout=60)
        Path(path).write_bytes(base64.b64decode(answer["data"]))
        return Path(path)

    def close(self):
        try:
            self.wire.close()
        finally:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


# -- the application, on a thread, against a temporary database ------------

def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def seed(path):
    """
    A small school: an administrator who can sign in, students, and a
    finished lesson so the dashboard has figures to draw.
    """
    from backend.services import state

    state.configure(path)
    database = state.get_database()

    from attendance_session import AttendanceSession
    from backend.services.auth_service import get_auth_service
    from repositories import (
        AttendanceRepository,
        SessionRepository,
        StudentRepository,
    )

    students = StudentRepository(database)
    names = ["Aarav Sharma", "Diya Patel", "Ishaan Gupta", "Meera Nair",
             "Rohan Das", "Saanvi Rao", "Kabir Singh", "Anaya Bose",
             "Vivaan Joshi", "Aisha Khan", "Arjun Menon", "Zara Sheikh"]

    for index, name in enumerate(names):
        students.upsert("BC%03d" % (index + 1), name, status="ACTIVE",
                        roll_number="%02d" % (index + 1),
                        class_name="CSE-A" if index < 6 else "CSE-B",
                        section="A" if index < 6 else "B")

    sessions = SessionRepository(database)
    attendance = AttendanceRepository(database)
    now = datetime.now(timezone.utc)

    for day in range(1, 6):
        start = now - timedelta(days=day, hours=2)
        session = AttendanceSession("BC-S%d" % day, "CSE-A",
                                    start, start + timedelta(hours=1),
                                    policy_version="1.0")
        session.start()
        session.complete()
        sessions.save(session)

        for index in range(6):
            status = "PRESENT" if (index + day) % 5 else "ABSENT"
            database.execute(
                "INSERT OR REPLACE INTO attendance_records (session_id, "
                "student_id, status, reason, policy_version, created_at, "
                "updated_at) VALUES (?, ?, ?, 'seeded for the browser "
                "check', '1.0', ?, ?)",
                ("BC-S%d" % day, "BC%03d" % (index + 1), status,
                 start.isoformat(), start.isoformat()))

    # A lesson happening now, for the assembly and live views.
    live = AttendanceSession("BC-NOW", "CSE-A",
                             now - timedelta(minutes=10),
                             now + timedelta(minutes=50),
                             policy_version="1.0")
    sessions.save(live)
    for index in range(6):
        sessions.add_expected_student("BC-NOW", "BC%03d" % (index + 1))

    service = get_auth_service(database)
    service.bootstrap_super_admin(USERNAME, "Browser Check", PASSWORD)

    counts = {
        "students": len(names),
        "sessions": 6,
        "records": database.query_one(
            "SELECT COUNT(*) AS n FROM attendance_records")["n"],
    }
    return database, counts


def classrooms(count):
    """
    `count` synthetic live sessions in the registry, with named rosters.

    No camera is opened: the runtimes are begun directly and a presence
    snapshot is published, which is what the panels read. This is the same
    device `test_multi_session_console.py` uses, and it is the only way to
    draw a ten-classroom grid on a laptop with one webcam.
    """
    from backend.services.live_service import get_live_registry

    registry = get_live_registry()
    roster = ["Aarav Sharma", "Diya Patel", "Ishaan Gupta", "Meera Nair",
              "Rohan Das", "Saanvi Rao", "Kabir Singh", "Anaya Bose",
              "Vivaan Joshi", "Aisha Khan"]

    for index in range(count):
        session_id = "BC-L%02d" % index
        manager = registry.manager_for(session_id)
        manager.runtime.begin(session_id,
                              class_id="CLASS-%02d" % (index + 1),
                              camera_index=index)
        manager.runtime.publish_presence([
            {
                "student_id": "BC%03d" % (position + 1),
                "student_name": name,
                "identity_state": "CONFIRMED",
                "presence_state": "RECOGNIZED",
                "liveness_state": "LIVE",
                "liveness_confirmed": True,
                "authenticity_state": "AUTHENTIC",
                "authenticity_confirmed": True,
                "attendance_eligibility": "COUNTED",
                "first_seen": None,
                "last_seen": None,
                "duration_seconds": 61.0 + position,
                "segment_count": 1,
            }
            for position, name in enumerate(roster)
        ])

    return registry


def serve(port):
    """uvicorn on a thread. Returns the server so it can be asked to stop."""
    import uvicorn

    from backend.app import app

    configuration = uvicorn.Config(app, host="127.0.0.1", port=port,
                                   log_level="error", access_log=False)
    server = uvicorn.Server(configuration)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline:
        if server.started:
            return server, thread
        time.sleep(0.1)

    raise RuntimeError("the server did not start on port %d" % port)


# -- the checks -------------------------------------------------------------

class Report:
    """What was checked, and what it measured. A skip is never a pass."""

    def __init__(self):
        self.rows = []

    def ok(self, name, detail):
        self.rows.append(("OK", name, detail))
        print("  OK       %-34s %s" % (name, detail))

    def failed(self, name, detail):
        self.rows.append(("FAILED", name, detail))
        print("  FAILED   %-34s %s" % (name, detail))

    def skipped(self, name, detail):
        self.rows.append(("SKIPPED", name, detail))
        print("  SKIPPED  %-34s %s" % (name, detail))

    def count(self, status):
        return sum(1 for row in self.rows if row[0] == status)


CONTRAST_SCRIPT = """
const ratio = (a, b) => {
  const channel = (c) => {
    c = c / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const lum = (rgb) => 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1])
                     + 0.0722 * channel(rgb[2]);
  const first = lum(a), second = lum(b);
  const light = Math.max(first, second), dark = Math.min(first, second);
  return (light + 0.05) / (dark + 0.05);
};
const parse = (value) => {
  const found = (value || "").match(/[\\d.]+/g);
  if (!found) return null;
  const parts = found.map(Number);
  if (parts.length >= 4 && parts[3] === 0) return null;      // transparent
  return parts.slice(0, 3);
};
const behind = (node) => {
  // The colour actually painted behind this text: walk up until something
  // opaque is found, which is what a reader's eye does.
  let current = node;
  while (current && current !== document.documentElement) {
    const found = parse(getComputedStyle(current).backgroundColor);
    if (found) return found;
    current = current.parentElement;
  }
  return parse(getComputedStyle(document.body).backgroundColor) || [0, 0, 0];
};
const measured = [];
document.querySelectorAll(SELECTOR).forEach((node) => {
  const box = node.getBoundingClientRect();
  if (!box.width || !box.height) return;
  const text = (node.textContent || "").trim();
  if (!text) return;
  const style = getComputedStyle(node);
  if (style.visibility === "hidden" || style.opacity === "0") return;
  const colour = parse(style.color);
  if (!colour) return;
  measured.push({
    text: text.slice(0, 28),
    ratio: Math.round(ratio(colour, behind(node)) * 100) / 100,
    size: parseFloat(style.fontSize),
  });
});
measured.sort((a, b) => a.ratio - b.ratio);
return measured.slice(0, 6);
"""


def check_sign_in(browser, base, shots, report):
    browser.navigate(base + "/", settle=2.5)

    built = browser.evaluate(
        "return {nodes: document.querySelectorAll('*').length,"
        " form: !!document.querySelector('#auth-form'),"
        " username: !!document.querySelector('#auth-username'),"
        " submit: !!document.querySelector('#auth-submit'),"
        " title: document.title,"
        " visible: !!(document.querySelector('#auth-form') &&"
        "   document.querySelector('#auth-form').getClientRects().length)};")

    browser.screenshot(shots / "01-sign-in.png")

    if not built or not built["form"] or not built["visible"]:
        report.failed("sign-in page renders",
                      "the form is not in the rendered page: %s" % built)
        return False

    report.ok("sign-in page renders",
              "%d DOM nodes, the form painted, title %r"
              % (built["nodes"], built["title"]))
    return True


def check_sign_in_works(browser, base, shots, report):
    browser.type_into("#auth-username", USERNAME)
    browser.type_into("#auth-password", PASSWORD)
    browser.click("#auth-submit")
    browser.drain(2.0)

    signed = browser.wait_until(
        "const gate = document.querySelector('#auth-form');"
        "const hidden = !gate || !gate.getClientRects().length;"
        "const nav = document.querySelectorAll('.nav-item').length;"
        "return (hidden && nav) ? {nav: nav,"
        "  views: document.querySelectorAll('.view').length} : null;",
        timeout=20)

    browser.screenshot(shots / "02-entry-screen.png")

    if not signed:
        report.failed("signing in reaches the dashboard",
                      "the sign-in form is still on screen")
        return False

    report.ok("signing in reaches the dashboard",
              "%d navigation items, %d views mounted"
              % (signed["nav"], signed["views"]))
    return True


def check_entry_screen(browser, shots, report):
    """
    The front door, and the fact that it really is a door.

    After signing in, `spatial.js` puts a full-viewport scene in front of the
    console - deliberately, once per page load, crossable by Enter, by Escape,
    by its own button or by a click on the scene. Rendered, that makes it an
    overlay at z-index 60 with `pointer-events: auto` over every control in
    the rail, which is exactly the kind of thing no source-reading test can
    see: the first version of this check mistook it for a broken theme switch,
    because its click on "Light" was swallowed by the door and crossed it
    instead.

    So two things are measured: that the door really is covering the console
    while it is open, and that nothing covers the rail once it is crossed.
    """
    standing = browser.evaluate(
        "const screen = document.querySelector('#entry-screen');"
        "if (!screen || screen.hidden) return null;"
        "const style = getComputedStyle(screen);"
        "const box = screen.getBoundingClientRect();"
        "const light = document.querySelector('#theme-light');"
        "const rect = light ? light.getBoundingClientRect() : null;"
        "const over = rect ? document.elementFromPoint("
        "  rect.left + rect.width / 2, rect.top + rect.height / 2) : null;"
        "return {width: Math.round(box.width),"
        " height: Math.round(box.height),"
        " layer: style.zIndex, pointers: style.pointerEvents,"
        " body: document.body.className,"
        " covers: over ? (over.id || over.tagName.toLowerCase()) : null};")

    if not standing:
        report.skipped("the entry screen is a door",
                       "no entry screen was up after signing in")
        return True

    # The door, then the console behind it, in that order.

    # Cross it the way the keyboard does, which is the path that has to work
    # for somebody with a class waiting.
    for kind in ("keyDown", "keyUp"):
        browser.call("Input.dispatchKeyEvent", {
            "type": kind, "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    browser.drain(1.6)

    crossed = browser.evaluate(
        "const screen = document.querySelector('#entry-screen');"
        "const light = document.querySelector('#theme-light');"
        "const rect = light ? light.getBoundingClientRect() : null;"
        "const over = rect ? document.elementFromPoint("
        "  rect.left + rect.width / 2, rect.top + rect.height / 2) : null;"
        "return {gone: !screen || screen.hidden,"
        " body: document.body.className,"
        " reaches: !!(over && light && (over === light"
        "   || light.contains(over))),"
        " covered: over ? (over.id || over.tagName.toLowerCase()) : null};")

    if not crossed["gone"]:
        report.failed("the entry screen is a door",
                      "Enter did not cross it; the console is still behind a "
                      "%dx%d pane" % (standing["width"], standing["height"]))
        return False

    if not crossed["reaches"]:
        report.failed("the entry screen is a door",
                      "it was crossed, but a click on the theme switch still "
                      "lands on %s" % crossed["covered"])
        return False

    browser.screenshot(shots / "03-dashboard.png")

    report.ok("the entry screen is a door",
              "%dx%d at z-index %s covered the rail, Enter crossed it, and "
              "the rail answers the mouse again"
              % (standing["width"], standing["height"], standing["layer"]))
    return True


def check_status_row(browser, base, report):
    """
    The status row says what the API said, in the API's own words.

    `/api/health` answers `ready`, `missing` or `unknown` - a state. The
    Schema tile printed "v" + that word and rendered **vready** on every
    dashboard this project has ever shown, which no suite could see, because
    the string is assembled from a value the source does not contain. So the
    rendered word is compared against the answer the page was given.
    """
    tiles = browser.evaluate(
        "const out = {};"
        "document.querySelectorAll('.status-module').forEach((node) => {"
        "  const name = node.querySelector('.status-name');"
        "  const state = node.querySelector('.status-state');"
        "  if (name && state) out[name.textContent.trim()] ="
        "    state.textContent.trim();"
        "});"
        "return out;")

    answered = browser.evaluate(
        "return fetch('/api/health', {credentials: 'same-origin'})"
        "  .then((answer) => answer.json());")

    if not tiles or "Schema" not in tiles:
        report.skipped("the status row repeats the API",
                       "no status row was rendered")
        return True

    rendered = tiles["Schema"]
    expected = str((answered or {}).get("schema", "")).upper()

    if not expected:
        report.skipped("the status row repeats the API",
                       "/api/health returned no schema state")
        return True

    if rendered != expected:
        report.failed(
            "the status row repeats the API",
            "/api/health says %r and the tile shows %r - a value the page "
            "assembled rather than reported" % (expected, rendered))
        return False

    report.ok("the status row repeats the API",
              "%d tiles, Schema reads %r as the API answered it"
              % (len(tiles), rendered))
    return True


def check_no_sideways_scroll(browser, shots, report):
    sizes = ((1600, 1000), (1024, 768), (820, 1180))
    worst = []

    for width, height in sizes:
        browser.resize(width, height)
        browser.drain(0.6)
        measured = browser.evaluate(
            "const doc = document.documentElement;"
            "let widest = null, over = 0;"
            "document.querySelectorAll('body *').forEach((node) => {"
            "  const box = node.getBoundingClientRect();"
            "  if (!box.width || !box.height) return;"
            "  const spill = Math.round(box.right - window.innerWidth);"
            "  if (spill > over) {"
            "    over = spill;"
            "    widest = node.tagName.toLowerCase() + (node.id ? '#' +"
            "      node.id : '.' + (node.className || '').toString()"
            "      .split(' ')[0]);"
            "  }"
            "});"
            "return {scroll: doc.scrollWidth, client: doc.clientWidth,"
            " over: over, node: widest};")
        worst.append((width, height, measured))

    browser.resize(*sizes[0])
    browser.drain(0.4)

    spilling = [item for item in worst
                if item[2]["scroll"] > item[2]["client"] + 1]

    if spilling:
        width, height, measured = spilling[0]
        report.failed(
            "nothing scrolls sideways",
            "%dx%d scrolls to %dpx in a %dpx page; worst element %s by %dpx"
            % (width, height, measured["scroll"], measured["client"],
               measured["node"], measured["over"]))
        return False

    report.ok("nothing scrolls sideways",
              "clean at " + ", ".join("%dx%d" % (w, h)
                                      for w, h, _ in worst))
    return True


def check_theme(browser, shots, report):
    readings = {}

    for name, selector in (("light", "#theme-light"), ("dark", "#theme-dark")):
        browser.click(selector)
        browser.drain(0.7)
        readings[name] = browser.evaluate(
            "const style = getComputedStyle(document.body);"
            "return {background: style.backgroundColor,"
            " colour: style.color,"
            " attribute: document.documentElement.getAttribute('data-theme'),"
            " checked: document.querySelector('%s')"
            "   .getAttribute('aria-checked')};" % selector)
        browser.screenshot(shots / ("04-theme-%s.png" % name))

    # `auto` must follow the system, so the system is changed underneath it.
    browser.click("#theme-auto")
    browser.emulate([{"name": "prefers-color-scheme", "value": "dark"}])
    following_dark = browser.evaluate(
        "return getComputedStyle(document.body).backgroundColor;")
    browser.emulate([{"name": "prefers-color-scheme", "value": "light"}])
    following_light = browser.evaluate(
        "return getComputedStyle(document.body).backgroundColor;")
    browser.emulate([])

    transparent = [name for name, reading in readings.items()
                   if "rgba(0, 0, 0, 0)" in (reading["background"] or "")]

    if transparent:
        report.failed("the theme switch repaints",
                      "the %s body has no background of its own"
                      % transparent[0])
        return False

    if readings["light"]["background"] == readings["dark"]["background"]:
        report.failed("the theme switch repaints",
                      "light and dark both paint %s"
                      % readings["light"]["background"])
        return False

    if following_dark == following_light:
        report.failed("auto follows the system",
                      "auto painted %s under both system preferences"
                      % following_dark)
        return False

    report.ok("the theme switch repaints",
              "light %s, dark %s, and auto follows the system (%s / %s)"
              % (readings["light"]["background"], readings["dark"]
                 ["background"], following_light, following_dark))

    # Leave it on the theme the screenshots that follow should use.
    browser.click("#theme-dark")
    browser.drain(0.5)
    return True


def check_contrast(browser, report, selector, where):
    script = CONTRAST_SCRIPT.replace("SELECTOR", json.dumps(selector))
    measured = browser.evaluate(script)

    if not measured:
        report.skipped("rendered contrast: %s" % where,
                       "no painted text matched %s" % selector)
        return None

    lowest = measured[0]
    # 4.5:1 is the WCAG AA floor for body text; large text may sit at 3:1,
    # and the figures this is aimed at are large.
    floor = 3.0 if lowest["size"] >= 24 else 4.5

    if lowest["ratio"] < floor:
        report.failed(
            "rendered contrast: %s" % where,
            "%r reads at %.2f:1 against what is painted behind it (%.0fpx "
            "needs %.1f:1)" % (lowest["text"], lowest["ratio"],
                               lowest["size"], floor))
        return False

    report.ok("rendered contrast: %s" % where,
              "lowest of %d measured is %.2f:1 (%r, %.0fpx)"
              % (len(measured), lowest["ratio"], lowest["text"],
                 lowest["size"]))
    return True


def check_reduced_motion(browser, report):
    browser.emulate([{"name": "prefers-reduced-motion", "value": "reduce"}])
    browser.drain(1.0)

    running = browser.evaluate(
        "const moving = [];"
        "document.querySelectorAll('body *').forEach((node) => {"
        "  if (!node.getClientRects().length) return;"
        "  const style = getComputedStyle(node);"
        "  const named = style.animationName;"
        "  if (named && named !== 'none' &&"
        "      parseFloat(style.animationDuration) > 0.01) {"
        "    moving.push(node.tagName.toLowerCase() + ' ' + named);"
        "  }"
        "});"
        "return moving.slice(0, 5);")

    animations = browser.evaluate(
        "return document.getAnimations ?"
        " document.getAnimations().filter((a) => a.playState === 'running')"
        "   .length : -1;")

    browser.emulate([])
    browser.drain(0.4)

    if running:
        report.failed("reduced motion is obeyed",
                      "still animating: %s" % ", ".join(running))
        return False

    report.ok("reduced motion is obeyed",
              "no element reports a running animation (%s web animations)"
              % ("getAnimations unavailable" if animations == -1
                 else animations))
    return True


def check_ten_classrooms(browser, base, shots, report, expected=10):
    browser.resize(1600, 1000)
    browser.click(".nav-item[data-view='live']")
    browser.drain(2.0)

    grid = browser.wait_until(
        "const grid = document.querySelector('#live-grid');"
        "if (!grid) return null;"
        "const panels = grid.querySelectorAll('.live-panel');"
        "if (panels.length < %d) return null;"
        "const style = getComputedStyle(grid);"
        "const columns = style.gridTemplateColumns.split(' ')"
        "  .filter((part) => part.trim()).length;"
        "const names = [];"
        "panels.forEach((panel) => {"
        "  const name = panel.querySelector('.live-panel-name');"
        "  const count = panel.querySelector('.live-panel-roster-count');"
        "  const faces = panel.querySelectorAll('.live-panel-face');"
        "  const extra = panel.querySelector("
        "    '.live-panel-roster-list li.muted');"
        "  names.push({class: name ? name.textContent.trim() : null,"
        "    count: count ? count.textContent.trim() : null,"
        "    faces: faces.length,"
        "    note: extra ? extra.textContent.trim() : null});"
        "});"
        "return {panels: panels.length, columns: columns,"
        " variable: style.getPropertyValue('--live-columns').trim(),"
        " rows: names, left: grid.getBoundingClientRect().left,"
        " right: Math.round(grid.getBoundingClientRect().right)};"
        % expected, timeout=25)

    browser.screenshot(shots / "05-ten-classrooms.png")

    if not grid:
        report.failed("ten classrooms draw ten panels",
                      "the grid never reached %d panels" % expected)
        return False

    classes = {row["class"] for row in grid["rows"] if row["class"]}
    capped = [row["faces"] for row in grid["rows"]]
    counts = {row["count"] for row in grid["rows"] if row["count"]}
    notes = [row["note"] for row in grid["rows"] if row["note"]]

    if grid["panels"] != expected:
        report.failed("ten classrooms draw ten panels",
                      "%d panels for %d classrooms"
                      % (grid["panels"], expected))
        return False

    if len(classes) != expected:
        report.failed("every panel names its own classroom",
                      "%d distinct names across %d panels"
                      % (len(classes), expected))
        return False

    if max(capped) > 6:
        report.failed("the roster cap holds in the DOM",
                      "a panel rendered %d names, and the cap is 6"
                      % max(capped))
        return False

    # `gridColumns` sets a MAXIMUM and `auto-fit` picks the real number from
    # the width - which is what the function's own comment says it is for, and
    # which only a laid-out page can confirm. A rendered count ABOVE the
    # maximum would mean the variable is not reaching the grid at all.
    asked = int(grid["variable"] or 0)
    if asked and grid["columns"] > asked:
        report.failed("the column maximum is respected",
                      "--live-columns is %d and the grid laid out %d columns"
                      % (asked, grid["columns"]))
        return False

    report.ok("ten classrooms draw ten panels",
              "%d panels, %d distinct classes, at most %d names each; "
              "auto-fit chose %d columns of the %d --live-columns allows"
              % (grid["panels"], len(classes), max(capped),
                 grid["columns"], asked))

    # A tile showing six of ten has to say ten, and say what it is not
    # showing - the cap is the backend's, and the panel reports it
    # rather than truncating quietly.
    if any("10" in text for text in counts) and \
            any("more" in note for note in notes):
        report.ok("a capped roster says the real figure",
                  "panels report %r and %r"
                  % (sorted(counts)[0], sorted(notes)[0]))
    else:
        report.failed("a capped roster says the real figure",
                      "count text %s, overflow note %s"
                      % (sorted(counts)[:2] or "absent",
                         sorted(notes)[:1] or "absent"))

    bulk = browser.evaluate(
        "const found = Array.from(document.querySelectorAll('button'))"
        "  .filter((node) => /finish all/i.test(node.textContent || '')"
        "    && node.getClientRects().length);"
        "return found.length ? found[0].textContent.trim() : null;")

    if bulk:
        report.ok("bulk finish appears for a row of classrooms",
                  "one control, %r, beside %d panels" % (bulk, expected))
    else:
        report.failed("bulk finish appears for a row of classrooms",
                      "no visible control named it with %d running classes"
                      % expected)

    spill = browser.evaluate(
        "const doc = document.documentElement;"
        "return {scroll: doc.scrollWidth, client: doc.clientWidth};")
    if spill["scroll"] > spill["client"] + 1:
        report.failed("the ten-panel grid fits its page",
                      "the page scrolls to %dpx in %dpx"
                      % (spill["scroll"], spill["client"]))
        return False

    report.ok("the ten-panel grid fits its page",
              "%dpx of content in a %dpx page"
              % (spill["scroll"], spill["client"]))
    return True


def check_assembly(browser, shots, report):
    browser.click(".nav-item[data-view='assembly']")
    browser.drain(2.0)

    view = browser.evaluate(
        "const view = document.querySelector(\"[data-view='assembly']\")"
        "  || document.querySelector('#view-assembly');"
        "const painted = document.querySelectorAll('.view').length;"
        "const visible = Array.from(document.querySelectorAll('.view'))"
        "  .filter((node) => node.getClientRects().length)"
        "  .map((node) => node.id);"
        "return {visible: visible, views: painted,"
        " buttons: document.querySelectorAll('button:not([hidden])').length};")

    browser.screenshot(shots / "06-assembly.png")

    if not view or not view["visible"]:
        report.failed("assembly mode renders", "no view is visible")
        return False

    report.ok("assembly mode renders",
              "visible view %s, %d operable buttons on the page"
              % (", ".join(view["visible"]), view["buttons"]))
    return True


def check_nothing_left_the_machine(browser, report):
    outside = [url for url in browser.requests
               if not (url.startswith("http://127.0.0.1")
                       or url.startswith("http://localhost")
                       or url.startswith("data:")
                       or url.startswith("blob:")
                       or url.startswith("about:")
                       or url.startswith("chrome"))]

    if outside:
        report.failed("nothing left the machine",
                      "%d request(s) to %s"
                      % (len(outside), ", ".join(sorted(set(outside))[:3])))
        return False

    report.ok("nothing left the machine",
              "%d requests, every one to 127.0.0.1" % len(browser.requests))
    return True


def check_every_request_was_answered(browser, report):
    """
    Nothing the page asked the server for came back 4xx or 5xx.

    A missing asset is invisible in a screenshot and invisible to a test that
    reads the HTML: the page simply renders without it.
    """
    broken = sorted(set(browser.failed_responses))

    if broken:
        report.failed("every request was answered",
                      "%d did not come back: %s"
                      % (len(broken), "; ".join(broken[:4])))
        return False

    report.ok("every request was answered",
              "%d requests, none 4xx or 5xx%s"
              % (len(browser.requests),
                 "" if not browser.aborted else
                 " (%d cancelled by the page itself: %s)"
                 % (len(browser.aborted),
                    ", ".join(sorted({url.rsplit("/", 1)[-1]
                                      for url in browser.aborted})[:3]))))
    return True


def check_no_script_errors(browser, report):
    errors = browser.page_errors + browser.console_errors
    # A missing camera frame is the expected consequence of having no camera:
    # the synthetic runtimes publish presence but never a JPEG. Nothing else
    # is filtered - /favicon.ico used to be excused here, and excusing it is
    # why nobody noticed the page was asking for a file that did not exist.
    errors = [text for text in errors if "live/frame" not in text]

    if errors:
        report.failed("no script error in any view",
                      "%d: %s" % (len(errors), errors[0][:160]))
        return False

    report.ok("no script error in any view",
              "none, across every view visited")
    return True


# -- runner -----------------------------------------------------------------

def find_chrome(given=None):
    if given:
        return given if Path(given).is_file() else None
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return shutil.which("chrome") or shutil.which("google-chrome") \
        or shutil.which("chromium")


def main():
    enable_safe_console()

    parser = argparse.ArgumentParser(
        description="Render the dashboard in real Chrome and measure it.")
    parser.add_argument("--into", default=None,
                        help="where to write the screenshots "
                             "(default: a temporary directory)")
    parser.add_argument("--chrome", default=None,
                        help="path to chrome.exe, if it is somewhere unusual")
    parser.add_argument("--classrooms", type=int, default=10,
                        help="how many synthetic live classrooms to draw")
    arguments = parser.parse_args()

    executable = find_chrome(arguments.chrome)
    if not executable:
        print("Chrome was not found, so the interface was not rendered.")
        print("This check is OPT-IN for exactly that reason: the automated "
              "suite does not need a browser.")
        print("Pass --chrome <path to chrome.exe> if it is installed "
              "somewhere unusual.")
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="classvision_browser_"))
    shots = Path(arguments.into) if arguments.into else workspace / "shots"
    shots.mkdir(parents=True, exist_ok=True)

    print("ClassVision AI - the interface, rendered")
    print("=" * 74)
    print("Chrome:      %s" % executable)
    print("Screenshots: %s" % shots)

    database = None
    server = None
    browser = None
    report = Report()

    try:
        database, counts = seed(workspace / "browser-check.db")
        print("Database:    %s (%d students, %d sessions, %d records)"
              % (workspace / "browser-check.db", counts["students"],
                 counts["sessions"], counts["records"]))

        port = free_port()
        server, _ = serve(port)
        base = "http://127.0.0.1:%d" % port
        registry = classrooms(arguments.classrooms)
        print("Serving:     %s with %d synthetic classrooms (no camera "
              "opened)" % (base, len(registry.active())))
        print("")

        browser = Browser(executable, free_port(), 1600, 1000,
                          workspace / "profile")

        if check_sign_in(browser, base, shots, report) and \
                check_sign_in_works(browser, base, shots, report):
            check_entry_screen(browser, shots, report)
            check_status_row(browser, base, report)
            check_no_sideways_scroll(browser, shots, report)
            check_theme(browser, shots, report)
            check_contrast(browser, report,
                           ".stat-value, .figure, .metric-value, h1, h2",
                           "dashboard figures")
            check_reduced_motion(browser, report)
            check_ten_classrooms(browser, base, shots, report,
                                 expected=arguments.classrooms)
            check_contrast(browser, report,
                           ".live-panel-name, .live-panel-telemetry *, "
                           ".live-panel-roster-list li",
                           "live console panels")
            check_assembly(browser, shots, report)

        check_nothing_left_the_machine(browser, report)
        check_every_request_was_answered(browser, report)
        check_no_script_errors(browser, report)

    except Exception as error:                            # noqa: BLE001
        report.failed("the check itself",
                      "%s: %s" % (type(error).__name__, error))
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:                             # noqa: BLE001
                pass
        if server is not None:
            server.should_exit = True
            time.sleep(1.0)
        try:
            from backend.services.live_service import get_live_registry
            get_live_registry().reset()
        except Exception:                                 # noqa: BLE001
            pass
        if database is not None:
            try:
                from backend.services import state
                state.close_database()
            except Exception:                             # noqa: BLE001
                pass

    print("")
    print("=" * 74)
    print("%d OK, %d failed, %d skipped. Screenshots in %s"
          % (report.count("OK"), report.count("FAILED"),
             report.count("SKIPPED"), shots))

    if report.count("SKIPPED"):
        print("A skipped check is not a pass.")

    if arguments.into is None:
        print("")
        print("The temporary database and profile are in %s and can be "
              "deleted." % workspace)

    return 1 if report.count("FAILED") else 0


if __name__ == "__main__":
    sys.exit(main())
