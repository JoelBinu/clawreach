# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ClawReach is a **single-file Python tool** (`clawreach.py`, ~2.2k lines, stdlib-only) that parses your local Claude Code transcripts (`~/.claude/projects/**/*.jsonl`), extracts every path Claude touched via tool calls, and serves a D3 collapsible tree at `http://127.0.0.1:8765/` showing the slice of the filesystem Claude reached and what it *did* there (write / edit / read / bash / list / observe).

No build system, no test suite, no lint config, no `pip install` — `python3 clawreach.py` is the entire interface. The frontend (HTML/CSS/JS + D3 from CDN) lives inside `clawreach.py` as the `FRONTEND_HTML` string constant; there is no separate frontend project.

## Run / smoke-test

```bash
python3 clawreach.py                # serve at http://127.0.0.1:8765/, opens browser
python3 clawreach.py --no-browser   # serve without auto-opening
python3 clawreach.py --print-only   # dump {tree, events, meta} JSON to stdout and exit
python3 clawreach.py --no-watch     # disable SSE auto-refresh on transcript changes
```

When iterating, the standard loop is: edit `clawreach.py` → kill the running server (`lsof -nP -iTCP:8765 -sTCP:LISTEN -t | xargs kill`) → re-run → curl the JSON endpoints to check shape, or reload the browser. `--print-only` is the fastest way to verify backend changes without standing up the server.

See `README.md` for the full CLI flag list and user-facing docs.

## Architecture: three-stage pipeline

All three stages are in `clawreach.py`, separated by `# ---` banner comments:

1. **Ingest** (`parse_transcript`, `ingest_all`). Walks JSONL files and emits `AccessEvent(path, tool, action, ts, session, project, is_sidechain, sensitive)` records. Three signal sources, in order of authority — **when adding a new way to extract paths, decide which bucket it belongs to**:
   1. `tool_use` blocks on `assistant` entries — most authoritative. Tool → action mapping lives in `PATH_KEY_TOOLS`, `DIR_KEY_TOOLS`, and `_bash_action` (Bash dispatches by command verb + `>`/`>>` redirect presence).
   2. `file-history-snapshot` entries — Claude Code's own ledger of files it wrote/edited (`snapshot.trackedFileBackups`). Use this for ground-truth writes, not heuristics.
   3. `tool_result` blocks on `user` entries — mine output text for absolute paths. Tagged `observe` (weakest signal) except for `Write`'s `"File created successfully at: <path>"` which is upgraded to `write`. Capped at `MAX_MINED_PATHS_PER_RESULT` so a chatty `npm install` can't drown the tree.

2. **Aggregate + tree** (`aggregate`, `build_tree`, `_collect_siblings`, `_materialize`). Events fold into `PathStats` per path; the tree is rooted at the common ancestor of accessed paths and includes every ancestor. `--siblings` opt-in adds one level of untouched neighbor leaves via `_collect_siblings` (`sibling_depth=0` is the default — siblings are noisy on large corpora).

3. **Serve** (`_Cache`, `make_handler`, `FRONTEND_HTML`). Stdlib `ThreadingHTTPServer`. Endpoints: `GET /api/tree`, `GET /api/rescan`, `GET /api/events` (SSE, `tree-updated` deltas pushed by a background mtime-polling watcher thread), `GET /api/snapshot?path=&session=` (side-by-side diff via `difflib.SequenceMatcher` against `~/.claude/file-history/<sess>/<sha256(abs_path)[:16]>@v2`).

## Server / client tree symmetry — important

The frontend has its own tree builder, `buildTreeFromEvents(events, extraSiblingPaths)` in `FRONTEND_HTML`, that **must mirror** server-side `aggregate` + `build_tree`'s output shape (the d3 hierarchy consumes it identically). It's used whenever any filter is active (sessions / projects / actions / missing / time slider) because filtering happens client-side over the events array.

If you change `PathStats.to_dict()` or `_materialize`'s node shape, **also update `buildTreeFromEvents` and `makeNode` in `FRONTEND_HTML`**, or the filtered view will silently diverge from the unfiltered view. The client has no disk access, so anything that requires `os.path.*` (e.g. siblings, file existence) must be precomputed server-side and shipped in `meta` or per-event flags (see how `sibling_paths`, `missing`, `sensitive` are propagated for examples).

## Filter dimensions

There are five orthogonal filters, all client-side, all persisted to `localStorage` under `clawreach.filters`:

- `sessions: string[] | null` — header dropdown
- `projects: string[] | null` — header dropdown
- `actions: string[] | null` — click colored dots in the bottom-left legend
- `includeMissing: boolean` — header checkbox (when off, paths not on disk are hidden)
- time cutoff — header slider; events with `ts > cutoff` are filtered out

`null` means "no constraint" (all selected). `isFilterActive()` returns true if any filter is non-default; that gates the server-tree vs client-rebuilt path in `load()`. Adding a sixth filter dimension means: extend `loadFilters`, `eventPasses`, `isFilterActive`, and add UI wiring.

## Sensitive paths config (user-owned)

The sensitive-path audit ("AI-free zones") reads patterns from `~/.clawreach/sensitive_paths.txt` (override with `--sensitive-paths FILE`). The hardcoded list was removed deliberately; defaults are *empty* so there are no false positives. Format: one line per pattern, `~`-expanded; trailing `/` = directory + contents, `*` / `?` / `[` = fnmatch glob, else exact-or-prefix match. See `sensitive_paths.example.txt` for the canonical reference.

Compilation lives in `_compile_sensitive(lines)`, which returns a closure `(path) → bool` (not a regex). The pattern types are dispatched internally — don't add regex support without explicit reason; the path/glob format was a deliberate choice for user-friendliness.

## Frontend gotchas

These caused real bugs and are easy to re-introduce:

- **Layout uses `grid-template-areas`** on `<body>` (not `grid-template-rows` alone). The alert banner has `display: none` when no sensitive paths are visible; with un-named rows, grid auto-placement would slide `<main>` up into the auto row and collapse the viz area to the SVG's 150px intrinsic height. Pin elements to named areas if you add more body-level children.
- **Fit-to-view is deferred to `requestAnimationFrame`** inside `update()` (calls `fitToView()`). `setAlert()` runs before `update()`, but CSS class changes don't reflow synchronously, so measuring `clientHeight` immediately reads stale dimensions. The RAF defer waits for the browser's layout pass before measuring.
- **Filter changes auto-fit by default; time-slider playback passes `skipFit: true`** to avoid jittering the camera every tick. Other callers that explicitly want to preserve the viewport should do the same.
- **`buildTreeFromEvents` does not include siblings unless `meta.sibling_paths` is non-empty** (i.e. `--siblings` was set on the server). The server emits the sibling list in meta so the client can re-include them after filtering — without this the filtered view silently drops them.

## When committing

- The README is the user-facing source of truth. New flags, endpoints, meta keys, or visible behavior changes must be reflected there (and usually in `CHANGELOG.md` if user-visible).
- Per-feature commits with descriptive bodies — recent history is a good template. Commits are attributed to `Joel Binu <48750567+JoelBinu@users.noreply.github.com>` (local git config in this repo). Don't change the identity.
- Don't create test fixtures with fake paths (e.g. `/Users/jane/...`) inside `~/.claude/projects/` or `/tmp/clawreach-test/`. The ingester picks them up from the conversation transcript and pollutes the real tree. If you need test data, use `--projects /some/throwaway/dir` pointing somewhere that won't get picked up.
