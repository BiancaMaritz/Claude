import json
import os
import zipfile

import pytest

from sdd_generator.__main__ import main
from sdd_generator.analyze import analyse, sql_facts
from sdd_generator.document import build
from sdd_generator.loader import load_export
from sdd_generator.render import to_markdown

R = "Automation Anywhere/Bots/"


def var(name, typ="STRING", inp=False, out=False, default=""):
    return {"name": name, "type": typ, "input": inp, "output": out,
            "defaultValue": {"type": typ, "string": default}}


def attrs(**kw):
    return [{"name": k, "value": {"type": "STRING", "expression": v}} for k, v in kw.items()]


def run_task(path):
    return {"commandName": "runTask", "packageName": "TaskBot", "attributes": [
        {"name": "taskbot", "value": {"type": "TASKBOT", "taskbotFile": {
            "type": "FILE", "string": "repository:///" + path.replace(" ", "%20")}}}]}


def step(label, children=()):
    return {"commandName": "step", "packageName": "Step", "children": list(children),
            "attributes": attrs(name=label)}


def sql(q):
    return {"commandName": "insertUpdateDelete", "packageName": "Database", "attributes": attrs(query=q)}


MAIN = R + "Acme/Logic Manager/Main"
WORK = R + "Acme/Work/Worker"
LOG = R + "Task Logging/Log"
LOOP_A = R + "Acme/Work/LoopA"
LOOP_B = R + "Acme/Work/LoopB"


def bot(nodes, variables=()):
    return {"nodes": nodes, "variables": list(variables), "triggers": [], "packages": []}


BOTS = {
    MAIN: bot([step("Connect", [sql("UPDATE $Db$.[dbo].[Jobs] SET [Status] = 'X', [Tries] = 1 WHERE [Id] = 2")]),
               step("Do work", [run_task(WORK)]), run_task(LOG)],
              [var("Environment", inp=True, default="Prod"), var("DbPassword", inp=True, default="hunter2")]),
    WORK: bot([step("Fetch", [{"commandName": "restPost", "packageName": "Rest",
                              "attributes": attrs(uri="$URL$/api/x")}]),
               {"commandName": "messageBox", "packageName": "MessageBox", "attributes": []}]),
    LOG: bot([sql("INSERT INTO [dbo].[RunLog] ([Name],[At]) VALUES ('a', 1)")]),
    LOOP_A: bot([run_task(LOOP_B)]),
    LOOP_B: bot([run_task(LOOP_A)]),
}


@pytest.fixture
def export_dir(tmp_path):
    for p, d in BOTS.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d))
    (tmp_path / "manifest.json").write_text(json.dumps({
        "files": [{"path": p.replace("/", "\\"), "contentType": "application/vnd.aa.taskbot",
                   "scannedDependencies": [], "description": ""} for p in BOTS],
        "packages": [{"name": "String", "version": "1"}, {"name": "String", "version": "2"}],
        "globalValues": []}))
    return tmp_path


def test_sql_facts():
    ops, cols = sql_facts("UPDATE $Db$.[dbo].[Jobs] SET [Status]='a', [Tries]=1 WHERE Id=2")
    assert ops == [("Jobs", "update")] and cols["Jobs"] == {"Status", "Tries"}
    ops, cols = sql_facts("INSERT INTO [dbo].[RunLog] ([Name],[At]) VALUES ('a',1)")
    assert cols["RunLog"] == {"Name", "At"}
    assert sql_facts("send a note from the server")[0] == []


def test_analysis(export_dir):
    ex = load_export(str(export_dir))
    a = analyse(ex)
    assert len(ex.bots) == 5
    assert a["ref"][MAIN] == "Main Bot 1"
    assert a["ref"][LOG] == "Common Bot 1"
    assert MAIN in a["entry"] and WORK not in a["entry"]
    assert [sorted(c) for c in a["cycles"]] == [sorted([LOOP_A, LOOP_B])]
    assert set(a["entry"]) >= {LOOP_A, LOOP_B}  # a cycle nobody else calls is still an entry group
    assert set(a["tables"]) == {"Jobs", "RunLog"}
    assert a["rest"][0]["method"] == "POST" and a["rest"][0]["uri"] == "{URL}/api/x"
    cats = {h[0] for h in a["hygiene"]}
    assert {"Circular call", "Unattended risk", "Package versions"} <= cats


