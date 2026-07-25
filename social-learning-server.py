#!/usr/bin/env python3
"""
social-learning — a compact, single-file document manager.

Run locally:   python3 social-learning-server.py         (http://127.0.0.1:8000)
               python3 social-learning-server.py --host 0.0.0.0 --port 9000
               python3 social-learning-server.py --dir ./notes  (another folder)
               python3 social-learning-server.py --token SECRET (gate writes)
               python3 social-learning-server.py --noprompt --no-open  (CI)
               python3 social-learning-server.py --selftest    (concurrency test)

Content lives as plain files in this repo, so it is git-trackable and
contributors can just push new documents:

    content/                 hierarchy of documents (folders = tree)
      <folder>/<doc>.md      one markdown file per document
      _assets/               pasted images/files and audio/video recordings
      _versions/             previous text of an overwritten/deleted document
      _workflows/            workflow definitions and per-document state

The whole application (server + HTML + CSS + JS) is this one file, using only
the Python standard library.

Several people can use one server at once. What that guarantees, precisely:
no silent data loss (writes are atomic, and a save based on a version somebody
else has replaced is refused with 409 rather than clobbering theirs), and
awareness within seconds (a change feed refreshes idle editors and the tree,
and shows who else is in a document). It is NOT simultaneous typing in one
paragraph — see CLAUDE.md for why that is the wrong fit for this data model.

There is still no login. --token gates writes behind a shared secret, which
means "whoever has the link" and not identity: the name in the header box is
self-declared and forgeable. Real per-user auth is the next step.
"""

import argparse
import collections
import difflib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# CONTENT (and its _assets subfolder) are set from the command line in main().
# Default is ./content, resolved against the current working directory.
CONTENT = os.path.realpath("content")
ASSETS = os.path.join(CONTENT, "_assets")
WORKFLOWS = os.path.join(CONTENT, "_workflows")
STATE_FILE = os.path.join(WORKFLOWS, "_state.json")
VERSIONS = os.path.join(CONTENT, "_versions")

# _state.json is read-modify-written from several worker threads. This is an
# RLock, not a Lock: load_state()/save_state() take it themselves, and they are
# also called from inside the `with STATE_LOCK` blocks of the mutators below.
STATE_LOCK = threading.RLock()

# Serializes the read-hash-compare-write sequence in PUT /api/doc. Without it
# two requests can both read the same ETag, both pass If-Match, and both write.
# One global lock is deliberate: documents are small and are held for a few ms,
# which is far cheaper than a per-path lock registry and its cleanup problem.
DOC_WRITE_LOCK = threading.Lock()

# Optional shared secret for writes (--token). Not authentication: it only
# means "whoever has the link", so that reaching the port is not enough to
# delete someone's content. Real per-user auth is still a separate step.
WRITE_TOKEN = ""

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("audio/webm", ".weba")


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
def ensure_dirs():
    os.makedirs(ASSETS, exist_ok=True)
    os.makedirs(WORKFLOWS, exist_ok=True)


def safe_join(rel):
    """Resolve a repo-relative path, refusing to escape the content dir."""
    rel = unquote(rel or "").strip().lstrip("/")
    full = os.path.realpath(os.path.join(CONTENT, rel))
    if full != CONTENT and not full.startswith(CONTENT + os.sep):
        raise PermissionError("path escapes content directory")
    return full


def atomic_write(full, data):
    """Write bytes to `full` so readers never observe a partial file.

    Writes a temp file in the *same* directory (os.replace is only atomic
    within one filesystem), fsyncs it, then renames it over the target. The
    temp name starts with a dot so build_tree hides it if anything goes wrong.
    Raises on failure, so the handler returns 500 and the client can say "save
    failed" instead of silently losing the write.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = os.path.dirname(full)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sl-tmp-", suffix=".part", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # On Windows os.replace raises PermissionError while another process
        # (editor, indexer, antivirus) holds the destination open; retry briefly.
        for attempt in range(3):
            try:
                os.replace(tmp, full)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def name_taken(src, dst):
    """True if renaming src->dst would clobber a different existing entry.

    The samefile clause is required, not defensive: on case-insensitive
    filesystems (macOS, Windows) renaming Foo.md -> foo.md is a legitimate
    case-only rename where dst already "exists" as the very same inode.
    """
    if not os.path.exists(dst):
        return False
    try:
        return not os.path.samefile(src, dst)
    except OSError:
        return True


def doc_etag(data):
    """Version token for a document: a hash of its exact bytes.

    A content hash (rather than mtime or a stored counter) is what makes
    conflict detection survive `git pull`, `git checkout`, an external editor,
    a server restart, or a second server on the same folder — all normal here,
    and all invisible to a counter.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return 'W/"%d-%s"' % (
        len(data), hashlib.blake2b(data, digest_size=8).hexdigest())


def read_bytes(full):
    """Current bytes of a file, or b"" if it does not exist yet."""
    try:
        with open(full, "rb") as f:
            return f.read()
    except (FileNotFoundError, IsADirectoryError):
        return b""


def build_tree(path):
    """Return the document hierarchy as nested dicts, sorted dirs-first."""
    entries = []
    try:
        names = sorted(os.listdir(path))
    except FileNotFoundError:
        return entries
    for name in names:
        if name.startswith(".") or name in ("_assets", "_workflows", "_versions"):
            continue
        full = os.path.join(path, name)
        rel = os.path.relpath(full, CONTENT).replace(os.sep, "/")
        if os.path.isdir(full):
            entries.append({"name": name, "path": rel, "type": "dir",
                            "children": build_tree(full)})
        elif name.lower().endswith(".md"):
            entries.append({"name": name[:-3], "path": rel, "type": "file"})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def unique_asset_name(filename):
    base = os.path.basename(filename or "file")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(". ") or "file"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    salt = os.urandom(3).hex()
    # The stamp+salt prefix means the result is never a Windows reserved name.
    return f"{stamp}-{salt}-{base}"


# Windows reserved device names (case-insensitive, with or without extension).
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}


def clean_name(name):
    """Validate a user-supplied document/folder name for all platforms.

    Returns the cleaned name, or raises ValueError with a user-facing reason.
    Rejects path separators, Windows-illegal characters (: * ? " < > |),
    control characters, reserved device names, leading/trailing dots or
    spaces, and the '.'/'..' entries.
    """
    name = (name or "").strip().strip(". ").strip()
    if not name:
        raise ValueError("name required")
    if name in (".", ".."):
        raise ValueError("invalid name")
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError('name may not contain \\ / : * ? " < > | or control characters')
    if name.split(".")[0].upper() in WIN_RESERVED:
        raise ValueError(f'"{name}" is a reserved name on Windows')
    if len(name) > 200:
        raise ValueError("name too long")
    return name


# --------------------------------------------------------------------------- #
# Workflows and per-document state
#
# Workflow definitions are Graphviz-DOT text files in _workflows/ (referenced
# by name, without the .dot extension). Each document's assigned workflow and
# current state live in a single sidecar _workflows/_state.json, so .md files
# stay pure markdown. All reads/writes of that file take STATE_LOCK.
# --------------------------------------------------------------------------- #
def workflow_path(name):
    """Resolve a workflow name to its .dot file (validated, inside content)."""
    return safe_join("_workflows/" + clean_name(name) + ".dot")


def list_workflows():
    try:
        names = os.listdir(WORKFLOWS)
    except FileNotFoundError:
        return []
    return sorted(n[:-4] for n in names if n.lower().endswith(".dot"))


