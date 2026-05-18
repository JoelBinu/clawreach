# ClawReach

> See which parts of your filesystem Claude Code has actually reached into.

ClawReach parses your local [Claude Code](https://docs.claude.com/en/docs/claude-code) transcripts, extracts every file and directory the agent (and any subagents) touched via tool calls, and renders the surrounding filesystem as a collapsible D3 tree. Touched branches are highlighted; everything else is collapsed by default so the signal is obvious at a glance.

```
┌─ ClawReach ── 31 events · 18 paths · root: / ───────[Re-scan][Reset]──┐
│                                                                       │
│  /                                                                    │
│  └── Users                                                            │
│      └── you                                                          │
│          ├── .claude/                                                 │
│          │   └── projects/  ●                                         │
│          │       ├── <encoded-project-A>/  ●●●                        │
│          │       └── <encoded-project-B>/  ●                          │
│          └── Desktop/                                                 │
│              └── ClawReach/  ●●●●                                     │
│                  ├── clawreach.py  ●●●                                │
│                  └── README.md                                        │
│                                                                       │
│  ● = touched by Claude  ·  size encodes access count                  │
└───────────────────────────────────────────────────────────────────────┘
```

## Why

You can scroll a transcript to see what Claude did — but you can't see *where* it happened in relation to the rest of your filesystem. ClawReach answers questions like:

- Which directories did Claude actually open files from this session?
- Did it stay inside the project, or wander into `~/.ssh`?
- Across all my sessions, which files have been touched most often?
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

The default port is `8765`; your browser opens to `http://127.0.0.1:8765/` automatically. The tree defaults to "every path Claude touched, plus one level of siblings for context" — fast, focused, no whole-disk walks.

## How it works

Three stages, all in one stdlib-only Python file (~450 lines):

1. **Ingest.** Walk `~/.claude/projects/**/*.jsonl`. For each `assistant`-typed entry, iterate the `message.content[]` blocks and pull every `tool_use`. Map the tool to the path(s) it touched:
   - `Read` / `Edit` / `Write` / `MultiEdit` / `NotebookEdit` / `NotebookRead` → `input.file_path` (or `notebook_path`)
   - `Glob` / `Grep` / `LS` → `input.path` (falls back to the entry's `cwd`)
   - `Bash` → shlex-split the command, keep tokens that look like absolute or `~`-relative paths

   Each hit becomes an `AccessEvent(path, tool, timestamp, session, project, is_sidechain)`.

2. **Scan.** Build a tree rooted at the common ancestor of every accessed path. Include all ancestors of every accessed path, and add one level of immediate siblings around each accessed directory so the user sees what's *next to* the file Claude touched. Skip noise dirs (`.git`, `node_modules`, `__pycache__`, build caches, IDE caches, etc.).

3. **Serve.** A stdlib `ThreadingHTTPServer` exposes:
   - `GET /` — the single-page D3 frontend
   - `GET /api/tree` — cached tree as JSON
   - `GET /api/rescan` — re-ingest transcripts and rebuild the tree

   The frontend collapses any subtree that contains zero accessed nodes, so the default view is just the slice Claude has actually reached. Click a node to expand/collapse; the sidebar shows per-node stats (count, last seen, tool breakdown, sessions, projects, sidechain count).

## CLI reference

```
python3 clawreach.py [options]

  --projects PATH    Transcripts directory (default: ~/.claude/projects)
  --port N           HTTP port (default: 8765)
  --host ADDR        Bind address (default: 127.0.0.1)
  --root PATH        Override tree root (default: common ancestor of all accessed paths)
  --full-walk DIR    Additionally walk this whole subtree (slow)
  --full-home        Shortcut for --full-walk $HOME
  --no-browser       Don't auto-open the browser
  --print-only       Dump the JSON tree to stdout and exit (no server)
```

## API

Two JSON endpoints, both return `{ "tree": <node>, "meta": <stats> }`.

A `<node>` is:

```jsonc
{
  "name": "clawreach.py",
  "path": "/abs/path/clawreach.py",
  "type": "file",                  // or "dir"
  "access": {                      // present only if the node was touched
    "count": 7,
    "last_ts": "2026-05-18T16:03:29.074Z",
    "tools": { "Read": 4, "Edit": 3 },
    "sessions": ["8e5900ea-..."],
    "projects": ["-Users-you-Desktop-ClawReach"],
    "sidechain": 0
  },
  "children": [ /* nested <node>s */ ]
}
```

`<meta>` contains `event_count`, `unique_paths`, `transcripts_dir`, `root`, `scanned_at`, `scan_ms`.

## Caveats

- **Bash path extraction is heuristic.** It keeps tokens matching `^~?/[\w./\-+@:%]+` and ignores everything else. This favors precision over recall — better to miss the occasional path than flood the tree with `-la`, `HEAD`, or `main`. If you want better Bash coverage, swap `_extract_bash_paths` for something AST-based (e.g. `bashlex`).
- **Sibling depth is fixed at 1.** Enough for context, not enough for clutter. Bump it by editing `build_tree`.
- **Transcripts live on disk and grow.** The ingester rescans them all on every `/api/rescan`. For very large transcript corpora, add an mtime-based incremental cache.
- **Symlinks aren't dereferenced.** Paths are normalized but not resolved through symlinks; the tree shows what the transcript literally referenced.

## Contributing

Issues and PRs welcome. The code is intentionally a single file so it stays approachable — please keep it stdlib-only unless there's a strong reason otherwise.

## License

MIT — see [LICENSE](LICENSE).
