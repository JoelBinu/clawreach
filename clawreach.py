#!/usr/bin/env python3
"""ClawReach — visualize the slice of the filesystem Claude Code has touched.

Single-file, stdlib-only. Parses ~/.claude/projects/**/*.jsonl, extracts every
path that any tool_use referenced, scans those paths plus their parents and
immediate siblings, and serves a D3 collapsible tree at http://127.0.0.1:PORT/.

Usage:
    python3 clawreach.py                       # default: port 8765
    python3 clawreach.py --port 9000
    python3 clawreach.py --projects ~/.claude/projects --root /
    python3 clawreach.py --full-home           # walk all of $HOME (slow)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_PORT = 8765

# Tools whose `input` carries a single absolute path under a well-known key.
PATH_KEY_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "NotebookRead": "notebook_path",
}

# Tools whose `input` carries a directory under `path` (Glob/Grep both optional).
DIR_KEY_TOOLS = {"Glob", "Grep", "LS"}

# Names of dirs we never want in the tree — they bury the signal in noise.
IGNORE_DIR_NAMES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".next", ".nuxt", "dist", "build", ".cache", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "target", ".gradle", ".idea", ".vscode",
    ".DS_Store", "Pods", "DerivedData",
}

# Heuristic: tokens that look like filesystem paths inside a Bash command.
# Matches absolute paths and tilde paths; conservative on relative paths to
# avoid pulling in random argv noise.
PATH_TOKEN_RE = re.compile(r"^(~?/[\w./\-+@:%]+)")


# ---------------------------------------------------------------------------
# Ingest — parse JSONL transcripts into AccessEvent records
# ---------------------------------------------------------------------------

@dataclass
class AccessEvent:
    path: str        # absolute, normalized
    tool: str
    ts: str          # ISO timestamp from the transcript
    session: str
    project: str     # the encoded project dir name (= cwd with / → -)
    is_sidechain: bool  # True if a subagent issued the call


def _expand(p: str, base: str | None = None) -> str | None:
    """Resolve to an absolute path. Returns None if it can't be made absolute."""
    if not p or not isinstance(p, str):
        return None
    p = p.strip()
    if p.startswith("~"):
        p = os.path.expanduser(p)
    if not os.path.isabs(p):
        if base:
            p = os.path.normpath(os.path.join(base, p))
        else:
            return None
    return os.path.normpath(p)


def _extract_bash_paths(command: str, cwd: str | None) -> list[str]:
    """Pull plausible filesystem paths out of a Bash command string.

    We use shlex to respect quoting, then keep tokens that look like paths.
    This is heuristic; it intentionally favors precision over recall — better
    to miss a path than to flood the tree with argv noise like `-la` or `HEAD`.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # unbalanced quotes etc. — fall back to whitespace split
        tokens = command.split()
    out: list[str] = []
    for tok in tokens:
        # strip leading shell redirects like `>` or `<`
        tok = tok.lstrip("<>")
        m = PATH_TOKEN_RE.match(tok)
        if not m:
            continue
        candidate = _expand(m.group(1), cwd)
        if candidate:
            out.append(candidate)
    return out


def _iter_tool_uses(entry: dict) -> Iterable[tuple[str, dict]]:
    """Yield (tool_name, input_dict) for every tool_use in an assistant entry."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if isinstance(inp, dict):
                yield name, inp


def parse_transcript(jsonl_path: Path) -> list[AccessEvent]:
    """Stream a single .jsonl transcript and return every AccessEvent in it."""
    project = jsonl_path.parent.name
    events: list[AccessEvent] = []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            ts = entry.get("timestamp") or ""
            session = entry.get("sessionId") or ""
            cwd = entry.get("cwd")
            sidechain = bool(entry.get("isSidechain"))
            for tool, inp in _iter_tool_uses(entry):
                paths = _paths_for_tool(tool, inp, cwd)
                for p in paths:
                    events.append(AccessEvent(p, tool, ts, session, project, sidechain))
    return events


def _paths_for_tool(tool: str, inp: dict, cwd: str | None) -> list[str]:
    """Map (tool, input) → list of absolute paths the call touched."""
    if tool in PATH_KEY_TOOLS:
        key = PATH_KEY_TOOLS[tool]
        p = _expand(inp.get(key), cwd)
        return [p] if p else []
    if tool in DIR_KEY_TOOLS:
        p = _expand(inp.get("path"), cwd) if inp.get("path") else cwd
        return [p] if p else []
    if tool == "Bash":
        cmd = inp.get("command")
        if isinstance(cmd, str):
            return _extract_bash_paths(cmd, cwd)
        return []
    return []


