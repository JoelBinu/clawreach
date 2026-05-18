# Changelog

## v2 — 2026-05-18

### New
- **Sensitive-path audit.** Every access is checked against a curated regex list (`~/.ssh`, `~/.aws`, `~/.gnupg`, `*.env`, `*.pem`, `*.key`, `credentials*`, etc.). A red banner shows the count at the top of the page; matched nodes get a red SVG ring; the sidebar gains a "⚠ sensitive" chip. Override the patterns with `--sensitive-patterns FILE`.
- **Time slider.** Header slider + play/pause button. Scrub to filter events by timestamp; play to watch Claude's reach grow over a session.
- **Session & project filters.** Two chip-style `<details>` dropdowns in the header. Selections persist to `localStorage`. Filtering rebuilds the d3 hierarchy client-side from the events array.
- **Watch mode.** Background thread polls JSONL mtimes; on change, all SSE clients receive a `tree-updated` event and the frontend re-fetches `/api/tree`. Disable with `--no-watch`; tune cadence with `--watch-interval SEC`.
- **Diff viewer.** Click any write/edit node to open a side-by-side diff against the current on-disk state, computed from Claude Code's `~/.claude/file-history/<sessionId>/<sha256(path)[:16]>@v2` snapshots. Override the snapshot dir with `--file-history PATH`.

### New endpoints
- `GET /api/events` — Server-Sent Events stream of cache updates.
- `GET /api/snapshot?path=<abs>&session=<hint>` — snapshot + current content + structured diff rows.

### Changed
- `/api/tree` and `/api/rescan` now return `{tree, events, meta}` (events array added). Meta gains `sessions`, `projects`, `time_min`, `time_max`, `sensitive_count`, `sensitive_paths`.

## v1 — 2026-05-18

Initial release.
- Parse `~/.claude/projects/**/*.jsonl` for `tool_use`, `file-history-snapshot`, and `tool_result` blocks.
- Categorize every access as `write` / `edit` / `read` / `bash` / `list` / `observe` and color tree nodes by the primary action.
- Build a tree rooted at the common ancestor of accessed paths, with one level of siblings for context.
- Serve at `http://127.0.0.1:8765/` — single-file Python, stdlib-only (D3 from CDN).
