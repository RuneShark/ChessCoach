/* ChessCoach front-end. Rules via vendored chess.js (global Chess). */
"use strict";
const GLYPH = { k:"♚", q:"♛", r:"♜", b:"♝", n:"♞", p:"♟" };
const $ = (s) => document.querySelector(s);
const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

const S = {
  game: new Chess(),
  mode: "analyze",
  orient: "white",
  sel: null,            // selected square
  targets: [],          // legal target squares from sel
  last: null,           // {from,to} last move
  line: [],             // analyze/play mainline — [san]; keeps moves ahead of the cursor
  ply: 0,               // cursor into S.line; S.game == position after S.ply half-moves
  baseFen: null,        // start position the line replays from (null = standard start)
  review: null,         // { moves:[san], ply, mistakes:{ply:m}, id, meta }
  explore: false,       // side-branch mode (review only): explore variations non-destructively
  branch: null,         // { atPly, baseFen, line:[san], cursor } — the one open variation
  drill: null,          // { fen, best, played, revealed }
  play: { skill: 8 },
  evalTimer: null,
  evalProb: 0.5,        // white's win probability (for the eval bar)
  chat: [],             // [{role, content}]
  coachSync: "silent",  // board→coach sync: "live" (auto-comment) | "silent" | "off"
  syncTimer: null,      // debounce for board-change sync
  drag: null,           // active drag-and-drop state
  suppressClick: false, // swallow the click that trails a drag
  arrows: [],           // [{from,to,color}] right-click arrows; coach arrows carry {hex,opacity}
  marks: {},            // sq -> color key, right-click tile highlights
  hlSquares: [],        // coach square highlights [{sq,color,opacity}] (weighted, SVG-rendered)
  annDrag: null,        // annotation (right-drag) in progress
  drillPlayedArrow: false,
};

/* annotation arrow colours (marks are coloured in CSS, per square shade) */
const ANN = {
  default: { arrow: "#e0873a" },   // planning arrow — orange
  shift:   { arrow: "#c0392b" },   // red
  ctrl:    { arrow: "#c9a227" },   // yellow
  alt:     { arrow: "#2f6fd0" },   // blue
  played:  { arrow: "#8b5cf6" },   // drill's played move — violet
  best:    { arrow: "#3aa757" },   // revealed best move — green
};
function annColor(ev) {
  return ev.shiftKey ? "shift" : (ev.ctrlKey || ev.metaKey) ? "ctrl" : ev.altKey ? "alt" : "default";
}
function sqXY(sq) {  // square centre in 0..8 board units, respecting orientation
  const f = "abcdefgh".indexOf(sq[0]), r = +sq[1];
  const col = S.orient === "white" ? f : 7 - f;
  const row = S.orient === "white" ? 8 - r : r - 1;
  return { x: col + 0.5, y: row + 0.5 };
}
function toggleMark(sq, color) {
  if (S.marks[sq] === color) delete S.marks[sq]; else S.marks[sq] = color;
}
function toggleArrow(from, to, color) {
  const i = S.arrows.findIndex((a) => a.from === from && a.to === to);
  if (i >= 0) { if (S.arrows[i].color === color) S.arrows.splice(i, 1); else S.arrows[i].color = color; }
  else S.arrows.push({ from, to, color });
}
function clearAnnotations() {
  S.arrows = []; S.marks = {}; S.hlSquares = []; S.drillPlayedArrow = false;
  renderAnnotations();
  document.querySelectorAll("#board .sq.marked").forEach((e) => {
    e.classList.remove("marked", "mark-default", "mark-shift", "mark-ctrl", "mark-alt");
  });
}
function arrowSVG(from, to, color, dashed, opacity) {
  const dx = to.x - from.x, dy = to.y - from.y, len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len, px = -uy, py = ux;
  const lw = 0.17, headLen = 0.36, headW = 0.27;                   // thicker
  const sx = from.x + ux * 0.32, sy = from.y + uy * 0.32;          // start at source edge
  const bx = to.x - ux * headLen, by = to.y - uy * headLen;        // arrowhead base
  const tx = to.x - ux * 0.06, ty = to.y - uy * 0.06;             // tip near target centre
  const dash = dashed ? ` stroke-dasharray="0.22 0.14"` : "";
  const op = (opacity == null ? 0.7 : opacity);                    // weighted arrows carry opacity
  return `<g opacity="${op}"><line x1="${sx}" y1="${sy}" x2="${bx}" y2="${by}" stroke="${color}" stroke-width="${lw}" stroke-linecap="round"${dash}/>` +
    `<polygon points="${tx},${ty} ${bx + px * headW},${by + py * headW} ${bx - px * headW},${by - py * headW}" fill="${color}"/></g>`;
}
// A coach arrow carries its own hex; a right-click arrow carries a colour key into ANN.
function annArrowHex(a) { return a.hex || (ANN[a.color] || ANN.default).arrow; }
function renderAnnotations() {
  const svg = $("#annotations"); if (!svg) return;
  // square highlights first (under the arrows), each with its own weighted opacity
  const marks = (S.hlSquares || []).map((h) => {
    const c = sqXY(h.sq);
    return `<rect x="${(c.x - 0.5).toFixed(3)}" y="${(c.y - 0.5).toFixed(3)}" width="1" height="1" rx="0.08" fill="${h.color}" opacity="${h.opacity}"/>`;
  }).join("");
  const arrows = S.arrows.map((a) =>
    arrowSVG(sqXY(a.from), sqXY(a.to), annArrowHex(a), a.color === "played", a.opacity)).join("");
  svg.innerHTML = marks + arrows;
}

/* ------------------------------------------------------------------ board */
function squares() {
  // return file/rank order for current orientation, rank 8..1 top for white
  const files = "abcdefgh".split("");
  const ranks = [8,7,6,5,4,3,2,1];
  const rr = S.orient === "white" ? ranks : ranks.slice().reverse();
  const ff = S.orient === "white" ? files : files.slice().reverse();
  const out = [];
  for (const r of rr) for (const f of ff) out.push(f + r);
  return out;
}
function renderBoard() {
  const b = $("#board");
  b.innerHTML = "";
  const pos = S.game.board(); // [rank8..rank1][fileA..H]
  const checkSq = S.game.in_check() ? kingSquare(S.game.turn()) : null;
  for (const sq of squares()) {
    const file = sq[0], rank = +sq[1];
    const fi = "abcdefgh".indexOf(file), ri = 8 - rank;
    const piece = pos[ri][fi];
    const dark = (fi + ri) % 2 === 1;
    const el = document.createElement("div");
    el.className = "sq " + (dark ? "d" : "l");
    el.dataset.sq = sq;
    if (S.sel === sq) el.classList.add("sel");
    if (S.targets.includes(sq)) el.classList.add(piece ? "cap" : "move");
    if (S.last && (S.last.from === sq || S.last.to === sq)) el.classList.add("last");
    if (checkSq === sq) el.classList.add(S.game.in_checkmate() ? "checkmate" : "check");
    if (S.marks[sq]) el.classList.add("marked", "mark-" + S.marks[sq]);
    if (piece) {
      const p = document.createElement("span");
      p.className = "pc " + (piece.color === "w" ? "w" : "b");
      p.textContent = GLYPH[piece.type];
      el.appendChild(p);
    }
    // coordinates on edges
    const lastFile = S.orient === "white" ? "h" : "a";
    const lastRank = S.orient === "white" ? 1 : 8;
    if (rank === lastRank) { const c = document.createElement("span"); c.className="co f"; c.textContent=file; el.appendChild(c); }
    if (file === (S.orient==="white"?"a":"h")) { const c=document.createElement("span"); c.className="co r"; c.textContent=rank; el.appendChild(c); }
    el.onclick = () => onSquare(sq);
    el.addEventListener("pointerdown", (ev) => onPointerDown(sq, el, ev));
    b.appendChild(el);
  }
  updateTurn();
  updateEvalBar();
  renderAnnotations();   // redraw arrows too, so they follow the orientation
}

/* --------------------------------------------------------- turn / helpers */
function pickTargets(sq) {
  return S.game.moves({ square: sq, verbose: true }).map((m) => m.to);
}
function canPickUp(sq) {
  if (S.mode === "review" && !S.explore) return false;   // review = navigation only…
  const turn = S.game.turn();                            // …unless exploring a side-branch
  if (S.mode === "play" && turn !== S.orient[0]) return false; // wait your turn
  const p = S.game.get(sq);
  return !!(p && p.color === turn);
}
function kingSquare(color) {
  const pos = S.game.board();
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const p = pos[r][f];
    if (p && p.type === "k" && p.color === color) return "abcdefgh"[f] + (8 - r);
  }
  return null;
}
function updateTurn() {
  const el = $("#turnbar"); if (!el) return;
  const txt = $("#turntext");
  if (S.game.game_over()) {
    el.className = "turnbar over";
    txt.textContent = S.game.in_checkmate()
      ? (S.game.turn() === "w" ? "Black" : "White") + " wins — checkmate"
      : S.game.in_stalemate() ? "Draw — stalemate" : "Game over — draw";
    return;
  }
  const white = S.game.turn() === "w";
  el.className = "turnbar " + (white ? "white" : "black");
  txt.textContent = (white ? "White" : "Black") + " to move" +
    (S.game.in_check() ? " — check!" : "");
}

