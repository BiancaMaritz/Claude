"""Persistent SDD documents: load files in, either overwriting or adding to what is there.

A document is a JSON file in the data directory holding the merged export (all bots seen),
the merged intake, and a history of loads. Generating the SDD is always a pure function of
that stored state, so the preview and the downloads can never disagree.
"""
from __future__ import annotations

import datetime
import json
import os
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
            "manifest_packages": ex.manifest_packages, "global_values": ex.global_values,
            "bots": {p: asdict(b) for p, b in ex.bots.items()}}


def export_from_dict(d: dict) -> Export:
    ex = Export(source=d.get("source", ""), has_manifest=d.get("has_manifest", False),
                has_bot_files=d.get("has_bot_files", False),
                manifest_packages=d.get("manifest_packages", []), global_values=d.get("global_values", []))
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
            return json.load(fh)

    def save(self, name, doc):
        doc["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self._path(name))  # never leave a half-written document

    def delete(self, name):
        if valid_name(name) and os.path.exists(self._path(name)):
            os.remove(self._path(name))
            return True
        return False

    def load(self, name, mode, exports, intakes, filenames, title=None):
        """Apply a load. mode: 'add' (merge into the document) or 'overwrite' (replace it)."""
        if mode not in ("add", "overwrite"):
            raise ValueError("Mode must be add or overwrite.")
        existing = self.get(name)
        if mode == "overwrite" or existing is None:
            doc = {"title": title or (existing or {}).get("title") or name, "intake": {},
                   "export": export_to_dict(Export(source=name)), "history": []}
        else:
            doc = existing
            if title:
                doc["title"] = title
        base = export_from_dict(doc["export"])
        report = {"mode": mode, "created": existing is None, "added": 0, "updated": 0, "unchanged": 0,
                  "removed": 0, "intake": 0}
        for ex in exports:
            a, u, s = merge_exports(base, ex)
            report["added"] += len(a)
            report["updated"] += len(u)
            report["unchanged"] += len(s)
        if mode == "overwrite" and existing:
            # report the replacement against what the document held before
            old = export_from_dict(existing["export"]).bots
            report.update(
                added=len(set(base.bots) - set(old)), removed=len(set(old) - set(base.bots)),
                updated=sum(1 for p in base.bots if p in old and _bot_key(old[p]) != _bot_key(base.bots[p])),
                unchanged=sum(1 for p in base.bots if p in old and _bot_key(old[p]) == _bot_key(base.bots[p])))
        for i in intakes:
            doc["intake"] = deep_merge(doc["intake"], i)
            report["intake"] += 1
        doc["export"] = export_to_dict(base)
        doc["history"].append({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                               "files": filenames, **report})
        self.save(name, doc)
        return report
