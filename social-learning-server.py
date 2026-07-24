#!/usr/bin/env python3
"""
social-learning — a compact, single-file document manager.

Run locally:   python3 app.py                    (opens http://127.0.0.1:8000)
               python3 app.py --host 0.0.0.0 --port 9000
               python3 app.py --dir ./notes      (serve a different folder)
               python3 app.py --noprompt --no-open   (for scripts / CI)

Content lives as plain files in this repo, so it is git-trackable and
contributors can just push new documents:

    content/                 hierarchy of documents (folders = tree)
      <folder>/<doc>.md      one markdown file per document
      _assets/               pasted images/files and audio/video recordings

The whole application (server + HTML + CSS + JS) is this one file, using only
the Python standard library. No login yet (a server deployment with auth is a
future step); everything runs against the local filesystem.
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
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

# _state.json is read-modify-written from several worker threads.
STATE_LOCK = threading.Lock()

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


def build_tree(path):
    """Return the document hierarchy as nested dicts, sorted dirs-first."""
    entries = []
    try:
        names = sorted(os.listdir(path))
    except FileNotFoundError:
        return entries
    for name in names:
        if name.startswith(".") or name in ("_assets", "_workflows"):
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
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(data):
    os.makedirs(WORKFLOWS, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def set_doc_state(key, workflow, state):
    """Set (or, with a falsy workflow, clear) a document's workflow state."""
    with STATE_LOCK:
        data = load_state()
        if workflow:
            data[key] = {"workflow": workflow, "state": state}
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
# HTTP handler
# --------------------------------------------------------------------------- #
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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _query(self):
        return parse_qs(urlparse(self.path).query)

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
                with open(full, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
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
            if path == "/api/doc":
                rel = self._query().get("path", [""])[0]
                full = safe_join(rel)
                if not full.lower().endswith(".md"):
                    return self._error(400, "only .md documents")
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(self._body().decode("utf-8"))
                return self._json({"ok": True})
            if path == "/api/workflow":
                full = workflow_path(self._query().get("name", [""])[0])
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(self._body().decode("utf-8"))
                return self._json({"ok": True,
                                   "name": os.path.basename(full)[:-4]})
            if path == "/api/state":
                full = safe_join(self._query().get("path", [""])[0])
                key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                data = json.loads(self._body() or b"{}")
                wf = (data.get("workflow") or "").strip()
                st = (data.get("state") or "").strip()
                return self._json({"ok": True, "state": set_doc_state(key, wf, st)})
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
            if path == "/api/create":
                data = json.loads(self._body() or b"{}")
                parent = data.get("parent", "")
                name = clean_name(data.get("name"))
                kind = data.get("type", "file")
                rel = f"{parent}/{name}" if parent else name
                if kind == "dir":
                    full = safe_join(rel)
                    os.makedirs(full, exist_ok=True)
                    return self._json({"ok": True, "path":
                                       os.path.relpath(full, CONTENT).replace(os.sep, "/")})
                full = safe_join(rel + ".md")
                os.makedirs(os.path.dirname(full), exist_ok=True)
                if not os.path.exists(full):
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(f"# {name}\n\n")
                return self._json({"ok": True, "path":
                                   os.path.relpath(full, CONTENT).replace(os.sep, "/")})

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
                os.rename(src, dst)
                oldkey = os.path.relpath(src, CONTENT).replace(os.sep, "/")
                newkey = os.path.relpath(dst, CONTENT).replace(os.sep, "/")
                rekey_state(oldkey, newkey)
                return self._json({"ok": True, "path": newkey})

            if path == "/api/workflow-rename":
                data = json.loads(self._body() or b"{}")
                old = clean_name(data.get("name"))
                new = clean_name(data.get("newname"))
                src = workflow_path(old)
                if not os.path.isfile(src):
                    return self._error(404, "not found")
                dst = workflow_path(new)
                os.rename(src, dst)
                rename_workflow_refs(old, new)
                return self._json({"ok": True, "name": new})

            if path == "/api/upload":
                fn = unique_asset_name(self.headers.get("X-Filename", "file"))
                dest = os.path.join(ASSETS, fn)
                with open(dest, "wb") as f:
                    f.write(self._body())
                return self._json({"url": "/content/_assets/" + fn, "name": fn})

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
            if path == "/api/doc":
                full = safe_join(self._query().get("path", [""])[0])
                key = os.path.relpath(full, CONTENT).replace(os.sep, "/")
                if os.path.isdir(full):
                    shutil.rmtree(full)
                elif os.path.isfile(full):
                    os.remove(full)
                else:
                    return self._error(404, "not found")
                drop_state(key)
                return self._json({"ok": True})
            if path == "/api/workflow":
                full = workflow_path(self._query().get("name", [""])[0])
                if not os.path.isfile(full):
                    return self._error(404, "not found")
                os.remove(full)
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
  #tree{flex:1;overflow:auto;padding:6px}
  .node{user-select:none}
  .row{display:flex;align-items:center;gap:5px;padding:3px 6px;border-radius:5px;
       cursor:pointer;white-space:nowrap}
  .row:hover{background:var(--panel2)}
  .row.sel{background:var(--accent);color:#0b1220}
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
</style>
</head>
<body>
<header>
  <h1>📚 social-learning</h1>
  <a href="#" id="about" title="Info, help &amp; Pandoc / Info, aiuto e Pandoc">ℹ Info</a>
  <a href="#" id="openWf" title="Define and edit workflows / Definisci e modifica i flussi">🔀 Workflows</a>
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
          <li><b>Git-friendly</b> — everything is saved as plain files under
              <code>content/</code>, so you can commit and push your work.</li>
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
          <li><b>Compatibile con Git</b> — tutto è salvato come file semplici in
              <code>content/</code>, così puoi fare commit e push del tuo lavoro.</li>
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
<main>
  <nav id="side">
    <div class="bar">
      <button id="newDoc">+ Doc</button>
      <button id="newDir">+ Folder</button>
      <button id="refresh" title="Reload tree">⟳</button>
    </div>
    <div id="tree"></div>
  </nav>
  <section id="work">
    <div id="toolbar">
      <span id="docpath">No document selected</span>
      <select id="wfAssign" title="Assign a workflow to this document" disabled></select>
      <span id="wfCur" class="wfcur" hidden></span>
      <select id="wfState" title="Advance to the next state" hidden></select>
      <button id="recAudio" title="Record audio">🎤</button>
      <button id="recVideo" title="Record video">🎥</button>
      <button id="save" class="pri">Save</button>
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
const api = {
  tree:      () => fetch("/api/tree").then(r => r.json()),
  doc:       p => fetch("/api/doc?path=" + encodeURIComponent(p)).then(r => r.text()),
  save:      (p, t) => fetch("/api/doc?path=" + encodeURIComponent(p),
                {method:"PUT", body:t}).then(r => r.json()),
  create:    d => fetch("/api/create", {method:"POST",
                body:JSON.stringify(d)}).then(r => r.json()),
  rename:    d => fetch("/api/rename", {method:"POST",
                body:JSON.stringify(d)}).then(r => r.json()),
  del:       p => fetch("/api/doc?path=" + encodeURIComponent(p),
                {method:"DELETE"}).then(r => r.json()),
  upload:    (blob, name) => fetch("/api/upload", {method:"POST",
                headers:{"X-Filename":name}, body:blob}).then(r => r.json()),
  workflows: () => fetch("/api/workflows").then(r => r.json()),
  workflow:  n => fetch("/api/workflow?name=" + encodeURIComponent(n)).then(r => r.text()),
  saveWf:    (n, t) => fetch("/api/workflow?name=" + encodeURIComponent(n),
                {method:"PUT", body:t}).then(r => r.json()),
  renameWf:  (n, nn) => fetch("/api/workflow-rename", {method:"POST",
                body:JSON.stringify({name:n, newname:nn})}).then(r => r.json()),
  delWf:     n => fetch("/api/workflow?name=" + encodeURIComponent(n),
                {method:"DELETE"}).then(r => r.json()),
  states:    () => fetch("/api/state").then(r => r.json()),
  setState:  (p, o) => fetch("/api/state?path=" + encodeURIComponent(p),
                {method:"PUT", body:JSON.stringify(o)}).then(r => r.json()),
};

let state = {path:null, dirty:false, open:{}};
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
  refreshDocWorkflowUI();
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

    if(isDir){
      tw.textContent = state.open[n.path] ? "▾" : "▸";
      const kids = document.createElement("div"); kids.className = "kids";
      kids.style.display = state.open[n.path] ? "" : "none";
      kids.append(renderNodes(n.children || [], n.path));
      node.append(kids);
      row.onclick = e => {
        if(e.target === ren || e.target === del) return;
        state.open[n.path] = !state.open[n.path];
        tw.textContent = state.open[n.path] ? "▾" : "▸";
        kids.style.display = state.open[n.path] ? "" : "none";
      };
    } else {
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
        await loadTree(); if(state.path===r.path) openDoc(r.path);
      }
    };
    del.onclick = async e => {
      e.stopPropagation();
      if(!confirm("Delete \"" + n.name + "\"" + (isDir?" and everything inside?":"?"))) return;
      await api.del(n.path);
      if(state.path === n.path){ state.path=null; ta.value=""; render(); setPath(); }
      loadTree();
    };
    frag.append(node);
  }
  return frag;
}

