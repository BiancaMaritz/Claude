"""CSV and PDF export plus plain-text candidate reports."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from .analyzer import SELECTION_THRESHOLD, CandidateReport

CSV_COLUMNS = [
    "Rank",
    "Candidate",
    "File",
    "Match %",
    "Status",
    "Relevant Experience (yrs)",
    "Matched Skills",
    "Missing Skills",
    "Experience Fit",
    "Strengths",
    "Gaps",
    "Interview Questions",
    "Summary",
    "Error",
]


def ranked(reports: Iterable[CandidateReport]) -> list[CandidateReport]:
    return sorted(reports, key=lambda r: (r.ok, r.match), reverse=True)


def format_text_report(report: CandidateReport) -> str:
    lines = [f"Candidate: {report.candidate_name}", f"File: {report.filename}"]
    if not report.ok:
        lines += ["Status: Error", "", f"Error: {report.error}"]
        return "\n".join(lines)

    r = report.result
    lines += [
        f"Match: {report.match}%",
        f"Status: {report.status} (threshold {SELECTION_THRESHOLD}%)",
        f"Relevant experience: {r.relevant_experience_years:g} years",
    ]
    if report.model_used:
        lines.append(f"Assessed by: {report.model_used}")
    for note in report.notes:
        lines.append(f"Note: {note}")

    def section(title: str, items: list[str]) -> None:
        lines.extend(["", title.upper()])
        lines.extend(f"  - {i}" for i in items) if items else lines.append("  (none)")

    section("Matched skills", r.matched_skills)
    section("Missing skills", r.missing_skills)
    lines += ["", "EXPERIENCE FIT", f"  {r.experience_fit}"]
    section("Strengths", r.strengths)
    section("Gaps", r.gaps)
    section("Suggested interview questions", r.interview_questions)
    lines += ["", "SUMMARY", f"  {r.summary}"]
    return "\n".join(lines)


def export_csv(reports: Iterable[CandidateReport], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM for Excel
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for rank, rep in enumerate(ranked(reports), start=1):
            r = rep.result
            writer.writerow(
                [
                    rank,
                    rep.candidate_name,
                    rep.filename,
                    rep.match if rep.ok else "",
                    rep.status,
                    f"{r.relevant_experience_years:g}" if r else "",
                    "; ".join(r.matched_skills) if r else "",
                    "; ".join(r.missing_skills) if r else "",
                    r.experience_fit if r else "",
                    "; ".join(r.strengths) if r else "",
                    "; ".join(r.gaps) if r else "",
                    "; ".join(r.interview_questions) if r else "",
                    r.summary if r else "",
                    rep.error or "",
                ]
            )


def export_pdf(reports: Iterable[CandidateReport], path: str | Path, job_title: str = "") -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    reports = ranked(reports)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=8.5, leading=11)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=13)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], spaceBefore=8, spaceAfter=3)
    green, red, grey = colors.HexColor("#1b7f3b"), colors.HexColor("#b3261e"), colors.HexColor("#666666")

    def p(text: str, style=body) -> Paragraph:
        return Paragraph(escape(str(text)), style)

    def bullets(items: list[str]) -> list:
        if not items:
            return [p("(none)", small)]
        return [Paragraph("&bull; " + escape(i), small) for i in items]

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title="Resume Screening Report",
    )
    story: list = [
        Paragraph("Resume Screening Report", styles["Title"]),
        p(f"Generated {datetime.now():%Y-%m-%d %H:%M}" + (f" | Role: {job_title}" if job_title else "")),
    ]
    ok = [r for r in reports if r.ok]
    selected = sum(1 for r in ok if r.status == "Selected")
    story += [
        p(
            f"Candidates screened: {len(reports)} | Selected: {selected} | "
            f"Rejected: {len(ok) - selected} | Errors: {len(reports) - len(ok)} | "
            f"Selection threshold: {SELECTION_THRESHOLD}%"
        ),
        Spacer(1, 6 * mm),
    ]

    header = ["#", "Candidate", "File", "Match", "Status"]
    rows = [header] + [
        [str(i), p(r.candidate_name, small), p(r.filename, small), f"{r.match}%" if r.ok else "-", r.status]
        for i, r in enumerate(reports, start=1)
    ]
    table = Table(rows, colWidths=[10 * mm, 50 * mm, 70 * mm, 18 * mm, 22 * mm], repeatRows=1)
    style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f3b52")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8c8c8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f9")]),
        ]
    )
    for i, r in enumerate(reports, start=1):
        style.add("TEXTCOLOR", (4, i), (4, i), green if r.status == "Selected" else red if r.status == "Rejected" else grey)
    table.setStyle(style)
    story.append(table)

    for r in reports:
        story.append(PageBreak())
        colour = green if r.status == "Selected" else red if r.status == "Rejected" else grey
        head = ParagraphStyle("head", parent=styles["Heading2"], textColor=colour)
        story.append(Paragraph(escape(f"{r.candidate_name}  |  {r.status}" + (f"  |  {r.match}%" if r.ok else "")), head))
        story.append(p(f"File: {r.filename}", small))
        if not r.ok:
            story.append(p(f"Error: {r.error}"))
            continue
        res = r.result
        story.append(p(f"Relevant experience: {res.relevant_experience_years:g} years", small))
        for note in r.notes:
            story.append(p(f"Note: {note}", small))
        story += [Paragraph("Summary", h3), p(res.summary)]
        story += [Paragraph("Experience fit", h3), p(res.experience_fit)]
        for title, items in (
            ("Matched skills", res.matched_skills),
            ("Missing skills", res.missing_skills),
            ("Strengths", res.strengths),
            ("Gaps", res.gaps),
            ("Suggested interview questions", res.interview_questions),
        ):
            story.append(KeepTogether([Paragraph(title, h3), *bullets(items)]))

    doc.build(story)