def test_zip_matches_directory(export_dir, tmp_path):
    z = tmp_path.parent / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for root, _, files in os.walk(export_dir):
            for f in files:
                full = os.path.join(root, f)
                zf.write(full, os.path.relpath(full, export_dir))
    assert set(load_export(str(z)).bots) == set(load_export(str(export_dir)).bots)


def test_secret_defaults_masked_and_intake(export_dir):
    ex = load_export(str(export_dir))
    md = to_markdown(build(ex, analyse(ex), {"process_description": "Does things.",
                                            "assumptions": ["A1"]}, title="T", date="2026-01-01"))
    assert "hunter2" not in md and "[masked]" in md
    assert "Does things." in md and "- A1" in md
    assert "[TO BE COMPLETED]: functional requirements." in md


def test_cli_outputs(export_dir, tmp_path):
    out = tmp_path / "out"
    assert main([str(export_dir), "--out", str(out), "--date", "2026-01-01"]) == 0
    assert (out / "SDD.md").read_text().startswith("# ")
    from docx import Document
    assert len(Document(str(out / "SDD.docx")).tables) > 5
    assert json.loads((out / "SDD.facts.json").read_text())["entry_points"]


def test_cli_with_intake_stakeholders(export_dir, tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps({"stakeholders": [
        {"team": "Team A", "name": "N", "role": "R", "email": "e", "approval": "High"}]}))
    out = tmp_path / "o"
    assert main([str(export_dir), "--out", str(out), "--intake", str(intake)]) == 0
    from docx import Document
    for t in Document(str(out / "SDD.docx")).tables:
        assert len({len(r.cells) for r in t.rows}) == 1


def zip_of(export_dir, skip=()):
    import io
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        for root, _, files in os.walk(export_dir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, export_dir)
                if rel.replace(os.sep, "/") not in skip:
                    zf.write(full, rel)
    return z.getvalue()


def test_store_add_vs_overwrite(export_dir, tmp_path):
    from sdd_generator.store import Store, deep_merge
    from sdd_generator.web import classify_uploads

    store = Store(str(tmp_path / "docs"))
    full = classify_uploads([("full.zip", zip_of(export_dir))])[0]
    part = classify_uploads([("part.zip", zip_of(export_dir, skip=("Automation Anywhere/Bots/Task Logging/Log", "manifest.json")))])[0]
    r = store.load("d", "add", part, [], ["part.zip"])
    assert (r["added"], r["created"]) == (4, True)
    r = store.load("d", "add", full, [], ["full.zip"])
    assert (r["added"], r["unchanged"], r["removed"]) == (1, 4, 0)
    assert len(store.get("d")["export"]["bots"]) == 5
    r = store.load("d", "overwrite", part, [], ["part.zip"])
    assert (r["removed"], r["added"], r["unchanged"]) == (1, 0, 4)
    assert len(store.get("d")["export"]["bots"]) == 4
    assert deep_merge({"a": [1], "b": 1}, {"a": [1, 2], "b": 2}) == {"a": [1, 2], "b": 2}


def test_add_keeps_logic_when_manifest_only_arrives(export_dir, tmp_path):
    from sdd_generator.store import Store, export_from_dict
    from sdd_generator.web import classify_uploads

    store = Store(str(tmp_path / "docs"))
    store.load("d", "add", classify_uploads([("a.zip", zip_of(export_dir))])[0], [], ["a.zip"])
    only_manifest = zip_of(export_dir, skip=tuple(BOTS))
    store.load("d", "add", classify_uploads([("m.zip", only_manifest)])[0], [], ["m.zip"])
    ex = export_from_dict(store.get("d")["export"])
    assert all(b.data is not None for b in ex.bots.values())


def test_reload_same_content_is_detected(export_dir, tmp_path):
    from sdd_generator.store import Store
    from sdd_generator.web import classify

    store = Store(str(tmp_path / "docs"))
    z = zip_of(export_dir)
    ex, it, names, src = classify([("v1.zip", z)], [])
    r1 = store.load("d", "add", ex, it, names, sources=src)
    assert not r1["noop"] and r1["already_loaded"] == []
    n_hist = len(store.get("d")["history"])
    # same content again, even under another file name: detected, nothing written
    ex, it, names, src = classify([("renamed copy.zip", z)], [])
    r2 = store.load("d", "add", ex, it, names, sources=src)
    assert r2["noop"] and r2["unchanged"] == 5 and r2["already_loaded"] == ["renamed copy.zip"]
    assert len(store.get("d")["history"]) == n_hist
    # overwrite with identical content is also a no-op
    assert store.load("d", "overwrite", ex, it, names, sources=src)["noop"]


