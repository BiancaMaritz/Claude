"""Read the SQL that bots send to a database: which tables, which columns, how they are used, how they join.

This is a reader for the T-SQL that A360 bots typically carry, not a SQL engine. It is deliberately
conservative: a column is only attributed to a table when the statement makes that unambiguous, and
anything it cannot place is left out rather than guessed.

parse_sql(text) -> {
    "tables": {name: {"db": set, "schema": set, "ops": set, "cols": {column: set of 'W','R','F','J'}, "star": bool}},
    "joins":  [(table_a, column_a, table_b, column_b)],
}
Column uses: W written (INSERT column list, UPDATE SET), R read (SELECT list), F filtered or ordered on
(WHERE, GROUP BY, HAVING, ORDER BY), J used in a join condition.
"""
from __future__ import annotations

import re

KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "in", "is", "null", "like", "between", "exists", "as", "on", "join",
    "inner", "left", "right", "full", "outer", "cross", "group", "by", "order", "having", "union", "all", "distinct",
    "top", "set", "values", "insert", "into", "update", "delete", "case", "when", "then", "else", "end", "asc", "desc",
    "declare", "if", "begin", "with", "merge", "using", "output", "true", "false", "day", "hour", "minute", "month",
    "year", "second", "week", "dd", "hh", "mi", "mm", "yy", "ss", "int", "bigint", "varchar", "nvarchar", "date",
    "datetime", "datetime2", "decimal", "float", "bit", "char", "nchar", "numeric", "money", "text", "exec", "execute",
    "print", "return", "while", "go", "table", "alter", "create", "drop"}
IDENT = r"(?:\[[^\]]+\]|\"[^\"]+\"|`[^`]+`|\$[^\s$]+\$|[A-Za-z_#@][\w$#@]*)"
QNAME = rf"{IDENT}(?:\s*\.\s*{IDENT}){{0,3}}"
TABLE_REF = re.compile(rf"\b(FROM|JOIN|UPDATE|INTO|DELETE\s+FROM|MERGE\s+INTO|USING)\s+({QNAME})(?:\s+(?:AS\s+)?({IDENT}))?", re.I)
NEXT_COMMA_REF = re.compile(rf"\s*,\s*({QNAME})(?:\s+(?:AS\s+)?({IDENT}))?", re.I)
CLAUSE = re.compile(r"\b(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|SET|VALUES|ON|INSERT\s+INTO|UPDATE|DELETE\s+FROM|JOIN)\b", re.I)
QUAL_COL = re.compile(rf"({IDENT})\s*\.\s*({IDENT})")
COMPARATOR = re.compile(rf"(?<![\w.\]\$])({IDENT})\s*(?:=|<>|!=|<=|>=|<|>|\bLIKE\b|\bIN\b|\bIS\b|\bBETWEEN\b)", re.I)
SET_LHS = re.compile(rf"(?:\bSET\b|,)\s*(\[[^\]]+\]|[A-Za-z_]\w*)\s*=", re.I)


