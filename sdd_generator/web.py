"""Local web UI: upload an A360 export, download the generated SDD.

Binds to 127.0.0.1 only. Nothing leaves the machine. Standard library plus python-docx.
    python -m sdd_generator.web [--port 8765] [--no-browser]
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import secrets
import tempfile
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .analyze import analyse
from .document import build
from .loader import load_export
from .render import to_docx, to_markdown

MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB
RESULTS: dict[str, dict] = {}  # id -> {filename: bytes}

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SDD Generator</title>
<style>
:root{--bg:#f6f7f9;--fg:#1c2024;--card:#fff;--line:#d9dde3;--acc:#1f5fbf;--mut:#5b6470}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e8eaed;--card:#1d2125;--line:#2f353b;--acc:#6ea8ff;--mut:#9aa3ad}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:20px;margin:16px 0}
label{display:block;font-weight:600;margin:14px 0 4px}
input[type=text],input[type=file]{width:100%;box-sizing:border-box;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{margin-top:18px;padding:10px 18px;border:0;border-radius:6px;background:var(--acc);color:#fff;font-size:15px;cursor:pointer}
.hint,.mut{color:var(--mut);font-size:13px}.err{color:#c0392b;font-weight:600}
a{color:var(--acc)}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px;text-align:left}
</style><main>@@BODY@@</main></html>"""

FORM = """<h1>SDD Generator</h1>
<p class="mut">Deterministic draft from an Automation Anywhere export. Runs locally; nothing is uploaded anywhere.</p>
@@MSG@@
<form class="card" method="post" action="/generate" enctype="multipart/form-data">
<label for="export">Bot export (.zip)</label>
<input id="export" type="file" name="export" accept=".zip" required>
<label for="title">Document title</label>
<input id="title" type="text" name="title" placeholder="Defaults to the zip file name">
<label for="intake">Intake JSON (optional, business sections)</label>
<input id="intake" type="file" name="intake" accept=".json">
<p class="hint">Without intake, sections 1, 2b, 2d and 2e stay as placeholders.</p>
<button type="submit">Generate</button></form>"""


def parse_multipart(headers, body: bytes) -> dict:
    """Return {field: (filename|None, bytes)} from a multipart/form-data body."""
    raw = b"Content-Type: " + headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    msg = BytesParser(policy=email_policy).parsebytes(raw)
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return fields


def generate(zip_bytes: bytes, title: str, intake_bytes: bytes | None, base_name: str) -> dict:
    """Run the pipeline in a temp dir; return {filename: bytes} plus a summary."""
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, "export.zip")
        with open(zpath, "wb") as fh:
            fh.write(zip_bytes)
        ex = load_export(zpath)
        if not ex.bots:
            raise ValueError("No bots found in that file. Upload the zip exported from the Control Room.")
        intake = json.loads(intake_bytes.decode("utf-8-sig")) if intake_bytes else {}
        a = analyse(ex)
        blocks = build(ex, a, intake, title=title or base_name, date=datetime.date.today().isoformat())
        docx_path = os.path.join(tmp, "SDD.docx")
        to_docx(blocks, docx_path)
        with open(docx_path, "rb") as fh:
            docx = fh.read()
    facts = {"entry_points": a["entry"], "cycles": a["cycles"], "tables": a["tables"],
             "rest": a["rest"], "vault_references": a["vault"], "packages": a["packages"],
             "findings": a["hygiene"]}
    return {
        "files": {"SDD.docx": docx, "SDD.md": to_markdown(blocks).encode("utf-8"),
                  "SDD.facts.json": json.dumps(facts, indent=2).encode("utf-8")},
        "summary": {"bots": len(ex.bots), "entry": len(a["entry"]), "tables": len(a["tables"]),
                    "findings": len(a["hygiene"]), "manifest_only": not ex.has_bot_files},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _page(self, inner, code=200):
        self._send(code, PAGE.replace("@@BODY@@", inner).encode("utf-8"))

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path == "/":
            return self._page(FORM.replace("@@MSG@@", ""))
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "download":
            files = RESULTS.get(parts[1], {})
            data = files.get(parts[2])
            if data is not None:
                ctype = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         "md": "text/markdown; charset=utf-8",
                         "json": "application/json"}[parts[2].rsplit(".", 1)[-1]]
                return self._send(200, data, ctype,
                                  {"Content-Disposition": f'attachment; filename="{parts[2]}"'})
        self._page("<p class='err'>Not found.</p>", 404)

    def do_POST(self):
        if self.path != "/generate":
            return self._page("<p class='err'>Not found.</p>", 404)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self._page(FORM.replace("@@MSG@@", "<p class='err'>Upload is empty or larger than 1 GB.</p>"), 400)
        try:
            f = parse_multipart(self.headers, self.rfile.read(length))
            name, zbytes = f.get("export", (None, b""))
            if not zbytes:
                raise ValueError("Choose a .zip export first.")
            title = f.get("title", (None, b""))[1].decode("utf-8").strip()
            intake = f.get("intake", (None, b""))[1] or None
            base = os.path.splitext(name or "export")[0].replace("_", " ")
            out = generate(zbytes, title, intake, base)
        except (ValueError, KeyError, OSError) as e:  # bad zip, bad JSON and similar
            return self._page(FORM.replace("@@MSG@@", f"<p class='err'>{html.escape(str(e) or type(e).__name__)}</p>"), 400)
        except Exception as e:  # noqa: BLE001 - surface any parse failure to the user, not a stack trace
            return self._page(FORM.replace("@@MSG@@", f"<p class='err'>Could not read the export: {html.escape(type(e).__name__)}: {html.escape(str(e))}</p>"), 400)
        rid = secrets.token_urlsafe(8)
        RESULTS[rid] = out["files"]
        s = out["summary"]
        warn = "<p class='err'>Manifest only: variables, steps and SQL need the bot files.</p>" if s["manifest_only"] else ""
        links = "".join(f"<li><a href='/download/{rid}/{n}'>{n}</a></li>" for n in out["files"])
        self._page(f"<h1>Done</h1>{warn}<div class='card'><table>"
                   f"<tr><th>Bots</th><td>{s['bots']}</td></tr><tr><th>Entry points</th><td>{s['entry']}</td></tr>"
                   f"<tr><th>Tables</th><td>{s['tables']}</td></tr>"
                   f"<tr><th>Review findings</th><td>{s['findings']}</td></tr></table>"
                   f"<ul>{links}</ul></div><p><a href='/'>Generate another</a></p>")

    def log_message(self, fmt, *args):  # one terse line per request
        print(f"{self.address_string()} {fmt % args}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sdd_generator.web", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}/"
    print(f"SDD Generator running at {url}  (Ctrl+C to stop)")
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
