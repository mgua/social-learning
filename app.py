#!/usr/bin/env python3
"""
social-learning — a compact, single-file document manager.

Run locally:   python3 app.py            (opens http://127.0.0.1:8000)
               python3 app.py --port 9000 --no-open

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

BASE = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(BASE, "content")
ASSETS = os.path.join(CONTENT, "_assets")

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("audio/webm", ".weba")


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
def ensure_dirs():
    os.makedirs(ASSETS, exist_ok=True)


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
        if name.startswith(".") or name == "_assets":
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
            if path.startswith("/content/"):
                return self._serve_file(safe_join(path[len("/content/"):]))
            return self._error(404, "not found")
        except PermissionError as e:
            return self._error(403, str(e))
        except Exception as e:  # pragma: no cover
            return self._error(500, str(e))

    do_HEAD = do_GET

    def do_PUT(self):
        try:
            if urlparse(self.path).path == "/api/doc":
                rel = self._query().get("path", [""])[0]
                full = safe_join(rel)
                if not full.lower().endswith(".md"):
                    return self._error(400, "only .md documents")
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(self._body().decode("utf-8"))
                return self._json({"ok": True})
            return self._error(404, "not found")
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
                return self._json({"ok": True, "path":
                                   os.path.relpath(dst, CONTENT).replace(os.sep, "/")})

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
        try:
            if urlparse(self.path).path == "/api/doc":
                full = safe_join(self._query().get("path", [""])[0])
                if os.path.isdir(full):
                    shutil.rmtree(full)
                elif os.path.isfile(full):
                    os.remove(full)
                else:
                    return self._error(404, "not found")
                return self._json({"ok": True})
            return self._error(404, "not found")
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
</style>
</head>
<body>
<header>
  <h1>📚 social-learning</h1>
  <a href="#" id="about" title="Features / Funzionalità">ℹ Info</a>
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
};

let state = {path:null, dirty:false, open:{}};
const ta = $("#ta"), preview = $("#preview"), status = $("#status");

/* ---------------------------------------------------------------- tree --- */
async function loadTree(){
  const data = await api.tree();
  const el = $("#tree"); el.innerHTML = "";
  el.appendChild(renderNodes(data, ""));
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

/* --------------------------------------------------------------- boot ----- */
loadTree(); render(); markSaved();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="social-learning document manager")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = ap.parse_args()

    ensure_dirs()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"social-learning serving {CONTENT}\n  {url}\n(Ctrl+C to stop)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