function onSquare(sq) {
  if (S.suppressClick) { S.suppressClick = false; return; } // trailed a drag
  clearAnnotations();                      // a left click clears arrows/marks (chess.com)
  if (S.mode === "review") return;         // navigation only
  const turn = S.game.turn();
  if (S.mode === "play" && turn !== S.orient[0]) return; // wait your turn
  const piece = S.game.get(sq);
  if (S.sel && S.targets.includes(sq)) {
    doMove(S.sel, sq);
    S.sel = null; S.targets = [];
    return;
  }
  if (S.sel === sq) {                       // click the selected piece again → deselect
    S.sel = null; S.targets = [];
  } else if (piece && piece.color === turn) {
    S.sel = sq;
    S.targets = pickTargets(sq);
  } else {
    S.sel = null; S.targets = [];
  }
  renderBoard();
}

/* ------------------------------------------------------ drag and drop */
function onPointerDown(sq, el, ev) {
  if (ev.button === 2) {                     // right button → start an annotation
    ev.preventDefault();
    S.annDrag = { from: sq, color: annColor(ev) };
    return;
  }
  if (ev.button !== undefined && ev.button !== 0) return; // left button / touch only
  clearAnnotations();                        // left interaction clears annotations
  if (!canPickUp(sq)) return;               // empty/opponent square → let click handle it
  ev.preventDefault();
  const targets = pickTargets(sq);
  // Highlight directly (no full re-render, so the lifted piece + ghost survive the drag).
  const bd = $("#board");
  el.classList.add("sel");
  targets.forEach((t) => {
    const te = bd.querySelector(`.sq[data-sq="${t}"]`);
    if (te) te.classList.add(S.game.get(t) ? "cap" : "move");
  });
  const pcEl = el.querySelector(".pc");
  if (pcEl) pcEl.classList.add("lifted");
  const piece = S.game.get(sq);
  const ghost = document.createElement("div");
  ghost.className = "dragghost " + (piece.color === "w" ? "w" : "b");
  ghost.textContent = GLYPH[piece.type];
  const cs = getComputedStyle(el);
  ghost.style.width = ghost.style.height = cs.width;
  ghost.style.fontSize = pcEl ? getComputedStyle(pcEl).fontSize : cs.fontSize;
  ghost.style.display = "none";
  document.body.appendChild(ghost);
  S.drag = { from: sq, targets, ghost, moved: false,
             startX: ev.clientX, startY: ev.clientY, hoverEl: null };
  moveGhost(ev.clientX, ev.clientY);
}
function moveGhost(x, y) {
  const g = S.drag && S.drag.ghost; if (!g) return;
  const half = (g.offsetWidth || 60) / 2;
  g.style.left = (x - half) + "px";
  g.style.top = (y - half) + "px";
}
function squareFromPoint(x, y) {
  const el = document.elementFromPoint(x, y);
  return el && el.closest ? el.closest(".sq") : null;
}
function onPointerMove(ev) {
  const d = S.drag; if (!d) return;
  if (!d.moved) {
    if (Math.hypot(ev.clientX - d.startX, ev.clientY - d.startY) < 5) return;
    d.moved = true; d.ghost.style.display = "flex"; // it's a real drag now
  }
  moveGhost(ev.clientX, ev.clientY);
  const sqEl = squareFromPoint(ev.clientX, ev.clientY);
  if (d.hoverEl && d.hoverEl !== sqEl) d.hoverEl.classList.remove("dragover");
  if (sqEl && d.targets.includes(sqEl.dataset.sq)) {
    sqEl.classList.add("dragover"); d.hoverEl = sqEl;
  } else { d.hoverEl = null; }
}
function onPointerUp(ev) {
  if (S.annDrag) {                            // finish a right-click annotation
    const { from, color } = S.annDrag; S.annDrag = null;
    if (S.drillPlayedArrow) {                 // your own annotation clears the drill hint
      S.arrows = S.arrows.filter((a) => a.color !== "played"); S.drillPlayedArrow = false;
    }
    const sqEl = squareFromPoint(ev.clientX, ev.clientY);
    const to = sqEl && sqEl.dataset.sq;
    if (to) { if (to === from) toggleMark(from, color); else toggleArrow(from, to, color); }
    renderAnnotations(); renderBoard();
    return;
  }
  const d = S.drag; if (!d) return;
  S.drag = null;
  d.ghost.remove();
  if (d.hoverEl) d.hoverEl.classList.remove("dragover");
  S.suppressClick = true;                   // swallow the click this pointer sequence emits
  if (d.moved) {
    const sqEl = squareFromPoint(ev.clientX, ev.clientY);
    const to = sqEl && sqEl.dataset.sq;
    S.sel = null; S.targets = [];
    if (to && d.targets.includes(to)) doMove(d.from, to);
    else renderBoard();                     // dropped off a legal square → snap back
  } else {                                  // no drag → treat as a click-select (with toggle)
    if (S.sel === d.from) { S.sel = null; S.targets = []; }
    else { S.sel = d.from; S.targets = d.targets; }
    renderBoard();
  }
}

function doMove(from, to) {
  if (S.mode === "review" && S.explore) { branchMove(from, to); return; }  // side-branch
  const mv = S.game.move({ from, to, promotion: "q" });
  if (!mv) return;
  S.sel = null; S.targets = [];      // clear before render so no stale highlights
  S.last = { from, to };
  // a move from a past position branches off — the mainline follows the live game
  S.line = S.game.history();
  S.ply = S.line.length;
  renderBoard();
  afterPositionChange();
  if (S.mode === "drill") gradeDrill(mv);
  if (S.mode === "play" && !S.game.game_over()) setTimeout(engineReply, 250);
}

function afterPositionChange() {
  renderMoveList();
  scheduleEval();
  saveBoard();
  notifyCoachBoard();
}

/* Move the analyze/play cursor to a ply by replaying S.line — never destroys the
   moves ahead of the cursor, so ◀/▶ are non-destructive (unlike S.game.undo()). */
function rebuildToPly(ply) {
  ply = Math.max(0, Math.min(S.line.length, ply));
  const g = new Chess();
  if (S.baseFen) g.load(S.baseFen);
  for (let i = 0; i < ply; i++) g.move(S.line[i], { sloppy: true });
  S.game = g; S.ply = ply;
  const h = g.history({ verbose: true });
  S.last = h.length ? { from: h[h.length - 1].from, to: h[h.length - 1].to } : null;
  S.sel = null; S.targets = [];
  renderBoard(); renderMoveList(); scheduleEval(); saveBoard();
  notifyCoachBoard();
}

/* Persist the board so a reload keeps the loaded game (mirrors cc_chat). */
function saveBoard() {
  try {
    localStorage.setItem("cc_board", JSON.stringify({
      mode: S.mode, line: S.line, ply: S.ply, baseFen: S.baseFen,
      orient: S.orient, review: S.review,
    }));
  } catch (e) {}
}

/* ↺ Reset — the explicit "new empty board" action (Analyze no longer wipes on entry). */
function resetBoard() {
  S.game = new Chess(); S.line = []; S.ply = 0; S.baseFen = null;
  S.review = null; S.last = null; S.sel = null; S.targets = [];
  clearAnnotations();
  if (S.mode !== "analyze" && S.mode !== "play") { setMode("analyze"); return; }
  renderBoard(); renderMoveList(); scheduleEval(); saveBoard();
}

/* ------------------------------------------------------------------ eval */
function scheduleEval() {
  clearTimeout(S.evalTimer);
  S.evalTimer = setTimeout(runEval, 220);
}
// The eval bar always shows the side at the BOTTOM of the board as the fill, so
// it flips with orientation.
function updateEvalBar() {
  const bar = document.querySelector(".evalbar"); if (!bar) return;
  bar.classList.toggle("flipped", S.orient === "black");
  const p = S.orient === "white" ? S.evalProb : (1 - S.evalProb);
  $("#evalfill").style.height = (p * 100).toFixed(1) + "%";
}
async function runEval() {
  // during an unsolved drill the engine mustn't assist (no eval, no best move)
  if (S.mode === "drill" && S.drill && !S.drill.revealed && !S.drill.solved) {
    S.evalProb = 0.5; updateEvalBar();
    $("#evaltext").innerHTML = "eval hidden — it's a drill";
    return;
  }
  const fen = S.game.fen();
  try {
    const r = await fetch("/api/eval", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen, movetime: 0.25 }) });
    const d = await r.json();
    if (d.error) return;
    let txt, prob;
    if (d.over) { txt = "game over " + d.over; prob = 0.5; }
    else if (d.mate !== null && d.mate !== undefined) {
      txt = "M" + Math.abs(d.mate); prob = d.mate > 0 ? 0.98 : 0.02;
      txt = (d.mate > 0 ? "+" : "-") + txt;
    } else {
      const cp = d.cp || 0;
      prob = 1 / (1 + Math.exp(-cp / 350));
      txt = (cp >= 0 ? "+" : "") + (cp / 100).toFixed(2);
    }
    S.evalProb = prob; updateEvalBar();
    $("#evaltext").innerHTML = `eval <b>${txt}</b>` + (d.best ? ` · best ${d.best}` : "");
  } catch (e) { /* engine busy */ }
}

