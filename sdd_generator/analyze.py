"""Derive SDD facts from a loaded export. Fully deterministic: no network, no LLM."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from .loader import Export, norm

VAULT_RE = re.compile(r"\$@([^$]+)\$")
SENSITIVE_RE = re.compile(r"pass|pwd|secret|token|key|credential", re.I)

# Package -> (system label, type). Only packages that evidence an external system.
PACKAGE_SYSTEMS = {
    "Browser": ("Web application (browser)", "Web Based"),
    "Excel_MS": ("Microsoft Excel", "Client/Server Based"),
    "Email": ("Email mailbox / SMTP", "Client/Server Based"),
    "Database": ("SQL database", "Database"),
    "Rest": ("REST web service", "Web Service"),
    "OCR": ("ABBYY OCR engine", "Client/Server Based"),
    "PDF": ("PDF documents", "File Based"),
    "CsvTxt": ("CSV / text files", "File Based"),
    "Recorder": ("Desktop / web UI (recorded actions)", "Client/Server Based"),
    "LegacyAutomation": ("Desktop UI (legacy object recognition)", "Client/Server Based"),
}


def walk(nodes, stack=()):
    """Yield (node, ancestors) depth-first, in execution order."""
    for n in nodes or []:
        yield n, stack
        yield from walk(n.get("children"), stack + (n,))
        yield from walk(n.get("branches"), stack + (n,))


def camel_words(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or "").lower()


def val(v) -> str:
    """Render an attribute value as readable text."""
    if not isinstance(v, dict):
        return str(v)
    for k in ("expression", "string", "number"):
        if v.get(k) not in (None, ""):
            return str(v[k])
    if "boolean" in v:
        return str(v["boolean"]).lower()
    if v.get("variableName"):
        return "$" + v["variableName"] + "$"
    if v.get("type") == "CONDITIONAL":
        return condition_text(v)
    return ""


def condition_text(c: dict) -> str:
    name = camel_words(c.get("conditionalName", "condition"))
    args = ", ".join(
        f"{a.get('name')}={val(a.get('value'))}"
        for a in c.get("attributes", [])
        if val(a.get("value")) != ""
    )
    return f"{name}({args})" if args else name


OPERATORS = {"INCLUDE": "contains", "NOT_INCLUDE": "does not contain", "NOTINCLUDE": "does not contain",
             "GT": "is greater than", "LT": "is less than", "GE": "is at least", "LE": "is at most", "EQ": "equals",
             "NE": "does not equal", "GTE": "is at least", "LTE": "is at most", "EQUALS": "equals", "NOT_EQUALS": "does not equal",
             "STARTS_WITH": "starts with", "ENDS_WITH": "ends with", "EMPTY": "is empty", "NOT_EMPTY": "is not empty",
             "GREATER_THAN": "is greater than", "LESS_THAN": "is less than", "GREATER_THAN_EQUALS": "is at least",
             "LESS_THAN_EQUALS": "is at most", "IS_TRUE": "is true", "IS_FALSE": "is false"}
FLAG_ARGS = {"matchCase", "isIgnoreCarriage", "isCaseSensitive", "waitTimeout", "session"}


def cond_text(node):
    """Readable condition of an If / Else If / While action, including its arguments.

    The arguments sit in the condition attribute's own attribute list, next to the value that names
    the condition, so both are needed.
    """
    for a in node.get("attributes", []) or []:
        if a.get("name") != "condition":
            continue
        v = a.get("value") or {}
        name = v.get("conditionalName", "condition")
        args = {x.get("name"): val(x.get("value")) for x in a.get("attributes", []) or [] if x.get("name") not in FLAG_ARGS}
        args = {k: x for k, x in args.items() if x not in ("", None)}
        if name.endswith("Variable") and "variable" in args and "operator" in args:
            op = OPERATORS.get(args["operator"].upper(), args["operator"].lower().replace("_", " "))
            return clean_var(f"{args['variable']} {op}" + (f" {args['value']}" if "value" in args else ""))
        words_ = camel_words(name)
        return clean_var(f"{words_}({', '.join(f'{k}={x}' for k, x in args.items())})" if args else words_)
    return ""


def attr(node, name):
    for a in node.get("attributes", []) or []:
        if a.get("name") == name:
            return a.get("value")
    return None


def step_label(node) -> str:
    for a in node.get("attributes", []) or []:
        t = val(a.get("value"))
        if t:
            return t
    return ""


def clean_var(s: str) -> str:
    return re.sub(r"\$([^$]+)\$", r"{\1}", s)


# ---------------------------------------------------------------- per bot ---

def sql_facts(query: str):
    """Return [(table, operation)] and {table: written columns} for one statement."""
    q = re.sub(r"--[^\n]*", " ", query)
    ops, cols = [], defaultdict(set)
    tok = r"([\w$@#\[\]\.]+)"
    for m in re.finditer(rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+{tok}", q, re.I):
        kw = re.sub(r"\s+", " ", m.group(1)).upper()
        table = re.findall(r"\[?([A-Za-z_][\w]*)\]?", m.group(2).split(".")[-1])
        if not table or table[0].upper() in ("SELECT", "SET"):
            continue
        t = table[0]
        qualified = "." in m.group(2) or "[" in m.group(2)
        if not qualified and not t[0].isupper():
            continue  # prose such as "from the server" inside a message string
        op = {"INSERT INTO": "insert", "UPDATE": "update", "DELETE FROM": "delete"}.get(kw, "select")
        ops.append((t, op))
        if op == "insert":
            cm = re.search(r"INSERT\s+INTO\s+[^\(]+\(([^)]*)\)", q[m.start():], re.I)
            if cm:
                cols[t].update(re.findall(r"\[?(\w+)\]?", cm.group(1)))
        elif op == "update":
            sm = re.search(r"\bSET\b(.*?)(\bWHERE\b|\bFROM\b|$)", q[m.start():], re.I | re.S)
            if sm:
                cols[t].update(re.findall(r"\[?(\w+)\]?\s*=", sm.group(1)))
    return ops, cols


def analyse_bot(bot) -> dict:
    d = {
        "steps": [], "decisions": [], "comments": [], "packages": Counter(),
        "commands": Counter(), "calls": [], "vault": set(), "rest": [],
        "email": Counter(), "tables": defaultdict(lambda: {"ops": set(), "columns": set()}),
        "message_boxes": 0, "disabled": 0, "log_to_file": 0, "error_handlers": 0,
        "sql": [], "connections": [], "var_refs": {},
    }
    for n, stack in walk(bot.nodes):
        pkg, cmd = n.get("packageName", ""), n.get("commandName", "")
        d["packages"][pkg] += 1
        d["commands"][(pkg, cmd)] += 1
        if n.get("disabled"):
            d["disabled"] += 1
        d["vault"].update(VAULT_RE.findall(json.dumps(n.get("attributes", []))))
        steps_above = [s for s in stack if s.get("commandName") == "step"]
        if cmd == "step":
            d["steps"].append((len(steps_above), step_label(n)))
        elif cmd == "Comment":
            d["comments"].append(val(attr(n, "comment")))
        elif cmd in ("if", "elseIf", "while"):
            ctx = step_label(steps_above[-1]) if steps_above else ""
            d["decisions"].append((ctx, cmd, cond_text(n)))
        elif cmd == "runTask":
            tb = attr(n, "taskbot") or {}
            target = norm((tb.get("taskbotFile") or {}).get("string", ""))
            if target:
                d["calls"].append(target)
        elif pkg == "Rest":
            uri = val(attr(n, "uri"))
            method = re.sub(r"^rest", "", cmd, flags=re.I) or "call"
            if uri:
                d["rest"].append((method.upper(), clean_var(uri)))
        elif pkg == "Email":
            d["email"][cmd] += 1
        elif pkg == "MessageBox":
            d["message_boxes"] += 1
        elif pkg == "LogToFile":
            d["log_to_file"] += 1
        elif pkg == "ErrorHandler" and cmd == "try":
            d["error_handlers"] += 1
        rv = (n.get("returnTo") or {}).get("variableName")
        if rv and cmd.startswith("assign"):  # a variable set from a vault entry, e.g. DatabaseName <- $@HerotelProdDatabaseName$
            refs = VAULT_RE.findall(json.dumps(n.get("attributes", [])))
            if refs:
                d["var_refs"][rv] = refs[0]
        if pkg == "Database" and cmd == "connect":
            cred = ((attr(n, "connectionURLCV") or {}).get("credential") or {}).get("name")
            if cred and cred not in d["connections"]:
                d["connections"].append(cred)
        q = attr(n, "query")
        if pkg == "Database" and q:
            from .sqlmodel import parse_sql  # imported here: sqlmodel does not need this module

            d["sql"].append({"step": step_label(steps_above[-1]) if steps_above else "", "parsed": parse_sql(val(q))})
            ops, cols = sql_facts(val(q))
            for t, op in ops:
                d["tables"][t]["ops"].add(op)
            for t, c in cols.items():
                d["tables"][t]["columns"].update(c)
    d["tables"] = {k: v for k, v in d["tables"].items()}
    from .flow import bot_flow  # imported here: flow builds on this module

    d["flow"] = bot_flow(bot.nodes)
    d["flow_kind"] = "steps" if d["steps"] else "actions"
    return d


# ------------------------------------------------------------- whole export ---

def strongly_connected(graph):
    """Tarjan's algorithm. graph: node -> iterable of nodes. Returns list of components."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, []):
            if w not in graph:
                continue
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(sorted(comp))

    for v in sorted(graph):
        if v not in index:
            visit(v)
    return out


