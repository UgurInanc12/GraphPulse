/* GraphPulse frontend: 3D force graph + SSE live updates.
 *
 * View modes:
 *  - full : every node (small/medium graphs)
 *  - agg  : repo x community super-nodes (large graphs, e.g. the global one)
 *  - focus: one community expanded to full detail + 1-hop neighbors
 */
/* global ForceGraph3D */
"use strict";

const COMMUNITY_COLORS = [
  "#7289da", "#43b581", "#faa61a", "#f04747", "#b573d6",
  "#3aa8c1", "#d68f5a", "#7fbf5f", "#c96a9c", "#8a9cc9",
  "#5fb0a0", "#c9c05f", "#9a6fd6", "#6fa8dc", "#d67f7f",
];
const SPAWN_MS = 1400;
const FLASH_MS = 2600;
const MAX_FEED = 60;
const FREEZE_ABOVE = 2500;   // physics freeze threshold (node count)
const FREEZE_EDGES = 4000;   // physics freeze threshold (edge count)
const DEFAULT_GRAPH = "global";  // opens here, pinned to top of the selector

const state = {
  gid: null,
  mode: "full",             // full | agg | focus
  focusId: null,
  totalNodes: 0,
  nodes: [],
  edges: [],
  byId: new Map(),
  spawn: new Map(),
  flash: new Map(),
  removing: new Map(),
  frozen: false,
};

const $ = (sel) => document.querySelector(sel);
const sceneEl = $("#scene");

/* ---------- graph setup ---------- */

const Graph = ForceGraph3D()(sceneEl)
  .backgroundColor("#1e2124")
  .showNavInfo(false)
  .nodeLabel(nodeTooltip)
  .nodeRelSize(4)
  .nodeVal((n) => nodeVal(n))
  .nodeColor((n) => nodeColor(n))
  .nodeOpacity(0.92)
  .linkColor((l) => (l.confidence === "EXTRACTED" ? "#8a93a6" : "#5a6172"))
  .linkOpacity(0.35)
  .linkWidth((l) => linkWidth(l))
  .linkDirectionalParticles((l) => (l._hot ? 2 : 0))
  .linkDirectionalParticleWidth(1.6)
  .onNodeClick(onNodeClick)
  .onBackgroundClick(() => hidePanel())
  .onEngineStop(() => {
    if (state.frozen) Graph.cooldownTicks(0);
  });

Graph.d3Force("charge").strength(-60);

function isAggNode(n) { return state.mode === "agg"; }

function nodeTooltip(n) {
  if (isAggNode(n)) {
    const top = (n.top || []).map(esc).join(", ");
    return `<b>${esc(n.label)}</b><br>${n.count} nodes<br><span style="color:#8a8f98">${top}</span>`;
  }
  return `${esc(n.label || n.id)}<br><span style="color:#8a8f98">${esc(n.source_file || "")} ${esc(n.source_location || "")}</span>`;
}

function nodeVal(n) {
  if (isAggNode(n)) return Math.max(2, Math.sqrt(n.count || 1) * 1.6);
  const t = state.spawn.get(n.id);
  const deg = Math.min(8, 1 + (n._deg || 0) * 0.35);
  if (t !== undefined) {
    const k = Math.min(1, (performance.now() - t) / SPAWN_MS);
    return Math.max(0.05, deg * easeOut(k));
  }
  const r = state.removing.get(n.id);
  if (r !== undefined) {
    const k = Math.min(1, (performance.now() - r) / SPAWN_MS);
    return Math.max(0.02, deg * (1 - k));
  }
  return deg;
}

function linkWidth(l) {
  if (l._hot) return 1.6;
  if (state.mode === "agg") return Math.min(3, 0.3 + Math.log1p(l.weight || 1) * 0.5);
  return 0.4;
}

function repoColor(repo) {
  let h = 0;
  for (let i = 0; i < repo.length; i++) h = (h * 31 + repo.charCodeAt(i)) >>> 0;
  return COMMUNITY_COLORS[h % COMMUNITY_COLORS.length];
}

