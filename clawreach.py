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
import difflib
import hashlib
import json
import os
import queue
import re
import shlex
import sys
import threading
import time
import urllib.parse
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
DEFAULT_FILE_HISTORY_DIR = Path.home() / ".claude" / "file-history"
DEFAULT_PORT = 8765
# Cap snapshot/current content sent to the browser. Bigger files still diff,
# but each side is truncated and marked.
MAX_DIFF_CONTENT_BYTES = 256 * 1024

# What Claude *did* to the path. Drives the visual category in the frontend.
#   read    — saw the bytes (Read, cat, Grep with content)
#   write   — created or fully replaced (Write, mkdir, touch, shell redirect)
#   edit    — modified in place (Edit, MultiEdit, NotebookEdit)
#   list    — saw the path exists but not its content (Glob, Grep, LS, find)
#   observe — surfaced incidentally in tool output (less authoritative)
#   bash    — touched by shell, can't tell what was done
ACTIONS = ("write", "edit", "read", "bash", "list", "observe")
ACTION_PRIORITY = {a: i for i, a in enumerate(ACTIONS)}  # lower = "more interesting"

# Tools whose `input` carries a single path under a well-known key.
# Each maps to (input_key, action).
PATH_KEY_TOOLS = {
    "Read":         ("file_path",     "read"),
    "Edit":         ("file_path",     "edit"),
    "MultiEdit":    ("file_path",     "edit"),
    "Write":        ("file_path",     "write"),
    "NotebookRead": ("notebook_path", "read"),
    "NotebookEdit": ("notebook_path", "edit"),
}

# Tools whose `input` carries a directory under `path`. All "list" actions.
DIR_KEY_TOOLS = {"Glob", "Grep", "LS"}

# Bash command dispatch: which verb implies what action.
BASH_READ_CMDS  = {"cat", "less", "more", "tail", "head", "file", "stat", "wc",
                   "md5", "md5sum", "sha1sum", "sha256sum", "shasum", "xxd",
                   "od", "diff", "cmp"}
BASH_LIST_CMDS  = {"ls", "find", "tree", "du", "fd", "grep", "rg", "ag", "ack",
                   "locate", "which", "whereis"}
BASH_WRITE_CMDS = {"mkdir", "touch", "rm", "rmdir", "cp", "mv", "ln", "chmod",
                   "chown", "dd", "tee", "install", "rsync"}

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
# Absolute paths anywhere in a free-form string (used to mine tool_result text).
ABS_PATH_IN_TEXT_RE = re.compile(r"(?:^|[\s'\"`(\[])(/[\w./\-+@:%]{2,})")

# Cap mined paths per tool_result to keep the tree small under chatty outputs
# like a recursive `ls` or a noisy `npm install`.
MAX_MINED_PATHS_PER_RESULT = 100

# Paths Claude probably shouldn't be in. Surfaces in the UI as a red ring on
# the node and a banner count at the top of the page. Override with
# --sensitive-patterns FILE (one regex per line).
DEFAULT_SENSITIVE_PATTERNS = [
    r"(^|/)\.ssh(/|$)",
    r"(^|/)\.aws(/|$)",
    r"(^|/)\.gnupg(/|$)",
    r"(^|/)\.config/gh/",
    r"/Library/Keychains/",
    r"\.env(\.|$)",
    r"\.pem$",
    r"\.key$",
    r"id_(rsa|ed25519|ecdsa|dsa)(\.|$)",
    r"credentials?",
    r"secret",
    r"\btoken\b",
    r"\bpassword\b",
    r"\bprivate[_-]?key\b",
]
# Cap how many sensitive paths we ship for the banner's click-to-jump list.
MAX_SENSITIVE_PATHS_IN_META = 50


# ---------------------------------------------------------------------------
# Ingest — parse JSONL transcripts into AccessEvent records
# ---------------------------------------------------------------------------

@dataclass
class AccessEvent:
    path: str        # absolute, normalized
    tool: str        # tool name (Read/Edit/Write/Bash/...) or "snapshot" / "result"
    action: str      # one of ACTIONS — what Claude *did* to the path
    ts: str          # ISO timestamp from the transcript
    session: str
    project: str     # the encoded project dir name (= cwd with / → -)
    is_sidechain: bool  # True if a subagent issued the call
    sensitive: bool = False  # matched a SENSITIVE_PATTERN — set in ingest_all


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


def _bash_action(command: str) -> str:
    """Classify a Bash command as read/write/list/bash by its primary verb.

    Order matters: a redirect like `python build.py > out.txt` is a write even
    though the verb is `python`, so we look for `>` / `>>` first.
    """
    # Redirect → file is being written. Cheap check; tolerates quoting noise.
    if re.search(r"(?<![<>])>{1,2}(?![&>])", command):
        return "write"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "bash"
    for tok in tokens:
        if not tok or tok.startswith("-"):
            continue
        # skip env-var prefixes like FOO=bar cmd ...
        if "=" in tok and tok.split("=", 1)[0].isupper() and tok.split("=", 1)[0].replace("_", "").isalnum():
            continue
        verb = os.path.basename(tok)
        # peel off `sudo` and try the next token
        if verb in {"sudo", "command", "exec", "time", "nohup"}:
            continue
        if verb in BASH_READ_CMDS:  return "read"
        if verb in BASH_LIST_CMDS:  return "list"
        if verb in BASH_WRITE_CMDS: return "write"
        return "bash"
    return "bash"


def _iter_tool_uses(entry: dict) -> Iterable[tuple[str, dict, str]]:
    """Yield (tool_name, input_dict, tool_use_id) for every tool_use in an assistant entry."""
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
                yield name, inp, block.get("id") or ""


def parse_transcript(jsonl_path: Path) -> list[AccessEvent]:
    """Stream a .jsonl transcript and return every AccessEvent in it.

    Three signal sources, in order of authority:
      1. `tool_use` blocks on assistant entries (Read/Edit/Write/Bash/...)
      2. `file-history-snapshot` entries — Claude Code's own ledger of files
         it modified. Ground truth for writes.
      3. `tool_result` blocks on user entries — mine the output text of
         Bash/LS/Glob/Grep for additional paths Claude *observed*.
    """
    project = jsonl_path.parent.name
    events: list[AccessEvent] = []
    # Map tool_use_id -> tool name, so we can interpret tool_result blocks.
    use_to_tool: dict[str, str] = {}
    # Track most-recently-seen cwd; file-history-snapshot entries don't carry
    # one but the surrounding messages do.
    last_cwd: str | None = _decode_project_cwd(project)
    last_session: str = ""
    last_sidechain: bool = False

    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            ts = entry.get("timestamp") or ""
            cwd = entry.get("cwd") or last_cwd
            if entry.get("cwd"):
                last_cwd = entry["cwd"]
            session = entry.get("sessionId") or last_session
            last_session = session or last_session
            sidechain = bool(entry.get("isSidechain", last_sidechain))
            last_sidechain = sidechain

            if etype == "assistant":
                for tool, inp, tu_id in _iter_tool_uses(entry):
                    if tu_id:
                        use_to_tool[tu_id] = tool
                    for path, action in _paths_for_tool(tool, inp, cwd):
                        events.append(AccessEvent(
                            path, tool, action, ts, session, project, sidechain))
            elif etype == "file-history-snapshot":
                snap = entry.get("snapshot") or {}
                tfb = snap.get("trackedFileBackups") or {}
                snap_ts = snap.get("timestamp") or ts
                for rel_path in tfb.keys():
                    p = _expand(rel_path, cwd)
                    if p:
                        events.append(AccessEvent(
                            p, "snapshot", "write", snap_ts, session, project, sidechain))
            elif etype == "user":
                # tool_result blocks live inside user-typed messages.
                for tu_id, text in _iter_tool_results(entry):
                    src_tool = use_to_tool.get(tu_id, "")
                    for path, action in _mine_result_paths(src_tool, text, cwd):
                        events.append(AccessEvent(
                            path, src_tool or "result", action, ts, session, project, sidechain))
    return events


