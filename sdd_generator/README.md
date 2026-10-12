# SDD Generator (Automation Anywhere)

Reads an A360 bot export (zip or extracted folder) and drafts a Solution Design Document in the standard SDD layout. Deterministic: no network access and no LLM. Every derived fact comes from a specific node in the export.

```bash
pip install -r sdd_generator/requirements.txt
python -m sdd_generator EXPORT.zip --out out --title "My Process"
python -m sdd_generator EXPORT.zip --intake sdd_generator/intake.example.json
```

Outputs in `--out`: `SDD.md`, `SDD.docx` (opens in Google Docs through Drive upload with conversion), `SDD.facts.json` (the parsed facts, for audit).

## Web app (local)

```bat
run.bat
```

Opens http://localhost:8765/ (bound to 127.0.0.1 only). Elsewhere: `python -m sdd_generator.web [--port N] [--data-dir DIR]`.

**Left:** the document picker, two input menus (**Current Code** and **Documentation**), and the section menu. Each section shows how many placeholders are still open (yellow) and how many passages came from documentation (blue). **Right:** the expected SDD. Click a menu item to jump to that section.

**Priority.** What the code shows always wins. Next comes intake you typed in. Documentation only fills what would otherwise be `[TO BE COMPLETED]` or missing, and every passage from it sits under a blue *From documentation: file, page N* caption, because it was not verified against the code.

- **Current Code:** one or more export `.zip` files, chosen in the browser (streamed to disk in chunks, so size is limited only by disk space) or given as a path on this computer (a `.zip` or an extracted export folder; fastest for large exports). Only bot and manifest JSON is read, so a multi-gigabyte export loads in well under a second.
  - **Add:** merges bots into the stored code. A bot at the same path is replaced; a manifest-only record never erases bot logic already stored.
  - **Overwrite:** restarts the code from the files you load. Documentation and intake are kept.
  - **Reloading the same content is detected.** Zips are identified by the size and CRC of their entries, folders by file hashes, so renaming or moving a file does not fool it. A load that changes nothing says "No changes", writes nothing and adds no history entry. Loading an older version still applies it.
- **Documentation:** old SDDs as `.pdf`, `.docx` or `.md`, several at once, or a folder of them, plus an optional intake `.json` (see `intake.example.json`). Sections are recognised by title, whatever the numbering, so differently structured SDDs work. Text and tables are read; screenshots and diagrams are not.
  - **Add:** keeps what is stored. The same file again changes nothing; a file with the same name but new content replaces the old copy.
  - **Overwrite:** replaces all stored documentation and intake. Code is never touched.
  - **Remove** takes one file out. Documentation can be loaded before, after or without code.
  - What it fills: process description, MoSCoW, assumptions, requirements, success criteria, benefits, stakeholders, changes, business rules, ERD text. It also adds a documented process flow, documented triggers and reports, access and bot usernames for matching applications, and a list of documented bots not found in the code. Appendix G records what was read from each file and what was used.
- **Download:** Word (.docx) or Markdown (.md) of exactly what the preview shows.

### Where your data lives

Everything you load is stored automatically, as soon as you load it, in `C:\Dev\sdd-generator\data\documents` (set by `run.bat` through `SDD_DATA_DIR`; the page shows the folder in use). You never need to load anything again. Every save keeps the previous version in `backups\<document>\` (the last 10), and deleting a document leaves a copy there too. Documents stored beside the code by older versions are copied across once, automatically.

### Updates

If `C:\Dev\sdd-generator` is a git clone, `run.bat` runs `git pull --ff-only` before starting, and installs any new requirements, so new versions arrive on the next start. Your data folder is not in git (`.gitignore`) and is never touched by a pull.

### If the page behaves like an old version

The page header shows the version (currently 0.3.0). If it does not, or you see a bare "Failed to fetch", an older copy of the app is still running and holding port 8765, so the new one could not start. `run.bat` now says so and waits; to clear the old one on Windows:

```bat
netstat -ano | findstr :8765
taskkill /PID <number in the last column> /F
```

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
