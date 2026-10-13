"""Entity relationship diagram of the database tables the bots use.

What the data can and cannot say:
  * Tables, the columns the bots touch and how (W written, R read, F filtered on, J joined on) come
    from the SQL in the bots. That is code, so it is certain.
  * The database name is shown when the SQL or a loaded schema script spells it out. When the bot takes
    it from a variable, the variable and its vault entry are shown; vault values are never read.
  * Primary and foreign keys do not exist in a bot export. They are drawn only from a loaded schema
    script (.sql). A column called XId is marked "key?" and is a guess, labelled as one.
  * Relationships are "confirmed" when a schema script declares the foreign key or a bot's SQL joins
    the two tables on those columns (solid line). They are "inferred" when two tables share an
    id-like column name and nothing confirms it (dashed line).

model(a, tables, inferred=True) -> {"tables": [...], "relations": [...]}
scene(model)                    -> drawable boxes and edges
to_svg(scene) / to_png(scene, path)
"""
from __future__ import annotations

import html
import math
import re
from collections import defaultdict

BOX_W, ROW_H, HEAD_H, GAP_X, GAP_Y, PAD = 270, 18, 44, 90, 34, 24
MAX_ROWS = 14
ID_LIKE = re.compile(r"(^|[a-z_])(id|no|number|code|key)$|id$", re.I)
GENERIC = {"id", "name", "status", "date", "description", "created", "updated", "type"}


def _plain(n):
    return re.sub(r"[^a-z0-9]", "", n.lower())