def test_reload_older_version_still_applies(export_dir, tmp_path):
    from sdd_generator.store import Store
    from sdd_generator.web import classify

    store = Store(str(tmp_path / "docs"))
    v1 = zip_of(export_dir)
    changed = json.loads((export_dir / WORK).read_text())
    changed["nodes"].append(step("New step"))
    (export_dir / WORK).write_text(json.dumps(changed))
    v2 = zip_of(export_dir)
    for name, z in (("v1.zip", v1), ("v2.zip", v2), ("v1.zip", v1)):
        ex, it, names, src = classify([(name, z)], [])
        r = store.load("d", "add", ex, it, names, sources=src)
    # v1 was loaded before, but the document now holds v2's Worker, so reloading v1 must revert it
    assert r["already_loaded"] == ["v1.zip"] and not r["noop"] and r["updated"] == 1
    assert [n.rsplit("/", 1)[-1] for n in r["names"]["updated"]] == ["Worker"]


def test_zip_fingerprint_ignores_name_and_location(export_dir, tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "sub" / "b.zip"
    b.parent.mkdir()
    data = zip_of(export_dir)
    a.write_bytes(data)
    b.write_bytes(data)
    assert load_export(str(a)).fingerprint == load_export(str(b)).fingerprint != ""
    (export_dir / "Automation Anywhere/Bots/lib.jar").write_bytes(b"x")
    b.write_bytes(zip_of(export_dir, skip=("a.zip", "sub/b.zip")))
    assert load_export(str(a)).fingerprint != load_export(str(b)).fingerprint  # an added file changes it


def test_binaries_are_never_read(export_dir, tmp_path, monkeypatch):
    (export_dir / "Automation Anywhere/Bots/huge.jar").write_bytes(b"\0" * 5_000_000)
    z = tmp_path / "x.zip"
    z.write_bytes(zip_of(export_dir))
    read = []
    orig = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda self, n, *a, **k: (read.append(getattr(n, "filename", n)), orig(self, n, *a, **k))[1])
    assert len(load_export(str(z)).bots) == 5
    assert not any(str(n).endswith(".jar") for n in read)


class Client:
    """Drives the real HTTP server the way the browser does: stage files, then load."""

    def __init__(self, tmp_path):
        import threading
        from http.server import ThreadingHTTPServer

        from sdd_generator.web import App, make_handler

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.srv.server_address[1]
        self.app = App(str(tmp_path / "docs"))
        self.srv.RequestHandlerClass = make_handler(self.app, self.port)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def call(self, method, url, body=None, headers=None):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self.base + url, body, headers or {}, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stage(self, name, data):
        import urllib.parse

        code, body = self.call("POST", "/api/stage?name=" + urllib.parse.quote(name), data)
        return code, json.loads(body)

    def load(self, doc, files=(), paths="", mode="add", title=""):
        tokens = []
        for name, data in files:
            code, j = self.stage(name, data)
            if code != 200:
                return code, j
            tokens.append(j["token"])
        code, body = self.call("POST", "/api/load", json.dumps(
            {"doc": doc, "mode": mode, "title": title, "paths": paths, "staged": tokens}).encode(),
            {"Content-Type": "application/json"})
        return code, json.loads(body)


@pytest.fixture
def client(tmp_path):
    c = Client(tmp_path)
    yield c
    c.srv.shutdown()


def test_web_api(export_dir, client):
    assert b"SDD Generator" in client.call("GET", "/")[1]
    code, j = client.load("My Doc", [("a b.zip", zip_of(export_dir)), ("intake.json", b'{"assumptions": ["A1"]}')], title="T")
    assert code == 200 and j["report"]["added"] == 5 and j["report"]["intake"] == 1
    d = json.loads(client.call("GET", "/api/doc?name=My%20Doc")[1])
    assert d["title"] == "T" and "A1" in d["html"] and d["menu"][0]["text"].startswith("1.")
    assert client.call("GET", "/download/My%20Doc/docx")[1][:2] == b"PK"
    assert b"# T" in client.call("GET", "/download/My%20Doc/md")[1]
    for bad in ([("x.txt", b"hi")], [("bad.zip", b"not a zip")], [("m.json", b'{"files": []}')]):
        code, j = client.load("My Doc", bad)
        assert code == 400 and j["error"]
    assert client.load("../evil", [("a.zip", zip_of(export_dir))])[0] == 400  # path traversal in the name
    code, _ = client.call("POST", "/api/delete", b'{"name": "My Doc"}', {"Origin": "http://evil.example"})
    assert code == 403  # cross-site request is refused
    assert json.loads(client.call("GET", "/api/docs")[1])[0]["bots"] == 5
    assert not os.listdir(client.app.staging)  # staged uploads are removed after the load