def role_of(path: str) -> str:
    low = path.lower()
    if "/logic manager/" in low:
        return "Main"
    if any(k in low for k in ("/common tasks/", "/utilities/", "/task logging/")):
        return "Common"
    return "Sub"


def analyse(ex: Export) -> dict:
    per = {p: analyse_bot(b) for p, b in ex.bots.items()}
    # Calls come from bot logic when available, otherwise from the manifest.
    calls = {p: sorted(set(per[p]["calls"])) if ex.bots[p].data else ex.bots[p].manifest_deps
             for p in ex.bots}
    sccs = strongly_connected(calls)
    comp = {p: i for i, c in enumerate(sccs) for p in c}
    incoming = defaultdict(set)
    for p, cs in calls.items():
        for c in cs:
            if c in comp and comp[c] != comp[p]:
                incoming[comp[c]].add(comp[p])
    entry = sorted(p for i, c in enumerate(sccs) if not incoming[i] for p in c)
    cycles = [sorted(c) for c in sccs if len(c) > 1 or c[0] in calls.get(c[0], [])]

    groups = {"Main": [], "Sub": [], "Common": []}
    for p in sorted(ex.bots):
        role = role_of(p)
        if role == "Common" and p in entry:
            role = "Sub"  # a bot nothing else calls is not a shared component
        groups[role].append(p)
    ref = {}
    for role, paths in groups.items():
        for i, p in enumerate(paths, 1):
            ref[p] = f"{role} Bot {i}"

    return {
        "per": per, "calls": calls, "entry": entry, "cycles": cycles, "ref": ref, "groups": groups,
        "io": io_rows(ex, ref),
        "systems": systems(ex, per),
        "tables": merge_tables(per),
        "rest": merge_rest(per),
        "vault": sorted({v for d in per.values() for v in d["vault"]}),
        "packages": package_versions(ex),
        "hygiene": hygiene(ex, per, calls, cycles),
    }


