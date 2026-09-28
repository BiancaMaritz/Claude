"""Tkinter desktop UI for AI resume screening."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .analyzer import AVAILABLE_MODELS, DEFAULT_MODEL, SELECTION_THRESHOLD, CandidateReport, ResumeAnalyzer
from .extractors import SUPPORTED_EXTENSIONS, ExtractionError, extract_resume
from .reports import export_csv, export_pdf, format_text_report

MAX_WORKERS = 3
MIN_JD_CHARS = 50
COLUMNS = (
    ("rank", "#", 40, "center"),
    ("candidate", "Candidate", 180, "w"),
    ("file", "File", 200, "w"),
    ("match", "Match %", 80, "center"),
    ("status", "Status", 90, "center"),
    ("experience", "Exp (yrs)", 80, "center"),
    ("matched", "Matched", 70, "center"),
    ("missing", "Missing", 70, "center"),
)
NUMERIC_COLUMNS = {"rank", "match", "experience", "matched", "missing"}


class ResultsTab(ttk.Frame):
    """One screening run: sortable table, per-candidate report, export."""

    def __init__(self, master: ttk.Notebook, job_title: str, total: int):
        super().__init__(master, padding=8)
        self.job_title = job_title
        self.total = total
        self.reports: dict[str, CandidateReport] = {}
        self._sort_state: dict[str, bool] = {}

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.summary_var = tk.StringVar(value=f"0 of {total} processed")
        ttk.Label(top, textvariable=self.summary_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Close Tab", command=self._close).pack(side="right")
        ttk.Button(top, text="Export PDF", command=self._export_pdf).pack(side="right", padx=4)
        ttk.Button(top, text="Export CSV", command=self._export_csv).pack(side="right")

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(8, 0))

        table_frame = ttk.Frame(pane)
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        for key, label, width, anchor in COLUMNS:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=key in ("candidate", "file"))
        self.tree.tag_configure("Selected", foreground="#1b7f3b")
        self.tree.tag_configure("Rejected", foreground="#b3261e")
        self.tree.tag_configure("Error", foreground="#777777")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        pane.add(table_frame, weight=1)

        detail_frame = ttk.LabelFrame(pane, text="Candidate Report", padding=4)
        self.detail = tk.Text(detail_frame, wrap="word", height=16, state="disabled", font=("TkFixedFont", 10))
        dsb = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        pane.add(detail_frame, weight=1)

    def add_report(self, report: CandidateReport) -> None:
        iid = str(len(self.reports))
        self.reports[iid] = report
        r = report.result
        values = {
            "rank": "",
            "candidate": report.candidate_name,
            "file": report.filename,
            "match": report.match if report.ok else "",
            "status": report.status,
            "experience": f"{r.relevant_experience_years:g}" if r else "",
            "matched": len(r.matched_skills) if r else "",
            "missing": len(r.missing_skills) if r else "",
        }
        self.tree.insert("", "end", iid=iid, values=[values[c[0]] for c in COLUMNS], tags=(report.status,))
        self._rerank()
        self._update_summary()
        if len(self.reports) == 1:
            self.tree.selection_set(iid)

    def finish(self, cancelled: bool) -> None:
        self._update_summary(done=True, cancelled=cancelled)
        self.sort_by("match", descending=True)

    def _rerank(self) -> None:
        order = sorted(self.reports, key=lambda i: (self.reports[i].ok, self.reports[i].match), reverse=True)
        for rank, iid in enumerate(order, start=1):
            self.tree.set(iid, "rank", rank)

    def _update_summary(self, done: bool = False, cancelled: bool = False) -> None:
        reps = self.reports.values()
        sel = sum(1 for r in reps if r.status == "Selected")
        rej = sum(1 for r in reps if r.status == "Rejected")
        err = sum(1 for r in reps if r.status == "Error")
        state = " (cancelled)" if cancelled else " (complete)" if done else ""
        self.summary_var.set(
            f"{len(self.reports)} of {self.total} processed{state}  |  Selected: {sel}  |  Rejected: {rej}  |  "
            f"Errors: {err}  |  Threshold: {SELECTION_THRESHOLD}%"
        )

    def sort_by(self, column: str, descending: bool | None = None) -> None:
        if descending is None:
            descending = not self._sort_state.get(column, column in NUMERIC_COLUMNS)
        self._sort_state[column] = descending

        def key(iid: str):
            value = self.tree.set(iid, column)
            if column in NUMERIC_COLUMNS:
                try:
                    return (1, float(value))
                except ValueError:
                    return (0, 0.0)  # blanks (errors) sink to the bottom when descending
            return (1, str(value).lower())

        for index, iid in enumerate(sorted(self.tree.get_children(""), key=key, reverse=descending)):
            self.tree.move(iid, "", index)
        for key_, label, *_ in COLUMNS:
            arrow = (" ▼" if descending else " ▲") if key_ == column else ""
            self.tree.heading(key_, text=label + arrow)

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", format_text_report(self.reports[sel[0]]))
        self.detail.configure(state="disabled")

    def _default_name(self, ext: str) -> str:
        return f"screening_report_{datetime.now():%Y%m%d_%H%M}{ext}"

    def _export(self, ext: str, label: str, writer) -> None:
        if not self.reports:
            messagebox.showinfo("Export", "No results to export yet.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=ext, initialfile=self._default_name(ext), filetypes=[(label, f"*{ext}")]
        )
        if not path:
            return
        try:
            writer(list(self.reports.values()), path)
        except PermissionError:
            messagebox.showerror("Export failed", f"Cannot write to {path}. Is the file open in another program?", parent=self)
        except Exception as exc:
            messagebox.showerror("Export failed", f"{type(exc).__name__}: {exc}", parent=self)
        else:
            messagebox.showinfo("Export", f"Report saved to:\n{path}", parent=self)

    def _export_csv(self) -> None:
        self._export(".csv", "CSV file", export_csv)

    def _export_pdf(self) -> None:
        self._export(".pdf", "PDF file", lambda reps, path: export_pdf(reps, path, self.job_title))

    def _close(self) -> None:
        if getattr(self, "running", False):
            messagebox.showwarning("Busy", "Screening is still running for this tab.", parent=self)
            return
        self.master.forget(self)
        self.destroy()


class ResumeScreenerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI Resume Screener (Claude)")
        self.geometry("1100x780")
        self.minsize(860, 620)
        self.files: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_tab: ResultsTab | None = None

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_setup_tab()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)

    # ---------- layout ----------
    def _build_setup_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Screening Setup")
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="Claude API key:").grid(row=0, column=0, sticky="w", pady=4)
        self.api_key_var = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.api_entry = ttk.Entry(tab, textvariable=self.api_key_var, show="•")
        self.api_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tab, text="Show", variable=self.show_key,
            command=lambda: self.api_entry.configure(show="" if self.show_key.get() else "•"),
        ).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(tab, text="Model:").grid(row=1, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        ttk.Combobox(tab, textvariable=self.model_var, values=AVAILABLE_MODELS, state="readonly", width=24).grid(
            row=1, column=1, sticky="w", pady=4
        )

        ttk.Label(tab, text="Job title (optional):").grid(row=2, column=0, sticky="w", pady=4)
        self.job_title_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.job_title_var).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(tab, text="Resumes:").grid(row=3, column=0, sticky="nw", pady=4)
        files_frame = ttk.Frame(tab)
        files_frame.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=4)
        files_frame.columnconfigure(0, weight=1)
        self.file_list = tk.Listbox(files_frame, height=6, selectmode="extended")
        self.file_list.grid(row=0, column=0, rowspan=4, sticky="nsew")
        fsb = ttk.Scrollbar(files_frame, orient="vertical", command=self.file_list.yview)
        fsb.grid(row=0, column=1, rowspan=4, sticky="ns")
        self.file_list.configure(yscrollcommand=fsb.set)
        ttk.Button(files_frame, text="Browse...", command=self._browse).grid(row=0, column=2, sticky="ew", padx=(6, 0))
        ttk.Button(files_frame, text="Remove", command=self._remove_selected).grid(row=1, column=2, sticky="ew", padx=(6, 0), pady=2)
        ttk.Button(files_frame, text="Clear", command=self._clear_files).grid(row=2, column=2, sticky="ew", padx=(6, 0))
        self.file_count_var = tk.StringVar(value="0 files")
        ttk.Label(files_frame, textvariable=self.file_count_var).grid(row=3, column=2, sticky="s")

        ttk.Label(tab, text="Job description:").grid(row=4, column=0, sticky="nw", pady=4)
        jd_frame = ttk.Frame(tab)
        jd_frame.grid(row=4, column=1, columnspan=2, sticky="nsew", pady=4)
        tab.rowconfigure(4, weight=1)
        self.jd_text = tk.Text(jd_frame, wrap="word", height=14, undo=True)
        jsb = ttk.Scrollbar(jd_frame, orient="vertical", command=self.jd_text.yview)
        self.jd_text.configure(yscrollcommand=jsb.set)
        self.jd_text.pack(side="left", fill="both", expand=True)
        jsb.pack(side="right", fill="y")

        actions = ttk.Frame(tab)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        actions.columnconfigure(2, weight=1)
        self.submit_btn = ttk.Button(actions, text="Submit", command=self._submit)
        self.submit_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(actions, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=6)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.grid(row=0, column=2, sticky="ew", padx=6)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(tab, textvariable=self.status_var, foreground="#555").grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))

    # ---------- file handling ----------
    def _browse(self) -> None:
        patterns = " ".join(f"*{e}" for e in SUPPORTED_EXTENSIONS)
        paths = filedialog.askopenfilenames(
            parent=self, title="Select resumes",
            filetypes=[("Resumes", patterns), ("PDF", "*.pdf"), ("Word", "*.docx"), ("Text", "*.txt")],
        )
        existing = {p.resolve() for p in self.files}
        skipped = []
        for raw in paths:
            p = Path(raw)
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped.append(p.name)
            elif p.resolve() not in existing:
                self.files.append(p)
                existing.add(p.resolve())
        if skipped:
            messagebox.showwarning("Unsupported files", "Skipped:\n" + "\n".join(skipped), parent=self)
        self._refresh_files()

    def _remove_selected(self) -> None:
        for index in reversed(self.file_list.curselection()):
            del self.files[index]
        self._refresh_files()

    def _clear_files(self) -> None:
        self.files.clear()
        self._refresh_files()

    def _refresh_files(self) -> None:
        self.file_list.delete(0, "end")
        for p in self.files:
            self.file_list.insert("end", p.name)
        self.file_count_var.set(f"{len(self.files)} file{'s' if len(self.files) != 1 else ''}")

    # ---------- run ----------
    def _validate(self) -> tuple[str, str] | None:
        key = self.api_key_var.get().strip()
        jd = self.jd_text.get("1.0", "end").strip()
        problems = []
        if not key:
            problems.append("Enter your Claude API key.")
        elif not key.startswith("sk-ant-"):
            problems.append("The API key format looks wrong (expected it to start with 'sk-ant-').")
        if not self.files:
            problems.append("Add at least one resume (PDF, DOCX or TXT).")
        if len(jd) < MIN_JD_CHARS:
            problems.append(f"Enter a job description (at least {MIN_JD_CHARS} characters).")
        if problems:
            messagebox.showerror("Cannot submit", "\n".join(problems), parent=self)
            return None
        return key, jd

    def _submit(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        validated = self._validate()
        if not validated:
            return
        key, jd = validated
        try:
            analyzer = ResumeAnalyzer(api_key=key, model=self.model_var.get())
        except Exception as exc:
            messagebox.showerror("Setup error", str(exc), parent=self)
            return

        files = list(self.files)
        title = self.job_title_var.get().strip()
        tab = ResultsTab(self.notebook, job_title=title, total=len(files))
        tab.running = True
        label = f"Results {datetime.now():%H:%M:%S}" + (f" - {title[:20]}" if title else "")
        self.notebook.add(tab, text=label)
        self.notebook.select(tab)
        self.active_tab = tab

        self.cancel_event.clear()
        self.progress.configure(maximum=len(files), value=0)
        self.submit_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_var.set(f"Screening {len(files)} resume(s) with {analyzer.model}...")
        self.worker = threading.Thread(target=self._run, args=(analyzer, files, jd), daemon=True)
        self.worker.start()

    def _run(self, analyzer: ResumeAnalyzer, files: list[Path], jd: str) -> None:
        def process(path: Path) -> CandidateReport:
            if self.cancel_event.is_set():
                return CandidateReport(filename=path.name, error="Cancelled before processing.")
            try:
                doc = extract_resume(path)
            except ExtractionError as exc:
                return CandidateReport(filename=path.name, error=str(exc))
            except Exception as exc:
                return CandidateReport(filename=path.name, error=f"Could not read file: {exc}")
            return analyzer.analyze(doc, jd)

        fatal = None
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(process, f): f for f in files}
            for fut in as_completed(futures):
                try:
                    report = fut.result()
                except Exception as exc:  # defensive: process() should not raise
                    report = CandidateReport(filename=futures[fut].name, error=f"Unexpected error: {exc}")
                self.events.put(("result", report))
                # An invalid key fails every request; stop instead of burning through the batch.
                if report.error and report.error.startswith("Invalid API key"):
                    fatal = report.error
                    self.cancel_event.set()
        self.events.put(("done", fatal))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self.status_var.set("Cancelling: waiting for in-flight requests to finish...")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                tab = self.active_tab
                if kind == "result" and tab is not None and tab.winfo_exists():
                    tab.add_report(payload)
                    self.progress.step(1)
                    self.status_var.set(f"Processed {len(tab.reports)} of {tab.total}: {payload.filename}")
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_done(self, fatal: str | None) -> None:
        tab = self.active_tab
        cancelled = self.cancel_event.is_set()
        if tab is not None and tab.winfo_exists():
            tab.running = False
            tab.finish(cancelled=cancelled)
        self.submit_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if fatal:
            self.status_var.set("Stopped: invalid API key.")
            messagebox.showerror("Authentication failed", fatal, parent=self)
        else:
            self.status_var.set("Cancelled." if cancelled else "Screening complete.")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "Screening is in progress. Quit anyway?", parent=self):
                return
            self.cancel_event.set()
        self.destroy()


def main() -> None:
    app = ResumeScreenerApp()
    app.mainloop()
