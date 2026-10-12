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
import secrets
import shutil
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .analyze import analyse
from .docparse import parse_document
from .document import build
from .loader import load_export
from .render import to_docx, to_html, to_markdown
from .store import Store, export_from_dict, find_stored_documents, valid_name

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


class App:
    def __init__(self, data_dir):
        self.store = Store(data_dir)
        self._cache = {}
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
        if ext not in (".zip", ".json") + DOC_EXT:
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
        hit = self._cache.get(name)
        if hit and hit[0] == doc["updated"]:
            return doc, hit[1]
        ex = export_from_dict(doc["export"])
        blocks = build(ex, analyse(ex), doc["intake"], title=doc["title"],
                       date=datetime.date.today().isoformat(), docs=doc["documentation"]
                       ) if ex.bots or doc["documentation"] else []
        self._cache[name] = (doc["updated"], blocks)
        return doc, blocks


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
            if len(parts) == 3 and parts[0] == "download":
                res = app.generate(parts[1])
                fmt = parts[2]
                if res and res[1] and fmt in ("docx", "md", "json"):
                    doc, blocks = res
                    stem = parts[1].replace(" ", "_") + "_SDD"
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
