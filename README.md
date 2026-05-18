# ClawReach

> See which parts of your filesystem Claude Code has actually reached into — and what it *did* there.

ClawReach parses your local [Claude Code](https://docs.claude.com/en/docs/claude-code) transcripts, extracts every file and directory the agent (and any subagents) touched, and renders the surrounding filesystem as a collapsible D3 tree. Each node is colored by what Claude did to it — **written, edited, read, bash-touched, listed, or observed in output** — so the signal is obvious at a glance.

**v2 highlights:** sensitive-path audit (configurable per user — see [Sensitive paths](#sensitive-paths) below) · time slider to replay a session · session/project filters · live auto-refresh via SSE · click any written/edited file to **diff exactly what Claude wrote** against the current state, using Claude Code's own file-history snapshots.

## Sensitive paths

ClawReach can flag accesses to "AI-free zones" — paths you'd rather Claude didn't touch. There are no built-in defaults; the feature is opt-in via a file you own:

```
~/.clawreach/sensitive_paths.txt
```

Each non-comment line is a directory, glob, or path:

```
~/.ssh/              # trailing slash → that dir and everything under it
~/.aws/
*.env                # glob → matches basename and full path (fnmatch)
*.pem
~/secrets.txt        # exact file path
```

If the file doesn't exist, the sensitive-path banner stays off — no false positives. Override the location with `--sensitive-paths FILE`. See [`sensitive_paths.example.txt`](sensitive_paths.example.txt) for a starter template you can copy.

When you do configure it, every matched access is tagged in the JSON (`access.sensitive: true`), gets a red ring around the tree node, a "⚠ sensitive" chip in the sidebar, and counts toward the red banner at the top of the page.

```
┌─ ClawReach ── 147 events · 52 paths · root: / ──────[Re-scan][Reset]──┐
│                                                                       │
│  /                                                                    │
│  └── Users/you                                                        │
│      ├── .claude/                                                     │
│      │   └── projects/  ◆ list                                        │
│      │       └── <encoded-project>/  ◆ read                           │
│      └── Desktop/ClawReach/                                           │
│          ├── clawreach.py  ● write   (created)                        │
│          ├── README.md     ● write   (created)                        │
│          ├── .gitignore    ◆ edit    (modified)                       │
│          └── LICENSE       ● write                                    │
│                                                                       │
│  ● write   ◆ edit   ● read   ● bash   ● list   ● observe              │
│  size = access count   ·   color = primary action                     │
└───────────────────────────────────────────────────────────────────────┘
```

## Why

You can scroll a transcript to see what Claude did — but you can't see *where* it happened in relation to the rest of your filesystem. ClawReach answers questions like:

- Which files did Claude **write or generate** this session?
- What did it merely **read** vs **modify in place**?
- Did it stay inside the project, or wander into `~/.ssh`?
- Across all my sessions, which paths have been touched most often, and how?
- Did a subagent reach into something the main thread never opened?

## Requirements

- Python 3.9+
- A modern browser
- Stdlib only — no `pip install` needed.

## Quick start

```bash
git clone https://github.com/<your-user>/clawreach.git
cd clawreach
python3 clawreach.py
```

The default port is `8765`; your browser opens to `http://127.0.0.1:8765/` automatically. The tree defaults to just the paths Claude touched and the directories that connect them — fast, focused, no whole-disk walks. Pass `--siblings` to also show one level of untouched neighbors for context; when enabled, the header shows `siblings: +N` so you can confirm at a glance that the neighbors are in the data (they appear as dim gray leaf nodes alongside touched branches, and persist correctly even when a filter is active).

## How it works

Three stages, all in one stdlib-only Python file:

1. **Ingest.** Walk `~/.claude/projects/**/*.jsonl` and collect access events from three signal sources, in order of authority:

   1. **`tool_use` blocks** on assistant entries. Each call is mapped to one or more `(path, action)` pairs:

      | Tool | Action | Path source |
      |---|---|---|
      | `Read`, `NotebookRead` | `read` | `input.file_path` / `notebook_path` |
      | `Write` | `write` | `input.file_path` |
      | `Edit`, `MultiEdit`, `NotebookEdit` | `edit` | `input.file_path` / `notebook_path` |
      | `Glob`, `Grep`, `LS` | `list` | `input.path` (falls back to entry `cwd`) |
      | `Bash` | `read` / `write` / `list` / `bash` | path tokens from `input.command`; action picked from the command verb (`cat`/`ls`/`mkdir`/…) and presence of `>` / `>>` redirects |

   2. **`file-history-snapshot` entries.** Claude Code keeps its own ledger of files it modified in `snapshot.trackedFileBackups`. These are emitted as authoritative `write` events — better than any heuristic.

   3. **`tool_result` blocks** (on user entries). For `Bash`/`LS`/`Glob`/`Grep`, absolute paths surfaced in the result text become `observe` events — files Claude *saw* but never directly opened. The Write tool's `"File created successfully at: <path>"` confirmation is upgraded to `write`. Capped per result so a noisy `npm install` doesn't drown out the tree.

2. **Scan.** Build a tree rooted at the common ancestor of every accessed path. Include all ancestors of every accessed path. With `--siblings`, also add one level of immediate siblings around each accessed directory so you see what's *next to* the file Claude touched. Skip noise dirs (`.git`, `node_modules`, `__pycache__`, build caches, IDE caches, etc.).

3. **Serve.** A stdlib `ThreadingHTTPServer` exposes:
   - `GET /` — the single-page D3 frontend
   - `GET /api/tree` — cached tree as JSON
   - `GET /api/rescan` — re-ingest transcripts and rebuild the tree

   The frontend collapses any subtree that contains zero accessed nodes, so the default view is just the slice Claude has actually reached. Each touched node is colored by its **primary action** (`write > edit > read > bash > list > observe`). Click a node to expand/collapse; the sidebar shows the full action breakdown (stacked bar + per-action counts) and per-session/per-project attribution.

## CLI reference

```
python3 clawreach.py [options]

  --projects PATH          Transcripts directory (default: ~/.claude/projects)
  --file-history PATH      Snapshot dir for diff viewer (default: ~/.claude/file-history)
  --port N                 HTTP port (default: 8765)
  --host ADDR              Bind address (default: 127.0.0.1)
  --root PATH              Override tree root (default: common ancestor of all accessed paths)
  --siblings               Show one level of untouched siblings for context (default: off)
  --full-walk DIR          Additionally walk this whole subtree (slow)
  --full-home              Shortcut for --full-walk $HOME
  --sensitive-paths FILE   Paths/dirs/globs you consider AI-free (default: ~/.clawreach/sensitive_paths.txt).
                           If the file doesn't exist, no paths are flagged. See sensitive_paths.example.txt.
  --no-watch               Disable file watcher (no SSE auto-refresh)
  --watch-interval SEC     Watcher poll interval (default: 2.0)
  --no-browser             Don't auto-open the browser
  --print-only             Dump the JSON tree to stdout and exit (no server)
```

## API

| Endpoint | Returns |
|---|---|
| `GET /api/tree` | `{tree, events, meta}` — full state |
| `GET /api/rescan` | same, after re-ingesting |
| `GET /api/events` | Server-Sent Events stream of `{"type":"tree-updated", "scanned_at", "event_count"}` deltas |
| `GET /api/snapshot?path=<abs>&session=<hint>` | `{snapshot, current, diff, missing, session}` for the diff viewer |

`/api/tree` and `/api/rescan` return `{tree: <node>, events: [...], meta: <stats>}`.

A `<node>` is:

```jsonc
{
  "name": "clawreach.py",
  "path": "/abs/path/clawreach.py",
  "type": "file",                  // or "dir"
  "access": {                      // present only if the node was touched
    "count": 7,
    "last_ts": "2026-05-18T16:03:29.074Z",
    "primary": "write",            // strongest signal: write > edit > read > bash > list > observe
    "actions": { "write": 1, "read": 4, "edit": 2 },
    "tools":   { "Write": 1, "Read": 4, "Edit": 2 },
    "sessions": ["8e5900ea-..."],
    "projects": ["-Users-you-Desktop-ClawReach"],
    "sidechain": 0,
    "sensitive": false             // matched a sensitive-path regex
  },
  "children": [ /* nested <node>s */ ]
}
```

`<meta>` contains: `event_count`, `unique_paths`, `transcripts_dir`, `root`, `scanned_at`, `scan_ms`, `sessions[]`, `projects[]`, `time_min`, `time_max`, `sensitive_count`, `sensitive_paths[]`.

Events (`<wire-event>`) are compact: `{path, tool, action, ts, session, project, sidechain?, sensitive?}` — booleans are omitted when false.

## Caveats

- **Bash classification is heuristic.** Path extraction keeps tokens matching `^~?/[\w./\-+@:%]+` (precision over recall). Action classification looks at the command's first verb (`cat`→read, `mkdir`→write, etc.) and the presence of `>` / `>>`. Files generated by tools like `gcc`, `npm install`, `python build.py` won't be tagged as `write` unless the command itself redirects — for those, [`file-history-snapshot`](#how-it-works) is the authoritative source for files Claude wrote through its own tools, but it can't see Bash side effects.
- **`observe` is a weak signal.** It's anything that surfaced as an absolute path in tool result text. Useful for context (Claude knew this file exists), but doesn't mean Claude opened it. The frontend colors these the most muted of any category.
- **Siblings are off by default.** Pass `--siblings` to add one level of untouched neighbors for context. Depth beyond 1 isn't implemented — `build_tree` collects a single level. The header shows `siblings: +N` when active, and the sibling paths are emitted in `meta.sibling_paths` so the client-side filter rebuild can include them too.
- **Transcripts live on disk and grow.** The ingester rescans them all on every `/api/rescan`. For very large transcript corpora, add an mtime-based incremental cache.
- **Symlinks aren't dereferenced.** Paths are normalized but not resolved through symlinks; the tree shows what the transcript literally referenced.

## Contributing

Issues and PRs welcome. The code is intentionally a single file so it stays approachable — please keep it stdlib-only unless there's a strong reason otherwise.

## License

MIT — see [LICENSE](LICENSE).
