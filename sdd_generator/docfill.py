"""Turn parsed documentation into SDD blocks that fill gaps the code cannot fill.

Priority, highest first: what the code export shows, then intake the user typed in, then
documentation. Documentation text is always preceded by a ("src", ...) caption naming the file
and page, so a reader can tell what was verified against code and what was not.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .docparse import letters

HEADERS = {
    "moscow": ["Priority", "Items"],
    "functional_requirements": ["#", "Requirement"],
    "benefits": ["#", "Benefit", "Description"],
    "stakeholders": ["Stakeholder", "Role in Automation", "Email", "Change Request Approval Level"],
    "business_rules": ["Number", "Description"],
    "process_flow": ["Step", "Screen Shot", "Description"],
    "applications": ["Nr", "Name", "Type", "Access", "Bot Username"],
    "bot_components": ["Bot", "Description"],
    "triggers": ["Process / Bot Initialized", "Trigger Type", "Notes"],
    "inputs_outputs": ["Item", "Task Bot Name", "Input", "Output"],
    "reports_logs": ["Description", "Log/Report Type", "Complexity Level"],
}
TOKEN_STOP = {"bot", "the", "and", "of", "to", "for", "a", "in", "task"}


def _is_header(row, header):
    first = letters(row[0]) if row else ""
    return first in {letters(h) for h in header[:1]} or first in ("number", "nr", "bot", "step", "item", "benefit")


def table_block(key, rows):
    """Return (table block, scenario captions) for documentation rows, dropping empty columns."""
    header = HEADERS.get(key)
    rows = [r for r in rows if any(r)]
    if not rows:
        return None
    if header and len(rows[0]) == len(header) and _is_header(rows[0], header):
        rows = rows[1:]  # the document's own header row; ours has the same shape
    elif not header or len(rows[0]) != len(header):
        header = [f"Column {i + 1}" for i in range(len(rows[0]))]
    if not rows:
        return None
    keep = [i for i in range(len(header)) if any(len(r) > i and r[i] for r in rows)]
    rows = [[r[i] if i < len(r) else "" for i in keep] for r in rows if not _is_header(r, HEADERS.get(key, [""]))]
    return ("table", [header[i] for i in keep], [[c.split("\n") if "\n" in c else c for c in r] for r in rows])


class Docs:
    """Documentation files loaded for a document, and what of them the SDD used."""

    def __init__(self, docs):
        self.docs = docs or []
        self.used = defaultdict(set)  # file name -> section keys

    def __bool__(self):
        return bool(self.docs)

    def has(self, key):
        return any(key in d["sections"] for d in self.docs)

    def caption(self, d, key, what="From documentation"):
        page = d["sections"][key]["page"]
        return ("src", f"{what}: {d['name']}" + (f", page {page}" if page else ""))

    def blocks(self, key, only_text=False):
        """Blocks for one section from every document that has it, each under its caption."""
        out = []
        for d in self.docs:
            s = d["sections"].get(key)
            if not s:
                continue
            self.used[d["name"]].add(key)
            out.append(self.caption(d, key))
            for kind, payload in s["blocks"]:
                if kind == "p":
                    out.append(("p", payload))
                elif kind == "ul":
                    out.append(("ul", payload))
                elif not only_text:
                    scenario = None
                    rows = payload
                    if key == "process_flow":
                        # a "Scenario: SC1" row in the table names the scenario that follows
                        while rows and rows[0] and re.match(r"scenario", rows[0][0], re.I) and not any(rows[0][1:]):
                            scenario, rows = rows[0][0], rows[1:]
                            out.append(("p", f"**{scenario}**"))
                    t = table_block(key, rows)
                    if t:
                        out.append(t)
        return out

    def rows(self, key):
        """[(doc, row)] for every data row of a section's tables."""
        out = []
        for d in self.docs:
            for kind, payload in d["sections"].get(key, {}).get("blocks", []):
                if kind == "table":
                    for r in payload:
                        if any(r) and not _is_header(r, HEADERS.get(key, [""])):
                            out.append((d, r))
        return out


def _tokens(name):
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)  # NetSuiteLogin -> NetSuite Login
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in TOKEN_STOP and len(t) > 1}


def same_thing(a, b):
    """Loose match between a documented name and a name from the code, on whole words only.

    Matching letters inside words would pair "Log" with "Logic Manager".
    """
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.75


def documented_bots(docs):
    """Bot names listed in documentation, as [(name, doc)]."""
    out, seen = [], set()
    for d, r in docs.rows("bot_components"):
        name = (r[1] if len(r) > 1 else r[0]).replace("\n", " ").strip()
        if name and letters(name) not in seen and "separatesdd" not in letters(name):
            seen.add(letters(name))
            out.append((re.sub(r"\(.*?\)", "", name).strip(), d))
    return out
