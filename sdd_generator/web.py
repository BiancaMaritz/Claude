"""Local web app: load A360 exports into a document, preview the SDD, download it.

Binds to 127.0.0.1 only; nothing leaves the machine.
    python -m sdd_generator.web [--port 8765] [--data-dir DIR] [--no-browser]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import zipfile
import webbrowser
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .analyze import analyse
from .docparse import parse_document
from .document import build
from .gaps import analyse_gaps, to_csv as gaps_csv
from .rules import analyse_rules
from .loader import load_export
from .media import (CONTENT_TYPES, IMAGE_EXT, SECTION_KEYS, SECTIONS, TRANSCRIPT_EXT, VIDEO_EXT, ext_of,
                    new_id, parse_transcript)
from .render import to_docx, to_html, to_markdown
from .store import Store, export_from_dict, find_stored_documents, valid_name
from .tables import TABLE_EXT, analyze_file

CHUNK = 1024 * 1024
STATIC = os.path.join(os.path.dirname(__file__), "static")
DOC_EXT = (".pdf", ".docx", ".md", ".txt")
# SDD_DATA_DIR keeps stored documents in one fixed place, whichever folder the code is run from.
DEFAULT_DATA = os.environ.get("SDD_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "documents")


def _load_zip_or_dir(label, source):
    try:
        ex = load_export(source)
    except Exception as e:  # bad zip, bad manifest JSON, unreadable folder
        raise ValueError(f"{label}: not a readable export ({type(e).__name__}: {e}).") from e
    if not ex.bots:
        raise ValueError(f"{label}: no bots found. Use the zip or folder exported from the Control Room.")
    ex.source = label
    return ex


def _parse_intake(label, data: bytes):
    try:
        d = json.loads(data.decode("utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{label}: invalid JSON ({e}).") from e
    if not isinstance(d, dict) or "nodes" in d or "files" in d:
        raise ValueError(f"{label}: expected an intake file. Bot files and manifests go inside the export zip.")
    return d


def classify(uploads, paths, staged=()):
    """Turn uploaded bytes, staged files and local paths into (exports, intakes, names, sources).

    uploads: [(filename, bytes)]. staged: [(label, path)] files the browser streamed to disk.
    paths: local file or folder paths. Everything except `uploads` is read from disk, so size is
    limited only by the disk. sources: [{name, fp}] identifying each file's content.
    Raises ValueError with a clear reason.
    """
    exports, intakes, names, sources = [], [], [], []

    def add_export(label, source):
        ex = _load_zip_or_dir(label, source)
        exports.append(ex)
        sources.append({"name": label, "fp": ex.fingerprint})
        names.append(label)

    def add_intake(label, data):
        intakes.append(_parse_intake(label, data))
        sources.append({"name": label, "fp": hashlib.sha256(data).hexdigest()})
        names.append(label)

    for fname, data in uploads:
        low = fname.lower()
        if low.endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "export.zip")
                with open(path, "wb") as fh:
                    fh.write(data)
                add_export(fname, path)
        elif low.endswith(".json"):
            add_intake(fname, data)
        else:
            raise ValueError(f"{fname}: only .zip exports and .json intake files are supported.")
    items = [(label, path) for label, path in staged]
    for raw in paths:
        path = raw.strip().strip('"').strip("'")
        items.append((os.path.basename(path.rstrip("\\/")) or path, path))
    for label, path in items:
        if not os.path.exists(path):
            raise ValueError(f"{label}: not found on this machine.")
        if os.path.isdir(path) or label.lower().endswith(".zip"):
            add_export(label, path)
        elif label.lower().endswith(".json"):
            with open(path, "rb") as fh:
                add_intake(label, fh.read())
        else:
            raise ValueError(f"{label}: give a .zip export, an extracted export folder, or a .json intake file.")
    return exports, intakes, names, sources


def classify_uploads(files):
    return classify(files, [])


def classify_docs(staged, paths):
    """Documentation files and intake JSON from staged uploads and local paths.

    Returns (parsed documents, intake dicts). A folder path contributes every documentation file
    inside it. Raises ValueError with a clear reason.
    """
    items = list(staged)
    for raw in paths:
        path = raw.strip().strip('"').strip("'")
        if not os.path.exists(path):
            raise ValueError(f"{path}: not found on this machine.")
        if os.path.isdir(path):
            found = sorted(os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs
                           if f.lower().endswith(DOC_EXT) and not f.startswith("."))
            if not found:  # .json is never picked up from a folder: it may be anything, so name it explicitly
                raise ValueError(f"{path}: no .pdf, .docx or .md files in that folder.")
            items += [(os.path.basename(f), f) for f in found[:200]]
        else:
            items.append((os.path.basename(path), path))
    parsed, intakes = [], []
    for label, path in items:
        low = label.lower()
        if low.endswith(DOC_EXT):
            parsed.append(parse_document(path, label))
        elif low.endswith(".json"):
            with open(path, "rb") as fh:
                intakes.append(_parse_intake(label, fh.read()))
        else:
            raise ValueError(f"{label}: documentation must be .pdf, .docx, .md or an intake .json.")
    return parsed, intakes


def classify_tables(staged, paths):
    """Table schemas from staged uploads and local paths (a file, or a folder of CSV/Excel files).

    Returns (tables, file names). A table named twice keeps the later one.
    """
    items = list(staged)
    for raw in paths:
        path = raw.strip().strip('"').strip("'")
        if not os.path.exists(path):
            raise ValueError(f"{path}: not found on this machine.")
        if os.path.isdir(path):
            found = sorted(os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs
                           if f.lower().endswith(TABLE_EXT) and not f.startswith(("~$", ".")))
            if not found:
                raise ValueError(f"{path}: no .csv, .tsv or .xlsx files in that folder.")
            items += [(os.path.basename(f), f) for f in found[:200]]
        else:
            items.append((os.path.basename(path), path))
    tables = {}
    for label, path in items:
        for t in analyze_file(path, label):
            tables[t["name"]] = t
    return list(tables.values()), [label for label, _ in items]


class App:
    def __init__(self, data_dir):
        self.store = Store(data_dir)
        self._cache = {}
        self._gap_cache = {}
        self.staging = os.path.join(os.path.dirname(os.path.abspath(data_dir)), "staging")
        shutil.rmtree(self.staging, ignore_errors=True)  # leftovers from a previous run
        os.makedirs(self.staging)
        self._staged = {}  # token -> (label, path)
        self.code_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def elsewhere(self):
        """Stored documents found in other folders, so a changed install location never hides them."""
        roots = [self.code_root, os.path.dirname(self.code_root)]
        if os.name == "nt":
            roots.append(r"C:\Dev\sdd-generator")
        have = {d["name"] for d in self.store.list()}
        # only what this install does not already hold, so the notice goes away once recovered
        return [(f, [n for n in names if n not in have]) for f, names in find_stored_documents(self.store.root, roots)
                if any(n not in have for n in names)]

    def stage(self, label, rfile, length):
        """Stream an upload to disk in chunks, so file size never depends on memory."""
        ext = os.path.splitext(label)[1].lower()
        if ext not in (".zip", ".json") + DOC_EXT + VIDEO_EXT + IMAGE_EXT + TRANSCRIPT_EXT + TABLE_EXT:
            raise ValueError(f"{label}: unsupported file type.")
        token = secrets.token_hex(8)
        path = os.path.join(self.staging, token + ext)
        left = length
        with open(path, "wb") as fh:
            while left > 0:
                chunk = rfile.read(min(CHUNK, left))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(path)
            raise ValueError(f"{label}: upload was interrupted.")
        self._staged[token] = (label, path)
        return token

    def take_staged(self, tokens):
        """Resolve tokens to [(label, path)]; unknown tokens are an error."""
        out = []
        for t in tokens:
            if t not in self._staged:
                raise ValueError("An uploaded file has expired. Choose it again.")
            out.append(self._staged[t])
        return out

    def drop_staged(self, tokens):
        for t in tokens:
            _, path = self._staged.pop(t, (None, None))
            if path and os.path.exists(path):
                os.remove(path)

    def generate(self, name):
        """Return (doc, blocks, analysis) for a stored document, or None."""
        doc = self.store.get(name)
        if doc is None:
            return None
        key = (doc["updated"], doc.get("rev", 0))
        hit = self._cache.get(name)
        if hit and hit[0] == key:
            return doc, hit[1]
        ex = export_from_dict(doc["export"])
        extra = {"videos": doc["videos"], "graphs": doc["graphs"], "tables": doc["tables"], "project": name,
                 "media_dir": self.store.media_dir(name), "acceptances": doc["acceptances"]}
        has_content = ex.bots or doc["documentation"] or doc["videos"] or doc["graphs"] or doc["tables"]
        blocks = build(ex, analyse(ex), doc["intake"], title=doc["title"], date=datetime.date.today().isoformat(),
                       docs=doc["documentation"], extra=extra) if has_content else []
        self._cache[name] = (key, blocks)
        return doc, blocks

    def gaps(self, name):
        """The Analysis tab's result for a stored project, or None."""
        doc = self.store.get(name)
        if doc is None:
            return None
        key = (doc["updated"], doc.get("rev", 0))
        hit = self._gap_cache.get(name)
        if hit and hit[0] == key:
            return hit[1]
        ex = export_from_dict(doc["export"])
        res = analyse_gaps(ex, analyse(ex), doc["documentation"], doc["tables"])
        res["rules"] = analyse_rules(ex, doc["documentation"], doc["acceptances"])
        self._gap_cache[name] = (key, res)
        return res

    def summary(self, name):
        """Everything the menu views need about a project, without building the SDD."""
        doc = self.store.get(name)
        if doc is None:
            if not valid_name(name):
                return None
            doc = {"title": name, "updated": "", "intake": {}, "sources": [], "documentation": [], "videos": [],
                   "graphs": [], "tables": [], "history": [], "export": {"bots": {}}, "fresh": True}
        folders = {}
        for path in doc["export"]["bots"]:
            parts = path.split("/")
            folder = "/".join(parts[2:-1]) or "(root)"
            folders[folder] = folders.get(folder, 0) + 1
        item = lambda x: {k: v for k, v in x.items() if k not in ("transcript", "path")}
        return {
            "name": name, "title": doc["title"], "updated": doc["updated"], "data_dir": self.store.root,
            "fresh": bool(doc.get("fresh")),
            "has_intake": bool(doc["intake"]), "sections": [{"key": k, "label": l} for k, l in SECTIONS],
            "code": {"bots": len(doc["export"]["bots"]),
                     "assets": [{"name": a_["path"].rsplit("/", 1)[-1], "kind": a_["kind"],
                                 "folder": "/".join(a_["path"].split("/")[2:-1])} for a_ in doc["export"].get("assets", [])],
                     "sources": [{"name": x["name"], "at": x.get("at", "")} for x in doc["sources"]],
                     "folders": [{"folder": f, "bots": n} for f, n in sorted(folders.items())]},
            "documentation": [{"name": d["name"], "fp": d["fp"], "pages": d["pages"], "sections": sorted(d["sections"])}
                              for d in doc["documentation"]],
            "videos": [dict(item(v), url=f"/media/{name}/{v['id']}", has_file=self._media_path(name, v) is not None,
                            transcript=({"name": v["transcript"]["name"], "chars": v["transcript"]["chars"]}
                                        if v.get("transcript") else None)) for v in doc["videos"]],
            "graphs": [dict(item(g), url=f"/media/{name}/{g['id']}") for g in doc["graphs"]],
            "tables": doc["tables"],
            "history": doc["history"][-12:][::-1],
            "gaps": ({"to_provide": g["summary"]["to_provide"],
                      "open_rules": g["rules"]["summary"]["open"] + g["rules"]["summary"]["changed"]}
                     if (g := self.gaps(name)) else {"to_provide": 0, "open_rules": 0}),
        }

    def _media_path(self, name, item):
        """Absolute path of a stored or linked media file, or None if it is gone."""
        path = item.get("path") if item.get("kind") == "path" else os.path.join(self.store.media_dir(name), item["file"])
        return path if path and os.path.isfile(path) else None

    def media_for(self, name, item_id):
        doc = self.store.get(name)
        if not doc or not re.fullmatch(r"[0-9a-f]{12}", item_id or ""):
            return None
        for kind in ("videos", "graphs"):
            for it in doc[kind]:
                if it["id"] == item_id:
                    path = self._media_path(name, it)
                    return (path, CONTENT_TYPES.get(ext_of(path), "application/octet-stream")) if path else None
        return None

    def add_media(self, name, kind, staged, paths, section, caption=""):
        """Add videos or graphs from staged uploads and local paths. Returns the new items."""
        if section not in SECTION_KEYS:
            raise ValueError("Unknown section.")
        allowed = VIDEO_EXT if kind == "videos" else IMAGE_EXT
        what = "video" if kind == "videos" else "image"
        items = [(label, path, True) for label, path in staged]
        for raw in paths:
            path = raw.strip().strip('"').strip("'")
            if not os.path.isfile(path):
                raise ValueError(f"{path}: not found on this machine.")
            items.append((os.path.basename(path), path, False))
        if not items:
            raise ValueError(f"Choose a {what} file or give its path.")
        for label, _, _ in items:
            if ext_of(label) not in allowed:
                raise ValueError(f"{label}: not a supported {what} file ({', '.join(allowed)}).")
        added = []
        for label, path, staged_file in items:
            item_id = new_id()
            entry = {"id": item_id, "name": label, "size": os.path.getsize(path), "section": section,
                     "caption": caption[:200], "added": datetime.datetime.now().isoformat(timespec="seconds")}
            if kind == "videos":
                entry["transcript"] = None
            if staged_file or kind == "graphs":  # uploads and images are kept in the project's media folder
                dest = os.path.join(self.store.media_dir(name), item_id + ext_of(label))
                (os.replace if staged_file else shutil.copy2)(path, dest)
                entry.update(kind="file", file=item_id + ext_of(label))
            else:  # a video given by path stays where it is; copying gigabytes would help nobody
                entry.update(kind="path", path=os.path.abspath(path))
            self.store.add_item(name, kind, entry)
            added.append(entry)
        return added