def load_state():
    """Read _state.json. Missing is empty; corrupt is an error, never empty.

    Treating a corrupt file as {} would be catastrophic: the next mutator would
    write {} plus its own single entry, discarding every document's workflow and
    history. This file is git-tracked, so a bad merge makes that a real path.
    """
    with STATE_LOCK:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError:
            aside = os.path.join(
                WORKFLOWS, "_state.corrupt-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
            try:
                os.replace(STATE_FILE, aside)
            except OSError:
                pass
            print("!! %s was not valid JSON; moved aside to %s"
                  % (STATE_FILE, aside))
            raise RuntimeError("document state file was corrupt; moved aside "
                               "to " + os.path.basename(aside))
        return data if isinstance(data, dict) else {}


def save_state(data):
    with STATE_LOCK:
        atomic_write(STATE_FILE, json.dumps(data, indent=2,
                                            ensure_ascii=False) + "\n")


def now_stamp():
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


# --------------------------------------------------------------------------- #
# Version snapshots
#
# Before a document's bytes are replaced (or the file deleted) the previous
# content is kept under _versions/<doc path>/<stamp>-<user>.md. These are inert
# files, hidden from the tree: git versions them if a contributor commits, and
# nothing here ever commits on its own. They are what makes the "overwrite"
# choice in a save conflict a decision rather than a data loss.
# --------------------------------------------------------------------------- #
SNAP_COALESCE = 120        # seconds — don't re-snapshot this soon, same author
SNAP_KEEP = 20             # newest snapshots kept per document


def keep_version(key, data, by):
    """Preserve `data` (the bytes about to be replaced) for document `key`."""
    if not data:
        return                                    # nothing to preserve
    d = os.path.join(VERSIONS, *key.split("/"))
    who = re.sub(r"[^A-Za-z0-9._-]", "_", by or "unknown")[:40] or "unknown"
    try:
        existing = sorted(os.listdir(d))
    except FileNotFoundError:
        existing = []
    # A 1.2 s autosave loop would otherwise produce thousands of files an hour,
    # so coalesce a burst by one author into its first snapshot.
    if existing and existing[-1].endswith("-%s.md" % who):
        try:
            if time.time() - os.path.getmtime(
                    os.path.join(d, existing[-1])) < SNAP_COALESCE:
                return
        except OSError:
            pass
    atomic_write(os.path.join(
        d, "%s-%s.md" % (time.strftime("%Y%m%d-%H%M%S"), who)), data)
    for stale in sorted(os.listdir(d))[:-SNAP_KEEP]:
        try:
            os.unlink(os.path.join(d, stale))
        except OSError:
            pass


def record_created(key, by):
    """Note who first created a document and when (once, at creation time)."""
    with STATE_LOCK:
        data = load_state()
        entry = data.setdefault(key, {})
        if "created" not in entry:
            entry["created"] = {"by": by, "at": now_stamp()}
            save_state(data)


def set_doc_state(key, workflow, state, event, terminal, by):
    """Assign/advance a document's workflow state, or (falsy workflow) clear it.

    Appends a {state, event, by, at} record to the document's history on every
    change; the per-document `created` record (if any) is always preserved.
    """
    with STATE_LOCK:
        data = load_state()
        entry = data.get(key, {})
        if workflow:
            entry["workflow"] = workflow
            entry["state"] = state
            entry["terminal"] = bool(terminal)
            entry.setdefault("history", []).append(
                {"state": state, "event": event, "by": by, "at": now_stamp()})
            data[key] = entry
        else:                                   # unassign: keep only `created`
            for k in ("workflow", "state", "terminal", "history"):
                entry.pop(k, None)
            if entry:
                data[key] = entry
            else:
                data.pop(key, None)
        save_state(data)
        return data.get(key)


def rekey_state(oldkey, newkey):
    """Move state entries when a document/folder is renamed."""
    with STATE_LOCK:
        data = load_state()
        changed = False
        for k in list(data.keys()):
            if k == oldkey:
                data[newkey] = data.pop(k)
                changed = True
            elif k.startswith(oldkey + "/"):
                data[newkey + k[len(oldkey):]] = data.pop(k)
                changed = True
        if changed:
            save_state(data)


def drop_state(key):
    """Remove state entries for a deleted document/folder."""
    with STATE_LOCK:
        data = load_state()
        removed = [k for k in data if k == key or k.startswith(key + "/")]
        for k in removed:
            data.pop(k, None)
        if removed:
            save_state(data)


def rename_workflow_refs(old, new):
    """Re-point document state entries after a workflow itself is renamed."""
    with STATE_LOCK:
        data = load_state()
        changed = False
        for v in data.values():
            if isinstance(v, dict) and v.get("workflow") == old:
                v["workflow"] = new
                changed = True
        if changed:
            save_state(data)


# --------------------------------------------------------------------------- #
# Change feed and presence
#
# Every mutation appends to a small in-memory ring buffer; clients poll
# /api/changes?since=<seq> every few seconds and react. Polling rather than SSE
# is deliberate: this handler speaks HTTP/1.0 (connection-close), so a held-open
# EventSource would permanently occupy one of the browser's ~6 connections per
# origin while _serve_file streams media on the others — a handful of tabs would
# deadlock the app, and the stdlib cannot speak HTTP/2 to fix it. Presence rides
# along in the same request, which an EventSource could not carry at all (it can
# set neither headers nor a body).
#
# Both structures are memory-only. Presence especially must never be persisted:
# it is ephemeral, it would be a write storm on a git-tracked file, and a crash
# would leave permanent ghost editors.
# --------------------------------------------------------------------------- #
CHANGE_LOCK = threading.Lock()
CHANGE_SEQ = 0
CHANGE_LOG = collections.deque(maxlen=200)
PRESENCE = {}                 # path -> {client_id: {"user", "last", "editing"}}
PRESENCE_TTL = 30             # seconds without a heartbeat before a peer expires


def publish(kind, path, by, origin=""):
    """Record a change. `origin` is the client id that caused it.

    Clients ignore events carrying their own origin; without that, a client's
    own 1.2 s autosave would echo back and reload the textarea under its cursor.
    """
    global CHANGE_SEQ
    with CHANGE_LOCK:
        CHANGE_SEQ += 1
        CHANGE_LOG.append({"seq": CHANGE_SEQ, "kind": kind, "path": path,
                           "by": by, "origin": origin, "at": now_stamp()})


def changes_since(since):
    """Events after `since`, or a resync request if we cannot serve that far."""
    with CHANGE_LOCK:
        oldest = CHANGE_LOG[0]["seq"] if CHANGE_LOG else CHANGE_SEQ + 1
        # since > CHANGE_SEQ means the server restarted under this client.
        if since > CHANGE_SEQ or (since and since + 1 < oldest):
            return {"seq": CHANGE_SEQ, "resync": True, "events": []}
        return {"seq": CHANGE_SEQ,
                "events": [e for e in CHANGE_LOG if e["seq"] > since]}


def last_writer(path):
    """Who last saved this document, from the change log.

    Read from the log rather than stored per document on purpose: recording it
    in _state.json would rewrite a git-tracked file on every 1.2 s autosave. The
    cost is that a server restart forgets the name, so the conflict notice just
    says "someone" — acceptable for a message, unacceptable as a write storm.
    """
    with CHANGE_LOCK:
        for e in reversed(CHANGE_LOG):
            if e["kind"] == "doc" and e["path"] == path:
                return e
    return {}


def touch_presence(path, client, user, editing):
    """Heartbeat one client's location, expire stale peers, return the roster."""
    now = time.time()
    with CHANGE_LOCK:
        if client and path:
            PRESENCE.setdefault(path, {})[client] = {
                "user": user, "last": now, "editing": bool(editing)}
        for p in list(PRESENCE):                  # prune on touch, no reaper
            for c in [c for c, v in PRESENCE[p].items()
                      if now - v["last"] > PRESENCE_TTL]:
                PRESENCE[p].pop(c, None)
            if not PRESENCE[p]:
                PRESENCE.pop(p, None)
        return {p: [{"user": v["user"] or "someone", "editing": v["editing"]}
                    for c, v in peers.items() if c != client]
                for p, peers in PRESENCE.items()}


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Server(ThreadingHTTPServer):
    # Every response is connection-close (HTTP/1.0), so each request needs a
    # fresh connection. With several people in the app — polling, autosaving,
    # streaming media — bursts easily exceed the stdlib default of 5 pending
    # connections, and the kernel answers the overflow with a TCP reset that
    # surfaces as a failed save. 128 costs nothing and removes the cliff.
    request_queue_size = 128
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "social-learning/1.0"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- response helpers ------------------------------------------------- #
    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        # no-store on every JSON reply: an ETag on a heuristically cacheable
        # response would let a fetch serve stale text paired with a stale ETag,
        # which surfaces as phantom conflicts or a silent overwrite.
        head = {"Cache-Control": "no-store"}
        head.update(extra or {})
        self._send(code, json.dumps(obj), "application/json; charset=utf-8", head)

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _who(self):
        """Resolve the acting user: the X-User header, else the client IP."""
        return self.headers.get("X-User", "").strip() or self.client_address[0]

    def _client(self):
        """The calling tab's random id, used to filter out its own echoes."""
        return self.headers.get("X-Client", "").strip()

    def _token_ok(self):
        """With --token set, writes must carry a matching X-Token header.

        This is not identity — anyone holding the token is anyone — but it stops
        a reachable port from being enough to overwrite or delete content.
        """
        if not WRITE_TOKEN:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Token", ""), WRITE_TOKEN)

    # -- static file serving (with Range support for media) --------------- #
    def _serve_file(self, full):
        if not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        with open(full, "rb") as f:
            if rng and rng.startswith("bytes="):
                start_s, _, end_s = rng[6:].partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                start = min(start, end)
                f.seek(start)
                chunk = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(chunk)
            else:
                self._send(200, f.read(), ctype, {"Accept-Ranges": "bytes"})

    # -- routing ---------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                return self._send(200, HTML, "text/html; charset=utf-8")
            if path == "/api/tree":
                return self._json(build_tree(CONTENT))
            if path == "/api/doc":
                full = safe_join(self._query().get("path", [""])[0])
                if not os.path.isfile(full):
                    return self._error(404, "not found")
                data = read_bytes(full)
                # The ETag is the token the client echoes back as If-Match when
                # it saves; no-store keeps a cached body/ETag pair out of play.
                return self._send(200, data, "text/plain; charset=utf-8",
                                  {"ETag": doc_etag(data),
                                   "Cache-Control": "no-store"})
            if path == "/api/changes":
                q = self._query()
                try:
                    since = int(q.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                out = changes_since(since)
                out["presence"] = touch_presence(
                    q.get("path", [""])[0], q.get("client", [""])[0],
                    q.get("user", [""])[0], q.get("editing", ["0"])[0] == "1")
                return self._json(out)
            if path == "/api/workflows":
                return self._json(list_workflows())
            if path == "/api/workflow":
                full = workflow_path(self._query().get("name", [""])[0])
                if not os.path.isfile(full):
                    return self._error(404, "not found")
                with open(full, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
            if path == "/api/state":
                return self._json(load_state())
            if path == "/api/whoami":
                return self._json({"ip": self.client_address[0],
                                   "token": bool(WRITE_TOKEN)})
            if path.startswith("/content/"):
                return self._serve_file(safe_join(path[len("/content/"):]))
            return self._error(404, "not found")
        except ValueError as e:
            return self._error(400, str(e))
        except PermissionError as e:
            return self._error(403, str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, str(e))

    do_HEAD = do_GET

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            if not self._token_ok():
                return self._error(403, "missing or wrong write token")
            if path == "/api/doc":
                rel = self._query().get("path", [""])[0]
                full = safe_join(rel)
                if not full.lower().endswith(".md"):
                    return self._error(400, "only .md documents")
                key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                entry = load_state().get(key)
                # Terminal-state check stays ahead of the conflict check, so a
                # read-only document answers 403 rather than 409.
                if entry and entry.get("terminal"):
                    return self._error(403, "document is read-only (terminal state)")
                body = self._body()
                who = self._who()
                with DOC_WRITE_LOCK:
                    cur = read_bytes(full)
                    cur_tag = doc_etag(cur)
                    want = self.headers.get("If-Match", "").strip()
                    # A PUT with no If-Match still writes unconditionally: that
                    # keeps curl and scripts working, and makes the "overwrite
                    # anyway" path trivial. It also means a stale cached
                    # front-end keeps the old last-writer-wins behaviour.
                    if want and want != cur_tag:
                        last = last_writer(key)
                        return self._json({"error": "conflict", "etag": cur_tag,
                                           "by": last.get("by", ""),
                                           "at": last.get("at", "")}, 409)
                    if cur != body:
                        keep_version(key, cur, who)
                    atomic_write(full, body)
                publish("doc", key, who, self._client())
                return self._json({"ok": True, "etag": doc_etag(body)},
                                  extra={"ETag": doc_etag(body)})
            if path == "/api/workflow":
                full = workflow_path(self._query().get("name", [""])[0])
                atomic_write(full, self._body())
                publish("workflow", os.path.basename(full)[:-4],
                        self._who(), self._client())
                return self._json({"ok": True,
                                   "name": os.path.basename(full)[:-4]})
            if path == "/api/state":
                full = safe_join(self._query().get("path", [""])[0])
                key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                data = json.loads(self._body() or b"{}")
                wf = (data.get("workflow") or "").strip()
                st = (data.get("state") or "").strip()
                ev = (data.get("event") or "").strip()
                term = bool(data.get("terminal"))
                entry = set_doc_state(key, wf, st, ev, term, self._who())
                publish("state", key, self._who(), self._client())
                return self._json({"ok": True, "state": entry})
            return self._error(404, "not found")
        except ValueError as e:
            return self._error(400, str(e))
        except PermissionError as e:
            return self._error(403, str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if not self._token_ok():
                return self._error(403, "missing or wrong write token")
            if path == "/api/create":
                data = json.loads(self._body() or b"{}")
                parent = data.get("parent", "")
                name = clean_name(data.get("name"))
                kind = data.get("type", "file")
                rel = f"{parent}/{name}" if parent else name
                if kind == "dir":
                    full = safe_join(rel)
                    os.makedirs(full, exist_ok=True)
                    key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                    publish("create", key, self._who(), self._client())
                    return self._json({"ok": True, "path": key})
                full = safe_join(rel + ".md")
                if not os.path.exists(full):
                    atomic_write(full, f"# {name}\n\n")
                key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                record_created(key, self._who())
                publish("create", key, self._who(), self._client())
                return self._json({"ok": True, "path": key})

            if path == "/api/rename":
                data = json.loads(self._body() or b"{}")
                src = safe_join(data.get("path", ""))
                if not os.path.exists(src):
                    return self._error(404, "not found")
                newname = clean_name(data.get("name"))
                if os.path.isfile(src) and src.lower().endswith(".md"):
                    newname += ".md"
                dst = safe_join(os.path.join(os.path.dirname(
                    os.path.relpath(src, CONTENT)), newname).replace(os.sep, "/"))
                if name_taken(src, dst):
                    return self._error(
                        409, "a file or folder with that name already exists")
                os.rename(src, dst)
                oldkey = os.path.relpath(src, CONTENT).replace(os.sep, "/")
                newkey = os.path.relpath(dst, CONTENT).replace(os.sep, "/")
                rekey_state(oldkey, newkey)
                publish("rename", oldkey, self._who(), self._client())
                publish("create", newkey, self._who(), self._client())
                return self._json({"ok": True, "path": newkey})

            if path == "/api/workflow-rename":
                data = json.loads(self._body() or b"{}")
                old = clean_name(data.get("name"))
                new = clean_name(data.get("newname"))
                src = workflow_path(old)
                if not os.path.isfile(src):
                    return self._error(404, "not found")
                dst = workflow_path(new)
                if name_taken(src, dst):
                    return self._error(409, "a workflow with that name already exists")
                os.rename(src, dst)
                rename_workflow_refs(old, new)
                publish("workflow", new, self._who(), self._client())
                return self._json({"ok": True, "name": new})

            if path == "/api/upload":
                # X-Filename is percent-encoded by the client (headers are latin-1)
                fn = unique_asset_name(
                    unquote(self.headers.get("X-Filename", "file")))
                # Atomic: a dropped connection must not leave a truncated asset
                # at a URL the client has already inserted into the markdown.
                atomic_write(os.path.join(ASSETS, fn), self._body())
                return self._json({"url": "/content/_assets/" + fn, "name": fn})

            if path == "/api/diff":
                # Unified diff of the on-disk document against the body (the
                # editor's unsaved text), for the "show differences" view.
                full = safe_join(self._query().get("path", [""])[0])
                mine = self._body().decode("utf-8", "replace").splitlines()
                theirs = read_bytes(full).decode("utf-8", "replace").splitlines()
                return self._send(200, "\n".join(difflib.unified_diff(
                    theirs, mine, "on the server", "in your editor", lineterm="")),
                    "text/plain; charset=utf-8", {"Cache-Control": "no-store"})

            return self._error(404, "not found")
        except ValueError as e:
            return self._error(400, str(e))
        except PermissionError as e:
            return self._error(403, str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, str(e))

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if not self._token_ok():
                return self._error(403, "missing or wrong write token")
            if path == "/api/doc":
                full = safe_join(self._query().get("path", [""])[0])
                key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                if os.path.isdir(full):
                    shutil.rmtree(full)
                elif os.path.isfile(full):
                    keep_version(key, read_bytes(full), self._who())
                    os.remove(full)
                else:
                    return self._error(404, "not found")
                drop_state(key)
                publish("delete", key, self._who(), self._client())
                return self._json({"ok": True})
            if path == "/api/workflow":
                full = workflow_path(self._query().get("name", [""])[0])
                if not os.path.isfile(full):
                    return self._error(404, "not found")
                os.remove(full)
                publish("workflow", os.path.basename(full)[:-4],
                        self._who(), self._client())
                return self._json({"ok": True})
            return self._error(404, "not found")
        except ValueError as e:
            return self._error(400, str(e))
        except PermissionError as e:
            return self._error(403, str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, str(e))


# --------------------------------------------------------------------------- #
# Embedded front-end (HTML + CSS + JS)
# --------------------------------------------------------------------------- #
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>social-learning</title>
<style>
  :root{
    --bg:#1e2127; --panel:#252a33; --panel2:#2d333f; --line:#3a414f;
    --fg:#e6e9ef; --muted:#9aa4b2; --accent:#5aa0f2; --accent2:#8b5cf6;
    --danger:#e06c75; --ok:#7ec97e;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;color:var(--fg);
       background:var(--bg);display:flex;flex-direction:column}
  header{display:flex;align-items:center;gap:12px;padding:8px 14px;
         background:var(--panel);border-bottom:1px solid var(--line)}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  header #about{color:var(--accent);text-decoration:none;font-size:12px;
    border:1px solid var(--line);border-radius:6px;padding:4px 8px}
  header #about:hover{border-color:var(--accent);background:var(--panel2)}
  header .sp{flex:1}
  #modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;
    align-items:center;justify-content:center;z-index:50;padding:20px}
  #modal[hidden]{display:none}
  #modal .box{background:var(--panel);border:1px solid var(--line);
    border-radius:12px;max-width:820px;width:100%;max-height:90vh;overflow:auto;
    padding:22px 26px;position:relative}
  #modal .x{position:absolute;top:12px;right:12px;padding:2px 9px}
  #modal h2{margin:.1em 0 .3em}
  #modal .lead{color:var(--muted);margin:.2em 0 1.1em}
  #modal .cols{display:flex;gap:28px;flex-wrap:wrap}
  #modal .cols>div{flex:1;min-width:260px}
  #modal h3{margin:.2em 0 .5em;font-size:14px}
  #modal ul{margin:.2em 0;padding-left:18px}
  #modal li{margin:.35em 0}
  #modal kbd{background:#171a20;border:1px solid var(--line);border-radius:4px;
    padding:0 5px;font-size:.85em}
  #modal code{background:#171a20;padding:1px 5px;border-radius:4px;font-size:.9em}
  #modal a{color:var(--accent)}
  #modal .pandoc{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}
  #modal .pandoc h3{margin-bottom:.2em}
  #modal .pandoc .lead{margin:.2em 0 .9em}
  #modal .pandoc b{display:block;margin:.6em 0 .2em}
  #modal pre{background:#171a20;border:1px solid var(--line);border-radius:6px;
    padding:8px 10px;overflow:auto;margin:.3em 0}
  #modal pre code{background:none;padding:0;font-size:.9em}
  #modal .note{color:var(--muted);font-size:.88em;margin:.15em 0 .3em}
  .status{color:var(--muted);font-size:12px;min-width:120px;text-align:right}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:6px;padding:5px 10px;cursor:pointer;font-size:13px}
  button:hover{border-color:var(--accent)}
  button.pri{background:var(--accent);border-color:var(--accent);color:#0b1220;font-weight:600}
  button.rec{background:var(--danger);border-color:var(--danger);color:#fff}
  main{flex:1;display:flex;min-height:0}
  #side{width:260px;background:var(--panel);border-right:1px solid var(--line);
        display:flex;flex-direction:column;min-height:0}
  #side .bar{display:flex;gap:6px;padding:8px;border-bottom:1px solid var(--line)}
  #side .bar button{flex:1;padding:5px 4px;font-size:12px}
  #side .bar #upDir,#side .bar #refresh{flex:0 0 28px}
  #cwdbar{padding:4px 10px;font-size:11px;color:var(--muted);
          border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;
          text-overflow:ellipsis}
  #cwdbar #cwd{color:var(--accent)}
  #tree{flex:1;overflow:auto;padding:6px}
  .node{user-select:none}
  .row{display:flex;align-items:center;gap:5px;padding:3px 6px;border-radius:5px;
       cursor:pointer;white-space:nowrap}
  .row:hover{background:var(--panel2)}
  .row.sel{background:var(--accent);color:#0b1220}
  /* the folder new documents/folders will be created in */
  .row.cwd{background:var(--panel2);outline:1px solid var(--accent)}
  .row .tw{width:12px;color:var(--muted);text-align:center}
  .row .ic{width:16px;text-align:center}
  .row .nm{flex:1;overflow:hidden;text-overflow:ellipsis}
  .row .act{opacity:0;display:flex;gap:2px}
  .row:hover .act{opacity:.8}
  .row .act span{padding:0 3px;font-size:12px}
  .row .act span:hover{color:var(--accent)}
  .kids{margin-left:14px;border-left:1px solid var(--line);padding-left:2px}
  #work{flex:1;display:flex;flex-direction:column;min-width:0}
  #toolbar{display:flex;align-items:center;gap:6px;padding:6px 10px;
           background:var(--panel);border-bottom:1px solid var(--line)}
  #docpath{color:var(--muted);font-size:12px;flex:1;overflow:hidden;
           text-overflow:ellipsis;white-space:nowrap}
  #panes{flex:1;display:flex;min-height:0}
  #editor,#preview{flex:1;overflow:auto;min-width:0}
  #editor{display:flex}
  #ta{flex:1;resize:none;border:0;outline:0;padding:16px;background:var(--bg);
      color:var(--fg);font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
  #ta.drop{outline:2px dashed var(--accent);outline-offset:-4px}
  #preview{padding:16px 26px;border-left:1px solid var(--line);background:#20242c}
  #panes.epreview #editor{display:none}
  #panes.eonly #preview{display:none}
  .empty{margin:auto;color:var(--muted);text-align:center;padding:40px}
  /* markdown preview */
  #preview h1,#preview h2,#preview h3{line-height:1.25;margin:.8em 0 .4em}
  #preview h1{font-size:1.7em;border-bottom:1px solid var(--line);padding-bottom:.2em}
  #preview h2{font-size:1.35em;border-bottom:1px solid var(--line);padding-bottom:.2em}
  #preview h3{font-size:1.15em}
  #preview a{color:var(--accent)}
  #preview code{background:#171a20;padding:1px 5px;border-radius:4px;
                font-family:ui-monospace,monospace;font-size:.9em}
  #preview pre{background:#171a20;padding:12px;border-radius:8px;overflow:auto}
  #preview pre code{background:0;padding:0}
  #preview blockquote{border-left:3px solid var(--accent);margin:.6em 0;
                      padding:.1em 12px;color:var(--muted)}
  #preview img{max-width:100%;border-radius:8px}
  #preview video,#preview audio{max-width:100%;border-radius:8px;margin:.4em 0}
  #preview table{border-collapse:collapse}
  #preview td,#preview th{border:1px solid var(--line);padding:4px 8px}
  #preview hr{border:0;border-top:1px solid var(--line);margin:1em 0}
  #preview .yt{display:inline-block;position:relative}
  #preview .yt img{width:320px}
  #preview .yt::after{content:"\25B6";position:absolute;inset:0;display:flex;
    align-items:center;justify-content:center;font-size:34px;color:#fff;
    text-shadow:0 0 8px #000;pointer-events:none}
  .rec-dot{display:inline-block;width:9px;height:9px;border-radius:50%;
    background:#fff;margin-right:6px;animation:blink 1s steps(2) infinite}
  @keyframes blink{50%{opacity:.2}}
  /* workflows */
  header #openWf{color:var(--accent);text-decoration:none;font-size:12px;
    border:1px solid var(--line);border-radius:6px;padding:4px 8px}
  header #openWf:hover{border-color:var(--accent);background:var(--panel2)}
  #work[hidden],#wfview[hidden]{display:none}
  #wfview{flex:1;display:flex;flex-direction:column;min-width:0}
  #wftoolbar{display:flex;align-items:center;gap:6px;padding:6px 10px;
    background:var(--panel);border-bottom:1px solid var(--line)}
  #wftoolbar select{min-width:140px}
  #wfpanes{flex:1;display:flex;min-height:0}
  #wfEditorWrap{flex:1;display:flex;min-width:0}
  #wfEditor{flex:1;resize:none;border:0;outline:0;padding:16px;background:var(--bg);
    color:var(--fg);font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
  #wfSummary{flex:1;overflow:auto;padding:16px 20px;
    border-left:1px solid var(--line);background:#20242c}
  #wfSummary h3{margin:.7em 0 .3em;font-size:13px}
  #wfSummary h3:first-child{margin-top:0}
  #wfSummary h3.err{color:var(--danger)}
  #wfSummary .ct{color:var(--muted);font-weight:400}
  #wfSummary ul{margin:.2em 0 .8em;padding-left:18px}
  #wfSummary li{margin:.25em 0}
  #wfSummary .ev{color:var(--muted)}
  #wfSummary .empty{margin:0;color:var(--muted);text-align:left;padding:0}
  #wfSummary .tag{font-size:11px;border-radius:4px;padding:0 5px;margin-left:5px}
  #wfSummary .tag.start{background:var(--accent);color:#0b1220}
  #wfSummary .tag.term{background:var(--ok);color:#0b1220}
  select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
    border-radius:6px;padding:5px 8px;font-size:13px}
  select:hover{border-color:var(--accent)}
  #toolbar select{padding:4px 6px;font-size:12px;max-width:160px}
  .wfcur{font-size:11px;padding:2px 9px;border-radius:10px;font-weight:600}
  .wfcur.moving{background:var(--accent);color:#0b1220}
  .wfcur.done{background:var(--ok);color:#0b1220}
  .row .wf{font-size:10px;padding:0 6px;border-radius:8px;margin-left:4px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:92px;flex:none}
  .row .wf.moving{background:var(--accent);color:#0b1220}
  .row .wf.done{background:var(--panel2);color:var(--ok);border:1px solid var(--line)}
  .row.sel .wf.moving{background:#0b1220;color:var(--accent)}
  /* presence chips: who else has this document open */
  .row .pr{font-size:10px;padding:0 5px;border-radius:8px;margin-left:4px;
    white-space:nowrap;flex:none;background:var(--panel2);color:var(--muted);
    border:1px solid var(--line)}
  .row .pr.typing{background:#3a3320;color:#e5c07b;border-color:#5c4a20}
  /* save-conflict bar: persistent and non-modal, because an alert() would
     re-fire with every autosave tick */
  #cbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;
    background:#3a2a2c;border-bottom:1px solid var(--danger);font-size:12px}
  #cbar[hidden]{display:none}
  #cbar .msg{flex:1;min-width:200px;color:#f3c9cc}
  #cbar button{font-size:12px;padding:4px 9px}
  #pbar{display:flex;align-items:center;gap:8px;padding:5px 10px;
    background:var(--panel2);border-bottom:1px solid var(--line);
    font-size:12px;color:var(--muted)}
  #pbar[hidden]{display:none}
  /* diff view inside the metadata modal */
  #meta pre.diff{background:var(--bg);border:1px solid var(--line);border-radius:8px;
    padding:10px;overflow:auto;max-height:60vh;font:12px/1.5 ui-monospace,monospace;
    white-space:pre}
  #meta pre.diff .a{color:var(--ok)}
  #meta pre.diff .d{color:var(--danger)}
  #meta pre.diff .h{color:var(--accent)}
  /* header identity boxes */
  header .hbox{display:flex;align-items:center;gap:4px;font-size:12px;
    color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:2px 6px}
  header .hbox input{background:transparent;border:0;outline:0;color:var(--fg);
    font:12px/1.4 system-ui,sans-serif;width:88px}
  header #clientIp{color:var(--fg);font-family:ui-monospace,monospace;font-size:11px}
  /* read-only (terminal) document */
  button:disabled,select:disabled{opacity:.45;cursor:not-allowed}
  button:disabled:hover{border-color:var(--line)}
  #ta.ro{background:#22262e;color:var(--muted);cursor:not-allowed}
  /* metadata modal */
  #meta{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;
    align-items:center;justify-content:center;z-index:50;padding:20px}
  #meta[hidden]{display:none}
  #meta .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    max-width:520px;width:100%;max-height:90vh;overflow:auto;padding:22px 26px;position:relative}
  #meta .x{position:absolute;top:12px;right:12px;padding:2px 9px}
  #meta h2{margin:.1em 0 .7em}
  #meta h3{margin:1em 0 .3em;font-size:13px;color:var(--muted)}
  #meta .mrow{display:flex;gap:10px;margin:.35em 0}
  #meta .mrow .ml{color:var(--muted);min-width:86px}
  #meta ol.mhist{margin:.3em 0;padding-left:20px}
  #meta ol.mhist li{margin:.5em 0}
  #meta .ct{color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>📚 social-learning</h1>
  <a href="#" id="about" title="Info, help &amp; Pandoc / Info, aiuto e Pandoc">ℹ Info</a>
  <a href="#" id="openWf" title="Define and edit workflows / Definisci e modifica i flussi">🔀 Workflows</a>
  <label class="hbox" title="Your name — used in document history / Il tuo nome — usato nella cronologia">👤
    <input id="userName" placeholder="username" spellcheck="false" autocomplete="off"></label>
  <span class="hbox ip" title="Your client IP address / Il tuo indirizzo IP">🌐 <span id="clientIp">…</span></span>
  <label class="hbox" id="tokBox" hidden
    title="Shared write token, required by this server / Token di scrittura condiviso, richiesto da questo server">🔑
    <input id="writeToken" placeholder="token" spellcheck="false"
      autocomplete="off" type="password"></label>
  <span class="sp"></span>
  <button id="mEdit" title="Editor only">✎</button>
  <button id="mSplit" class="pri" title="Split">⬍</button>
  <button id="mView" title="Preview only">👁</button>
  <span class="status" id="status"></span>
</header>

<div id="modal" hidden>
  <div class="box">
    <button class="x" id="closeAbout" title="Close">✕</button>
    <h2>📚 social-learning</h2>
    <p class="lead">A compact, single-file document manager that runs locally in
       your browser. &nbsp;·&nbsp; Un gestore di documenti compatto, in un unico
       file, che gira localmente nel tuo browser.</p>
    <div class="cols">
      <div>
        <h3>🇬🇧 Features</h3>
        <ul>
          <li><b>Document hierarchy</b> — organise notes in folders and
              sub-folders; create, rename and delete from the sidebar.</li>
          <li><b>Markdown editor</b> — write in markdown with a live preview;
              edits autosave (or press <kbd>Ctrl/Cmd</kbd>+<kbd>S</kbd>).</li>
          <li><b>Paste images &amp; files</b> — paste an image to embed it, or any
              file to store it and insert a download link.</li>
          <li><b>Smart links</b> — pasted URLs are clickable; YouTube links become
              clickable thumbnails.</li>
          <li><b>Audio &amp; video</b> — record clips from your camera/mic; they are
              saved and embedded as players.</li>
          <li><b>Several people at once</b> — the sidebar refreshes on its own and
              a 👥 badge shows who else has a document open. If two of you save
              the same document, nobody's text is lost: the second save is
              stopped and you choose to keep both, overwrite, reload or compare.
              Replaced text is kept under <code>_versions/</code>.</li>
          <li><b>Git-friendly</b> — everything is saved as plain files under
              <code>content/</code>, so you can commit and push your work.
              Changes made outside the app (a <code>git pull</code>, another
              editor) are detected too, rather than silently overwritten.</li>
        </ul>
      </div>
      <div>
        <h3>🇮🇹 Funzionalità</h3>
        <ul>
          <li><b>Gerarchia di documenti</b> — organizza le note in cartelle e
              sotto-cartelle; crea, rinomina ed elimina dalla barra laterale.</li>
          <li><b>Editor markdown</b> — scrivi in markdown con anteprima dal vivo;
              le modifiche si salvano da sole (o premi <kbd>Ctrl/Cmd</kbd>+<kbd>S</kbd>).</li>
          <li><b>Incolla immagini e file</b> — incolla un'immagine per inserirla, o
              un file qualsiasi per salvarlo con un link di download.</li>
          <li><b>Link intelligenti</b> — gli URL incollati sono cliccabili; i link
              di YouTube diventano miniature cliccabili.</li>
          <li><b>Audio e video</b> — registra clip da webcam/microfono; vengono
              salvate e incorporate come lettori multimediali.</li>
          <li><b>In più persone insieme</b> — la barra laterale si aggiorna da sola
              e un segno 👥 mostra chi altro ha aperto un documento. Se in due
              salvate lo stesso documento non si perde il testo di nessuno: il
              secondo salvataggio viene fermato e scegli tu se tenere entrambe le
              versioni, sovrascrivere, ricaricare o confrontare. Il testo
              sostituito resta in <code>_versions/</code>.</li>
          <li><b>Compatibile con Git</b> — tutto è salvato come file semplici in
              <code>content/</code>, così puoi fare commit e push del tuo lavoro.
              Anche le modifiche fatte fuori dall'app (un <code>git pull</code>,
              un altro editor) vengono rilevate invece di essere sovrascritte in
              silenzio.</li>
        </ul>
      </div>
    </div>
    <div class="pandoc">
      <h3>🔄 Convert with Pandoc &nbsp;·&nbsp; Converti con Pandoc</h3>
      <p class="lead">Documents here are plain markdown, so
        <a href="https://pandoc.org/" target="_blank" rel="noopener">Pandoc</a>
        can turn them into — and from — many other formats.
        &nbsp;·&nbsp; I documenti qui sono semplice markdown, quindi
        <a href="https://pandoc.org/" target="_blank" rel="noopener">Pandoc</a>
        può trasformarli da e verso molti altri formati.</p>
      <pre><code># install Pandoc:  sudo apt install pandoc   ·   brew install pandoc
#                  Windows: winget install JohnMacFarlane.Pandoc  (or pandoc.org/installing)</code></pre>

      <b>Markdown → Word / ODT (.docx / .odt)</b>
      <pre><code>pandoc note.md -o note.docx    # Word
pandoc note.md -o note.odt     # LibreOffice / OpenOffice</code></pre>
      <p class="note">🇬🇧 Images live in <code>_assets/</code>; if they don't appear,
        point Pandoc at them with <code>--resource-path=_assets</code> (or drop the
        leading <code>/</code> from the paths). Add
        <code>--reference-doc=style.docx</code> (or <code>.odt</code>) to reuse a
        template's styles.<br>
        🇮🇹 Le immagini sono in <code>_assets/</code>; se non compaiono, indicale con
        <code>--resource-path=_assets</code> (o togli lo <code>/</code> iniziale dai
        percorsi). Usa <code>--reference-doc=style.docx</code> (o <code>.odt</code>) per
        riusare gli stili di un modello.</p>

      <b>Word / ODT → Markdown (.docx / .odt)</b>
      <pre><code>pandoc note.docx -t gfm -o note.md --extract-media=_assets
pandoc note.odt  -t gfm -o note.md --extract-media=_assets</code></pre>
      <p class="note">🇬🇧 Pandoc reads <code>.docx</code> and <code>.odt</code>
        natively; <code>--extract-media</code> pulls embedded images into
        <code>_assets/</code>. Add <code>--wrap=none</code> to keep paragraphs on
        single lines.<br>
        🇮🇹 Pandoc legge <code>.docx</code> e <code>.odt</code> in modo nativo;
        <code>--extract-media</code> estrae le immagini incorporate in
        <code>_assets/</code>. Aggiungi <code>--wrap=none</code> per tenere i paragrafi
        su righe singole.</p>

      <b>PDF → Markdown &nbsp;·&nbsp; option A — pdftotext + Pandoc (lightweight)</b>
      <pre><code># install pdftotext (Poppler):
#   Debian/Ubuntu   sudo apt install poppler-utils
#   macOS (brew)    brew install poppler
#   Windows         choco install poppler   (or: scoop install poppler)
pdftotext book.pdf - | pandoc -t gfm -o book.md</code></pre>
      <p class="note">🇬🇧 Pandoc can't read PDF itself — <code>pdftotext</code> (from
        Poppler, <i>not</i> ImageMagick/Ghostscript) extracts the raw text, then Pandoc
        formats it. Fine for clean text; complex layouts, tables and images need manual
        cleanup. On Windows Poppler has no winget package and is fiddly — option B is
        usually easier there.<br>
        🇮🇹 Pandoc non legge il PDF da solo — <code>pdftotext</code> (da Poppler,
        <i>non</i> ImageMagick/Ghostscript) estrae il testo grezzo, poi Pandoc lo
        formatta. Va bene per testo semplice; layout complessi, tabelle e immagini
        richiedono pulizia manuale. Su Windows Poppler non ha un pacchetto winget ed è
        scomodo — di solito l'opzione B è più semplice.</p>

      <b>PDF → Markdown &nbsp;·&nbsp; option B — LiteParse (structured output + OCR)</b>
      <pre><code># needs Node.js — on Windows:  winget install OpenJS.NodeJS.LTS
