# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`social-learning` is a compact, local-first document manager. The **entire
application — HTTP server, HTML, CSS, and JavaScript — lives in the single file
`app.py`**, using only the Python 3.10+ standard library (no third-party
dependencies, no build step). User content is stored as plain files under
`content/`, so the git repo doubles as the datastore: contributors add or edit
documents and push them.

## Run / develop

```sh
python3 app.py                 # serve http://127.0.0.1:8000 and open a browser
python3 app.py --port 9000     # choose a port
python3 app.py --host 0.0.0.0  # bind other interfaces (still no auth — see below)
python3 app.py --no-open       # don't launch a browser (use in scripts/CI)
```

There is no test suite, linter config, or package manifest. Quick checks used
during development:

```sh
python3 -c "import ast; ast.parse(open('app.py').read())"   # syntax check
# The markdown renderer is pure JS and can be exercised under node by slicing
# the <script> block out of app.py (see git history for the pattern).
```

Note: `basedpyright` may flag a few type warnings in `app.py` (e.g. the
`log_message` signature, `_send` accepting `str`). These are intentional and
handled at runtime (`_send` converts `str`→`bytes` via `isinstance`); do not
"fix" them by narrowing the types unless you preserve the runtime behavior.

## Architecture

Two halves that communicate over a small JSON/REST API:

1. **Python server** (top of `app.py`) — a `ThreadingHTTPServer` with one
   `Handler`. Routes:
   - `GET /` → serves the embedded `HTML` string (the whole front-end).
   - `GET /api/tree` → the document hierarchy as nested JSON (`build_tree`).
   - `GET|PUT /api/doc?path=…` → read / write a markdown document.
   - `POST /api/create`, `POST /api/rename`, `DELETE /api/doc` → tree mutations.
   - `POST /api/upload` (raw body, filename in `X-Filename` header) → save an
     attachment, returns its URL.
   - `GET /content/…` → serve documents and attachments, **with HTTP Range
     support** so recorded/embedded video can seek.

2. **Front-end** — the `HTML` string constant (a raw string, `r"""..."""`)
   holds the markup, CSS, and a vanilla-JS SPA: sidebar tree, markdown editor
   with debounced autosave + live preview, paste handling, `MediaRecorder`
   audio/video capture, and a bilingual (EN/IT) info modal (`#about`/`#modal`).

### On-disk layout (this IS the data model)

```
content/
  <folder>/<document>.md   folders form the tree; one markdown file per document
  _assets/                 pasted images/files and audio/video recordings
```

The tree hierarchy is literally the directory structure under `content/`.
`_assets/` and dotfiles are hidden from the tree (`build_tree`). Attachment
names get a `YYYYMMDD-HHMMSS-<salt>-` prefix for uniqueness.

## Conventions and constraints that aren't obvious from a quick read

- **Keep it one file, stdlib-only.** The value proposition is a
  zero-dependency, single-file app that runs anywhere Python does
  (Windows/macOS/Linux). Do not add packages, split modules, or introduce a
  build step without a strong reason.
- **All paths go through `safe_join`.** It resolves against `content/` and
  raises `PermissionError` on traversal (`../`). Never open a
  request-supplied path directly.
- **All user-supplied names go through `clean_name`.** It enforces
  cross-platform-safe filenames: rejects `\ / : * ? " < > |`, control chars,
  Windows reserved device names (`CON`, `PRN`, `COM1`…), `.`/`..`, and
  leading/trailing dots/spaces. It raises `ValueError`, which `do_POST` maps to
  HTTP 400. Reuse it for any new name-accepting endpoint.
- **The markdown renderer is hand-written** (`mdToHtml`/`inline` in the JS). It
  escapes HTML first, then protects code/links with a private-use sentinel
  character (`U+E000`) before applying inline rules — do **not** change that
  sentinel to a digit- or space-based placeholder (an earlier bug: plain
  numbers in text got mangled). Media links are rendered by extension:
  `MEDIA_V` → `<video>`, `MEDIA_A` → `<audio>`; YouTube URLs → clickable
  thumbnail. Audio recordings are saved as `.weba` (not `.webm`) so they match
  `MEDIA_A` and render as audio.
- **Line endings** are pinned to LF via `.gitattributes`; media extensions are
  marked `binary`. Keep new binary asset types out of text conversion.
- **No authentication yet — this is intentionally single-user and local.** The
  server trusts the local filesystem and whoever can reach the port. A
  server/multi-user deployment with login is the planned next step; add auth
  before exposing it beyond localhost.