def model(a: dict, tables: list | None = None, inferred: bool = True) -> dict:
    tables = tables or []
    T = {}  # lower name -> table record

    def get(name):
        k = name.lower()
        return T.setdefault(k, {"name": name, "dbs": set(), "vars": set(), "vaults": set(), "conns": set(), "schema": "",
                                "cols": {}, "used": False, "ddl": False, "ops": set(), "bots": set(), "star": False,
                                "pk": [], "rows": None})

    joins = {}
    for path, d in a["per"].items():
        for q in d.get("sql", []):
            p = q["parsed"]
            for key, t in p["tables"].items():
                r = get(t["name"])
                r["used"] = True
                r["ops"] |= t["ops"]
                r["bots"].add(path)
                r["star"] |= t["star"]
                r["schema"] = r["schema"] or next(iter(sorted(t["schema"])), "")
                for db in t["db"]:
                    if db.startswith("{"):
                        var = db[1:-1]
                        r["vars"].add(var)
                        if var in d.get("var_refs", {}):
                            r["vaults"].add(d["var_refs"][var])
                    else:
                        r["dbs"].add(db)
                r["conns"] |= set(d.get("connections", []))
                for col, uses in t["cols"].items():
                    c = r["cols"].setdefault(col, {"uses": set(), "key": "", "type": "", "nullable": None})
                    c["uses"] |= uses
            for ta, ca, tb, cb in p["joins"]:
                k = tuple(sorted(((ta.lower(), ca.lower()), (tb.lower(), cb.lower()))))
                joins.setdefault(k, (ta, ca, tb, cb, set()))[4].add(path.rsplit("/", 1)[-1])

    for t in tables:  # schema scripts and exports
        r = T.get(t["name"].lower()) or (get(t["name"]) if t.get("ddl") else None)
        if r is None:
            continue
        r["rows"] = t.get("rows")
        d = t.get("ddl")
        if d:
            r["ddl"] = True
            r["schema"] = r["schema"] or d.get("schema", "")
            if d.get("database"):
                r["dbs"].add(d["database"])
            r["pk"] = d["pk"]
        for c in t["columns"]:
            if d or c["name"] in r["cols"]:
                col = next((k for k in r["cols"] if k.lower() == c["name"].lower()), c["name"])
                rc = r["cols"].setdefault(col, {"uses": set(), "key": "", "type": "", "nullable": None})
                rc["type"] = c.get("type", "")
                if d:
                    rc["nullable"] = d["nullable"].get(c["name"])
        if d:
            for col in d["pk"]:
                rc = next((v for k, v in r["cols"].items() if k.lower() == col.lower()), None)
                if rc is not None:
                    rc["key"] = "PK"

    rel = {}
    for t in tables:
        d = t.get("ddl")
        if not d or t["name"].lower() not in T:
            continue
        for fk in d["fks"]:
            parent = fk["ref_table"]
            for c_from, c_to in zip(fk["cols"], fk["ref_cols"] or [None] * len(fk["cols"])):
                rel[("fk", t["name"].lower(), c_from.lower(), parent.lower(), (c_to or "").lower())] = {
                    "from": t["name"], "from_col": c_from, "to": parent, "to_col": c_to or "", "kind": "confirmed",
                    "evidence": f"Foreign key {fk['name'] or ''} declared in {t['source']}".replace("  ", " "), "dir": "fk"}
                get(parent)
                fr = T[t["name"].lower()]
                rc = fr["cols"].setdefault(c_from, {"uses": set(), "key": "", "type": "", "nullable": None})
                rc["key"] = rc["key"] or "FK"
    for (ta, ca, tb, cb, bots) in joins.values():
        if any((r["from"].lower(), r["to"].lower()) in ((ta.lower(), tb.lower()), (tb.lower(), ta.lower()))
               and {r["from_col"].lower(), r["to_col"].lower()} == {ca.lower(), cb.lower()} for r in rel.values()):
            continue
        rel[("join", ta.lower(), ca.lower(), tb.lower(), cb.lower())] = {
            "from": ta, "from_col": ca, "to": tb, "to_col": cb, "kind": "confirmed", "dir": "join",
            "evidence": "Joined in SQL by " + ", ".join(sorted(bots))}

    if inferred:
        linked = {frozenset((r["from"].lower(), r["to"].lower())) for r in rel.values()}
        names = list(T)
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                if frozenset((x, y)) in linked:
                    continue
                cx = {k.lower(): k for k in T[x]["cols"]}
                cy = {k.lower(): k for k in T[y]["cols"]}
                shared = [k for k in cx if k in cy and k not in GENERIC and ID_LIKE.search(k)]
                if shared:
                    k = shared[0]
                    rel[("inf", x, k, y, k)] = {"from": T[x]["name"], "from_col": cx[k], "to": T[y]["name"], "to_col": cy[k],
                                                "kind": "inferred", "dir": "", "evidence": f"Both use a column named {cx[k]}"}
                    continue
                for src, dst in ((x, y), (y, x)):  # a column named after the other table: CustomerId -> Customer
                    for k, orig in {k: v for k, v in {c.lower(): c for c in T[src]["cols"]}.items()}.items():
                        m = re.fullmatch(r"(.+?)_?(id|no|key)", k)
                        if m and _plain(m.group(1)) == _plain(T[dst]["name"]) and _plain(T[dst]["name"]) not in ("", "id"):
                            rel[("inf", src, k, dst, "")] = {"from": T[src]["name"], "from_col": orig, "to": T[dst]["name"],
                                                             "to_col": "", "kind": "inferred", "dir": "",
                                                             "evidence": f"{orig} is named after table {T[dst]['name']}"}
                            break

    for r in T.values():  # a guess at keys, only where no script settled it
        if not r["ddl"]:
            for col, c in r["cols"].items():
                if _plain(col) in ("id", _plain(r["name"]) + "id"):
                    c["key"] = "key?"
    related = defaultdict(set)
    for v in rel.values():
        for end, col in ((v["from"], v["from_col"]), (v["to"], v["to_col"])):
            if col:
                related[end.lower()].add(col.lower())
    rel = {k: v for k, v in rel.items() if T[v["from"].lower()]["used"] or T[v["to"].lower()]["used"]}
    touching = {x for v in rel.values() for x in (v["from"].lower(), v["to"].lower())}
    out = []
    for r in sorted(T.values(), key=lambda x: x["name"].lower()):
        if not r["used"] and r["name"].lower() not in touching:
            continue  # a table in the schema script that neither the bots nor any relation reach
        cols = []
        for col, c in r["cols"].items():
            cols.append({"name": col, "uses": "".join(u for u in "WRFJ" if u in c["uses"]), "key": c["key"], "type": c["type"],
                         "nullable": c["nullable"], "related": col.lower() in related[r["name"].lower()]})
        cols.sort(key=lambda c: (not c["key"], not c["related"], c["name"].lower()))
        db = ", ".join(sorted(r["dbs"]))
        if not db and r["vars"]:
            db = ", ".join("{" + v + "}" for v in sorted(r["vars"]))
        out.append({"name": r["name"], "schema": r["schema"], "database": db,
                    "database_note": ", ".join(sorted("vault " + v for v in r["vaults"])) if not r["dbs"] else "",
                    "connections": sorted(r["conns"]), "used": r["used"], "ddl": r["ddl"], "ops": sorted(r["ops"]),
                    "star": r["star"], "columns": cols, "rows": r["rows"], "bots": len(r["bots"])})
    return {"tables": out, "relations": sorted(rel.values(), key=lambda v: (v["kind"], v["from"].lower(), v["to"].lower()))}