npm install -g @llamaindex/liteparse
lit parse book.pdf --format markdown -o book.md</code></pre>
      <p class="note">🇬🇧 LiteParse rebuilds headings, tables and lists from the page
        layout and can OCR scans — all locally, no cloud or API keys. It replaces the
        pdftotext+Pandoc step (it emits Markdown directly). For very dense tables or
        scans, cloud tools (LlamaParse, Docling) still do better.<br>
        🇮🇹 LiteParse ricostruisce titoli, tabelle ed elenchi dal layout della pagina e
        può fare l'OCR delle scansioni — tutto in locale, senza cloud né chiavi API.
        Sostituisce il passaggio pdftotext+Pandoc (produce Markdown direttamente). Per
        tabelle molto dense o scansioni, gli strumenti cloud (LlamaParse, Docling)
        restano migliori.</p>

      <b>Ebooks (EPUB) → Markdown</b>
      <pre><code>pandoc book.epub -t gfm -o book.md --extract-media=_assets</code></pre>
      <p class="note">🇬🇧 <code>--extract-media</code> pulls embedded images into
        <code>_assets/</code>. Pair with
        <a href="https://calibre-ebook.com/" target="_blank" rel="noopener">Calibre</a>
        to manage and convert your ebook library.<br>
        🇮🇹 <code>--extract-media</code> estrae le immagini incorporate in
        <code>_assets/</code>. Abbinalo a
        <a href="https://calibre-ebook.com/" target="_blank" rel="noopener">Calibre</a>
        per gestire e convertire la tua libreria di ebook.</p>
    </div>
  </div>