def _decode_project_cwd(project_dir_name: str) -> str | None:
    """Best-effort decode of ~/.claude/projects/<dir>/ into the original cwd.

    Claude Code encodes the cwd by replacing `/` with `-`. The decode is lossy
    for paths that legitimately contain `-` in component names, but works for
    the typical case (~/Desktop/Foo etc.). Only used as a fallback when no
    sibling entry exposes the true cwd.
    """
    if not project_dir_name.startswith("-"):
        return None
    return "/" + project_dir_name.lstrip("-").replace("-", "/")


def _paths_for_tool(tool: str, inp: dict, cwd: str | None) -> list[tuple[str, str]]:
    """Map (tool, input) → list of (absolute_path, action) the call touched."""
    if tool in PATH_KEY_TOOLS:
        key, action = PATH_KEY_TOOLS[tool]
        p = _expand(inp.get(key), cwd)
        return [(p, action)] if p else []
    if tool in DIR_KEY_TOOLS:
        p = _expand(inp.get("path"), cwd) if inp.get("path") else cwd
        return [(p, "list")] if p else []
    if tool == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str):
            return []
        action = _bash_action(cmd)
        return [(p, action) for p in _extract_bash_paths(cmd, cwd)]
    return []


def _iter_tool_results(entry: dict) -> Iterable[tuple[str, str]]:
    """Yield (tool_use_id, text) for every tool_result block in a user entry."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tu_id = block.get("tool_use_id") or ""
        c = block.get("content")
        # tool_result content can be a string or a list of {type:text, text:...}.
        if isinstance(c, str):
            yield tu_id, c
        elif isinstance(c, list):
            for sub in c:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    t = sub.get("text")
                    if isinstance(t, str):
                        yield tu_id, t


def _mine_result_paths(src_tool: str, text: str, cwd: str | None) -> list[tuple[str, str]]:
    """Pull absolute paths out of tool_result text, tag as 'observe'.

    For Write results we look for the specific 'File created successfully at: <path>'
    marker and tag those as 'write' — that's a direct confirmation, not a hint.
    Capped at MAX_MINED_PATHS_PER_RESULT to avoid drowning the tree.
    """
    if not text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Write tool's own success message — strongest confirmation we have.
    if src_tool == "Write":
        m = re.search(r"File created successfully at:\s*(\S+)", text)
        if m:
            p = _expand(m.group(1), cwd)
            if p and p not in seen:
                out.append((p, "write"))
                seen.add(p)

    if src_tool in {"Bash", "LS", "Glob", "Grep", "result", ""}:
        for m in ABS_PATH_IN_TEXT_RE.finditer(text):
            raw = m.group(1)
            # Strip trailing punctuation that often follows paths in prose.
            raw = raw.rstrip(".,;:)\"'`]")
            p = _expand(raw, cwd)
            if not p or p in seen:
                continue
            seen.add(p)
            out.append((p, "observe"))
            if len(out) >= MAX_MINED_PATHS_PER_RESULT:
                break
    return out


def ingest_all(projects_dir: Path,
               sensitive_patterns: list[str] | None = None) -> list[AccessEvent]:
    """Parse every transcript under projects_dir; tag sensitive paths in place."""
    events: list[AccessEvent] = []
    if not projects_dir.exists():
        return events
    for jsonl in projects_dir.rglob("*.jsonl"):
        try:
            events.extend(parse_transcript(jsonl))
        except OSError:
            continue
    matcher = _compile_sensitive(sensitive_patterns or DEFAULT_SENSITIVE_PATTERNS)
    if matcher is not None:
        for ev in events:
            if matcher.search(ev.path):
                ev.sensitive = True
    return events


def _compile_sensitive(patterns: list[str]) -> "re.Pattern | None":
    """Combine patterns into one case-insensitive regex; None if list is empty."""
    cleaned = [p for p in (s.strip() for s in patterns) if p and not p.startswith("#")]
    if not cleaned:
        return None
    return re.compile("|".join(f"(?:{p})" for p in cleaned), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Aggregate — collapse events into per-path summaries
# ---------------------------------------------------------------------------

@dataclass
class PathStats:
    count: int = 0
    last_ts: str = ""
    tools: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    actions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sessions: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    sidechain_count: int = 0
    sensitive: bool = False  # any event for this path was sensitive

    def add(self, ev: AccessEvent) -> None:
        self.count += 1
        if ev.ts > self.last_ts:
            self.last_ts = ev.ts
        self.tools[ev.tool] += 1
        self.actions[ev.action] += 1
        self.sessions.add(ev.session)
        self.projects.add(ev.project)
        if ev.is_sidechain:
            self.sidechain_count += 1
        if ev.sensitive:
            self.sensitive = True

    @property
    def primary_action(self) -> str:
        """The 'most interesting' action seen on this path.

        Precedence (per ACTION_PRIORITY): write > edit > read > bash > list > observe.
        A path that was written once and read ten times still shows as 'write'
        because that's the stronger signal about what Claude *did* there.
        """
        if not self.actions:
            return "observe"
        return min(self.actions.keys(), key=lambda a: ACTION_PRIORITY.get(a, 99))

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "last_ts": self.last_ts,
            "primary": self.primary_action,
            "actions": dict(self.actions),
            "tools": dict(self.tools),
            "sessions": sorted(self.sessions),
            "projects": sorted(self.projects),
            "sidechain": self.sidechain_count,
            "sensitive": self.sensitive,
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
# Snapshot diffing — resolve and diff ~/.claude/file-history/<sess>/<hash>@v2
# ---------------------------------------------------------------------------

# Claude Code names snapshot files by the first 8 bytes of the SHA-256 of the
# original file's absolute path, hex-encoded, with a "@v2" suffix.
def _snapshot_filename(abs_path: str) -> str:
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16] + "@v2"


def _find_snapshot(file_history_dir: Path, abs_path: str,
                   sessions: Iterable[str]) -> tuple[str, Path] | None:
    """First session that has a snapshot of `abs_path`, or None.

    Caller usually passes `sessions` ordered by relevance (e.g. the sessions
    that actually touched this path, most recent first).
    """
    fn = _snapshot_filename(abs_path)
    for s in sessions:
        if not s:
            continue
        p = file_history_dir / s / fn
        if p.exists():
            return s, p
    return None


def _read_capped(path: Path, cap: int) -> tuple[str, bool]:
    """Read text from path, capped at `cap` bytes. Returns (text, truncated)."""
    try:
        data = path.read_bytes()
    except OSError:
        return "", False
    truncated = len(data) > cap
    if truncated:
        data = data[:cap]
    return data.decode("utf-8", errors="replace"), truncated


def _diff_rows(old: str, new: str) -> list[dict]:
    """Side-by-side line diff using difflib.

    Each row is {t: equal|del|add|change, l: left_line, r: right_line}.
    Frontend renders rows directly — no diff lib needed in the browser.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for a, b in zip(old_lines[i1:i2], new_lines[j1:j2]):
                rows.append({"t": "equal", "l": a, "r": b})
        elif tag == "delete":
            for a in old_lines[i1:i2]:
                rows.append({"t": "del", "l": a, "r": ""})
        elif tag == "insert":
            for b in new_lines[j1:j2]:
                rows.append({"t": "add", "l": "", "r": b})
        elif tag == "replace":
            left = old_lines[i1:i2]
            right = new_lines[j1:j2]
            for k in range(max(len(left), len(right))):
                rows.append({
                    "t": "change",
                    "l": left[k] if k < len(left) else "",
                    "r": right[k] if k < len(right) else "",
                })
    return rows


