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

    def snapshot_sets(self, graph: dict) -> None:
        self.node_ids = {n.get("id") for n in graph["nodes"]}
        self.edge_keys = {
            (e.get("source"), e.get("target"), e.get("relation"))
            for e in graph["edges"]
        }
        self.node_count = len(graph["nodes"])
        self.edge_count = len(graph["edges"])
        lbl: dict[str, str] = {}
        for n in graph["nodes"]:
            for key in (n.get("label"), n.get("norm_label")):
                if key:
                    lbl.setdefault(str(key).lower(), n.get("id"))
        self.label_to_id = lbl


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
        # Skip only history that predates the server; a file that appears
        # later is entirely live and must be read from offset 0.
        try:
            self.pos = self.path.stat().st_size
        except OSError:
            self.pos = 0

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
        entry = self.registry.get(gid) if gid else None
        if entry:
            for lbl in labels:
                nid = entry.label_to_id.get(lbl.lower())
                if nid:
                    node_ids.append(nid)
        BUS.publish(
            "read",
            {
                "graph": gid,
                "kind": rec.get("kind"),
                "question": rec.get("question"),
                "ts": rec.get("ts"),
                "nodes_returned": rec.get("nodes_returned"),
                "duration_ms": rec.get("duration_ms"),
                "labels": labels[:25],
                "node_ids": node_ids[:60],
            },
        )


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    registry: Registry = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:  # quiet
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
        elif route.startswith("/api/graph/"):
            gid = route[len("/api/graph/"):]
            entry = self.registry.get(gid)
            if not entry:
                self._send_json({"error": f"unknown graph {gid!r}"}, 404)
                return
            graph = _load_graph(entry.path)
            if graph is None:
                self._send_json({"error": "graph unreadable"}, 503)
                return
            self._send_json(
                {
                    "id": gid,
                    "directed": graph["directed"],
                    "nodes": graph["nodes"],
                    "edges": graph["edges"],
                }
            )
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
            hello = json.dumps({"graphs": self.registry.listing()})
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
    QueryLogTail(Path(args.querylog), registry).start()

    Handler.registry = registry
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"GraphPulse: http://{args.host}:{args.port}  "
          f"({len(registry.listing())} graphs, querylog={args.querylog})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