</div>
<div id="meta" hidden>
  <div class="box">
    <button class="x" id="closeMeta" title="Close">✕</button>
    <h2>🕑 Document metadata</h2>
    <div id="metaBody"></div>
  </div>
</div>
<main>
  <nav id="side">
    <div class="bar">
      <button id="newDoc">+ Doc</button>
      <button id="newDir">+ Folder</button>
      <button id="upDir" title="Leave this folder">↑</button>
      <button id="refresh" title="Reload tree">⟳</button>
    </div>
    <div id="cwdbar">in <span id="cwd">content/</span></div>
    <div id="tree"></div>
  </nav>
  <section id="work">
    <div id="toolbar">
      <span id="docpath">No document selected</span>
      <select id="wfAssign" title="Assign a workflow to this document" disabled></select>
      <span id="wfCur" class="wfcur" hidden></span>
      <select id="wfState" title="Advance to the next state" hidden></select>
      <button id="metaBtn" title="Document metadata &amp; history" disabled>🕑</button>
      <button id="attachBtn" title="Attach a file of any type">📎</button>
      <input id="attachInput" type="file" multiple hidden>
      <button id="recAudio" title="Record audio">🎤</button>
      <button id="recVideo" title="Record video">🎥</button>
      <button id="save" class="pri">Save</button>
    </div>
    <div id="pbar" hidden><span id="pbarMsg"></span></div>
    <div id="cbar" hidden>
      <span class="msg" id="cbarMsg"></span>
      <button id="cCopy" class="pri"
        title="Keep both versions: save yours as a new document">Save as a copy</button>
      <button id="cOver" title="Replace the newer version with yours">Overwrite</button>
      <button id="cReload" title="Throw away your changes and load theirs">Reload</button>
      <button id="cDiff" title="Compare the two versions">Show differences</button>
    </div>
    <div id="panes" class="split">
      <div id="editor"><textarea id="ta" spellcheck="false"
        placeholder="Select or create a document…"></textarea></div>
      <div id="preview"></div>
    </div>
  </section>
  <section id="wfview" hidden>
    <div id="wftoolbar">
      <select id="wfPick" title="Choose a workflow"></select>
      <button id="wfNew">+ New</button>
      <button id="wfRename">Rename</button>
      <button id="wfDelete">Delete</button>
      <span class="sp" style="flex:1"></span>
      <span class="status" id="wfStatus"></span>
      <button id="wfClose">✕ Close</button>
    </div>
    <div id="wfpanes">
      <div id="wfEditorWrap"><textarea id="wfEditor" spellcheck="false"
        placeholder='Define a workflow in DOT, e.g.&#10;&#10;digraph {&#10;  Draft -> Submitted [label="submit"];&#10;  Submitted -> Approved [label="approve"];&#10;}'></textarea></div>
      <div id="wfSummary"></div>
    </div>
  </section>
</main>

<script>
"use strict";
const $ = s => document.querySelector(s);
/* Every write carries who did it (X-User), which tab did it (X-Client, so the
   tab can ignore its own echoes from the change feed) and, if the server was
   started with --token, the shared write token. */
