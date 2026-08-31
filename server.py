#!/usr/bin/env python3
"""GraphPulse server.

Serves the 3D viewer frontend, discovers graphify graphs, watches them for
changes (structural diffs) and tails the graphify query log (read events),
streaming everything to browsers over Server-Sent Events.

Stdlib only. Windows/macOS/Linux.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
FRONTEND = APP_DIR / "frontend"

# Graphs larger than this are served aggregated (repo x community super-nodes)
# unless the client explicitly asks for mode=full.
AGG_THRESHOLD = 6000

# ---------------------------------------------------------------------------
# Event bus


class Bus:
    """Fan-out of JSON events to every connected SSE client."""

    def __init__(self) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def publish(self, event: str, data: dict) -> None:
        msg = (event, json.dumps(data, ensure_ascii=False))
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # slow client; it will resync on reconnect


BUS = Bus()

# ---------------------------------------------------------------------------
# Graph registry + watcher


def _graph_id(path: Path) -> str:
    """Stable id for a graph file: its project folder name (or 'global')."""
    if path.name == "global-graph.json":
        return "global"
    # <project>/graphify-out/graph.json -> <project>
    return path.parent.parent.name


def _load_graph(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    nodes = data.get("nodes") or []
    edges = data.get("links") or data.get("edges") or []
    return {"nodes": nodes, "edges": edges, "directed": bool(data.get("directed"))}


# mtime-keyed cache so big graphs (global: ~50 MB JSON) are not re-parsed
# on every API hit.
_GRAPH_CACHE: dict[str, tuple[float, dict]] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def _load_graph_cached(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    with _GRAPH_CACHE_LOCK:
        hit = _GRAPH_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    graph = _load_graph(path)
    if graph is not None:
        with _GRAPH_CACHE_LOCK:
            _GRAPH_CACHE[key] = (mtime, graph)
            while len(_GRAPH_CACHE) > 6:  # keep the working set small
                _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)))
    return graph


def _agg_key(n: dict) -> str:
    return f"{n.get('repo') or ''}|{n.get('community', '')}"


def aggregate_graph(graph: dict) -> dict:
    """Collapse nodes into (repo, community) super-nodes.

    Turns the 32k-node global graph into a few hundred renderable objects.
    """
    groups: dict[str, dict] = {}
    member_of: dict[str, str] = {}
    for n in graph["nodes"]:
        key = _agg_key(n)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "id": key,
                "repo": n.get("repo") or "",
                "community": n.get("community", ""),
                "community_name": n.get("community_name") or "",
                "count": 0,
                "top": [],  # a few sample labels for the tooltip
            }
        g["count"] += 1
        if len(g["top"]) < 5 and n.get("label"):
            g["top"].append(n["label"])
        member_of[n.get("id")] = key

    agg_edges: dict[tuple[str, str], int] = {}
    for e in graph["edges"]:
        s = member_of.get(e.get("source") if not isinstance(e.get("source"), dict) else e["source"].get("id"))
        t = member_of.get(e.get("target") if not isinstance(e.get("target"), dict) else e["target"].get("id"))
        if not s or not t or s == t:
            continue
        k = (s, t) if s <= t else (t, s)
        agg_edges[k] = agg_edges.get(k, 0) + 1

    for g in groups.values():
        g["label"] = (
            f"{g['repo'] or 'graph'} · "
            + (g["community_name"] or f"community {g['community']}")
        )
    return {
        "mode": "agg",
        "nodes": list(groups.values()),
        "edges": [
            {"source": s, "target": t, "weight": w}
            for (s, t), w in agg_edges.items()
        ],
    }


def focus_graph(graph: dict, agg_id: str) -> dict:
    """Full detail for one (repo, community) group + one hop of neighbors."""
    members = {n.get("id") for n in graph["nodes"] if _agg_key(n) == agg_id}
    keep_nodes: dict[str, dict] = {}
    keep_edges: list[dict] = []
    for e in graph["edges"]:
        s, t = e.get("source"), e.get("target")
        s = s.get("id") if isinstance(s, dict) else s
        t = t.get("id") if isinstance(t, dict) else t
        if s in members or t in members:
            keep_edges.append(e)
    wanted = set(members)
    for e in keep_edges:
        s, t = e.get("source"), e.get("target")
        s = s.get("id") if isinstance(s, dict) else s
        t = t.get("id") if isinstance(t, dict) else t
        wanted.add(s)
        wanted.add(t)
    for n in graph["nodes"]:
        nid = n.get("id")
        if nid in wanted:
            keep_nodes[nid] = dict(n, _in_focus=nid in members)
    return {
        "mode": "focus",
        "focus": agg_id,
        "nodes": list(keep_nodes.values()),
        "edges": keep_edges,
    }


class GraphEntry:
    def __init__(self, gid: str, path: Path):
        self.gid = gid
        self.path = path
        self.mtime = 0.0
        self.size = -1
        self.node_ids: set[str] = set()
        self.edge_keys: set[tuple] = set()
        self.node_count = 0
        self.edge_count = 0
        self.label_to_id: dict[str, str] = {}
        # id -> (repo, community) for aggregate views / read mapping
        self.node_meta: dict[str, tuple[str, str]] = {}

    def snapshot_sets(self, graph: dict) -> None:
        self.node_ids = {n.get("id") for n in graph["nodes"]}
        self.edge_keys = {
            (e.get("source"), e.get("target"), e.get("relation"))
            for e in graph["edges"]
        }
        self.node_count = len(graph["nodes"])
        self.edge_count = len(graph["edges"])
        lbl: dict[str, str] = {}
        meta: dict[str, tuple[str, str]] = {}
        for n in graph["nodes"]:
            for key in (n.get("label"), n.get("norm_label")):
                if key:
                    lbl.setdefault(str(key).lower(), n.get("id"))
            meta[n.get("id")] = (
                str(n.get("repo") or ""),
                str(n.get("community", "")),
            )
        self.label_to_id = lbl
        self.node_meta = meta

    def agg_id_for(self, node_id: str) -> str | None:
        m = self.node_meta.get(node_id)
        if m is None:
            return None
        return f"{m[0]}|{m[1]}"


class Registry:
    """Discovers graphs under roots and diffs them on change."""

    # Deltas bigger than this fraction of the graph trigger a full reload
    # event instead (the client refetches - cheaper than a huge SSE payload).
    DELTA_FRACTION = 0.35

    def __init__(self, roots: list[Path], global_graph: Path | None):
        self.roots = roots
        self.global_graph = global_graph
        self.entries: dict[str, GraphEntry] = {}
        self._lock = threading.Lock()

    # -- discovery ---------------------------------------------------------
    def discover(self) -> bool:
        found: dict[str, Path] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                children = sorted(root.iterdir())
            except OSError:
                continue
            for child in children:
                gj = child / "graphify-out" / "graph.json"
                if gj.is_file():
                    found[_graph_id(gj)] = gj
        if self.global_graph and self.global_graph.is_file():
            found["global"] = self.global_graph

        changed = False
        with self._lock:
            for gid, path in found.items():
                if gid not in self.entries:
                    self.entries[gid] = GraphEntry(gid, path)
                    changed = True
            for gid in list(self.entries):
                if gid not in found:
                    del self.entries[gid]
                    changed = True
        return changed

    def listing(self) -> list[dict]:
        with self._lock:
            entries = sorted(self.entries.values(), key=lambda e: e.gid.lower())
            return [
                {
                    "id": e.gid,
                    "path": str(e.path),
                    "nodes": e.node_count,
                    "edges": e.edge_count,
                }
                for e in entries
            ]

    def get(self, gid: str) -> GraphEntry | None:
        with self._lock:
            return self.entries.get(gid)

    # -- watching ----------------------------------------------------------
    def poll_once(self) -> None:
        with self._lock:
            entries = list(self.entries.values())
        for entry in entries:
            try:
                st = entry.path.stat()
            except OSError:
                continue
            if st.st_mtime == entry.mtime and st.st_size == entry.size:
                continue
            graph = _load_graph(entry.path)
            if graph is None:  # mid-write; retry next tick
                continue
            first_load = entry.size == -1
            old_nodes, old_edges = entry.node_ids, entry.edge_keys
            entry.mtime, entry.size = st.st_mtime, st.st_size

            new_node_ids = {n.get("id") for n in graph["nodes"]}
            new_edge_keys = {
                (e.get("source"), e.get("target"), e.get("relation"))
                for e in graph["edges"]
            }
            added_nodes = [
                n for n in graph["nodes"] if n.get("id") not in old_nodes
            ]
            removed_nodes = sorted(old_nodes - new_node_ids)
            added_edges = [
                e
                for e in graph["edges"]
                if (e.get("source"), e.get("target"), e.get("relation"))
                not in old_edges
            ]
            removed_edges = sorted(
                old_edges - new_edge_keys, key=lambda k: (str(k[0]), str(k[1]))
            )
            entry.snapshot_sets(graph)

            if first_load:
                BUS.publish("graphs", {"graphs": self.listing()})
                continue
            if not (added_nodes or removed_nodes or added_edges or removed_edges):
                continue

            total = max(1, entry.node_count)
            churn = (len(added_nodes) + len(removed_nodes)) / total
            if churn > self.DELTA_FRACTION:
                BUS.publish(
                    "graph_reload",
                    {"graph": entry.gid, "nodes": entry.node_count,
                     "edges": entry.edge_count},
                )
            else:
                BUS.publish(
                    "graph_delta",
                    {
                        "graph": entry.gid,
                        "added_nodes": added_nodes,
                        "removed_nodes": removed_nodes,
                        "added_edges": added_edges,
                        "removed_edges": [
                            {"source": s, "target": t, "relation": r}
                            for (s, t, r) in removed_edges
                        ],
                        "nodes": entry.node_count,
                        "edges": entry.edge_count,
                    },
                )
            BUS.publish("graphs", {"graphs": self.listing()})


# ---------------------------------------------------------------------------
# Query-log tail -> read events

NODE_LINE = re.compile(r"^NODE\s+(.+?)\s+\[src=", re.M)
NODE_HEADER = re.compile(r"^Node:\s+(.+?)\s*$", re.M)
START_LIST = re.compile(r"Start:\s*\[([^\]]*)\]")
PATH_SEG = re.compile(r"(?:^|-->|<--)\s*([^-<>\n][^-<>\n]*?)\s*(?:--|$)")


def _labels_from_response(kind: str, response: str) -> list[str]:
    labels: list[str] = []
    labels += NODE_LINE.findall(response)
    labels += NODE_HEADER.findall(response)
    m = START_LIST.search(response)
    if m:
        for part in m.group(1).split(","):
            part = part.strip().strip("'\"")
            if part:
                labels.append(part)
    if kind == "path":
        for line in response.splitlines():
            if "--" in line and ("-->" in line or "<--" in line):
                for seg in re.split(r"--[a-z_]+(?:\s*\[[A-Z]+\])?-->|<--[a-z_]+(?:\s*\[[A-Z]+\])?--", line):
                    seg = seg.strip()
                    if seg and not seg.startswith("Shortest path"):
                        labels.append(seg)
    seen: set[str] = set()
    out: list[str] = []
    for lbl in labels:
        low = lbl.lower()
        if low not in seen:
            seen.add(low)
            out.append(lbl)
    return out[:80]


class QueryLogTail(threading.Thread):
    def __init__(self, path: Path, registry: Registry, poll: float = 0.5):
        super().__init__(daemon=True, name="querylog-tail")
        self.path = path
        self.registry = registry
        self.poll = poll
        # Ring buffer of the most recent emitted events, so a page that opens
        # (or reconnects) is not stuck with an empty Activity feed until the
        # next query happens.
        self.recent: list[dict] = []
        self._recent_lock = threading.Lock()
        # Skip only history that predates the server; a file that appears
        # later is entirely live and must be read from offset 0.
        try:
            self.pos = self.path.stat().st_size
        except OSError:
            self.pos = 0

    def history(self, limit: int = 30) -> list[dict]:
        with self._recent_lock:
            return list(self.recent[-limit:])

    def run(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.poll)

    def _tick(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            self.pos = 0
            return
        if size < self.pos:  # rotated/truncated
            self.pos = 0
        if size == self.pos:
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.pos)
            chunk = fh.read()
            self.pos = fh.tell()
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._emit(rec)

    def _emit(self, rec: dict) -> None:
        corpus = str(rec.get("corpus") or "")
        gid = None
        norm = corpus.replace("\\", "/")
        for entry_id, entry in list(self.registry.entries.items()):
            if str(entry.path).replace("\\", "/") == norm:
                gid = entry_id
                break
        if gid is None:
            # corpus may be the project dir rather than the graph file
            for entry_id, entry in list(self.registry.entries.items()):
                proj = str(entry.path.parent.parent).replace("\\", "/")
                if norm.startswith(proj):
                    gid = entry_id
                    break
        labels = _labels_from_response(
            str(rec.get("kind") or ""), str(rec.get("response") or "")
        )
        node_ids: list[str] = []
        agg_ids: list[str] = []
        entry = self.registry.get(gid) if gid else None
        if entry:
            seen_agg: set[str] = set()
            for lbl in labels:
                nid = entry.label_to_id.get(lbl.lower())
                if nid:
                    node_ids.append(nid)
                    aid = entry.agg_id_for(nid)
                    if aid and aid not in seen_agg:
                        seen_agg.add(aid)
                        agg_ids.append(aid)
        payload = {
            "graph": gid,
            "kind": rec.get("kind"),
            "question": rec.get("question"),
            "ts": rec.get("ts"),
            "nodes_returned": rec.get("nodes_returned"),
            "duration_ms": rec.get("duration_ms"),
            "labels": labels[:25],
            "node_ids": node_ids[:60],
            "agg_ids": agg_ids[:30],
        }
        with self._recent_lock:
            self.recent.append(payload)
            del self.recent[:-60]
        BUS.publish("read", payload)


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    registry: Registry = None  # type: ignore[assignment]
    tail: "QueryLogTail | None" = None

    def log_message(self, fmt: str, *args) -> None:  # quiet
        pass

    def handle(self) -> None:
        # Swallow client disconnects (SSE tabs closing) instead of letting
        # socketserver print a full traceback for each one.
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The frontend is edited live; without this the browser keeps serving a
        # stale app.js from cache and fixes appear to have no effect.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route in ("/", "/index.html"):
            self._send_file(FRONTEND / "index.html", "text/html; charset=utf-8")
        elif route == "/app.js":
            self._send_file(FRONTEND / "app.js", "text/javascript; charset=utf-8")
        elif route == "/style.css":
            self._send_file(FRONTEND / "style.css", "text/css; charset=utf-8")
        elif route == "/vendor/3d-force-graph.min.js":
            self._send_file(
                FRONTEND / "vendor" / "3d-force-graph.min.js",
                "text/javascript; charset=utf-8",
            )
        elif route == "/api/graphs":
            self._send_json({"graphs": self.registry.listing()})
        elif route.startswith("/api/find/"):
            # /api/find/<gid>?q=label  -> node + its agg group (for search in agg mode)
            gid = route[len("/api/find/"):]
            entry = self.registry.get(gid)
            if not entry:
                self._send_json({"error": f"unknown graph {gid!r}"}, 404)
                return
            from urllib.parse import parse_qs
            q = (parse_qs(parsed.query).get("q") or [""])[0].strip().lower()
            if not q:
                self._send_json({"error": "missing q"}, 400)
                return
            graph = _load_graph_cached(entry.path)
            if graph is None:
                self._send_json({"error": "graph unreadable"}, 503)
                return
            exact, partial = None, None
            for n in graph["nodes"]:
                lbl = str(n.get("label") or "").lower()
                if lbl == q:
                    exact = n
                    break
                if partial is None and q in lbl:
                    partial = n
            n = exact or partial
            if not n:
                self._send_json({"found": False})
                return
            self._send_json({"found": True, "node": n, "agg_id": _agg_key(n)})
        elif route.startswith("/api/graph/"):
            gid = unquote(route[len("/api/graph/"):])
            entry = self.registry.get(gid)
            if not entry:
                self._send_json({"error": f"unknown graph {gid!r}"}, 404)
                return
            graph = _load_graph_cached(entry.path)
            if graph is None:
                self._send_json({"error": "graph unreadable"}, 503)
                return
            from urllib.parse import parse_qs
            params = parse_qs(parsed.query)
            focus = (params.get("focus") or [None])[0]
            mode = (params.get("mode") or [None])[0]
            n_nodes = len(graph["nodes"])
            if focus:
                self._send_json(dict(focus_graph(graph, unquote(focus)), id=gid))
            elif mode == "full" or (mode != "agg" and n_nodes <= AGG_THRESHOLD):
                self._send_json(
                    {
                        "id": gid,
                        "mode": "full",
                        "directed": graph["directed"],
                        "nodes": graph["nodes"],
                        "edges": graph["edges"],
                    }
                )
            else:
                self._send_json(dict(aggregate_graph(graph), id=gid, total_nodes=n_nodes))
        elif route == "/events":
            self._sse()
        else:
            self._send_json({"error": "not found"}, 404)

    def _sse(self) -> None:
        q = BUS.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            hello = json.dumps({
                "graphs": self.registry.listing(),
                "recent_reads": self.tail.history() if self.tail else [],
            })
            self.wfile.write(f"event: hello\ndata: {hello}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)


# ---------------------------------------------------------------------------
# main


def main() -> None:
    ap = argparse.ArgumentParser(description="GraphPulse - live 3D graphify viewer")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--roots",
        default="D:/Hermes;D:/AI",
        help="';'-separated directories whose children are scanned for graphify-out/graph.json",
    )
    ap.add_argument(
        "--global-graph",
        default=str(Path.home() / ".graphify" / "global-graph.json"),
    )
    ap.add_argument(
        "--querylog",
        default=str(Path.home() / ".graphify" / "queries.log"),
        help="graphify query log (set GRAPHIFY_QUERY_LOG to the same path)",
    )
    ap.add_argument("--rescan", type=float, default=30.0)
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args()

    roots = [Path(r.strip()) for r in args.roots.split(";") if r.strip()]
    registry = Registry(roots, Path(args.global_graph))
    registry.discover()
    registry.poll_once()  # initial snapshot (also fills label maps)

    def watch() -> None:
        last_rescan = time.time()
        while True:
            time.sleep(args.poll)
            try:
                if time.time() - last_rescan >= args.rescan:
                    last_rescan = time.time()
                    if registry.discover():
                        BUS.publish("graphs", {"graphs": registry.listing()})
                registry.poll_once()
            except Exception:
                pass

    threading.Thread(target=watch, daemon=True, name="graph-watch").start()

    Handler.registry = registry
    tail = QueryLogTail(Path(args.querylog), registry)
    Handler.tail = tail
    tail.start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"GraphPulse: http://{args.host}:{args.port}  "
          f"({len(registry.listing())} graphs, querylog={args.querylog})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