/* --------------------------------------------------------------- movelist */
function renderMoveList() {
  const el = $("#movelist");
  let sans, curPly, blunders = {};
  if (S.mode === "review" && S.review) {
    sans = S.review.moves; curPly = S.review.ply; blunders = S.review.mistakes;
  } else {
    sans = S.line; curPly = S.ply;
  }
  const b = (S.mode === "review") ? S.branch : null;
  const onMain = !b || b.cursor === 0;            // is the cursor on the mainline right now?
  // chess.com-style rows: move number | white | black
  const cell = (i) => {
    if (i >= sans.length) return `<span class="mv empty"></span>`;
    const cls = ["mv"];
    if (i + 1 === curPly && onMain) cls.push("cur");
    if (b && i + 1 === b.atPly) cls.push("fork");   // where the open variation branches
    if (blunders[i + 1]) cls.push("blunder");
    return `<span class="${cls.join(" ")}" data-ply="${i + 1}">${sans[i]}</span>`;
  };
  // The open variation, rendered as an inset line right after its fork move.
  const branchBlock = () => {
    if (!b) return "";
    const cells = b.line.map((san, i) => {
      const absPly = b.atPly + i + 1;               // 1-based ply of this branch move
      const no = Math.ceil(absPly / 2);
      const prefix = (absPly % 2 === 1) ? `${no}.` : (i === 0 ? `${no}…` : "");
      const cls = ["bmv"]; if (i + 1 === b.cursor) cls.push("cur");
      return `<span class="bwrap">${prefix}<span class="${cls.join(" ")}" data-bc="${i + 1}">${san}</span></span>`;
    }).join(" ");
    return `<div class="branchline"><span class="branch-label">⤷</span> ${cells}</div>`;
  };
  const forkRow = b ? (b.atPly > 0 ? Math.floor((b.atPly - 1) / 2) : -1) : null;
  let html = "";
  for (let i = 0, row = 0; i < sans.length; i += 2, row++) {
    html += `<div class="mv-row"><span class="mv-no">${i / 2 + 1}.</span>` +
      cell(i) + cell(i + 1) + `</div>`;
    if (b && row === forkRow) html += branchBlock();
  }
  if (b && forkRow === -1) html = branchBlock() + html;  // branch off the very start
  el.innerHTML = html || '<div class="hint">no moves yet</div>';
  el.querySelectorAll(".mv[data-ply]").forEach((m) =>
    m.onclick = () => {
      const ply = +m.dataset.ply;
      if (S.mode === "review") { S.branch = null; gotoPly(ply); renderLines(); }
      else rebuildToPly(ply);
    });
  el.querySelectorAll(".bmv[data-bc]").forEach((m) =>
    m.onclick = () => branchGoto(+m.dataset.bc));
  const cur = el.querySelector(".cur"); if (cur) cur.scrollIntoView({ block: "nearest" });
}

/* ---------------------------------------------------------------- review */
function gotoPly(ply) {
  if (!S.review) return;
  const g = new Chess();
  for (let i = 0; i < ply; i++) g.move(S.review.moves[i]);
  S.game = g; S.review.ply = ply;
  S.baseFen = null;                   // review replays from the standard start
  const h = g.history({ verbose: true });
  S.last = h.length ? { from: h[h.length-1].from, to: h[h.length-1].to } : null;
  S.sel = null; S.targets = [];
  renderBoard(); renderMoveList(); scheduleEval(); saveBoard();
  const m = S.review.mistakes[ply];
  if (m) $("#evaltext").innerHTML =
    `<b style="color:var(--bad)">blunder</b> ${m.win_before}%→${m.win_after}% · best was <b>${m.best}</b>`;
  notifyCoachBoard();
}

/* ------------------------------------------------------- side-branch explorer
   Explore mode (review only): play variations WITHOUT touching the game's mainline.
   One branch at a time, no nesting — a move from a past branch ply replaces the moves
   ahead. ◀/◀ are non-destructive within the branch; stepping back onto the mainline
   self-closes it. See renderLines() for the clickable top-3. */
function exploreEnabled() {
  return S.mode === "review" && S.review && S.review.moves.length > 0;
}
function toggleExplore() {
  if (!exploreEnabled()) return;
  S.explore = !S.explore;
  if (!S.explore) S.branch = null;          // leaving explore drops any open variation
  updateExploreUI();
  renderBoard(); renderMoveList(); renderLines();
}
function updateExploreUI() {
  const btn = $("#explore-toggle");
  if (btn) {
    btn.disabled = !exploreEnabled();
    btn.classList.toggle("on", !!S.explore && exploreEnabled());
    btn.textContent = S.explore && exploreEnabled() ? "🔀 Exploring" : "🔀 Explore";
  }
  const lines = $("#lines");
  if (lines) lines.style.display = (S.explore && exploreEnabled()) ? "block" : "none";
}
// Play a move into the branch: open one at the current ply, or extend/replace-forward.
function branchMove(from, to, promo) {
  const beforeFen = S.game.fen();
  const mv = S.game.move({ from, to, promotion: promo || "q" });
  if (!mv) return;
  if (!S.branch) {
    S.branch = { atPly: S.review.ply, baseFen: beforeFen, line: [mv.san], cursor: 1 };
  } else {
    S.branch.line = S.branch.line.slice(0, S.branch.cursor);   // no nesting: replace forward
    S.branch.line.push(mv.san);
    S.branch.cursor = S.branch.line.length;
  }
  S.last = { from, to }; S.sel = null; S.targets = [];
  renderBoard(); renderMoveList(); scheduleEval(); renderLines();
}
function playExploreUci(uci) {   // a click on one of the top-3 lines
  branchMove(uci.slice(0, 2), uci.slice(2, 4), uci.slice(4) || "q");
}
// ◀ — inside a branch, walk back up it (kept for ▶); stepping back off the fork onto the
// mainline self-closes the branch and continues back through the real game.
function navPrev() {
  if (S.branch) {
    if (S.branch.cursor > 0) { branchGoto(S.branch.cursor - 1); return; }
    S.branch = null; renderLines();                 // left the whole branch → self-close
    gotoPly(Math.max(0, S.review.ply - 1)); return;
  }
  if (S.mode === "review") gotoPly(Math.max(0, S.review.ply - 1));
  else rebuildToPly(S.ply - 1);
}
// ▶ — inside a branch, walk forward down the kept path (re-enters it from the fork).
function navNext() {
  if (S.branch) {
    if (S.branch.cursor < S.branch.line.length) branchGoto(S.branch.cursor + 1);
    return;                                          // at the branch tip → nothing ahead
  }
  if (S.mode === "review") gotoPly(Math.min(S.review.moves.length, S.review.ply + 1));
  else rebuildToPly(S.ply + 1);
}
// Move the cursor within the branch (non-destructive: the line is kept for ◀/▶).
function branchGoto(cursor) {
  const b = S.branch; if (!b) return;
  b.cursor = Math.max(0, Math.min(b.line.length, cursor));
  const g = new Chess(); g.load(b.baseFen);
  for (let i = 0; i < b.cursor; i++) g.move(b.line[i], { sloppy: true });
  S.game = g;
  const h = g.history({ verbose: true });
  S.last = h.length ? { from: h[h.length - 1].from, to: h[h.length - 1].to } : null;
  S.sel = null; S.targets = [];
  renderBoard(); renderMoveList(); scheduleEval(); renderLines();
}
// Fetch + render the engine's top-3 candidate moves (with White-POV evals) for the
// current position; each is clickable to play it into the branch.
async function renderLines() {
  const el = $("#lines"); if (!el) return;
  if (!(S.explore && exploreEnabled())) { el.style.display = "none"; return; }
  el.style.display = "block";
  const fen = S.game.fen();
  if (!el.dataset.fen) el.innerHTML = '<div class="hint">analyzing…</div>';
  el.dataset.fen = fen;
  try {
    const r = await (await fetch("/api/lines", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen, n: 3, depth: 14 }) })).json();
    if (el.dataset.fen !== fen) return;               // position moved on — stale response
    if (r.over || !r.lines || !r.lines.length) {
      el.innerHTML = `<div class="hint">${r.over ? "game over" : "no moves"}</div>`; return;
    }
    el.innerHTML = r.lines.map((l) => {
      const ev = l.mate != null ? ("#" + l.mate)
        : ((l.cp > 0 ? "+" : "") + (l.cp / 100).toFixed(2));
      const cls = l.mate != null ? (l.mate > 0 ? "up" : "down")
        : (l.cp > 30 ? "up" : l.cp < -30 ? "down" : "");
      return `<button class="lineitem" data-uci="${l.uci}">` +
        `<span class="ln-mv">${l.san}</span><span class="ln-ev ${cls}">${ev}</span></button>`;
    }).join("");
    el.querySelectorAll(".lineitem").forEach((b) =>
      b.onclick = () => playExploreUci(b.dataset.uci));
  } catch (e) { el.innerHTML = '<div class="hint">engine busy — try again</div>'; }
}
async function loadGame(id) {
  const r = await fetch("/api/game/" + id);
  const g = await r.json();
  const mistakes = {};
  (g.mistakes || []).forEach((m) => { if (m.severity === "blunder") mistakes[m.ply] = m; });
  S.review = { moves: g.moves || [], ply: 0, mistakes, id,
    meta: { opponent: g.opponent, outcome: g.outcome, accuracy: g.accuracy,
            date: g.date, color: g.color, opening: g.opening } };
  setMode("review");
  // jump to first blunder if any, else start
  const firstB = Object.keys(mistakes).map(Number).sort((a,b)=>a-b)[0];
  gotoPly(firstB || 0);
  addChat("sys", `Loaded game vs ${g.opponent} (${g.outcome}, ${g.accuracy}% acc). ` +
    (firstB ? `Jumped to your first blunder — ask me about it.` : `Step through with ◀ ▶.`));
  switchTab("chat");
}