function wHead(extra){
  const h = {"X-User":getUser(), "X-Client":CLIENT_ID};
  const t = ($("#writeToken").value || "").trim();
  if(t) h["X-Token"] = t;
  return Object.assign(h, extra || {});
}
const api = {
  tree:      () => fetch("/api/tree").then(r => r.json()),
  // returns {text, etag}: the etag is echoed back as If-Match when saving, so a
  // save can tell "nobody touched this" from "someone did"
  doc:       p => fetch("/api/doc?path=" + encodeURIComponent(p))
                .then(r => r.text().then(text => ({text, etag:r.headers.get("ETag")}))),
  save:      (p, t, etag) => fetch("/api/doc?path=" + encodeURIComponent(p),
                {method:"PUT", headers:wHead(etag ? {"If-Match":etag} : null),
                 body:t}).then(r => r.json().then(j => ({...j, status:r.status}))),
  create:    d => fetch("/api/create", {method:"POST",
                headers:wHead(), body:JSON.stringify(d)}).then(r => r.json()),
  rename:    d => fetch("/api/rename", {method:"POST",
                headers:wHead(), body:JSON.stringify(d)}).then(r => r.json()),
  del:       p => fetch("/api/doc?path=" + encodeURIComponent(p),
                {method:"DELETE", headers:wHead()}).then(r => r.json()),
  // the filename travels in a header, so percent-encode it: HTTP headers are
  // latin-1 and fetch throws on accented/non-ASCII names otherwise
  upload:    (blob, name) => fetch("/api/upload", {method:"POST",
                headers:wHead({"X-Filename":encodeURIComponent(name)}),
                body:blob}).then(r => r.json()),
  diff:      (p, mine) => fetch("/api/diff?path=" + encodeURIComponent(p),
                {method:"POST", headers:wHead(), body:mine}).then(r => r.text()),
  changes:   q => fetch("/api/changes?" + new URLSearchParams(q)).then(r => r.json()),
  workflows: () => fetch("/api/workflows").then(r => r.json()),
  workflow:  n => fetch("/api/workflow?name=" + encodeURIComponent(n)).then(r => r.text()),
  saveWf:    (n, t) => fetch("/api/workflow?name=" + encodeURIComponent(n),
                {method:"PUT", headers:wHead(), body:t}).then(r => r.json()),
  renameWf:  (n, nn) => fetch("/api/workflow-rename", {method:"POST",
                headers:wHead(),
                body:JSON.stringify({name:n, newname:nn})}).then(r => r.json()),
  delWf:     n => fetch("/api/workflow?name=" + encodeURIComponent(n),
                {method:"DELETE", headers:wHead()}).then(r => r.json()),
  states:    () => fetch("/api/state").then(r => r.json()),
  setState:  (p, o) => fetch("/api/state?path=" + encodeURIComponent(p),
                {method:"PUT", headers:wHead(),
                 body:JSON.stringify(o)}).then(r => r.json()),
  whoami:    () => fetch("/api/whoami").then(r => r.json()),
};

/* Identity: the username lives in the header box (persisted); the client IP
   comes from the server. If no username is set, the server falls back to IP.
   CLIENT_ID identifies this *tab* — two tabs of one user are two clients, which
   is what the change feed and presence need. */
const CLIENT_ID = Math.random().toString(36).slice(2) +
                  Math.random().toString(36).slice(2);
function getUser(){ return ($("#userName").value || "").trim(); }

// state.dir = the folder new documents/folders go into ("" = content root)
// state.etag/baseText = the version this editor's text is based on
let state = {path:null, dir:"", dirty:false, open:{},
             etag:null, baseText:"", conflict:false, openSeq:0};
const ta = $("#ta"), preview = $("#preview"), status = $("#status");

/* ------------------------------------------------------------ workflows --- */
let stateMap = {};        // docpath -> {workflow, state}
let wfNames = [];         // available workflow names
const wfCache = {};       // name -> parsed workflow (from parseDot)

/* Parse a small subset of Graphviz DOT into states + labelled transitions.
   Deliberately lenient: we own both ends, so we tolerate messy input rather
   than reject it. Returns {states, transitions:[{from,to,event}], errors}. */
