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

**Layout.** The left menu starts with **Select Project**. Once a project is chosen, its menu opens: **SDD, Analysis, Code, Documentation, Videos, Graphs, Tables**. Clicking one shows its actions on the right. A new project starts at Code. Every load is saved at once; nothing needs reloading.

**Priority.** What the code shows always wins. Next comes intake you typed in. Everything else only fills what would otherwise be `[TO BE COMPLETED]` or missing, and each passage sits under a blue caption naming its source, because it was not verified against the code.

| Menu | What you do there | What it adds to the SDD |
|---|---|---|
| **SDD** | Read the current SDD, generated from everything loaded each time you open it. Download Word or Markdown. The section list sits under the menu item, with open-item and from-docs counts. | |
| **Analysis** | **The code is the source of truth, so anything found only outside it is assumed retired.** Two things matter here. **Rule differences:** documented rules (business rules, requirements, assumptions, process steps) are searched for facts that can be read exactly (durations, retry counts, lists of codes, file naming patterns) and compared with the same kind of fact in the step or bot whose label shares words with the rule. Each difference shows what the documentation says, what the code does and where, and can be **accepted as a valid change** with a note. An acceptance is tied to those exact values: if the documentation or the code changes later, the difference reopens by itself. Accepted differences appear in the SDD with your note; open ones carry `[CONFIRM]`. **Bots to provide:** only bots the code itself shows missing (another bot calls it, or the export manifest lists it, but its file is not there), with a CSV download. Documented bots or steps with no code, and exported tables no SQL touches, are listed apart under "assumed retired". | Business Rules (2d): a "Documentation compared with the code" table. |
| **Code** | **Update: only what changed** merges the bots in the files into the stored code (use it when one part changed). **Replace everything** is a fresh reload. Zips of any size, or a path. Reloading identical content says "No changes". | Everything technical: bots, calls, I/O, applications, triggers, tables the SQL touches, review findings. |
| **Documentation** | Old SDDs (`.pdf`, `.docx`, `.md`), several at once or a folder, plus an optional intake `.json`. Add, replace all, or remove one. | Process description, MoSCoW, requirements, benefits, stakeholders, rules, documented flows, triggers, reports, access details, and documented bots missing from the code. |
| **Videos** | Upload a video (stored) or give a path (linked, not copied). It plays in the app. Attach a transcript (`.srt`, `.vtt`, `.txt`, `.md`, `.docx`) and pick the SDD section it belongs in. | The transcript text, in the chosen section or Appendix H. The audio itself is not read; a video without a transcript adds nothing. |
| **Graphs** | Upload PNG, JPG, GIF or BMP images (ERDs, flows, screenshots, old or new), with a caption and an SDD section. | The image, embedded in the preview, the Word file and a Markdown bundle (`.zip` with a `media/` folder). |
| **Tables** | Exports of current tables as CSV, TSV or Excel (one table per sheet). **Update** adds new tables and replaces any whose name already exists, showing what changed (columns added, removed or retyped, row counts). **Replace all** makes the stored set match the files. A per-table Replace button loads a newer export of one table. | Column names, inferred types and row counts in the ERD section, checked against the columns the SQL writes. Only the shape is stored, never cell values. |

Details worth knowing:

- **No process flow is ever blank.** A bot with named Step actions is shown as its Steps, each with a line on what it does (bots run, tables touched, calls made, counts of the rest). A bot without any is shown as its actions in order, nested under If / Loop / Try, with runs of variable assignments collapsed, so the flow always comes from the code. Entry-point bots are in section 2a; the flows of bots that other bots call are in Appendix A.
- **Other files in the export.** Forms, processes and any JSON that is not a task bot are listed (Code tab, SDD section 3b), not dropped. Their contents are not analysed yet.

- Code loads: only bot and manifest JSON is read, so a multi-gigabyte export loads in well under a second. Zips are fingerprinted by entry size and CRC, so renaming a file does not fool the reload check. A manifest-only record never erases bot logic already stored.
- Replacing code keeps documentation, videos, graphs and tables; replacing documentation keeps code; and so on. Each input is independent.
- Documentation sections are recognised by title whatever the numbering. Text and tables are read; screenshots and diagrams are not (add them under Graphs).
- Uploaded videos and images are kept in `data\media\<project>\`; videos given by path stay where they are.

### Where your data lives

Everything you load is stored automatically, as soon as you load it, in `C:\Dev\sdd-generator\data\documents` (set by `run.bat` through `SDD_DATA_DIR`; the page shows the folder in use). You never need to load anything again. Every save keeps the previous version in `backups\<document>\` (the last 10), and deleting a document leaves a copy there too. If an install ever points at a different folder (for example after unzipping into a nested folder), the app looks for stored documents in the folders around it. When its own folder is empty it copies them across automatically at start-up. Otherwise the page shows them with an Import button, and "Recover documents from a folder" takes any folder by path. Recovery copies and never overwrites; a name clash arrives as "Lucy recovered-1".

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
