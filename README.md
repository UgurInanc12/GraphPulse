# GraphPulse

Live 3D viewer for [graphify](https://github.com/Graphify-Labs/graphify) knowledge graphs.

The stock `graph.html` that graphify exports is a static snapshot. GraphPulse turns the same
`graph.json` files into a living picture: a full-screen 3D force graph that updates itself while
you work.

- **Live structure**: watches every `graphify-out/graph.json` under your project roots (plus the
  global graph). When a graph is rebuilt (`graphify update`, `graphify extract`), new nodes pop in
  with a spawn animation and removed nodes fade out. No reload.
- **Live reads**: tails the graphify query log. Every `graphify query` / `path` / `explain` an
  agent or human runs makes the touched nodes flash in the scene and lands as a card in the
  activity drawer, so you can watch an AI walk your codebase in real time.
- **Full 3D navigation**: rotate (left drag), pan (right drag), zoom (wheel), click a node for a
  side panel with its file, location, community, and clickable neighbors. Search flies the camera
  to any node.
- **Multi-project**: a selector lists every discovered graph; switch without restarting.

No build step, no npm, no external Python packages. One stdlib server, one vendored
[3d-force-graph](https://github.com/vasturiano/3d-force-graph) bundle.

## Quick start

```
python server.py --roots "D:/Hermes;D:/AI" --port 8123
```

Open http://127.0.0.1:8123

### See live reads

graphify only writes its query log when asked to (off by default for privacy). Enable it for your
user so every future `graphify` call logs:

```
setx GRAPHIFY_QUERY_LOG "%USERPROFILE%\.graphify\queries.log"
setx GRAPHIFY_QUERY_LOG_RESPONSES 1
```

(`GRAPHIFY_QUERY_LOG_RESPONSES=1` is what lets GraphPulse know *which* nodes a query touched,
not just that a query happened.)

Then run any query and watch the scene:

```
cd <some-project>
graphify query "what calls the download engine"
```

## CLI

```
python server.py [--port 8123]
                 [--roots "D:/Hermes;D:/AI"]        # ; separated scan roots
                 [--global-graph ~/.graphify/global-graph.json]
                 [--querylog ~/.graphify/queries.log]
                 [--rescan 30]                       # seconds between root rescans
                 [--poll 1.0]                        # seconds between graph mtime checks
```

## How it works

| Piece | Mechanism |
|---|---|
| Discovery | scans `<root>/*/graphify-out/graph.json` + the global graph |
| Change detection | mtime poll, then a node/edge key diff; small changes stream as deltas, big ones trigger a reload event |
| Read detection | size-poll tail of the query-log JSONL; node labels are parsed from the logged response (`NODE ...` lines, `Node:` headers, BFS `Start: [...]` lists) and resolved to node ids |
| Transport | Server-Sent Events (`/events`), heartbeat every 15 s, auto-reconnect |
| Rendering | 3d-force-graph v1.77 (bundles three.js), community-colored nodes, EXTRACTED vs INFERRED edge opacity |

## Endpoints

- `GET /api/graphs` - discovered graphs with counts
- `GET /api/graph/<id>` - full graph payload for the viewer
- `GET /events` - SSE stream (`graphs`, `graph_delta`, `graph_reload`, `read`, `hello`)

## Third-party

- [3d-force-graph](https://github.com/vasturiano/3d-force-graph) (MIT, vendored in
  `frontend/vendor/`), which bundles [three.js](https://threejs.org) (MIT).

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