function parseDot(src){
  const res = {states:[], transitions:[], errors:[]};
  const seen = new Set();
  const add = s => { if(s && !seen.has(s)){ seen.add(s); res.states.push(s); } };
  const unq = s => s.replace(/^"|"$/g, "");
  let t = (src || "")
    .replace(/\/\*[\s\S]*?\*\//g, " ")        // /* block */ comments
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1")     // // line comments (keep http://)
    .replace(/(^|\s)#[^\n]*/g, "$1");         // # line comments
  const bm = t.match(/\{([\s\S]*)\}/);        // digraph body, if wrapped
  const body = bm ? bm[1] : t;
  const ID = '("[^"]*"|[A-Za-z_][A-Za-z0-9_.]*)';
  const nodeRe = new RegExp('^' + ID + '\\s*(\\[|$)');
  for(const raw of body.split(/[;\n]+/)){
    const s = raw.trim();
    if(!s) continue;
    if(/^(strict|digraph|graph|subgraph|rankdir|node|edge|label|bgcolor|size|ratio|splines|nodesep|ranksep)\b/i.test(s)
       && !/->/.test(s)) continue;            // graph-level attrs / defaults
    if(/->/.test(s)){                          // edge, possibly chained
      const am = s.match(/\[([^\]]*)\]\s*$/);
      const attrs = am ? am[1] : "";
      const core = am ? s.slice(0, am.index) : s;
      const lm = attrs.match(/label\s*=\s*("([^"]*)"|[A-Za-z0-9_]+)/i);
      const event = lm ? (lm[2] !== undefined ? lm[2] : lm[1]) : "";
      const parts = core.split("->").map(p => unq(p.trim())).filter(Boolean);
      parts.forEach(add);
      for(let i = 0; i + 1 < parts.length; i++)
        res.transitions.push({from:parts[i], to:parts[i+1], event});
      continue;
    }
    const nm = s.match(nodeRe);                // node declaration
    if(nm){ add(unq(nm[1])); continue; }
    res.errors.push(s);
  }
  return res;
}
function wfStart(wf){
  const incoming = new Set(wf.transitions.map(t => t.to));
  return wf.states.find(s => !incoming.has(s)) || wf.states[0] || "";
}
function isTerminal(wf, s){ return !wf.transitions.some(t => t.from === s); }
function allowedFrom(wf, s){ return wf.transitions.filter(t => t.from === s); }
async function getWf(name){
  if(!name) return null;
  if(!wfCache[name]) wfCache[name] = parseDot(await api.workflow(name));
  return wfCache[name];
}

/* ---------------------------------------------------------------- tree --- */
async function loadTree(){
  const data = await api.tree();
  stateMap = await api.states();
  // preload the workflows referenced by state so markers render synchronously
  const needed = new Set(Object.values(stateMap)
    .map(v => v && v.workflow).filter(Boolean));
  await Promise.all([...needed].map(getWf));
  const el = $("#tree"); el.innerHTML = "";
  el.appendChild(renderNodes(data, ""));
  $("#cwd").textContent = state.dir || "content/";
  paintPresence();                    // the rebuild dropped the chips
  // awaited: it settles docReadOnly, and callers switching documents must not
  // run on the previous document's read-only flag
  await refreshDocWorkflowUI();
}
function renderNodes(nodes, parent){
  const frag = document.createDocumentFragment();
  for(const n of nodes){
    const node = document.createElement("div"); node.className = "node";
    const row = document.createElement("div"); row.className = "row";
    const isDir = n.type === "dir";
    const tw = document.createElement("span"); tw.className = "tw";
    const ic = document.createElement("span"); ic.className = "ic";
    const nm = document.createElement("span"); nm.className = "nm";
    ic.textContent = isDir ? "📁" : "📄"; nm.textContent = n.name;
    row.append(tw, ic, nm);
    if(!isDir){
      const info = stateMap[n.path];
      if(info && info.workflow){
        const wf = wfCache[info.workflow];
        const term = wf && isTerminal(wf, info.state);
        const chip = document.createElement("span");
        chip.className = "wf " + (term ? "done" : "moving");
        chip.textContent = (term ? "✓ " : "") + info.state;
        chip.title = info.workflow + " · " + info.state +
          (term ? " (done)" : " (in progress)");
        row.append(chip);
      }
    }
    const act = document.createElement("span"); act.className = "act";
    const ren = document.createElement("span"); ren.textContent = "✎"; ren.title="Rename";
    const del = document.createElement("span"); del.textContent = "🗑"; del.title="Delete";
    act.append(ren, del); row.append(act);
    node.append(row);
    if(state.path === n.path) row.classList.add("sel");
    if(isDir){ row.dataset.dir = n.path; if(state.dir === n.path) row.classList.add("cwd"); }

    if(isDir){
      tw.textContent = state.open[n.path] ? "▾" : "▸";
      const kids = document.createElement("div"); kids.className = "kids";
      kids.style.display = state.open[n.path] ? "" : "none";
      kids.append(renderNodes(n.children || [], n.path));
      node.append(kids);
      row.onclick = e => {
        if(e.target === ren || e.target === del) return;
        // clicking the twisty folds; clicking the name enters the folder
        // (clicking the name of the folder you are already in folds it too)
        if(e.target === tw || state.dir === n.path)
          state.open[n.path] = !state.open[n.path];
        else { state.dir = n.path; state.open[n.path] = true; }
        tw.textContent = state.open[n.path] ? "▾" : "▸";
        kids.style.display = state.open[n.path] ? "" : "none";
        markCwd();
      };
    } else {
      row.dataset.doc = n.path;                   // presence chips attach here
      row.onclick = e => { if(e.target!==ren && e.target!==del) openDoc(n.path); };
    }
    ren.onclick = async e => {
      e.stopPropagation();
      const cur = n.name;
      const nv = prompt("Rename to:", cur);
      if(nv && nv !== cur){
        const r = await api.rename({path:n.path, name:nv});
        if(r.error) return alert(r.error);
        if(state.path === n.path) state.path = r.path;
        if(state.dir === n.path) state.dir = r.path;
        else if(state.dir.startsWith(n.path + "/"))
          state.dir = r.path + state.dir.slice(n.path.length);
        await loadTree(); if(state.path===r.path) openDoc(r.path);
      }
    };
    del.onclick = async e => {
      e.stopPropagation();
      if(!confirm("Delete \"" + n.name + "\"" + (isDir?" and everything inside?":"?"))) return;
      await api.del(n.path);
      if(state.path === n.path){ state.path=null; ta.value=""; render(); setPath(); }
      // don't leave the current folder pointing at something deleted
      if(state.dir === n.path || state.dir.startsWith(n.path + "/"))
        state.dir = n.path.includes("/") ?
          n.path.split("/").slice(0,-1).join("/") : "";
      loadTree();
    };
    frag.append(node);
  }
  return frag;
}

// re-paint the "current folder" outline without rebuilding the tree
function markCwd(){
  for(const r of document.querySelectorAll("#tree .row"))
    r.classList.toggle("cwd", r.dataset.dir !== undefined &&
                              r.dataset.dir === state.dir);
  $("#cwd").textContent = state.dir || "content/";
}

/* ------------------------------------------------------------- document --- */
async function openDoc(path){
  if(state.dirty && !confirm("Discard unsaved changes?")) return;
  const seq = ++state.openSeq;
  state.path = path; state.dirty = false;
  // opening a document makes its folder the current one
  state.dir = path.includes("/") ? path.split("/").slice(0,-1).join("/") : "";
  clearConflict();
  const r = await api.doc(path);
  // another openDoc started while this fetch was in flight: that one wins, and
  // overwriting ta.value here would silently discard what the user has typed
  if(seq !== state.openSeq) return;
  ta.value = r.text; state.etag = r.etag; state.baseText = r.text;
  dropDraft(path);
  render(); setPath();
  await loadTree();                   // settles read-only for the new document
}
function setPath(){
  $("#docpath").textContent = state.path || "No document selected";
}
function markSaved(ok){
  status.textContent = ok===false ? "save failed" :
    (state.dirty ? "unsaved…" : "saved");
  status.style.color = ok===false ? "var(--danger)" : "var(--muted)";
}
/* One save in flight at a time. Without this guard two PUTs from the 1.2 s
   debounce can overlap and land out of order, letting the older body win. */
let saving = false, pending = false, saveTimer = null;
async function save(){
  if(!state.path || docReadOnly || state.conflict) return;
  if(saving){ pending = true; return; }          // coalesce, don't queue
  saving = true;
  const path = state.path, text = ta.value, seq = state.openSeq;
  let r;
  try{ r = await api.save(path, text, state.etag); }
  catch(e){ r = null; }
  saving = false;
  // a different document was opened meanwhile: that reply is about the old one
  if(seq !== state.openSeq){ pending = false; return; }
  if(r && r.status === 409){
    pending = false;
    return enterConflict(r);
  }
  if(r && r.ok){
    // adopting the returned etag is what keeps autosave working: otherwise the
    // next save would collide with this one's own write
    state.etag = r.etag; state.baseText = text;
    if(ta.value === text){ state.dirty = false; dropDraft(path); }
    markSaved(true);
  } else markSaved(false);
  if(pending){ pending = false; save(); }        // fire once for what arrived
}
ta.addEventListener("input", () => {
  state.dirty = true; render(); markSaved(); keepDraft();
  if(state.conflict) return;                     // paused until it is resolved
  clearTimeout(saveTimer); saveTimer=setTimeout(save, 1200);
});

/* --------------------------------------------------------- draft mirror --- */
/* Unsaved text is mirrored to localStorage, so "Reload" and a crash or closed
   tab during a conflict are both recoverable rather than final. */
function keepDraft(){
  if(!state.path) return;
  try{ localStorage.setItem("sl-draft:" + state.path, ta.value); }catch(e){}
}
function dropDraft(path){
  try{ localStorage.removeItem("sl-draft:" + path); }catch(e){}
}

/* ------------------------------------------------------ save conflicts --- */
/* Raised either by a 409 from our own save, or pre-emptively when the change
   feed reports someone else saved a document we have unsaved edits in. The bar
   is persistent and non-modal, and autosave stays paused until it is resolved:
   an alert() here would re-fire on every autosave tick. */
function enterConflict(info){
  state.conflict = true;
  clearTimeout(saveTimer);
  const who = (info && info.by) ? info.by : "someone else";
  const when = (info && info.at) ? " at " + info.at : "";
  if(info && info.etag) state.etag = info.etag;
  $("#cbarMsg").textContent = "⚠ " + who + " saved a newer version of this " +
    "document" + when + ". Your changes are not saved.";
  $("#cbar").hidden = false;
  status.textContent = "conflict"; status.style.color = "var(--danger)";
}
function clearConflict(){
  state.conflict = false;
  $("#cbar").hidden = true;
  // a delete/rename notice hides the choices that no longer apply — restore them
  $("#cOver").hidden = false; $("#cReload").hidden = false; $("#cDiff").hidden = false;
}
/* Keep both versions. The default: nobody's work is lost, the divergence is
   visible in the tree, and a human merges the two. */
$("#cCopy").onclick = async () => {
  const mine = ta.value, dir = state.path.includes("/") ?
    state.path.split("/").slice(0,-1).join("/") : "";
  const base = state.path.split("/").pop().replace(/\.md$/i, "");
  const stamp = new Date().toTimeString().slice(0,5).replace(":", "");
  const name = base + " (mine " + (getUser() || "copy") + " " + stamp + ")";
  const r = await api.create({parent:dir, name, type:"file"});
  if(r.error) return alert(r.error);
  const w = await api.save(r.path, mine, null);   // fresh document, no If-Match
  if(w && w.error) return alert(w.error);
  dropDraft(state.path);
  clearConflict();
  await loadTree();
  state.dirty = false; state.openSeq++;           // openDoc must not warn
  openDoc(r.path);
};
/* Replace their version with ours. Safe only because the server keeps the bytes
   it is about to replace under _versions/. */
$("#cOver").onclick = async () => {
  const mine = ta.value;
  const cur = await api.doc(state.path);          // fresh etag to write against
  const w = await api.save(state.path, mine, cur.etag);
  if(w && w.status === 409) return enterConflict(w);
  if(!w || !w.ok) return markSaved(false);
  state.etag = w.etag; state.baseText = mine; state.dirty = false;
  dropDraft(state.path); clearConflict(); markSaved(true);
};
/* Throw ours away and take theirs. The draft mirror keeps ours recoverable. */
$("#cReload").onclick = async () => {
  const r = await api.doc(state.path);
  ta.value = r.text; state.etag = r.etag; state.baseText = r.text;
  state.dirty = false; dropDraft(state.path);
  clearConflict(); render(); markSaved(true);
};
$("#cDiff").onclick = async () => {
  const d = await api.diff(state.path, ta.value);
  $("#metaBody").innerHTML = "<p class='ct'>Server version vs. the text in your " +
    "editor.</p><pre class='diff'>" + (d.trim() ?
      d.split("\n").map(l => {
        const c = l.startsWith("+") ? "a" : l.startsWith("-") ? "d" :
                  l.startsWith("@") ? "h" : "";
        return c ? "<span class='" + c + "'>" + escHtml(l) + "</span>" : escHtml(l);
      }).join("\n") : "(identical)") + "</pre>";
  $("#meta").hidden = false;
};

/* ------------------------------------------------------- change feed ------ */
/* A 4 s poll rather than SSE: see the server-side note — a held-open
   EventSource would eat one of the browser's ~6 connections per origin (and
   could not carry the presence heartbeat, which has to travel upstream). */
let feedSeq = 0, feedTimer = null, feedWait = 4000, treeTimer = null;
async function pollChanges(){
  clearTimeout(feedTimer);
  if(document.hidden || saving){                  // nothing to learn right now
    feedTimer = setTimeout(pollChanges, feedWait);
    return;
  }
  let r;
  try{
    r = await api.changes({since:feedSeq, client:CLIENT_ID, user:getUser(),
                           path:state.path || "", editing:state.dirty ? 1 : 0});
  }catch(e){
    feedWait = 15000;                             // server down: back off
    feedTimer = setTimeout(pollChanges, feedWait);
    return;
  }
  feedWait = 4000;
  if(r.resync){                                   // restarted or fell behind
    feedSeq = r.seq || 0;
    loadTree();
  } else if(r.events && r.events.length){
    feedSeq = r.seq;
    for(const e of r.events) if(e.origin !== CLIENT_ID) applyChange(e);
  } else if(r.seq !== undefined) feedSeq = r.seq;
  presence = r.presence || {};
  paintPresence();
  feedTimer = setTimeout(pollChanges, feedWait);
}
function applyChange(e){
  if(e.path !== state.path){                      // somewhere else in the tree
    clearTimeout(treeTimer);                      // debounced: another user's
    treeTimer = setTimeout(loadTree, 400);        // autosave burst is many events
    return;
  }
  if(e.kind === "delete" || e.kind === "rename"){
    const verb = e.kind === "delete" ? "deleted" : "renamed";
    state.conflict = true; clearTimeout(saveTimer);
    $("#cbarMsg").textContent = "⚠ " + (e.by || "someone") + " " + verb +
      " this document. Your changes are not saved.";
    $("#cbar").hidden = false;
    $("#cOver").hidden = true; $("#cReload").hidden = true; $("#cDiff").hidden = true;
    loadTree();
    return;
  }
  if(e.kind !== "doc") { loadTree(); return; }    // workflow/state change
  if(state.dirty || state.conflict){
    // Never touch text the user is editing. Surfacing it now, seconds after
    // their save, is the whole point: the edit is still small enough to merge.
    if(!state.conflict) enterConflict(e);
    return;
  }
  refreshOpenDoc(e.by);
}
async function refreshOpenDoc(by){
  const path = state.path, seq = state.openSeq;
  const at = ta.selectionStart, atEnd = ta.selectionEnd, top = ta.scrollTop;
  const r = await api.doc(path);
  if(seq !== state.openSeq || state.dirty) return;
  ta.value = r.text; state.etag = r.etag; state.baseText = r.text;
  ta.selectionStart = Math.min(at, r.text.length);
  ta.selectionEnd = Math.min(atEnd, r.text.length);
  ta.scrollTop = top;
  render();
  status.textContent = "updated by " + (by || "someone");
  status.style.color = "var(--accent)";
  setTimeout(markSaved, 2500);
}

/* --------------------------------------------------------- presence ------- */
let presence = {};            // path -> [{user, editing}] (never includes us)
function paintPresence(){
  for(const row of document.querySelectorAll("#tree .row")){
    const old = row.querySelector(".pr");
    if(old) old.remove();
    const peers = presence[row.dataset.doc || ""] || [];
    if(!peers.length || !row.dataset.doc) continue;
    const chip = document.createElement("span");
    chip.className = "pr" + (peers.some(p => p.editing) ? " typing" : "");
    chip.textContent = "👥" + (peers.length > 1 ? peers.length : "");
    chip.title = peers.map(p => p.user + (p.editing ? " (typing)" : "")).join(", ");
    row.insertBefore(chip, row.querySelector(".act"));
  }
  const here = (state.path && presence[state.path]) || [];
  $("#pbar").hidden = !here.length;
  if(here.length)
    $("#pbarMsg").textContent = "👥 " + here.map(p => p.user).join(", ") +
      (here.length > 1 ? " also have " : " is also editing ") +
      (here.length > 1 ? "this document open" : "this document");
}

/* ------------------------------------------- document workflow / state --- */
let docReadOnly = false;
// states that can transition INTO a terminal state — where a "reopen" leads
function predecessors(wf, s){
  const p = [...new Set(wf.transitions.filter(t => t.to === s).map(t => t.from))];
  if(!p.length){ const start = wfStart(wf); if(start && start !== s) return [start]; }
  return p;
}
function setReadOnly(ro){
  docReadOnly = ro;
  ta.readOnly = ro; ta.classList.toggle("ro", ro);
  $("#save").disabled = ro; $("#recAudio").disabled = ro; $("#recVideo").disabled = ro;
  $("#attachBtn").disabled = ro;
  // note: a background loadTree() lands here too, so an unresolved conflict must
  // survive — only a genuine read-only document drops the pending edit
  if(ro && !state.conflict){
    clearTimeout(saveTimer);
    if(state.dirty){ state.dirty = false; markSaved(); }
  }
}
async function refreshDocWorkflowUI(){
  const asg = $("#wfAssign"), stSel = $("#wfState"), cur = $("#wfCur");
  asg.innerHTML = "";
  asg.append(new Option("— no workflow —", ""));
  for(const n of wfNames) asg.append(new Option(n, n));
  $("#metaBtn").disabled = !state.path;
  if(!state.path){
    asg.disabled = true; asg.value = ""; cur.hidden = true; stSel.hidden = true;
    setReadOnly(false); return;
  }
  asg.disabled = false;
  const info = stateMap[state.path];
  asg.value = info && info.workflow ? info.workflow : "";
  if(info && info.workflow){
    const wf = await getWf(info.workflow);
    const term = isTerminal(wf, info.state);
    cur.hidden = false; cur.textContent = info.state;
    cur.className = "wfcur " + (term ? "done" : "moving");
    stSel.innerHTML = "";
    if(term){                                    // terminal → offer to reopen
      const preds = predecessors(wf, info.state);
      stSel.append(new Option(preds.length ? "Reopen…" : "✓ terminal", ""));
      for(const p of preds) stSel.append(new Option("↩ " + p, p));
      stSel.disabled = !preds.length; stSel.dataset.mode = "reopen";
    } else {                                     // otherwise → allowed advances
      const opts = allowedFrom(wf, info.state);
      stSel.append(new Option("Advance…", ""));
      for(const t of opts)
        stSel.append(new Option((t.event ? t.event + " → " : "→ ") + t.to, t.to));
      stSel.disabled = !opts.length; stSel.dataset.mode = "advance";
    }
    stSel.hidden = false;
    setReadOnly(term);
  } else {
    cur.hidden = true; stSel.hidden = true; setReadOnly(false);
  }
}
$("#wfAssign").onchange = async () => {
  if(!state.path) return;
  const name = $("#wfAssign").value;
  let st = "", term = false;
  if(name){ const wf = await getWf(name); st = wfStart(wf); term = isTerminal(wf, st); }
  const r = await api.setState(state.path,
    {workflow:name, state:st, event:"", terminal:term});
  if(r.error) return alert(r.error);
  await loadTree();
};
$("#wfState").onchange = async () => {
  const to = $("#wfState").value; if(!to || !state.path) return;
  const info = stateMap[state.path]; if(!info || !info.workflow) return;
  const wf = await getWf(info.workflow);
  const reopen = $("#wfState").dataset.mode === "reopen";
  const tr = wf.transitions.find(t => t.from === info.state && t.to === to);
  const event = reopen ? "reopen" : (tr ? tr.event : "");
  const r = await api.setState(state.path,
    {workflow:info.workflow, state:to, event, terminal:isTerminal(wf, to)});
  if(r.error) return alert(r.error);
  await loadTree();
};

/* --------------------------------------------------- metadata / history --- */
function showMeta(){
  const b = $("#metaBody");
  const info = state.path ? stateMap[state.path] : null;
  if(!state.path){ b.innerHTML = '<p class="ct">No document selected.</p>'; }
  else {
    let h = '<div class="mrow"><span class="ml">Document</span><span>' +
      escHtml(state.path) + "</span></div>";
    h += '<div class="mrow"><span class="ml">Created</span><span>' +
      (info && info.created
        ? escHtml(info.created.at) + " · by " + escHtml(info.created.by)
        : '<span class="ct">unknown</span>') + "</span></div>";
    if(info && info.workflow){
      h += '<div class="mrow"><span class="ml">Workflow</span><span>' +
        escHtml(info.workflow) + "</span></div>";
      h += '<div class="mrow"><span class="ml">State</span><span>' +
        escHtml(info.state) + (info.terminal ? " (terminal · read-only)" : "") +
        "</span></div>";
      const hist = info.history || [];
      h += "<h3>State history</h3>";
      if(!hist.length) h += '<p class="ct">no transitions recorded</p>';
      else {
        h += '<ol class="mhist">';
        for(const e of hist){
          const lbl = e.event ? escHtml(e.event) + " → " + escHtml(e.state)
                              : escHtml(e.state);
          h += "<li><b>" + lbl + "</b><br><span class=\"ct\">" +
            escHtml(e.at) + " · by " + escHtml(e.by) + "</span></li>";
        }
        h += "</ol>";
      }
    } else {
      h += '<p class="ct">No workflow assigned.</p>';
    }
    b.innerHTML = h;
  }
  $("#meta").hidden = false;
}
$("#metaBtn").onclick = showMeta;
$("#closeMeta").onclick = () => { $("#meta").hidden = true; };
$("#meta").onclick = e => { if(e.target === $("#meta")) $("#meta").hidden = true; };

/* --------------------------------------------------------- new / toolbar --- */
// the folder new items are created in: whatever folder was last entered
function selectedDir(){ return state.dir || ""; }
$("#newDoc").onclick = async () => {
  const name = prompt("New document in " + (state.dir || "content/") + ":");
  if(!name) return;
  const r = await api.create({parent:selectedDir(), name, type:"file"});
  if(r.error) return alert(r.error);
  await loadTree(); openDoc(r.path);
};
$("#newDir").onclick = async () => {
  const name = prompt("New folder in " + (state.dir || "content/") + ":");
  if(!name) return;
  const r = await api.create({parent:selectedDir(), name, type:"dir"});
  if(r.error) return alert(r.error);
  // enter the folder we just created so the next item lands inside it
  state.dir = r.path; state.open[r.path] = true;
  loadTree();
};
// leave the current folder (go to its parent)
$("#upDir").onclick = () => {
  if(!state.dir) return;
  state.dir = state.dir.includes("/") ?
    state.dir.split("/").slice(0,-1).join("/") : "";
  loadTree();
};
$("#refresh").onclick = loadTree;
$("#save").onclick = save;
$("#about").onclick = e => { e.preventDefault(); $("#modal").hidden = false; };
$("#closeAbout").onclick = () => { $("#modal").hidden = true; };
$("#modal").onclick = e => { if(e.target === $("#modal")) $("#modal").hidden = true; };
document.addEventListener("keydown", e => {
  if(e.key === "Escape"){ $("#modal").hidden = true; $("#meta").hidden = true; }
});
$("#mEdit").onclick  = () => setMode("eonly", "#mEdit");
$("#mSplit").onclick = () => setMode("split", "#mSplit");
$("#mView").onclick  = () => setMode("epreview", "#mView");
function setMode(cls, btn){
  $("#panes").className = cls;
  for(const b of ["#mEdit","#mSplit","#mView"]) $(b).classList.remove("pri");
  $(btn).classList.add("pri");
}
document.addEventListener("keydown", e => {
  if((e.ctrlKey||e.metaKey) && e.key==="s"){ e.preventDefault(); save(); }
});
window.addEventListener("beforeunload", e => {
  if(state.dirty){ e.preventDefault(); e.returnValue=""; }
});

/* ------------------------------------------------------- insert at caret -- */
function insertAtCursor(text){
  if(docReadOnly) return;
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0,s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus(); state.dirty=true; render(); markSaved(); keepDraft();
  if(state.conflict) return;          // don't force a save past the conflict bar
  clearTimeout(saveTimer); saveTimer=setTimeout(save, 1200);
}

/* --------------------------------------------------------------- paste ---- */
ta.addEventListener("paste", async e => {
  const items = (e.clipboardData || window.clipboardData).items;
  const files = [];
  for(const it of items) if(it.kind === "file"){ const f=it.getAsFile(); if(f) files.push(f); }
  if(!files.length) return;                 // plain text/URL → default paste
  e.preventDefault();
  for(const f of files) await uploadAndInsert(f, f.type.startsWith("image/"));
});
async function uploadAndInsert(blob, asImage){
  if(docReadOnly){ alert("Document is read-only (terminal state)."); return; }
  if(!state.path){ alert("Open a document first."); return; }
  const name = blob.name || ("paste-" + Date.now() +
      (asImage ? ".png" : (blob.type.split("/")[1] ? "."+blob.type.split("/")[1] : "")));
  status.textContent = "uploading…";
  const r = await api.upload(blob, name);
  if(r.error){ status.textContent = "upload failed"; alert(r.error); return; }
  const label = blob.name || name;
  insertAtCursor(asImage ? `![${label}](${r.url})\n` : `[${label}](${r.url})\n`);
}

/* --------------------------------------------------- attach any file ------ */
$("#attachBtn").onclick = () => $("#attachInput").click();
$("#attachInput").onchange = async e => {
  for(const f of e.target.files) await uploadAndInsert(f, f.type.startsWith("image/"));
  e.target.value = "";                       // allow re-picking the same file
};
/* drag a file of any type onto the editor */
for(const ev of ["dragenter","dragover"]) ta.addEventListener(ev, e => {
  if(!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
  e.preventDefault(); e.dataTransfer.dropEffect = "copy";
  ta.classList.add("drop");
});
for(const ev of ["dragleave","dragend"]) ta.addEventListener(ev,
  () => ta.classList.remove("drop"));
ta.addEventListener("drop", async e => {
  const files = e.dataTransfer ? [...e.dataTransfer.files] : [];
  ta.classList.remove("drop");
  if(!files.length) return;                  // dropped text/URL → default drop
  e.preventDefault();
  for(const f of files) await uploadAndInsert(f, f.type.startsWith("image/"));
});

/* ---------------------------------------------------------- recording ----- */
let media = null;
async function record(kind){
  if(docReadOnly){ alert("Document is read-only (terminal state)."); return; }
  if(media){ media.stop(); return; }              // toggle off
  let stream;
  try{
    stream = await navigator.mediaDevices.getUserMedia(
      kind==="video" ? {video:true, audio:true} : {audio:true});
  }catch(err){ alert("Could not access "+kind+": "+err.message); return; }
  const btn = kind==="video" ? $("#recVideo") : $("#recAudio");
  btn.classList.add("rec"); btn.innerHTML = '<span class="rec-dot"></span>stop';
  const chunks = [];
  const mr = new MediaRecorder(stream);
  media = mr;
  mr.ondataavailable = ev => { if(ev.data.size) chunks.push(ev.data); };
  mr.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    btn.classList.remove("rec");
    btn.textContent = kind==="video" ? "🎥" : "🎤";
    media = null;
    const blob = new Blob(chunks, {type: kind==="video" ? "video/webm":"audio/webm"});
    const name = kind==="video" ? "video-"+Date.now()+".webm" : "audio-"+Date.now()+".weba";
    const r = await api.upload(blob, name);
    if(r.error) return alert(r.error);
    insertAtCursor(`[${name}](${r.url})\n`);   // renderer turns .webm into a player
  };
  mr.start();
}
$("#recAudio").onclick = () => record("audio");
$("#recVideo").onclick = () => record("video");

/* -------------------------------------------------- markdown → HTML ------- */
function render(){
  if(!state.path && !ta.value){
    preview.innerHTML = '<div class="empty">Select a document on the left, ' +
      'or create one.<br><br>Paste images/files, drop links, or record ' +
      'audio/video — it all saves into <code>content/</code>.</div>';
    return;
  }
  preview.innerHTML = mdToHtml(ta.value);
}
const escHtml = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const MEDIA_V = /\.(webm|mp4|mov|m4v|ogv)$/i;
const MEDIA_A = /\.(mp3|wav|ogg|weba|m4a|aac)$/i;

function ytId(url){
  let m = url.match(/(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([\w-]{11})/);
  return m ? m[1] : null;
}
function linkOrMedia(text, url){
  const yt = ytId(url);
  if(yt) return `<a class="yt" href="${url}" target="_blank" rel="noopener">` +
                `<img src="https://img.youtube.com/vi/${yt}/hqdefault.jpg" alt="${text}"></a>`;
  if(MEDIA_V.test(url)) return `<video controls src="${url}"></video>`;
  if(MEDIA_A.test(url)) return `<audio controls src="${url}"></audio>`;
  return `<a href="${url}" target="_blank" rel="noopener">${text||url}</a>`;
}
function inline(s){
  const holds = [];
  const hold = h => { holds.push(h); return "" + (holds.length-1) + ""; };
  // inline code
  s = s.replace(/`([^`]+)`/g, (_,c) => hold("<code>"+c+"</code>"));
  // images
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
      (_,a,u) => hold(`<img src="${u}" alt="${a}">`));
  // links (with media/youtube detection)
  s = s.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_,t,u) => hold(linkOrMedia(t,u)));
  // bare urls
  s = s.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g,
      (_,pre,u) => pre + hold(linkOrMedia(u,u)));
  // bold / italic
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  // restore holds
  s = s.replace(/(\d+)/g, (_,i) => holds[+i]);
  return s;
}
function mdToHtml(src){
  const lines = escHtml(src.replace(/\r\n?/g,"\n")).split("\n");
  let out = [], i = 0;
  const flushList = st => { if(st.open){ out.push("</"+st.tag+">"); st.open=false; } };
  const listSt = {open:false, tag:"ul"};
  while(i < lines.length){
    let ln = lines[i];
    // fenced code
    if(/^```/.test(ln)){
      flushList(listSt);
      const buf=[]; i++;
      while(i<lines.length && !/^```/.test(lines[i])){ buf.push(lines[i]); i++; }
      i++; out.push("<pre><code>"+buf.join("\n")+"</code></pre>"); continue;
    }
    // heading
    let h = ln.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flushList(listSt); out.push("<h"+h[1].length+">"+inline(h[2])+
           "</h"+h[1].length+">"); i++; continue; }
    // hr
    if(/^\s*(---|\*\*\*|___)\s*$/.test(ln)){ flushList(listSt); out.push("<hr>"); i++; continue; }
    // blockquote
    if(/^\s*&gt;\s?/.test(ln)){
      flushList(listSt);
      const buf=[];
      while(i<lines.length && /^\s*&gt;\s?/.test(lines[i])){
        buf.push(lines[i].replace(/^\s*&gt;\s?/,"")); i++; }
      out.push("<blockquote>"+inline(buf.join(" "))+"</blockquote>"); continue;
    }
    // lists
    let li = ln.match(/^\s*([-*+])\s+(.*)$/), ol = ln.match(/^\s*(\d+)\.\s+(.*)$/);
    if(li || ol){
      const tag = ol ? "ol" : "ul";
      if(listSt.open && listSt.tag!==tag) flushList(listSt);
      if(!listSt.open){ out.push("<"+tag+">"); listSt.open=true; listSt.tag=tag; }
      out.push("<li>"+inline((li||ol)[2])+"</li>"); i++; continue;
    }
    // blank
    if(/^\s*$/.test(ln)){ flushList(listSt); i++; continue; }
    // paragraph (gather consecutive non-blank lines)
    flushList(listSt);
    const buf=[ln]; i++;
    while(i<lines.length && !/^\s*$/.test(lines[i]) &&
          !/^(#{1,6}\s|```|\s*(---|\*\*\*|___)\s*$|\s*&gt;|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[i])){
      buf.push(lines[i]); i++;
    }
    out.push("<p>"+inline(buf.join("<br>"))+"</p>");
  }
  flushList(listSt);
  return out.join("\n");
}