/* ----------------------------------------------------------------- drill */
async function refreshDrillStats() {
  try {
    const s = await (await fetch("/api/drill/stats")).json();
    const el = $("#drill-stats"); if (!el) return;
    if (!s.total) { el.innerHTML = `<span class="hint">No attempts yet — start below. Everything you do here is tracked.</span>`; return; }
    el.innerHTML =
      `<b>Today:</b> ${s.today_solved}/${s.today} solved` +
      ` &nbsp;·&nbsp; <b>All time:</b> ${s.solved}/${s.total} (${s.solve_rate}%)` +
      ` &nbsp;·&nbsp; 🔥 streak ${s.streak}` +
      ` &nbsp;·&nbsp; ${s.distinct} positions seen`;
  } catch (e) { /* stats are best-effort */ }
}
async function logAttempt(outcome, yourMove) {
  const d = S.drill; if (!d) return;
  try {
    await fetch("/api/drill/attempt", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome, your_move: yourMove || null, id: d.id,
        best: d.best, played: d.played, phase: d.phase, deciding: d.deciding,
        win_before: d.win_before, win_after: d.win_after, url: d.url }) });
  } catch (e) { /* logging is best-effort, don't block the drill */ }
  refreshDrillStats();
}
function drillInfo(html, btns) {
  const map = {
    idea: '<button id="d-idea">💡 What\'s the idea?</button>',
    giveup: '<button id="d-giveup">Give up</button>',
    next: '<button id="d-next">Next ▸</button>',
    showbest: '<button id="d-showbest">Show best</button>',
  };
  $("#drill-info").innerHTML = html +
    `<div class="drill-btns">${(btns || []).map((b) => map[b]).join("")}</div>`;
  const bind = (id, fn) => { const e = $(id); if (e) e.onclick = fn; };
  bind("#d-idea", askIdea); bind("#d-giveup", giveUp);
  bind("#d-next", newDrill); bind("#d-showbest", showBest);
}
async function newDrill() {
  const mode = ($("#drill-mode") && $("#drill-mode").value) || "standard";
  const d = await (await fetch("/api/drill?mode=" + mode)).json();
  if (d.error) { drillInfo(`<p class="hint">${d.error}</p>`, []); return; }
  S.drill = { ...d, solved: false, revealed: false, tries: 0 };
  S.game = new Chess(d.fen); S.last = null; S.sel = null; S.targets = [];
  S.line = []; S.ply = 0; S.baseFen = d.fen;   // drill position is the line's base
  S.orient = S.game.turn() === "w" ? "white" : "black";
  // show the played (blunder) move as an arrow; the engine's best stays hidden
  S.arrows = []; S.marks = {}; S.drillPlayedArrow = false;
  try {
    const pm = new Chess(d.fen).move(d.played);
    if (pm) { S.arrows = [{ from: pm.from, to: pm.to, color: "played" }]; S.drillPlayedArrow = true; }
  } catch (e) {}
  setMode("drill", true);
  renderBoard(); renderAnnotations(); renderMoveList(); scheduleEval();
  const side = S.game.turn() === "w" ? "White" : "Black";
  const egLabel = { pawn: "King & pawn", rook: "Rook", queen: "Queen",
    "rook+minor": "Rook + minor", minor: "Minor-piece", "opposite-bishops":
    "Opposite-bishop", other: "Heavy-piece" };
  const tag = d.eg_type ? `<b>${egLabel[d.eg_type] || d.eg_type} endgame — you were winning.</b> ` : "";
  drillInfo(`<p>${tag}<b>${side} to move.</b> The violet arrow is what you played
    (<b>${d.played}</b>) — it dropped your win% ${d.win_before}%→${d.win_after}%.
    Find a better move.</p>`, ["idea", "showbest", "giveup"]);
}
async function gradeDrill(mv) {
  const d = S.drill; if (!d || d.solved) return;
  const uci = mv.from + mv.to + (mv.promotion || "");
  drillInfo(`<p class="hint">Checking ${mv.san}…</p>`, []);
  let res;
  try {
    res = await (await fetch("/api/drill/grade", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: d.fen, uci }) })).json();
  } catch (e) { drillInfo(`<p class="hint">Couldn't reach the engine.</p>`, ["next"]); return; }
  if (res.error) { drillInfo(`<p class="hint">${res.error}</p>`, ["idea", "giveup"]); return; }
  if (res.grade === "retry") {
    d.tries++;
    S.game.undo(); S.last = null; S.sel = null; S.targets = []; renderBoard();
    drillInfo(`<p style="color:var(--warn)">✗ <b>${mv.san}</b> drops your win% by
      <b>${res.win_loss}%</b> — a slip. Try again.</p>`,
      d.tries >= 2 ? ["idea", "showbest", "giveup"] : ["idea", "showbest"]);
    return;
  }
  d.solved = true;
  logAttempt("solved", mv.san);
  const tag = res.grade === "best"
    ? "✓ That's the engine's top move."
    : `✓ Solid — only ${res.win_loss}% off the best.`;
  drillInfo(`<p class="big" style="color:var(--good)">${tag}</p>`, ["idea", "showbest", "next"]);
  scheduleEval();                             // drill done → eval bar may show again
}
async function showBest() {
  const d = S.drill; if (!d) return;
  const btn = document.querySelector("#d-showbest");
  if (d.bestShown) {                          // toggle OFF
    S.arrows = S.arrows.filter((a) => a.color !== "best"); renderAnnotations();
    d.bestShown = false;
    if (btn) btn.textContent = "Show best";
    const note = document.querySelector("#drill-info .best-note"); if (note) note.remove();
    return;
  }
  d.bestShown = true; d.revealed = true;      // toggle ON (deep eval for a reliable best)
  if (btn) btn.textContent = "…";
  try {
    const ev = await (await fetch("/api/eval", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: d.fen, depth: 16 }) })).json();
    if (ev.best_uci) {
      S.arrows = S.arrows.filter((a) => a.color !== "best");
      S.arrows.push({ from: ev.best_uci.slice(0, 2), to: ev.best_uci.slice(2, 4), color: "best" });
      S.drillPlayedArrow = false; renderAnnotations();
    }
    if (!document.querySelector("#drill-info .best-note")) {
      const info = $("#drill-info"), p = document.createElement("p");
      p.className = "best-note";
      p.innerHTML = `Best: <b>${ev.best || "?"}</b> (green arrow).`;
      info.insertBefore(p, info.querySelector(".drill-btns"));
    }
  } catch (e) {}
  if (btn) btn.textContent = "Hide best";
  scheduleEval();                             // revealed → show the eval too
}
async function giveUp() {
  const d = S.drill; if (!d || d.solved) return;
  d.solved = true; logAttempt("revealed", null);
  drillInfo(`<p class="hint">No worries — here's the best move and idea.</p>`,
    ["idea", "showbest", "next"]);
  await showBest();
}
function askIdea() {
  // In a drill, ask about the drill's (bare) position; elsewhere ask about the live board
  // with full context (move list + loaded-game metadata), not just a FEN.
  if (S.mode === "drill" && S.drill) {
    switchTab("chat");
    sendChat("What's the idea in this position — the key features and what I should be " +
      "looking for? Explain the plan; don't just name the single best move.", S.drill.fen);
    return;
  }
  coachSeeBoard(false);
}

/* ------------------------------------------------------------------ play */
async function engineReply() {
  const r = await fetch("/api/move", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fen: S.game.fen(), skill: S.play.skill, movetime: 0.3 }) });
  const d = await r.json();
  if (d.error) return;
  S.game.move(d.san);
  const h = S.game.history({ verbose: true });
  const lm = h[h.length - 1]; S.last = { from: lm.from, to: lm.to };
  S.line = S.game.history(); S.ply = S.line.length;
  renderBoard(); afterPositionChange();
}

/* ----------------------------------------------- coach board control
   The coach can drive the board by emitting [[directive]] tokens in its reply
   (see COACH_PERSONA). We hide them from the displayed text and execute them. */
const DIR_RE = /\[\[(fen|moves|move|reset|orient|flip|analyze|play|level|arrows?|marks?|highlight|clearann)\s*:?\s*([^\]]*)\]\]/gi;

/* Coach annotation directives: weighted arrows + square highlights (opacity = weight).
   Colour names → hex; a spec is "<squares>[/weight][/colour]", specs space/comma-separated. */
const COACH_HEX = { green:"#3aa757", red:"#c0392b", blue:"#2f6fd0", yellow:"#c9a227",
                    orange:"#e0873a", violet:"#8b5cf6", teal:"#17a2b8" };
function annWeight(w) {                        // "0.8" or "80" → opacity 0.1..1 (0.7 default)
  let v = parseFloat(w);
  if (isNaN(v)) return 0.7;
  if (v > 1) v /= 100;                         // tolerate a 0–100 scale
  return Math.max(0.1, Math.min(1, v));
}
// A spec's square part is either from+to squares ("f1c4") OR a SAN move ("Bc4") — coaches
// naturally name the move, so resolve SAN against the current position (`game` clone).
function specSquares(sq, game) {
  const uci = (sq || "").match(/^([a-h][1-8])([a-h][1-8])$/i);
  if (uci) return { from: uci[1].toLowerCase(), to: uci[2].toLowerCase() };
  if (game) {
    try { const mv = game.move(sq, { sloppy: true }); if (mv) { game.undo(); return mv; } } catch (e) {}
  }
  return null;
}
function parseArrows(arg, game) {               // "Bc4/0.9 g1f3/0.5" or "e2e4/0.9/green"
  return (arg || "").split(/[\s,]+/).filter(Boolean).map((spec) => {
    const [sq, w, col] = spec.split("/");
    const s = specSquares(sq, game);
    if (!s) return null;
    return { from: s.from, to: s.to, opacity: annWeight(w),
             hex: COACH_HEX[(col || "").toLowerCase()] || COACH_HEX.green };
  }).filter(Boolean);
}
function parseMarks(arg, game) {                // "d5/0.8" or a move whose TO-square to highlight
  return (arg || "").split(/[\s,]+/).filter(Boolean).map((spec) => {
    const [sq, w, col] = spec.split("/");
    let square = /^[a-h][1-8]$/i.test(sq || "") ? sq.toLowerCase() : null;
    if (!square) { const s = specSquares(sq, game); if (s) square = s.to; }  // SAN → its target square
    if (!square) return null;
    return { sq: square, opacity: annWeight(w),
             color: COACH_HEX[(col || "").toLowerCase()] || COACH_HEX.yellow };
  }).filter(Boolean);
}

