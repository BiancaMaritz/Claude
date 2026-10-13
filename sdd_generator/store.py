"""Persistent SDD documents: load files in, either overwriting or adding to what is there.

A document is a JSON file in the data directory holding the merged export (all bots seen),
the merged intake, and a history of loads. Generating the SDD is always a pure function of
that stored state, so the preview and the downloads can never disagree.
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import shutil
import re
from dataclasses import asdict

from .loader import Bot, Export

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or "")) and ".." not in name


def deep_merge(a, b):
    """Merge intake structures: dicts recurse, lists append without duplicates, scalars: b wins."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(a[k], v) if k in a else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        seen = {json.dumps(x, sort_keys=True) for x in a}
        return a + [x for x in b if json.dumps(x, sort_keys=True) not in seen]
    return b


def export_to_dict(ex: Export) -> dict:
    return {"source": ex.source, "has_manifest": ex.has_manifest, "has_bot_files": ex.has_bot_files,
            "manifest_packages": ex.manifest_packages, "global_values": ex.global_values, "assets": ex.assets,
            "bots": {p: asdict(b) for p, b in ex.bots.items()}}


def export_from_dict(d: dict) -> Export:
    ex = Export(source=d.get("source", ""), has_manifest=d.get("has_manifest", False),
                has_bot_files=d.get("has_bot_files", False),
                manifest_packages=d.get("manifest_packages", []), global_values=d.get("global_values", []),
                assets=d.get("assets", []))
    ex.bots = {p: Bot(**b) for p, b in d.get("bots", {}).items()}
    return ex


def _bot_key(b: Bot):
    return json.dumps([b.description, b.tags, b.manifest_deps, b.data], sort_keys=True)


def merge_exports(base: Export, new: Export):
    """Add `new` into `base`. Returns (added, updated, unchanged) lists of bot paths."""
    added, updated, same = [], [], []
    for p, b in new.bots.items():
        old = base.bots.get(p)
        if old is None:
            added.append(p)
        else:
            # a manifest-only record must not erase real bot logic already stored
            if b.data is None and old.data is not None:
                b = Bot(path=p, description=b.description or old.description, tags=b.tags or old.tags,
                        manifest_deps=b.manifest_deps or old.manifest_deps, data=old.data)
            (same if _bot_key(old) == _bot_key(b) else updated).append(p)
        base.bots[p] = b
    have = {(x["name"], x["version"]) for x in base.manifest_packages}
    base.manifest_packages += [x for x in new.manifest_packages if (x["name"], x["version"]) not in have]
    base.global_values += [x for x in new.global_values if x not in base.global_values]
    have_assets = {a["path"] for a in base.assets}
    base.assets += [a for a in new.assets if a["path"] not in have_assets]
    base.has_manifest |= new.has_manifest
    base.has_bot_files |= new.has_bot_files
    return added, updated, same


