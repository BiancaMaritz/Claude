"""Read table definitions from a SQL script (CREATE TABLE and ALTER TABLE ... ADD CONSTRAINT).

This is where real keys come from. A bot export holds no primary or foreign keys, and neither does a
CSV export of a table; a schema script does. Script the tables out of the database (SQL Server
Management Studio: Tasks > Generate Scripts, schema only) and load the .sql file under Tables.

parse_ddl(text) -> [{name, schema, database, columns: [{name, type, nullable}], pk: [...],
                     fks: [{cols, ref_table, ref_cols, name}], unique: [[...]]}]
"""
from __future__ import annotations

import re

from .sqlmodel import IDENT, QNAME, _split_qname, _unq, clean

CREATE = re.compile(rf"\bCREATE\s+TABLE\s+({QNAME})\s*\(", re.I)
ALTER_FK = re.compile(
    rf"\bALTER\s+TABLE\s+({QNAME})\s+(?:WITH\s+(?:NO)?CHECK\s+)?ADD\s+(?:CONSTRAINT\s+({IDENT})\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)\s*"
    rf"REFERENCES\s+({QNAME})\s*\(([^)]*)\)", re.I)
ALTER_PK = re.compile(
    rf"\bALTER\s+TABLE\s+({QNAME})\s+ADD\s+(?:CONSTRAINT\s+{IDENT}\s+)?PRIMARY\s+KEY\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(([^)]*)\)", re.I)
USE_DB = re.compile(rf"\bUSE\s+({IDENT})", re.I)
CONSTRAINT_START = re.compile(r"^(?:CONSTRAINT\b|PRIMARY\s+KEY\b|FOREIGN\s+KEY\b|UNIQUE\b|CHECK\b|INDEX\b|KEY\b)", re.I)


def _cols(text: str):
    return [_unq(c) for c in re.findall(IDENT, re.sub(r"\b(ASC|DESC)\b", "", text, flags=re.I))]


def _balanced(text: str, start: int):
    """Text inside the parentheses that open at `start` (index of '('), and the index after the closing one."""
    depth = 0
    for i in range(start, len(text)):
        depth += (text[i] == "(") - (text[i] == ")")
        if depth == 0:
            return text[start + 1:i], i + 1
    return text[start + 1:], len(text)


def _split_top(body: str):
    items, depth, cur = [], 0, ""
    for ch in body:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip())
    return items


def parse_ddl(text: str) -> list:
    sql = clean(text)
    db_at = [(m.start(), _unq(m.group(1))) for m in USE_DB.finditer(sql)]
    tables = {}
    for m in CREATE.finditer(sql):
        name, schema, db = _split_qname(m.group(1))
        if not db:
            db = next((d for p, d in reversed(db_at) if p < m.start()), "")
        body, _ = _balanced(sql, m.end() - 1)
        t = {"name": name, "schema": schema, "database": db, "columns": [], "pk": [], "fks": [], "unique": []}
        for item in _split_top(body):
            if CONSTRAINT_START.match(item):
                body_item = re.sub(rf"^CONSTRAINT\s+{IDENT}\s+", "", item, flags=re.I)
                cname = (re.match(rf"CONSTRAINT\s+({IDENT})", item, re.I) or [None, ""])[1]
                if re.match(r"PRIMARY\s+KEY", body_item, re.I):
                    pm = re.search(r"\(([^)]*)\)", body_item)
                    t["pk"] = _cols(pm.group(1)) if pm else []
                elif re.match(r"FOREIGN\s+KEY", body_item, re.I):
                    fm = re.match(rf"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+({QNAME})\s*(?:\(([^)]*)\))?", body_item, re.I)
                    if fm:
                        t["fks"].append({"cols": _cols(fm.group(1)), "ref_table": _split_qname(fm.group(2))[0],
                                         "ref_cols": _cols(fm.group(3) or ""), "name": _unq(cname) if cname else ""})
                elif re.match(r"UNIQUE", body_item, re.I):
                    um = re.search(r"\(([^)]*)\)", body_item)
                    if um:
                        t["unique"].append(_cols(um.group(1)))
                continue
            cm = re.match(rf"({IDENT})\s+((?:\[[^\]]+\]|[A-Za-z_]\w*)(?:\s*\(\s*[\w,\s]+\s*\))?)", item, re.I)
            if not cm:
                continue
            col = _unq(cm.group(1))
            ctype = re.sub(r"\s+", "", cm.group(2).replace("[", "").replace("]", "")).lower()
            t["columns"].append({"name": col, "type": ctype, "nullable": not re.search(r"\bNOT\s+NULL\b", item, re.I)})
            if re.search(r"\bPRIMARY\s+KEY\b", item, re.I):
                t["pk"] = [col]
            ref = re.search(rf"\bREFERENCES\s+({QNAME})\s*(?:\(([^)]*)\))?", item, re.I)
            if ref:
                t["fks"].append({"cols": [col], "ref_table": _split_qname(ref.group(1))[0],
                                 "ref_cols": _cols(ref.group(2) or ""), "name": ""})
        for c in t["columns"]:
            if c["name"] in t["pk"]:
                c["nullable"] = False  # a key column cannot be null
        tables[name.lower()] = t
    for m in ALTER_PK.finditer(sql):
        t = tables.get(_split_qname(m.group(1))[0].lower())
        if t:
            t["pk"] = _cols(m.group(2))
    for m in ALTER_FK.finditer(sql):
        t = tables.get(_split_qname(m.group(1))[0].lower())
        if t:
            t["fks"].append({"cols": _cols(m.group(3)), "ref_table": _split_qname(m.group(4))[0],
                             "ref_cols": _cols(m.group(5)), "name": _unq(m.group(2)) if m.group(2) else ""})
    return list(tables.values())