# ---- layout --------------------------------------------------------------------------------

def _box_rows(t):
    cols = t["columns"]
    shown = cols[:MAX_ROWS]
    rows = [{"name": c["name"], "key": c["key"], "uses": c["uses"], "type": c["type"]} for c in shown]
    if len(cols) > MAX_ROWS:
        rows.append({"name": f"+{len(cols) - MAX_ROWS} more columns", "key": "", "uses": "", "type": "", "more": True})
    if t["star"] and not rows:
        rows.append({"name": "(all columns, SELECT *)", "key": "", "uses": "R", "type": "", "more": True})
    if not rows:
        rows.append({"name": "(no columns identified)", "key": "", "uses": "", "type": "", "more": True})
    return rows


def scene(m: dict) -> dict:
    tabs = {t["name"].lower(): t for t in m["tables"]}
    adj = defaultdict(set)
    for r in m["relations"]:
        a, b = r["from"].lower(), r["to"].lower()
        if a in tabs and b in tabs and a != b:
            adj[a].add(b)
            adj[b].add(a)
    seen, comps = set(), []
    for n in sorted(tabs, key=lambda k: (-len(adj[k]), k)):
        if n in seen or not adj[n]:
            continue
        comp, queue = [], [n]
        seen.add(n)
        while queue:
            x = queue.pop(0)
            comp.append(x)
            for y in sorted(adj[x], key=lambda k: (-len(adj[k]), k)):
                if y not in seen:
                    seen.add(y)
                    queue.append(y)
        comps.append(comp)
    loose = sorted(k for k in tabs if k not in seen)

    boxes, x0, y_max = {}, PAD, PAD
    rows_of = {k: _box_rows(tabs[k]) for k in tabs}

    def box(k, x, y):
        t = tabs[k]
        h = HEAD_H + ROW_H * len(rows_of[k]) + 8
        sub = (t["database"] + ("." + t["schema"] if t["schema"] else "")) if t["database"] else (t["schema"] or "database not stated")
        if t["database_note"]:
            sub += " (" + t["database_note"] + ")"
        boxes[k] = {"x": x, "y": y, "w": BOX_W, "h": h, "title": t["name"], "sub": sub,
                    "note": t["database_note"], "context": not t["used"], "rows": rows_of[k], "key": k}
        return h

    for comp in comps:  # layers by distance from the most connected table
        depth = {comp[0]: 0}
        queue = [comp[0]]
        while queue:
            x = queue.pop(0)
            for y in adj[x]:
                if y not in depth:
                    depth[y] = depth[x] + 1
                    queue.append(y)
        layers = defaultdict(list)
        for k in comp:
            layers[depth[k]].append(k)
        bottom = y_max
        right = x0
        for d in sorted(layers):
            y = y_max
            for k in layers[d]:
                y += box(k, x0 + d * (BOX_W + GAP_X), y) + GAP_Y
            bottom = max(bottom, y)
            right = max(right, x0 + d * (BOX_W + GAP_X) + BOX_W)
        y_max = bottom + GAP_Y
    if loose:
        per_row = max(1, min(4, math.ceil(len(loose) ** 0.5) + 1))
        x, y, row_h, n = x0, y_max, 0, 0
        for k in loose:
            h = box(k, x, y)
            row_h = max(row_h, h)
            n += 1
            if n % per_row == 0:
                x, y, row_h = x0, y + row_h + GAP_Y, 0
            else:
                x += BOX_W + GAP_X // 2
        y_max = y + row_h + GAP_Y

    def anchor(k, col, side):
        b = boxes[k]
        idx = next((i for i, r in enumerate(b["rows"]) if r["name"].lower() == (col or "").lower() and not r.get("more")), None)
        y = b["y"] + HEAD_H + (idx + 0.5) * ROW_H if idx is not None else b["y"] + HEAD_H / 2
        return (b["x"] + (b["w"] if side == "r" else 0), y)

    edges = []
    for r in m["relations"]:
        a, b = r["from"].lower(), r["to"].lower()
        if a not in boxes or b not in boxes:
            continue
        if a == b:
            continue
        ba, bb = boxes[a], boxes[b]
        if ba["x"] == bb["x"]:  # same layer: loop out on the right-hand side
            p0, p3 = anchor(a, r["from_col"], "r"), anchor(b, r["to_col"], "r")
            bulge = 60
            p1, p2 = (p0[0] + bulge, p0[1]), (p3[0] + bulge, p3[1])
            sa, sb = "r", "r"
        else:
            sa, sb = ("r", "l") if ba["x"] < bb["x"] else ("l", "r")
            p0, p3 = anchor(a, r["from_col"], sa), anchor(b, r["to_col"], sb)
            dx = (p3[0] - p0[0]) / 2
            p1, p2 = (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1])
        many = one = None
        if r["dir"] == "fk":
            many, one = "N", "1"  # the table holding the foreign key is the many side
        edges.append({"p": [p0, p1, p2, p3], "kind": r["kind"], "from_mark": many, "to_mark": one,
                      "label": f"{r['from_col']} = {r['to_col']}" if r["from_col"] and r["to_col"] else r["from_col"] or r["to_col"]})
    width = max([b["x"] + b["w"] for b in boxes.values()] + [400]) + PAD + 60
    height = max([b["y"] + b["h"] for b in boxes.values()] + [100]) + PAD
    return {"w": int(width), "h": int(height), "boxes": list(boxes.values()), "edges": edges}


