# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`social-learning` is a compact, local-first document manager. The **entire
application — HTTP server, HTML, CSS, and JavaScript — lives in the single file
`social-learning-server.py`**, using only the Python 3.10+ standard library (no third-party
dependencies, no build step). User content is stored as plain files under
`content/`, so the git repo doubles as the datastore: contributors add or edit
documents and push them.

## Run / develop

```sh
python3 social-learning-server.py                 # serve http://127.0.0.1:8000 and open a browser
python3 social-learning-server.py --port 9000     # choose a port
python3 social-learning-server.py --host 0.0.0.0  # bind other interfaces (no login — see below)
python3 social-learning-server.py --token SECRET  # require a shared secret for writes
python3 social-learning-server.py --no-open       # don't launch a browser (use in scripts/CI)
```

There is no unit-test suite, linter config, or package manifest. Checks used
during development:

```sh
python3 -c "import ast; ast.parse(open('social-learning-server.py').read())"   # syntax check

# Concurrency self-test — the only way to exercise the multi-user paths, since a
# single browser never races itself. Drives real HTTP from many threads and
# asserts: exactly one winner among racing If-Match writes, no torn _state.json,
# no lost accepted write, no leftover .sl-tmp-* files, rename-collision refused,
# and 40 simultaneous connections none reset. Use a throwaway --dir.
python3 social-learning-server.py --selftest --port 8137 --dir /tmp/sltest

# The markdown renderer is pure JS and can be exercised under node by slicing
# the <script> block out of social-learning-server.py (see git history for the pattern).
```

Note: `basedpyright` may flag a few type warnings in `social-learning-server.py` (e.g. the
`log_message` signature, `_send` accepting `str`). These are intentional and
handled at runtime (`_send` converts `str`→`bytes` via `isinstance`); do not
"fix" them by narrowing the types unless you preserve the runtime behavior.

## Architecture

Two halves that communicate over a small JSON/REST API:

1. **Python server** (top of `social-learning-server.py`) — a `ThreadingHTTPServer`
   subclass (`Server`) with one `Handler`. Routes:
   - `GET /` → serves the embedded `HTML` string (the whole front-end).
   - `GET /api/tree` → the document hierarchy as nested JSON (`build_tree`).
   - `GET|PUT /api/doc?path=…` → read / write a markdown document. The GET
     returns an `ETag`; a PUT may send it back as `If-Match` and gets **409**
     if someone else has written since.
   - `POST /api/create`, `POST /api/rename`, `DELETE /api/doc` → tree mutations.
   - `POST /api/upload` (raw body, filename in `X-Filename` header) → save an
     attachment, returns its URL.
   - `GET|PUT /api/state?path=…` → per-document workflow state.
   - `GET /api/changes?since=<seq>` → the change feed **and** the presence
     heartbeat in one request (clients poll it every 4 s).
   - `POST /api/diff?path=…` → unified diff of the request body against the
     file on disk, for the conflict bar's "show differences".
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
  _versions/<doc path>/    previous text of an overwritten/deleted document,
                           as <YYYYmmdd-HHMMSS>-<user>.md
  _workflows/              <name>.dot definitions + _state.json (per-doc state)
```

The tree hierarchy is literally the directory structure under `content/`.
`_assets/`, `_workflows/`, `_versions/` and dotfiles are hidden from the tree
(`build_tree` — **add any new sidecar directory to that skip list**). Attachment
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

### Multi-user invariants (easy to break by accident)

- **Every write goes through `atomic_write`** (temp file in the same directory →
  `fsync` → `os.replace`, with a retry because Windows raises `PermissionError`
  when another process holds the target open). Never `open(..., "w")` on a file
  under `content/` again: a plain truncate lets a concurrent reader see a
  half-written document, and it was what made torn `_state.json` reads possible.
- **`STATE_LOCK` is an `RLock` on purpose.** `load_state`/`save_state` take it
  themselves *and* are called from inside the `with STATE_LOCK` blocks of the
  mutators. Making it a plain `Lock` deadlocks every workflow operation on the
  first click.
- **`load_state` treats corrupt as an error, not as empty.** Returning `{}` for
  unparseable JSON would make the next mutator write `{}` plus one entry and
  discard every document's history. It moves the bad file aside and raises. That
  file is git-tracked, so a bad merge makes this a real path — do not "simplify"
  it back into a bare `except`.
- **The document version token is a content hash** (`doc_etag`), not mtime and
  not a stored counter. A counter cannot see out-of-band edits, so after a
  `git pull` rewrote a document the server would claim "no conflict" and the
  next autosave would destroy the pulled text. Hashing is what makes `git pull`,
  `git checkout`, an external editor and a server restart all detectable.
- **`DOC_WRITE_LOCK` must wrap the read-hash-compare-write in `PUT /api/doc`.**
  Without it two requests both read the same ETag, both pass `If-Match`, and
  both write. One global lock is deliberate — do not "optimize" it into a
  per-path registry.
- **A PUT with no `If-Match` still writes unconditionally**, keeping `curl` and
  scripts working. That also means a stale cached front-end silently keeps the
  old last-writer-wins behaviour.
- **Presence lives in memory only.** Persisting it would be a write storm on a
  git-tracked file, and a crash would leave permanent ghost editors. Same
  reasoning for "who saved last", which is read from `CHANGE_LOG`
  (`last_writer`) rather than recorded per document.
- **`Server.request_queue_size = 128`.** Every response is HTTP/1.0
  connection-close, so each request needs a fresh connection; the stdlib default
  of 5 made the kernel reset burst overflow, which looked exactly like lost
  saves. Don't drop it back to the default.
- **Client-side:** `save()` is non-reentrant (`saving`/`pending`) because two
  overlapping PUTs from the 1.2 s debounce can land out of order; it must
  **adopt the ETag returned by its own PUT**, or every subsequent save 409s
  against its own write. `setReadOnly` must not clear an unresolved conflict (a
  background `loadTree` reaches it), and `insertAtCursor` re-arms the autosave
  timer independently so it needs the same conflict check.
- **Polling, not SSE.** An `EventSource` would hold one of the browser's ~6
  connections per origin open permanently while `_serve_file` streams media on
  the others — a handful of tabs deadlocks the app, and the stdlib cannot speak
  HTTP/2 to fix it. It also cannot carry the presence heartbeat upstream (no
  headers, no body). `publish()`/`CHANGE_LOG` is transport-agnostic if this is
  ever revisited.
- **Deliberately NOT real-time co-editing.** Character-level collaboration would
  need a hand-written OT/CRDT plus an op log (~1,500-2,500 lines, and the
  failure mode is silent divergence). Worse, an op log makes the `.md` a render
  target rather than the source of truth, so a `git pull` mid-session would
  silently revert the pulled text — a direct contradiction of git-as-datastore.
  The guarantees to promise instead: **no silent data loss**, and **you find out
  within seconds**.
- **No login yet.** `--token SECRET` gates writes behind a shared secret
  (`hmac.compare_digest`), which means "whoever has the link", *not* identity —
  the `X-User` name is self-declared and forgeable, so attribution in the UI is
  a hint and never a fact. That is also why locks here are advisory (a 👥 badge),
  never enforced: a lock anyone can forge or steal is a lie with a UI. Real
  per-user auth is still the next step; add it before exposing this to an
  untrusted network.