def ingest_all(projects_dir: Path) -> list[AccessEvent]:
    """Parse every transcript under projects_dir."""
    events: list[AccessEvent] = []
    if not projects_dir.exists():
        return events
    for jsonl in projects_dir.rglob("*.jsonl"):
        try:
            events.extend(parse_transcript(jsonl))
        except OSError:
            continue
    return events


# ---------------------------------------------------------------------------
# Aggregate — collapse events into per-path summaries
# ---------------------------------------------------------------------------

@dataclass
class PathStats:
    count: int = 0
    last_ts: str = ""
    tools: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sessions: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    sidechain_count: int = 0

    def add(self, ev: AccessEvent) -> None:
        self.count += 1
        if ev.ts > self.last_ts:
            self.last_ts = ev.ts
        self.tools[ev.tool] += 1
        self.sessions.add(ev.session)
        self.projects.add(ev.project)
        if ev.is_sidechain:
            self.sidechain_count += 1

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "last_ts": self.last_ts,
            "tools": dict(self.tools),
            "sessions": sorted(self.sessions),
            "projects": sorted(self.projects),
            "sidechain": self.sidechain_count,
        }


def aggregate(events: list[AccessEvent]) -> dict[str, PathStats]:
    stats: dict[str, PathStats] = {}
    for ev in events:
        stats.setdefault(ev.path, PathStats()).add(ev)
    return stats


# ---------------------------------------------------------------------------
# Scan — build the tree rooted at common ancestor, with parents + 1 sibling level
# ---------------------------------------------------------------------------

def _ancestors(path: str) -> list[str]:
    """All ancestor directories of `path`, root-first, including path itself."""
    parts: list[str] = []
    p = path
    while True:
        parts.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return list(reversed(parts))


def _common_root(paths: Iterable[str]) -> str:
    paths = list(paths)
    if not paths:
        return "/"
    try:
        return os.path.commonpath(paths)
    except ValueError:
        return "/"


def build_tree(
    stats: dict[str, PathStats],
    *,
    root: str | None = None,
    sibling_depth: int = 1,
    full_walk_root: str | None = None,
) -> dict:
    """Build the nested tree.

    - All accessed paths and their ancestors up to `root` are included.
    - For each accessed directory (or directory containing an accessed file),
      one level of siblings is added so users see "what's next to the file
      Claude touched."
    - If full_walk_root is set, walk that whole subtree (ignoring noise dirs).
    """
    accessed = set(stats.keys())
    if not accessed and not full_walk_root:
        return {"name": "(no Claude activity found)", "path": "", "type": "dir", "children": []}

    if root is None:
        root = _common_root(accessed) if accessed else "/"
    root = os.path.normpath(root)

    # Collect every node we want in the tree.
    nodes: set[str] = set()
    nodes.add(root)
    for p in accessed:
        if not p.startswith(root):
            continue
        for anc in _ancestors(p):
            if anc.startswith(root) or anc == root:
                nodes.add(anc)

    # Add siblings around each accessed dir / parent dir.
    sibling_targets: set[str] = set()
    for p in list(nodes):
        d = p if os.path.isdir(p) else os.path.dirname(p)
        sibling_targets.add(d)
    for d in sibling_targets:
        try:
            for name in os.listdir(d):
                if name in IGNORE_DIR_NAMES:
                    continue
                child = os.path.join(d, name)
                if child.startswith(root):
                    nodes.add(child)
        except OSError:
            continue

    # Optionally walk a full subtree as well.
    if full_walk_root:
        fw = os.path.normpath(full_walk_root)
        for dirpath, dirnames, filenames in os.walk(fw):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIR_NAMES]
            nodes.add(dirpath)
            for fn in filenames:
                nodes.add(os.path.join(dirpath, fn))

    return _materialize(root, nodes, stats)


def _materialize(root: str, nodes: set[str], stats: dict[str, PathStats]) -> dict:
    """Turn a flat set of paths into a nested tree, attaching access stats."""
    # children index: parent -> list of immediate child paths
    by_parent: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        if n == root:
            continue
        parent = os.path.dirname(n)
        # walk up if parent missing from nodes (shouldn't happen, but defensive)
        while parent and parent not in nodes and parent != root:
            nodes.add(parent)
            parent = os.path.dirname(parent)
        by_parent[parent].append(n)

    def node(path: str) -> dict:
        try:
            is_dir = os.path.isdir(path)
        except OSError:
            is_dir = False
        st = stats.get(path)
        out: dict = {
            "name": os.path.basename(path) or path,
            "path": path,
            "type": "dir" if is_dir else "file",
        }
        if st:
            out["access"] = st.to_dict()
        kids = sorted(by_parent.get(path, []), key=lambda p: (not os.path.isdir(p), p.lower()))
        if kids:
            out["children"] = [node(k) for k in kids]
        return out

    return node(root)


