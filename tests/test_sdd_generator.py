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


def test_documents_survive_a_restart_and_a_moved_install(tmp_path):
    from sdd_generator.store import Store, find_stored_documents

    old = tmp_path / "Dev" / "sdd-generator" / "sdd-generator" / "data" / "documents"   # nested by an unzip
    s_old = Store(str(old))
    s_old.load_docs("Lucy", "add", [_doc_from(tmp_path)], [])
    assert Store(str(old)).get("Lucy")["documentation"]                  # a new process sees it: it is on disk
    new = tmp_path / "Dev" / "sdd-generator" / "data" / "documents"       # where the new install keeps data
    s_new = Store(str(new))
    found = find_stored_documents(str(new), [str(tmp_path / "Dev" / "sdd-generator")])
    assert found == [(str(old), ["Lucy"])]
    assert s_new.import_from(str(old), rename=False) == {"Lucy": "Lucy"}
    assert s_new.import_from(str(old), rename=False) == {}               # not duplicated
    assert s_new.import_from(str(old)) == {"Lucy": "Lucy recovered-1"}   # manual import never overwrites
    assert s_new.get("Lucy")["documentation"] and s_new.get("Lucy recovered-1")["documentation"]
    with pytest.raises(ValueError):
        s_new.import_from(str(new))


def test_web_recover_endpoint(tmp_path, client):
    from sdd_generator.store import Store

    other = tmp_path / "elsewhere"
    Store(str(other)).load_docs("Lucy", "add", [_doc_from(tmp_path)], [])
    code, body = client.call("POST", "/api/recover", json.dumps({"folder": str(other)}).encode(),
                             {"Content-Type": "application/json"})
    assert code == 200 and json.loads(body)["imported"] == {"Lucy": "Lucy"}
    assert json.loads(client.call("GET", "/api/docs")[1])[0]["name"] == "Lucy"
    code, body = client.call("POST", "/api/recover", json.dumps({"folder": str(tmp_path / "nope")}).encode(),
                             {"Content-Type": "application/json"})
    assert code == 400


# ------------------------------------------- tables, videos, graphs, new layout ---

def png_bytes():
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = zlib.compress(b"\x00\xff\x00\x00")  # one red pixel
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def test_table_analysis_csv_xlsx_and_diff(tmp_path):
    import openpyxl

    from sdd_generator.tables import analyze_file, diff_table

    csv_path = tmp_path / "Rebates.csv"
    csv_path.write_text("Id;Code;Amount;Day;Note\n1;007;12.5;2026-01-02;a\n2;008;3;03/01/2026;\n", encoding="cp1252")
    (t,) = analyze_file(str(csv_path), "Rebates.csv")
    types = {c["name"]: c["type"] for c in t["columns"]}
    assert t["name"] == "Rebates" and t["rows"] == 2
    assert types == {"Id": "int", "Code": "text", "Amount": "float", "Day": "date", "Note": "text"}
    assert types["Code"] == "text"  # 007 is an identifier, not a number
    assert [c["nonnull"] for c in t["columns"]][-1] == 1
    assert "12.5" not in repr(t)    # values are never kept
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Doc", "Total"])
    ws.append(["A", 1.5])
    wb.create_sheet("Lines").append(["Item"])
    wb.save(tmp_path / "Book.xlsx")
    sheets = {x["name"]: x["rows"] for x in analyze_file(str(tmp_path / "Book.xlsx"), "Book.xlsx")}
    assert sheets == {"Summary": 1, "Lines": 0}  # one table per sheet; a header-only sheet is an empty table
    csv2 = tmp_path / "Rebates2.csv"
    csv2.write_text("Id,Amount,Extra\n1,2,x\n")
    (t2,) = analyze_file(str(csv2), "Rebates.csv")
    d = diff_table(t, t2)
    assert d["added"] == ["Extra"] and set(d["removed"]) == {"Code", "Day", "Note"} and d["rows"] == [2, 1]
    with pytest.raises(ValueError):
        analyze_file(str(tmp_path / "x.txt"), "x.txt")