/* ------------------------------------------- workflow definition editor --- */
let curWf = null;                 // name of the workflow being edited
let wfSaveTimer = null;
async function loadWorkflows(){ wfNames = await api.workflows(); }
function populateWfPick(){
  const p = $("#wfPick"); p.innerHTML = "";
  if(!wfNames.length){ p.append(new Option("— no workflows —", "")); }
  for(const n of wfNames) p.append(new Option(n, n));
  if(curWf) p.value = curWf;
}
async function selectWf(name){
  curWf = name || null;
  $("#wfEditor").value = name ? await api.workflow(name) : "";
  $("#wfEditor").disabled = !name;
  $("#wfPick").value = name || "";
  $("#wfStatus").textContent = "";
  renderWfSummary();
}
function renderWfSummary(){
  const el = $("#wfSummary");
  if(!curWf){
    el.innerHTML = '<div class="empty">No workflow selected. Click ' +
      '<b>+ New</b> to define one.</div>';
    return;
  }
  const wf = parseDot($("#wfEditor").value);
  wfCache[curWf] = wf;                        // keep the cache fresh while editing
  const start = wfStart(wf);
  let h = '<h3>States <span class="ct">(' + wf.states.length + ')</span></h3><ul>';
  if(!wf.states.length) h += '<li class="ct">none yet</li>';
  for(const s of wf.states){
    let tags = "";
    if(s === start) tags += '<span class="tag start">start</span>';
    if(isTerminal(wf, s)) tags += '<span class="tag term">terminal</span>';
    h += "<li>" + escHtml(s) + tags + "</li>";
  }
  h += '</ul><h3>Transitions <span class="ct">(' + wf.transitions.length +
       ')</span></h3><ul>';
  if(!wf.transitions.length) h += '<li class="ct">none yet</li>';
  for(const t of wf.transitions)
    h += "<li>" + escHtml(t.from) + " → " + escHtml(t.to) +
      (t.event ? ' <span class="ev">on "' + escHtml(t.event) + '"</span>' : "") + "</li>";
  h += "</ul>";
  if(wf.errors.length)
    h += '<h3 class="err">Unparsed (' + wf.errors.length + ")</h3><ul>" +
      wf.errors.map(e => "<li>" + escHtml(e) + "</li>").join("") + "</ul>";
  el.innerHTML = h;
}
async function saveWf(){
  if(!curWf) return;
  const r = await api.saveWf(curWf, $("#wfEditor").value);
  $("#wfStatus").textContent = r && r.ok ? "saved" : "save failed";
}
$("#wfEditor").addEventListener("input", () => {
  renderWfSummary(); $("#wfStatus").textContent = "unsaved…";
  clearTimeout(wfSaveTimer); wfSaveTimer = setTimeout(saveWf, 1000);
});
function openWorkflows(){
  $("#wfview").hidden = false; $("#work").hidden = true;
  populateWfPick();
  if(wfNames.length){ if(!curWf || !wfNames.includes(curWf)) curWf = wfNames[0];
    selectWf(curWf); } else selectWf(null);
}
function closeWorkflows(){ $("#wfview").hidden = true; $("#work").hidden = false; }
$("#openWf").onclick = e => { e.preventDefault(); openWorkflows(); };
$("#wfClose").onclick = closeWorkflows;
$("#wfPick").onchange = () => { if($("#wfPick").value) selectWf($("#wfPick").value); };
$("#wfNew").onclick = async () => {
  const name = prompt("New workflow name:"); if(!name) return;
  const tmpl = 'digraph {\n  Draft -> Submitted   [label="submit"];\n' +
    '  Submitted -> Approved [label="approve"];\n' +
    '  Submitted -> Draft    [label="revise"];\n}\n';
  const r = await api.saveWf(name, tmpl);
  if(r.error) return alert(r.error);
  await loadWorkflows(); curWf = r.name; populateWfPick(); selectWf(r.name);
};
$("#wfRename").onclick = async () => {
  if(!curWf) return;
  const nn = prompt("Rename workflow to:", curWf); if(!nn || nn === curWf) return;
  const r = await api.renameWf(curWf, nn);
  if(r.error) return alert(r.error);
  delete wfCache[curWf]; curWf = r.name;
  await loadWorkflows(); await loadTree(); populateWfPick(); selectWf(r.name);
};
$("#wfDelete").onclick = async () => {
  if(!curWf) return;
  if(!confirm('Delete workflow "' + curWf + '"?')) return;
  const r = await api.delWf(curWf);
  if(r.error) return alert(r.error);
  delete wfCache[curWf]; curWf = null;
  await loadWorkflows(); populateWfPick();
  selectWf(wfNames[0] || null);
};

