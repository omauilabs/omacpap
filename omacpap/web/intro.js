/* OmaCPAP entrance: the usage calendar becomes an LED sign that says hello,
   then one wave rolls across and hands the squares back to your data.
   Runs once per app open. Skipped for reduced-motion, the Settings toggle,
   or any click/keypress (which jumps straight to the calendar). */
(() => {
  "use strict";

  // 7-row dot-matrix font (mostly 5 wide; I, J, E, S and space are narrower so
  // "HI JAMES" plus the hand fits a 53-week calendar). Rows top→bottom; "#" = lit.
  const FONT = {
    A: [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    B: ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    C: [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    D: ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    E: ["####", "#...", "#...", "###.", "#...", "#...", "####"],
    F: ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    G: [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    H: ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    I: ["###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"],
    J: ["..##", "...#", "...#", "...#", "...#", "#..#", ".##."],
    K: ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    L: ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    M: ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    N: ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    O: [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    P: ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    Q: [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    R: ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    S: [".###", "#...", "#...", ".##.", "...#", "...#", "###."],
    T: ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    U: ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    V: ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    W: ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
    X: ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    Y: ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    Z: ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    " ": ["..", "..", "..", "..", "..", "..", ".."],
  };

  // Pixel-art hand, two poses a beat apart so it reads as waving.
  // The fingertips swing one column; the small marks show motion.
  const HAND = [
    [
      "..#.#.#..",
      "..#.#.#.#",
      "..#######",
      "#.#######",
      ".########",
      "..######.",
      "...####..",
    ],
    [
      "...#.#.#.",
      "..#.#.#.#",
      "..#######",
      "##.######",
      ".########",
      "..######.",
      "...####..",
    ],
  ];

  let noSign = false;
  const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function glyphs(text) {
    // lay the message out as a list of [col, row, kind] pixels, width returned
    const px = [];
    let x = 0;
    for (const ch of text) {
      const g = FONT[ch] || FONT[" "];
      g.forEach((line, r) => [...line].forEach((c, i) => c === "#" && px.push([x + i, r])));
      x += g[0].length + 1;
    }
    return { px, width: Math.max(0, x - 1) };
  }

  function handPixels(frame, x0) {
    const px = [];
    HAND[frame].forEach((line, r) => [...line].forEach((c, i) => c === "#" && px.push([x0 + i, r])));
    return px;
  }

  /** Columns of the calendar that are fully on screen and have all 7 days. */
  function usableColumns(strip) {
    const wrapRect = strip.parentElement.getBoundingClientRect();
    const byCol = new Map();
    for (const cell of strip.querySelectorAll(".cell:not(.pad)")) {
      const col = Number(cell.style.getPropertyValue("--col"));
      const row = Number(cell.style.getPropertyValue("--row"));
      const r = cell.getBoundingClientRect();
      if (r.left < wrapRect.left || r.right > wrapRect.right) continue;
      if (!byCol.has(col)) byCol.set(col, new Map());
      byCol.get(col).set(row, cell);
    }
    return [...byCol.entries()].filter(([, rows]) => rows.size === 7).sort((a, b) => a[0] - b[0]);
  }

  function run(strip, name, { onDone } = {}) {
    const cols = usableColumns(strip);
    const handW = HAND[0][0].length;
    const clean = (name || "").toUpperCase().replace(/[^A-Z ]/g, "").trim().split(/\s+/)[0] || "";
    // Try "HI JAMES" + hand, then "HI" + hand; bail on very narrow windows.
    let layout = null;
    for (const text of clean ? [`HI ${clean}`, "HI"] : ["HI"]) {
      const g = glyphs(text);
      const total = g.width + 2 + handW;
      if (total <= cols.length) { layout = { g, total }; break; }
    }
    if (!layout) { noSign = true; onDone?.(); return () => {}; }

    const start = Math.floor((cols.length - layout.total) / 2);
    const cellAt = (x, r) => cols[start + x]?.[1].get(r);
    const handX = layout.g.width + 2;
    const timers = [];
    const later = (ms, fn) => timers.push(setTimeout(fn, ms));
    let finished = false;

    // 1) power on: every square goes dark, the message scans in column by column
    strip.classList.add("display");
    for (const [x, r] of layout.g.px) {
      const c = cellAt(x, r);
      if (!c) continue;
      c.style.setProperty("--on", `${x * 22}ms`);
      c.classList.add("px");
    }
    const hand = (frame) => {
      strip.querySelectorAll(".px-hand").forEach((c) => c.classList.remove("px-hand"));
      for (const [x, r] of handPixels(frame, handX)) {
        const c = cellAt(x, r);
        if (c) { c.style.setProperty("--on", "0ms"); c.classList.add("px-hand"); }
      }
    };
    later(layout.g.width * 22 + 120, () => hand(0));

    // 2) the hand waves: five swings
    const waveStart = layout.g.width * 22 + 420;
    for (let i = 1; i <= 6; i++) later(waveStart + i * 260, () => hand(i % 2));

    // 3) one wave rolls across and washes the sign back into the calendar
    const washAt = waveStart + 7 * 260 + 250;
    later(washAt, () => later(wash(strip), finish));

    function finish() {
      if (finished) return;
      finished = true;
      timers.forEach(clearTimeout);
      clearWash(strip);
      onDone?.();
    }
    return finish;
  }

  /** The single entrance wave; also used alone when there's no room for the sign.
      Returns how long it takes (ms). Delays are relative to the first visible column
      so the crest starts at the left edge of what you can see. */
  function wash(strip) {
    const wrapR = strip.parentElement.getBoundingClientRect();
    const cells = [...strip.querySelectorAll(".cell:not(.pad)")];
    let first = Infinity, last = -Infinity;
    for (const c of cells) {
      const r = c.getBoundingClientRect();
      if (r.right >= wrapR.left && r.left <= wrapR.right) {
        const col = Number(c.style.getPropertyValue("--col"));
        first = Math.min(first, col); last = Math.max(last, col);
      }
    }
    if (!Number.isFinite(first)) { first = 0; last = 0; }
    const byW = new Map();
    for (const c of cells) {
      const w = Math.max(0, Number(c.style.getPropertyValue("--col")) - first);
      c.style.setProperty("--wcol", w);
      if (!byW.has(w)) byW.set(w, []);
      byW.get(w).push(c);
    }
    strip.classList.add("display", "washing", "wave-once");
    // each column flips back to its real colors right as the crest lifts it
    strip._washTimers = [...byW.entries()].map(([w, col]) => setTimeout(() => {
      for (const c of col) { c.classList.add("shown"); c.classList.remove("px", "px-hand"); }
    }, w * 30 + 330));
    return (last - first + 1) * 30 + 6 * 22 + 950;
  }

  function clearWash(strip) {
    (strip._washTimers || []).forEach(clearTimeout);
    strip._washTimers = null;
    strip.classList.remove("display", "washing", "wave-once");
    strip.querySelectorAll(".px, .px-hand, .shown").forEach((c) => c.classList.remove("px", "px-hand", "shown"));
  }

  window.OmaIntro = {
    /** Play once. Returns a function that skips to the end. */
    play(strip, name) {
      if (reduced() || localStorage.getItem("omacpap.wave") === "off") return () => {};
      let skip = () => {};
      const stop = () => { skip(); cleanup(); };
      const cleanup = () => {
        window.removeEventListener("pointerdown", stop, true);
        window.removeEventListener("keydown", stop, true);
      };
      window.addEventListener("pointerdown", stop, true);
      window.addEventListener("keydown", stop, true);
      skip = run(strip, name, {
        onDone: () => {
          cleanup();
          if (noSign) { // no room for the sign → still roll the entrance wave
            const ms = wash(strip);
            setTimeout(() => clearWash(strip), ms);
          }
        },
      });
      return stop;
    },
  };
})();
