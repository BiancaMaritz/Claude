"""Work out what is still missing: bots to provide, and documented steps the code does not cover.

The code is the source of truth, so only what the code itself shows missing is a bot to provide:
  Certain   a bot calls it, or the export manifest lists it, but its file is not in the export.
Anything found only in documentation or table exports (a documented bot or step with no code, a table no
SQL touches) is assumed retired. It is still worked out, but kept apart as "documentation only":
  Likely    documentation lists a bot component that no bot in the code matches.
  Possible  an exported table that no bot in the code touches.

Documented process steps are matched to the code by shared words (bot names, step labels, comments,
variables, text constants, packages used). That is a heuristic and the result says so: every finding
carries the evidence it rests on, and a match is never presented as proof.
"""
from __future__ import annotations

import json
import re

from .analyze import walk
from .docfill import documented_bots, same_thing, Docs
from .docparse import letters

LOW_INFO = {
    "the", "and", "for", "with", "from", "that", "this", "then", "into", "each", "all", "any", "are", "was", "will",
    "shall", "must", "should", "can", "may", "not", "out", "per", "via", "has", "have", "its", "bot", "bots", "process",
    "step", "steps", "data", "file", "files", "document", "documents", "list", "item", "items", "number", "name", "date",
    "type", "value", "new", "old", "current", "use", "using", "used", "one", "two", "set", "get", "when", "where",
    "which", "after", "before", "once", "also", "only", "than", "them", "these", "those", "following", "relevant",
    "respective", "required", "related", "specified", "per", "per", "does", "did", "being", "been", "both", "such",
    "will", "would", "able", "else", "other", "same", "more", "most", "some", "there", "their", "they", "next", "last"}
SYNONYMS = {"signin": "login", "logon": "login", "logoff": "logout", "signout": "logout", "retriev": "download",
            "fetch": "download", "pull": "download"}
PACKAGE_WORDS = {
    "Browser": ["browser", "web", "website"], "Excel_MS": ["excel", "spreadsheet", "workbook"],
    "Email": ["email", "mail", "mailbox", "outlook", "inbox"], "Database": ["database", "sql", "table"],
    "PDF": ["pdf"], "OCR": ["ocr"], "CsvTxt": ["csv", "text"], "Rest": ["api", "rest", "service"],
    "Folder": ["folder", "directory"], "File": ["folder"], "Credential": ["credential", "password"],
    "Json": ["json"], "Datetime": ["date"]}
CERTAIN, LIKELY, POSSIBLE = "Certain", "Likely", "Possible"
ORDER = {CERTAIN: 0, LIKELY: 1, POSSIBLE: 2}


def _stem(w: str) -> str:
    w = w.lower()
    if len(w) > 6 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 5 and w.endswith("ied"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 6 and w.endswith("ion"):
        w = w[:-3]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return SYNONYMS.get(w, w)


def words(text: str, keep_low=False):
    """Stemmed words of a text, in order, without repeats."""
    text = re.sub(r"\blog[\s-]?in\b", "login", str(text), flags=re.I)
    text = re.sub(r"\blog[\s-]?out\b", "logout", text, flags=re.I)
    text = re.sub(r"\bsign[\s-]?in\b", "login", text, flags=re.I)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)  # camelCase
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", text):
        s = _stem(w)
        if len(s) < 3 or (not keep_low and (w.lower() in LOW_INFO or s in LOW_INFO)) or s.isdigit():
            continue
        if s not in out:
            out.append(s)
    return out


def _strings(node):
    """Short text constants in an action's attributes."""
    out = []

    def rec(v):
        if isinstance(v, dict):
            for k in ("string", "expression"):
                if isinstance(v.get(k), str) and 2 < len(v[k]) < 160:
                    out.append(v[k])
            for x in v.values():
                if isinstance(x, (dict, list)):
                    rec(x)
        elif isinstance(v, list):
            for x in v:
                rec(x)

    rec(node.get("attributes", []))
    return out


def code_vocabulary(ex, a):
    """{bot path: set of stemmed words that describe what the bot does}."""
    vocab = {}
    for p, bot in ex.bots.items():
        d = a["per"][p]
        w = set(words(bot.name + " " + bot.folder.replace("/", " ")))
        w |= set(words(" ".join(label for _, label in d["steps"])))
        w |= set(words(" ".join(d["comments"])))
        w |= set(words(" ".join(v["name"] for v in bot.variables)))
        w |= set(words(bot.description))
        for pkg in d["packages"]:
            for x in PACKAGE_WORDS.get(pkg, []):
                w |= set(words(x))
        w |= set(words(" ".join(d["tables"])))
        if bot.data:
            lits = []
            for n, _ in walk(bot.nodes):
                if n.get("commandName") != "runTask":  # a call's file path would copy the callee's name into the caller
                    lits += _strings(n)
                if len(lits) > 1500:
                    break
            w |= set(words(" ".join(lits)))
        vocab[p] = w
    return vocab