def test_transcripts(tmp_path):
    from sdd_generator.media import parse_transcript, transcript_paragraphs

    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n2\n00:01:05,500 --> 00:01:07,000\nSecond <i>line</i>.\n")
    t = parse_transcript(str(srt), "a.srt")
    assert [c["t"] for c in t["cues"]] == [1.0, 65.5] and t["cues"][1]["text"] == "Second line."
    assert transcript_paragraphs(t["cues"], limit=5)[1].startswith("[1:05] Second")
    txt = tmp_path / "b.txt"
    txt.write_text("First paragraph.\n\nSecond one.")
    assert [c["text"] for c in parse_transcript(str(txt), "b.txt")["cues"]] == ["First paragraph.", "Second one."]
    (tmp_path / "e.txt").write_text("   ")
    with pytest.raises(ValueError):
        parse_transcript(str(tmp_path / "e.txt"), "e.txt")


def test_store_tables_add_replace_overwrite(tmp_path):
    from sdd_generator.store import Store

    def tbl(name, cols, rows, fp):
        return {"name": name, "source": name + ".csv", "rows": rows, "fp": fp,
                "columns": [{"name": c, "type": "text", "nonnull": rows} for c in cols]}

    store = Store(str(tmp_path / "docs"))
    r = store.load_tables("P", "add", [tbl("A", ["x"], 1, "f1"), tbl("B", ["y"], 2, "f2")], ["a.csv"])
    assert r["added"] == ["A", "B"]
    r = store.load_tables("P", "add", [tbl("A", ["x"], 1, "f1")], ["a.csv"])
    assert r["noop"] and r["unchanged"] == ["A"]
    r = store.load_tables("P", "add", [tbl("A", ["x", "z"], 5, "f3")], ["a.csv"])
    assert r["replaced"] == ["A"] and r["changes"]["A"]["added"] == ["z"] and r["changes"]["A"]["rows"] == [1, 5]
    assert {t["name"] for t in store.get("P")["tables"]} == {"A", "B"}      # B was not in this load: kept
    r = store.load_tables("P", "overwrite", [tbl("C", ["q"], 1, "f4")], ["c.csv"])
    assert r["removed"] == ["A", "B"] and [t["name"] for t in store.get("P")["tables"]] == ["C"]
    assert store.remove_table("P", "C") and not store.remove_table("P", "C")


def test_sdd_places_graphs_videos_and_tables(export_dir, tmp_path):
    ex = load_export(str(export_dir))
    a = analyse(ex)
    media = tmp_path / "media"
    media.mkdir()
    (media / "g1.png").write_bytes(png_bytes())
    cues = [{"t": 3.0, "text": "The bot starts by reading mail."}]
    extra = {"project": "P", "media_dir": str(media),
             "graphs": [{"id": "g1", "name": "erd.png", "file": "g1.png", "section": "erd", "caption": "The ERD"},
                        {"id": "g2", "name": "loose.png", "file": "g1.png", "section": "appendix", "caption": ""}],
             "videos": [{"id": "v1", "name": "walk.mp4", "size": 2048, "section": "process_description",
                         "transcript": {"name": "walk.srt", "cues": cues, "chars": 30}},
                        {"id": "v2", "name": "silent.mp4", "size": 10, "section": "appendix", "transcript": None}],
             "tables": [{"name": "Jobs", "source": "Jobs.csv", "rows": 1200, "fp": "x",
                         "columns": [{"name": "Status", "type": "text", "nonnull": 1200}]},
                        {"name": "Orphan", "source": "Orphan.csv", "rows": 3, "fp": "y", "columns": []}]}
    blocks = build(ex, a, {}, title="T", date="x", extra=extra)
    md = to_markdown(blocks)
    erd = md[md.index("### c. ERD Design"):md.index("### d. Business Rules")]
    assert "![The ERD](media/g1.png)" in erd and "From graph: erd.png" in erd
    assert "Tables from current exports" in erd and "1,200" in erd
    assert "written by the code but not in the exported table: Tries" in erd          # code writes Jobs.Tries
    assert "Exported but not referenced by SQL in the code: Orphan" in erd
    assert "RunLog" in erd and "no export was loaded for: RunLog" in erd
    pd = md[md.index("### a. Process Description"):md.index("### b. Priority")]
    assert "From walkthrough video: walk.mp4" in pd and "[0:03] The bot starts by reading mail." in pd
    assert "Appendix H. Videos and Graphs" in md and "silent.mp4" in md and "adds nothing" in md
    from sdd_generator.render import to_docx, to_html

    to_docx(blocks, str(tmp_path / "o.docx"))
    import docx

    assert len(docx.Document(str(tmp_path / "o.docx")).inline_shapes) == 2
    html = to_html(blocks)[1]
    assert '<img src="/media/P/g1"' in html


