"""Load an A360 export (zip or extracted directory) into plain Python objects.

Bot files in an export are extension-less JSON documents under
"Automation Anywhere/Bots/...". manifest.json lists them with their
scanned dependencies, descriptions and the package versions in use.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

ROOT = "Automation Anywhere"


def norm(path: str) -> str:
    """Normalise to forward slashes, rooted at 'Automation Anywhere'."""
    p = unquote(path).replace("\\", "/")
    p = p.replace("repository:///", "")
    if ROOT + "/" in p:
        p = p[p.index(ROOT + "/"):]
    return p.strip("/")


@dataclass
class Bot:
    path: str
    description: str = ""
    tags: list = field(default_factory=list)
    manifest_deps: list = field(default_factory=list)
    data: dict | None = None  # None when only the manifest was available

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        parts = self.path.split("/")
        return "/".join(parts[2:-1]) if parts[:2] == [ROOT, "Bots"] else "/".join(parts[:-1])

    @property
    def variables(self) -> list:
        return (self.data or {}).get("variables", [])

    @property
    def nodes(self) -> list:
        return (self.data or {}).get("nodes", [])

    @property
    def triggers(self) -> list:
        return (self.data or {}).get("triggers", [])


@dataclass
class Export:
    source: str
    bots: dict = field(default_factory=dict)  # path -> Bot
    manifest_packages: list = field(default_factory=list)
    global_values: list = field(default_factory=list)
    has_manifest: bool = False
    has_bot_files: bool = False
    fingerprint: str = ""  # identifies the content of the source, not its name or location


# Exports carry jars, screenshots and other binaries that can total gigabytes. Only bot and
# manifest JSON is ever read; everything else is skipped without being opened.
SKIP_EXT = (".jar", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".dll", ".exe", ".zip", ".7z",
            ".mp4", ".avi", ".mov", ".pdf", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".bin", ".so")
MAX_READ_BYTES = 64 * 1024 * 1024


def _wanted(rel: str, size: int) -> bool:
    low = rel.replace("\\", "/")
    base = low.rsplit("/", 1)[-1]
    if size > MAX_READ_BYTES or base.lower().endswith(SKIP_EXT):
        return False
    return base == "manifest.json" or ROOT + "/" in low + "/"


def _scan(source: str, parts: list):
    """Yield (relative_path, bytes) for the files worth reading.

    Appends one (name, size, check) tuple per file to `parts` for the fingerprint. For a zip the
    check is the CRC32 already stored in the central directory, so the whole archive never has to
    be read to fingerprint it. For a folder it is a hash of the bytes that were read.
    """
    if os.path.isdir(source):
        for r, _, fs in os.walk(source):
            for f in fs:
                full = os.path.join(r, f)
                rel = os.path.relpath(full, source).replace("\\", "/")
                size = os.path.getsize(full)
                if _wanted(rel, size):
                    with open(full, "rb") as fh:
                        blob = fh.read()
                    parts.append((rel, size, hashlib.sha256(blob).hexdigest()))
                    yield rel, blob
                else:
                    parts.append((rel, size, ""))
    else:
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                parts.append((name, info.file_size, format(info.CRC, "08x")))
                if _wanted(name, info.file_size):
                    yield info.filename, z.read(info)


def load_export(source: str) -> Export:
    ex = Export(source=source)
    manifest = None
    raw_bots = {}
    parts: list = []
    for rel, blob in _scan(source, parts):
        base = rel.replace("\\", "/").rsplit("/", 1)[-1]
        if base == "manifest.json":
            manifest = json.loads(blob)
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if isinstance(data, dict) and "nodes" in data:
            raw_bots[norm(rel)] = data
    ex.fingerprint = hashlib.sha256(json.dumps(sorted(parts)).encode()).hexdigest()

    if manifest:
        ex.has_manifest = True
        ex.manifest_packages = manifest.get("packages", [])
        ex.global_values = manifest.get("globalValues", [])
        for f in manifest.get("files", []):
            if f.get("contentType") != "application/vnd.aa.taskbot":
                continue
            p = norm(f["path"])
            ex.bots[p] = Bot(
                path=p,
                description=f.get("description") or "",
                tags=f.get("tags") or [],
                manifest_deps=[norm(d) for d in f.get("scannedDependencies") or []],
            )
    for p, data in raw_bots.items():
        ex.bots.setdefault(p, Bot(path=p)).data = data
    ex.has_bot_files = bool(raw_bots)
    return ex
