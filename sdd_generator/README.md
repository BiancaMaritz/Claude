# SDD Generator (Automation Anywhere)

Reads an A360 bot export (zip or extracted folder) and drafts a Solution Design Document in the standard SDD layout. Deterministic: no network access and no LLM. Every derived fact comes from a specific node in the export.

```bash
pip install python-docx
python -m sdd_generator EXPORT.zip --out out --title "My Process"
python -m sdd_generator EXPORT.zip --intake sdd_generator/intake.example.json
```

Outputs in `--out`: `SDD.md`, `SDD.docx` (opens in Google Docs through Drive upload with conversion), `SDD.facts.json` (the parsed facts, for audit).

## Web app (local)

```bat
run.bat
```

Opens http://localhost:8765/ (bound to 127.0.0.1 only). Elsewhere: `python -m sdd_generator.web [--port N] [--data-dir DIR]`.

- **Left:** document picker, a Load files panel, and the section menu. Each section shows how many placeholders are still open.
- **Right:** the expected SDD, rendered from the stored document. Click a menu item to jump to that section.
- **Load files:** one or more export `.zip` files and an optional intake `.json`, either chosen in the browser (streamed to disk in chunks, so size is limited only by disk space) or given as a **path on this computer** (a `.zip`, an extracted export folder, or a `.json`). A path skips the upload step entirely, so it is the fastest way to load a large export. Only bot and manifest JSON is read; jars, screenshots and other binaries are skipped without being opened, so a multi-gigabyte export loads in well under a second.
  - **Add to document:** merges bots into the stored document. A bot with the same path replaces the old one; a manifest-only record never erases bot logic already stored. Intake files merge (lists append without duplicates, other values are replaced).
  - **Overwrite document:** the document restarts from the files you load. You are asked to confirm.
  - Each load reports bots added, updated, unchanged and removed (with the names of the changed ones), and is kept in the load history.
  - **Reloading the same content is detected.** Zips are identified by the size and CRC of their entries, folders by file hashes, so renaming or moving a file does not fool it. If a load changes nothing the app says "No changes", writes nothing and adds no history entry. Reloading an older version still applies it, because the document may have moved on since.
- **Download:** Word (.docx) or Markdown (.md) of exactly what the preview shows.

Documents are stored as JSON in `data/documents/` next to the package (override with `--data-dir`). They contain the bot logic from your exports, so keep that folder out of git (it is in `.gitignore`).

## What is filled in from the export

| SDD section | Source |
|---|---|
| 2a Process flow steps | Named Step actions in each entry-point bot |
| 2c ERD | Tables and written columns from SQL in Database actions |
| 3a Applications and systems | Packages in use, plus `<name> Login` bots |
| 3b Bot components | Manifest paths, runTask calls, folder-based roles |
| 3c Triggers | Entry points only; schedules are not in an export |
| 3d Inputs/outputs | Variable input/output flags and types; identical signatures grouped |
| 3e Reports and logs | REST logging endpoints, error-emailer calls, log-to-file actions |
| Appendices | Decision points, vault reference names, package versions, review findings, developer comments |

Sections 1, 2b, 2d and 2e are business content. Supply them through `--intake` (see `intake.example.json`); otherwise they stay as highlighted `[TO BE COMPLETED]` placeholders. Access levels, bot usernames, trigger types and complexity stay as `[CONFIRM]`.

## Rules worth knowing

- Roles: bots under `Logic Manager` are Main; under `Common Tasks`, `Utilities` or `Task Logging` are Common, unless nothing calls them; the rest are Sub.
- Entry points are bots no other bot calls. Bots that call each other count as one entry group and are reported as a circular call.
- Credential vault references are listed by name only. Defaults of STRING variables with names like password, token or key are masked. REST headers and bodies are never read into the output.
- Review findings are observations (leftover copies, duplicate bots, message boxes in unattended bots, manifest drift, package version sprawl), not confirmed defects.

## Limits

- Tested against one A360 export (bot code version 7). Other versions may use different node shapes.
- Decision points show the condition as written (for example `folder does not exists(folderPath=$RootDirectory$)`). Turning them into business rules is a human step.
- The Word TOC is a field; Word asks to update it on open.