# ------------------------------------------------------------ documented steps ---

def documented_steps(docs):
    """[{doc, page, scenario, step, text}] from the process flow of every documentation file."""
    out = []
    for d in docs:
        sec = d["sections"].get("process_flow")
        if not sec:
            continue
        scenario, n = "", 0
        for kind, payload in sec["blocks"]:
            if kind == "table":
                for r in payload:
                    if not any(r):
                        continue
                    if re.match(r"scenario", r[0] or "", re.I) and not any(r[1:]):
                        scenario = r[0].strip()
                        continue
                    if letters(r[0]) in ("step", "") and len(r) > 1 and letters(r[-1]) == "description":
                        continue  # header row
                    text = re.sub(r"\s+", " ", (r[-1] if len(r) > 1 else r[0]).replace("•", " ")).strip()
                    if text:
                        out.append({"doc": d["name"], "page": sec["page"], "scenario": scenario,
                                    "step": (r[0] or "").strip() or str(len(out) + 1), "text": text})
            elif kind == "p" and len(payload) > 20:
                n += 1
                out.append({"doc": d["name"], "page": sec["page"], "scenario": scenario, "step": f"p{n}", "text": payload})
            elif kind == "ul":
                for item in payload:
                    n += 1
                    out.append({"doc": d["name"], "page": sec["page"], "scenario": scenario, "step": f"b{n}", "text": item})
    return out


def _original(text, stems):
    """Show evidence in the document's own words, not stems ('shoprit' becomes 'Shoprite')."""
    plain = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", re.sub(r"\blog[\s-]?(in|out)\b", lambda m: "log" + m.group(1), text, flags=re.I))
    found = {}
    for w in re.findall(r"[A-Za-z0-9]+", plain):
        found.setdefault(_stem(w), w.lower())
    return [found.get(x, x) for x in stems]


def word_weights(vocab):
    """How much a matching word proves. A word nearly every bot uses (folder, email) proves little;
    a word few bots use, or none, is what tells the processes apart."""
    n = len(vocab)
    df = {}
    for w in vocab.values():
        for t in w:
            df[t] = df.get(t, 0) + 1
    return lambda t: 0.3 if n >= 8 and df.get(t, 0) >= 0.3 * n else 1.0


def judge_step(step, vocab, ex, weight):
    """Match one documented step against every bot's vocabulary. Returns the step with its verdict."""
    toks = words(step["text"])[:12]
    res = dict(step, tokens=toks, status="Not checkable", matched=[], best=None, score=0.0)
    if len(toks) < 2:
        return res
    total = sum(weight(t) for t in toks)
    best, best_m, best_got = None, [], 0.0
    for p, w in vocab.items():
        m = [t for t in toks if t in w]
        got = sum(weight(t) for t in m)
        if (got, len(m)) > (best_got, len(best_m)):
            best, best_m, best_got = p, m, got
    spread = [t for t in toks if any(t in w for w in vocab.values())]
    spread_got = sum(weight(t) for t in spread)
    r = best_got / total
    if best_got >= 2 and r >= 0.6:
        status = "Covered"
    elif (best_got >= 1.0 and r >= 0.4) or (spread_got >= 2 and spread_got / total >= 0.6):
        status = "Partly"
    else:
        status = "Not found"
    return dict(res, status=status, matched=_original(step["text"], best_m if best_m else spread), score=round(r, 2),
                best=ex.bots[best].name if best and best_m else None)


# ----------------------------------------------------------------------- main ---