// Repair a partial/malformed FEN (small models drop trailing fields or emit the wrong
// rank count) by padding to 8 ranks and filling defaults. The caller still guards with load().
function normalizeFen(raw) {
  const parts = (raw || "").trim().split(/\s+/);
  let placement = parts[0] || "";
  if (!/^[1-8pnbrqkPNBRQK/]+$/.test(placement)) return null;
  let rows = placement.split("/");
  if (rows.length < 8) rows = rows.concat(Array(8 - rows.length).fill("8"));  // pad empty ranks
  if (rows.length > 8) rows = rows.slice(0, 8);
  placement = rows.join("/");
  parts[0] = placement;
  const defaults = ["w", "-", "-", "0", "1"];   // side, castling, en-passant, halfmove, fullmove
  for (let i = 1; i <= 5; i++) if (!parts[i]) parts[i] = defaults[i - 1];
  return parts.slice(0, 6).join(" ");
}

function parseDirectives(text) {
  const directives = [];
  const clean = text.replace(DIR_RE, (_m, type, arg) => {
    directives.push({ type: type.toLowerCase(), arg: (arg || "").trim() });
    return "";
  });
  return { clean: clean.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim(),
           directives };
}
// Hide directive text (complete, or a partial one still streaming) while it arrives.
function stripDirectives(text) {
  return text.replace(DIR_RE, "").replace(/\[\[[^\]]*$/g, "");
}
function applyCoachBoard(directives, annotationsOnly) {
  const prevMode = S.mode;
  if (annotationsOnly) {
    // "explain THIS position" asks (idea button / live-sync / ⚖ Analyze hand-off) may DRAW on
    // the board but never drive/replace it. And never annotate a live unsolved drill — an arrow
    // to the right square would spoil the puzzle.
    if (S.mode === "drill" && S.drill && !S.drill.solved) return false;
    directives = directives.filter((d) => /^(arrows?|marks?|highlight|clearann)$/.test(d.type));
    if (!directives.length) return false;
  }
  // Leaving a drill: the coach is setting up a new position (the user asked), so retire
  // the old drill so it can't linger — otherwise re-entering Drill would show the stale
  // one instead of dealing a fresh puzzle. (The "what's the idea?" flow never reaches
  // here: sendChat suppresses board control when a drill fen override is in play.)
  if (prevMode === "drill") S.drill = null;
  // An explicit [[fen]] is authoritative: small models sometimes tack a stray (often
  // garbage) [[moves]] line onto the real endgame FEN, and the moves' reset+replay
  // could otherwise wipe the position the FEN just set. Drop moves when a FEN is present.
  if (directives.some((d) => d.type === "fen")) {
    directives = directives.filter((d) => d.type !== "moves" && d.type !== "move");
  }
  let touched = false, jump = null, lastMv = null, playSide = null, orientSet = false,
      annotated = false;
  for (const d of directives) {
    if (d.type === "fen") {
      const fen = normalizeFen(d.arg);
      if (fen && S.game.load(fen)) {
        touched = true; lastMv = null; S.baseFen = fen;
        S.arrows = []; S.hlSquares = [];       // a fresh position drops stale annotations
      }
    } else if (d.type === "reset") {
      S.game.reset(); touched = true; lastMv = null; S.baseFen = null;
      S.arrows = []; S.hlSquares = [];
    } else if (d.type === "moves" || d.type === "move") {
      const toks = d.arg.split(/[\s,]+/).filter(Boolean)
        .map((t) => t.replace(/^\d+\.(\.\.)?/, "")).filter(Boolean);  // drop move numbers
      const playInto = (g) => {                 // apply in order, stop at first illegal
        let n = 0, last = null;
        for (const san of toks) {
          const mv = g.move(san, { sloppy: true });
          if (!mv) break;
          last = mv; n++;
        }
        return { n, last };
      };
      let r = playInto(S.game);
      // Nothing applied onto a non-starting board = the coach is setting up a
      // position (a full line from move 1), not continuing this one. Reset and replay
      // — but only if the WHOLE line is legal from the start, so a stray/garbled
      // continuation can never wipe the current position. NB: check the FEN, not
      // history() — a position loaded via [[fen]]/drill/review has an empty history
      // but is still very much "not the start", and is the common setup case.
      if (!r.n && toks.length && S.game.fen() !== START_FEN) {
        const probe = new Chess();
        if (playInto(probe).n === toks.length) {
          S.game.reset(); r = playInto(S.game); lastMv = null; S.baseFen = null;
        }
      }
      if (r.n) { touched = true; lastMv = r.last; }
    } else if (d.type === "orient") {
      S.orient = /^b/i.test(d.arg) ? "black" : "white"; touched = true; orientSet = true;
    } else if (d.type === "flip") {
      S.orient = S.orient === "white" ? "black" : "white"; touched = true; orientSet = true;
    } else if (d.type === "level") {
      const n = parseInt(d.arg, 10);
      if (!isNaN(n)) { S.play.skill = Math.max(0, Math.min(20, n)); touched = true; }
    } else if (d.type === "analyze") { jump = "analyze"; }
    else if (d.type === "play") {
      jump = "play";
      if (/^b/i.test(d.arg)) playSide = "black";
      else if (/^w/i.test(d.arg)) playSide = "white";
    } else if (/^arrows?$/.test(d.type)) {
      const specs = parseArrows(d.arg, new Chess(S.game.fen()));   // clone resolves SAN specs
      if (specs.length) { S.arrows.push(...specs); annotated = true; }
    } else if (/^(marks?|highlight)$/.test(d.type)) {
      const specs = parseMarks(d.arg, new Chess(S.game.fen()));
      if (specs.length) { S.hlSquares.push(...specs); annotated = true; }
    } else if (d.type === "clearann") {
      S.arrows = []; S.hlSquares = []; annotated = true;
    }
  }
  if (!touched && !jump) {          // annotation-only: draw it, leave the board/mode alone
    if (annotated) { renderAnnotations(); return true; }
    return false;
  }
  S.sel = null; S.targets = [];
  // the loaded order becomes the mainline you can step back AND forward through
  S.line = S.game.history(); S.ply = S.line.length;
  if (lastMv) S.last = { from: lastMv.from, to: lastMv.to };
  else if (touched) S.last = null;
  // Playing vs the engine: the student takes `playSide` (default = the side to move,
  // so they move first); the engine takes the other colour and moves when it's its
  // turn. Orientation follows the student's side unless the coach set it explicitly.
  if (jump === "play") {
    const student = playSide || (S.game.turn() === "w" ? "white" : "black");
    if (playSide || !orientSet) S.orient = student;
  }
  // a board change from the dashboard / a locked mode surfaces on the analysis board
  const target = jump ||
    (["home", "review", "drill"].includes(prevMode) ? "analyze" : prevMode);
  S.review = null;
  if (target !== prevMode) enterBoardMode(target);     // switches mode + kicks the engine
  else {
    renderBoard(); renderMoveList(); scheduleEval(); saveBoard();
    // Already in the target mode: enterBoardMode won't run, so kick the engine here if
    // the coach re-set the position and it's now the engine's move.
    if (target === "play" && !S.game.game_over() && S.game.turn() !== S.orient[0]) {
      setTimeout(engineReply, 300);
    }
  }
  // Only claim a board update when something real happened: the position changed, or we
  // switched into a different mode. A bare [[play]] while already in play is a no-op.
  return touched || target !== prevMode;
}

/* Carry the CURRENT position into a board mode (button "Analyze / Play from here",
   or a coach [[analyze]]/[[play]] directive). Keeps S.game as-is. */
function enterBoardMode(mode, askCoach) {
  S.review = null;
  setMode(mode, true);                       // keepBoard: don't reset to the start position
  if (mode === "play" && !S.game.game_over() && S.game.turn() !== S.orient[0]) {
    setTimeout(engineReply, 300);            // it's the engine's move in this position
  }
  switchTab("chat");
  // Explicit "⚖ Analyze position" hand-off: auto-send the FEN so the coach responds about
  // it — no need to type. (Not when the coach itself opened the board, and not when sync is
  // Off.) Only for a real position, so opening an empty Analyze board stays quiet.
  const realPosition = S.line.length > 0 || S.game.fen() !== START_FEN;
  if (askCoach && mode === "analyze" && S.coachSync !== "off" && realPosition) {
    setTimeout(() => coachSeeBoard(false), 150);
  }
}

/* ------------------------------------------------------------------ chat */
// How close to the bottom (px) still counts as "parked at the bottom".
const CHAT_STICK_PX = 30;
function renderChat() {
  document.querySelectorAll(".chatlog").forEach((log) => {
    // Decide BEFORE rewriting whether the reader is parked at the bottom. Only then do we
    // follow the incoming stream; if they've scrolled up to read, keep their exact position
    // so a newly-written line doesn't yank them to the newest message. New tokens always
    // append to the last (bottom) message, so a preserved scrollTop keeps the same lines in
    // view. (chatScroller = the real scroll container: .chatlog on the dashboard, .tabbody
    // in the board view.)
    const box = chatScroller(log);
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight <= CHAT_STICK_PX;
    const savedTop = box.scrollTop;
    log.innerHTML = S.chat.map((m) => {
      const cls = m.role === "user" ? "user" : m.role === "sys" ? "sys" : "coach";
      return `<div class="msg ${cls}"></div>`;
    }).join("");
    [...log.children].forEach((el, i) => {
      const m = S.chat[i];
      if (m.role !== "user" && m.role !== "sys" && !m.content) {   // coach still thinking
        el.classList.add("thinking");
        el.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
      } else {
        el.textContent = m.content;
      }
    });
    box.scrollTop = atBottom ? box.scrollHeight : savedTop;
  });
}
function addChat(role, content) {
  S.chat.push({ role, content });
  renderChat(); saveChat();
}
// The element that actually scrolls: the .chatlog itself on the dashboard
// (max-height + overflow), or its .tabbody wrapper in the board view.
function chatScroller(log) {
  return log.scrollHeight > log.clientHeight ? log : (log.closest(".tabbody") || log);
}
// Pin visible chat logs to the newest message. Needed when a chat becomes visible
// (tab/mode switch): scrollTop is a no-op while it's display:none, so we wait a
// frame for layout, then scroll the real container to the bottom.
function scrollChatToBottom() {
  requestAnimationFrame(() => {
    document.querySelectorAll(".chatlog").forEach((log) => {
      if (log.offsetParent === null) return;   // hidden
      const box = chatScroller(log); box.scrollTop = box.scrollHeight;
    });
  });
}
function saveChat() {
  try { localStorage.setItem("cc_chat", JSON.stringify(S.chat.slice(-40))); } catch (e) {}
}
/* Generic SSE reader shared by chat / coach-review / sync. Calls onEvent(obj). */
async function streamSSE(url, body, onEvent) {
  const r = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 409) { onEvent({ error: "A sync is already running." }); return; }
  const reader = r.body.getReader(); const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n"); buf = parts.pop();
    for (const p of parts) {
      const line = p.replace(/^data: /, "");
      if (line === "[DONE]") continue;
      try { onEvent(JSON.parse(line)); } catch (e) {}
    }
  }
}
async function resetCoach() {
  document.querySelectorAll(".chat-reset").forEach((b) => b.disabled = true);
  try { await fetch("/api/coach/reset", { method: "POST" }); } catch (e) {}
  S.chat = []; saveChat();
  addChat("sys", "Coach session reset — fresh start.");
  document.querySelectorAll(".chat-reset").forEach((b) => b.disabled = false);
}
// What the coach can "see" of the board each turn: the live FEN, the move list that led
// there (SAN + cursor), and — when it's a loaded Chess.com game — which game it is.
function coachBoardInfo() {
  const inReview = S.mode === "review" && S.review;
  return {
    fen: S.game.fen(),
    moves: inReview ? S.review.moves : S.line,
    ply: inReview ? S.review.ply : S.ply,
    game: inReview ? (S.review.meta || null) : null,
  };
}