function nodeColor(n) {
  const f = state.flash.get(n.id);
  if (f !== undefined) {
    const k = Math.min(1, (performance.now() - f) / FLASH_MS);
    return k < 0.5 ? "#ffffff" : blend("#ffffff", baseColor(n), (k - 0.5) * 2);
  }
  const s = state.spawn.get(n.id);
  if (s !== undefined) {
    const k = Math.min(1, (performance.now() - s) / SPAWN_MS);
    return blend("#43b581", baseColor(n), k);
  }
  if (state.removing.has(n.id)) return "#f04747";
  if (state.mode === "focus" && n._in_focus === false) return "#4a4f57";
  return baseColor(n);
}

function baseColor(n) {
  if (isAggNode(n)) return repoColor(n.repo || "");
  const c = typeof n.community === "number" ? n.community : 0;
  return COMMUNITY_COLORS[c % COMMUNITY_COLORS.length];
}

/* Animation pump */
setInterval(() => {
  const now = performance.now();
  let dirty = state.spawn.size || state.flash.size || state.removing.size;
  for (const [id, t] of state.spawn) if (now - t > SPAWN_MS) state.spawn.delete(id);
  for (const [id, t] of state.flash) if (now - t > FLASH_MS) state.flash.delete(id);
  if (state.removing.size) {
    const gone = [];
    for (const [id, t] of state.removing) if (now - t > SPAWN_MS) gone.push(id);
    if (gone.length) reallyRemove(gone);
  }
  if (dirty) {
    Graph.nodeColor(Graph.nodeColor());
    Graph.nodeVal(Graph.nodeVal());
  }
}, 120);

