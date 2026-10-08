"""Build the SDD as a neutral list of blocks, then render it to md/docx elsewhere.

Block forms:
  ("h", level, text)  ("p", text)  ("todo", text)  ("ul", [text, ...])
  ("table", [header, ...], [[cell, ...], ...])  cell = str | list[str] (one line each)
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .loader import Export

TODO = "[TO BE COMPLETED]"
CONFIRM = "[CONFIRM]"


def load_intake(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _list_or_todo(items, what):
    return [("ul", items)] if items else [("todo", f"{TODO}: {what}")]


def _stakeholder_rows(intake, with_date=False):
    rows = []
    last = None
    for s in intake.get("stakeholders", []):
        if s.get("team") != last:
            last = s.get("team")
            rows.append([f"**{last}**", "", "", ""] + ([""] if with_date else []))
        base = [s.get("name", ""), s.get("role", ""), s.get("email", "")]
        rows.append(base + ([s.get("approval", "")] if not with_date else ["Enter date.", ""]))
    return rows


def _outline(steps):
    """Numbered outline lines from [(depth, label)], collapsing immediate repeats."""
    collapsed = []
    for depth, label in steps:
        if collapsed and collapsed[-1][0] == depth and collapsed[-1][1] == label:
            collapsed[-1][2] += 1
        else:
            collapsed.append([depth, label, 1])
    counters, out = [], []
    for depth, label, n in collapsed:
        counters = counters[: depth + 1]
        while len(counters) <= depth:
            counters.append(0)
        counters[depth] += 1
        num = ".".join(str(c) for c in counters)
        out.append((num, label + (f" (x{n})" if n > 1 else "")))
    return out


def build(ex: Export, a: dict, intake: dict, title=None, date=None) -> list:
    B = []
    ref, per = a["ref"], a["per"]
    short = lambda p: ex.bots[p].name
    label = lambda p: f"{ref[p]}: {short(p)}"

    name = intake.get("title") or title or "Automation"
    B += [
        ("title", name),
        ("p", "Solution Design Document"),
        ("p", f"Version: {intake.get('version', '0.1 (generated draft)')}    Date: {date}"),
        ("p", "Generated deterministically from an Automation Anywhere export "
              f"({len(ex.bots)} bots). Text in square brackets needs human input; "
              "everything else is read directly from the export."),
        ("toc",),
    ]

    # ---- 1 Business requirements (intake only) ------------------------------
    B.append(("h", 1, "1. Business Requirements"))
    B.append(("h", 2, "a. Process Description"))
    B += [("p", intake["process_description"])] if intake.get("process_description") else [
        ("todo", f"{TODO}: business description of the process (not present in a bot export).")]
    B.append(("h", 2, "b. Priority (MoSCoW)"))
    m = intake.get("moscow", {})
    B.append(("table", ["Priority", "Items"], [
        ["Must Have", m.get("must", [TODO])], ["Should Have", m.get("should", [TODO])],
        ["Could Have", m.get("could", [""])], ["Won't Have", m.get("wont", [TODO])]]))
    B.append(("h", 2, "c. Assumptions"))
    B += _list_or_todo(intake.get("assumptions"), "assumptions.")
    B.append(("h", 2, "d. Functional Requirements"))
    fr = intake.get("functional_requirements")
    B += [("table", ["#", "Requirement"], [[f"FR{i}", t] for i, t in enumerate(fr, 1)])] if fr else [
        ("todo", f"{TODO}: functional requirements.")]
    B.append(("h", 2, "e. Success Criteria"))
    B += _list_or_todo(intake.get("success_criteria"), "success criteria and how they are measured.")
    B.append(("h", 2, "f. Expected Benefits"))
    ben = intake.get("benefits")
    B += [("table", ["#", "Benefit", "Description"],
           [[str(i), b["benefit"], b["description"]] for i, b in enumerate(ben, 1)])] if ben else [
        ("todo", f"{TODO}: expected benefits.")]
    B.append(("h", 2, "g. Stakeholders"))
    rows = _stakeholder_rows(intake)
    B.append(("table", ["Stakeholder", "Role in Automation", "Email", "Change Request Approval Level"],
              rows or [[TODO, "", "", ""]]))

    # ---- 2 Functional design ---------------------------------------------------
    B.append(("h", 1, "2. Functional Design"))
    B.append(("h", 2, "a. To Be Process Flow Steps"))
    B.append(("p", "Steps below are the named Step actions in each entry-point bot, in execution order. "
                   "Indented numbering shows nesting. Screenshots and business wording need review."))
    for p in a["entry"]:
        d = per[p]
        outline = _outline(d["steps"])
        B.append(("h", 3, f"Scenario: {short(p)}"))
        if not outline:
            B.append(("todo", f"{TODO}: bot has no named Step actions."))
            continue
        B.append(("table", ["Step", "Screen Shot", "Description"],
                  [[n, "[Screenshot]", t] for n, t in outline]))
    B.append(("h", 2, "b. Changes brought about by Automation"))
    B += _list_or_todo(intake.get("changes_by_automation"), "what users no longer have to do.")
    B.append(("h", 2, "c. ERD Design"))
    if a["tables"]:
        B.append(("p", "Tables referenced by SQL in the bots. Columns listed are those written by "
                       "INSERT or UPDATE statements; keys and relationships are not in an export."))
        B.append(("table", ["Table", "Operations", "Columns written", "Used by"],
                  [[t, ", ".join(i["ops"]), ", ".join(i["columns"]) or "-",
                    [ref[b] for b in i["bots"]]] for t, i in a["tables"].items()]))
    else:
        B.append(("p", "No SQL statements found in the bots."))
    B.append(("todo", f"{TODO}: entity relationship diagram and key definitions."))
    B.append(("h", 2, "d. Business Rules"))
    br = intake.get("business_rules")
    if br:
        B.append(("table", ["Number", "Description"], [[r["id"], r["description"]] for r in br]))
    else:
        B.append(("todo", f"{TODO}: confirmed business rules. Candidate rules are in Appendix B "
                          "(decision points read from the bot logic)."))
    B.append(("h", 2, "e. Functional Design - Sign Off"))
    B.append(("table", ["Stakeholder", "Role in Automation", "Email", "Date", "Initial"],
              _stakeholder_rows(intake, True) or [[TODO, "", "", "Enter date.", ""]]))

    # ---- 3 Technical design ------------------------------------------------------
    B.append(("h", 1, "3. Technical Design"))
    B.append(("h", 2, "a. Applications & Systems"))
    given = {x["name"].lower(): x for x in intake.get("applications", [])}
    rows = []
    for i, s in enumerate(a["systems"], 1):
        g = given.get(s["name"].lower(), {})
        rows.append([f"APP{i}", [s["name"], f"Evidence: {s['package']}, {s['count']} action(s) in {len(s['bots'])} bot(s)"],
                     s["type"], g.get("access", CONFIRM), g.get("bot_username", CONFIRM)])
    B.append(("table", ["Nr", "Name", "Type", "Access", "Bot Username"], rows))
    B.append(("h", 2, "b. Bot Components"))
    B.append(("p", "Role rule: bots under Logic Manager are Main; under Common Tasks, Utilities or "
                   "Task Logging are Common unless nothing calls them; all others are Sub. Entry points are bots no other bot calls "
                   "(or, for bots that call each other, the whole group)."))
    rows = []
    for role in ("Main", "Sub", "Common"):
        for p in a["groups"][role]:
            b = ex.bots[p]
            note = []
            if b.description:
                note.append(b.description)
            if p in a["entry"]:
                note.append("Entry point")
            rows.append([ref[p], [short(p), b.folder], note or "-",
                         [ref[c] for c in a["calls"][p] if c in ref] or "-"])
    B.append(("table", ["Bot", "Name / Folder", "Description", "Calls"], rows))
    if a["cycles"]:
        B.append(("p", "Circular calls: " + "; ".join(" <-> ".join(short(x) for x in c) for c in a["cycles"])))
    B.append(("h", 2, "c. Triggers"))
    rows = []
    for p in a["entry"]:
        d = per[p]
        notes = []
        if ex.bots[p].triggers:
            notes.append(f"{len(ex.bots[p].triggers)} in-bot trigger(s) defined.")
        if d["email"].get("emailConnect"):
            notes.append("Connects to a mailbox (emailConnect); likely mailbox-driven.")
        notes.append("Schedule or trigger is configured in the Control Room, not in the export.")
        rows.append([short(p), CONFIRM, notes])
    B.append(("table", ["Process / Bot Initialized", "Trigger Type", "Notes"], rows))
    B.append(("h", 2, "d. Inputs/Outputs"))
    B.append(("p", "Bots with identical input and output signatures are grouped. Defaults of "
                   "sensitive-looking variables are masked."))
    rows = []
    for i, r in enumerate(a["io"], 1):
        names = [short(p) for p in r["bots"]]
        bot_cell = names if len(names) <= 4 else names[:3] + [f"... and {len(names) - 3} more with the same signature"]
        fmt_in = [f"{n} ({t})" + (f" = {d}" if d else "") for n, t, d in r["inputs"]] or "N/A"
        fmt_out = [f"{n} ({t})" for n, t in r["outputs"]] or "N/A"
        rows.append([f"IO{i}", bot_cell, fmt_in, fmt_out])
    B.append(("table", ["Item", "Task Bot Name", "Input", "Output"], rows))
    B.append(("h", 2, "e. Reports and Logs"))
    rows = []
    cloud = [r for r in a["rest"] if "sql-logging" in r["uri"]]
    if cloud:
        B.append(("p", "Logging endpoints: " + ", ".join(r["uri"].rsplit("/", 1)[-1] for r in cloud)))
        rows.append(["Process and error logging to SQL through REST endpoints", "Process Progress Log", CONFIRM])
    mailers = [p for p in ex.bots if any(c.endswith("Generic Error Emailer") for c in a["calls"][p])]
    if mailers:
        rows.append([f"Error notification emails ({len(mailers)} bots call Generic Error Emailer)",
                     "Process Failure Notifications", CONFIRM])
    lf = sum(d["log_to_file"] for d in per.values())
    if lf:
        rows.append([f"Log to file actions ({lf})", "File log", CONFIRM])
    B.append(("table", ["Description", "Log/Report Type", "Complexity Level"], rows or [["-", "-", CONFIRM]]))

    # ---- Appendices ----------------------------------------------------------------
    B.append(("h", 1, "Appendix A. Step Outline per Bot"))
    groups = defaultdict(list)
    for p in sorted(ex.bots):
        if p not in a["entry"] and per[p]["steps"]:
            groups[tuple(per[p]["steps"])].append(p)
    for steps, paths in groups.items():
        B.append(("h", 3, ", ".join(short(p) for p in paths[:3]) + (f" and {len(paths) - 3} more with the same steps" if len(paths) > 3 else "")))
        B.append(("table", ["Step", "Description"], [[n, t] for n, t in _outline(list(steps))]))

    B.append(("h", 1, "Appendix B. Decision Points (candidate business rules)"))
    B.append(("p", "Conditions read from If / Else If / While actions, grouped by the enclosing step. "
                   "A human must confirm which of these are business rules and which are technical guards."))
    for p in a["groups"]["Main"] + [e for e in a["entry"] if e not in a["groups"]["Main"]]:
        dec = Counter(per[p]["decisions"])
        if not dec:
            continue
        B.append(("h", 3, label(p)))
        B.append(("table", ["Step", "Type", "Condition", "Count"],
                  [[c or "-", t, cond or "-", str(n)] for (c, t, cond), n in dec.items()]))

    B.append(("h", 1, "Appendix C. Credential Vault and Shared References"))
    B.append(("p", "Names only; values are never read from the export."))
    B += [("ul", a["vault"])] if a["vault"] else [("p", "None found.")]

    B.append(("h", 1, "Appendix D. Packages"))
    B.append(("table", ["Package", "Versions"], [[k, ", ".join(v)] for k, v in a["packages"].items()]))

    B.append(("h", 1, "Appendix E. Review Findings"))
    B.append(("p", "Automatic checks on the export. These are observations for review, not defects confirmed."))
    cats = defaultdict(list)
    for cat, where, msg in a["hygiene"]:
        cats[cat].append([where.replace("Automation Anywhere/Bots/", ""), msg])
    for cat, rows in cats.items():
        B.append(("h", 3, f"{cat} ({len(rows)})"))
        B.append(("table", ["Where", "Finding"], rows))

    notes = [(short(p), c) for p in sorted(ex.bots) for c in per[p]["comments"] if c]
    if notes:
        B.append(("h", 1, "Appendix F. Developer Comments in Bots"))
        B.append(("table", ["Bot", "Comment"], [[b, c] for b, c in notes]))
    return B