/* ----- board → coach sync (3-state toggle: live / silent / off) ----- */
const SYNC_ORDER = ["silent", "live", "off"];
const SYNC_UI = {
  live:   { label: "🔄 Sync: Live",   title: "Coach auto-comments on every board change" },
  silent: { label: "🔇 Sync: Silent", title: "Coach silently tracks the board; speaks only when asked" },
  off:    { label: "⏸ Sync: Off",     title: "Coach is not sent the board unless you click a board button" },
};
function loadSync() {
  const v = localStorage.getItem("cc_sync");
  if (SYNC_ORDER.includes(v)) S.coachSync = v;
  renderSyncToggle();
}
function renderSyncToggle() {
  const ui = SYNC_UI[S.coachSync];
  document.querySelectorAll(".coach-sync").forEach((b) => {
    b.textContent = ui.label; b.title = ui.title;
    b.dataset.state = S.coachSync;
  });
}
function cycleSync() {
  const i = SYNC_ORDER.indexOf(S.coachSync);
  S.coachSync = SYNC_ORDER[(i + 1) % SYNC_ORDER.length];
  try { localStorage.setItem("cc_sync", S.coachSync); } catch (e) {}
  renderSyncToggle();
}
// Only board modes carry a meaningful position to sync.
function boardMode() { return S.mode === "analyze" || S.mode === "review" || S.mode === "play"; }
// Called after any board change. Debounced so stepping quickly collapses to one sync.
function notifyCoachBoard() {
  if (!boardMode() || S.coachSync === "off" || S.mode === "drill") return;
  clearTimeout(S.syncTimer);
  S.syncTimer = setTimeout(() => {
    if (S.coachSync === "live") coachSeeBoard(true);
    else if (S.coachSync === "silent") silentSyncBoard();
  }, 800);
}
// Silently tell the warm coach session about the current position (no visible reply).
async function silentSyncBoard() {
  try {
    await fetch("/api/coach/board", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(coachBoardInfo()) });
  } catch (e) {}
}
// Ask the coach about the CURRENT live position (full board context, not a bare FEN).
// Used by the "What's the idea?" button, live-sync, and the Analyze hand-off.
function coachSeeBoard(brief) {
  switchTab("chat");
  const q = brief
    ? "The board just changed. In one or two sentences: what's the idea / plan in this new position?"
    : "What's the idea in this position — the key features and the plan I should follow? "
      + "Don't just name the single best move.";
  sendChat(q, null, coachBoardInfo());
}
async function sendChat(text, fenOverride, boardInfo) {
  text = (text || "").trim();
  if (!text) return;
  addChat("user", text);
  const msgs = S.chat.filter((m) => m.role === "user" || m.role === "coach")
    .map((m) => ({ role: m.role === "coach" ? "assistant" : "user", content: m.content }));
  addChat("coach", "");
  const idx = S.chat.length - 1; let acc = "";
  document.querySelectorAll("#send,#dash-send").forEach((b) => b.disabled = true);
  // Three ways board context reaches the coach:
  //  - boardInfo: an explicit "explain THIS position" ask (idea button / Analyze hand-off /
  //    live-sync) — always carries the full live board (fen + moves + game).
  //  - fenOverride: a drill's bare position (no move history).
  //  - a plain typed message: carries the live board UNLESS the sync toggle is Off.
  const body = boardInfo ? { messages: msgs, ...boardInfo }
             : fenOverride ? { messages: msgs, fen: fenOverride }
             : S.coachSync === "off" ? { messages: msgs }
             : { messages: msgs, ...coachBoardInfo() };
  const suppressDrive = !!fenOverride || !!boardInfo;   // "explain this" asks don't drive the board
  try {
    await streamSSE("/api/chat", body, (j) => {
      if (j.t) acc += j.t;
      if (j.error) acc = "⚠ " + j.error;
      S.chat[idx].content = stripDirectives(acc); renderChat();   // hide raw directives
    });
  } catch (e) { S.chat[idx].content = "⚠ " + e.message; renderChat(); }
  const { clean, directives } = parseDirectives(acc);
  S.chat[idx].content = clean || stripDirectives(acc);
  renderChat();
  // "Explain this position" asks (drill idea, live idea, live-sync) are questions ABOUT the
  // shown position — the reply may still DRAW on it (arrows/highlights) but not drive/replace
  // it (annotationsOnly = suppressDrive). Plain chat can do both.
  if (directives.length && applyCoachBoard(directives, suppressDrive)) {
    const where = ({ analyze: "Analyze", play: "Play vs engine" })[S.mode];
    addChat("sys", suppressDrive ? "↪ Coach drew on the board."
      : "↪ Coach updated the board" + (where ? ` — you're now in ${where}.` : "."));
  }
  saveChat();
  document.querySelectorAll("#send,#dash-send").forEach((b) => b.disabled = false);
}