# ---------------------------------------------------------------------------
# Server — stdlib HTTP, serves frontend + JSON API
# ---------------------------------------------------------------------------

class _Cache:
    """Thread-safe cache of the last scan. Re-scans on demand.

    Optionally runs a background thread that polls JSONL mtimes and triggers
    re-scans + SSE notifications when transcripts change on disk.
    """

    def __init__(self, projects_dir: Path, root: str | None, full_walk_root: str | None,
                 sensitive_patterns: list[str] | None = None,
                 file_history_dir: Path | None = None):
        self.projects_dir = projects_dir
        self.root = root
        self.full_walk_root = full_walk_root
        self.sensitive_patterns = sensitive_patterns  # None → use defaults
        self.file_history_dir = file_history_dir or DEFAULT_FILE_HISTORY_DIR
        self._lock = threading.Lock()
        self._tree: dict | None = None
        self._events: list[AccessEvent] = []
        self._meta: dict = {}
        # SSE subscribers (one queue per connected client) + watcher thread.
        self._sse_subscribers: list["queue.Queue[dict]"] = []
        self._sse_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None

    # --- SSE plumbing -------------------------------------------------------
    def add_subscriber(self) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue(maxsize=8)
        with self._sse_lock:
            self._sse_subscribers.append(q)
        return q

    def remove_subscriber(self, q: "queue.Queue[dict]") -> None:
        with self._sse_lock:
            try:
                self._sse_subscribers.remove(q)
            except ValueError:
                pass

    def _notify_subscribers(self, msg: dict) -> None:
        with self._sse_lock:
            subs = list(self._sse_subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Slow consumer — drop. They'll catch up on the next event.
                pass

    # --- Watcher ------------------------------------------------------------
    def start_watch(self, interval: float = 2.0) -> None:
        if self._watch_thread is not None:
            return
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), daemon=True,
            name="clawreach-watch")
        self._watch_thread.start()

    def _watch_loop(self, interval: float) -> None:
        mtimes: dict[str, float] | None = None
        while True:
            try:
                current: dict[str, float] = {}
                if self.projects_dir.exists():
                    for j in self.projects_dir.rglob("*.jsonl"):
                        try:
                            current[str(j)] = j.stat().st_mtime
                        except OSError:
                            continue
                if mtimes is not None and current != mtimes:
                    self.rescan()
                    _, _, meta = self.get()
                    self._notify_subscribers({
                        "type": "tree-updated",
                        "scanned_at": meta.get("scanned_at"),
                        "event_count": meta.get("event_count"),
                    })
                mtimes = current
            except Exception as e:
                # Don't let one bad iteration kill the watcher.
                sys.stderr.write(f"[clawreach] watcher error: {e}\n")
            time.sleep(interval)

    def get(self) -> tuple[dict, list[AccessEvent], dict]:
        with self._lock:
            if self._tree is None:
                self._rescan_locked()
            return self._tree, list(self._events), dict(self._meta)

    def rescan(self) -> tuple[dict, list[AccessEvent], dict]:
        with self._lock:
            self._rescan_locked()
            return self._tree, list(self._events), dict(self._meta)

    def _rescan_locked(self) -> None:
        t0 = time.time()
        events = ingest_all(self.projects_dir, self.sensitive_patterns)
        stats = aggregate(events)
        tree = build_tree(stats, root=self.root, full_walk_root=self.full_walk_root)
        self._tree = tree
        self._events = events
        # Pre-compute the inputs the filter/slider UIs need so the frontend
        # doesn't have to walk every event to discover them.
        sessions = sorted({ev.session for ev in events if ev.session})
        projects = sorted({ev.project for ev in events if ev.project})
        timestamps = [ev.ts for ev in events if ev.ts]
        sensitive_paths = sorted({ev.path for ev in events if ev.sensitive})
        self._meta = {
            "event_count": len(events),
            "unique_paths": len(stats),
            "transcripts_dir": str(self.projects_dir),
            "root": tree.get("path", ""),
            "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_ms": int((time.time() - t0) * 1000),
            "sessions": sessions,
            "projects": projects,
            "time_min": min(timestamps) if timestamps else "",
            "time_max": max(timestamps) if timestamps else "",
            "sensitive_count": len(sensitive_paths),
            "sensitive_paths": sensitive_paths[:MAX_SENSITIVE_PATHS_IN_META],
        }