def make_handler(app: App, port: int):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path, ctype):
            """Serve a local file with Range support, so a video can be played and skipped through."""
            size = os.path.getsize(path)
            start, end, code = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
            if m and (m.group(1) or m.group(2)):
                a, b = m.groups()
                start = max(0, size - int(b)) if not a else int(a)
                end = min(size - 1, int(b)) if (a and b) else size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("X-Content-Type-Options", "nosniff")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = fh.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the player stopped reading, which is normal when seeking

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _same_origin(self) -> bool:
            """Reject requests from other sites and DNS-rebinding hosts."""
            host_ok = self.headers.get("Host", "") in (f"localhost:{port}", f"127.0.0.1:{port}")
            origin = self.headers.get("Origin")
            return host_ok and (origin is None or origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}"))

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = unquote(u.path)
            if not self._same_origin():
                return self._json({"error": "Forbidden"}, 403)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/":
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read().replace(b"@@VERSION@@", __version__.encode()))
            if path == "/api/docs":
                return self._json(app.store.list())
            if path == "/api/recover":
                return self._json([{"folder": f, "documents": n} for f, n in app.elsewhere()])
            if path == "/api/analysis":
                res = app.gaps((q.get("name") or [""])[0])
                return self._json(res) if res else self._json({"error": "No such project."}, 404)
            if path == "/api/project":
                res = app.summary((q.get("name") or [""])[0])
                return self._json(res) if res else self._json({"error": "No such project."}, 404)
            if path.startswith("/media/"):
                parts = path.split("/")
                found = app.media_for(parts[2], parts[3]) if len(parts) == 4 else None
                return self._file(*found) if found else self._json({"error": "Not found."}, 404)
            if path == "/api/doc":
                name = (q.get("name") or [""])[0]
                res = app.generate(name)
                if res is None:
                    return self._json({"error": "No such document."}, 404)
                doc, blocks = res
                menu, html = to_html(blocks) if blocks else ([], "")
                return self._json({
                    "name": name, "title": doc["title"], "bots": len(doc["export"]["bots"]),
                    "updated": doc["updated"], "history": doc["history"][-8:][::-1],
                    "has_intake": bool(doc["intake"]), "menu": menu, "html": html,
                    "sources": [{"name": x["name"], "at": x.get("at", "")} for x in doc["sources"]],
                    "documentation": [{"name": d["name"], "fp": d["fp"], "pages": d["pages"],
                                       "sections": sorted(d["sections"])} for d in doc["documentation"]],
                    "data_dir": app.store.root})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "download" and parts[2] == "analysis":
                res = app.gaps(parts[1])
                if res:
                    return self._send(200, ("\ufeff" + gaps_csv(res)).encode("utf-8"), "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{parts[1].replace(" ", "_")}_missing_bots.csv"'})
            if len(parts) == 3 and parts[0] == "download":
                res = app.generate(parts[1])
                fmt = parts[2]
                if res and res[1] and fmt in ("docx", "md", "json"):
                    doc, blocks = res
                    stem = parts[1].replace(" ", "_") + "_SDD"
                    images = [b[1] for b in blocks if b[0] == "img" and os.path.exists(b[1]["path"])]
                    if fmt == "md" and images:  # the Markdown links to media/, so ship it as a bundle
                        buf = BytesIO()
                        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                            z.writestr("SDD.md", to_markdown(blocks))
                            for im in {i["file"]: i for i in images}.values():
                                z.write(im["path"], "media/" + im["file"])
                        data, ctype = buf.getvalue(), "application/zip"
                        return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{stem}_md.zip"'})
                    if fmt == "md":
                        data, ctype = to_markdown(blocks).encode("utf-8"), "text/markdown; charset=utf-8"
                    elif fmt == "json":
                        data, ctype = json.dumps(doc["intake"], indent=2).encode("utf-8"), "application/json"
                        stem += "_intake"
                    else:
                        with tempfile.TemporaryDirectory() as tmp:
                            p = os.path.join(tmp, "x.docx")
                            to_docx(blocks, p)
                            with open(p, "rb") as fh:
                                data = fh.read()
                        ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    return self._send(200, data, ctype,
                                      {"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'})
            self._json({"error": "Not found."}, 404)

        def do_POST(self):
            if not self._same_origin():
                return self._json({"error": "Forbidden"}, 403)
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                if u.path == "/api/stage":
                    label = os.path.basename((parse_qs(u.query).get("name") or [""])[0])
                    if not label or length <= 0:
                        raise ValueError("Empty upload.")
                    token = app.stage(label, self.rfile, length)
                    return self._json({"token": token, "name": label, "size": length})
                body = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
                if u.path == "/api/load":
                    req = json.loads(body or b"{}")
                    tokens = [str(t) for t in req.get("staged", [])]
                    try:  # staged uploads are always removed, whether or not the load succeeds
                        name = str(req.get("doc", "")).strip()
                        if not valid_name(name):
                            raise ValueError("Document name must be 1 to 60 letters, digits, spaces, dot, dash or underscore.")
                        paths = [x for x in str(req.get("paths", "")).splitlines() if x.strip()]
                        if not tokens and not paths:
                            raise ValueError("Choose a file or give a path to a .zip export, export folder or .json intake.")
                        exports, intakes, names, sources = classify([], paths, app.take_staged(tokens))
                        report = app.store.load(name, str(req.get("mode", "add")), exports, intakes, names,
                                                title=str(req.get("title", "")).strip() or None, sources=sources)
                    finally:
                        app.drop_staged(tokens)
                    return self._json({"ok": True, "name": name, "report": report})
                if u.path == "/api/documentation/load":
                    req = json.loads(body or b"{}")
                    tokens = [str(t) for t in req.get("staged", [])]
                    try:
                        name = str(req.get("doc", "")).strip()
                        if not valid_name(name):
                            raise ValueError("Document name must be 1 to 60 letters, digits, spaces, dot, dash or underscore.")
                        paths = [x for x in str(req.get("paths", "")).splitlines() if x.strip()]
                        if not tokens and not paths:
                            raise ValueError("Choose a file or give a path to a documentation file or folder.")
                        parsed, intakes = classify_docs(app.take_staged(tokens), paths)
                        report = app.store.load_docs(name, str(req.get("mode", "add")), parsed, intakes,
                                                     title=str(req.get("title", "")).strip() or None)
                    finally:
                        app.drop_staged(tokens)
                    return self._json({"ok": True, "name": name, "report": report})
                if u.path in ("/api/video/add", "/api/graph/add", "/api/video/transcript", "/api/tables/load"):
                    req = json.loads(body or b"{}")
                    tokens = [str(t) for t in req.get("staged", [])]
                    try:  # staged uploads are always removed, whether or not the request succeeds
                        name = str(req.get("doc", "")).strip()
                        if not valid_name(name):
                            raise ValueError("Project name must be 1 to 60 letters, digits, spaces, dot, dash or underscore.")
                        paths = [x for x in str(req.get("paths", "")).splitlines() if x.strip()]
                        staged = app.take_staged(tokens)
                        if u.path == "/api/tables/load":
                            if not staged and not paths:
                                raise ValueError("Choose a table export or give its path.")
                            found = classify_tables(staged, paths)
                            report = app.store.load_tables(name, str(req.get("mode", "add")), found[0], found[1])
                            return self._json({"ok": True, "report": report})
                        if u.path == "/api/video/transcript":
                            doc = app.store.get(name)
                            if not doc or not any(v["id"] == req.get("id") for v in doc["videos"]):
                                raise ValueError("No such video.")
                            files = staged + [(os.path.basename(x.strip().strip('"')), x.strip().strip('"')) for x in paths]
                            if len(files) != 1 or not os.path.isfile(files[0][1]):
                                raise ValueError("Give exactly one transcript file.")
                            t = parse_transcript(files[0][1], files[0][0])
                            return self._json({"ok": True, "item": app.store.update_item(name, "videos", req["id"], {"transcript": t})["id"]})
                        kind = "videos" if u.path == "/api/video/add" else "graphs"
                        added = app.add_media(name, kind, staged, paths, str(req.get("section", "appendix")),
                                              str(req.get("caption", "")))
                        return self._json({"ok": True, "added": [a["name"] for a in added]})
                    finally:
                        app.drop_staged(tokens)
                if u.path == "/api/item/update":
                    req = json.loads(body or b"{}")
                    fields = {k: req[k] for k in ("section", "caption") if k in req}
                    if req.get("transcript") is None and "transcript" in req:
                        fields["transcript"] = None
                    app.store.update_item(str(req.get("doc", "")), str(req.get("kind", "")), str(req.get("id", "")), fields)
                    return self._json({"ok": True})
                if u.path == "/api/item/remove":
                    req = json.loads(body or b"{}")
                    name = str(req.get("doc", ""))
                    item = app.store.remove_item(name, str(req.get("kind", "")), str(req.get("id", "")))
                    if item and item.get("kind") == "file":
                        try:
                            os.remove(os.path.join(app.store.media_dir(name), item["file"]))
                        except OSError:
                            pass
                    return self._json({"ok": bool(item)})
                if u.path in ("/api/rules/accept", "/api/rules/reopen"):
                    req = json.loads(body or b"{}")
                    name, fid = str(req.get("doc", "")), str(req.get("id", ""))
                    if u.path == "/api/rules/reopen":
                        return self._json({"ok": app.store.reopen(name, fid)})
                    res = app.gaps(name)
                    found = next((f for f in (res["rules"]["findings"] if res else []) if f["id"] == fid), None)
                    if not found:
                        raise ValueError("That difference no longer exists. Reload the page.")
                    # the values accepted are the server's own, never the browser's
                    app.store.accept(name, fid, found["sig"], str(req.get("note", "")).strip())
                    return self._json({"ok": True})
                if u.path == "/api/tables/remove":
                    req = json.loads(body or b"{}")
                    return self._json({"ok": app.store.remove_table(str(req.get("doc", "")), str(req.get("table", "")))})
                if u.path == "/api/recover":
                    req = json.loads(body or b"{}")
                    got = app.store.import_from(str(req.get("folder", "")))
                    return self._json({"ok": True, "imported": got})
                if u.path == "/api/documentation/remove":
                    req = json.loads(body or b"{}")
                    return self._json({"ok": app.store.remove_doc(str(req.get("doc", "")), str(req.get("fp", "")))})
                if u.path == "/api/delete":
                    return self._json({"ok": app.store.delete(json.loads(body or b"{}").get("name", ""))})
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            self._json({"error": "Not found."}, 404)

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

    return Handler


def migrate_legacy(data_dir):
    """Copy documents stored beside the code (older versions) into the data folder, once.

    Runs only when the data folder has no documents yet, so it can never overwrite anything.
    """
    legacy = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "documents")
    if os.path.abspath(legacy) == os.path.abspath(data_dir) or not os.path.isdir(legacy):
        return 0
    have = os.path.isdir(data_dir) and any(f.endswith(".sdd.json") for f in os.listdir(data_dir))
    old = [f for f in os.listdir(legacy) if f.endswith(".sdd.json")]
    if have or not old:
        return 0
    os.makedirs(data_dir, exist_ok=True)
    for f in old:
        shutil.copy2(os.path.join(legacy, f), os.path.join(data_dir, f))
    return len(old)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sdd_generator.web", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default=DEFAULT_DATA, help="where documents are stored")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler)
    except OSError as e:
        print(f"\nCannot start: port {args.port} is not available ({e.strerror or e}).\n"
              f"An older copy of this app is probably still running and is what your browser is showing.\n"
              f"Close its window, or on Windows run:  netstat -ano | findstr :{args.port}\n"
              f"then:  taskkill /PID <the number in the last column> /F\n"
              f"Or start this one on another port:  run.bat --port {args.port + 1}")
        return 1
    # Only after the port is ours: building the app clears the staging folder, which a copy
    # that is already running may still be using.
    moved = migrate_legacy(args.data_dir)
    if moved:
        print(f"Copied {moved} stored document(s) from the old location into {args.data_dir}")
    app = App(args.data_dir)
    if not app.store.list():  # nothing here yet: bring across anything an earlier install left behind
        newest = lambda f: max(os.path.getmtime(os.path.join(f, n)) for n in os.listdir(f) if n.endswith(".sdd.json"))
        for folder, names in sorted(app.elsewhere(), key=lambda fn: newest(fn[0]), reverse=True):
            got = app.store.import_from(folder, rename=False)  # newest copy of each document wins
            print(f"Recovered {len(got)} document(s) from {folder}: {', '.join(got.values())}")
    for folder, names in app.elsewhere():
        print(f"Note: other stored documents exist in {folder} ({', '.join(names)}). Use Recover in the page to import them.")
    server.RequestHandlerClass = make_handler(app, args.port)
    url = f"http://localhost:{args.port}/"
    print(f"SDD Generator v{__version__} running at {url}\nDocuments stored in {args.data_dir}\nCtrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