function easeOut(k) { return 1 - Math.pow(1 - k, 3); }
function blend(a, b, k) {
  const pa = hex(a), pb = hex(b);
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * k));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
function hex(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

/* ---------- FPS meter ---------- */
(function fpsMeter() {
  let frames = 0;
  let last = performance.now();
  function loop() {
    frames++;
    const now = performance.now();
    if (now - last >= 1000) {
      const fps = Math.round((frames * 1000) / (now - last));
      const el = $("#fps");
      el.textContent = `${fps} fps`;
      el.style.color = fps >= 45 ? "#43b581" : fps >= 20 ? "#faa61a" : "#f04747";
      frames = 0;
      last = now;
    }
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();

/* Pause rendering when the tab is hidden (saves GPU for the rest of the app). */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) Graph.pauseAnimation();
  else Graph.resumeAnimation();
});

/* ---------- data plumbing ---------- */

function computeDegrees() {
  const deg = new Map();
  for (const e of state.edges) {
    const s = idOf(e.source), t = idOf(e.target);
    deg.set(s, (deg.get(s) || 0) + 1);
    deg.set(t, (deg.get(t) || 0) + 1);
  }
  for (const n of state.nodes) n._deg = deg.get(n.id) || 0;
}
function idOf(x) { return typeof x === "object" && x !== null ? x.id : x; }

function tunePhysics() {
  const n = state.nodes.length;
  const e = state.edges.length;
  // Cost is driven by edges as much as nodes: the aggregate overview has few
  // nodes but very dense weighted links, so gate on both. Freezing means the
  // layout is solved during warmup and the render loop stops re-simulating.
  const heavy = n > FREEZE_ABOVE || e > FREEZE_EDGES || state.mode === "agg";
  if (heavy) {
    state.frozen = true;
    Graph.nodeResolution(n > 10000 ? 4 : 6);
    Graph.warmupTicks(n > 10000 ? 60 : 100);
    Graph.cooldownTicks(0);
  } else {
    state.frozen = false;
    Graph.nodeResolution(8);
    Graph.warmupTicks(0);
    Graph.cooldownTicks(Infinity);
  }
}

function pushData() {
  computeDegrees();
  state.byId = new Map(state.nodes.map((n) => [n.id, n]));
  tunePhysics();
  Graph.graphData({ nodes: state.nodes, links: state.edges });
  updateStats();
  updateCrumb();
}

function viewUrl(gid, opts = {}) {
  let url = `/api/graph/${encodeURIComponent(gid)}`;
  const qs = [];
  if (opts.focus) qs.push(`focus=${encodeURIComponent(opts.focus)}`);
  if (opts.mode) qs.push(`mode=${opts.mode}`);
  if (qs.length) url += "?" + qs.join("&");
  return url;
}

async function loadGraph(gid, opts = {}) {
  const res = await fetch(viewUrl(gid, opts));
  if (!res.ok) return null;
  const data = await res.json();
  state.gid = gid;
  state.mode = data.mode || "full";
  state.focusId = data.focus || null;
  state.totalNodes = data.total_nodes || (data.nodes || []).length;
  state.nodes = data.nodes || [];
  state.edges = data.edges || [];
  state.spawn.clear();
  state.flash.clear();
  state.removing.clear();
  hidePanel();
  pushData();
  if (!opts.keepCamera) Graph.zoomToFit(600, 40);
  return data;
}

function updateCrumb() {
  const crumb = $("#crumb");
  if (state.mode === "agg") {
    crumb.innerHTML =
      `<span class="chip">overview · ${state.nodes.length} groups of ${state.totalNodes.toLocaleString()} nodes</span>` +
      `<button class="chip btn" id="btn-full">render all (slow)</button>`;
    $("#btn-full").addEventListener("click", () => loadGraph(state.gid, { mode: "full" }));
  } else if (state.mode === "focus") {
    crumb.innerHTML =
      `<button class="chip btn" id="btn-back">← overview</button>` +
      `<span class="chip">${esc(state.focusId || "")} · ${state.nodes.length} nodes</span>`;
    $("#btn-back").addEventListener("click", () => loadGraph(state.gid));
  } else {
    crumb.innerHTML = "";
  }
}

function applyDelta(d) {
  if (d.graph !== state.gid) return;
  if (state.mode !== "full") {
    // Agg/focus views: refetch the compact view instead of patching ids
    // that may not exist in this projection.
    loadGraph(state.gid, { keepCamera: true, mode: state.mode === "agg" ? "agg" : undefined, focus: state.focusId || undefined });
    feedCard({ kind: "update", graph: d.graph, question: `graph updated: +${(d.added_nodes || []).length}/-${(d.removed_nodes || []).length} nodes`, ts: new Date().toISOString() });
    return;
  }
  const now = performance.now();
  for (const n of d.added_nodes || []) {
    if (!state.byId.has(n.id)) {
      state.nodes.push(n);
      state.spawn.set(n.id, now);
    }
  }
  const addedIds = new Set((d.added_nodes || []).map((n) => n.id));
  for (const e of d.added_edges || []) {
    state.edges.push(e);
    if (!addedIds.has(idOf(e.source))) state.flash.set(idOf(e.source), now);
  }
  const removedKeys = new Set(
    (d.removed_edges || []).map((e) => `${e.source}|${e.target}|${e.relation}`)
  );
  if (removedKeys.size) {
    state.edges = state.edges.filter(
      (e) => !removedKeys.has(`${idOf(e.source)}|${idOf(e.target)}|${e.relation}`)
    );
  }
  for (const id of d.removed_nodes || []) state.removing.set(id, now);
  if (state.frozen) {
    Graph.cooldownTicks(120);
    Graph.d3ReheatSimulation();
  }
  pushData();
  feedCard({
    kind: "update",
    graph: d.graph,
    question: `graph updated: +${(d.added_nodes || []).length} nodes, +${(d.added_edges || []).length} edges, -${(d.removed_nodes || []).length} nodes`,
    ts: new Date().toISOString(),
  });
}

function reallyRemove(ids) {
  const drop = new Set(ids);
  for (const id of ids) state.removing.delete(id);
  state.nodes = state.nodes.filter((n) => !drop.has(n.id));
  state.edges = state.edges.filter(
    (e) => !drop.has(idOf(e.source)) && !drop.has(idOf(e.target))
  );
  pushData();
}

function applyRead(r) {
  feedCard(r);
  if (r.graph !== state.gid) return;
  const now = performance.now();
  const ids = state.mode === "agg" ? (r.agg_ids || []) : (r.node_ids || []);
  if (!ids.length) return;
  const hot = new Set(ids);
  for (const id of ids) if (state.byId.has(id)) state.flash.set(id, now);
  for (const e of state.edges) {
    e._hot = hot.has(idOf(e.source)) && hot.has(idOf(e.target));
  }
  Graph.linkWidth(Graph.linkWidth());
  Graph.linkDirectionalParticles(Graph.linkDirectionalParticles());
  setTimeout(() => {
    for (const e of state.edges) e._hot = false;
    Graph.linkWidth(Graph.linkWidth());
    Graph.linkDirectionalParticles(Graph.linkDirectionalParticles());
  }, FLASH_MS);
}

/* ---------- UI ---------- */

function updateStats() {
  const label = state.mode === "agg"
    ? `${state.nodes.length} groups · ${state.totalNodes.toLocaleString()} nodes`
    : `${state.nodes.length.toLocaleString()} nodes · ${state.edges.length.toLocaleString()} edges`;
  $("#stats").textContent = label;
}

/* The merged global graph is the default view, so it is pinned to the top of
 * the selector instead of sorting alphabetically into the middle. */
function orderGraphs(graphs) {
  const global = graphs.filter((g) => g.id === DEFAULT_GRAPH);
  const rest = graphs.filter((g) => g.id !== DEFAULT_GRAPH);
  return [...global, ...rest];
}

function refreshSelect(graphs) {
  const sel = $("#graph-select");
  const cur = state.gid;
  sel.innerHTML = "";
  for (const g of orderGraphs(graphs)) {
    const opt = document.createElement("option");
    opt.value = g.id;
    opt.textContent = `${g.id} (${g.nodes.toLocaleString()})`;
    sel.appendChild(opt);
  }
  if (cur && graphs.some((g) => g.id === cur)) sel.value = cur;
}

$("#graph-select").addEventListener("change", (e) => loadGraph(e.target.value));

$("#search").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  const q = e.target.value.trim();
  if (!q) return;
  const lq = q.toLowerCase();
  if (state.mode === "full") {
    const n =
      state.nodes.find((x) => (x.label || "").toLowerCase() === lq) ||
      state.nodes.find((x) => (x.label || "").toLowerCase().includes(lq));
    if (n) focusNode(n);
    return;
  }
  // agg/focus: resolve on the server, drill into the node's community
  const res = await fetch(`/api/find/${encodeURIComponent(state.gid)}?q=${encodeURIComponent(q)}`);
  if (!res.ok) return;
  const hit = await res.json();
  if (!hit.found) return;
  await loadGraph(state.gid, { focus: hit.agg_id });
  const n = state.byId.get(hit.node.id);
  if (n) focusNode(n);
});