def io_rows(ex, ref):
    sig = defaultdict(list)
    for p, b in sorted(ex.bots.items()):
        ins = tuple((v["name"], v["type"], _default(v)) for v in b.variables if v.get("input"))
        outs = tuple((v["name"], v["type"]) for v in b.variables if v.get("output"))
        sig[(ins, outs)].append(p)
    rows = []
    for (ins, outs), paths in sorted(sig.items(), key=lambda kv: kv[1][0]):
        rows.append({"bots": paths, "inputs": list(ins), "outputs": list(outs)})
    return rows


def _default(v):
    d = v.get("defaultValue") or {}
    text = val(d) if d.get("type") not in ("TABLE", "LIST", "DICTIONARY", "RECORD") else ""
    if text and v.get("type") == "STRING" and SENSITIVE_RE.search(v["name"]):
        return "[masked]"
    return text


def systems(ex, per):
    use = defaultdict(lambda: {"bots": set(), "count": 0})
    for p, d in per.items():
        for pkg, n in d["packages"].items():
            if pkg in PACKAGE_SYSTEMS:
                use[pkg]["bots"].add(p)
                use[pkg]["count"] += n
    out = []
    for pkg, u in sorted(use.items()):
        label, typ = PACKAGE_SYSTEMS[pkg]
        out.append({"package": pkg, "name": label, "type": typ,
                    "bots": sorted(u["bots"]), "count": u["count"]})
    for p in sorted(ex.bots):
        m = re.match(r"(.+?)\s+Login$", ex.bots[p].name, re.I)
        if m:
            out.append({"package": "login bot", "name": m.group(1), "type": "Web Based",
                        "bots": [p], "count": 1})
    return out


