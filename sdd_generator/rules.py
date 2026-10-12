"""Compare rules written in the documentation with what the code actually does.

The code is the source of truth. Documentation that has no counterpart in the code is assumed to be
retired, so it is never reported as missing. What is reported is a rule that exists on both sides and
reads differently: a time limit, a retry count, a list of codes, a file naming pattern.

Every difference can be accepted ("the change is valid") with a note. An acceptance is tied to the exact
pair of values it was given for, so if the documentation or the code later changes, the difference
reopens by itself.

Only facts that can be read exactly are compared. Matching a documented rule to a place in the code is
by shared words and is only a pointer to evidence, never proof.
"""
from __future__ import annotations

import hashlib
import json
import re

from .analyze import attr, cond_text, step_label, val, walk
from .gaps import _strings, words
from .docparse import letters

UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
                "week": 604800, "month": 2592000, "year": 31536000}
SQL_UNITS = {"second": "second", "ss": "second", "s": "second", "minute": "minute", "mi": "minute", "n": "minute",
             "hour": "hour", "hh": "hour", "day": "day", "dd": "day", "d": "day", "week": "week", "wk": "week",
             "ww": "week", "month": "month", "mm": "month", "m": "month", "year": "year", "yy": "year", "yyyy": "year"}
NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "twelve": 12, "fourteen": 14, "twenty": 20, "thirty": 30}
TIME_RE = re.compile(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fourteen|twenty|thirty)"
                     r"[\s-]*(?:(?:business|working|calendar|full)\s+)?"
                     r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)\b", re.I)
COUNT_RE = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:times|retries|attempts|tries)\b"
                      r"|\b(?:retry|retries|attempts?)\s*(?:of|up to|limit(?: of)?|max(?:imum)?(?: of)?)?\s*(\d+)\b", re.I)
ID_RE = re.compile(r"\b\d{6,}\b|\b[A-Z]{1,3}\d{4,}\b")
FILE_RE = re.compile(r"([^/\\\s\"']+)\.(pdf|csv|xlsx?|txt|zip|json|xml|docx?)\b", re.I)
RETRY_WORDS = re.compile(r"retry|retries|attempt|tries", re.I)
SQL_DATEADD = re.compile(r"DATEADD\s*\(\s*(\w+)\s*,\s*-?\s*(\d+)", re.I)
SQL_INTERVAL = re.compile(r"INTERVAL\s+'?(\d+)\s*(second|minute|hour|day|week|month|year)", re.I)
SQL_RETRY = re.compile(r"(?:retry\w*|attempt\w*|tries)\s*(?:<=|>=|<|>|=)\s*(\d+)", re.I)


