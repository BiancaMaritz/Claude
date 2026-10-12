"""Describe what a bot does, step by step, from its actions.

A bot whose developer never grouped actions into named Step actions still has a process flow: its actions.
This module reads that flow straight from the code, so no bot ever shows an empty process flow.

  bot_flow(nodes)  ->  [(depth, text, does)]
    A bot with named Steps gives one entry per Step; `does` summarises the actions inside it.
    A bot without gives its actions in order, nested under If / Loop / Try, runs of similar
    actions collapsed ("Set 6 variables: a, b, c ...").
"""
from __future__ import annotations

import re

from .analyze import attr, camel_words, clean_var, cond_text, sql_facts, step_label, val
from .loader import norm

MAX_ENTRIES = 400
PACKAGE_LABEL = {
    "String": "Text", "Number": "Number", "Datetime": "Date/time", "DatetimeUtilities": "Date/time",
    "Boolean": "True/false", "List": "List", "DataTable": "Table", "Dictionary": "Dictionary", "File": "File",
    "Folder": "Folder", "Excel_MS": "Excel", "CsvTxt": "CSV/text file", "PDF": "PDF", "OCR": "OCR", "Email": "Email",
    "Database": "Database", "Rest": "Web service", "Browser": "Browser", "Recorder": "Screen", "Application": "Application",
    "Window": "Window", "Screen": "Screen", "MessageBox": "Message box", "LogToFile": "Log", "TaskBot": "Bot",
    "Delay": "Wait", "System": "System", "VBScript": "VBScript", "Credential": "Credential", "Json": "JSON",
    "REGEX Tools": "Regex", "ErrorHandler": "Error handling", "Loop": "Loop", "If": "If", "Comment": "Note"}
DETAIL_KEYS = ["uri", "url", "filePath", "sourceFilePath", "destinationPath", "folderPath", "path", "sheetName", "subject",
               "to", "title", "sourceString", "input", "text", "expression", "variable"]
SKIP_DETAIL = {"STRING", "TRUE", "FALSE", "DEFAULT", "NONE"}
SQL_VERB = {"select": "Read", "insert": "Insert into", "update": "Update", "delete": "Delete from"}
UI_PACKAGES = {"Recorder", "Browser", "Screen", "Window", "Application"}


def _short(text, n=90):
    text = re.sub(r"\s+", " ", clean_var(str(text))).strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def _detail(n):
    for key in DETAIL_KEYS:
        v = val(attr(n, key))
        if v and v.upper() not in SKIP_DETAIL:
            return _short(v)
    return ""


def _var(n):
    return (n.get("returnTo") or {}).get("variableName") or ""


def callee(n):
    tb = attr(n, "taskbot") or {}
    path = norm((tb.get("taskbotFile") or {}).get("string", ""))
    return path.rsplit("/", 1)[-1] if path else "another bot"


