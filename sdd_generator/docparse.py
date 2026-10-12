"""Read old SDD documents (PDF, Word, Markdown) into sections the generator can use as gap fillers.

Documentation never overrides the code. It supplies what a bot export cannot say: business
description, requirements, stakeholders, triggers, access details. Each section keeps the page it
came from so the generated SDD can cite it.

Only text and tables are read. Screenshots and diagrams are not imported.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata

# Section title (letters only, lower case) -> key. Matched on the whole title, or its start.
PARTS = {"businessrequirements", "functionaldesign", "technicaldesign"}
TITLES = [
    ("processdescription", "process_description"),
    ("priority", "moscow"),
    ("assumptions", "assumptions"),
    ("functionalrequirements", "functional_requirements"),
    ("successcriteria", "success_criteria"),
    ("expectedbenefits", "benefits"),
    ("stakeholders", "stakeholders"),
    ("tobeprocessflowsteps", "process_flow"),
    ("changesbroughtaboutbyautomation", "changes_by_automation"),
    ("functionaldesignsignoff", "signoff"),
    ("businessrules", "business_rules"),
    ("applicationssystems", "applications"),
    ("botcomponents", "bot_components"),
    ("triggers", "triggers"),
    ("inputsoutputs", "inputs_outputs"),
    ("reportsandlogs", "reports_logs"),
]
BULLETS = "●•○➢▪■◦‣-–—*"
BULLET_RE = re.compile(rf"^\s*[{re.escape(BULLETS)}]\s*(.*)$")
HEADING_RE = re.compile(r"^\s*(?:\d+|[a-g])\s*[.)]\s*(.+?)\s*$", re.I)


def clean(s) -> str:
    s = unicodedata.normalize("NFKC", s or "")  # also turns ligatures such as fi into plain letters
    return re.sub(r"[​­]", "", s).replace("\r", "").strip()


def letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", clean(s).lower())


def heading_key(text: str):
    """Return the section key for a heading line, '_part' for 1./2./3., or None."""
    m = HEADING_RE.match(clean(text))
    if not m or len(m.group(1)) > 70:
        return None
    title = m.group(1)
    if re.search(r"\s\d{1,3}$", title):  # a contents-page line ends in its page number
        return None
    k = letters(title)
    if k in PARTS:
        return "_part"
    if "erd" in k and "design" in k:
        return "erd"
    for prefix, key in TITLES:
        if k == prefix or (k.startswith(prefix) and key in ("process_flow", "moscow")):
            return key
    return None


# --------------------------------------------------------------- streams ---
# A stream is a list of items in reading order:
#   {"kind": "line", "text", "page", "top", "bottom"}  or  {"kind": "table", "rows", "page"}

def _pdf_stream(path):
    import pdfplumber

    stream, pages = [], 0
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, 1):
            tables = page.find_tables({"text_x_tolerance": 1.0})
            boxes = [t.bbox for t in tables]

            def outside(obj, boxes=boxes):
                if obj.get("object_type") != "char":
                    return True
                cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)

            for ln in page.filter(outside).extract_text_lines(x_tolerance=1.0):
                text = clean(ln["text"])
                if text and not re.fullmatch(r"\d{1,3}", text):  # bare page numbers
                    stream.append({"kind": "line", "text": text, "page": pno,
                                   "top": ln["top"], "bottom": ln["bottom"]})
            for t in tables:
                rows = t.extract(x_tolerance=1.0)
                stream.append({"kind": "table", "rows": rows, "page": pno, "top": t.bbox[1]})
            # reading order within the page
            page_items = [i for i in stream if i["page"] == pno]
            page_items.sort(key=lambda i: i.get("top", 0))
            stream[:] = [i for i in stream if i["page"] != pno] + page_items
    return stream, pages


def _docx_stream(path):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    stream = []
    for el in d.element.body.iterchildren():
        if el.tag.endswith("}p"):
            p = Paragraph(el, d)
            text = clean(p.text)
            if text:
                bullet = (p.style.name or "").lower().startswith("list")
                stream.append({"kind": "line", "text": ("● " if bullet else "") + text, "page": 0, "para": True})
        elif el.tag.endswith("}tbl"):
            t = Table(el, d)
            stream.append({"kind": "table", "page": 0,
                           "rows": [[clean(c.text) for c in r.cells] for r in t.rows]})
    return stream, 0


def _md_stream(path):
    with open(path, encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    stream, rows = [], []

    def flush():
        nonlocal rows
        if rows:
            stream.append({"kind": "table", "rows": rows, "page": 0})
            rows = []

    for raw in lines:
        s = raw.strip()
        if s.startswith("|"):
            cells = [clean(c.replace("<br>", "\n")) for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                rows.append(cells)
            continue
        flush()
        s = re.sub(r"^#+\s*", "", s)
        if s:
            stream.append({"kind": "line", "text": clean(s), "page": 0, "para": True})
    flush()
    return stream, 0


# ------------------------------------------------------------- cleaning ---

def unwrap(cell) -> str:
    """Turn a table cell's visual line wraps back into items; one item per line of output."""
    items = []  # [text, is_bullet]
    for ln in clean(cell or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        m = BULLET_RE.match(ln)
        if m:
            items.append([m.group(1).strip(), True])
        elif items and (ln[0].islower() or items[-1][0].endswith((",", "(", "-", "&", "/", " and", " or"))
                        or (items[-1][1] and not items[-1][0].endswith((".", ":", ";")))):
            items[-1][0] += " " + ln
        else:
            items.append([ln, False])
    return "\n".join(("• " if b else "") + t for t, b in items if t)


def _merge_rows(tables):
    """Join tables split across pages and rows continued on the next page."""
    rows = []
    for t in tables:
        for r in t:
            r = [unwrap(c) for c in r]
            if not any(r):
                continue
            if rows and not r[0] and any(r[1:]) and len(r) == len(rows[-1]):
                for i in range(1, len(r)):
                    if r[i]:
                        rows[-1][i] = (rows[-1][i] + "\n" + r[i]).strip()
                continue
            rows.append(r)
    return rows


def _blocks(items):
    """Group a section's items into paragraphs, bullet lists and tables."""
    blocks, para, ul, prev = [], [], [], None

    def flush():
        nonlocal para, ul
        if para:
            blocks.append(("p", " ".join(para)))
            para = []
        if ul:
            blocks.append(("ul", ul))
            ul = []

    tables = []
    for it in items:
        if it["kind"] == "table":
            flush()
            blocks.append(("table", it["rows"]))
            prev = None
            continue
        text = it["text"]
        m = BULLET_RE.match(text)
        gap_break = (prev is not None and "top" in it and "bottom" in prev
                     and it["top"] - prev["bottom"] > 0.5 * (prev["bottom"] - prev["top"]))
        if m:
            if para:
                flush()
            ul.append(m.group(1).strip())
        elif ul and not gap_break and not it.get("para"):
            ul[-1] += " " + text  # wrapped bullet
        elif para and not gap_break and not it.get("para"):
            para.append(text)
        else:
            flush()
            para.append(text)
        prev = it
    flush()
    # merge consecutive table blocks (page-split tables) into one
    out = []
    for b in blocks:
        if b[0] == "table" and out and out[-1][0] == "table":
            out[-1] = ("table", out[-1][1] + b[1])
        else:
            out.append(b)
    return [("table", _merge_rows([b[1]])) if b[0] == "table" else b for b in out]


def sectionise(stream):
    secs, cur = {}, None
    for it in stream:
        if it["kind"] == "line":
            key = heading_key(it["text"])
            if key:
                cur = key
                if key != "_part":
                    secs.setdefault(key, {"page": it["page"], "items": []})
                continue
        if cur and cur != "_part":
            secs[cur]["items"].append(it)
    out = {}
    for key, s in secs.items():
        blocks = [b for b in _blocks(s["items"]) if not (b[0] == "table" and not b[1])]
        if blocks:
            out[key] = {"page": s["page"], "blocks": blocks}
    return out


def parse_document(path: str, label: str | None = None) -> dict:
    """Parse one documentation file. Raises ValueError when it is not a readable SDD."""
    label = label or os.path.basename(path)
    ext = os.path.splitext(label)[1].lower()
    with open(path, "rb") as fh:
        fp = hashlib.sha256(fh.read()).hexdigest()
    try:
        if ext == ".pdf":
            stream, pages = _pdf_stream(path)
        elif ext == ".docx":
            stream, pages = _docx_stream(path)
        elif ext in (".md", ".txt"):
            stream, pages = _md_stream(path)
        else:
            raise ValueError(f"{label}: documentation must be .pdf, .docx or .md.")
    except ValueError:
        raise
    except Exception as e:  # corrupt or encrypted file
        raise ValueError(f"{label}: could not read the file ({type(e).__name__}: {e}).") from e
    sections = sectionise(stream)
    sections.pop("signoff", None)  # sign-off pages carry dates and initials, not content
    if not sections:
        raise ValueError(f"{label}: no SDD sections found. Scanned (image-only) PDFs cannot be read.")
    return {"name": label, "fp": fp, "kind": ext.lstrip("."), "pages": pages, "sections": sections}