class Store:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, name):
        return os.path.join(self.root, name + ".sdd.json")

    def list(self):
        out = []
        for f in sorted(os.listdir(self.root)):
            if f.endswith(".sdd.json"):
                d = self.get(f[:-9])
                if d:
                    out.append({"name": f[:-9], "title": d["title"], "bots": len(d["export"]["bots"]),
                                "updated": d["updated"]})
        return out

    def get(self, name):
        if not valid_name(name) or not os.path.exists(self._path(name)):
            return None
        with open(self._path(name), encoding="utf-8") as fh:
            doc = json.load(fh)
        for key in ("documentation", "sources", "videos", "graphs", "tables"):
            doc.setdefault(key, [])
        doc.setdefault("acceptances", {})
        return doc

    def save(self, name, doc):
        doc["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        doc["rev"] = doc.get("rev", 0) + 1  # distinguishes two saves inside the same second
        self._backup(name)
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self._path(name))  # never leave a half-written document

    KEEP_BACKUPS = 10

    def _backup(self, name):
        """Keep the previous version of a document before every save, so nothing is ever lost."""
        src = self._path(name)
        if not os.path.exists(src):
            return
        folder = os.path.join(self.root, "backups", name)
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(src, os.path.join(folder, stamp + ".sdd.json"))
        old = sorted(os.listdir(folder))[:-self.KEEP_BACKUPS]
        for f in old:
            os.remove(os.path.join(folder, f))

    def delete(self, name):
        if valid_name(name) and os.path.exists(self._path(name)):
            self._backup(name)  # deleting is the one way to lose a document, so it leaves a copy behind
            os.remove(self._path(name))
            return True
        return False

    def load(self, name, mode, exports, intakes, filenames, title=None, sources=None):
        """Apply a load. mode: 'add' (merge into the document) or 'overwrite' (replace it).

        `sources` is [{name, fp}] for every loaded file. The result says which of them were
        already part of the document and whether the load changed anything at all; a load that
        changes nothing is not written and not added to the history.
        """
        if mode not in ("add", "overwrite"):
            raise ValueError("Mode must be add or overwrite.")
        sources = sources or []
        existing = self.get(name)
        old_ex = export_from_dict(existing["export"]) if existing else Export(source=name)
        if mode == "overwrite" or existing is None:
            # overwriting the code never touches documentation or intake: those are other inputs
            doc = {"title": title or (existing or {}).get("title") or name,
                   "intake": (existing or {}).get("intake", {}),
                   "documentation": (existing or {}).get("documentation", []),
                   **{k: (existing or {}).get(k, []) for k in ("videos", "graphs", "tables")},
                   "acceptances": (existing or {}).get("acceptances", {}),
                   "export": export_to_dict(Export(source=name)), "history": (existing or {}).get("history", []),
                   "sources": []}
        else:
            doc = copy.deepcopy(existing)  # `existing` stays untouched for the before/after comparison
            doc.setdefault("sources", [])
            if title:
                doc["title"] = title
        known = {x["fp"] for x in (existing or {}).get("sources", [])}
        base = export_from_dict(doc["export"])
        report = {"mode": mode, "created": existing is None, "added": 0, "updated": 0, "unchanged": 0,
                  "removed": 0, "intake": 0,
                  "already_loaded": [x["name"] for x in sources if x["fp"] in known]}
        for ex in exports:
            merge_exports(base, ex)
        # Compare the result with what the document held before, bot by bot.
        old = old_ex.bots
        added = sorted(set(base.bots) - set(old))
        removed = sorted(set(old) - set(base.bots))
        updated = sorted(p for p in base.bots if p in old and _bot_key(old[p]) != _bot_key(base.bots[p]))
        report.update(added=len(added), removed=len(removed), updated=len(updated),
                      unchanged=len(base.bots) - len(added) - len(updated),
                      names={"added": added[:100], "updated": updated[:100], "removed": removed[:100]})
        for i in intakes:
            doc["intake"] = deep_merge(doc["intake"], i)
            report["intake"] += 1
        doc["export"] = export_to_dict(base)
        seen = {x["fp"] for x in doc["sources"]}
        doc["sources"] += [x for x in sources if x["fp"] not in seen]
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        for x in sources:
            for have in doc["sources"]:
                if have["fp"] == x["fp"]:
                    have.update(name=x["name"], at=stamp)
        unchanged_state = (existing is not None and doc["export"] == existing["export"]
                           and doc["intake"] == existing["intake"] and doc["title"] == existing["title"])
        report["noop"] = unchanged_state
        if not unchanged_state:
            doc["history"].append({"at": stamp, "files": filenames,
                                   **{k: v for k, v in report.items() if k not in ("names", "noop")}})
            self.save(name, doc)
        return report

    # ------------------------------------------------------------ documentation ---

    def _blank(self, name, title=None):
        return {"title": title or name, "intake": {}, "documentation": [], "sources": [], "history": [],
                "videos": [], "graphs": [], "tables": [], "acceptances": {},
                "export": export_to_dict(Export(source=name))}

    def media_dir(self, name):
        """Where a project's uploaded videos and graphs live, beside the documents folder."""
        d = os.path.join(os.path.dirname(os.path.abspath(self.root)), "media", name)
        os.makedirs(d, exist_ok=True)
        return d

    def _stamp(self):
        return datetime.datetime.now().isoformat(timespec="seconds")

    def _doc_for_edit(self, name, title=None):
        existing = self.get(name)
        return (copy.deepcopy(existing) if existing else self._blank(name, title)), existing

    # -------------------------------------------------------- videos and graphs ---

    def add_item(self, name, kind, item):
        """Add a video or graph record to a project (the file itself is already in media_dir)."""
        if kind not in ("videos", "graphs"):
            raise ValueError("Unknown kind.")
        doc, _ = self._doc_for_edit(name)
        doc[kind].append(item)
        doc["history"].append({"at": self._stamp(), "kind": kind, "mode": "add", "files": [item["name"]],
                               "added": 1, "updated": 0, "unchanged": 0, "removed": 0})
        self.save(name, doc)

    def update_item(self, name, kind, item_id, fields):
        doc = self.get(name)
        if not doc or kind not in ("videos", "graphs"):
            raise ValueError("No such project.")
        item = next((x for x in doc[kind] if x["id"] == item_id), None)
        if not item:
            raise ValueError("No such item.")
        from .media import SECTION_KEYS

        if "section" in fields:
            if fields["section"] not in SECTION_KEYS:
                raise ValueError("Unknown section.")
            item["section"] = fields["section"]
        if "caption" in fields:
            item["caption"] = str(fields["caption"])[:200]
        if "transcript" in fields:
            item["transcript"] = fields["transcript"]
        self.save(name, doc)
        return item

    def remove_item(self, name, kind, item_id):
        """Remove a record. Returns the removed item so the caller can delete a stored file."""
        doc = self.get(name)
        if not doc or kind not in ("videos", "graphs"):
            return None
        item = next((x for x in doc[kind] if x["id"] == item_id), None)
        if not item:
            return None
        doc[kind] = [x for x in doc[kind] if x["id"] != item_id]
        self.save(name, doc)
        return item

    # ------------------------------------------------------------- acceptances ---

    def accept(self, name, finding_id, sig, note):
        """Record that a documentation/code difference is a valid change. Tied to the values it was given for."""
        doc = self.get(name)
        if not doc:
            raise ValueError("No such project.")
        doc["acceptances"][finding_id] = {"sig": sig, "note": str(note)[:500], "at": self._stamp()}
        self.save(name, doc)

    def reopen(self, name, finding_id):
        doc = self.get(name)
        if not doc or finding_id not in doc["acceptances"]:
            return False
        del doc["acceptances"][finding_id]
        self.save(name, doc)
        return True

    # ------------------------------------------------------------------ tables ---

    def load_tables(self, name, mode, tables, filenames):
        """Add table schemas to a project.

        'add': new tables are added, and a table whose name already exists is replaced when its
        content changed (the change is reported), or left alone when identical.
        'overwrite': the project's tables become exactly these.
        """
        if mode not in ("add", "overwrite"):
            raise ValueError("Mode must be add or overwrite.")
        from .tables import diff_table

        doc, existing = self._doc_for_edit(name)
        before = {t["name"]: t for t in doc["tables"]}
        if mode == "overwrite":
            doc["tables"] = []
        stamp = self._stamp()
        report = {"mode": mode, "added": [], "replaced": [], "unchanged": [], "removed": [], "changes": {}}
        current = {t["name"]: t for t in doc["tables"]}
        for t in tables:
            t = dict(t, loaded_at=stamp)
            tname = t["name"]
            cur, old = current.get(tname), before.get(tname)
            if t.get("ddl") and t["rows"] is None and cur is not None and cur.get("rows") is not None:
                # a table definition for a table that already has an export: attach the keys to it
                if (cur.get("ddl") or {}).get("fp") == t["ddl"]["fp"]:
                    report["unchanged"].append(tname)
                else:
                    current[tname] = dict(cur, ddl=t["ddl"], loaded_at=stamp)
                    (report["replaced"] if old else report["added"]).append(tname)
                    if old:
                        report["changes"][tname] = {"added": [], "removed": [], "retyped": [], "rows": [old["rows"], cur["rows"]],
                                                   "note": "keys and types updated from the table script"}
                continue
            if not t.get("ddl") and cur is not None and cur.get("ddl"):
                t["ddl"] = cur["ddl"]  # a refreshed export keeps the keys already loaded
            if old is None:
                report["added"].append(tname)
            elif old["fp"] == t["fp"]:
                report["unchanged"].append(tname)
                t = old  # nothing changed: keep the stored record as it is
            else:
                report["replaced"].append(tname)
                report["changes"][tname] = diff_table(old, t)
            current[tname] = t
        if mode == "overwrite":
            report["removed"] = sorted(set(before) - set(current))
        doc["tables"] = sorted(current.values(), key=lambda x: x["name"].lower())
        changed = existing is None or doc["tables"] != existing["tables"]
        report["noop"] = not changed
        if changed:
            doc["history"].append({"at": stamp, "kind": "tables", "mode": mode, "files": filenames,
                                   **{k: len(report[k]) for k in ("added", "replaced", "unchanged", "removed")},
                                   "updated": len(report["replaced"])})
            self.save(name, doc)
        return report

    def remove_table(self, name, table):
        doc = self.get(name)
        if not doc or not any(t["name"] == table for t in doc["tables"]):
            return False
        doc["tables"] = [t for t in doc["tables"] if t["name"] != table]
        self.save(name, doc)
        return True

    def load_docs(self, name, mode, parsed, intakes, title=None):
        """Add documentation files (parsed SDDs) and intake JSON to a document.

        'add' keeps what is stored: a file with the same name replaces the older copy, and the same
        content is a no-op. 'overwrite' replaces all documentation and intake. Code is never touched.
        """
        if mode not in ("add", "overwrite"):
            raise ValueError("Mode must be add or overwrite.")
        existing = self.get(name)
        doc = copy.deepcopy(existing) if existing else self._blank(name, title)
        report = {"mode": mode, "added": [], "updated": [], "unchanged": [], "removed": [], "intake": len(intakes)}
        if mode == "overwrite":
            keep = {d["fp"] for d in parsed}
            report["removed"] = [d["name"] for d in doc["documentation"] if d["fp"] not in keep]
            doc["documentation"], doc["intake"] = [], {}
        for d in parsed:
            same = next((x for x in doc["documentation"] if x["fp"] == d["fp"]), None)
            renamed = next((x for x in doc["documentation"] if x["name"] == d["name"] and x["fp"] != d["fp"]), None)
            if same:
                report["unchanged"].append(d["name"])
                continue
            if renamed:
                doc["documentation"].remove(renamed)
                report["updated"].append(d["name"])
            else:
                report["added"].append(d["name"])
            doc["documentation"].append(d)
        for i in intakes:
            doc["intake"] = deep_merge(doc["intake"], i)
        if title:
            doc["title"] = title
        changed = (existing is None or doc["documentation"] != existing["documentation"]
                   or doc["intake"] != existing["intake"] or doc["title"] != existing["title"])
        report["noop"] = not changed
        if changed:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            doc["history"].append({"at": stamp, "kind": "documentation", "mode": mode,
                                   "files": [d["name"] for d in parsed],
                                   **{k: (len(v) if isinstance(v, list) else v) for k, v in report.items()
                                      if k not in ("mode", "noop")}})
            self.save(name, doc)
        return report

    def remove_doc(self, name, fp):
        doc = self.get(name)
        if not doc:
            return False
        keep = [d for d in doc["documentation"] if d["fp"] != fp]
        if len(keep) == len(doc["documentation"]):
            return False
        doc["documentation"] = keep
        self.save(name, doc)
        return True

    # ----------------------------------------------------------------- recovery ---

    def import_from(self, folder, rename=True):
        """Copy stored documents from another folder into this one. Never overwrites anything.

        A document whose name is already taken arrives under a new name ("Lucy recovered-1"), or is
        skipped when rename is False.
        """
        folder = os.path.abspath(folder.strip().strip('"').strip("'"))
        if not os.path.isdir(folder):
            raise ValueError(f"{folder}: not a folder.")
        if os.path.abspath(self.root) == folder:
            raise ValueError("That is the folder the app already uses.")
        names = [f[:-9] for f in sorted(os.listdir(folder)) if f.endswith(".sdd.json")]
        if not names:
            raise ValueError(f"{folder}: no stored documents (*.sdd.json) in that folder.")
        imported = {}
        for n in names:
            try:
                with open(os.path.join(folder, n + ".sdd.json"), encoding="utf-8") as fh:
                    if "export" not in json.load(fh):
                        continue
            except (ValueError, OSError):
                continue
            target, i = n, 0
            while os.path.exists(self._path(target)):
                if not rename:
                    break
                i += 1
                target = f"{n} recovered-{i}"
            if os.path.exists(self._path(target)):
                continue
            if valid_name(target):
                shutil.copy2(os.path.join(folder, n + ".sdd.json"), self._path(target))
                imported[n] = target
        return imported


def find_stored_documents(data_dir, roots, max_depth=4):
    """Folders other than data_dir that hold stored documents: [(folder, [document names])].

    Looks for data/documents folders under the given roots, which is where earlier versions kept
    them, including inside a nested copy of the code folder.
    """
    skip = {".git", ".venv", "node_modules", "backups", "__pycache__", "staging"}
    here = os.path.abspath(data_dir)
    found, seen = [], set()
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root) or root == os.path.dirname(root):  # never walk a whole drive
            continue
        base = root.count(os.sep)
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            if r.count(os.sep) - base >= max_depth:
                dirs[:] = []
            names = sorted(f[:-9] for f in files if f.endswith(".sdd.json"))
            if names and os.path.abspath(r) != here and os.path.abspath(r) not in seen:
                seen.add(os.path.abspath(r))
                found.append((os.path.abspath(r), names))
    return found