def _num(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else NUMWORDS.get(tok)


def _time_label(n, unit):
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _is_date_like(s: str) -> bool:
    return len(s) == 8 and s[:2] in ("19", "20") and 1 <= int(s[4:6]) <= 12 and 1 <= int(s[6:]) <= 31


def times_in(text):
    """{seconds: 'N units'} for every duration written in a text."""
    out = {}
    for m in TIME_RE.finditer(text or ""):
        n = _num(m.group(1))
        unit = m.group(2).lower().rstrip("s")
        unit = {"sec": "second", "min": "minute", "hr": "hour"}.get(unit, unit)
        if n and unit in UNIT_SECONDS:
            out[n * UNIT_SECONDS[unit]] = _time_label(n, unit)
    return out


def counts_in(text):
    out = set()
    for m in COUNT_RE.finditer(text or ""):
        n = _num(m.group(1)) if m.group(1) else int(m.group(2))
        if n:
            out.add(n)
    return out


def ids_in(text):
    return {m for m in ID_RE.findall(text or "") if not _is_date_like(m)}


def files_in(text):
    """{(frozenset(name tokens), extension): display name} for file names written in a text."""
    out = {}
    for m in FILE_RE.finditer(text or ""):
        name = m.group(1)
        toks = frozenset(t for t in re.split(r"[^a-z0-9]+", name.lower().replace("$", " ")) if len(t) > 1)
        if toks:
            out[(toks, m.group(2).lower())] = f"{name}.{m.group(2).lower()}"
    return out


# ------------------------------------------------------------------ code side ---

def code_units(ex):
    """Evidence units: one per (bot, step), holding the exact facts found there and the words describing it."""
    units = []
    for p, bot in sorted(ex.bots.items()):
        if not bot.data:
            continue
        by_label = {}
        for n, stack in walk(bot.nodes):
            steps = [s for s in stack if s.get("commandName") == "step"]
            if n.get("commandName") == "step":
                steps.append(n)
            label = step_label(steps[-1]) if steps else "(outside steps)"
            u = by_label.setdefault(label, {"bot": bot.name, "path": p, "step": label, "texts": [label] if label else [],
                                            "times": {}, "counts": set(), "ids": set(), "files": {}, "vars": set(), "comments": []})
            texts = _strings(n)
            pkg, cmd = n.get("packageName"), n.get("commandName")
            if pkg == "Comment":
                texts.append(val(attr(n, "comment")))
                u["comments"].append(val(attr(n, "comment")))
            if pkg == "Datetime" and cmd in ("add", "subtract"):
                v, unit = val(attr(n, "val")), val(attr(n, "unit")).lower().rstrip("s")
                if v.isdigit() and unit in UNIT_SECONDS:
                    u["times"][int(v) * UNIT_SECONDS[unit]] = _time_label(int(v), unit)
            q = attr(n, "query")
            if pkg == "Database" and q:
                sql = val(q)
                texts.append(sql)
                for m in SQL_DATEADD.finditer(sql):
                    unit = SQL_UNITS.get(m.group(1).lower())
                    if unit:
                        u["times"][int(m.group(2)) * UNIT_SECONDS[unit]] = _time_label(int(m.group(2)), unit)
                for m in SQL_INTERVAL.finditer(sql):
                    u["times"][int(m.group(1)) * UNIT_SECONDS[m.group(2).lower()]] = _time_label(int(m.group(1)), m.group(2).lower())
                u["counts"] |= {int(x) for x in SQL_RETRY.findall(sql)}
            if cmd in ("if", "elseIf", "while"):
                text = cond_text(n)
                if text:
                    texts.append(text)
                    if RETRY_WORDS.search(text) or RETRY_WORDS.search(label):
                        u["counts"] |= {int(x) for x in re.findall(r"(?:=|equals|than|least|most)\s*\{?(\d+)\b", text)}
            rt = n.get("returnTo") or {}
            if rt.get("variableName"):
                u["vars"].add(rt["variableName"])
            for t in texts:
                u["texts"].append(t)
                u["times"].update(times_in(t) if len(t) < 200 else {})
                u["counts"] |= counts_in(t)
                u["ids"] |= ids_in(t)
                u["files"].update(files_in(t))
        for u in by_label.values():
            u["words"] = set(words(" ".join([u["bot"], *u["texts"], *u["vars"]])))
            # what a person wrote to describe the step counts for more than words found in its constants
            u["label_words"] = set(words(" ".join([u["step"] if u["step"] != "(outside steps)" else "", *u["comments"], *u["vars"]])))
            u["label_has"] = {"time": bool(times_in(u["step"])), "count": bool(counts_in(u["step"]))}
            units.append(u)
    return units


# ------------------------------------------------------------- documentation ---

def documented_rules(docs):
    """Rule-like statements from documentation: business rules, requirements, assumptions, flow steps."""
    out = []
    for d in docs:
        for key, label in (("business_rules", "Business rule"), ("functional_requirements", "Requirement"),
                           ("process_flow", "Process step"), ("assumptions", "Assumption"),
                           ("process_description", "Process description")):
            sec = d["sections"].get(key)
            if not sec:
                continue
            scenario, n = "", 0
            for kind, payload in sec["blocks"]:
                base = {"doc": d["name"], "page": sec["page"], "section": label}
                if kind == "table":
                    for r in payload:
                        if not any(r):
                            continue
                        if key == "process_flow" and re.match(r"scenario", r[0] or "", re.I) and not any(r[1:]):
                            scenario = r[0].strip()
                            continue
                        if letters(r[0]) in ("number", "nr", "step", "") and len(r) > 1 and letters(r[-1]) == "description":
                            continue
                        text = " ".join(x for x in ((r[1] if key in ("business_rules", "functional_requirements") and len(r) > 1 else r[-1]),) if x)
                        text = re.sub(r"\s+", " ", text.replace("•", " ").replace("\n", " ")).strip()
                        rid = (r[0] or "").strip() if key != "process_flow" else f"{scenario or 'flow'} step {(r[0] or '').strip()}"
                        if text and len(text) > 8:
                            out.append(dict(base, id=rid or label, text=text))
                elif kind == "ul":
                    for item in payload:
                        n += 1
                        out.append(dict(base, id=f"{label} {n}", text=item))
                elif kind == "p" and key in ("process_description", "assumptions"):
                    n += 1
                    out.append(dict(base, id=f"{label} {n}", text=payload))
    return out


# --------------------------------------------------------------------- compare ---

def _fid(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def _sig(doc_says, code_says):
    return hashlib.sha1(json.dumps([doc_says, code_says], sort_keys=True).encode()).hexdigest()[:12]


def _weights(units):
    n = len(units)
    df = {}
    for u in units:
        for t in u["words"]:
            df[t] = df.get(t, 0) + 1
    return lambda t: 0.3 if n >= 8 and df.get(t, 0) >= 0.3 * n else 1.0


UNIT_STEMS = set(words("second minute hour day week month year", keep_low=True))


def _anchor(rule_words, candidates, weight, kind):
    """(unit, link) for the step whose words best fit a rule, or (None, None). A pointer, not proof.

    Words of the step's own label, comments and variable names count in full; words found only in its
    constants count a third. Units of time are left out: "1 week" in the documentation and "7 days" in
    the code are the very thing being compared, so they must not be what ties the two together.
      strong  two or more shared words, at least one from the label side.
      weak    one shared label word and a step that holds a duration or count of this kind (best when its label names it).
    """
    rule_words = [t for t in rule_words if t not in UNIT_STEMS]
    total = sum(weight(t) for t in rule_words) or 1.0
    best, best_key = None, (0, 0.0)
    for u in candidates:
        strong = [t for t in rule_words if t in u["label_words"]]
        weak = [t for t in rule_words if t in u["words"] and t not in u["label_words"]]
        if not strong:
            continue
        got = sum(weight(t) for t in strong) + 0.35 * sum(weight(t) for t in weak)
        if len(strong) + len(weak) >= 2 and got >= 1.0 and got / total >= 0.25:
            key, link = (2, got), "strong"
        elif len(strong) == 1:
            key, link = (1 + (0.5 if u["label_has"][kind] else 0), got), "weak"
        else:
            continue
        if key > best_key:
            best, best_key, best_link = u, key, link
    return (best, best_link) if best else (None, None)


def analyse_rules(ex, docs, acceptances=None):
    """Compare documented rules with the code. Returns {findings, agreements, summary}.

    findings: differences, each Open, Accepted or Changed (reopened because a side changed).
    agreements: documented facts the code confirms, kept so the check can be seen working.
    """
    acceptances = acceptances or {}
    rules = documented_rules(docs)
    empty = {"findings": [], "agreements": [], "summary": {"rules": len(rules), "checked": 0, "agree": 0, "open": 0,
                                                              "accepted": 0, "changed": 0, "weak": 0}}
    if not rules or not ex.bots:
        return empty
    units = code_units(ex)
    weight = _weights(units)
    bot_ids, bot_files = {}, {}
    for u in units:
        bot_ids.setdefault(u["path"], set()).update(u["ids"])
        bot_files.setdefault(u["path"], {}).update(u["files"])
    doc_all_ids = set()
    for r in rules:
        doc_all_ids |= ids_in(r["text"])
    findings, agreements = [], []

    def record(rule, kind, subject, doc_says, code_says, where, detail, differs, link="strong"):
        fid = _fid(rule["doc"], rule["id"], kind, subject)
        item = {"id": fid, "kind": kind, "rule": rule, "doc_says": doc_says, "code_says": code_says, "where": where,
                "detail": detail, "link": link}
        if not differs:
            agreements.append(item)
            return
        sig = _sig(doc_says, code_says)
        acc = acceptances.get(fid)
        item.update(sig=sig, status="Open", note="", accepted_at="")
        if acc:
            item.update(status="Accepted" if acc.get("sig") == sig else "Changed", note=acc.get("note", ""),
                        accepted_at=acc.get("at", ""))
        findings.append(item)

    for rule in rules:
        text = rule["text"]
        rw = words(text)[:14]
        # ---- durations and retry counts: compared at the step whose words fit the rule
        for kind, doc_vals, field in (("time", times_in(text), "times"), ("count", counts_in(text), "counts")):
            if not doc_vals:
                continue
            cands = [u for u in units if u[field]]
            u, link = _anchor(rw, cands, weight, kind)
            if not u:
                continue  # nothing in the code to compare with: documentation-only, assumed retired
            where = f"{u['bot']} > {u['step']}"
            if kind == "time":
                doc_disp = ", ".join(sorted(doc_vals.values()))
                code_disp = ", ".join(sorted(u["times"].values()))
                missing = set(doc_vals) - set(u["times"])
                subject = "time"
            else:
                doc_disp = ", ".join(f"{n} times" for n in sorted(doc_vals))
                code_disp = ", ".join(f"{n} times" for n in sorted(u["counts"]))
                missing = doc_vals - u["counts"]
                subject = "count"
            record(rule, kind, subject, doc_disp, code_disp, where,
                   [f"Documentation: {doc_disp}", f"Code: {code_disp}"], bool(missing), link)
        # ---- lists of codes: compared in the bot that contains them
        d_ids = ids_in(text)
        if d_ids:
            hits = sorted(((len(d_ids & ids), p) for p, ids in bot_ids.items() if d_ids & ids), reverse=True)
            if hits:
                path = hits[0][1]
                shapes = {len(i) if i.isdigit() else re.sub(r"\d", "9", i) for i in d_ids}
                same_shape = lambda i: (len(i) if i.isdigit() else re.sub(r"\d", "9", i)) in shapes
                code_ids = {i for i in bot_ids[path] if same_shape(i)}
                only_code = sorted(i for i in code_ids - doc_all_ids)
                only_doc = sorted(d_ids - bot_ids[path])
                name = ex.bots[path].name
                differs = bool(only_code or only_doc)
                detail = []
                if only_code:
                    detail.append("In the code only: " + ", ".join(only_code))
                if only_doc:
                    detail.append("In the documentation only: " + ", ".join(only_doc))
                record(rule, "codes", "ids", f"{len(d_ids)} codes: {', '.join(sorted(d_ids))}",
                       f"{len(code_ids)} codes of the same kind: {', '.join(sorted(code_ids))}", name, detail, differs)
        # ---- file naming patterns
        d_files = files_in(text)
        for (toks, ext), shown in d_files.items():
            cands = [(sum(1 for (t, e) in files if e == ext and t & toks), p, files) for p, files in bot_files.items()]
            cands = [c for c in cands if c[0]]
            if not cands:
                continue
            _, path, files = max(cands, key=lambda c: c[0])
            related = {(t, e): s for (t, e), s in files.items() if e == ext and t & toks}
            exact = (toks, ext) in related
            name = ex.bots[path].name
            code_disp = ", ".join(sorted(related.values()))
            record(rule, "file", f"file {shown}", shown, code_disp, name,
                   [f"Documentation: {shown}", f"Code builds: {code_disp}"], not exact)

    findings.sort(key=lambda f: (f["link"] != "strong", {"Open": 0, "Changed": 0, "Accepted": 1}[f["status"]],
                                 f["rule"]["doc"], f["rule"]["id"]))
    strong = [f for f in findings if f["link"] == "strong"]
    count = lambda s: sum(1 for f in strong if f["status"] == s)
    return {"findings": findings, "agreements": agreements,
            "summary": {"rules": len(rules), "checked": len(findings) + len(agreements), "agree": len(agreements),
                        "open": count("Open"), "accepted": count("Accepted"), "changed": count("Changed"),
                        "weak": len(findings) - len(strong)}}