# ---- renderers -----------------------------------------------------------------------------

COL = {"bg": "#ffffff", "head": "#1f3a5f", "head_ctx": "#7a8591", "line": "#44546a", "text": "#1d2733", "dim": "#6b7785",
       "key": "#a8620a", "ok": "#2d6a3e", "edge": "#2c5d8f", "guess": "#b0651a", "row_alt": "#f3f6fa"}
TAGS = {"W": "#b3261e", "R": "#2d6a3e", "F": "#1f5fbf", "J": "#7b3fa0"}


def _bez(p, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    u = 1 - t
    return (u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
            u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3)


def to_svg(s: dict) -> str:
    e = html.escape
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s["w"]} {s["h"]}" width="{s["w"]}" height="{s["h"]}" '
         f'font-family="Segoe UI, Arial, sans-serif" font-size="12">',
         f'<rect width="100%" height="100%" fill="{COL["bg"]}"/>']
    for ed in s["edges"]:
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = ed["p"]
        col = COL["guess"] if ed["kind"] == "inferred" else COL["edge"]
        dash = ' stroke-dasharray="7 5"' if ed["kind"] == "inferred" else ""
        o.append(f'<path d="M{x0:.1f},{y0:.1f} C{x1:.1f},{y1:.1f} {x2:.1f},{y2:.1f} {x3:.1f},{y3:.1f}" fill="none" '
                 f'stroke="{col}" stroke-width="1.8"{dash}><title>{e(ed["label"])} ({ed["kind"]})</title></path>')
        for pt, ctl, mark in ((ed["p"][0], ed["p"][1], ed["from_mark"]), (ed["p"][3], ed["p"][2], ed["to_mark"])):
            o.append(f'<circle cx="{pt[0]:.1f}" cy="{pt[1]:.1f}" r="3.5" fill="{col}"/>')
            if mark:
                tx = pt[0] + (6 if ctl[0] > pt[0] else -14)
                o.append(f'<text x="{tx:.1f}" y="{pt[1] - 6:.1f}" font-weight="700" fill="{col}">{mark}</text>')
    for b in s["boxes"]:
        head = COL["head_ctx"] if b["context"] else COL["head"]
        o.append(f'<g><rect x="{b["x"]}" y="{b["y"]}" width="{b["w"]}" height="{b["h"]}" rx="6" fill="#fff" stroke="{COL["line"]}" stroke-width="1.4"/>'
                 f'<path d="M{b["x"]},{b["y"] + HEAD_H} V{b["y"] + 6} Q{b["x"]},{b["y"]} {b["x"] + 6},{b["y"]} H{b["x"] + b["w"] - 6} '
                 f'Q{b["x"] + b["w"]},{b["y"]} {b["x"] + b["w"]},{b["y"] + 6} V{b["y"] + HEAD_H} Z" fill="{head}"/>'
                 f'<text x="{b["x"] + 10}" y="{b["y"] + 19}" fill="#fff" font-weight="700" font-size="14">{e(b["title"])}</text>'
                 f'<text x="{b["x"] + 10}" y="{b["y"] + 36}" fill="#d6e2f0" font-size="11">{e(_clip(b["sub"], 37))}'
                 f'{"  (not used by the bots)" if b["context"] else ""}</text>')
        for i, r in enumerate(b["rows"]):
            y = b["y"] + HEAD_H + i * ROW_H
            if i % 2:
                o.append(f'<rect x="{b["x"] + 1}" y="{y}" width="{b["w"] - 2}" height="{ROW_H}" fill="{COL["row_alt"]}"/>')
            if r["key"]:
                o.append(f'<text x="{b["x"] + 8}" y="{y + 13}" fill="{COL["key"]}" font-weight="700" font-size="10">{e(r["key"])}</text>')
            style = ' font-style="italic"' if r.get("more") else ""
            o.append(f'<text x="{b["x"] + 40}" y="{y + 13}" fill="{COL["dim"] if r.get("more") else COL["text"]}"{style}>'
                     f'{e(_clip(r["name"], 26))}</text>')
            if r["type"]:
                o.append(f'<text x="{b["x"] + 170}" y="{y + 13}" fill="{COL["dim"]}" font-size="10">{e(_clip(r["type"], 12))}</text>')
            for j, u in enumerate(r["uses"]):
                o.append(f'<text x="{b["x"] + b["w"] - 12 - 12 * (len(r["uses"]) - 1 - j)}" y="{y + 13}" fill="{TAGS[u]}" font-weight="700" '
                         f'font-size="10" text-anchor="middle">{u}</text>')
        o.append("</g>")
    o.append("</svg>")
    return "".join(o)


