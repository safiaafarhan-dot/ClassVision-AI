/* =========================================================================
   THE SPATIAL ENGINE
   =========================================================================

   The 3D half of ClassVision AI: the entry scene, the living background, the
   AI core, the student constellation, the glass-cube charts, the spatial
   timeline and the cinematic move between views.

   THERE IS NO THREE.JS, NO WEBGL, NO GSAP AND NO CDN.

   That is not an aesthetic preference, it is the same promise the sign-in
   screen makes in writing: this interface loads and runs on a classroom
   machine with no network. So the 3D here is about ninety lines of
   arithmetic - a Fibonacci point distribution, two rotation matrices written
   out longhand, and one divide for the perspective - drawn onto a 2D canvas.
   Everything else is CSS transforms, which the compositor does on the GPU for
   free. app.js already contained exactly this engine for the sign-in globe;
   this file is that idea applied to the whole product.

   HOW THIS RELATES TO app.js, which is the important part:

     app.js owns the data. Every figure, state and name drawn by this file
     was fetched from the backend by app.js and handed over. This file calls
     no endpoint, holds no attendance state, and invents no value - a
     constellation with nothing to draw draws nothing and says so.

     app.js does not depend on this file. Every call into here is made
     through the window.Spatial object and guarded, so if this script is
     removed, fails to parse or is blocked, the console loses its depth and
     keeps every number, table, form and control it had. The visual layer
     sits on top of a working system; it is not load-bearing.

   WHAT STOPS IT RUNNING, and all four matter:

     prefers-reduced-motion   no loop is started at all. The scenes are
                              drawn once, as still frames, because a blank
                              rectangle where a scene should be looks like a
                              broken page - but nothing moves.
     a narrow viewport        the background loop and the core loop do not
                              start below 1080px. CSS drops the perspective
                              there; a stylesheet cannot stop a
                              requestAnimationFrame, so this does.
     a hidden tab             rAF already throttles, and the visibility
                              check makes the loops stop rather than crawl.
     nothing on screen        each loop checks that its own canvas is still
                              connected and visible, and stops itself when
                              it is not. A dashboard left open on the
                              students page must not be painting a core
                              nobody can see.
   ========================================================================= */