def test_new_endpoints_end_to_end(export_dir, tmp_path, client):
    import urllib.parse

    def post(path, obj):
        code, body = client.call("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
        return code, json.loads(body)

    # a project nobody has saved yet is "fresh", with the section list the dropdowns need
    code, body = client.call("GET", "/api/project?name=Fresh")
    j = json.loads(body)
    assert code == 200 and j["fresh"] and len(j["sections"]) > 10 and j["code"]["bots"] == 0
    assert client.call("GET", "/api/project?name=../x")[0] == 404
    client.load("P", [("a.zip", zip_of(export_dir))])

    # video by upload: stored, summarised, served with Range, transcript attached, then removed
    movie = b"0123456789" * 1000
    code, tok = client.stage("walk.mp4", movie)
    code, j = post("/api/video/add", {"doc": "P", "staged": [tok["token"]], "section": "process_description"})
    assert code == 200 and j["added"] == ["walk.mp4"]
    info = json.loads(client.call("GET", "/api/project?name=P")[1])
    v = info["videos"][0]
    assert v["has_file"] and v["transcript"] is None and "path" not in v and v["kind"] == "file"
    code, body = client.call("GET", v["url"], headers={"Range": "bytes=10-19"})
    assert code == 206 and body == b"0123456789"
    assert client.call("GET", v["url"])[1] == movie
    assert client.call("GET", "/media/P/zzzzzzzzzzzz")[0] == 404
    code, tok = client.stage("walk.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHello from the video.\n")
    assert post("/api/video/transcript", {"doc": "P", "id": v["id"], "staged": [tok["token"]]})[0] == 200
    assert json.loads(client.call("GET", "/api/project?name=P")[1])["videos"][0]["transcript"]["chars"] > 10
    sdd = json.loads(client.call("GET", "/api/doc?name=P")[1])
    assert "Hello from the video." in sdd["html"]
    assert post("/api/item/update", {"doc": "P", "kind": "videos", "id": v["id"], "section": "bogus"})[0] == 400
    # a video by path is linked, not copied
    vp = tmp_path / "linked.mp4"
    vp.write_bytes(b"x" * 50)
    assert post("/api/video/add", {"doc": "P", "paths": str(vp), "section": "appendix"})[0] == 200
    linked = [x for x in json.loads(client.call("GET", "/api/project?name=P")[1])["videos"] if x["kind"] == "path"][0]
    assert client.call("GET", linked["url"])[1] == b"x" * 50
    stored = os.listdir(client.app.store.media_dir("P"))
    assert len(stored) == 1                                                  # only the uploaded one was copied
    assert post("/api/item/remove", {"doc": "P", "kind": "videos", "id": v["id"]})[1]["ok"]
    assert os.listdir(client.app.store.media_dir("P")) == []                 # its stored file is deleted

    # graphs: wrong type refused, image placed in the SDD, Markdown ships as a bundle
    code, tok = client.stage("flow.png", png_bytes())
    assert post("/api/graph/add", {"doc": "P", "staged": [tok["token"]], "section": "erd", "caption": "Flow"})[0] == 200
    code, tok = client.stage("bad.mp4", b"x")
    assert post("/api/graph/add", {"doc": "P", "staged": [tok["token"]], "section": "erd"})[0] == 400
    sdd = json.loads(client.call("GET", "/api/doc?name=P")[1])
    assert '<img src="/media/P/' in sdd["html"]
    code, body = client.call("GET", "/download/P/md")
    assert body[:2] == b"PK"
    with zipfile.ZipFile(__import__("io").BytesIO(body)) as z:
        assert "SDD.md" in z.namelist() and any(n.startswith("media/") for n in z.namelist())

    # tables: add, an unchanged reload, then a changed export replaces and reports the difference
    code, tok = client.stage("Jobs.csv", b"Id,Status\n1,a\n2,b\n")
    code, j = post("/api/tables/load", {"doc": "P", "mode": "add", "staged": [tok["token"]]})
    assert j["report"]["added"] == ["Jobs"]
    code, tok = client.stage("Jobs.csv", b"Id,Status\n1,a\n2,b\n")
    assert post("/api/tables/load", {"doc": "P", "mode": "add", "staged": [tok["token"]]})[1]["report"]["noop"]
    code, tok = client.stage("Jobs.csv", b"Id,Status,Tries\n1,a,1\n")
    r = post("/api/tables/load", {"doc": "P", "mode": "add", "staged": [tok["token"]]})[1]["report"]
    assert r["replaced"] == ["Jobs"] and r["changes"]["Jobs"]["added"] == ["Tries"]
    # replacing all code keeps every other input
    client.load("P", [("a.zip", zip_of(export_dir))], mode="overwrite")
    kept = client.app.store.get("P")
    assert kept["tables"] and kept["graphs"] and len(kept["videos"]) == 1
    assert post("/api/tables/remove", {"doc": "P", "table": "Jobs"})[1]["ok"]


# ------------------------------------------------------------------ analysis ---

def _doc_with(process_rows, bot_rows):
    return {"name": "Old.pdf", "fp": "f" * 64, "kind": "pdf", "pages": 3, "sections": {
        "process_flow": {"page": 5, "blocks": [("table", process_rows)]},
        "bot_components": {"page": 7, "blocks": [("table", bot_rows)]}}}


def _code(tmp_path, bots):
    root = tmp_path / "code"
    for p, d in bots.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d))
    return load_export(str(root))