# ---------------------------------------------------------------------------
# Server — stdlib HTTP, serves frontend + JSON API
# ---------------------------------------------------------------------------

class _Cache:
    """Thread-safe cache of the last scan. Re-scans on demand."""

    def __init__(self, projects_dir: Path, root: str | None, full_walk_root: str | None):
        self.projects_dir = projects_dir
        self.root = root
        self.full_walk_root = full_walk_root
        self._lock = threading.Lock()
        self._tree: dict | None = None
        self._meta: dict = {}

    def get(self) -> tuple[dict, dict]:
        with self._lock:
            if self._tree is None:
                self._rescan_locked()
            return self._tree, dict(self._meta)

    def rescan(self) -> tuple[dict, dict]:
        with self._lock:
            self._rescan_locked()
            return self._tree, dict(self._meta)

    def _rescan_locked(self) -> None:
        t0 = time.time()
        events = ingest_all(self.projects_dir)
        stats = aggregate(events)
        tree = build_tree(stats, root=self.root, full_walk_root=self.full_walk_root)
        self._tree = tree
        self._meta = {
            "event_count": len(events),
            "unique_paths": len(stats),
            "transcripts_dir": str(self.projects_dir),
            "root": tree.get("path", ""),
            "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_ms": int((time.time() - t0) * 1000),
        }


def make_handler(cache: _Cache, html: str):
    class Handler(BaseHTTPRequestHandler):
        # Quiet the default request logging — keep stdout for actual signal.
        def log_message(self, fmt, *args):  # noqa: N802
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/tree":
                tree, meta = cache.get()
                self._send_json({"tree": tree, "meta": meta})
                return
            if self.path == "/api/rescan":
                tree, meta = cache.rescan()
                self._send_json({"tree": tree, "meta": meta})
                return
            self.send_response(404)
            self.end_headers()

    return Handler


# ---------------------------------------------------------------------------
# Frontend — single embedded HTML page, D3 collapsible tree
# ---------------------------------------------------------------------------

FRONTEND_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ClawReach — Claude's filesystem footprint</title>
<style>
  :root {
    --bg: #0b0d11;
    --panel: #14181f;
    --ink: #d8dee9;
    --muted: #6b7280;
    --rule: #1f2630;
    --accent: #ff7a45;       /* Claude orange-ish */
    --accent-soft: #ffb088;
    --dir: #5b7cff;
    --untouched: #2a313b;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--ink);
    font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
    display: grid; grid-template-rows: auto 1fr; overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: 16px;
    padding: 10px 16px; border-bottom: 1px solid var(--rule);
    background: var(--panel);
  }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; letter-spacing: .5px; }
  header .meta { color: var(--muted); font-size: 12px; }
  header button {
    background: var(--rule); color: var(--ink); border: 1px solid var(--rule);
    padding: 4px 10px; border-radius: 4px; cursor: pointer; font: inherit;
  }
  header button:hover { background: #232b38; }
  main { display: grid; grid-template-columns: 1fr 320px; min-height: 0; }
  #viz { position: relative; overflow: hidden; }
  #viz svg { width: 100%; height: 100%; cursor: grab; }
  #viz svg:active { cursor: grabbing; }
  aside {
    border-left: 1px solid var(--rule); background: var(--panel);
    padding: 14px 16px; overflow: auto;
  }
  aside h2 { font-size: 12px; color: var(--muted); text-transform: uppercase;
             letter-spacing: 1px; margin: 0 0 8px; font-weight: 600; }
  aside .path { word-break: break-all; color: var(--accent-soft); margin-bottom: 12px; }
  aside dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0 0 14px; }
  aside dt { color: var(--muted); }
  aside dd { margin: 0; }
  aside .tools li { list-style: none; padding-left: 0; }
  aside ul { padding-left: 0; margin: 0; }
  .node circle { stroke: var(--rule); stroke-width: 1.2; cursor: pointer; }
  .node text {
    fill: var(--ink); font: 11px ui-monospace, monospace;
    paint-order: stroke; stroke: var(--bg); stroke-width: 3px; stroke-linejoin: round;
  }
  .node.untouched text { fill: var(--muted); }
  .link {
    fill: none; stroke: var(--rule); stroke-width: 1px;
  }
  .link.touched { stroke: var(--accent); stroke-opacity: .55; }
  .legend {
    position: absolute; bottom: 10px; left: 10px;
    background: rgba(20,24,31,.85); padding: 8px 10px; border-radius: 4px;
    border: 1px solid var(--rule); color: var(--muted); font-size: 11px;
  }
  .legend .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
                 vertical-align: middle; margin-right: 6px; }
