# SDD Generator (Automation Anywhere)

Reads an A360 bot export (zip or extracted folder) and drafts a Solution Design Document in the standard SDD layout. Deterministic: no network access and no LLM. Every derived fact comes from a specific node in the export.

```bash
pip install python-docx
python -m sdd_generator EXPORT.zip --out out --title "My Process"
python -m sdd_generator EXPORT.zip --intake sdd_generator/intake.example.json
```

Outputs in `--out`: `SDD.md`, `SDD.docx` (opens in Google Docs through Drive upload with conversion), `SDD.facts.json` (the parsed facts, for audit).

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