def describe(n):
    """One short sentence for one action."""
    pkg, cmd = n.get("packageName", ""), n.get("commandName", "")
    if cmd == "step":
        return step_label(n) or "Step"
    if pkg == "If":
        return {"if": "If ", "elseIf": "Otherwise, if "}.get(cmd, "Otherwise") + (_short(cond_text(n), 110) if cmd != "else" else "")
    if pkg == "ErrorHandler":
        return {"try": "Try", "catch": "If an error occurs", "finally": "Always, afterwards",
                "throw": "Raise an error: " + _detail(n)}.get(cmd, "Error handling: " + camel_words(cmd))
    if pkg == "Loop":
        if cmd.endswith("break"):
            return "Leave the loop"
        if cmd.endswith("continue"):
            return "Skip to the next pass of the loop"
        return _loop_text(n)
    if pkg == "TaskBot":
        return "Stop the bot" if cmd == "stopTask" else f"Run bot {callee(n)}"
    if pkg == "Database":
        if cmd in ("connect", "disconnect"):
            return "Connect to the database" if cmd == "connect" else "Disconnect from the database"
        q = attr(n, "query")
        if q:
            ops, _ = sql_facts(val(q))
            seen = []
            for t, op in ops:
                s = f"{SQL_VERB[op]} {t}"
                if s not in seen:
                    seen.append(s)
            if seen:
                return "Database: " + ", ".join(seen)
        return "Database: " + camel_words(cmd)
    if pkg == "Rest":
        return f"Call web service ({re.sub('^rest', '', cmd, flags=re.I).upper() or 'call'}) {_detail(n)}".strip()
    if pkg == "Email":
        names = {"emailConnect": "Open the mailbox", "SendMailV2": "Send an email", "saveAttachment": "Save the email attachments",
                 "move": "Move the email", "changeStatus": "Change the email status", "closeEmail": "Close the mailbox"}
        return names.get(cmd, "Email: " + camel_words(cmd)) + (f" {_detail(n)}" if cmd == "SendMailV2" and _detail(n) else "")
    if pkg == "File":
        names = {"copyFiles": "Copy file to", "deleteFiles": "Delete file", "moveFiles": "Move file to"}
        return f"{names.get(cmd, 'File: ' + camel_words(cmd))} {_detail(n)}".strip()
    if pkg == "Folder" and cmd == "createFolder":
        return f"Create folder {_detail(n)}".strip()
    if pkg == "MessageBox":
        return "Show a message box" + (f": {_detail(n)}" if _detail(n) else "")
    if pkg == "LogToFile":
        return "Write to the log file" + (f" {val(attr(n, 'filePath'))}" if val(attr(n, "filePath")) else "")
    if pkg == "Delay":
        return f"Wait {val(attr(n, 'delayTime'))} {val(attr(n, 'timeUnit')).lower()}".strip()
    if pkg == "Browser" and cmd == "openbrowser":
        return f"Open the browser {_detail(n)}".strip()
    if pkg == "Datetime" and cmd in ("add", "subtract"):
        return f"{cmd.capitalize()} {val(attr(n, 'val'))} {val(attr(n, 'unit')).lower()} {'to' if cmd == 'add' else 'from'} a date"
    if pkg == "Comment":
        return "Note: " + _short(val(attr(n, "comment")), 140)
    if cmd.startswith("assign") and _var(n):
        return f"Set {_var(n)}" + (f" to {_detail(n)}" if _detail(n) else "")
    base = f"{PACKAGE_LABEL.get(pkg, pkg)}: {camel_words(cmd.split('.')[-1])}"
    extra = _detail(n)
    return base + (f" {extra}" if extra else "") + (f" (into {_var(n)})" if _var(n) else "")


def _loop_text(n):
    """'For each table row in {Source} (as Row)', 'Repeat 5 times', 'Repeat while ...'."""
    kind = val(attr(n, "loopType")).upper()
    for a in n.get("attributes", []) or []:
        v = a.get("value") or {}
        if a.get("name") == "iterator" or v.get("type") == "ITERATOR":
            name = v.get("iteratorName", "")
            what = "item" if "." in name else (camel_words(name.replace("iterator", "", 1)) or "item")
            src = next((val(x.get("value")) for x in a.get("attributes", []) or [] if val(x.get("value"))), "")
            who = (a.get("returnTo") or {}).get("variableName", "")
            return f"For each {what}" + (f" in {clean_var(src)}" if src else "") + (f" (as {who})" if who else "")
    if kind == "WHILE":
        return "Repeat while " + _short(cond_text(n), 100)
    times = val(attr(n, "times"))
    if kind == "TIMES" and times:
        return f"Repeat {clean_var(times)} times"
    return "Repeat" + (": " + _detail(n) if _detail(n) else "")


def _is_leaf(n):
    return not n.get("children") and not n.get("branches") and n.get("packageName") not in ("If", "ErrorHandler", "Loop", "Step")


def _group_key(n):
    """Actions that collapse into one line: runs of variable assignments, and exact repeats."""
    return "assign" if n.get("commandName", "").startswith("assign") else describe(n)