</style>
</head>
<body>
<header>
  <h1>ClawReach</h1>
  <span class="meta" id="meta">loading…</span>
  <button id="rescan">Re-scan</button>
  <button id="reset">Reset view</button>
  <span class="meta" style="margin-left:auto">click a node to expand/collapse · drag to pan · scroll to zoom</span>
</header>
<main>
  <div id="viz">
    <svg></svg>
    <div class="legend">
      <div><span class="dot" style="background:var(--accent)"></span>touched by Claude</div>
      <div><span class="dot" style="background:var(--dir)"></span>directory (untouched)</div>
      <div><span class="dot" style="background:var(--untouched)"></span>file (untouched)</div>
      <div style="margin-top:4px">node size = access count</div>
    </div>
  </div>
  <aside id="details">
    <h2>Selection</h2>
    <div class="path" id="sel-path">— pick a node —</div>
    <div id="sel-body"></div>
  </aside>
</main>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const svg = d3.select("#viz svg");
const gZoom = svg.append("g");
const gLinks = gZoom.append("g").attr("class", "links");
const gNodes = gZoom.append("g").attr("class", "nodes");

const tree = d3.tree().nodeSize([18, 220]);
const zoom = d3.zoom().scaleExtent([0.1, 4]).on("zoom", e => gZoom.attr("transform", e.transform));
svg.call(zoom);

let root = null;
let initialTransform = null;

function totalAccess(d) {
  // sum of access.count over node + descendants — used to size aggregate nodes
  let s = d.data.access ? d.data.access.count : 0;
  if (d.children) d.children.forEach(c => { s += totalAccess(c); });
  if (d._children) d._children.forEach(c => { s += totalAccess(c); });
  return s;
}

function update(source) {
  const nodes = root.descendants();
  const links = root.links();
  tree(root);

  // collapse coords so root is at left
  let minX = Infinity, maxX = -Infinity;
  nodes.forEach(n => { if (n.x < minX) minX = n.x; if (n.x > maxX) maxX = n.x; });

  const linkSel = gLinks.selectAll("path.link").data(links, d => d.target.data.path);
  linkSel.enter().append("path")
      .attr("class", d => "link" + (d.target.data.access ? " touched" : ""))
    .merge(linkSel)
      .attr("d", d3.linkHorizontal().x(d => d.y).y(d => d.x));
  linkSel.exit().remove();

  const nodeSel = gNodes.selectAll("g.node").data(nodes, d => d.data.path);
  const enter = nodeSel.enter().append("g")
      .attr("class", d => "node" + (d.data.access ? " touched" : " untouched"))
      .attr("transform", d => `translate(${d.y},${d.x})`)
      .on("click", (_, d) => {
        if (d.children) { d._children = d.children; d.children = null; }
        else if (d._children) { d.children = d._children; d._children = null; }
        showDetails(d);
        update(d);
      });

  enter.append("circle")
      .attr("r", d => {
        const t = totalAccess(d);
        if (d.data.access) return Math.min(10, 3 + Math.log2(d.data.access.count + 1) * 1.6);
        if (t > 0) return 3 + Math.log2(t + 1) * 0.6;
        return 2.5;
      })
      .attr("fill", d => {
        if (d.data.access) return "var(--accent)";
        if (d.data.type === "dir") return "var(--dir)";
        return "var(--untouched)";
      });

  enter.append("text")
      .attr("dy", "0.32em")
      .attr("x", d => (d.children || d._children) ? -8 : 8)
      .attr("text-anchor", d => (d.children || d._children) ? "end" : "start")
      .text(d => d.data.name);

  const merged = enter.merge(nodeSel);
  merged.attr("transform", d => `translate(${d.y},${d.x})`);
  merged.select("circle").attr("fill", d => {
    if (d.data.access) return "var(--accent)";
    if (d.data.type === "dir") return "var(--dir)";
    return "var(--untouched)";
  });

  nodeSel.exit().remove();

  if (initialTransform === null) {
    const bbox = gZoom.node().getBBox();
    const w = svg.node().clientWidth, h = svg.node().clientHeight;
    const scale = Math.min(1, (w - 40) / Math.max(bbox.width, 1), (h - 40) / Math.max(bbox.height, 1));
    const tx = 20 - bbox.x * scale;
    const ty = (h - bbox.height * scale) / 2 - bbox.y * scale;
    initialTransform = d3.zoomIdentity.translate(tx, ty).scale(scale);
    svg.call(zoom.transform, initialTransform);
  }
}