/* --------------------------------------------------------------- dashboard */
async function loadDashboard() {
  let d;
  try { d = await (await fetch("/api/dashboard?t=" + Date.now())).json(); } catch (e) { return; }
  S.dash = d;
  renderRating(d.rating);
  renderRatingChart(d.rating_history || []);
  renderWeek(d.week, d.streak);
  $("#training").innerHTML = (d.training || []).map((t) => `<li>${t}</li>`).join("");
  renderRecommended(d.recommended);
  renderLast10(d.last10);
  renderDepthCoverage(d.depth);
}
function renderRating(r) {
  const span = (r.target - r.floor) || 1000;
  const pct = (v) => Math.max(0, Math.min(100, ((v - r.floor) / span) * 100));
  const cur = r.current || r.floor;
  $("#rating-pool").textContent = "(" + (r.pool || "") + ")";
  $("#rating-fill").style.width = pct(cur) + "%";
  const cm = $("#rating-cur"); cm.style.left = pct(cur) + "%"; cm.textContent = cur;
  const pk = $("#rating-peak");
  if (r.peak && r.peak > cur) { pk.style.left = pct(r.peak) + "%"; pk.title = "peak " + r.peak; pk.style.display = "block"; }
  else pk.style.display = "none";
}
function niceStep(x) {
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const f = x / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
function renderRatingChart(history) {
  const el = $("#ratingchart"); if (!el) return;
  if (!history || history.length < 2) { el.innerHTML = ""; return; }
  const W = 700, H = 180, padL = 40, padR = 12, padT = 14, padB = 20;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const ts = history.map((p) => p[0]);
  const tmin = ts[0], tmax = ts[ts.length - 1];
  let rmin = Math.min(...history.map((p) => p[1]));
  let rmax = Math.max(...history.map((p) => p[1]));
  const pad = Math.max(20, (rmax - rmin) * 0.12); rmin -= pad; rmax += pad;
  const sx = (t) => x0 + (t - tmin) / (tmax - tmin || 1) * (x1 - x0);
  const sy = (r) => y1 - (r - rmin) / (rmax - rmin || 1) * (y1 - y0);
  const pts = history.map((p) => ({ x: sx(p[0]), y: sy(p[1]), t: p[0], r: p[1] }));

  // recessive y gridlines + labels at nice rating steps
  const step = niceStep((rmax - rmin) / 4);
  let grid = "";
  for (let v = Math.ceil(rmin / step) * step; v < rmax; v += step) {
    const y = sy(v).toFixed(1);
    grid += `<line class="rc-grid" x1="${x0}" x2="${x1}" y1="${y}" y2="${y}"/>`;
    grid += `<text class="rc-ylab" x="${x0 - 6}" y="${(+y + 3).toFixed(1)}" text-anchor="end">${v}</text>`;
  }
  // month ticks along x
  let xt = "", cur = new Date(tmin * 1000); cur = new Date(cur.getFullYear(), cur.getMonth(), 1);
  const months = [];
  while (cur.getTime() / 1000 <= tmax) { months.push(new Date(cur)); cur.setMonth(cur.getMonth() + 1); }
  const every = Math.max(1, Math.ceil(months.length / 7));
  months.forEach((m, i) => {
    const t = m.getTime() / 1000; if (i % every || t < tmin) return;
    xt += `<text class="rc-xlab" x="${sx(t).toFixed(1)}" y="${H - 6}" text-anchor="middle">${m.toLocaleString("en", { month: "short" })}</text>`;
  });

  const line = pts.map((p, i) => (i ? "L" : "M") + p.x.toFixed(1) + " " + p.y.toFixed(1)).join(" ");
  const area = `M${pts[0].x.toFixed(1)} ${y1} ` +
    pts.map((p) => "L" + p.x.toFixed(1) + " " + p.y.toFixed(1)).join(" ") +
    ` L${pts[pts.length - 1].x.toFixed(1)} ${y1} Z`;
  const last = pts[pts.length - 1];
  let pk = pts[0]; for (const p of pts) if (p.r > pk.r) pk = p;

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="rc-svg">
    <defs><linearGradient id="rcfill" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="var(--accent)" stop-opacity=".28"/>
      <stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    ${grid}
    <path class="rc-area" d="${area}" fill="url(#rcfill)"/>
    <path class="rc-line" d="${line}"/>
    <circle class="rc-peak" cx="${pk.x.toFixed(1)}" cy="${pk.y.toFixed(1)}" r="3"/>
    <text class="rc-peaklab" x="${pk.x.toFixed(1)}" y="${(pk.y - 7).toFixed(1)}" text-anchor="middle">peak ${pk.r}</text>
    <circle class="rc-cur" cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="3.5"/>
    ${xt}
    <line class="rc-cross" y1="${y0}" y2="${y1}" style="display:none"/>
    <circle class="rc-hoverdot" r="3.5" style="display:none"/>
  </svg><div class="rc-tip" style="display:none"></div>`;

  const svg = el.querySelector("svg"), cross = el.querySelector(".rc-cross");
  const hd = el.querySelector(".rc-hoverdot"), tip = el.querySelector(".rc-tip");
  svg.addEventListener("mousemove", (ev) => {
    const rc = svg.getBoundingClientRect(); const sc = rc.width / W;
    const vx = (ev.clientX - rc.left) / sc;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (pts[mid].x < vx) lo = mid + 1; else hi = mid; }
    let best = pts[lo]; if (lo > 0 && Math.abs(pts[lo - 1].x - vx) < Math.abs(best.x - vx)) best = pts[lo - 1];
    cross.setAttribute("x1", best.x); cross.setAttribute("x2", best.x); cross.style.display = "";
    hd.setAttribute("cx", best.x); hd.setAttribute("cy", best.y); hd.style.display = "";
    const dt = new Date(best.t * 1000);
    tip.style.display = ""; tip.style.left = (best.x * sc) + "px"; tip.style.top = (best.y * sc) + "px";
    tip.innerHTML = `<b>${best.r}</b> <span>${dt.toLocaleDateString("en", { month: "short", day: "numeric", year: "2-digit" })}</span>`;
  });
  svg.addEventListener("mouseleave", () => {
    cross.style.display = "none"; hd.style.display = "none"; tip.style.display = "none";
  });
}
function renderWeek(week, streak) {
  $("#streak").textContent = streak;
  $("#week").innerHTML = (week || []).map((w) => {
    const st = w.completed ? "done" : w.is_today ? "today" : w.is_future ? "future" : "miss";
    const mark = w.completed ? "✓" : w.is_future ? "" : w.is_today ? (w.drills + "/5") : "✗";
    return `<div class="weekchip ${st}"><span class="wc-d">${w.dow}</span><span class="wc-m">${mark}</span></div>`;
  }).join("");
}
function renderRecommended(rec) {
  const rev = rec.review;
  const el = $("#rec");
  el.innerHTML =
    `<button class="bigbtn" id="rec-drill">Do ${rec.drills_target} drills
       <span class="sub">${rec.drills_today}/${rec.drills_target} today</span></button>` +
    (rev ? `<button class="bigbtn" id="rec-review">Review your last blunder-loss
       <span class="sub">vs ${rev.opponent} · ${rev.date}</span></button>`
         : `<div class="hint">No recent blunder-loss to review — clean.</div>`);
  $("#rec-drill").onclick = () => setMode("drill");
  const rv = $("#rec-review"); if (rv) rv.onclick = () => loadGame(rev.id);
}
function renderLast10(list) {
  $("#last10").innerHTML = (list || []).map((g) => {
    const acc = g.accuracy != null ? g.accuracy + "%" : "—";
    const bl = g.n_blunders != null ? " · " + g.n_blunders + "bl" : "";
    return `<div class="growmini" data-id="${g.id || ""}">
      <span class="pill ${g.outcome}">${(g.outcome || "?")[0].toUpperCase()}</span>
      <span class="l10-opp">${g.opponent || "?"}</span>
      <span class="l10-meta">${g.color} · ${g.time_class} · ${acc}${bl}</span>
      <span class="l10-date">${g.date}</span></div>`;
  }).join("");
  $("#last10").querySelectorAll(".growmini").forEach((row) => {
    if (row.dataset.id) row.onclick = () => loadGame(row.dataset.id);
    else row.classList.add("noana");
  });
}
async function startSync(deepen) {
  const log = $("#sync-log");
  const buttons = [$("#btn-sync"), $("#btn-deepen")];
  const depth = parseInt(($("#sync-depth") || {}).value, 10) || 12;
  buttons.forEach((b) => b && (b.disabled = true));
  log.style.display = "block"; log.textContent = "";
  const put = (t) => { log.textContent += t + "\n"; log.scrollTop = 1e9; };
  try {
    await streamSSE("/api/sync", { depth, deepen: !!deepen }, (j) => {
      if (j.t) put(j.t);
      if (j.error) put("⚠ " + j.error);
      if (j.done) {
        const verb = deepen ? "deepened" : "new game(s) analyzed";
        put(`\n✔ ${j.done.new_analyzed} ${verb}. ` +
            `Total ${j.done.games_total}. Rating ${j.done.current_rating}.`);
        S.lastSyncNew = j.done.new_analyzed;
        loadDashboard();
      }
    });
  } catch (e) { put("⚠ " + e.message); }
  buttons.forEach((b) => b && (b.disabled = false));
}
// Analysis-depth coverage line under the sync buttons.
function renderDepthCoverage(depth) {
  const el = $("#depth-cover"); if (!el || !depth) return;
  const parts = [];
  if (depth.min != null) parts.push(`min depth ${depth.min}` +
    (depth.max !== depth.min ? `–${depth.max}` : ""));
  if (depth.unknown) parts.push(`${depth.unknown} not yet depth-tagged`);
  el.textContent = parts.length
    ? `${depth.analyzed} analyzed · ${parts.join(" · ")}`
    : `${depth.analyzed} analyzed`;
}
async function coachReview() {
  addChat("sys", "Reviewing your recent progress…");
  addChat("coach", "");
  const idx = S.chat.length - 1; let acc = "";
  const btn = $("#btn-review"); btn.disabled = true;
  try {
    await streamSSE("/api/coach/review", { new_analyzed: S.lastSyncNew || null }, (j) => {
      if (j.t) acc += j.t;
      if (j.error) acc = "⚠ " + j.error;
      S.chat[idx].content = acc; renderChat();
    });
  } catch (e) { S.chat[idx].content = "⚠ " + e.message; renderChat(); }
  saveChat(); btn.disabled = false;
}

/* ------------------------------------------------------------------ modes/tabs */
function setMode(mode, keepBoard) {
  S.mode = mode;
  if (mode !== "review") { S.explore = false; S.branch = null; }  // explorer is review-only
  document.querySelectorAll("#modes button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode));
  const home = mode === "home";
  $("#home").style.display = home ? "block" : "none";
  $("#app-main").style.display = home ? "none" : "flex";
  if (home) { loadDashboard(); scrollChatToBottom(); return; }
  if (mode === "analyze") {
    if (S.review) {                    // carry a loaded game into an editable line
      S.line = S.review.moves.slice(); S.ply = S.review.ply;
      S.baseFen = null; S.review = null;
      rebuildToPly(S.ply);             // build S.game at the cursor from the line
    }
    // otherwise keep the current position/line — Analyze never wipes; ↺ Reset does.
  }
  if (mode === "play" && !keepBoard) {
    S.game = new Chess(); S.last = null; S.line = []; S.ply = 0; S.baseFen = null;
  }
  S.sel = null; S.targets = [];
  renderBoard(); renderMoveList(); scheduleEval(); saveBoard();
  updateExploreUI(); renderLines();
  if (mode === "review") switchTab("games");
  if (mode === "drill") { switchTab("drill"); if (!S.drill) newDrill(); }
  scrollChatToBottom();   // if the Coach tab is the visible one, pin it to newest
}
function switchTab(tab) {
  document.querySelectorAll("#tabhead button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.id === "tab-" + tab));
  if (tab === "games") loadGames();
  if (tab === "drill") refreshDrillStats();
  if (tab === "chat") scrollChatToBottom();
}
async function loadGames() {
  const r = await fetch("/api/games"); const games = await r.json();
  const wrap = $("#games-list");
  if (!games.length) { $("#games-hint").textContent =
    "No analyzed games yet — the overnight run is still working. Check back soon."; return; }
  $("#games-hint").textContent = `${games.length} analyzed games — click one to review.`;
  wrap.innerHTML = games.slice(0, 200).map((g) => `
    <div class="grow" data-id="${g.id}">
      <div><b>vs ${g.opponent || "?"}</b><div class="meta">${g.date} · ${g.color} · ${g.opening || ""}</div></div>
      <div style="text-align:right">
        <span class="pill ${g.outcome}">${g.outcome}</span>
        <div class="meta">${g.accuracy}% acc · ${g.n_blunders} blund</div>
      </div>
    </div>`).join("");
  wrap.querySelectorAll(".grow").forEach((row) => row.onclick = () => loadGame(row.dataset.id));
}

/* ------------------------------------------------------------------ init */
function wire() {
  document.querySelectorAll("#modes button").forEach((b) =>
    b.onclick = () => setMode(b.dataset.mode));
  document.querySelectorAll("#tabhead button").forEach((b) =>
    b.onclick = () => switchTab(b.dataset.tab));
  $("#flip").onclick = () => { S.orient = S.orient === "white" ? "black" : "white"; renderBoard(); };
  $("#reset").onclick = resetBoard;
  $("#prev").onclick = navPrev;
  $("#next").onclick = navNext;
  const et = $("#explore-toggle"); if (et) et.onclick = toggleExplore;
  const wireChat = (boxId, btnId) => {
    const box = $("#" + boxId), btn = $("#" + btnId);
    const go = () => { const t = box.value; box.value = ""; sendChat(t); };
    btn.onclick = go;
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } });
  };
  wireChat("chatbox", "send");
  wireChat("dash-chatbox", "dash-send");
  document.querySelectorAll(".chat-reset").forEach((b) => b.onclick = resetCoach);
  document.querySelectorAll(".coach-sync").forEach((b) => b.onclick = cycleSync);
  document.querySelectorAll(".coach-idea").forEach((b) => b.onclick = askIdea);
  loadSync();
  // Only the position-jump buttons — NOT the sibling sync/idea buttons in the same row.
  document.querySelectorAll(".chat-jump button[data-jump]").forEach((b) =>
    b.onclick = () => enterBoardMode(b.dataset.jump, b.dataset.jump === "analyze"));
  $("#btn-sync").onclick = () => startSync(false);
  const bd = $("#btn-deepen"); if (bd) bd.onclick = () => startSync(true);
  // Remember the analysis-depth choice across reloads (default 12 otherwise).
  const depthSel = $("#sync-depth");
  if (depthSel) {
    const saved = localStorage.getItem("cc_depth");
    if (saved && [...depthSel.options].some((o) => o.value === saved)) depthSel.value = saved;
    depthSel.addEventListener("change", () => {
      try { localStorage.setItem("cc_depth", depthSel.value); } catch (e) {}
    });
  }
  $("#btn-review").onclick = coachReview;
  $("#drill-new").onclick = newDrill;
  // onboarding + settings
  $("#setup-go").onclick = doSetup;
  $("#setup-user").addEventListener("keydown", (e) => { if (e.key === "Enter") doSetup(); });
  const sl = $("#setup-settings-link"); if (sl) sl.onclick = (e) => { e.preventDefault(); openSettings(); };
  $("#open-settings").onclick = openSettings;
  $("#settings-close").onclick = closeSettings;
  $("#settings-modal").addEventListener("click", (e) => { if (e.target.id === "settings-modal") closeSettings(); });
  $("#cfg-backend").onchange = updateBackendGroups;
  $("#cfg-save").onclick = saveConfig;
  document.addEventListener("pointermove", onPointerMove);
  document.addEventListener("pointerup", onPointerUp);
  const bw = document.querySelector(".boardwrap");
  if (bw) bw.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("keydown", (e) => {
    if (document.activeElement.tagName === "TEXTAREA") return;
    if (e.key === "ArrowLeft") $("#prev").click();
    if (e.key === "ArrowRight") $("#next").click();
  });
}
function restoreBoard() {
  // Bring back the loaded game across a reload (still lands on Home; re-opening
  // Analyze shows it). Only restore analyze/review — play/drill start fresh.
  try {
    const b = JSON.parse(localStorage.getItem("cc_board") || "null");
    if (!b || (b.mode !== "analyze" && b.mode !== "review")) return;
    S.orient = b.orient === "black" ? "black" : "white";
    if (b.mode === "review" && b.review) {
      S.review = b.review; S.mode = "review";
      const g = new Chess();
      for (let i = 0; i < (b.review.ply || 0); i++) g.move(b.review.moves[i], { sloppy: true });
      S.game = g;
    } else {
      S.line = Array.isArray(b.line) ? b.line : [];
      S.baseFen = b.baseFen || null;
      S.mode = "analyze";
      rebuildToPly(b.ply || 0);
    }
  } catch (e) {}
}
/* ------------------------------------------------------------- setup / settings */
const LOCAL_BACKENDS_JS = ["local", "ollama", "qwen", "gemma"];

// Status line + first-run onboarding: show the Setup card until a username is set or games exist.
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("#status").textContent = `${s.analyzed} games` + (s.coach ? ` · coach: ${s.backend}` : " · coach: off");
    const setup = $("#setup"), grid = document.querySelector("#home .dash-grid");
    if (setup) setup.style.display = s.configured ? "none" : "block";
    if (grid) grid.style.display = s.configured ? "" : "none";
    return s;
  } catch (e) { $("#status").textContent = "offline"; return null; }
}

async function doSetup() {
  const u = $("#setup-user").value.trim();
  if (!u) { $("#setup-user").focus(); return; }
  const btn = $("#setup-go"); btn.disabled = true;
  const log = $("#setup-log"); log.style.display = "block"; log.textContent = "";
  const put = (t) => { log.textContent += t + "\n"; log.scrollTop = 1e9; };
  try {
    await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u }) });
    put("Saved. Fetching and analyzing your games — the first run can take a while…");
    const depth = parseInt(localStorage.getItem("cc_depth") || "12", 10) || 12;
    await streamSSE("/api/sync", { depth, deepen: false }, (j) => {
      if (j.t) put(j.t);
      if (j.error) put("⚠ " + j.error);
      if (j.done) put(`\n✔ ${j.done.new_analyzed} game(s) analyzed. Loading your dashboard…`);
    });
    const s = await refreshStatus();
    if (s && s.configured) loadDashboard();
  } catch (e) { put("⚠ " + e.message); }
  btn.disabled = false;
}

async function openSettings() {
  $("#settings-modal").style.display = "flex";
  try {
    const c = await (await fetch("/api/config")).json();
    const isLocal = LOCAL_BACKENDS_JS.includes(c.backend);
    $("#cfg-user").value = c.username || "";
    $("#cfg-backend").value = isLocal ? "local" : (c.backend || "");
    $("#cfg-url").value = c.local_url || "http://localhost:11434";
    $("#cfg-think").checked = !!c.think;
    $("#cfg-model-api").value = c.backend === "api" ? (c.model || "") : "";
    $("#cfg-model-local").value = isLocal ? (c.model || "") : "";
    $("#cfg-cli-status").textContent = c.claude_cli ? "✓ CLI detected" : "✗ CLI not found on PATH";
    $("#cfg-key").placeholder = c.has_key ? "•••• saved — leave blank to keep" : "sk-ant-…";
    $("#cfg-status").textContent = "";
  } catch (e) {}
  updateBackendGroups();
}
function closeSettings() { $("#settings-modal").style.display = "none"; }
function updateBackendGroups() {
  const b = $("#cfg-backend").value;
  $("#cfg-sub").style.display = (b === "subscription" || b === "") ? "block" : "none";
  $("#cfg-api").style.display = b === "api" ? "block" : "none";
  $("#cfg-local").style.display = b === "local" ? "block" : "none";
}
async function saveConfig() {
  const b = $("#cfg-backend").value;
  const body = { username: $("#cfg-user").value.trim(), backend: b,
    local_url: $("#cfg-url").value.trim(), think: $("#cfg-think").checked };
  if (b === "api") {
    body.model = $("#cfg-model-api").value.trim();
    const k = $("#cfg-key").value.trim(); if (k) body.anthropic_api_key = k;
  } else if (b === "local") {
    body.model = $("#cfg-model-local").value.trim();
  } else {
    body.model = "";                 // subscription/auto → let the server default the model
  }
  const st = $("#cfg-status"); st.textContent = "Saving…";
  try {
    const r = await (await fetch("/api/config", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    st.textContent = "Saved — coach: " + (r.backend === "none" ? "not configured" : r.backend);
    refreshStatus();
  } catch (e) { st.textContent = "⚠ " + e.message; }
}

async function boot() {
  wire(); restoreBoard(); renderBoard(); renderMoveList();
  try { const saved = localStorage.getItem("cc_chat"); if (saved) S.chat = JSON.parse(saved) || []; } catch (e) {}
  if (S.chat.length) renderChat();     // restore the conversation across sessions
  else addChat("coach", "Hi — I'm your chess coach. Load a game to review, hit Drill to " +
    "train on your own blunder positions, or just ask me anything. I read your game analysis " +
    "and can set up positions, spar with you, and draw on the board.");
  setMode("home");                    // land on the dashboard
  refreshStatus();                    // status line + first-run onboarding toggle
}
boot();
