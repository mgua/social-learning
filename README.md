# social-learning

A compact, single-file document manager that runs locally in your browser.
Build and maintain a hierarchy of markdown documents, paste images/files,
drop links (YouTube links become clickable thumbnails), and record audio/video
straight from the browser.

The entire application — web server, HTML, CSS and JavaScript — is one file
(`app.py`) using only the Python 3.10+ standard library. **No dependencies, no
build step, no login.**

## Run

```sh
python3 app.py                 # serves http://127.0.0.1:8000 and opens a browser
python3 app.py --port 9000     # pick a port
python3 app.py --no-open       # don't launch a browser
```

Stop with `Ctrl+C`.

## How content is stored

Everything you create is saved as plain files under `content/`, so the repo
*is* the database. That makes documents easy to review, diff, and share:
contributors just add or edit documents and `git push`.

```
content/
  <folder>/<document>.md    one markdown file per document; folders form the tree
  _assets/                  pasted images/files and audio/video recordings
```

## Using it

- **Tree (left):** `+ Doc` / `+ Folder` create inside the current selection;
  hover a row for rename (✎) and delete (🗑).
- **Editor / preview:** toggle editor-only, split, or preview-only in the header.
  Edits autosave ~1s after you stop typing; `Ctrl/Cmd+S` saves immediately.
- **Paste:** paste an image and it's uploaded and embedded; paste any other file
  and it's stored and inserted as a download link. Pasted URLs are clickable in
  the preview; YouTube links render as clickable thumbnails.
- **Record:** 🎤 audio and 🎥 video use the browser camera/mic (`MediaRecorder`);
  the clip is saved into `content/_assets/` and embedded as a player.

## Roadmap

- Server deployment with user login/auth (the current design is single-user,
  local-only, and intentionally trusts the local filesystem).