function focusNode(n) {
  const d = 120;
  const ratio = 1 + d / Math.hypot(n.x || 1, n.y || 1, n.z || 1);
  Graph.cameraPosition(
    { x: (n.x || 0) * ratio, y: (n.y || 0) * ratio, z: (n.z || 0) * ratio },
    n, 900
  );
  state.flash.set(n.id, performance.now());
  showPanel(n);
}

function onNodeClick(n) {
  if (state.mode === "agg") {
    loadGraph(state.gid, { focus: n.id });
    return;
  }
  showPanel(n);
}

function showPanel(n) {
  $("#sp-title").textContent = n.label || n.id;
  const rows = [
    ["file", n.source_file || "—"],
    ["location", n.source_location || "—"],
    ["community", n.community_name || n.community],
    ["repo", n.repo || "—"],
    ["degree", n._deg || 0],
  ];
  let html = '<dl class="kv">';
  for (const [k, v] of rows) html += `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`;
  html += "</dl><div><b>Connections</b></div>";
  const neigh = [];
  for (const e of state.edges) {
    const s = idOf(e.source), t = idOf(e.target);
    if (s === n.id && state.byId.has(t)) neigh.push([t, `→ ${e.relation || ""}`]);
    else if (t === n.id && state.byId.has(s)) neigh.push([s, `← ${e.relation || ""}`]);
    if (neigh.length >= 40) break;
  }
  for (const [id, rel] of neigh) {
    const m = state.byId.get(id);
    html += `<button class="neigh" data-id="${esc(id)}"><span class="rel">${esc(rel)}</span> ${esc(m.label || id)}</button>`;
  }
  $("#sp-body").innerHTML = html;
  $("#sp-body").querySelectorAll(".neigh").forEach((b) =>
    b.addEventListener("click", () => {
      const m = state.byId.get(b.dataset.id);
      if (m) focusNode(m);
    })
  );
  $("#side-panel").classList.remove("hidden");
}
function hidePanel() { $("#side-panel").classList.add("hidden"); }
$("#sp-close").addEventListener("click", hidePanel);