def _group_text(group):
    first = group[0]
    cmd, pkg = first.get("commandName", ""), first.get("packageName", "")
    if len(group) == 1:
        return describe(first)
    if cmd.startswith("assign"):
        names = [_var(n) for n in group if _var(n)]
        more = f" ... (+{len(names) - 4} more)" if len(names) > 4 else ""
        return f"Set {len(group)} variables: {', '.join(names[:4])}{more}"
    return f"{describe(first)} (x{len(group)})"


def bot_flow(nodes):
    """[(depth, text, does)] for a whole bot."""
    entries = []
    has_steps = any(True for n, _ in _walk(nodes) if n.get("commandName") == "step")
    if has_steps:
        for n, depth in _steps(nodes, 0):
            entries.append((depth, step_label(n) or "(unnamed step)", step_does(n)))
        return _cap(entries)

    def build(items, depth):
        group = []

        def flush():
            if group:
                entries.append((depth, _group_text(group), ""))
                group.clear()

        for n in items or []:
            if n.get("disabled"):
                continue
            if _is_leaf(n):
                if group and _group_key(group[0]) != _group_key(n):
                    flush()
                group.append(n)
                continue
            flush()
            entries.append((depth, describe(n), ""))
            build(n.get("children"), depth + 1)
            build(n.get("branches"), depth)
        flush()

    build(nodes, 0)
    return _cap(entries)


def _cap(entries):
    if len(entries) > MAX_ENTRIES:
        return entries[:MAX_ENTRIES] + [(0, f"... {len(entries) - MAX_ENTRIES} more actions not shown", "")]
    return entries


def _walk(nodes):
    for n in nodes or []:
        yield n, None
        yield from _walk(n.get("children"))
        yield from _walk(n.get("branches"))


def _steps(nodes, depth):
    """Step nodes in execution order with their nesting depth among steps."""
    for n in nodes or []:
        if n.get("commandName") == "step":
            yield n, depth
            yield from _steps(n.get("children"), depth + 1)
        else:
            yield from _steps(n.get("children"), depth)
            yield from _steps(n.get("branches"), depth)


def _own_actions(step):
    """Actions inside a step, leaving out the subtrees of steps nested in it."""
    stack = list(step.get("children") or [])
    while stack:
        n = stack.pop(0)
        if n.get("commandName") == "step":
            continue
        yield n
        stack = list(n.get("children") or []) + list(n.get("branches") or []) + stack


def step_does(step):
    """What a Step does, in a line: bots run, tables touched, calls made, counts of the rest."""
    runs, db, rest, email, files, ui, decisions, sets = [], [], 0, 0, 0, 0, 0, 0
    for n in _own_actions(step):
        pkg, cmd = n.get("packageName"), n.get("commandName")
        if n.get("disabled"):
            continue
        if cmd == "runTask":
            runs.append(callee(n))
        elif pkg == "Database" and attr(n, "query"):
            ops, _ = sql_facts(val(attr(n, "query")))
            for t, op in ops:
                s = f"{SQL_VERB[op].lower()} {t}"
                if s not in db:
                    db.append(s)
        elif pkg == "Rest":
            rest += 1
        elif pkg == "Email":
            email += 1
        elif pkg in ("File", "Folder"):
            files += 1
        elif pkg in UI_PACKAGES:
            ui += 1
        elif pkg == "If" and cmd in ("if", "elseIf"):
            decisions += 1
        elif cmd.startswith("assign"):
            sets += 1
    bits = []
    if runs:
        bits.append("runs " + ", ".join(dict.fromkeys(runs)))
    if db:
        bits.append("database: " + ", ".join(db[:4]) + (f" (+{len(db) - 4})" if len(db) > 4 else ""))
    for n, label in ((rest, "web service call"), (email, "email action"), (files, "file/folder action"), (ui, "screen action")):
        if n:
            bits.append(f"{n} {label}{'s' if n > 1 else ''}")
    if decisions:
        bits.append(f"{decisions} decision{'s' if decisions > 1 else ''}")
    if sets:
        bits.append(f"sets {sets} variable{'s' if sets > 1 else ''}")
    return "; ".join(bits)