/* ------------------------------------------------------------- document --- */
async function openDoc(path){
  if(state.dirty && !confirm("Discard unsaved changes?")) return;
  state.path = path; state.dirty = false;
  ta.value = await api.doc(path);
  render(); setPath(); loadTree();
}
function setPath(){
  $("#docpath").textContent = state.path || "No document selected";
}
function markSaved(ok){
  status.textContent = ok===false ? "save failed" :
    (state.dirty ? "unsaved…" : "saved");
  status.style.color = ok===false ? "var(--danger)" : "var(--muted)";
}
async function save(){
  if(!state.path) return;
  const r = await api.save(state.path, ta.value);
  if(r && r.ok){ state.dirty=false; markSaved(true); }
  else markSaved(false);
}
let saveTimer=null;
ta.addEventListener("input", () => {
  state.dirty = true; render(); markSaved();
  clearTimeout(saveTimer); saveTimer=setTimeout(save, 1200);
});

/* ------------------------------------------- document workflow / state --- */
async function refreshDocWorkflowUI(){
  const asg = $("#wfAssign"), stSel = $("#wfState"), cur = $("#wfCur");
  asg.innerHTML = "";
  asg.append(new Option("— no workflow —", ""));
  for(const n of wfNames) asg.append(new Option(n, n));
  if(!state.path){
    asg.disabled = true; asg.value = ""; cur.hidden = true; stSel.hidden = true;
    return;
  }
  asg.disabled = false;
  const info = stateMap[state.path];
  asg.value = info && info.workflow ? info.workflow : "";
  if(info && info.workflow){
    const wf = await getWf(info.workflow);
    const term = isTerminal(wf, info.state);
    cur.hidden = false; cur.textContent = info.state;
    cur.className = "wfcur " + (term ? "done" : "moving");
    const opts = allowedFrom(wf, info.state);
    stSel.innerHTML = "";
    stSel.append(new Option(opts.length ? "Advance…" : "✓ terminal", ""));
    for(const t of opts)
      stSel.append(new Option((t.event ? t.event + " → " : "→ ") + t.to, t.to));
    stSel.disabled = !opts.length; stSel.hidden = false;
  } else {
    cur.hidden = true; stSel.hidden = true;
  }
}
$("#wfAssign").onchange = async () => {
  if(!state.path) return;
  const name = $("#wfAssign").value;
  let st = "";
  if(name){ const wf = await getWf(name); st = wfStart(wf); }
  const r = await api.setState(state.path, {workflow:name, state:st});
  if(r.error) return alert(r.error);
  await loadTree();
};
$("#wfState").onchange = async () => {
  if(!state.path) return;
  const to = $("#wfState").value; if(!to) return;
  const info = stateMap[state.path]; if(!info) return;
  const r = await api.setState(state.path, {workflow:info.workflow, state:to});
  if(r.error) return alert(r.error);
  await loadTree();
};