def test_analysis_bots_to_provide_come_only_from_the_code(tmp_path):
    from sdd_generator.gaps import analyse_gaps

    bots = {
        R + "Acme/Logic Manager/Orchestrator": bot([step("Run", [run_task(R + "Acme/Common/Shoprite Login"),
                                                                  run_task(R + "Acme/Common/Missing Helper")])]),
        R + "Acme/Common/Shoprite Login": bot([step("Login to the Shoprite portal")]),
        R + "Acme/Work/Rebate Downloader": bot([step("Download rebate claims for the week")]),
        R + "Acme/Work/Filler": bot([step("Initialize variables")]),
        R + "Acme/Work/Filler2": bot([step("Initialize variables")]),
        R + "Acme/Work/Filler3": bot([step("Initialize variables")]),
    }
    ex = _code(tmp_path, bots)
    doc = _doc_with(
        [["Scenario: SC1", "", ""], ["Step", "Screen Shot", "Description"],
         ["1", "", "\u2022 Login to the Shoprite Supplier Portal."],
         ["2", "", "\u2022 Download the rebate claims for the week."],
         ["3", "", "\u2022 Send the claims to the Zorblax gateway."]],
        [["Bot", "Description"], ["Main Bot 1", "Logic Manager"], ["Sub Bot 1", "Shoprite Login Bot"],
         ["Sub Bot 2", "Zorblax Sender Bot"]])
    tables = [{"name": "Orphan", "source": "Orphan.csv", "rows": 1, "fp": "x", "columns": []}]
    res = analyse_gaps(ex, analyse(ex), [doc], tables)
    # what the code shows missing is a bot to provide
    assert [(e["name"], e["confidence"]) for e in res["bots"]] == [("Missing Helper", "Certain")]
    assert "Called by Orchestrator" in res["bots"][0]["reasons"][0]
    assert res["bots"][0]["where"] == ["Bots/Acme/Common/Missing Helper"]
    # a bot only the documentation knows is assumed retired: kept apart, never counted as to provide
    doc_only = {e["name"]: e for e in res["doc_only"]["bots"]}
    assert set(doc_only) == {"Zorblax Sender Bot"} and doc_only["Zorblax Sender Bot"]["confidence"] == "Likely"
    assert any("Documented step 3" in r for r in doc_only["Zorblax Sender Bot"]["reasons"])
    assert [e["name"] for e in res["doc_only"]["tables"]] == ["A bot that uses table Orphan"]
    status = {s["step"]: s["status"] for s in res["doc_only"]["steps"]}
    assert status == {"1": "Covered", "2": "Covered", "3": "Not found"}
    assert res["doc_only"]["steps"][0]["best"] == "Shoprite Login" and {"login", "shoprite"} <= set(res["doc_only"]["steps"][0]["matched"])
    sm = res["summary"]
    assert (sm["to_provide"], sm["doc_only_bots"], sm["doc_only_tables"]) == (1, 1, 1)
    assert (sm["documented_bots"], sm["documented_found"]) == (3, 2)