def test_large_upload_streams_to_disk(export_dir, client):
    # An export that carries a 150 MB jar, as real ones do. It is uploaded in chunks to disk,
    # and the loader then skips the jar without reading it.
    import io

    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for root, _, files in os.walk(export_dir):
            for f in files:
                full = os.path.join(root, f)
                zf.write(full, os.path.relpath(full, export_dir))
        zf.writestr("packages/big.jar", b"\0" * (150 * 1024 * 1024))
    code, j = client.stage("big.zip", z.getvalue())
    assert code == 200 and j["size"] > 150 * 1024 * 1024
    assert os.path.getsize(client.app._staged[j["token"]][1]) == j["size"]
    code, body = client.call("POST", "/api/load", json.dumps({"doc": "L", "staged": [j["token"]]}).encode(),
                             {"Content-Type": "application/json"})
    assert code == 200 and json.loads(body)["report"]["added"] == 5


def test_web_load_from_path(export_dir, tmp_path, client):
    z = tmp_path / "big export.zip"
    z.write_bytes(zip_of(export_dir, skip=("big export.zip",)))
    assert client.load("P", paths=f'"{z}"')[1]["report"]["added"] == 5   # Windows "Copy as path" adds quotes
    assert client.load("P", paths=str(z))[1]["report"]["noop"] is True    # same file again
    assert client.load("P", paths=str(export_dir))[1]["report"]["already_loaded"] == []  # a folder is another source
    assert "not found" in client.load("P", paths=str(tmp_path / "nope.zip"))[1]["error"]
    (tmp_path / "notes.txt").write_text("hi")
    assert "give a .zip" in client.load("P", paths=str(tmp_path / "notes.txt"))[1]["error"]


def test_page_shows_version_and_port_clash_is_explained(client, capsys, tmp_path):
    import socket

    from sdd_generator import __version__
    from sdd_generator.web import main

    assert f"Version {__version__}".encode() in client.call("GET", "/")[1]
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    try:
        assert main(["--port", str(held.getsockname()[1]), "--no-browser", "--data-dir", str(tmp_path / "x" / "d")]) == 1
    finally:
        held.close()
    assert "older copy" in capsys.readouterr().out
    assert not (tmp_path / "x").exists()  # a failed start must not touch a running copy's files


# ------------------------------------------------------------ documentation ---

def make_sdd_pdf(path, process="The bot logs in and downloads the rebate files."):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()["Normal"]
    grid = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)])

    def table(rows):
        t = Table([[Paragraph(c, st) for c in r] for r in rows], colWidths=[130, 330])
        t.setStyle(grid)
        return t

    story = [Paragraph("Contents", st), Paragraph("1. Business Requirements 2", st),
             Paragraph("a. Process Description 2", st), Paragraph("1. Business Requirements", st),
             Paragraph("a. Process Description", st), Paragraph(process, st), Spacer(1, 12),
             Paragraph("b. Priority (MoSCoW)", st),
             table([["Must Have", "&#9679; Download all files"], ["Should Have", "&#9679; Summary table"],
                    ["Could Have", ""], ["Won't Have", "&#9679; Scanned documents"]]), Spacer(1, 12),
             Paragraph("c. Assumptions", st), Paragraph("- All portals look the same.", st), Spacer(1, 12),
             Paragraph("d. Functional Requirements", st),
             table([["FR1", "Download all claims for a week."], ["FR2", "Save files in SharePoint."]]), Spacer(1, 12),
             Paragraph("3. Technical Design", st), Paragraph("a. Applications &amp; Systems", st),
             table([["APP1", "Excel", "Client/Server", "Full", "SVC_bot@example.com"]]), Spacer(1, 12),
             Paragraph("b. Bot Components", st),
             table([["Main Bot 1", "Logic Manager"], ["Sub Bot 1", "Shoprite Downloader Bot"]])]
    SimpleDocTemplate(path).build(story)