/* ------------------------------------------------------------- identity --- */
$("#userName").value = localStorage.getItem("sl-user") || "";
$("#userName").addEventListener("input", () =>
  localStorage.setItem("sl-user", $("#userName").value));
$("#writeToken").value = localStorage.getItem("sl-token") || "";
$("#writeToken").addEventListener("input", () =>
  localStorage.setItem("sl-token", $("#writeToken").value));

/* --------------------------------------------------------------- boot ----- */
(async function boot(){
  api.whoami().then(w => {
    $("#clientIp").textContent = (w && w.ip) || "?";
    // only show the token box on a server that actually requires one
    if(w && w.token) $("#tokBox").hidden = false;
  }).catch(() => { $("#clientIp").textContent = "?"; });
  await loadWorkflows();
  await loadTree();
  render(); markSaved();
  pollChanges();
  // coming back to a backgrounded tab should catch up at once, not in 4 s
  document.addEventListener("visibilitychange", () => {
    if(!document.hidden) pollChanges();
  });
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Concurrency self-test (--selftest)
#
# None of the races this guards against is reachable by clicking: one browser
# never races itself. This drives the real HTTP surface from many threads.
# --------------------------------------------------------------------------- #
def selftest(host, port):
    import urllib.error
    import urllib.request

    base = f"http://{host}:{port}"
    failures = []
    ran = []

    def check(ok, label):
        ran.append(label)
        print(("  ok   " if ok else "  FAIL ") + label)
        if not ok:
            failures.append(label)

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
        except OSError as e:
            # a reset connection is a transport failure, not a server decision:
            # report it as itself so it is never mistaken for lost data
            return None, repr(e).encode(), {}

    ensure_dirs()
    httpd = Server((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"self-test against {base} (content: {CONTENT})")
    try:
        doc = "_selftest.md"
        q = "/api/doc?path=" + doc
        call("PUT", q, b"# start\n")

        # 1. Concurrent unconditional writers: all succeed, one wins, and the
        #    winner is a whole body — never a mix of two.
        bodies = [("# writer %02d\n" % i + "x" * i).encode() for i in range(24)]
        codes = []
        lock = threading.Lock()

        def put(b):
            st, _, _ = call("PUT", q, b)
            with lock:
                codes.append(st)

        ts = [threading.Thread(target=put, args=(b,)) for b in bodies]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check(all(c == 200 for c in codes), "24 concurrent writes all accepted")
        _, final, _ = call("GET", q)
        check(final in bodies, "final content is exactly one writer's body")

        # 2. If-Match: exactly one of N racing conditional writers may win.
        _, cur, head = call("GET", q)
        tag = head.get("ETag")
        won = []

        def cput(i):
            st, _, _ = call("PUT", q, b"# cond %d\n" % i, {"If-Match": tag})
            with lock:
                won.append(st)

        ts = [threading.Thread(target=cput, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check(won.count(200) == 1, "If-Match: exactly 1 of 8 racing writes won "
                                   "(got %d)" % won.count(200))
        check(won.count(409) == 7, "If-Match: the other 7 were told 409")

        # 3. A stale token is refused; a fresh one is accepted.
        st, _, _ = call("PUT", q, b"stale\n", {"If-Match": 'W/"1-deadbeefdeadbeef"'})
        check(st == 409, "stale If-Match refused with 409")
        _, _, head = call("GET", q)
        st, _, _ = call("PUT", q, b"# fresh\n", {"If-Match": head.get("ETag")})
        check(st == 200, "fresh If-Match accepted")

        # 4. An out-of-band edit (git pull / vim) invalidates the token — the
        #    case a stored revision counter could not see.
        _, _, head = call("GET", q)
        tag = head.get("ETag")
        atomic_write(safe_join(doc), b"# changed on disk\n")
        st, _, _ = call("PUT", q, b"# mine\n", {"If-Match": tag})
        check(st == 409, "edit made directly on disk is detected as a conflict")

        # 5. Displaced content is recoverable.
        snaps = os.path.join(VERSIONS, doc)
        check(os.path.isdir(snaps) and bool(os.listdir(snaps)),
              "replaced versions kept under _versions/")

        # 6. Concurrent state mutations: the file always parses and no accepted
        #    write is lost (this is what the corrupt-vs-missing rule protects).
        #    40 at once also exercises the accept backlog — with the stdlib
        #    default of 5 the kernel resets the overflow, which used to look
        #    exactly like lost data.
        accepted = []

        def setstate(i):
            st, _, _ = call("PUT", "/api/state?path=_sel%02d.md" % i,
                            json.dumps({"workflow": "wf",
                                        "state": "s%d" % i}).encode(),
                            {"X-User": "t%d" % i})
            with lock:
                accepted.append((i, st))

        ts = [threading.Thread(target=setstate, args=(i,)) for i in range(40)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        dropped = [i for i, st in accepted if st is None]
        check(not dropped, "40 simultaneous connections none reset (%d dropped)"
                           % len(dropped))
        data = load_state()
        ok200 = [i for i, st in accepted if st == 200]
        check(all("_sel%02d.md" % i in data for i in ok200),
              "every accepted state write is present in _state.json "
              "(%d accepted)" % len(ok200))

        # 7. Rename collision refused, case-only rename allowed.
        call("POST", "/api/create", json.dumps(
            {"parent": "", "name": "_selA", "type": "file"}).encode())
        call("POST", "/api/create", json.dumps(
            {"parent": "", "name": "_selB", "type": "file"}).encode())
        st, _, _ = call("POST", "/api/rename", json.dumps(
            {"path": "_selA.md", "name": "_selB"}).encode())
        check(st == 409, "rename onto an existing name refused with 409")
        st, _, _ = call("POST", "/api/rename", json.dumps(
            {"path": "_selA.md", "name": "_selA"}).encode())
        check(st == 200, "case-only / same-name rename still allowed")

        # 8. No temp files left behind anywhere under content/.
        strays = [os.path.join(r, n) for r, _, fs in os.walk(CONTENT)
                  for n in fs if n.startswith(".sl-tmp-")]
        check(not strays, "no .sl-tmp-* files left behind (%d)" % len(strays))

        # 9. The change feed saw the writes, and replays for a new client.
        r = json.loads(call("GET", "/api/changes?since=0")[1])
        check(r.get("seq", 0) > 0 and len(r.get("events", [])) > 0,
              "change feed recorded events")
        r = json.loads(call("GET", "/api/changes?since=999999")[1])
        check(r.get("resync") is True, "a client from the future is told to resync")

        # 10. With a token set, writes without it are refused.
        global WRITE_TOKEN
        WRITE_TOKEN = "s3cret"
        st, _, _ = call("PUT", q, b"nope\n")
        check(st == 403, "write without --token refused")
        st, _, _ = call("PUT", q, b"yes\n", {"X-Token": "s3cret"})
        check(st == 200, "write with the right token accepted")
        WRITE_TOKEN = ""
    finally:
        httpd.shutdown()
        # tidy up: remove only what this test created
        for k in list(load_state()):
            if k.startswith("_sel"):
                drop_state(k)
        for p in ("_selftest.md", "_selA.md", "_selB.md"):
            try:
                os.remove(safe_join(p))
            except OSError:
                pass
        shutil.rmtree(os.path.join(VERSIONS, "_selftest.md"), ignore_errors=True)

    print("\n%d checks, %d failures" % (len(ran), len(failures)))
    for f in failures:
        print("  FAILED: " + f)
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
def main():
    global CONTENT, ASSETS, WORKFLOWS, STATE_FILE, VERSIONS, WRITE_TOKEN
    ap = argparse.ArgumentParser(description="social-learning document manager")
    ap.add_argument("--host", default="127.0.0.1",
                    help="IP address to bind (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000,
                    help="TCP port to serve on (default 8000)")
    ap.add_argument("--dir", default="content", metavar="FOLDER",
                    help="folder to serve (default ./content)")
    ap.add_argument("--token", default="", metavar="SECRET",
                    help="require this secret for any write (not a login: "
                         "anyone holding it can write)")
    ap.add_argument("--noprompt", action="store_true",
                    help="start serving immediately, without waiting for Enter")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--selftest", action="store_true",
                    help="run the concurrency self-test and exit")
    args = ap.parse_args()

    CONTENT = os.path.realpath(args.dir)
    ASSETS = os.path.join(CONTENT, "_assets")
    WORKFLOWS = os.path.join(CONTENT, "_workflows")
    STATE_FILE = os.path.join(WORKFLOWS, "_state.json")
    VERSIONS = os.path.join(CONTENT, "_versions")
    WRITE_TOKEN = args.token
    url = f"http://{args.host}:{args.port}/"

    if args.selftest:
        return selftest(args.host, args.port)

    print("social-learning")
    print(f"  content : {CONTENT}")
    print(f"  address : {url}")
    if WRITE_TOKEN:
        print("  writes  : require --token (share the token with contributors)")
    else:
        print("  writes  : open to anyone who can reach this address")

    if not args.noprompt:
        try:
            input("Press Enter to start serving (Ctrl+C to cancel)... ")
        except EOFError:
            pass                      # no interactive stdin (piped / CI) — proceed
        except KeyboardInterrupt:
            print("\ncancelled")
            return

    ensure_dirs()
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"cannot bind {args.host}:{args.port}: {e}")
        return

    print(f"serving {url} (Ctrl+C to stop)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    raise SystemExit(main() or 0)