def test_analysis_without_docs_or_code_and_manifest_gap(export_dir, tmp_path):
    from sdd_generator.gaps import analyse_gaps, to_csv
    from sdd_generator.loader import Export

    empty = Export(source="x")
    r = analyse_gaps(empty, analyse(empty), [], [])
    assert r["bots"] == [] and "No code is loaded" in r["notes"][0]
    ex = load_export(str(export_dir))
    r = analyse_gaps(ex, analyse(ex), [], [])
    assert r["bots"] == [] and r["notes"] == []
    # the manifest lists a bot whose file is not in the export
    m = json.loads((export_dir / "manifest.json").read_text())
    m["files"].append({"path": "Automation Anywhere\\Bots\\Acme\\Work\\Ghost", "contentType": "application/vnd.aa.taskbot",
                       "scannedDependencies": [], "description": ""})
    (export_dir / "manifest.json").write_text(json.dumps(m))
    ex = load_export(str(export_dir))
    r = analyse_gaps(ex, analyse(ex), [], [])
    assert [(e["name"], e["confidence"]) for e in r["bots"]] == [("Ghost", "Certain")]
    assert "manifest" in r["bots"][0]["reasons"][0]
    csv_text = to_csv(r)
    assert csv_text.splitlines()[0].startswith("Bot to provide,Confidence") and "Ghost" in csv_text


def test_unrelated_code_is_not_called_covered(export_dir, tmp_path):
    from sdd_generator.gaps import analyse_gaps

    ex = load_export(str(export_dir))
    doc = _doc_with([["1", "", "\u2022 Push files for storage to the correct folder as per the retailer."]], [["Bot", "Description"]])
    steps = analyse_gaps(ex, analyse(ex), [doc], [])["doc_only"]["steps"]
    assert steps and steps[0]["status"] != "Covered"


def test_analysis_endpoints(export_dir, tmp_path, client):
    client.load("A", [("a.zip", zip_of(export_dir))])
    j = json.loads(client.call("GET", "/api/analysis?name=A")[1])
    assert j["summary"]["has_code"] and j["bots"] == [] and j["rules"]["findings"] == []
    code, body = client.call("GET", "/download/A/analysis")
    assert code == 200 and body.startswith(b"\xef\xbb\xbfBot to provide")
    assert client.call("GET", "/api/analysis?name=Nope")[0] == 404


# ------------------------------------------------------- rule differences ---

def datetime_subtract(n, unit):
    return {"commandName": "subtract", "packageName": "Datetime", "attributes": [
        {"name": "val", "value": {"type": "NUMBER", "number": str(n)}},
        {"name": "unit", "value": {"type": "STRING", "string": unit}}]}


def string_assign(text):
    return {"commandName": "assign", "packageName": "String", "attributes": attrs(sourceString=text)}


def rules_doc(rows, name="Old.pdf"):
    return {"name": name, "fp": "e" * 64, "kind": "pdf", "pages": 4, "sections": {
        "business_rules": {"page": 9, "blocks": [("table", [["Number", "Description"], *rows])]}}}


STRONG = "A payment is checked and ignored when it is more than {n} days old."