(function () {
  "use strict";

  const TAU = Math.PI * 2;

  /* ======================================================================
     PREFERENCES AND CAPABILITIES
     ====================================================================== */

  function reducedMotion() {
    return !!(window.matchMedia &&
              window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function hoverPointer() {
    return !!(window.matchMedia &&
              window.matchMedia("(hover: hover) and (pointer: fine)").matches);
  }

  /* The same 1080px the stylesheet collapses the stage at. One number, one
     place, so the two halves cannot disagree about where 2.5D begins. */
  function narrowViewport() {
    return !!(window.matchMedia &&
              window.matchMedia("(max-width: 1080px)").matches);
  }

  /* A canvas worth painting into: on screen, laid out, and in the document. */
  function paintable(canvas) {
    if (!canvas || !canvas.isConnected || !canvas.getContext) return false;
    const box = canvas.getBoundingClientRect();
    return box.width > 1 && box.height > 1;
  }

  function sizeCanvas(canvas) {
    /* Device resolution, capped at 2: a 3x buffer on a large monitor is four
       times the pixels for no visible gain. */
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const box = canvas.getBoundingClientRect();
    const width = Math.max(1, Math.round(box.width * ratio));
    const height = Math.max(1, Math.round(box.height * ratio));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    return ratio;
  }

  /* ======================================================================
     THE PALETTE, READ OUT OF THE PAGE
     ----------------------------------------------------------------------
     Not one colour is written in this file. Every mark asks the computed
     style for a token out of styles.css, which means there is ONE definition
     of what colour the room is, the canvases follow a theme change for free,
     and the light theme gets a bright glass atrium rather than a dark scene
     with its alpha turned down.
     ====================================================================== */

  function channels(raw) {
    const value = (raw || "").trim();

    if (value.charAt(0) === "#") {
      const hex = value.length === 4
        ? value.charAt(1) + value.charAt(1) + value.charAt(2) +
          value.charAt(2) + value.charAt(3) + value.charAt(3)
        : value.slice(1, 7);
      return [parseInt(hex.slice(0, 2), 16),
              parseInt(hex.slice(2, 4), 16),
              parseInt(hex.slice(4, 6), 16)];
    }

    /* rgb() or rgba(). Only the three channels are wanted: the alpha a mark
       is drawn at is decided per mark, not taken from the token. */
    const parts = value.replace(/[^0-9.,]/g, "").split(",");
    return [Number(parts[0]) || 0, Number(parts[1]) || 0, Number(parts[2]) || 0];
  }

  function readToken(style, name, fallback) {
    const raw = (style.getPropertyValue(name) || "").trim();
    return channels(raw || fallback);
  }

  function rgba(colour, alpha) {
    return "rgba(" + Math.round(colour[0]) + ", " + Math.round(colour[1]) +
           ", " + Math.round(colour[2]) + ", " + alpha.toFixed(3) + ")";
  }

  /*
     The scene palette, plus one derived number.

     `weight` is the whole of the theme adaptation. Ink on a near-white ground
     needs more alpha to register than light on near-black does, so every
     alpha in this file is multiplied by it. One number, measured from the
     ground colour, instead of a second set of light-theme literals.
  */
  function palette() {
    const style = window.getComputedStyle(document.body);
    const ground = readToken(style, "--bg", "#05070d");
    const luminance =
      (ground[0] * 0.299 + ground[1] * 0.587 + ground[2] * 0.114) / 255;

    return {
      ground: ground,
      core: readToken(style, "--core", "#2ee6ff"),
      core2: readToken(style, "--core-2", "#9d8cff"),
      spark: readToken(style, "--spark", "#bef0ff"),
      trail: readToken(style, "--trail", "#7dd3fc"),
      line: readToken(style, "--glass-line", "#94b4de"),
      edge: readToken(style, "--glass-edge", "#bee8ff"),
      ink: readToken(style, "--text-muted", "#9aabc7"),
      light: luminance > 0.5,
      weight: luminance > 0.5 ? 1.75 : 1
    };
  }

  /* ======================================================================
     THE 3D ENGINE
     ----------------------------------------------------------------------
     This is all of it.

     A point is [x, y, z] in object space. `camera()` precomputes the four
     trigonometric values for a yaw and a pitch so they are not recomputed
     per point, and `project()` rotates about Y, then about X, then divides
     once for a one-point perspective.

     Nearer points spread further from the centre and are drawn brighter and
     larger, which is the only depth cue a scene this abstract needs. The
     returned `s` is that scale factor, and callers use it for size, alpha
     and line width alike.
     ====================================================================== */

  function camera(yaw, pitch, distance) {
    return {
      sinY: Math.sin(yaw), cosY: Math.cos(yaw),
      sinX: Math.sin(pitch), cosX: Math.cos(pitch),
      distance: distance
    };
  }

  function project(point, cam, centreX, centreY, scale) {
    const x = point[0], y = point[1], z = point[2];

    const x1 = x * cam.cosY - z * cam.sinY;
    const z1 = x * cam.sinY + z * cam.cosY;
    const y2 = y * cam.cosX - z1 * cam.sinX;
    const z2 = y * cam.sinX + z1 * cam.cosX;

    const depth = 1 / Math.max(0.25, cam.distance - z2);

    return {
      x: centreX + x1 * scale * depth * cam.distance,
      y: centreY - y2 * scale * depth * cam.distance,
      s: depth * cam.distance,
      z: z2
    };
  }

  /* Points spread evenly over a sphere. A lat/long grid bunches them at the
     poles and reads as a net; the golden-angle spiral is even for any count,
     which is worth the two lines. */
  function spherePoints(count) {
    const points = [];
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let index = 0; index < count; index += 1) {
      const y = 1 - (index / Math.max(1, count - 1)) * 2;
      const radius = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = golden * index;
      points.push([Math.cos(theta) * radius, y, Math.sin(theta) * radius]);
    }
    return points;
  }

  function drawDot(context, at, radius, fill) {
    context.fillStyle = fill;
    context.beginPath();
    context.arc(at.x, at.y, Math.max(0.3, radius), 0, TAU);
    context.fill();
  }

  /* A quad in object space, projected and filled. Used for every flat
     surface in the miniature classroom: the board, the desks, the floor
     panels. Four projections and a path. */
  function drawQuad(context, corners, cam, cx, cy, scale, fill, stroke) {
    const flat = corners.map((point) => project(point, cam, cx, cy, scale));
    context.beginPath();
    context.moveTo(flat[0].x, flat[0].y);
    for (let index = 1; index < flat.length; index += 1) {
      context.lineTo(flat[index].x, flat[index].y);
    }
    context.closePath();
    if (fill) { context.fillStyle = fill; context.fill(); }
    if (stroke) { context.strokeStyle = stroke; context.stroke(); }
    return flat;
  }

  /* ======================================================================
     THE POINTER
     ----------------------------------------------------------------------
     One listener for the whole interface. It writes --mx and --my on the
     root, normalised to -1..1 from the centre of the viewport, and every
     parallaxing layer in spatial.css reads those two numbers and decides for
     itself how far to lean.

     The cursor is drawn here too, in the same handler, for the same reason:
     two pointermove listeners doing trigonometry is twice the work on a
     signal that fires at the refresh rate.
     ====================================================================== */

  const POINTER = {
    bound: false,
    x: 0, y: 0,            /* where the pointer is, in pixels */
    ringX: 0, ringY: 0,    /* where the trailing ring has got to */
    dot: null, ring: null,
    frame: null
  };

  function buildCursor() {
    const dot = document.createElement("div");
    dot.className = "cursor-dot";
    dot.setAttribute("aria-hidden", "true");

    const ring = document.createElement("div");
    ring.className = "cursor-ring";
    ring.setAttribute("aria-hidden", "true");

    document.body.appendChild(ring);
    document.body.appendChild(dot);

    POINTER.dot = dot;
    POINTER.ring = ring;
  }

  /*
     What is under the pointer, as one of five words.

     Read off the element rather than from a registry of selectors that
     JavaScript has to keep in step with the DOM: closest() asks the page
     what this thing is, so a button added next year is a control without
     anybody updating this function.
  */
  function pointerContext(target) {
    if (!target || !target.closest) return "";

    if (target.closest("input, textarea, select")) return "";
    if (target.closest("button, a, [role=\"tab\"], [role=\"radio\"], " +
                       "summary, label")) return "control";
    if (target.closest(".orb-figure, .hall-seat, .cube-bar, .spine-node, " +
                       ".seat, .att-ring")) return "object";
    if (target.closest(".pane, .panel, .card, .hero, .glass-cube, .hall, " +
                       "dialog")) return "glass";
    if (target.closest("p, td, th, li, h1, h2, h3, dd, dt")) return "text";
    return "";
  }

  function stepCursor() {
    POINTER.frame = null;
    if (!POINTER.ring) return;

    /* Critically damped: the ring closes a fixed fraction of the remaining
       gap each frame, so it lags and arrives and never overshoots. A spring
       would wobble, and a wobbling pointer feels broken. */
    POINTER.ringX += (POINTER.x - POINTER.ringX) * 0.22;
    POINTER.ringY += (POINTER.y - POINTER.ringY) * 0.22;

    POINTER.ring.style.transform =
      "translate3d(" + POINTER.ringX.toFixed(1) + "px, " +
      POINTER.ringY.toFixed(1) + "px, 0)";

    const gap = Math.abs(POINTER.x - POINTER.ringX) +
                Math.abs(POINTER.y - POINTER.ringY);
    if (gap > 0.6) POINTER.frame = window.requestAnimationFrame(stepCursor);
  }

  function bindPointer() {
    if (POINTER.bound) return;
    POINTER.bound = true;

    const drawsCursor = hoverPointer();
    if (drawsCursor) buildCursor();

    document.addEventListener("pointermove", (event) => {
      if (event.pointerType === "touch") return;

      /* The parallax. Two properties, and the whole room leans off them. */
      const width = window.innerWidth || 1;
      const height = window.innerHeight || 1;
      const root = document.documentElement;
      root.style.setProperty("--mx",
        ((event.clientX / width) * 2 - 1).toFixed(3));
      root.style.setProperty("--my",
        ((event.clientY / height) * 2 - 1).toFixed(3));

      if (!POINTER.dot) return;

      POINTER.x = event.clientX;
      POINTER.y = event.clientY;

      /* The dot is pinned to the pointer, with no interpolation at all: a
         lagging dot is a pointer that has stopped tracking. */
      POINTER.dot.style.transform =
        "translate3d(" + POINTER.x + "px, " + POINTER.y + "px, 0)";

      if (!document.body.classList.contains("has-cursor")) {
        document.body.classList.add("has-cursor");
        POINTER.ringX = POINTER.x;
        POINTER.ringY = POINTER.y;
      }

      const over = pointerContext(event.target);
      if (POINTER.ring.dataset.over !== over) POINTER.ring.dataset.over = over;

      if (reducedMotion()) {
        POINTER.ringX = POINTER.x;
        POINTER.ringY = POINTER.y;
        POINTER.ring.style.transform = POINTER.dot.style.transform;
      } else if (!POINTER.frame) {
        POINTER.frame = window.requestAnimationFrame(stepCursor);
      }
    }, { passive: true });

    document.addEventListener("pointerdown", () => {
      document.body.classList.add("pressing");
    }, { passive: true });
    document.addEventListener("pointerup", () => {
      document.body.classList.remove("pressing");
    }, { passive: true });

    /* The pointer leaving the window. The marks go rather than freezing at
       the edge of the screen like something that has crashed. */
    document.addEventListener("pointerleave", () => {
      document.body.classList.remove("has-cursor");
    });

    /* And a keyboard user gets the native focus ring back, because a drawn
       cursor is meaningless to somebody who is tabbing. */
    document.addEventListener("keydown", (event) => {
      if (event.key === "Tab") document.body.classList.remove("has-cursor");
    });
  }

  /* ======================================================================
     THE LIVING BACKGROUND
     ----------------------------------------------------------------------
     Motes, glass fragments, light trails and a few large spheres, all far
     away, all drifting slowly, on one canvas behind everything.

     Every element is stored in a UNIT SQUARE and scaled at paint time, so a
     window resize does not rebuild the field and the background does not
     jump when somebody drags the window edge. The third number on a mote is
     its depth, and it sets the size, the brightness and the speed at once -
     that is the whole of the parallax within the layer.

     It never distracts, which is a measurable claim: nothing in here is
     drawn above alpha 0.5, nothing moves faster than a few pixels a second,
     and the fog gradient over the top of it removes most of what is left at
     the edges of the screen.
     ====================================================================== */

  const DEEP = {
    canvas: null, context: null,
    motes: null, shards: null, trails: null, spheres: null,
    clock: 0, frame: null, running: false
  };

  function buildDeep() {
    if (DEEP.motes) return;

    DEEP.motes = [];
    for (let index = 0; index < 90; index += 1) {
      DEEP.motes.push([Math.random(), Math.random(), Math.random()]);
    }

    /* Glass fragments: a position, a depth, a size and a turn rate. Drawn as
       small rotating quadrilaterals, which is enough to read as a shard of
       something transparent at this scale. */
    DEEP.shards = [];
    for (let index = 0; index < 11; index += 1) {
      DEEP.shards.push({
        x: Math.random(), y: Math.random(),
        depth: 0.3 + Math.random() * 0.7,
        size: 10 + Math.random() * 26,
        spin: (Math.random() - 0.5) * 0.3,
        angle: Math.random() * TAU
      });
    }

    /* Light trails: a start, a length, a speed. Thin horizontal streaks that
       cross the screen and wrap. */
    DEEP.trails = [];
    for (let index = 0; index < 7; index += 1) {
      DEEP.trails.push({
        y: Math.random(),
        at: Math.random(),
        length: 0.08 + Math.random() * 0.2,
        speed: 0.004 + Math.random() * 0.012
      });
    }

    /* Three large, very faint spheres a long way off, turning slowly. They
       are what gives the fog something to be in front of. */
    DEEP.spheres = [
      { x: 0.16, y: 0.26, radius: 0.2,  points: spherePoints(70), angle: 0.4 },
      { x: 0.86, y: 0.7,  radius: 0.15, points: spherePoints(54), angle: 2.1 },
      { x: 0.5,  y: 1.06, radius: 0.3,  points: spherePoints(90), angle: 4.3 }
    ];
  }

  function drawDeep() {
    const canvas = DEEP.canvas;
    const context = DEEP.context;
    const width = canvas.width;
    const height = canvas.height;
    const colours = palette();
    const weight = colours.weight;
    const clock = DEEP.clock;

    context.clearRect(0, 0, width, height);

    /* ---- the far spheres ---- */

    for (const sphere of DEEP.spheres) {
      const cam = camera(sphere.angle + clock * 0.08, 0.38, 2.6);
      const centreX = sphere.x * width;
      const centreY = sphere.y * height;
      const scale = sphere.radius * Math.min(width, height);

      for (const point of sphere.points) {
        const at = project(point, cam, centreX, centreY, scale);
        const near = (at.z + 1) * 0.5;
        drawDot(context, at, 0.4 + near * 1.1,
                rgba(colours.core, (0.02 + near * 0.06) * weight));
      }
    }

    /* ---- the light trails ---- */

    context.lineWidth = 1;
    for (const trail of DEEP.trails) {
      const head = (trail.at + clock * trail.speed) % 1;
      const y = trail.y * height;
      const from = head * width;
      const to = (head - trail.length) * width;

      const gradient = context.createLinearGradient(from, y, to, y);
      gradient.addColorStop(0, rgba(colours.trail, 0.26 * weight));
      gradient.addColorStop(1, rgba(colours.trail, 0));
      context.strokeStyle = gradient;
      context.beginPath();
      context.moveTo(from, y);
      context.lineTo(to, y);
      context.stroke();
    }

    /* ---- the glass fragments ---- */

    for (const shard of DEEP.shards) {
      const y = ((shard.y + clock * 0.004 * shard.depth) % 1.2) - 0.1;
      const centreX = shard.x * width;
      const centreY = y * height;
      const angle = shard.angle + clock * shard.spin * 0.1;
      const size = shard.size * shard.depth;

      context.save();
      context.translate(centreX, centreY);
      context.rotate(angle);
      context.beginPath();
      context.moveTo(-size * 0.5, -size * 0.34);
      context.lineTo(size * 0.6, -size * 0.2);
      context.lineTo(size * 0.42, size * 0.5);
      context.lineTo(-size * 0.36, size * 0.3);
      context.closePath();
      context.fillStyle = rgba(colours.edge, 0.025 * weight * shard.depth);
      context.fill();
      context.strokeStyle = rgba(colours.line, 0.1 * weight * shard.depth);
      context.lineWidth = 1;
      context.stroke();
      context.restore();
    }

    /* ---- the motes ---- */

    for (const mote of DEEP.motes) {
      const depth = mote[2];
      const x = ((mote[0] + clock * (0.002 + depth * 0.006)) % 1) * width;
      const y = ((mote[1] - clock * 0.0014 * depth + 1) % 1) * height;
      const colour = depth > 0.82 ? colours.core2 : colours.spark;
      drawDot(context, { x: x, y: y }, 0.4 + depth * 1.5,
              rgba(colour, (0.05 + depth * 0.2) * weight));
    }
  }

  function stepDeep() {
    if (!DEEP.running) { DEEP.frame = null; return; }

    if (!paintable(DEEP.canvas)) {
      /* The layer went away. Stop rather than paint into nothing. */
      DEEP.running = false;
      DEEP.frame = null;
      return;
    }

    if (!document.hidden) {
      DEEP.clock += 1;
      drawDeep();
    }
    DEEP.frame = window.requestAnimationFrame(stepDeep);
  }

  function startDeep() {
    const canvas = DEEP.canvas;
    if (!canvas || !canvas.getContext) return;

    buildDeep();
    DEEP.context = DEEP.context || canvas.getContext("2d");

    /* Below the stage breakpoint there is no background loop at all. The fog
       and the floor are CSS and stay; a per-frame canvas on a phone is a
       battery cost for an effect the small screen cannot show anyway. */
    if (narrowViewport()) {
      stopDeep();
      canvas.hidden = true;
      return;
    }
    canvas.hidden = false;

    if (!paintable(canvas)) return;
    sizeCanvas(canvas);

    if (reducedMotion()) {
      /* One still frame: a field of motes that does not drift. */
      stopDeep();
      drawDeep();
      return;
    }

    if (DEEP.running) return;
    DEEP.running = true;
    DEEP.frame = window.requestAnimationFrame(stepDeep);
  }

  function stopDeep() {
    DEEP.running = false;
    if (DEEP.frame) {
      window.cancelAnimationFrame(DEEP.frame);
      DEEP.frame = null;
    }
  }

  function installDeep() {
    if (DEEP.canvas) return;

    const canvas = document.createElement("canvas");
    canvas.className = "deep";
    canvas.id = "deep-canvas";
    canvas.setAttribute("aria-hidden", "true");

    const fog = document.createElement("div");
    fog.className = "deep-fog";
    fog.setAttribute("aria-hidden", "true");

    const floor = document.createElement("div");
    floor.className = "deep-floor";
    floor.setAttribute("aria-hidden", "true");

    /* First three children of the body, so they are behind everything in
       paint order as well as in z-index. */
    document.body.insertBefore(floor, document.body.firstChild);
    document.body.insertBefore(fog, document.body.firstChild);
    document.body.insertBefore(canvas, document.body.firstChild);

    DEEP.canvas = canvas;
    startDeep();
  }

  /* ======================================================================
     THE ENTRY SCENE
     ----------------------------------------------------------------------
     A miniature futuristic classroom, turning inside a glass sphere.

     What is in there, and all of it is drawn from the geometry below rather
     than from an image: a floor of light, a digital board at the far end, a
     teacher position in front of it, a camera above looking down the room
     with its field of view drawn as two lines, four rows of students as
     upright glass cards, a desk in front of each row, detection points that
     settle onto one student at a time, and a thin column of data motes
     rising from whoever has just been detected.

     The whole scene is clipped to a circle, which is what makes it read as
     being INSIDE the sphere rather than drawn over it. The shell, the
     highlight and the rim light are CSS on top.

     It is a threshold and not a splash screen: the button has focus the
     moment it appears, so Enter crosses it, and Escape does too. The
     dashboard loads behind it the entire time.
     ====================================================================== */

  const ENTRY = {
    canvas: null, context: null,
    students: null, motes: null,
    angle: 0, clock: 0,
    frame: null, running: false
  };

  /* The room, in object space. x across, y up, z towards the reader; the
     far wall is at z = -1 and the front row at z = 0.75. Built once. */
  function buildEntryRoom() {
    if (ENTRY.students) return;

    const students = [];
    const columns = 5;
    const rows = 4;

    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < columns; column += 1) {
        students.push({
          x: -0.62 + (column / (columns - 1)) * 1.24,
          z: -0.42 + (row / (rows - 1)) * 1.08,
          /* A per-student phase, so the glow does not pulse in unison and
             the room reads as a roomful of people rather than a grid. */
          phase: Math.random() * TAU,
          row: row
        });
      }
    }
    ENTRY.students = students;

    /* Data motes rising out of the floor. Stored with a start height so they
       do not all begin at the floor on the first frame. */
    ENTRY.motes = [];
    for (let index = 0; index < 46; index += 1) {
      ENTRY.motes.push({
        x: -0.8 + Math.random() * 1.6,
        z: -0.9 + Math.random() * 1.7,
        y: Math.random(),
        speed: 0.002 + Math.random() * 0.004
      });
    }
  }

  function drawEntryScene() {
    const canvas = ENTRY.canvas;
    const context = ENTRY.context;
    const width = canvas.width;
    const height = canvas.height;
    const colours = palette();
    const weight = colours.weight;

    const centreX = width * 0.5;
    const centreY = height * 0.5;
    const radius = Math.min(width, height) * 0.405;
    const scale = Math.min(width, height) * 0.3;

    context.clearRect(0, 0, width, height);

    /* Inside the sphere, and only inside it. */
    context.save();
    context.beginPath();
    context.arc(centreX, centreY, radius, 0, TAU);
    context.clip();

    /* The room turns slowly, and leans a few degrees with the pointer. The
       lean is read off the same two custom properties the CSS uses, so the
       canvas and the glass shell above it move together. */
    const rootStyle = window.getComputedStyle(document.documentElement);
    const leanX = Number(rootStyle.getPropertyValue("--mx")) || 0;
    const leanY = Number(rootStyle.getPropertyValue("--my")) || 0;

    const cam = camera(ENTRY.angle + leanX * 0.22,
                       0.5 + leanY * 0.1, 3.1);

    /* ---- the floor ---- */

    const floorY = -0.46;
    drawQuad(context, [[-0.95, floorY, -1.05], [0.95, floorY, -1.05],
                       [0.95, floorY, 1.0], [-0.95, floorY, 1.0]],
             cam, centreX, centreY, scale,
             rgba(colours.core, 0.045 * weight), null);

    /* The grid on it: lines across and lines up the room. Thin, and fainter
       towards the far wall, which is the whole depth cue. */
    context.lineWidth = 1;
    for (let step = 0; step <= 8; step += 1) {
      const z = -1.05 + (step / 8) * 2.05;
      const fade = 0.04 + (step / 8) * 0.09;
      drawQuad(context, [[-0.95, floorY, z], [0.95, floorY, z]],
               cam, centreX, centreY, scale, null,
               rgba(colours.trail, fade * weight));
    }
    for (let step = 0; step <= 6; step += 1) {
      const x = -0.95 + (step / 6) * 1.9;
      drawQuad(context, [[x, floorY, -1.05], [x, floorY, 1.0]],
               cam, centreX, centreY, scale, null,
               rgba(colours.trail, 0.05 * weight));
    }

    /* ---- the digital board, on the far wall ---- */

    drawQuad(context, [[-0.66, 0.44, -1.04], [0.66, 0.44, -1.04],
                       [0.66, -0.12, -1.04], [-0.66, -0.12, -1.04]],
             cam, centreX, centreY, scale,
             rgba(colours.core, 0.1 * weight),
             rgba(colours.edge, 0.3 * weight));

    /* Three lines of light on the board, sweeping - a board with something
       on it, without drawing anything anybody could try to read. */
    for (let line = 0; line < 3; line += 1) {
      const sweep = (Math.sin(ENTRY.clock * 0.012 + line) + 1) * 0.5;
      const left = -0.58 + line * 0.04;
      const right = left + 0.3 + sweep * 0.5;
      const y = 0.3 - line * 0.14;
      drawQuad(context, [[left, y, -1.03], [Math.min(right, 0.58), y, -1.03]],
               cam, centreX, centreY, scale, null,
               rgba(colours.core, (0.2 + sweep * 0.2) * weight));
    }

    /* ---- the teacher position ---- */

    drawQuad(context, [[-0.1, -0.46, -0.78], [0.1, -0.46, -0.78],
                       [0.1, -0.1, -0.78], [-0.1, -0.1, -0.78]],
             cam, centreX, centreY, scale,
             rgba(colours.core2, 0.2 * weight),
             rgba(colours.core2, 0.4 * weight));

    /* ---- the camera, above and behind the reader's end of the room ---- */

    const camAt = [0, 0.56, 0.92];
    const lens = project(camAt, cam, centreX, centreY, scale);

    /* Its field of view, as two lines down onto the floor. This is the one
       mark in the scene that says what the product does. */
    for (const corner of [[-0.72, floorY, -0.9], [0.72, floorY, -0.9]]) {
      const foot = project(corner, cam, centreX, centreY, scale);
      const beam = context.createLinearGradient(lens.x, lens.y, foot.x, foot.y);
      beam.addColorStop(0, rgba(colours.core, 0.26 * weight));
      beam.addColorStop(1, rgba(colours.core, 0));
      context.strokeStyle = beam;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(lens.x, lens.y);
      context.lineTo(foot.x, foot.y);
      context.stroke();
    }

    /* The camera body and its lens. */
    drawQuad(context, [[-0.09, 0.64, 0.92], [0.09, 0.64, 0.92],
                       [0.09, 0.48, 0.92], [-0.09, 0.48, 0.92]],
             cam, centreX, centreY, scale,
             rgba(colours.edge, 0.12 * weight),
             rgba(colours.line, 0.45 * weight));
    drawDot(context, lens, 2.4 * lens.s, rgba(colours.core, 0.85 * weight));

    /* ---- the students, far rows first ---- */

    /* Which student the system is looking at this second. One at a time,
       cycling, because a room where everybody is being detected at once is a
       room with no sense of a process happening in it. */
    const detectedIndex =
      Math.floor(ENTRY.clock / 46) % ENTRY.students.length;

    const ordered = ENTRY.students
      .map((student, index) => ({ student: student, index: index }))
      .sort((one, two) => one.student.z - two.student.z);

    for (const entry of ordered) {
      const student = entry.student;
      const detected = entry.index === detectedIndex;

      const base = [student.x, floorY, student.z];
      const top = [student.x, floorY + 0.3, student.z];
      const head = project([student.x, floorY + 0.36, student.z],
                           cam, centreX, centreY, scale);

      /* The desk in front of them. */
      drawQuad(context, [[student.x - 0.09, floorY + 0.02, student.z + 0.12],
                         [student.x + 0.09, floorY + 0.02, student.z + 0.12],
                         [student.x + 0.09, floorY + 0.02, student.z + 0.03],
                         [student.x - 0.09, floorY + 0.02, student.z + 0.03]],
               cam, centreX, centreY, scale,
               rgba(colours.line, 0.1 * weight), null);

      /* The silhouette: an upright glass card, brighter when detected and
         breathing gently otherwise. Not a figure of a person - an abstract
         presence, which is the honest way to draw somebody the system has
         not been asked to picture. */
      const breath = 0.5 + Math.sin(ENTRY.clock * 0.02 + student.phase) * 0.5;
      const alpha = detected
        ? 0.5 * weight
        : (0.12 + breath * 0.1) * weight;

      drawQuad(context, [[student.x - 0.062, floorY, student.z],
                         [student.x + 0.062, floorY, student.z],
                         [student.x + 0.062, floorY + 0.3, student.z],
                         [student.x - 0.062, floorY + 0.3, student.z]],
               cam, centreX, centreY, scale,
               rgba(detected ? colours.core : colours.edge, alpha),
               rgba(colours.line, (detected ? 0.6 : 0.2) * weight));

      /* The head: a bead above the card. */
      drawDot(context, head, 2.3 * head.s,
              rgba(detected ? colours.core : colours.edge,
                   (detected ? 0.95 : 0.3) * weight));

      /* The detection marker over whoever is being identified: a small
         bracket, which is the same shape the camera overlay uses. */
      if (detected) {
        const size = 9 * head.s;
        context.strokeStyle = rgba(colours.core, 0.9 * weight);
        context.lineWidth = 1.2;
        for (const side of [-1, 1]) {
          context.beginPath();
          context.moveTo(head.x + side * size, head.y - size * 1.5);
          context.lineTo(head.x + side * size, head.y - size * 2.1);
          context.lineTo(head.x + side * size * 0.4, head.y - size * 2.1);
          context.stroke();
        }
        void base; void top;
      }
    }

    /* ---- the data motes ---- */

    for (const mote of ENTRY.motes) {
      mote.y += mote.speed;
      if (mote.y > 1) mote.y = 0;

      const at = project([mote.x, floorY + mote.y * 0.95, mote.z],
                         cam, centreX, centreY, scale);
      drawDot(context, at, 0.8 * at.s,
              rgba(colours.spark, (1 - mote.y) * 0.3 * weight));
    }

    context.restore();

    /* ---- the rim, inside the clip boundary ---- */

    context.strokeStyle = rgba(colours.edge, 0.22 * weight);
    context.lineWidth = 1;
    context.beginPath();
    context.arc(centreX, centreY, radius, 0, TAU);
    context.stroke();
  }

  function stepEntry() {
    if (!ENTRY.running) { ENTRY.frame = null; return; }

    const screen = document.getElementById("entry-screen");
    if (!paintable(ENTRY.canvas) || !screen || screen.hidden) {
      ENTRY.running = false;
      ENTRY.frame = null;
      return;
    }

    if (!document.hidden) {
      ENTRY.clock += 1;
      ENTRY.angle += 0.0016;
      drawEntryScene();
    }
    ENTRY.frame = window.requestAnimationFrame(stepEntry);
  }

  function startEntryScene() {
    const canvas = document.getElementById("entry-canvas");
    if (!canvas || !canvas.getContext) return;

    ENTRY.canvas = canvas;
    ENTRY.context = canvas.getContext("2d");
    buildEntryRoom();

    if (!paintable(canvas)) return;
    sizeCanvas(canvas);

    if (reducedMotion()) {
      /* One frame of the room, with the sweep and the detection at a fixed
         moment. Everything in the scene is visible; none of it moves. */
      stopEntryScene();
      drawEntryScene();
      return;
    }

    if (ENTRY.running) return;
    ENTRY.running = true;
    ENTRY.frame = window.requestAnimationFrame(stepEntry);
  }

  function stopEntryScene() {
    ENTRY.running = false;
    if (ENTRY.frame) {
      window.cancelAnimationFrame(ENTRY.frame);
      ENTRY.frame = null;
    }
  }

  /* ======================================================================
     THE AI CORE
     ----------------------------------------------------------------------
     The object in the middle of the room.

     Two counter-rotating shells of points with three great circles through
     them and a brighter inner core, plus motes in orbit on tilted planes.
     Counter-rotating is the whole trick: one turning sphere reads as a
     turning sphere, and two turning opposite ways read as a mechanism.

     It has three states and they are not decoration - they are what the
     pipeline is doing, handed over by app.js from /api/live/status:

       idle      nothing running. Slow, dim, no pulse.
       live      a session is running. Brighter, faster, and the inner core
                 pulses once per second, which is roughly the rate the
                 status endpoint is actually polled at.
       settling  a session has just ended. The rotation decays towards
                 idle rather than stopping dead, so the room comes to rest.

     The word under it says which, because a reader must never have to infer
     a system state from a rotation speed.
     ====================================================================== */

  const CORE = {
    canvas: null, context: null,
    shell: null, inner: null, orbits: null,
    angle: 0, clock: 0, speed: 0.004, target: 0.004,
    state: "idle",
    frame: null, running: false
  };

  function buildCore() {
    if (CORE.shell) return;

    CORE.shell = spherePoints(130);
    CORE.inner = spherePoints(46);

    /* The orbiting motes: each on its own tilted plane, at its own radius
       and rate. Three planes rather than one, so the orbits cross. */
    CORE.orbits = [];
    for (let index = 0; index < 22; index += 1) {
      CORE.orbits.push({
        radius: 1.16 + Math.random() * 0.4,
        tilt: (index % 3) * 0.7 + 0.2,
        at: Math.random() * TAU,
        rate: 0.004 + Math.random() * 0.01
      });
    }
  }

  function drawCore() {
    const canvas = CORE.canvas;
    const context = CORE.context;
    const width = canvas.width;
    const height = canvas.height;
    const colours = palette();
    const weight = colours.weight;

    const centreX = width * 0.5;
    const centreY = height * 0.5;
    /* Large enough to read as an object rather than as a smudge inside the
       shell. The first pass drew it at 0.19 and the core was a faint haze in
       the middle of an empty sphere; the shell is at 6% inset, so the core
       wants to fill most of what is left. */
    const scale = Math.min(width, height) * 0.3;

    /* How lit the core is. One number, from the state, used for every alpha
       in here - so "the session is running" is visible as the whole object
       brightening rather than as one indicator changing. */
    const live = CORE.state === "live";
    /* Idle is dimmer than live and still has to be VISIBLE: an idle core that
       cannot be seen reads as a broken canvas rather than as a system at
       rest. 0.68 against the live 0.82-1.0 is a difference somebody notices
       without having to look for it. */
    const pulse = live
      ? 0.82 + Math.sin(CORE.clock * 0.055) * 0.18
      : 0.68;

    context.clearRect(0, 0, width, height);

    const cam = camera(CORE.angle, 0.44, 3.0);
    const counter = camera(-CORE.angle * 1.45, 0.44, 3.0);

    /* ---- three great circles ---- */

    context.lineWidth = 1;
    for (let ring = 0; ring < 3; ring += 1) {
      const tilt = ring * (Math.PI / 3);
      context.strokeStyle = rgba(colours.core, 0.14 * pulse * weight);
      context.beginPath();
      for (let step = 0; step <= 64; step += 1) {
        const theta = (step / 64) * TAU;
        const point = [
          Math.cos(theta),
          Math.sin(theta) * Math.cos(tilt),
          Math.sin(theta) * Math.sin(tilt)
        ];
        const at = project(point, cam, centreX, centreY, scale);
        if (step === 0) context.moveTo(at.x, at.y);
        else context.lineTo(at.x, at.y);
      }
      context.stroke();
    }

    /* ---- the outer shell ---- */

    for (const point of CORE.shell) {
      const at = project(point, cam, centreX, centreY, scale);
      const near = (at.z + 1) * 0.5;
      drawDot(context, at, 0.4 + near * 1.3,
              rgba(near > 0.88 ? colours.core2 : colours.core,
                   (0.08 + near * 0.3) * pulse * weight));
    }

    /* ---- the inner core, counter-rotating and solid-looking ---- */

    for (const point of CORE.inner) {
      const at = project([point[0] * 0.44, point[1] * 0.44, point[2] * 0.44],
                         counter, centreX, centreY, scale);
      const near = (at.z + 1) * 0.5;
      drawDot(context, at, 0.7 + near * 1.6,
              rgba(colours.spark, (0.14 + near * 0.46) * pulse * weight));
    }

    /* ---- the motes in orbit ---- */

    for (const orbit of CORE.orbits) {
      orbit.at += orbit.rate * (live ? 1.6 : 0.6);
      const point = [
        Math.cos(orbit.at) * orbit.radius,
        Math.sin(orbit.at) * orbit.radius * Math.sin(orbit.tilt),
        Math.sin(orbit.at) * orbit.radius * Math.cos(orbit.tilt)
      ];
      const at = project(point, cam, centreX, centreY, scale);
      const near = (at.z + 1) * 0.5;
      drawDot(context, at, 0.6 + near * 1.5,
              rgba(colours.spark, (0.12 + near * 0.4) * pulse * weight));
    }
  }

  function stepCore() {
    if (!CORE.running) { CORE.frame = null; return; }

    if (!paintable(CORE.canvas)) {
      CORE.running = false;
      CORE.frame = null;
      return;
    }

    if (!document.hidden) {
      /* The rotation eases towards its target rate rather than switching to
         it, which is what makes a session starting feel like the room coming
         alive and a session ending feel like it settling. */
      CORE.speed += (CORE.target - CORE.speed) * 0.02;
      CORE.angle += CORE.speed;
      CORE.clock += 1;
      drawCore();
    }
    CORE.frame = window.requestAnimationFrame(stepCore);
  }

  function startCore() {
    const canvas = document.getElementById("core-canvas");
    if (!canvas || !canvas.getContext) return;

    CORE.canvas = canvas;
    CORE.context = canvas.getContext("2d");
    buildCore();

    if (!paintable(canvas)) return;
    sizeCanvas(canvas);

    if (reducedMotion()) {
      stopCore();
      drawCore();
      return;
    }

    if (CORE.running) return;
    CORE.running = true;
    CORE.frame = window.requestAnimationFrame(stepCore);
  }

  function stopCore() {
    CORE.running = false;
    if (CORE.frame) {
      window.cancelAnimationFrame(CORE.frame);
      CORE.frame = null;
    }
  }

  /*
     The state, set by app.js out of the live status it already fetched.

     `word` and `detail` are written into the caption under the core. They
     are the authority on what the core means; the brightness is a second
     signal on top of a word that is already there.
  */
  function setCoreState(state, word, detail) {
    CORE.state = state === "live" ? "live"
               : state === "settling" ? "settling" : "idle";
    CORE.target = CORE.state === "live" ? 0.0115
                : CORE.state === "settling" ? 0.0025 : 0.004;

    const wordNode = document.getElementById("core-word");
    const detailNode = document.getElementById("core-state");
    if (wordNode && typeof word === "string") wordNode.textContent = word;
    if (detailNode && typeof detail === "string") detailNode.textContent = detail;

    /* A still core must still show its state, so redraw once when nothing is
       looping. */
    if (!CORE.running && CORE.canvas && paintable(CORE.canvas)) drawCore();
  }

  /* ======================================================================
     THE ATTENDANCE RING
     ----------------------------------------------------------------------
     One registered custom property away from being a plain conic gradient.

     Because --arc is declared with syntax "<angle>" in spatial.css it is
     interpolable, so setting it to a new value makes the browser SWEEP the
     arc there over the transition - no JavaScript animation, no rAF, and it
     is the compositor doing the work. Where @property is unsupported the
     value jumps, which shows the right number without the sweep.

     A missing value is not zero. A ring showing 0% for a class that has not
     met yet is a lie, and this product has a convention for that already:
     the words "No data", set smaller, with the arc left empty.
     ====================================================================== */

  function setRing(id, value, label) {
    const ring = document.getElementById(id);
    if (!ring) return;

    const valueNode = ring.querySelector(".att-ring-value");
    const labelNode = ring.querySelector(".att-ring-label");
    if (labelNode && typeof label === "string") labelNode.textContent = label;
    if (!valueNode) return;

    const known = typeof value === "number" && isFinite(value);

    if (!known) {
      ring.style.setProperty("--arc", "0deg");
      valueNode.textContent = "No data";
      valueNode.classList.add("nodata");
      ring.setAttribute("aria-label",
        (label || "Attendance") + ": no data");
      return;
    }

    const clamped = Math.max(0, Math.min(100, value));
    ring.style.setProperty("--arc", (clamped * 3.6).toFixed(2) + "deg");
    valueNode.textContent = Math.round(clamped) + "%";
    valueNode.classList.remove("nodata");

    /* The ring is a graphic, so it says what it shows. Without this it is a
       percentage drawn as a gradient, which a screen reader cannot see at
       all - the figure inside is text, but the arc carries the comparison. */
    ring.setAttribute("role", "img");
    ring.setAttribute("aria-label",
      (label || "Attendance") + ": " + Math.round(clamped) + " percent");
  }

  /* ======================================================================
     THE STUDENT CONSTELLATION
     ----------------------------------------------------------------------
     A formation of glass avatars, built from the roster app.js already has.

     `people` is an array of plain objects:

       { id, name, state, note }

     and `state` is one of present, late, uncertain, absent, unknown - the
     five the rest of the interface uses. Nothing is inferred here: a person
     whose state app.js did not supply is drawn as unknown, with the word
     UNKNOWN under them, rather than being guessed at as absent.

     Each avatar is a real button with real text. The formation is a LIST OF
     BUTTONS that happen to be arranged in a circle, which is the only
     version of this idea that works with a keyboard and a screen reader.
     ====================================================================== */

  /*
     TWO VOCABULARIES, and they must never be mixed up.

     The first five are PRESENCE, reported by the live pipeline during a
     session: who is in the room right now.

     The last three are the REGISTER, which has nothing to do with a session:
     whether the system holds enough face samples to recognise somebody at
     all. A student can be ENROLLED and absent, or on the register with no
     face and sitting in the front row.

     They are kept in one pair of maps because one function draws both and the
     accessibility contract - symbol, word, colour, in that order of
     importance - is identical. They are never shown together: presence is the
     overview stage and the assembly hall, the register is the Students page.
  */
  const STATE_SYMBOL = {
    present: "●",      /* a filled circle */
    late: "◐",         /* half filled */
    uncertain: "◐",
    absent: "○",       /* an empty circle */
    unknown: "◌",      /* a dotted circle */

    enrolled: "◉",     /* a circle with a core: a face the system holds */
    pending: "○",      /* on the register, nothing to recognise */
    inactive: "◌"      /* not expected at all */
  };

  const STATE_WORD = {
    present: "PRESENT",
    late: "LATE",
    uncertain: "UNCERTAIN",
    absent: "ABSENT",
    unknown: "UNKNOWN",

    /* The same two words the register's own pills use in the table below the
       formation, so one student is never described two ways on one page. */
    enrolled: "ENROLLED",
    pending: "NOT ENROLLED",
    inactive: "INACTIVE"
  };

  /* Who was present last time this was drawn, so a newly detected student can
     be given the one-shot detection pulse. Keyed by student id, and it holds
     nothing but a state word - no name, no biometric, nothing that outlives
     the page. */
  const SEEN = Object.create(null);

  function orbState(raw) {
    const state = String(raw || "").toLowerCase();
    return STATE_SYMBOL[state] ? state : "unknown";
  }

  function renderConstellation(container, people, options) {
    if (!container) return;

    const settings = options || {};
    container.textContent = "";

    if (!people || !people.length) {
      /* Nothing to draw, and it SAYS SO. An empty formation looks exactly
         like a class with nobody in it, which is the one thing it must never
         be mistaken for - so the caller supplies the reason, because "no
         session is running" and "the session has nobody in it yet" are
         different facts and only the caller knows which. */
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = settings.empty ||
        "No students to show for this view yet.";
      container.appendChild(empty);
      return;
    }

    /* How many across. Narrow screens wrap anyway - this only decides which
       row an avatar is assigned to, which is what sets its depth. */
    const across = settings.columns || Math.max(4, Math.min(9,
      Math.round(Math.sqrt(people.length * 1.6))));

    people.forEach((person, index) => {
      const state = orbState(person.state);
      const row = Math.floor(index / across);

      const figure = document.createElement("button");
      figure.type = "button";
      figure.className = "orb-figure";
      figure.dataset.state = state;
      figure.style.setProperty("--row", String(row));
      if (person.id) figure.dataset.studentId = person.id;

      const bead = document.createElement("span");
      bead.className = "orb-bead";
      bead.setAttribute("aria-hidden", "true");
      bead.textContent = STATE_SYMBOL[state];

      const name = document.createElement("span");
      name.className = "orb-name";
      name.textContent = person.name || "Unnamed";

      const word = document.createElement("span");
      word.className = "orb-state";
      word.textContent = STATE_WORD[state];

      figure.appendChild(bead);
      figure.appendChild(name);
      figure.appendChild(word);

      /* The accessible name says all three things, because the symbol is
         hidden and the two spans would otherwise be read as a fragment. */
      figure.setAttribute("aria-label",
        (person.name || "Unnamed") + ": " + STATE_WORD[state] +
        (person.note ? ". " + person.note : ""));

      /* The detection beat: only when this person was not present a moment
         ago and is now. One animation, once. */
      const before = person.id ? SEEN[person.id] : null;
      if (person.id) SEEN[person.id] = state;
      if (before && before !== state && state === "present") {
        figure.dataset.justSeen = "true";
        window.setTimeout(() => { delete figure.dataset.justSeen; }, 1000);
      }

      /* Clicking a person opens whatever the caller says it opens - the
         student's profile on the register, nothing at all on the overview
         stage. The formation itself holds no opinion about what a click
         means, which is what lets one function serve both. */
      if (typeof settings.onPick === "function" && person.id) {
        figure.addEventListener("click", () => settings.onPick(person.id));
      } else {
        /* A button that does nothing is a button that should not be in the
           tab order promising something. */
        figure.tabIndex = -1;
      }

      container.appendChild(figure);
    });
  }

  /* ======================================================================
     THE GLASS CUBE CHART
     ----------------------------------------------------------------------
     A bar chart standing on a floor inside a transparent box.

     It takes the same shape of data the existing barChart() in app.js takes
     - a list of rows with a label and a count - and it keeps the same two
     accessibility properties that function established: role="img" on the
     chart, and an aria-label carrying the whole series as text. A chart that
     only exists as geometry is a chart half the room cannot read.

     Every bar is a button so it is reachable by keyboard, which is what the
     "hovering raises the data point" interaction needs in order to be
     available to somebody who is tabbing rather than pointing.
     ====================================================================== */

  function renderCube(container, rows, options) {
    if (!container) return;

    const settings = options || {};
    container.textContent = "";

    if (!rows || !rows.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = settings.empty || "No data for this range yet.";
      container.appendChild(empty);
      return;
    }

    const highest = rows.reduce(
      (most, row) => Math.max(most, Number(row.value) || 0), 0);

    const bars = document.createElement("div");
    bars.className = "cube-bars";
    bars.setAttribute("role", "img");
    bars.setAttribute("aria-label",
      (settings.label || "Chart") + ". Values — " +
      rows.map((row) => row.label + ": " + row.value +
                        (settings.unit || "")).join(", "));

    for (const row of rows) {
      const value = Number(row.value) || 0;
      const share = highest > 0 ? (value / highest) * 100 : 0;

      const bar = document.createElement("button");
      bar.type = "button";
      bar.className = "cube-bar";
      /* The bar is already described by the chart's own label, and a bar
         announced individually would read the series twice. */
      bar.setAttribute("aria-hidden", "true");
      bar.tabIndex = -1;

      const figure = document.createElement("span");
      figure.className = "cube-bar-value";
      figure.textContent = String(row.display != null ? row.display : value);

      const fill = document.createElement("span");
      fill.className = "cube-bar-fill";
      fill.style.setProperty("--h", share.toFixed(1) + "%");

      const key = document.createElement("span");
      key.className = "cube-bar-key";
      key.textContent = row.label;

      bar.appendChild(figure);
      bar.appendChild(fill);
      bar.appendChild(key);
      bars.appendChild(bar);
    }

    container.appendChild(bars);
  }

  /* ======================================================================
     THE SPATIAL TIMELINE
     ----------------------------------------------------------------------
     A session as a lit path: glass nodes on a spine, in order, with the one
     that is happening now pulsing.

     The horizontal scroll container IS the drag: a native overflow scroller
     is draggable with a trackpad, flickable with a finger, scrollable with a
     wheel, and reachable with the arrow keys. A hand-written drag handler
     would be a worse version of all four.

     `events` is an array of { label, time, done, current }. Nothing in here
     invents a step: the stages drawn are the stages app.js was told about.
     ====================================================================== */

  function renderSpine(container, events) {
    if (!container) return;

    container.textContent = "";

    if (!events || !events.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = "Nothing has happened in this session yet.";
      container.appendChild(empty);
      return;
    }

    const spine = document.createElement("div");
    spine.className = "spine";
    spine.setAttribute("role", "list");
    spine.tabIndex = 0;
    spine.setAttribute("aria-label", "Session timeline, scrollable");

    for (const event of events) {
      const node = document.createElement("div");
      node.className = "spine-node";
      node.setAttribute("role", "listitem");
      node.dataset.done = event.done ? "true" : "false";
      if (event.current) node.dataset.current = "true";

      const bead = document.createElement("span");
      bead.className = "spine-bead";
      bead.setAttribute("aria-hidden", "true");

      const card = document.createElement("span");
      card.className = "spine-card";

      const label = document.createElement("span");
      label.className = "spine-label";
      label.textContent = event.label;

      const time = document.createElement("span");
      time.className = "spine-time";
      /* An event that has not happened has no time, and an em dash is how
         the rest of this interface says "not yet" rather than inventing a
         clock reading. */
      time.textContent = event.time || "—";

      card.appendChild(label);
      card.appendChild(time);
      node.appendChild(bead);
      node.appendChild(card);
      spine.appendChild(node);
    }

    container.appendChild(spine);
  }

  /* ======================================================================
     THE CAMERA TELEMETRY RAIL
     ----------------------------------------------------------------------
     LIVE, CAMERA, FPS, STUDENTS, AUTHENTICITY, around the frame.

     Every one of those is a measured value off /api/live/status, handed over
     by app.js. A cell with nothing behind it shows an em dash, because this
     interface does not report a frame rate it has not been told.
     ====================================================================== */

  function renderCameraRail(container, cells) {
    if (!container) return;

    container.textContent = "";
    if (!cells || !cells.length) return;

    for (const cell of cells) {
      const item = document.createElement("li");
      item.className = "holo-cam-cell";

      const key = document.createElement("span");
      key.className = "holo-cam-key";
      key.textContent = cell.key;

      const value = document.createElement("span");
      value.className = "holo-cam-val";
      value.textContent = (cell.value === null || cell.value === undefined ||
                           cell.value === "") ? "—" : String(cell.value);

      item.appendChild(key);
      item.appendChild(value);
      container.appendChild(item);
    }
  }

  /* Whether the recognition sweep is running, over any container. Driven off
     a data attribute rather than a class, so the attribute IS the pipeline
     state and the plane cannot be left sweeping over a stopped camera. */
  function setScanning(element, on) {
    if (!element) return;
    if (on) element.dataset.scanning = "true";
    else delete element.dataset.scanning;
  }

  /* ======================================================================
     MOVING BETWEEN VIEWS
     ----------------------------------------------------------------------
     The view being left falls back into the room; the one being opened comes
     forward out of the glass.

     Both are one-shot CSS animations, which means the browser runs them and
     cleans up after itself. The ghost removes itself on animationend and
     also on a timer, because an animationend that never fires - a hidden
     tab, a thrown-away compositor layer - must not leave a pane of glass
     over the interface.

     Short on purpose: 320ms and 420ms. A tool somebody navigates two hundred
     times a day cannot charge them a second each time for a camera move.
     ====================================================================== */

  function transitionView(name) {
    if (reducedMotion()) return;

    const view = document.getElementById("view-" + name);
    if (!view || view.hidden) return;

    /* The pane of glass the old view recedes behind. */
    const ghost = document.createElement("div");
    ghost.className = "view-ghost";
    ghost.setAttribute("aria-hidden", "true");
    document.body.appendChild(ghost);

    const drop = () => { if (ghost.parentNode) ghost.remove(); };
    ghost.addEventListener("animationend", drop, { once: true });
    window.setTimeout(drop, 700);

    /* Restarting the animation needs the class off and the layout flushed,
       or navigating to the view you are already on does nothing visible. */
    view.classList.remove("view-arriving");
    void view.offsetWidth;
    view.classList.add("view-arriving");
    view.addEventListener("animationend",
      () => view.classList.remove("view-arriving"), { once: true });
  }

  /* ======================================================================
     THE THRESHOLD
     ----------------------------------------------------------------------
     Showing and crossing the entry scene.

     Two things about this are deliberate and worth stating, because the
     usual version of this screen gets both wrong:

     THE DASHBOARD LOADS BEHIND IT. show() does not block and does not
     delay anything: app.js carries on fetching, so by the time somebody has
     read the three lines beside the sphere the console behind is already
     populated. The scene costs no loading time, it occupies some.

     IT CAN BE CROSSED INSTANTLY. The button takes focus the moment the
     scene appears, so Enter goes straight through; Escape does the same; and
     a click anywhere on the scene that is not the copy does too. Nobody with
     a class waiting is made to watch an animation.

     It is shown once per page load rather than once per session, because the
     one case where it would be unwelcome - a teacher reloading mid-lesson -
     is also the case where one keypress is no burden, and remembering a
     dismissal in storage means the product's own front door silently
     disappears for everybody who ever clicked past it.
     ====================================================================== */

  const THRESHOLD = { open: false, after: null };

  function crossThreshold() {
    if (!THRESHOLD.open) return;
    THRESHOLD.open = false;

    const screen = document.getElementById("entry-screen");
    const after = THRESHOLD.after;
    THRESHOLD.after = null;

    const finish = () => {
      if (screen) {
        screen.hidden = true;
        screen.classList.remove("entry-leaving");
      }
      stopEntryScene();
      document.body.classList.remove("entering");
      /* Focus has to go somewhere a keyboard user can work from, and the
         main region is where the console starts. Leaving it on a button
         inside a hidden element loses the focus ring entirely. */
      const main = document.getElementById("main");
      if (main && !main.hidden) main.focus();
      if (typeof after === "function") after();
    };

    if (!screen || reducedMotion()) { finish(); return; }

    screen.classList.add("entry-leaving");
    screen.addEventListener("animationend", finish, { once: true });
    /* And a timer, because an animationend in a backgrounded tab may never
       arrive and the console must not stay behind a pane of glass. */
    window.setTimeout(finish, 800);
  }

  function showThreshold(after) {
    const screen = document.getElementById("entry-screen");
    if (!screen) { if (typeof after === "function") after(); return; }

    THRESHOLD.open = true;
    THRESHOLD.after = after || null;

    screen.hidden = false;
    screen.classList.remove("entry-leaving");
    document.body.classList.add("entering");

    startEntryScene();

    const button = document.getElementById("entry-enter");
    if (button) {
      if (!button.dataset.bound) {
        button.dataset.bound = "true";
        button.addEventListener("click", crossThreshold);
      }
      button.focus();
    }

    if (!screen.dataset.bound) {
      screen.dataset.bound = "true";

      /* Clicking the scene itself crosses too - the sphere is the door. The
         copy column is excluded so that selecting a word is not a
         navigation. */
      screen.addEventListener("click", (event) => {
        if (event.target.closest(".entry-copy")) return;
        crossThreshold();
      });

      screen.addEventListener("keydown", (event) => {
        if (event.key === "Escape" || event.key === "Enter") {
          event.preventDefault();
          crossThreshold();
        }
      });
    }
  }

  /* ======================================================================
     PER-VIEW LOOPS
     ----------------------------------------------------------------------
     Which canvas is allowed to be running depends entirely on which view is
     on screen, and that is the rule the whole file is built around: a
     dashboard left open on the reports page must not be painting an AI core
     nobody can see.

     Called by switchView() in app.js, which is the one place in the product
     that knows what is on screen.
     ====================================================================== */

  function enterView(name) {
    transitionView(name);

    /* The core lives on the overview stage and in the assembly hall. */
    if (name === "overview" || name === "assembly") startCore();
    else stopCore();

    /* The background keeps running across every view - it is the room, and
       the room does not stop existing when you turn around - but the resize
       that a view change can cause needs picking up. */
    if (DEEP.canvas && paintable(DEEP.canvas)) sizeCanvas(DEEP.canvas);
  }

  /*
     Redraw the still frames after a theme change.

     Only matters under prefers-reduced-motion, where each scene is a single
     frame painted once: a running loop re-reads the palette sixty times a
     second and picks a theme change up on its own. Doing nothing when a
     canvas is not on screen is the point - this must not resurrect a loop
     that stopped itself.
  */
  function repaint() {
    if (!reducedMotion()) return;

    if (DEEP.canvas && paintable(DEEP.canvas) && !DEEP.canvas.hidden) {
      sizeCanvas(DEEP.canvas);
      drawDeep();
    }
    const screen = document.getElementById("entry-screen");
    if (ENTRY.canvas && screen && !screen.hidden && paintable(ENTRY.canvas)) {
      sizeCanvas(ENTRY.canvas);
      drawEntryScene();
    }
    if (CORE.canvas && paintable(CORE.canvas)) {
      sizeCanvas(CORE.canvas);
      drawCore();
    }
  }

  /* ======================================================================
     START-UP
     ====================================================================== */

  let started = false;

  function init() {
    if (started) return;
    started = true;

    bindPointer();
    installDeep();

    /* A resize changes three things at once: whether the arc fits, how big
       every canvas buffer should be, and whether the background loop should
       be running at all. One handler, debounced by a frame, because a drag
       of a window edge fires this continuously. */
    let pending = null;
    window.addEventListener("resize", () => {
      if (pending) return;
      pending = window.requestAnimationFrame(() => {
        pending = null;
        startDeep();
        if (ENTRY.canvas && paintable(ENTRY.canvas)) sizeCanvas(ENTRY.canvas);
        if (CORE.canvas && paintable(CORE.canvas)) sizeCanvas(CORE.canvas);
        repaint();
      });
    });

    /* Coming back to the tab. rAF throttles in a hidden tab and the loops
       stop themselves rather than crawl, so returning has to restart them. */
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      startDeep();
      const screen = document.getElementById("entry-screen");
      if (screen && !screen.hidden) startEntryScene();
      if (CORE.canvas && paintable(CORE.canvas)) startCore();
    });

    /* Somebody turning reduced motion on while the page is open. Stopping
       immediately is the only correct response - a preference that takes
       effect on the next reload is a preference that was ignored. */
    if (window.matchMedia) {
      const query = window.matchMedia("(prefers-reduced-motion: reduce)");
      const react = () => {
        if (reducedMotion()) {
          stopDeep(); stopEntryScene(); stopCore();
          repaint();
        } else {
          startDeep();
          const screen = document.getElementById("entry-screen");
          if (screen && !screen.hidden) startEntryScene();
        }
      };
      if (query.addEventListener) query.addEventListener("change", react);
      else if (query.addListener) query.addListener(react);
    }
  }

  /* ======================================================================
     THE PUBLIC SURFACE
     ----------------------------------------------------------------------
     Everything app.js is allowed to call, and nothing else.

     Deliberately small, and deliberately all one-way: app.js hands this
     object data and tells it what is on screen. Nothing here reaches back
     into app.js, calls an endpoint, or holds a copy of anything the backend
     said. That is what makes the two files separable, and it is why every
     call site in app.js can be a single guarded line.
     ====================================================================== */

  window.Spatial = {
    init: init,

    /* The threshold */
    showEntry: showThreshold,
    crossEntry: crossThreshold,

    /* Navigation and views */
    view: enterView,
    repaint: repaint,

    /* The central object */
    core: setCoreState,

    /* Figures and formations */
    ring: setRing,
    constellation: renderConstellation,
    cube: renderCube,
    spine: renderSpine,
    cameraRail: renderCameraRail,
    scanning: setScanning,

    /* Shared vocabulary, so assembly.js and app.js cannot drift from this
       file about what a state is called or which symbol it takes. */
    stateWord: STATE_WORD,
    stateSymbol: STATE_SYMBOL,
    normaliseState: orbState,

    /* Capability questions, asked by assembly.js rather than re-derived. */
    reducedMotion: reducedMotion,
    narrow: narrowViewport
  };
})();