$("#feed-toggle").addEventListener("click", () => {
  $("#feed").classList.toggle("collapsed");
  $("#feed-toggle").textContent = $("#feed").classList.contains("collapsed") ? "+" : "–";
});

function feedCard(r) {
  const list = $("#feed-list");
  const card = document.createElement("div");
  card.className = "card";
  const kind = r.kind || "query";
  const time = (r.ts || "").slice(11, 19) || new Date().toISOString().slice(11, 19);
  const meta = [];
  if (r.nodes_returned != null) meta.push(`${r.nodes_returned} nodes`);
  if (r.duration_ms != null) meta.push(`${Math.round(r.duration_ms)} ms`);
  card.innerHTML =
    `<div class="row1"><span class="badge ${esc(kind)}">${esc(kind)}</span>` +
    `<span class="graph">${esc(r.graph || "")}</span><span class="time">${esc(time)}</span></div>` +
    `<div class="q">${esc(r.question || "")}</div>` +
    (meta.length ? `<div class="meta">${esc(meta.join(" · "))}</div>` : "");
  card.addEventListener("click", async () => {
    if (r.graph && r.graph !== state.gid) await loadGraph(r.graph);
    applyRead({ ...r, ts: undefined });
    const pool = state.mode === "agg" ? (r.agg_ids || []) : (r.node_ids || []);
    const first = pool.find((id) => state.byId.has(id));
    if (first) focusNode(state.byId.get(first));
  });
  list.prepend(card);
  while (list.children.length > MAX_FEED) list.lastChild.remove();
}

/* ---------- SSE ---------- */

function connect() {
  const es = new EventSource("/events");
  es.addEventListener("hello", (e) => {
    $("#conn").textContent = "live";
    $("#conn").style.color = "#43b581";
    const graphs = JSON.parse(e.data).graphs || [];
    refreshSelect(graphs);
    if (!state.gid && graphs.length) {
      // Open on the merged global graph (aggregated overview) by default.
      const prefer = graphs.find((g) => g.id === DEFAULT_GRAPH) || graphs[0];
      loadGraph(prefer.id);
    }
  });
  es.addEventListener("graphs", (e) => refreshSelect(JSON.parse(e.data).graphs || []));
  es.addEventListener("graph_delta", (e) => applyDelta(JSON.parse(e.data)));
  es.addEventListener("graph_reload", (e) => {
    const d = JSON.parse(e.data);
    if (d.graph === state.gid) {
      loadGraph(state.gid, {
        keepCamera: true,
        mode: state.mode === "agg" ? "agg" : undefined,
        focus: state.focusId || undefined,
      });
    }
    feedCard({ kind: "update", graph: d.graph, question: `graph rebuilt: ${d.nodes} nodes, ${d.edges} edges`, ts: new Date().toISOString() });
  });
  es.addEventListener("read", (e) => applyRead(JSON.parse(e.data)));
  es.onerror = () => {
    $("#conn").textContent = "reconnecting";
    $("#conn").style.color = "#faa61a";
  };
}

connect();