def rules_code(tmp_path, days=5):
    return _code(tmp_path, {
        R + "Acme/Work/Payments": bot([step("Check Payment is more than 5 days ago", [datetime_subtract(days, "DAYS")]),
                                       step("Set business units", [string_assign("200000177,200000180,200007175")]),
                                       step("Save PDF", [{"commandName": "copyFiles", "packageName": "File",
                                                          "attributes": attrs(destinationPath="$Out$/$ClaimNumber$-$ContractNumber$-Debit.pdf")}])]),
        R + "Acme/Work/Other": bot([step("Initialize variables")]),
    })


def test_fact_extraction():
    from sdd_generator.rules import counts_in, files_in, ids_in, times_in

    assert times_in("a date range of 1 week, retry after two hours and 30 minutes") == {
        604800: "1 week", 7200: "2 hours", 1800: "30 minutes"}
    assert times_in("5 business days") == {432000: "5 days"}
    assert counts_in("retry up to 3 times, then 2 attempts") == {2, 3}
    assert ids_in("units 200000177 and A32546 on 20260115, order 123") == {"200000177", "A32546"}  # dates and short numbers are not codes
    assert list(files_in("saved as ClaimNumber-ContractNumber-Debit.pdf")) == [(frozenset({"claimnumber", "contractnumber", "debit"}), "pdf")]


def test_rule_differences_and_acceptance_lifecycle(tmp_path):
    from sdd_generator.rules import analyse_rules
    from sdd_generator.store import Store

    ex = rules_code(tmp_path)
    same = rules_doc([["BR1", STRONG.format(n=5)]])
    r = analyse_rules(ex, [same])
    assert r["findings"] == [] and r["summary"]["agree"] == 1 and r["agreements"][0]["kind"] == "time"
    # one week and seven days are the same rule
    seven = rules_code(tmp_path / "s", days=7)
    assert analyse_rules(seven, [rules_doc([["BR1", "A payment is checked and ignored when it is more than 1 week old."]])])["summary"]["agree"] == 1
    # the documentation says 7 days, the code uses 5
    old = rules_doc([["BR1", STRONG.format(n=7)]])
    f = analyse_rules(ex, [old])["findings"]
    assert len(f) == 1 and f[0]["status"] == "Open" and f[0]["doc_says"] == "7 days" and f[0]["code_says"] == "5 days"
    assert f[0]["where"] == "Payments > Check Payment is more than 5 days ago"
    # accept it: tied to these exact values
    store = Store(str(tmp_path / "docs"))
    store.load_docs("P", "add", [old], [])
    store.accept("P", f[0]["id"], f[0]["sig"], "Client approved 5 days in March")
    acc = store.get("P")["acceptances"]
    g = analyse_rules(ex, [old], acc)["findings"][0]
    assert g["status"] == "Accepted" and g["note"] == "Client approved 5 days in March" and g["accepted_at"]
    assert analyse_rules(ex, [old], acc)["summary"] == {"rules": 1, "checked": 1, "agree": 0, "open": 0, "accepted": 1, "changed": 0, "weak": 0}
    # the code changes afterwards: the acceptance no longer applies, the difference reopens
    ten = rules_code(tmp_path / "t", days=10)
    h = analyse_rules(ten, [old], acc)["findings"][0]
    assert h["id"] == g["id"] and h["status"] == "Changed"
    # so does a change to the documentation
    newer = rules_doc([["BR1", STRONG.format(n=9)]])
    assert analyse_rules(ex, [newer], acc)["findings"][0]["status"] == "Changed"
    assert store.reopen("P", f[0]["id"]) and analyse_rules(ex, [old], store.get("P")["acceptances"])["findings"][0]["status"] == "Open"
    assert not store.reopen("P", "nope")
    # one shared word is only a weak link: still reported, but kept apart and not counted as open
    weak = analyse_rules(ex, [rules_doc([["BR1", "Payments more than 7 days old are not processed."]])])
    assert [x["link"] for x in weak["findings"]] == ["weak"] and weak["summary"]["open"] == 0 and weak["summary"]["weak"] == 1