def test_parse_pdf_sections_and_tables(tmp_path):
    from sdd_generator.docparse import parse_document

    pdf = tmp_path / "Old SDD.pdf"
    make_sdd_pdf(str(pdf))
    d = parse_document(str(pdf))
    s = d["sections"]
    assert d["name"] == "Old SDD.pdf" and len(d["fp"]) == 64
    assert s["process_description"]["blocks"][0] == ("p", "The bot logs in and downloads the rebate files.")
    assert {"moscow", "functional_requirements", "applications", "bot_components"} <= set(s)
    moscow = s["moscow"]["blocks"][0][1]
    assert moscow[0][0] == "Must Have" and "Download all files" in moscow[0][1]
    assert s["functional_requirements"]["blocks"][0][1][1] == ["FR2", "Save files in SharePoint."]
    assert s["assumptions"]["blocks"][0] == ("ul", ["All portals look the same."])


def test_parse_docx_and_md_and_errors(tmp_path):
    import docx

    from sdd_generator.docparse import parse_document

    d = docx.Document()
    d.add_heading("1. Business Requirements", 1)
    d.add_heading("a. Process Description", 2)
    d.add_paragraph("Does the thing.")
    d.add_heading("d. Functional Requirements", 2)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "FR1", "Do it."
    d.save(tmp_path / "x.docx")
    s = parse_document(str(tmp_path / "x.docx"))["sections"]
    assert s["process_description"]["blocks"] == [("p", "Does the thing.")]
    assert s["functional_requirements"]["blocks"][0][1] == [["FR1", "Do it."]]
    (tmp_path / "x.md").write_text("## a. Process Description\n\nHello.\n\n## d. Functional Requirements\n\n| a | b |\n| --- | --- |\n| FR1 | Go |\n")
    assert parse_document(str(tmp_path / "x.md"))["sections"]["functional_requirements"]["blocks"][0][1][-1] == ["FR1", "Go"]
    (tmp_path / "bad.md").write_text("nothing useful here")
    with pytest.raises(ValueError, match="no SDD sections"):
        parse_document(str(tmp_path / "bad.md"))
    (tmp_path / "x.exe").write_bytes(b"MZ")
    with pytest.raises(ValueError, match="must be"):
        parse_document(str(tmp_path / "x.exe"))


def _doc_from(tmp_path, process="Documented description."):
    from sdd_generator.docparse import parse_document

    p = tmp_path / "doc.pdf"
    make_sdd_pdf(str(p), process)
    return parse_document(str(p), "Old SDD.pdf")


def test_documentation_fills_gaps_but_code_and_intake_win(export_dir, tmp_path):
    ex = load_export(str(export_dir))
    a = analyse(ex)
    doc = _doc_from(tmp_path)
    plain = to_markdown(build(ex, a, {}, title="T", date="2026-01-01"))
    withdoc = to_markdown(build(ex, a, {}, title="T", date="2026-01-01", docs=[doc]))
    assert "[TO BE COMPLETED]: business description" in plain
    assert "Documented description." in withdoc and "*From documentation: Old SDD.pdf, page 1*" in withdoc
    assert "[TO BE COMPLETED]: business description" not in withdoc
    # intake the user typed beats documentation
    over = to_markdown(build(ex, a, {"process_description": "Typed by me."}, docs=[doc], title="T", date="x"))
    assert "Typed by me." in over and "Documented description." not in over
    # code stays the source of truth: its bot table is untouched and documented-only bots are listed apart
    assert "Main Bot 1" in withdoc and "Shoprite Downloader Bot (Old SDD.pdf)" in withdoc
    assert "0 of 2 bot names in documentation match" in withdoc
    # Appendix G says what was read and used
    assert "Appendix G. Documentation Coverage" in withdoc and "moscow" in withdoc


def test_documentation_only_document_renders(tmp_path):
    from sdd_generator.loader import Export

    ex = Export(source="x")
    md = to_markdown(build(ex, analyse(ex), {}, title="Docs only", date="x", docs=[_doc_from(tmp_path)]))
    assert "Documented description." in md


