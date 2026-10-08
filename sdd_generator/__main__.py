"""CLI: python -m sdd_generator EXPORT.zip [--intake intake.json] [--out DIR]"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from . import __version__
from .analyze import analyse
from .document import build, load_intake
from .loader import load_export
from .render import to_docx, to_markdown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sdd_generator", description=__doc__)
    ap.add_argument("export", help="A360 export: .zip file or extracted folder")
    ap.add_argument("--intake", help="JSON file with the business sections (see README)")
    ap.add_argument("--out", default="out", help="output folder (default: out)")
    ap.add_argument("--name", default="SDD", help="output file base name")
    ap.add_argument("--title", help="document title (default: derived from the export name)")
    ap.add_argument("--date", help="document date, YYYY-MM-DD (default: today)")
    ap.add_argument("--format", default="md,docx,json", help="comma list of md, docx, json")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    ex = load_export(args.export)
    if not ex.bots:
        print("No bots found in the export.", file=sys.stderr)
        return 1
    if not ex.has_bot_files:
        print("Warning: manifest only. Variables, steps, SQL and I/O need the bot files.", file=sys.stderr)

    a = analyse(ex)
    title = args.title or os.path.splitext(os.path.basename(args.export.rstrip("/\\")))[0].replace("_", " ")
    blocks = build(ex, a, load_intake(args.intake), title=title,
                   date=args.date or datetime.date.today().isoformat())

    os.makedirs(args.out, exist_ok=True)
    fmts = {f.strip() for f in args.format.split(",")}
    base = os.path.join(args.out, args.name)
    if "md" in fmts:
        with open(base + ".md", "w", encoding="utf-8") as fh:
            fh.write(to_markdown(blocks))
    if "docx" in fmts:
        to_docx(blocks, base + ".docx")
    if "json" in fmts:
        facts = {
            "bots": {p: {"ref": a["ref"][p], "calls": a["calls"][p]} for p in sorted(ex.bots)},
            "entry_points": a["entry"], "cycles": a["cycles"], "tables": a["tables"],
            "rest": a["rest"], "vault_references": a["vault"], "packages": a["packages"],
            "findings": a["hygiene"],
        }
        with open(base + ".facts.json", "w", encoding="utf-8") as fh:
            json.dump(facts, fh, indent=2)
    print(f"{len(ex.bots)} bots -> {os.path.abspath(args.out)} ({', '.join(sorted(fmts))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
