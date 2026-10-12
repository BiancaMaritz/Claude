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
import tempfile
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .analyze import analyse
from .document import build
from .loader import load_export
from .render import to_docx, to_html, to_markdown
from .store import Store, export_from_dict, valid_name

MAX_UPLOAD = 300 * 1024 * 1024  # larger exports are read from a local path instead
STATIC = os.path.join(os.path.dirname(__file__), "static")
DEFAULT_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "documents")


def parse_multipart(headers, body: bytes) -> list:
    """Return [(field, filename|None, bytes)] from a multipart/form-data body."""
    raw = b"Content-Type: " + headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    msg = BytesParser(policy=email_policy).parsebytes(raw)
    out = []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out.append((name, part.get_filename(), part.get_payload(decode=True) or b""))
    return out


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


def classify(uploads, paths):
    """Turn uploaded files and local paths into (exports, intakes, names, sources).

    uploads: [(filename, bytes)]. paths: local file or folder paths read straight from disk, so
    size is limited only by the disk. sources: [{name, fp}] identifying each file's content.
    Raises ValueError with a clear reason.
    """
    exports, intakes, names, sources = [], [], [], []
    for fname, data in uploads:
        low = fname.lower()
        if low.endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "export.zip")
                with open(path, "wb") as fh:
                    fh.write(data)
                ex = _load_zip_or_dir(fname, path)
            exports.append(ex)
            sources.append({"name": fname, "fp": ex.fingerprint})
        elif low.endswith(".json"):
            intakes.append(_parse_intake(fname, data))
            sources.append({"name": fname, "fp": hashlib.sha256(data).hexdigest()})
        else:
            raise ValueError(f"{fname}: only .zip exports and .json intake files are supported.")
        names.append(fname)
    for raw in paths:
        path = raw.strip().strip('"').strip("'")
        label = os.path.basename(path.rstrip("\\/")) or path
        if not os.path.exists(path):
            raise ValueError(f"{path}: not found on this machine.")
        if os.path.isdir(path) or path.lower().endswith(".zip"):
            ex = _load_zip_or_dir(label, path)
            exports.append(ex)
            sources.append({"name": label, "fp": ex.fingerprint})
        elif path.lower().endswith(".json"):
            with open(path, "rb") as fh:
                data = fh.read()
            intakes.append(_parse_intake(label, data))
            sources.append({"name": label, "fp": hashlib.sha256(data).hexdigest()})
        else:
            raise ValueError(f"{path}: give a .zip export, an extracted export folder, or a .json intake file.")
        names.append(label)
    return exports, intakes, names, sources


def classify_uploads(files):
    return classify(files, [])


class App:
    def __init__(self, data_dir):
        self.store = Store(data_dir)
        self._cache = {}

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
                       date=datetime.date.today().isoformat()) if ex.bots else []
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
                    return self._send(200, fh.read())
            if path == "/api/docs":
                return self._json(app.store.list())
            if path == "/api/doc":
                name = (q.get("name") or [""])[0]
                res = app.generate(name)
                if res is None:
                    return self._json({"error": "No such document."}, 404)
                doc, blocks = res
                menu, html = to_html(blocks) if blocks else ([], "")
                return self._json({"name": name, "title": doc["title"], "bots": len(doc["export"]["bots"]),
                                   "updated": doc["updated"], "history": doc["history"][-8:][::-1],
                                   "has_intake": bool(doc["intake"]), "menu": menu, "html": html})
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
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_UPLOAD:
                return self._json({"error": "Upload is empty or larger than 300 MB. For large exports use "
                                              "Load from path: the app reads the file from disk without uploading it."}, 400)
            body = self.rfile.read(length)
            try:
                if self.path == "/api/load":
                    parts = parse_multipart(self.headers, body)
                    field = {n: d.decode("utf-8").strip() for n, f, d in parts if f is None}
                    name = field.get("doc", "")
                    if not valid_name(name):
                        raise ValueError("Document name must be 1 to 60 letters, digits, spaces, dot, dash or underscore.")
                    files = [(f, d) for n, f, d in parts if n == "files" and f]
                    paths = [x for x in field.get("paths", "").splitlines() if x.strip()]
                    if not files and not paths:
                        raise ValueError("Choose a file or give a path to a .zip export, export folder or .json intake.")
                    exports, intakes, names, sources = classify(files, paths)
                    report = app.store.load(name, field.get("mode", "add"), exports, intakes, names,
                                            title=field.get("title") or None, sources=sources)
                    return self._json({"ok": True, "name": name, "report": report})
                if self.path == "/api/delete":
                    name = json.loads(body).get("name", "")
                    return self._json({"ok": app.store.delete(name)})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            self._json({"error": "Not found."}, 404)

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sdd_generator.web", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default=DEFAULT_DATA, help="where documents are stored")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(App(args.data_dir), args.port))
    url = f"http://localhost:{args.port}/"
    print(f"SDD Generator running at {url}\nDocuments stored in {args.data_dir}\nCtrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