def _events_to_wire(events: list[AccessEvent]) -> list[dict]:
    """Compact event list for the wire. Skips booleans when False."""
    out: list[dict] = []
    for ev in events:
        d = {
            "path": ev.path, "tool": ev.tool, "action": ev.action,
            "ts": ev.ts, "session": ev.session, "project": ev.project,
        }
        if ev.is_sidechain:
            d["sidechain"] = True
        if ev.sensitive:
            d["sensitive"] = True
        out.append(d)
    return out


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
                tree, events, meta = cache.get()
                self._send_json({"tree": tree, "events": _events_to_wire(events), "meta": meta})
                return
            if self.path == "/api/rescan":
                tree, events, meta = cache.rescan()
                self._send_json({"tree": tree, "events": _events_to_wire(events), "meta": meta})
                return
            if self.path == "/api/events":
                self._serve_sse()
                return
            if self.path.startswith("/api/snapshot"):
                self._serve_snapshot()
                return
            self.send_response(404)
            self.end_headers()

        def _serve_snapshot(self):
            """Resolve a path's snapshot and return JSON with side-by-side diff."""
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            abs_path = (qs.get("path") or [""])[0]
            session_hint = (qs.get("session") or [""])[0]
            if not abs_path:
                self._send_json({"error": "missing path"}, status=400)
                return
            # Walk the cache to find which sessions touched this path; try the
            # hinted session first, then the others — most recent first feels
            # right but PathStats.sessions is sorted alphabetically.
            _, events, _ = cache.get()
            sessions_for_path = sorted(
                {ev.session for ev in events if ev.path == abs_path and ev.session},
                reverse=True)  # roughly recent-first for uuid-prefixed session ids
            ordered = ([session_hint] if session_hint else []) + \
                      [s for s in sessions_for_path if s != session_hint]
            hit = _find_snapshot(cache.file_history_dir, abs_path, ordered)
            if hit is None:
                self._send_json({
                    "missing": True,
                    "reason": "No snapshot found for this path in any session that touched it.",
                    "expected_filename": _snapshot_filename(abs_path),
                    "searched_sessions": ordered,
                }, status=200)
                return
            sess, snap_path = hit
            snap_text, snap_trunc = _read_capped(snap_path, MAX_DIFF_CONTENT_BYTES)
            cur_text, cur_trunc = "", False
            cur_exists = os.path.exists(abs_path)
            if cur_exists:
                cur_text, cur_trunc = _read_capped(Path(abs_path), MAX_DIFF_CONTENT_BYTES)
            self._send_json({
                "missing": False,
                "session": sess,
                "snapshot": {
                    "path": str(snap_path), "bytes": snap_path.stat().st_size,
                    "text": snap_text, "truncated": snap_trunc,
                },
                "current": {
                    "path": abs_path, "exists": cur_exists,
                    "bytes": os.path.getsize(abs_path) if cur_exists else 0,
                    "text": cur_text, "truncated": cur_trunc,
                },
                "diff": _diff_rows(snap_text, cur_text),
            })

        def _serve_sse(self):
            """Stream cache events to the client until they disconnect."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
            self.end_headers()
            q = cache.add_subscriber()
            try:
                # Hello so the client knows the stream is live.
                self.wfile.write(b"data: {\"type\":\"hello\"}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15.0)
                    except queue.Empty:
                        # Comment-only line is a keep-alive per the SSE spec.
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    body = b"data: " + json.dumps(msg).encode("utf-8") + b"\n\n"
                    self.wfile.write(body)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # client went away
            finally:
                cache.remove_subscriber(q)

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
    --accent-soft: #ffb088;
    --dir: #3a4252;          /* untouched directory */
    --untouched: #242a33;    /* untouched file */
    /* Per-action palette — keep aligned with ACTIONS in clawreach.py */
    --act-write:   #ff7a45;  /* orange  — created / generated  */
    --act-edit:    #ffd166;  /* yellow  — modified in place    */
    --act-read:    #4dd0e1;  /* cyan    — bytes read           */
    --act-bash:    #b88cff;  /* purple  — shell-touched        */
    --act-list:    #5b7cff;  /* blue    — listed / matched     */
    --act-observe: #6b7280;  /* grey    — surfaced in output   */
    --danger: #ef4444;       /* sensitive-path callouts        */
    --danger-soft: #fca5a5;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--ink);
    font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
    display: grid; grid-template-rows: auto auto 1fr; overflow: hidden;
  }
  #alert {
    display: none;
    align-items: center; gap: 10px;
    padding: 8px 16px;
    background: rgba(239, 68, 68, .12);
    border-bottom: 1px solid var(--danger);
    color: var(--danger-soft);
    font-size: 12px;
  }
  #alert.visible { display: flex; }
  #alert .icon { font-size: 14px; }
  #alert button {
    background: transparent; border: 1px solid var(--danger);
    color: var(--danger-soft); padding: 3px 10px; border-radius: 3px;
    font: inherit; cursor: pointer;
  }
  #alert button:hover { background: rgba(239,68,68,.18); }
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
  .filter {
    position: relative;
  }
  .filter > summary {
    list-style: none; cursor: pointer; padding: 4px 10px;
    border: 1px solid var(--rule); border-radius: 4px;
    background: var(--rule); color: var(--ink); font: inherit;
    user-select: none;
  }
  .filter > summary::-webkit-details-marker { display: none; }
  .filter > summary:hover { background: #232b38; }
  .filter .count {
    color: var(--accent-soft); margin-left: 4px;
  }
  .filter-body {
    position: absolute; top: 100%; left: 0; margin-top: 4px;
    background: var(--panel); border: 1px solid var(--rule); border-radius: 4px;
    padding: 8px 10px; min-width: 220px; max-width: 360px;
    max-height: 320px; overflow-y: auto; z-index: 10;
    box-shadow: 0 4px 16px rgba(0,0,0,.4);
  }
  .filter-body label {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 2px; cursor: pointer;
    font-size: 12px; word-break: break-all;
  }
  .filter-body label:hover { background: var(--rule); }
  .filter-body input[type="checkbox"] { accent-color: var(--act-write); }
  .time-controls {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 8px; background: var(--rule); border-radius: 4px;
  }
  .time-controls input[type=range] {
    accent-color: var(--act-write); width: 180px;
  }
  .time-controls #time-display {
    font-size: 11px; color: var(--accent-soft); min-width: 9ch; text-align: right;
  }
  .time-controls button {
    background: transparent; border: none; color: var(--ink);
    padding: 0 6px; cursor: pointer; font-size: 14px;
  }
  /* Diff modal */
  #diff-modal {
    border: none; padding: 0; max-width: 92vw; max-height: 88vh;
    width: 1200px; background: var(--panel); color: var(--ink);
    border-radius: 6px; overflow: hidden;
  }
  #diff-modal::backdrop { background: rgba(0,0,0,.55); }
  .diff-head {
    display: flex; align-items: center; gap: 16px;
    padding: 10px 14px; border-bottom: 1px solid var(--rule);
    background: var(--bg);
  }
  .diff-head .title { font-size: 12px; color: var(--accent-soft); word-break: break-all; }
  .diff-head .stats { color: var(--muted); font-size: 11px; margin-left: auto; }
  .diff-head button {
    background: var(--rule); color: var(--ink); border: 1px solid var(--rule);
    padding: 3px 10px; border-radius: 3px; cursor: pointer; font: inherit;
  }
  .diff-body {
    overflow: auto; max-height: calc(88vh - 50px); background: var(--bg);
    font: 11px/1.4 ui-monospace, monospace;
  }
  .diff-body table { border-collapse: collapse; width: 100%; }
  .diff-body td {
    padding: 1px 8px; vertical-align: top; white-space: pre; word-break: normal;
    border-bottom: 1px solid transparent;
  }
  .diff-body td.side {
    width: 50%; max-width: 0;   /* lets long lines scroll the table cell */
    overflow-x: auto;
  }
  .diff-body tr.equal td { color: var(--muted); }
  .diff-body tr.add td.right   { background: rgba(255, 122, 69, .08); color: var(--act-write); }
  .diff-body tr.del td.left    { background: rgba(239, 68,  68, .12); color: var(--danger-soft); }
  .diff-body tr.change td.left { background: rgba(255, 209, 102, .08); color: var(--act-edit); }
  .diff-body tr.change td.right{ background: rgba(255, 209, 102, .08); color: var(--act-edit); }
  .diff-empty { padding: 40px; text-align: center; color: var(--muted); }
  .filter-body .actions {
    display: flex; gap: 8px; padding: 4px 0;
    border-bottom: 1px solid var(--rule); margin-bottom: 4px;
  }
  .filter-body .actions a { color: var(--accent-soft); cursor: pointer; font-size: 11px; }
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
  aside .primary-chip {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-size: 11px; margin-bottom: 10px; color: #0b0d11; font-weight: 600;
    letter-spacing: .5px; text-transform: uppercase;
  }
  aside .sensitive-chip {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-size: 11px; margin-bottom: 10px; margin-left: 6px;
    color: var(--danger-soft); border: 1px solid var(--danger); font-weight: 600;
    letter-spacing: .5px; text-transform: uppercase;
  }
  .action-bar { display: flex; gap: 4px; height: 8px; margin: 4px 0 12px;
                border-radius: 2px; overflow: hidden; }
  .action-bar > div { height: 100%; }
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
  .link { fill: none; stroke: var(--rule); stroke-width: 1px; }
  .link.touched { stroke-opacity: .55; stroke-width: 1.4px; }
  .legend {
    position: absolute; bottom: 10px; left: 10px;
    background: rgba(20,24,31,.92); padding: 8px 12px; border-radius: 4px;
    border: 1px solid var(--rule); color: var(--ink); font-size: 11px;
    display: grid; grid-template-columns: max-content max-content; gap: 4px 14px;
  }
  /* Floating viewport controls — always-visible safety net when the tree
     has been panned/zoomed off-screen. Top-right corner of the viz area. */
  .viz-controls {
    position: absolute; top: 10px; right: 10px; z-index: 5;
    display: flex; flex-direction: column; gap: 4px;
  }
  .viz-controls button {
    width: 34px; height: 34px;
    background: rgba(20,24,31,.92);
    border: 1px solid var(--rule);
    color: var(--ink); border-radius: 4px; cursor: pointer;
    font-size: 16px; line-height: 1;
    display: flex; align-items: center; justify-content: center;
  }
  .viz-controls button:hover { background: var(--rule); border-color: var(--accent-soft); }
  .viz-controls button:active { background: #232b38; }
  .viz-controls .recenter { font-size: 18px; }
  /* Brief flash on the recenter button when the keyboard shortcut fires,
     so the user discovers it exists. */
  @keyframes flash-recenter {
    0%   { box-shadow: 0 0 0 2px var(--accent-soft); }
    100% { box-shadow: 0 0 0 0 transparent; }
  }
  .viz-controls .recenter.flash { animation: flash-recenter .6s ease-out; }
  .legend .item { display: flex; align-items: center; gap: 6px; }
  .legend .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
  .legend .hint { grid-column: 1 / -1; color: var(--muted); margin-top: 4px;
                  padding-top: 4px; border-top: 1px solid var(--rule); }
</style>
</head>
<body>
<header>
  <h1>ClawReach</h1>
  <span class="meta" id="meta">loading…</span>
  <details class="filter" id="filter-sessions-wrap">
    <summary>Sessions <span class="count" id="filter-sessions-count"></span></summary>
    <div class="filter-body">
      <div class="actions">
        <a data-target="filter-sessions-body" data-mode="all">all</a>
        <a data-target="filter-sessions-body" data-mode="none">none</a>
      </div>
      <div id="filter-sessions-body"></div>
    </div>
  </details>
  <details class="filter" id="filter-projects-wrap">
    <summary>Projects <span class="count" id="filter-projects-count"></span></summary>
    <div class="filter-body">
      <div class="actions">
        <a data-target="filter-projects-body" data-mode="all">all</a>
        <a data-target="filter-projects-body" data-mode="none">none</a>
      </div>
      <div id="filter-projects-body"></div>
    </div>
  </details>
  <div class="time-controls">
    <button id="time-play" title="Play / pause">▶</button>
    <input type="range" id="time-slider" min="0" max="1000" value="1000" step="1">
    <span id="time-display">live</span>
  </div>
  <button id="rescan">Re-scan</button>
  <button id="reset">Reset view</button>
  <span class="meta" style="margin-left:auto">click a node to expand/collapse · drag to pan · scroll to zoom</span>
</header>
<div id="alert">
  <span class="icon">⚠</span>
  <span id="alert-text"></span>
  <button id="alert-show">Show all</button>
</div>
<main>
  <div id="viz">
    <svg></svg>
    <div class="viz-controls">
      <button id="zoom-in"  title="Zoom in (+ or =)">+</button>
      <button id="zoom-out" title="Zoom out (− or _)">−</button>
      <button id="recenter" class="recenter" title="Recenter view (R or 0)">⌂</button>
    </div>
    <div class="legend">
      <div class="item"><span class="dot" style="background:var(--act-write)"></span>written / generated</div>
      <div class="item"><span class="dot" style="background:var(--act-edit)"></span>edited in place</div>
      <div class="item"><span class="dot" style="background:var(--act-read)"></span>read</div>
      <div class="item"><span class="dot" style="background:var(--act-bash)"></span>bash-touched</div>
      <div class="item"><span class="dot" style="background:var(--act-list)"></span>listed / matched</div>
      <div class="item"><span class="dot" style="background:var(--act-observe)"></span>observed in output</div>
      <div class="hint">node size = access count · color = primary action · press <kbd>R</kbd> to recenter</div>
    </div>
  </div>
  <aside id="details">
    <h2>Selection</h2>
    <div class="path" id="sel-path">— pick a node —</div>
    <div id="sel-body"></div>
  </aside>
</main>

<dialog id="diff-modal">
  <div class="diff-head">
    <div class="title" id="diff-title">…</div>
    <div class="stats" id="diff-stats"></div>
    <button id="diff-close">Close</button>
  </div>
  <div class="diff-body" id="diff-body"></div>
</dialog>

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
// Server-supplied state cached for re-filtering.
let lastEvents = [];
let lastMeta = null;
// Filter state. `null` for either field means "all selected"; otherwise
// it's an array of explicitly-selected values.
let filters = loadFilters();

// Time-slider state.
let timeMin = 0, timeMax = 0;
let timeCutoff = 0;       // current slider value (ms since epoch)
let isPlaying = false;
let playInterval = null;

const ACTION_PRIORITY = { write:0, edit:1, read:2, bash:3, list:4, observe:5 };

function loadFilters() {
  try {
    const raw = localStorage.getItem("clawreach.filters");
    if (raw) return JSON.parse(raw);
  } catch {}
  return { sessions: null, projects: null };
}
function saveFilters() {
  try { localStorage.setItem("clawreach.filters", JSON.stringify(filters)); } catch {}
}

function eventPasses(ev) {
  if (filters.sessions !== null && !filters.sessions.includes(ev.session)) return false;
  if (filters.projects !== null && !filters.projects.includes(ev.project)) return false;
  // Time slider: when cutoff is at max we accept everything (incl. ts==max).
  if (timeMax > 0 && timeCutoff < timeMax && ev.ts) {
    const evMs = Date.parse(ev.ts);
    if (Number.isFinite(evMs) && evMs > timeCutoff) return false;
  }
  return true;
}

function isFilterActive() {
  if (filters.sessions !== null && lastMeta &&
      filters.sessions.length !== lastMeta.sessions.length) return true;
  if (filters.projects !== null && lastMeta &&
      filters.projects.length !== lastMeta.projects.length) return true;
  if (timeMax > 0 && timeCutoff < timeMax) return true;
  return false;
}

// Build a hierarchy from a filtered event list. No filesystem access, so the
// "+1 level of siblings" the server adds is lost — but for a deliberately
// filtered view, the focused layout is usually what you want.
function buildTreeFromEvents(events) {
  if (!events.length) {
    return { name: "(no events match filter)", path: "", type: "dir", children: [] };
  }
  // Aggregate per-path stats — mirror PathStats.to_dict() on the server.
  const stats = new Map();
  for (const ev of events) {
    let s = stats.get(ev.path);
    if (!s) {
      s = { count:0, last_ts:"", actions:{}, tools:{},
            sessions:new Set(), projects:new Set(), sidechain:0, sensitive:false };
      stats.set(ev.path, s);
    }
    s.count++;
    if (ev.ts > s.last_ts) s.last_ts = ev.ts;
    s.actions[ev.action] = (s.actions[ev.action] || 0) + 1;
    s.tools[ev.tool] = (s.tools[ev.tool] || 0) + 1;
    if (ev.session) s.sessions.add(ev.session);
    if (ev.project) s.projects.add(ev.project);
    if (ev.sidechain) s.sidechain++;
    if (ev.sensitive) s.sensitive = true;
  }
  // Common-ancestor root.
  const paths = [...stats.keys()];
  let rootPath = paths[0];
  for (const p of paths.slice(1)) {
    while (!p.startsWith(rootPath + "/") && p !== rootPath) {
      const cut = rootPath.lastIndexOf("/");
      if (cut <= 0) { rootPath = "/"; break; }
      rootPath = rootPath.substring(0, cut);
    }
    if (rootPath === "/") break;
  }
  // All ancestors of every accessed path.
  const nodes = new Set([rootPath]);
  for (const p of paths) {
    let cur = p;
    while (cur && cur.length >= rootPath.length) {
      nodes.add(cur);
      if (cur === rootPath) break;
      const cut = cur.lastIndexOf("/");
      cur = cut <= 0 ? "/" : cur.substring(0, cut);
    }
  }
  // Build the parent → children index.
  const byParent = new Map();
  for (const n of nodes) {
    if (n === rootPath) continue;
    const cut = n.lastIndexOf("/");
    const parent = cut <= 0 ? "/" : n.substring(0, cut);
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(n);
  }
  function makeNode(path) {
    const s = stats.get(path);
    const kids = (byParent.get(path) || []).sort();
    const node = {
      name: path.substring(path.lastIndexOf("/") + 1) || path,
      path: path,
      type: kids.length > 0 ? "dir" : (path === rootPath ? "dir" : "file"),
    };
    if (s) {
      const primary = Object.keys(s.actions)
        .sort((a, b) => (ACTION_PRIORITY[a] ?? 99) - (ACTION_PRIORITY[b] ?? 99))[0]
        || "observe";
      node.access = {
        count: s.count, last_ts: s.last_ts, primary,
        actions: s.actions, tools: s.tools,
        sessions: [...s.sessions].sort(), projects: [...s.projects].sort(),
        sidechain: s.sidechain, sensitive: s.sensitive,
      };
    }
    if (kids.length) node.children = kids.map(makeNode);
    return node;
  }
  return makeNode(rootPath);
}

function applyFiltersAndRender(opts = {}) {
  const filtered = lastEvents.filter(eventPasses);
  const treeData = buildTreeFromEvents(filtered);
  root = d3.hierarchy(treeData);
  root.x0 = 0; root.y0 = 0;
  collapseUntouched(root);
  // skipFit: during time-slider playback we want the camera to stay put.
  // On manual filter changes we re-fit so the focused view is centered.
  if (!opts.skipFit) initialTransform = null;
  update(root);
}

function setTimeCutoff(ms, opts = {}) {
  timeCutoff = ms;
  const slider = document.getElementById("time-slider");
  slider.value = ms;
  const display = document.getElementById("time-display");
  display.textContent = ms >= timeMax ? "live" :
    new Date(ms).toLocaleString(undefined, { month: "short", day: "2-digit",
                                              hour: "2-digit", minute: "2-digit",
                                              second: "2-digit" });
  applyFiltersAndRender(opts);
}

function initTimeSlider(meta) {
  timeMin = meta.time_min ? Date.parse(meta.time_min) : 0;
  timeMax = meta.time_max ? Date.parse(meta.time_max) : 0;
  if (!Number.isFinite(timeMin) || !Number.isFinite(timeMax) || timeMax <= timeMin) {
    document.getElementById("time-slider").disabled = true;
    document.getElementById("time-play").disabled = true;
    return;
  }
  timeCutoff = timeMax;
  const slider = document.getElementById("time-slider");
  slider.min = timeMin;
  slider.max = timeMax;
  slider.value = timeMax;
  // Smooth dragging: ~1000 distinct stops across the whole range.
  slider.step = Math.max(1, Math.floor((timeMax - timeMin) / 1000));
  slider.oninput = () => {
    if (isPlaying) togglePlay();  // any manual scrub pauses playback
    setTimeCutoff(parseInt(slider.value, 10));
  };
}

function togglePlay() {
  const btn = document.getElementById("time-play");
  isPlaying = !isPlaying;
  btn.textContent = isPlaying ? "⏸" : "▶";
  if (isPlaying) {
    // If we're at the end, rewind to the start before playing.
    if (timeCutoff >= timeMax) setTimeCutoff(timeMin, { skipFit: true });
    // ~10 ticks/sec, advancing ~2% of the range per tick → ~5s playback.
    const stepSize = Math.max(1, Math.floor((timeMax - timeMin) / 50));
    playInterval = setInterval(() => {
      const next = Math.min(timeMax, timeCutoff + stepSize);
      setTimeCutoff(next, { skipFit: true });
      if (next >= timeMax) togglePlay();  // auto-pause at end
    }, 100);
  } else {
    if (playInterval) { clearInterval(playInterval); playInterval = null; }
  }
}
document.getElementById("time-play").onclick = () => togglePlay();

function renderFilterUI(kind, all, eventsBySession) {
  // kind: "sessions" | "projects"
  const body = document.getElementById(`filter-${kind}-body`);
  const selected = filters[kind] === null ? new Set(all) : new Set(filters[kind]);
  // Count events per option for context.
  const counts = {};
  for (const ev of lastEvents) {
    const k = kind === "sessions" ? ev.session : ev.project;
    if (!k) continue;
    counts[k] = (counts[k] || 0) + 1;
  }
  body.innerHTML = all.map(v => {
    const isChecked = selected.has(v);
    const label = kind === "sessions" ? `${v.slice(0, 8)}…` : v;
    return `<label><input type="checkbox" value="${v}" ${isChecked ? "checked" : ""}> `
         + `${label} <span style="color:var(--muted);margin-left:auto">${counts[v] || 0}</span></label>`;
  }).join("");
  body.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.onchange = () => {
      const picked = [...body.querySelectorAll("input:checked")].map(i => i.value);
      filters[kind] = picked.length === all.length ? null : picked;
      saveFilters();
      updateFilterSummaries();
      applyFiltersAndRender();
    };
  });
  // "all" / "none" shortcuts.
  document.querySelectorAll(`a[data-target=filter-${kind}-body]`).forEach(a => {
    a.onclick = (e) => {
      e.preventDefault();
      const want = a.dataset.mode === "all";
      body.querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = want; });
      filters[kind] = want ? null : [];
      saveFilters();
      updateFilterSummaries();
      applyFiltersAndRender();
    };
  });
}

function updateFilterSummaries() {
  for (const kind of ["sessions", "projects"]) {
    const all = (lastMeta && lastMeta[kind]) || [];
    const sel = filters[kind] === null ? all.length : filters[kind].length;
    document.getElementById(`filter-${kind}-count`).textContent =
      `(${sel}/${all.length})`;
  }
}

// Color resolved from CSS custom properties so the palette stays in one place.
const ACTION_COLORS = {
  write:   "var(--act-write)",
  edit:    "var(--act-edit)",
  read:    "var(--act-read)",
  bash:    "var(--act-bash)",
  list:    "var(--act-list)",
  observe: "var(--act-observe)",
};
const ACTION_LABELS = {
  write: "written", edit: "edited", read: "read",
  bash: "bash-touched", list: "listed", observe: "observed",
};
function colorFor(d) {
  if (d.data.access) return ACTION_COLORS[d.data.access.primary] || "var(--act-observe)";
  if (d.data.type === "dir") return "var(--dir)";
  return "var(--untouched)";
}

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
  const linkEnter = linkSel.enter().append("path")
      .attr("class", d => "link" + (d.target.data.access ? " touched" : ""));
  linkEnter.merge(linkSel)
      .attr("class", d => "link" + (d.target.data.access ? " touched" : ""))
      .attr("stroke", d => d.target.data.access ? colorFor(d.target) : null)
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
      .attr("class", "main")
      .attr("r", d => {
        const t = totalAccess(d);
        if (d.data.access) return Math.min(10, 3 + Math.log2(d.data.access.count + 1) * 1.6);
        if (t > 0) return 3 + Math.log2(t + 1) * 0.6;
        return 2.5;
      })
      .attr("fill", colorFor);

  // Sensitive paths get a red ring around the main circle.
  enter.filter(d => d.data.access && d.data.access.sensitive)
      .append("circle")
      .attr("class", "sensitive-ring")
      .attr("r", d => {
        const base = d.data.access
          ? Math.min(10, 3 + Math.log2(d.data.access.count + 1) * 1.6) : 4;
        return base + 3.5;
      })
      .attr("fill", "none")
      .attr("stroke", "var(--danger)")
      .attr("stroke-width", 1.4);

  enter.append("text")
      .attr("dy", "0.32em")
      .attr("x", d => (d.children || d._children) ? -8 : 8)
      .attr("text-anchor", d => (d.children || d._children) ? "end" : "start")
      .text(d => d.data.name);

  const merged = enter.merge(nodeSel);
  merged.attr("transform", d => `translate(${d.y},${d.x})`);
  merged.select("circle.main").attr("fill", colorFor);

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
  // Stacked bar showing the mix of actions that ever hit this path.
  const total = Object.values(a.actions).reduce((s, n) => s + n, 0) || 1;
  const order = ["write","edit","read","bash","list","observe"];
  const bar = order
    .filter(k => a.actions[k])
    .map(k => `<div title="${ACTION_LABELS[k]} × ${a.actions[k]}"
                    style="flex:${a.actions[k]};background:${ACTION_COLORS[k]}"></div>`)
    .join("");
  const actionRows = order
    .filter(k => a.actions[k])
    .map(k => `<li><span class="dot" style="background:${ACTION_COLORS[k]};
                  display:inline-block;width:8px;height:8px;border-radius:50%;
                  margin-right:6px;vertical-align:middle"></span>${ACTION_LABELS[k]} × ${a.actions[k]}</li>`)
    .join("");
  const tools = Object.entries(a.tools).sort((x, y) => y[1] - x[1])
    .map(([t, n]) => `<li>${t} × ${n}</li>`).join("");
  const sessions = a.sessions.map(s => `<li>${s.slice(0, 8)}…</li>`).join("");
  const projects = a.projects.map(p => `<li>${p}</li>`).join("");
  const sensChip = a.sensitive
    ? `<span class="sensitive-chip">⚠ sensitive</span>` : "";
  const canDiff = a.primary === "write" || a.primary === "edit" || a.actions.write || a.actions.edit;
  const diffBtn = canDiff
    ? `<button class="diff-btn" data-path="${(d.data.path || "").replace(/"/g, '&quot;')}"
              data-session="${(a.sessions && a.sessions[0]) || ""}"
              style="margin-left:8px;padding:2px 8px;background:var(--rule);
                     color:var(--ink);border:1px solid var(--rule);border-radius:3px;
                     cursor:pointer;font:inherit;font-size:11px">View diff</button>`
    : "";
  body.innerHTML = `
    <span class="primary-chip" style="background:${ACTION_COLORS[a.primary]}">
      ${ACTION_LABELS[a.primary]}
    </span>${sensChip}${diffBtn}
    <div class="action-bar">${bar}</div>
    <dl>
      <dt>type</dt><dd>${d.data.type}</dd>
      <dt>accesses</dt><dd>${a.count}</dd>
      <dt>last seen</dt><dd>${a.last_ts || "—"}</dd>
      <dt>sidechain</dt><dd>${a.sidechain}</dd>
    </dl>
    <h2>Actions</h2><ul>${actionRows}</ul>
    <h2>Tools</h2><ul class="tools">${tools}</ul>
    <h2>Sessions</h2><ul>${sessions}</ul>
    <h2>Projects</h2><ul>${projects}</ul>
  `;
  // Wire the "View diff" button if it exists.
  const btn = body.querySelector(".diff-btn");
  if (btn) btn.onclick = () => openDiff(btn.dataset.path, btn.dataset.session);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;" }[c]));
}

async function openDiff(absPath, sessionHint) {
  const modal = document.getElementById("diff-modal");
  document.getElementById("diff-title").textContent = absPath;
  document.getElementById("diff-stats").textContent = "loading…";
  document.getElementById("diff-body").innerHTML =
    `<div class="diff-empty">Fetching snapshot…</div>`;
  if (!modal.open) modal.showModal();

  const url = `/api/snapshot?path=${encodeURIComponent(absPath)}`
            + (sessionHint ? `&session=${encodeURIComponent(sessionHint)}` : "");
  let data;
  try {
    const r = await fetch(url);
    data = await r.json();
  } catch (e) {
    document.getElementById("diff-body").innerHTML =
      `<div class="diff-empty">Error: ${escapeHtml(e.message || e)}</div>`;
    return;
  }
  if (data.missing) {
    document.getElementById("diff-stats").textContent = "no snapshot";
    document.getElementById("diff-body").innerHTML =
      `<div class="diff-empty">
         No snapshot found for this path.<br>
         Expected filename: <code>${escapeHtml(data.expected_filename || "")}</code><br>
         Searched sessions: ${(data.searched_sessions || []).length}
       </div>`;
    return;
  }
  // Render the diff rows.
  const changeCount = data.diff.filter(r => r.t !== "equal").length;
  document.getElementById("diff-stats").textContent =
    `session ${data.session.slice(0, 8)}…  ·  `
    + `${data.snapshot.bytes}B → ${data.current.bytes}B  ·  `
    + `${changeCount} changed line${changeCount === 1 ? "" : "s"}`
    + (data.snapshot.truncated || data.current.truncated ? "  ·  (truncated)" : "");
  const rows = data.diff.map(r =>
    `<tr class="${r.t}">
       <td class="side left">${escapeHtml(r.l) || "&nbsp;"}</td>
       <td class="side right">${escapeHtml(r.r) || "&nbsp;"}</td>
     </tr>`
  ).join("");
  document.getElementById("diff-body").innerHTML =
    `<table><tbody>${rows || `<tr><td colspan=2 class="diff-empty">No differences</td></tr>`}</tbody></table>`;
}

document.getElementById("diff-close").onclick = () =>
  document.getElementById("diff-modal").close();
// Click on backdrop closes the dialog.
document.getElementById("diff-modal").addEventListener("click", (e) => {
  if (e.target.id === "diff-modal") e.target.close();
});

function setMeta(meta) {
  document.getElementById("meta").textContent =
    `${meta.event_count} events · ${meta.unique_paths} paths · root: ${meta.root} · scanned ${meta.scanned_at} (${meta.scan_ms}ms)`;
}

let sensitivePaths = [];
function setAlert(meta) {
  sensitivePaths = meta.sensitive_paths || [];
  const alertEl = document.getElementById("alert");
  if ((meta.sensitive_count || 0) > 0) {
    document.getElementById("alert-text").textContent =
      `Claude has touched ${meta.sensitive_count} sensitive path${meta.sensitive_count === 1 ? "" : "s"} — review.`;
    alertEl.classList.add("visible");
  } else {
    alertEl.classList.remove("visible");
  }
}

// Walk the d3 hierarchy to find a node by absolute path; expand ancestors,
// scroll/zoom to it, and open the sidebar.
function focusPath(targetPath) {
  if (!root) return;
  let found = null;
  function walk(d) {
    if (found) return;
    if (d.data.path === targetPath) { found = d; return; }
    const kids = (d.children || []).concat(d._children || []);
    for (const k of kids) walk(k);
  }
  walk(root);
  if (!found) return;
  // Expand ancestors
  for (let p = found.parent; p; p = p.parent) {
    if (p._children) { p.children = p._children; p._children = null; }
  }
  update(found);
  showDetails(found);
  // Pan/zoom to bring the node roughly to center.
  setTimeout(() => {
    const t = d3.zoomTransform(svg.node());
    const w = svg.node().clientWidth, h = svg.node().clientHeight;
    const tx = w / 2 - found.y * t.k;
    const ty = h / 2 - found.x * t.k;
    svg.transition().duration(450).call(zoom.transform,
      d3.zoomIdentity.translate(tx, ty).scale(t.k));
  }, 50);
}

document.getElementById("alert-show").onclick = () => {
  if (!sensitivePaths.length) return;
  // Open the sidebar with a clickable list of sensitive paths.
  document.getElementById("sel-path").textContent = `${sensitivePaths.length} sensitive paths`;
  const listed = sensitivePaths.map(p =>
    `<li><a href="#" data-path="${p.replace(/"/g, '&quot;')}" style="color:var(--danger-soft);text-decoration:none">${p}</a></li>`
  ).join("");
  document.getElementById("sel-body").innerHTML = `<ul style="list-style:none;padding:0">${listed}</ul>`;
  document.querySelectorAll("#sel-body a[data-path]").forEach(a => {
    a.onclick = (e) => { e.preventDefault(); focusPath(a.dataset.path); };
  });
};

async function load(rescan) {
  const r = await fetch(rescan ? "/api/rescan" : "/api/tree");
  const { tree: data, events, meta } = await r.json();
  lastEvents = events || [];
  lastMeta = meta;
  setMeta(meta);
  setAlert(meta);
  // Render the filter UIs against the new option lists.
  renderFilterUI("sessions", meta.sessions || []);
  renderFilterUI("projects", meta.projects || []);
  updateFilterSummaries();
  initTimeSlider(meta);
  // Unfiltered → use the server's tree (it includes sibling context the
  // client can't reconstruct without disk access). Otherwise rebuild locally.
  if (!isFilterActive()) {
    root = d3.hierarchy(data);
    root.x0 = 0; root.y0 = 0;
    collapseUntouched(root);
    initialTransform = null;
    update(root);
  } else {
    applyFiltersAndRender();
  }
}

document.getElementById("rescan").onclick = () => load(true);
document.getElementById("reset").onclick = () => recenter();

// --- Viewport controls -----------------------------------------------------
// Two ways to never lose the tree: floating overlay buttons (always visible
// in the top-right of the viz area) and keyboard shortcuts.
function recenter() {
  initialTransform = null;
  update(root);
}
function zoomBy(factor) {
  svg.transition().duration(180).call(zoom.scaleBy, factor);
}
document.getElementById("recenter").onclick = recenter;
document.getElementById("zoom-in").onclick  = () => zoomBy(1.4);
document.getElementById("zoom-out").onclick = () => zoomBy(1 / 1.4);

// Keyboard shortcuts. Ignored when focus is in an input/textarea/select or
// when a modifier key is held (so e.g. Cmd-R still reloads the page).
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target && e.target.matches && e.target.matches("input, textarea, select, [contenteditable]")) return;
  const k = e.key;
  if (k === "r" || k === "R" || k === "0" || k === "Home") {
    e.preventDefault();
    recenter();
    // Flash the button so users discover the shortcut works.
    const el = document.getElementById("recenter");
    el.classList.remove("flash");
    void el.offsetWidth;  // restart animation
    el.classList.add("flash");
  } else if (k === "+" || k === "=") {
    e.preventDefault(); zoomBy(1.4);
  } else if (k === "-" || k === "_") {
    e.preventDefault(); zoomBy(1 / 1.4);
  } else if (k === "Escape") {
    // Close the diff modal first if it's open; otherwise close any
    // open filter dropdowns (a small bonus polish).
    const dlg = document.getElementById("diff-modal");
    if (dlg.open) { dlg.close(); return; }
    document.querySelectorAll("details.filter[open]").forEach(d => (d.open = false));
  }
});

// Watch mode: listen for tree-updated SSE events and re-fetch.
// Suppressed during playback so the user's animation isn't interrupted.
let sseSource = null;
function startSse() {
  if (sseSource) sseSource.close();
  try {
    sseSource = new EventSource("/api/events");
  } catch { return; }
  sseSource.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "tree-updated" && !isPlaying) load(false);
    } catch {}
  };
  // EventSource auto-reconnects on transient errors; nothing to do.
}
startSse();

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
    ap.add_argument("--sensitive-patterns", type=Path, default=None, metavar="FILE",
                    help="Path to a file with custom sensitive-path regexes (one per line, "
                         "# for comments). Replaces the built-in list.")
    ap.add_argument("--no-watch", action="store_true",
                    help="Disable the file watcher (no auto-refresh on transcript changes).")
    ap.add_argument("--watch-interval", type=float, default=2.0, metavar="SEC",
                    help="Watcher poll interval (default: %(default)s seconds).")
    ap.add_argument("--file-history", type=Path, default=DEFAULT_FILE_HISTORY_DIR,
                    help="Path to Claude Code's file-history dir (default: %(default)s). "
                         "Used by the diff viewer.")
    args = ap.parse_args(argv)

    full_walk = args.full_walk
    if args.full_home and not full_walk:
        full_walk = str(Path.home())

    sensitive_patterns: list[str] | None = None
    if args.sensitive_patterns:
        try:
            sensitive_patterns = args.sensitive_patterns.read_text().splitlines()
        except OSError as e:
            print(f"[clawreach] could not read --sensitive-patterns: {e}", file=sys.stderr)
            return 2

    cache = _Cache(args.projects, args.root, full_walk, sensitive_patterns,
                   file_history_dir=args.file_history)

    if args.print_only:
        tree, events, meta = cache.get()
        json.dump({"tree": tree, "events": _events_to_wire(events), "meta": meta},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    # Warm the cache up front so the first request is instant.
    tree, _events, meta = cache.get()
    print(f"[clawreach] {meta['event_count']} tool events across "
          f"{meta['unique_paths']} unique paths from {meta['transcripts_dir']}",
          file=sys.stderr)
    print(f"[clawreach] tree root: {meta['root']}  (scan {meta['scan_ms']}ms)", file=sys.stderr)

    handler = make_handler(cache, FRONTEND_HTML)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[clawreach] serving at {url}", file=sys.stderr)

    if not args.no_watch:
        cache.start_watch(interval=args.watch_interval)
        print(f"[clawreach] watching {args.projects} (poll every {args.watch_interval}s)",
              file=sys.stderr)

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.3), webbrowser.open(url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[clawreach] bye.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
