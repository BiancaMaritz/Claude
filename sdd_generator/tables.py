"""Read exports of current tables (CSV, TSV, Excel) into a schema: names, types, row counts.

Only the shape is kept. Cell values are never stored, because exports of live tables can hold
personal or commercial data that has no place in a design document.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import os
import re

TABLE_EXT = (".csv", ".tsv", ".xlsx")
SAMPLE_ROWS = 5000  # rows used to infer a column's type; every row is still counted
MAX_COLUMNS = 500
DATE_RES = [re.compile(p) for p in (
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$",
    r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")]
INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _kind(v):
    """Type of one cell: 'int', 'float', 'date', 'bool', 'text' or None when empty."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return "date"
    s = str(v).strip()
    if s == "":
        return None
    if INT_RE.match(s):
        return "text" if len(s.lstrip("+-")) > 1 and s.lstrip("+-").startswith("0") else "int"  # 007 is an identifier
    if FLOAT_RE.match(s):
        return "float"
    if any(r.match(s) for r in DATE_RES):
        return "date"
    if s.lower() in ("true", "false"):
        return "bool"
    return "text"


def _combine(kinds):
    kinds = {k for k in kinds if k}
    if not kinds:
        return "empty"
    if len(kinds) == 1:
        return kinds.pop()
    if kinds == {"int", "float"}:
        return "float"
    return "text"


class _Columns:
    def __init__(self, names):
        self.names = names
        self.kinds = [set() for _ in names]
        self.nonnull = [0] * len(names)
        self.rows = 0

    def add(self, row):
        self.rows += 1
        for i in range(len(self.names)):
            k = _kind(row[i]) if i < len(row) else None
            if k:
                self.nonnull[i] += 1
                if self.rows <= SAMPLE_ROWS:
                    self.kinds[i].add(k)

    def result(self):
        return [{"name": n, "type": _combine(self.kinds[i]), "nonnull": self.nonnull[i]}
                for i, n in enumerate(self.names)]


def _header(raw):
    seen, out = {}, []
    for i, h in enumerate(raw[:MAX_COLUMNS]):
        h = re.sub(r"\s+", " ", str(h if h is not None else "")).strip() or f"Column {i + 1}"
        seen[h] = seen.get(h, 0) + 1
        out.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
    return out


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _csv_table(path, label):
    delim = "\t" if label.lower().endswith(".tsv") else None
    last = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as fh:
                if delim is None:
                    head = fh.read(65536)
                    try:
                        delim = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
                    except csv.Error:
                        delim = ","
                    fh.seek(0)
                reader = csv.reader(fh, delimiter=delim)
                first = next(reader, None)
                if not first:
                    raise ValueError(f"{label}: the file is empty.")
                cols = _Columns(_header(first))
                for row in reader:
                    if any(c.strip() for c in row):
                        cols.add(row)
                return cols
        except UnicodeDecodeError as e:
            last = e
            delim = "\t" if label.lower().endswith(".tsv") else None
    raise ValueError(f"{label}: could not decode the file ({last}).")


def analyze_file(path, label):
    """Return [{name, source, rows, columns, fp}] for a CSV/TSV (one table) or workbook (one per sheet)."""
    ext = os.path.splitext(label)[1].lower()
    if ext not in TABLE_EXT:
        raise ValueError(f"{label}: tables must be .csv, .tsv or .xlsx.")
    stem = os.path.splitext(label)[0].strip()
    fp = _file_hash(path)
    out = []
    if ext in (".csv", ".tsv"):
        cols = _csv_table(path, label)
        out.append({"name": stem, "source": label, "rows": cols.rows, "columns": cols.result(), "fp": fp})
    elif ext == ".xlsx":
        try:
            import openpyxl

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as e:  # corrupt, password protected
            raise ValueError(f"{label}: could not open the workbook ({type(e).__name__}: {e}).") from e
        try:
            for ws in wb.worksheets:
                rows = ws.iter_rows(values_only=True)
                first = next(rows, None)
                if not first or not any(c is not None and str(c).strip() for c in first):
                    continue
                cols = _Columns(_header(list(first)))
                for row in rows:
                    if any(c is not None and str(c).strip() for c in row):
                        cols.add(row)
                name = stem if len(wb.worksheets) == 1 else ws.title
                body = repr((name, [(c["name"], c["type"]) for c in cols.result()], cols.rows))
                out.append({"name": name, "source": label, "sheet": ws.title, "rows": cols.rows,
                            "columns": cols.result(),
                            "fp": hashlib.sha256((fp + body).encode()).hexdigest()})
        finally:
            wb.close()
    else:
        raise ValueError(f"{label}: tables must be .csv, .tsv or .xlsx.")
    if not out:
        raise ValueError(f"{label}: no table with a header row found.")
    return out


def diff_table(old, new):
    """What changed between two versions of a table: columns added/removed/retyped, row count."""
    a = {c["name"]: c["type"] for c in old["columns"]}
    b = {c["name"]: c["type"] for c in new["columns"]}
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "retyped": sorted(f"{n}: {a[n]} to {b[n]}" for n in a if n in b and a[n] != b[n]),
            "rows": [old["rows"], new["rows"]]}