def test_rule_differences_in_code_lists_and_file_names(tmp_path):
    from sdd_generator.rules import analyse_rules

    ex = rules_code(tmp_path)
    doc = rules_doc([
        ["BR4", "Business units: 200000177 Sugar, 200000180 Grocery, 200009999 Pies."],
        ["BR3", "Files are named ClaimNumber-Debit.pdf"]])
    by = {f["kind"]: f for f in analyse_rules(ex, [doc])["findings"]}
    ids = by["codes"]
    assert any("In the code only: 200007175" in d for d in ids["detail"])
    assert any("In the documentation only: 200009999" in d for d in ids["detail"])
    assert ids["where"] == "Payments"
    assert by["file"]["doc_says"] == "ClaimNumber-Debit.pdf" and by["file"]["code_says"] == "$ClaimNumber$-$ContractNumber$-Debit.pdf"
    # a code list that a second documented rule also covers is not "code only"
    both = rules_doc([["BR4", "Units 200000177, 200000180 and 200007175."], ["BR9", "Check 200000177."]])
    assert analyse_rules(ex, [both])["findings"] == []


def test_documentation_only_rules_are_ignored(export_dir, tmp_path):
    from sdd_generator.rules import analyse_rules

    ex = load_export(str(export_dir))
    doc = rules_doc([["BR1", "Payments older than 30 days are rejected by the Zorblax gateway."],
                     ["BR2", "Units 777777777 and 888888888 apply."]])
    r = analyse_rules(ex, [doc])
    assert r["findings"] == [] and r["agreements"] == [] and r["summary"]["checked"] == 0   # no counterpart in the code


def test_differences_appear_in_the_sdd_and_through_the_api(tmp_path, client):
    import io

    ex = rules_code(tmp_path)
    old = rules_doc([["BR1", STRONG.format(n=7)]])
    md = to_markdown(build(ex, analyse(ex), {}, title="T", date="x", docs=[old]))
    sec = md[md.index("#### Documentation compared with the code"):md.index("### e. Functional Design")]
    assert "7 days" in sec and "5 days" in sec and "Open [CONFIRM]" in sec
    from sdd_generator.render import to_html

    menu = to_html(build(ex, analyse(ex), {}, title="T", date="x", docs=[old]))[0]
    assert next(m for m in menu if m["text"].startswith("d. Business Rules"))["todo"] >= 1   # open differences show in the menu
    # through the API: the server works out the values, the browser only names the difference
    root = tmp_path / "code"
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        for f in root.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(root))
    client.load("R", [("c.zip", z.getvalue())])
    client.app.store.load_docs("R", "add", [old], [])
    j = json.loads(client.call("GET", "/api/analysis?name=R")[1])
    fid = j["rules"]["findings"][0]["id"]
    assert j["rules"]["summary"]["open"] == 1
    info = json.loads(client.call("GET", "/api/project?name=R")[1])
    assert info["gaps"]["open_rules"] == 1
    def post(path, obj):
        code, body = client.call("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
        return code, json.loads(body)
    assert post("/api/rules/accept", {"doc": "R", "id": "nope"})[0] == 400
    assert post("/api/rules/accept", {"doc": "R", "id": fid, "note": "valid change"})[0] == 200
    j = json.loads(client.call("GET", "/api/analysis?name=R")[1])
    assert j["rules"]["findings"][0]["status"] == "Accepted" and j["rules"]["summary"]["open"] == 0
    assert json.loads(client.call("GET", "/api/project?name=R")[1])["gaps"]["open_rules"] == 0
    sdd = json.loads(client.call("GET", "/api/doc?name=R")[1])["html"]
    assert "Accepted: valid change" in sdd
    # replacing the code keeps the acceptance
    client.load("R", [("c.zip", z.getvalue())], mode="overwrite")
    assert client.app.store.get("R")["acceptances"]
    assert post("/api/rules/reopen", {"doc": "R", "id": fid})[1]["ok"]
    assert json.loads(client.call("GET", "/api/analysis?name=R")[1])["rules"]["summary"]["open"] == 1