def _clip(t, n):
    return t if len(t) <= n else t[: n - 1] + "…"


def _font(size, bold=False):
    from PIL import ImageFont

    for p in (("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
              ("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
              ("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
              "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def to_png(s: dict, path: str, scale: float = 1.6) -> str:
    from PIL import Image, ImageDraw

    W, H = int(s["w"] * scale), int(s["h"] * scale)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f10, f11, f12, f14 = (_font(int(v * scale), v == 14) for v in (10, 11, 12, 14))
    fb10 = _font(int(10 * scale), True)

    def sc(p):
        return (p[0] * scale, p[1] * scale)

    for ed in s["edges"]:
        col = COL["guess"] if ed["kind"] == "inferred" else COL["edge"]
        pts = [sc(_bez(ed["p"], i / 60)) for i in range(61)]
        for i in range(60):
            if ed["kind"] == "inferred" and (i // 3) % 2:
                continue
            d.line([pts[i], pts[i + 1]], fill=col, width=max(2, int(1.8 * scale)))
        for pt, ctl, mark in ((ed["p"][0], ed["p"][1], ed["from_mark"]), (ed["p"][3], ed["p"][2], ed["to_mark"])):
            x, y = sc(pt)
            r = 3.5 * scale
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
            if mark:
                d.text((x + (6 if ctl[0] > pt[0] else -14) * scale, y - 16 * scale), mark, fill=col, font=fb10)
    for b in s["boxes"]:
        x, y, w, h = (v * scale for v in (b["x"], b["y"], b["w"], b["h"]))
        d.rounded_rectangle([x, y, x + w, y + h], radius=6 * scale, fill="white", outline=COL["line"], width=max(2, int(1.4 * scale)))
        d.rounded_rectangle([x, y, x + w, y + HEAD_H * scale], radius=6 * scale, fill=COL["head_ctx"] if b["context"] else COL["head"])
        d.rectangle([x, y + 20 * scale, x + w, y + HEAD_H * scale], fill=COL["head_ctx"] if b["context"] else COL["head"])
        d.text((x + 10 * scale, y + 6 * scale), b["title"], fill="white", font=f14)
        d.text((x + 10 * scale, y + 26 * scale), _clip(b["sub"], 37) + ("  (not used by the bots)" if b["context"] else ""),
               fill="#d6e2f0", font=f11)
        for i, r in enumerate(b["rows"]):
            ry = y + (HEAD_H + i * ROW_H) * scale
            if i % 2:
                d.rectangle([x + scale, ry, x + w - scale, ry + ROW_H * scale], fill=COL["row_alt"])
            if r["key"]:
                d.text((x + 8 * scale, ry + 3 * scale), r["key"], fill=COL["key"], font=fb10)
            d.text((x + 40 * scale, ry + 2 * scale), _clip(r["name"], 26), fill=COL["dim"] if r.get("more") else COL["text"], font=f12)
            if r["type"]:
                d.text((x + 170 * scale, ry + 4 * scale), _clip(r["type"], 12), fill=COL["dim"], font=f10)
            for j, u in enumerate(r["uses"]):
                d.text((x + w - (12 + 12 * (len(r["uses"]) - 1 - j) + 4) * scale, ry + 3 * scale), u, fill=TAGS[u], font=fb10)
    img.save(path, "PNG")
    return path


LEGEND = ("W written, R read, F filtered or ordered on, J used in a join. PK/FK come from a loaded schema script; "
          "key? is a guess from the column name. Solid line: confirmed by a foreign key or a SQL join. "
          "Dashed line: inferred from matching id-like column names, not confirmed. Grey header: table is "
          "referenced by a foreign key but not used by the bots.")