def merge_tables(per):
    t = defaultdict(lambda: {"ops": set(), "columns": set(), "bots": set()})
    for p, d in per.items():
        for name, info in d["tables"].items():
            t[name]["ops"] |= info["ops"]
            t[name]["columns"] |= info["columns"]
            t[name]["bots"].add(p)
    return {k: {"ops": sorted(v["ops"]), "columns": sorted(v["columns"]), "bots": sorted(v["bots"])}
            for k, v in sorted(t.items())}


def merge_rest(per):
    r = defaultdict(set)
    for p, d in per.items():
        for m, u in d["rest"]:
            r[(m, u)].add(p)
    return [{"method": m, "uri": u, "bots": sorted(b)} for (m, u), b in sorted(r.items())]


def package_versions(ex):
    ver = defaultdict(set)
    for p in ex.manifest_packages:
        ver[p["name"]].add(p["version"])
    for b in ex.bots.values():
        for p in (b.data or {}).get("packages", []):
            ver[p["name"]].add(p["version"])
    return {k: sorted(v) for k, v in sorted(ver.items())}


def hygiene(ex, per, calls, cycles):
    f = []
    for c in cycles:
        f.append(("Circular call", "; ".join(c),
                  "These bots call each other. Confirm the recursion is intended and bounded."))
    for p, b in sorted(ex.bots.items()):
        if re.search(r"\b(copy|test|backup|bak|temp)\b|\(\d+\)$", b.name, re.I):
            f.append(("Naming", p, "Name suggests a leftover copy or draft."))
        elif re.search(r"\bnew\b", b.name, re.I):
            f.append(("Naming", p, "Name carries a NEW suffix; confirm which version is live."))
    norm_names = defaultdict(list)
    for p, b in ex.bots.items():
        norm_names[re.sub(r"[\s_\-]+", "", b.name).lower()].append(p)
    for paths in norm_names.values():
        if len(paths) > 1:
            f.append(("Duplicate", "; ".join(sorted(paths)), "Same bot name (ignoring spaces) at multiple paths."))
    descs = defaultdict(list)
    for p, b in ex.bots.items():
        if b.description:
            descs[b.description].append(p)
    for paths in descs.values():
        if len(paths) > 1:
            f.append(("Duplicate", "; ".join(sorted(paths)), "Identical description at multiple paths."))
    for p, b in sorted(ex.bots.items()):
        for c in calls[p]:
            if c not in ex.bots:
                f.append(("Missing dependency", p, f"Calls {c}, which is not in the export."))
        if b.data:
            declared, actual = set(b.manifest_deps), set(per[p]["calls"])
            for c in sorted(actual - declared) if ex.has_manifest else []:
                f.append(("Manifest mismatch", p, f"Bot logic calls {c}; manifest does not list it."))
            for c in sorted(declared - actual) if ex.has_manifest else []:
                f.append(("Manifest mismatch", p, f"Manifest lists {c}; no runTask in the bot logic calls it."))
        if per[p]["message_boxes"]:
            f.append(("Unattended risk", p,
                      f"{per[p]['message_boxes']} message box action(s); these block an unattended run if reached."))
        if per[p]["disabled"]:
            f.append(("Disabled code", p, f"{per[p]['disabled']} disabled action(s)."))
    for name, vs in package_versions(ex).items():
        if len(vs) > 1:
            f.append(("Package versions", name, f"{len(vs)} versions in use: {', '.join(vs)}."))
    return f