def test_store_documentation_lifecycle_and_backups(export_dir, tmp_path):
    from sdd_generator.store import Store
    from sdd_generator.web import classify

    store = Store(str(tmp_path / "docs"))
    doc = _doc_from(tmp_path)
    r = store.load_docs("L", "add", [doc], [{"assumptions": ["A"]}])
    assert r["added"] == ["Old SDD.pdf"] and not r["noop"]
    assert store.load_docs("L", "add", [doc], [])["noop"]                      # same file again: nothing changes
    newer = dict(doc, fp="f" * 64)
    assert store.load_docs("L", "add", [newer], [])["updated"] == ["Old SDD.pdf"]  # same name, new content
    assert len(store.get("L")["documentation"]) == 1
    # code load and overwrite never touch documentation or intake
    ex, it, names, src = classify([("a.zip", zip_of(export_dir))], [])
    store.load("L", "add", ex, it, names, sources=src)
    store.load("L", "overwrite", ex, it, names, sources=src)
    got = store.get("L")
    assert len(got["documentation"]) == 1 and got["intake"] == {"assumptions": ["A"]} and len(got["export"]["bots"]) == 5
    # documentation overwrite never touches code
    store.load_docs("L", "overwrite", [], [])
    got = store.get("L")
    assert got["documentation"] == [] and got["intake"] == {} and len(got["export"]["bots"]) == 5
    # every save keeps the previous version; deleting leaves a copy too
    backups = os.listdir(tmp_path / "docs" / "backups" / "L")
    assert len(backups) >= 3
    store.delete("L")
    assert store.get("L") is None and len(os.listdir(tmp_path / "docs" / "backups" / "L")) == len(backups) + 1
    assert store.remove_doc("L", "x") is False


def test_web_documentation_api(export_dir, tmp_path, client):
    pdf = tmp_path / "Old SDD.pdf"
    make_sdd_pdf(str(pdf))

    def docs_load(paths="", staged=(), mode="add"):
        code, body = client.call("POST", "/api/documentation/load", json.dumps(
            {"doc": "W", "mode": mode, "paths": paths, "staged": list(staged)}).encode(),
            {"Content-Type": "application/json"})
        return code, json.loads(body)

    code, tok = client.stage("Old SDD.pdf", pdf.read_bytes())
    assert code == 200
    code, j = docs_load(staged=[tok["token"]])
    assert code == 200 and j["report"]["added"] == ["Old SDD.pdf"]
    assert docs_load(paths=str(pdf))[1]["report"]["noop"] is True          # same content by path
    assert docs_load(paths=str(tmp_path))[0] == 200                         # a folder works too
    code, j = docs_load(paths=str(tmp_path / "missing.pdf"))
    assert code == 400 and "not found" in j["error"]
    client.load("W", [("a.zip", zip_of(export_dir, skip=("Old SDD.pdf",)))])
    d = json.loads(client.call("GET", "/api/doc?name=W")[1])
    assert d["bots"] == 5 and d["documentation"][0]["name"] == "Old SDD.pdf"
    assert any(m["docs"] for m in d["menu"]) and "From documentation" in d["html"]
    fp = d["documentation"][0]["fp"]
    code, body = client.call("POST", "/api/documentation/remove", json.dumps({"doc": "W", "fp": fp}).encode(),
                             {"Content-Type": "application/json"})
    assert json.loads(body)["ok"] is True
    assert json.loads(client.call("GET", "/api/doc?name=W")[1])["documentation"] == []


def test_legacy_documents_are_copied_once_and_never_overwrite(tmp_path, monkeypatch):
    import sdd_generator.web as web

    pkg = tmp_path / "code" / "sdd_generator"
    pkg.mkdir(parents=True)
    legacy = tmp_path / "code" / "data" / "documents"
    legacy.mkdir(parents=True)
    (legacy / "Lucy.sdd.json").write_text('{"old": true}')
    monkeypatch.setattr(web, "__file__", str(pkg / "web.py"))
    new = tmp_path / "keep" / "documents"
    assert web.migrate_legacy(str(new)) == 1 and (new / "Lucy.sdd.json").read_text() == '{"old": true}'
    (new / "Lucy.sdd.json").write_text('{"new": true}')
    assert web.migrate_legacy(str(new)) == 0                       # data already there: nothing is touched
    assert (new / "Lucy.sdd.json").read_text() == '{"new": true}'
    assert web.migrate_legacy(str(legacy)) == 0                    # same folder: nothing to do