def clean(sql: str) -> str:
    """Mask string literals as '' and drop comments in one pass, so neither can confuse the reader.

    A single pass matters: an apostrophe inside a comment must not open a string, and "--" inside a
    string must not start a comment.
    """
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            out.append("''")
        elif c == "-" and sql[i:i + 2] == "--":
            while i < n and sql[i] != "\n":
                i += 1
        elif c == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c == "[":  # bracketed identifiers can hold anything but ]
            j = sql.find("]", i)
            j = n - 1 if j < 0 else j
            out.append(sql[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _unq(tok: str) -> str:
    tok = tok.strip()
    if tok[:1] == "[" and tok[-1:] == "]" or tok[:1] in "\"`" and tok[-1:] == tok[:1]:
        return tok[1:-1]
    if tok[:1] == "$" and tok[-1:] == "$":
        return "{" + tok[1:-1] + "}"  # a bot variable used as a name, usually the database
    return tok


def _split_qname(q: str):
    parts = [_unq(p) for p in re.findall(IDENT, q)]
    table = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else ""
    db = parts[-3] if len(parts) >= 3 else ""
    return table, schema, db


def _is_name(tok: str) -> bool:
    return bool(tok) and _unq(tok).lower() not in KEYWORDS


def parse_sql(text: str) -> dict:
    tables, joins = {}, []
    for stmt in clean(text).split(";"):
        _parse_statement(stmt.strip(), tables, joins)
    return {"tables": tables, "joins": joins}


def _parse_statement(stmt: str, tables: dict, joins: list):
    if not stmt:
        return
    alias = {}  # alias or bare table name (lower case) -> table key
    refs = []   # (table key, position, op)

    def add_ref(qname, al, op, pos):
        table, schema, db = _split_qname(qname)
        if not _is_name(table) or table.startswith("@") or table.startswith("#") or table.lower() in ("dual",):
            return None
        key = table.lower()
        t = tables.setdefault(key, {"name": table, "db": set(), "schema": set(), "ops": set(), "cols": {}, "star": False})
        if db:
            t["db"].add(db)
        if schema:
            t["schema"].add(schema)
        t["ops"].add(op)
        alias[key] = key
        if al and _is_name(al):
            alias[_unq(al).lower()] = key
        refs.append((key, pos, op))
        return key

    OPS = {"FROM": "select", "JOIN": "select", "UPDATE": "update", "INTO": "insert", "USING": "select"}
    for m in TABLE_REF.finditer(stmt):
        kw = re.sub(r"\s+", " ", m.group(1).upper())
        op = "delete" if kw == "DELETE FROM" else "insert" if kw in ("INTO", "MERGE INTO") else OPS.get(kw, "select")
        add_ref(m.group(2), m.group(3), op, m.start())
        if kw == "FROM":  # FROM a, b c : further tables separated by commas
            pos = m.end()
            while True:
                c = NEXT_COMMA_REF.match(stmt, pos)
                if not c or not _is_name(_split_qname(c.group(1))[0]):
                    break
                add_ref(c.group(1), c.group(2), "select", c.start())
                pos = c.end()
    if not refs:
        return
    single = len({r[0] for r in refs}) == 1
    only = refs[0][0]
    clauses = [(m.start(), re.sub(r"\s+", " ", m.group(1).upper())) for m in CLAUSE.finditer(stmt)]

    def clause_at(pos):
        cur = ""
        for p, name in clauses:
            if p <= pos:
                cur = name
        return cur

    def mark(key, col, use):
        col = _unq(col)
        if not col or col == "*" or col.lower() in KEYWORDS:
            return
        t = tables[key]
        t["cols"].setdefault(col, set()).add(use)

    def use_for(pos):
        c = clause_at(pos)
        return {"SELECT": "R", "SET": "R", "WHERE": "F", "GROUP BY": "F", "ORDER BY": "F", "HAVING": "F", "ON": "J"}.get(c)

    # qualified columns: alias.col
    seen = set()
    for m in QUAL_COL.finditer(stmt):
        a, col = _unq(m.group(1)).lower(), m.group(2)
        if a in alias and (m.start(), col) not in seen:
            seen.add((m.start(), col))
            use = use_for(m.start())
            if use:
                mark(alias[a], col, use)
    # INSERT INTO t (cols)
    for m in re.finditer(rf"\bINSERT\s+INTO\s+({QNAME})\s*\(([^)]*)\)", stmt, re.I):
        key = _split_qname(m.group(1))[0].lower()
        if key in tables:
            for col in re.findall(IDENT, m.group(2)):
                mark(key, col, "W")
    # UPDATE ... SET a = ..., b = ...
    set_m = re.search(r"\bSET\b(.*?)(?=\bWHERE\b|\bFROM\b|$)", stmt, re.I)
    if set_m and any(op == "update" for _, _, op in refs):
        key = next(k for k, _, op in refs if op == "update")
        for m in SET_LHS.finditer(stmt[set_m.start():set_m.end()]):
            lhs = m.group(1)
            if "." not in lhs:
                mark(key, lhs, "W")
    if single:
        # one table: bracketed names are columns, and so are bare names where the statement's shape says so
        for m in re.finditer(r"\[([^\]]+)\]", stmt):
            name = m.group(1)
            before = stmt[max(0, m.start() - 1):m.start()]
            if before == "." or stmt[m.end():m.end() + 1] == ".":
                continue  # a schema, database or table name
            if name.lower() == tables[only]["name"].lower() or name.lower() in {s.lower() for s in tables[only]["schema"] | tables[only]["db"]}:
                continue
            if clause_at(m.start()) in ("INSERT INTO", "UPDATE", "DELETE FROM", "FROM", "JOIN", "VALUES"):
                continue
            if clause_at(m.start()) == "SET" and re.match(r"\s*=", stmt[m.end():]):
                continue  # already a written column
            use = use_for(m.start())
            if use:
                mark(only, name, use)
        for m in COMPARATOR.finditer(stmt):
            use = use_for(m.start())
            tok = m.group(1)
            if use == "F" and _is_name(tok) and not tok.startswith("$") and not tok.startswith("@"):
                if clause_at(m.start()) == "WHERE" and stmt[max(0, m.start() - 1):m.start()] != ".":
                    mark(only, tok, "F")
        for m in re.finditer(rf"\b(?:CAST|CONVERT|ISNULL|NULLIF|LTRIM|RTRIM|UPPER|LOWER|COUNT|SUM|MAX|MIN|AVG)\s*\(\s*({IDENT})\s*(?:\)|,|\bAS\b)", stmt, re.I):
            tok = m.group(1)
            use = use_for(m.start())
            if use and _is_name(tok) and not tok.startswith(("$", "@")) and not tok.startswith("["):
                mark(only, tok, use)
        sel = re.search(r"\bSELECT\b(?:\s+(?:DISTINCT|TOP\s+\d+))?(.*?)\bFROM\b", stmt, re.I)
        if sel:
            body = sel.group(1)
            if re.search(r"(^|,)\s*\*", body):
                tables[only]["star"] = True
            depth, item, items = 0, "", []
            for ch in body:
                depth += (ch == "(") - (ch == ")")
                if ch == "," and depth == 0:
                    items.append(item)
                    item = ""
                else:
                    item += ch
            items.append(item)
            for it in items:
                it = re.sub(r"\s+AS\s+\w+\s*$", "", it.strip(), flags=re.I)
                if re.fullmatch(IDENT, it) and _is_name(it) and not it.startswith(("$", "@")):
                    mark(only, it, "R")
    # joins: a.x = b.y between two different tables, in ON or WHERE
    for m in re.finditer(rf"({IDENT})\s*\.\s*({IDENT})\s*=\s*({IDENT})\s*\.\s*({IDENT})", stmt):
        a1, c1, a2, c2 = _unq(m.group(1)).lower(), m.group(2), _unq(m.group(3)).lower(), m.group(4)
        if a1 in alias and a2 in alias and alias[a1] != alias[a2]:
            ta, tb = alias[a1], alias[a2]
            joins.append((tables[ta]["name"], _unq(c1), tables[tb]["name"], _unq(c2)))
            mark(ta, c1, "J")
            mark(tb, c2, "J")
