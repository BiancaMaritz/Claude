# AI Resume Screener (Claude + Tkinter)

Desktop app that screens multiple resumes against a job description using the Claude API and produces a ranked, exportable report.

## Features

- Masked Claude API key field (with a Show toggle). Pre-filled from `ANTHROPIC_API_KEY` if set. The key is never written to disk.
- Browse and queue multiple resumes: PDF, DOCX, TXT. Remove or clear files from the queue.
- Multi-line job description box, optional job title, and a model picker (`claude-opus-5` default, `claude-sonnet-5`).
- Submit processes resumes in parallel (3 at a time) on a background thread. The UI stays responsive, and a progress bar and Cancel button are provided.
- Each run opens a new **Results** tab with:
  - a sortable table (click any header; click again to reverse): rank, candidate, file, match %, status, relevant years, matched and missing skill counts
  - a detailed per-candidate report: match %, status, matched skills, missing skills, experience fit, strengths, gaps, suggested interview questions, and a summary
  - CSV export (Excel-friendly UTF-8 BOM) and PDF export (summary table plus one page per candidate)
- **Status rule:** `Selected` if match is 85% or higher, otherwise `Rejected`. The app applies this rule to Claude's score; the model does not decide it.

## Setup

Requires Python 3.10+ with Tk (on Debian/Ubuntu: `sudo apt install python3-tk`; Windows and macOS python.org installers include Tk).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m resume_screener
```

## How it works

| Module | Responsibility |
|---|---|
| `resume_screener/extractors.py` | Text extraction (pypdf, python-docx including tables, multi-encoding TXT). PDFs with little or no text layer (scanned) are sent to Claude as native PDF documents instead. |
| `resume_screener/analyzer.py` | Claude call via `client.beta.messages.parse` with a Pydantic schema (structured output), so every response is validated JSON. The job description sits in a cached system block, so it is billed at the cache rate across a batch. |
| `resume_screener/reports.py` | Plain-text report, CSV and PDF export (reportlab). |
| `resume_screener/gui.py` | Tkinter UI, worker thread and queue, sortable results tabs. |

### Screening prompt

The system prompt instructs Claude to act as a senior HR screener, to score only on job-relevant evidence, to weight must-have requirements most heavily, and to ignore protected attributes (age, gender, ethnicity, nationality, religion, family status, disability, photo, address). Employment gaps are not penalised unless the JD makes recency an explicit requirement.

### Error handling

- Per-file failures (corrupt, password-protected, unsupported or empty files) become `Error` rows; the rest of the batch continues.
- API errors are mapped to plain-language messages (invalid key, permissions, rate limit, server error, network). The SDK retries 429/5xx/connection errors up to 4 times with backoff.
- An invalid API key stops the batch after the first failure to avoid pointless requests.
- A `refusal` or `max_tokens` stop reason is reported per candidate instead of failing silently.
- With `claude-opus-5`, the request opts into server-side refusal fallback (`fallbacks: "default"`), so a safety-classifier decline is re-run on Anthropic's recommended fallback model within the same call.

## Tests

```bash
pip install pytest
python -m pytest -q
```

Tests cover extraction, the request shape sent to Claude (with a stubbed client), the 85% threshold boundary, error mapping, ranking, and CSV/PDF export. No API key or network is needed.

## Limitations

- AI scoring supports, and does not replace, human review. Audit a sample of Selected and Rejected outcomes against your own judgement before relying on the threshold, and check obligations under local anti-discrimination and privacy law (for example the Australian Privacy Principles) before processing candidate data with a third-party API.
- Scanned PDFs rely on Claude reading the page images; DOCX files with text in images or text boxes may extract incompletely.
- Cost scales with resume count and length. Each resume is one request.