def analyse_gaps(ex, a, docs, tables):
    """Everything the Analysis tab shows, from the stored project."""
    D = Docs(docs)
    entries = {}  # key -> {name, confidence, reasons, where, documented_as}

    def add(name, conf, reason, where="", documented_as=""):
        key = next((k for k, e in entries.items() if same_thing(name, e["name"])), None) or letters(name) or name
        e = entries.setdefault(key, {"name": name, "confidence": conf, "reasons": [], "where": [], "documented_as": ""})
        if ORDER[conf] < ORDER[e["confidence"]]:
            e["confidence"] = conf
        if reason not in e["reasons"]:
            e["reasons"].append(reason)
        if where and where not in e["where"]:
            e["where"].append(where)
        if documented_as and not e["documented_as"]:
            e["documented_as"] = documented_as

    # Certain: the code calls a bot that is not in the export, or the manifest lists a bot without its file.
    callers = {}
    for p, cs in a["calls"].items():
        for c in cs:
            if c not in ex.bots:
                callers.setdefault(c, []).append(ex.bots[p].name)
    for path, who in sorted(callers.items()):
        add(path.rsplit("/", 1)[-1], CERTAIN, f"Called by {', '.join(sorted(set(who))[:4])}, but it is not in the export.",
            where=path.replace("Automation Anywhere/Bots/", "Bots/"))
    for p, bot in sorted(ex.bots.items()):
        if bot.data is None and ex.has_bot_files:
            add(bot.name, CERTAIN, "Listed in the export manifest, but its bot file is not in the export.",
                where=p.replace("Automation Anywhere/Bots/", "Bots/"))

    # Likely: documented as a bot component, with no matching bot in the code.
    code_names = [f"{b.folder.rsplit('/', 1)[-1]} {b.name}" for b in ex.bots.values()]
    documented = documented_bots(D)
    found = 0
    missing_docs = []
    for name, d in documented:
        if any(same_thing(name, c) for c in code_names):
            found += 1
        else:
            row = next((r for dd, r in D.rows("bot_components") if dd is d and name.lower() in r[-1].lower()), None)
            role = row[0] if row and len(row) > 1 else ""
            missing_docs.append((name, d, role))
            page = d["sections"]["bot_components"]["page"]
            add(name, LIKELY, f"Listed as a bot component in {d['name']}" + (f" ({role})" if role else "") + ", no bot in the code matches.",
                where=f"{d['name']}" + (f", page {page}" if page else ""), documented_as=role)

    # Documented steps against the code.
    steps = []
    notes = []
    if not ex.bots:
        notes.append("No code is loaded yet, so documented steps cannot be compared with it.")
    elif D.has("process_flow"):
        vocab = code_vocabulary(ex, a)
        weight = word_weights(vocab)
        steps = [judge_step(s, vocab, ex, weight) for s in documented_steps(docs)]
        for s in steps:
            if s["status"] != "Not found":
                continue
            stoks = set(s["tokens"])

            def overlap(cand):
                bt = set(words(cand[0]))
                return (len(bt & stoks) / len(bt), len(bt & stoks)) if bt else (0, 0)

            cands = [c for c in missing_docs if overlap(c)[0] >= 0.5]
            if cands:
                name, d, _ = max(cands, key=overlap)  # the documented bot whose name the step's words fit best
                s["missing_bot"] = name
                add(name, LIKELY, f"Documented step {s['step']} in {s['doc']} (\"{s['text'][:70]}...\") has no match in the code.")
            else:
                s["missing_bot"] = None

    # Possible: an exported table that no bot in the code reads or writes.
    from .document import _plain

    in_code = {_plain(n) for n in a["tables"]}
    for t in tables:
        if _plain(t["name"]) not in in_code:
            add(f"A bot that uses table {t['name']}", POSSIBLE,
                f"The exported table {t['name']} is not referenced by any SQL in the code, so whichever bot fills or reads it may be missing.",
                where=f"Table export {t['source']}")

    everything = sorted(entries.values(), key=lambda e: (ORDER[e["confidence"]], e["name"].lower()))
    # The code is the source of truth. Only what the code itself shows missing is a bot to provide; anything
    # found only in documentation or table exports is assumed retired and kept apart, for reference.
    bots = [e for e in everything if e["confidence"] == CERTAIN]
    doc_only = {"bots": [e for e in everything if e["confidence"] == LIKELY],
                "tables": [e for e in everything if e["confidence"] == POSSIBLE], "steps": steps}
    st = lambda s: sum(1 for x in steps if x["status"] == s)
    return {
        "bots": bots, "doc_only": doc_only, "notes": notes,
        "summary": {"to_provide": len(bots), "doc_only_bots": len(doc_only["bots"]),
                    "doc_only_tables": len(doc_only["tables"]),
                    "documented_bots": len(documented), "documented_found": found,
                    "steps_total": len(steps), "steps_covered": st("Covered"), "steps_partly": st("Partly"),
                    "steps_missing": st("Not found"), "steps_unchecked": st("Not checkable"),
                    "has_code": bool(ex.bots), "has_docs": bool(docs)},
    }


def to_csv(result):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Bot to provide", "Confidence", "Why", "Where to find it"])
    for e in result["bots"]:  # only bots the code itself shows missing
        w.writerow([e["name"], e["confidence"], " | ".join(e["reasons"]), "; ".join(e["where"])])
    return buf.getvalue()
