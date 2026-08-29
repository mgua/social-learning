# social-learning

A compact, single-file document manager that runs locally in your browser.
Build and maintain a hierarchy of markdown documents, paste images/files,
drop links (YouTube links become clickable thumbnails), and record audio/video
straight from the browser.

The entire application — web server, HTML, CSS and JavaScript — is one file
(`social-learning-server.py`) using only the Python 3.10+ standard library.
**No dependencies, no build step, no login.**

Several people can share one server: the sidebar refreshes on its own, a 👥 badge
shows who else has a document open, and if two of you save the same document the
second save is stopped so you can choose what to keep — nobody's text is lost.

**social-learning** has been created in 2020 by [Neta srl](https://neta.it), and 
subsequently evolved.
It is currently mantained by [Marco Guardigli](https://marco.guardigli.it), 
with the help of Anthropic Claude.

## Run

```sh
python3 social-learning-server.py                # http://127.0.0.1:8000, opens a browser
python3 social-learning-server.py --port 9000    # pick a port
python3 social-learning-server.py --host 0.0.0.0 # let others on your network in
python3 social-learning-server.py --token SECRET # require a secret to write
python3 social-learning-server.py --no-open      # don't launch a browser
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
  _versions/                the previous text of anything overwritten or deleted
  _workflows/               workflow definitions and each document's state
```

Changes made outside the app — a `git pull`, another editor — are noticed rather
than silently overwritten: a save that was based on text somebody has since
replaced is stopped and shown to you.

## Using it

- **Tree (left):** `+ Doc` / `+ Folder` create inside the current selection;
  hover a row for rename (✎) and delete (🗑).
- **Editor / preview:** toggle editor-only, split, or preview-only in the header.
  Edits autosave ~1s after you stop typing; `Ctrl/Cmd+S` saves immediately.
- **Paste:** paste an image and it's uploaded and embedded; paste any other file
  and it's stored and inserted as a download link. Pasted URLs are clickable in
  the preview; YouTube links render as clickable thumbnails.
- **Attach:** 📎 picks any file, or drag one onto the editor.
- **Record:** 🎤 audio and 🎥 video use the browser camera/mic (`MediaRecorder`);
  the clip is saved into `content/_assets/` and embedded as a player.
- **Together:** an idle editor picks up someone else's save on its own. If you
  have unsaved changes when they save, your text is left alone and a bar offers
  **Save as a copy** (keeps both), **Overwrite**, **Reload**, or **Show
  differences**. Whatever gets replaced is kept under `content/_versions/`.


## Integrations

A great integration for digital books is to use social-learning with [Calibre](https://calibre-ebook.com/) 
by Kovid Goyal and with [Pandoc](https://pandoc.org/) by John McFarlane.
**Calibre** allows to manage e-book collections.
**Pandoc** allows to reformat ebooks into markdown and many other formats. Markdown 
conversion allows to import efficiently those contents in git repos, for reading/editing
and content updates. 
```bash
  # this pandoc command converts an epub ebook into markdown
  pandoc book.epub -t gfm -o book.md, --extract-media
```


## Sharing a server

`--host 0.0.0.0` lets other people on your network use the same server, and
`--token SECRET` then requires that secret for any write (contributors paste it
into the 🔑 box in the header). Be clear about what this is: a shared password,
not accounts. The name in the 👤 box is self-declared, so "saved by alice" is a
helpful hint and not proof. Don't put this on an untrusted network yet.

## Roadmap

- Real user login/auth, so identity and attribution can be trusted (writes can
  be gated with `--token` today, but that is a shared secret, not accounts).
- Simultaneous typing in the same paragraph is deliberately not supported: it
  would need a hand-written OT/CRDT and an operation log, which would make the
  `.md` files a render target instead of the source of truth and break the
  git-as-datastore model. Concurrent *use* is supported; concurrent typing in
  one paragraph is not.
