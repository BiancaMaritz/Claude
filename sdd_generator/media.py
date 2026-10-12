"""Videos, graphs and their transcripts: file types, SDD section choices, transcript parsing.

A video is stored (or linked) and played in the app. What it adds to the SDD is its transcript,
which the user attaches as .srt, .vtt, .txt, .md or .docx. Reading the audio itself would need a
speech-to-text model and is not part of this version.
"""
from __future__ import annotations

import os
import re
import secrets

VIDEO_EXT = (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp")  # the formats Word can embed
TRANSCRIPT_EXT = (".srt", ".vtt", ".txt", ".md", ".docx")
CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
                 ".webm": "video/webm", ".avi": "video/x-msvideo", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".gif": "image/gif", ".bmp": "image/bmp"}

# Where a video transcript or a graph can be placed in the SDD. "appendix" is the default.
SECTIONS = [
    ("appendix", "Appendix (not tied to a section)"),
    ("process_description", "1a. Process Description"), ("moscow", "1b. Priority (MoSCoW)"),
    ("assumptions", "1c. Assumptions"), ("functional_requirements", "1d. Functional Requirements"),
    ("success_criteria", "1e. Success Criteria"), ("benefits", "1f. Expected Benefits"),
    ("stakeholders", "1g. Stakeholders"), ("process_flow", "2a. To Be Process Flow Steps"),
    ("changes_by_automation", "2b. Changes brought about by Automation"), ("erd", "2c. ERD Design"),
    ("business_rules", "2d. Business Rules"), ("applications", "3a. Applications & Systems"),
    ("bot_components", "3b. Bot Components"), ("triggers", "3c. Triggers"),
    ("inputs_outputs", "3d. Inputs/Outputs"), ("reports_logs", "3e. Reports and Logs"),
]
SECTION_KEYS = {k for k, _ in SECTIONS}


def new_id() -> str:
    return secrets.token_hex(6)


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


# ------------------------------------------------------------- transcripts ---

TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")


def _secs(stamp):
    m = TIME_RE.fullmatch(stamp.strip())
    if not m:
        return None
    h, mi, s, ms = int(m.group(1) or 0), int(m.group(2)), int(m.group(3)), m.group(4)
    return h * 3600 + mi * 60 + s + (int(ms.ljust(3, "0")) / 1000 if ms else 0)


def _cues_from_timed(text):
    """Cues from SRT or WebVTT text: [{t: seconds, text}]."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            if "-->" in ln:
                t = _secs(ln.split("-->")[0])
                body = " ".join(re.sub(r"<[^>]+>", "", x).strip() for x in lines[i + 1:])
                if body:
                    cues.append({"t": t, "text": body})
                break
    return cues


def parse_transcript(path, label):
    """Read a transcript file into {name, cues, chars}. Raises ValueError when there is no text."""
    ext = ext_of(label)
    if ext == ".docx":
        import docx

        text = "\n\n".join(p.text for p in docx.Document(path).paragraphs if p.text.strip())
    elif ext in (".srt", ".vtt", ".txt", ".md"):
        raw = open(path, "rb").read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    else:
        raise ValueError(f"{label}: a transcript must be .srt, .vtt, .txt, .md or .docx.")
    cues = _cues_from_timed(text) if ext in (".srt", ".vtt") or "-->" in text[:2000] else []
    if not cues:  # plain text: one cue per paragraph
        cues = [{"t": None, "text": re.sub(r"\s+", " ", p).strip()} for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not cues:
        raise ValueError(f"{label}: no text found in the transcript.")
    return {"name": label, "cues": cues, "chars": sum(len(c["text"]) for c in cues)}


def stamp(seconds):
    s = int(seconds)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:d}:{s % 60:02d}"


def transcript_paragraphs(cues, limit=450):
    """Group cues into readable paragraphs, each starting with the time of its first cue."""
    out, cur, start = [], "", None
    for c in cues:
        if start is None:
            start = c["t"]
        cur = (cur + " " + c["text"]).strip()
        if len(cur) >= limit:
            out.append((f"[{stamp(start)}] " if start is not None else "") + cur)
            cur, start = "", None
    if cur:
        out.append((f"[{stamp(start)}] " if start is not None else "") + cur)
    return out


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