/* --------------------------------------------------------- new / toolbar --- */
function selectedDir(){
  // create inside the selected folder, or the folder of the current doc
  if(state.path){
    return state.path.includes("/") ?
      state.path.split("/").slice(0,-1).join("/") : "";
  }
  return "";
}
$("#newDoc").onclick = async () => {
  const name = prompt("New document name:"); if(!name) return;
  const r = await api.create({parent:selectedDir(), name, type:"file"});
  if(r.error) return alert(r.error);
  await loadTree(); openDoc(r.path);
};
$("#newDir").onclick = async () => {
  const name = prompt("New folder name:"); if(!name) return;
  const r = await api.create({parent:selectedDir(), name, type:"dir"});
  if(r.error) return alert(r.error);
  state.open[r.path]=true; loadTree();
};
$("#refresh").onclick = loadTree;
$("#save").onclick = save;
$("#about").onclick = e => { e.preventDefault(); $("#modal").hidden = false; };
$("#closeAbout").onclick = () => { $("#modal").hidden = true; };
$("#modal").onclick = e => { if(e.target === $("#modal")) $("#modal").hidden = true; };
document.addEventListener("keydown", e => {
  if(e.key === "Escape") $("#modal").hidden = true;
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
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0,s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus(); state.dirty=true; render(); markSaved();
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
  const name = blob.name || ("paste-" + Date.now() +
      (asImage ? ".png" : (blob.type.split("/")[1] ? "."+blob.type.split("/")[1] : "")));
  status.textContent = "uploading…";
  const r = await api.upload(blob, name);
  if(r.error){ alert(r.error); return; }
  const label = blob.name || name;
  insertAtCursor(asImage ? `![${label}](${r.url})\n` : `[${label}](${r.url})\n`);
}

/* ---------------------------------------------------------- recording ----- */
let media = null;
async function record(kind){
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

/* --------------------------------------------------------------- boot ----- */
(async function boot(){
  await loadWorkflows();
  await loadTree();
  render(); markSaved();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def main():
    global CONTENT, ASSETS, WORKFLOWS, STATE_FILE
    ap = argparse.ArgumentParser(description="social-learning document manager")
    ap.add_argument("--host", default="127.0.0.1",
                    help="IP address to bind (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000,
                    help="TCP port to serve on (default 8000)")
    ap.add_argument("--dir", default="content", metavar="FOLDER",
                    help="folder to serve (default ./content)")
    ap.add_argument("--noprompt", action="store_true",
                    help="start serving immediately, without waiting for Enter")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = ap.parse_args()

    CONTENT = os.path.realpath(args.dir)
    ASSETS = os.path.join(CONTENT, "_assets")
    WORKFLOWS = os.path.join(CONTENT, "_workflows")
    STATE_FILE = os.path.join(WORKFLOWS, "_state.json")
    url = f"http://{args.host}:{args.port}/"

    print("social-learning")
    print(f"  content : {CONTENT}")
    print(f"  address : {url}")

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
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
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
    main()