function collapseUntouched(d) {
  // collapse subtrees that contain zero accessed nodes — keeps default view clean
  if (d.children) {
    d.children.forEach(collapseUntouched);
    const touchedHere = d.data.access ? true : false;
    const anyTouchedBelow = d.children.some(c => c.data.access || c._children || (c.children && c.children.length));
    // Only collapse if NOTHING below has access either
    if (!touchedHere && !d.children.some(hasTouchedDescendant)) {
      d._children = d.children;
      d.children = null;
    }
  }
}
function hasTouchedDescendant(d) {
  if (d.data.access) return true;
  const kids = d.children || d._children || [];
  return kids.some(hasTouchedDescendant);
}

function showDetails(d) {
  document.getElementById("sel-path").textContent = d.data.path || d.data.name;
  const body = document.getElementById("sel-body");
  const a = d.data.access;
  if (!a) {
    body.innerHTML = `<dl><dt>type</dt><dd>${d.data.type}</dd><dt>touched</dt><dd>no</dd></dl>`;
    return;
  }
  const tools = Object.entries(a.tools).sort((x, y) => y[1] - x[1])
    .map(([t, n]) => `<li>${t} × ${n}</li>`).join("");
  const sessions = a.sessions.map(s => `<li>${s.slice(0, 8)}…</li>`).join("");
  const projects = a.projects.map(p => `<li>${p}</li>`).join("");
  body.innerHTML = `
    <dl>
      <dt>type</dt><dd>${d.data.type}</dd>
      <dt>accesses</dt><dd>${a.count}</dd>
      <dt>last seen</dt><dd>${a.last_ts || "—"}</dd>
      <dt>sidechain</dt><dd>${a.sidechain}</dd>
    </dl>
    <h2>Tools</h2><ul class="tools">${tools}</ul>
    <h2>Sessions</h2><ul>${sessions}</ul>
    <h2>Projects</h2><ul>${projects}</ul>
  `;
}

function setMeta(meta) {
  document.getElementById("meta").textContent =
    `${meta.event_count} events · ${meta.unique_paths} paths · root: ${meta.root} · scanned ${meta.scanned_at} (${meta.scan_ms}ms)`;
}

async function load(rescan) {
  const r = await fetch(rescan ? "/api/rescan" : "/api/tree");
  const { tree: data, meta } = await r.json();
  setMeta(meta);
  root = d3.hierarchy(data);
  root.x0 = 0; root.y0 = 0;
  collapseUntouched(root);
  initialTransform = null;
  update(root);
}

document.getElementById("rescan").onclick = () => load(true);
document.getElementById("reset").onclick = () => { initialTransform = null; update(root); };
load(false);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI / entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS_DIR,
                    help="Path to ~/.claude/projects (default: %(default)s)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port (default: %(default)s)")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (default: %(default)s)")
    ap.add_argument("--root", default=None,
                    help="Tree root. Defaults to common ancestor of all accessed paths.")
    ap.add_argument("--full-walk", default=None, metavar="DIR",
                    help="Additionally walk this whole subtree (slow; e.g. ~ or /).")
    ap.add_argument("--full-home", action="store_true",
                    help="Shortcut for --full-walk $HOME.")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser.")
    ap.add_argument("--print-only", action="store_true",
                    help="Dump the JSON tree to stdout and exit (skip the server).")
    args = ap.parse_args(argv)

    full_walk = args.full_walk
    if args.full_home and not full_walk:
        full_walk = str(Path.home())

    cache = _Cache(args.projects, args.root, full_walk)

    if args.print_only:
        tree, meta = cache.get()
        json.dump({"tree": tree, "meta": meta}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    # Warm the cache up front so the first request is instant.
    tree, meta = cache.get()
    print(f"[clawreach] {meta['event_count']} tool events across "
          f"{meta['unique_paths']} unique paths from {meta['transcripts_dir']}",
          file=sys.stderr)
    print(f"[clawreach] tree root: {meta['root']}  (scan {meta['scan_ms']}ms)", file=sys.stderr)

    handler = make_handler(cache, FRONTEND_HTML)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[clawreach] serving at {url}", file=sys.stderr)

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.3), webbrowser.open(url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[clawreach] bye.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
