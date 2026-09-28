"""Claude-backed resume analysis."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import List

import anthropic
from pydantic import BaseModel, Field

from .extractors import ResumeDocument

DEFAULT_MODEL = "claude-opus-5"
AVAILABLE_MODELS = ("claude-opus-5", "claude-sonnet-5")
SELECTION_THRESHOLD = 85

# Server-side refusal fallback: supported for claude-opus-5 on the Claude API.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5"}

SYSTEM_PROMPT = """You are a senior HR manager and talent acquisition specialist screening resumes against a job description.

Assess each resume strictly on job-relevant evidence: skills, qualifications, certifications, relevant experience (depth, recency, seniority, industry), measurable achievements, and tools or technologies.

Fairness rules:
- Ignore and never reason about protected or non-job-relevant attributes: name, age or date of birth, gender, ethnicity, nationality, religion, marital or family status, disability, photo, or address.
- Do not penalise employment gaps unless the job description makes continuous recent experience an explicit requirement; note them neutrally.
- Treat equivalent skills and synonymous technologies as matches where a competent hiring manager would (for example, Postgres and PostgreSQL). Do not award credit for skills merely implied without evidence.

Scoring (match_percentage, integer 0-100):
- Weight mandatory or must-have requirements most heavily, then years and relevance of experience, then preferred or nice-to-have items.
- A candidate missing any must-have requirement should normally score below 85.
- 85 or higher means the candidate should progress to interview. Be calibrated; do not inflate scores.

Be specific and evidence-based. Reference concrete items from the resume. Keep each list item short (one line)."""


class ScreeningResult(BaseModel):
    candidate_name: str = Field(description="Candidate name as written on the resume, or 'Unknown'")
    match_percentage: int = Field(description="Overall fit against the job description, 0-100")
    matched_skills: List[str] = Field(description="JD skills/requirements evidenced in the resume")
    missing_skills: List[str] = Field(description="JD skills/requirements absent or not evidenced")
    relevant_experience_years: float = Field(description="Estimated years of experience relevant to this role")
    experience_fit: str = Field(description="2-4 sentences on how the experience aligns with the role's level and scope")
    strengths: List[str]
    gaps: List[str] = Field(description="Weaknesses, risks or areas to probe at interview")
    interview_questions: List[str] = Field(description="3-5 targeted questions to validate gaps or claims")
    summary: str = Field(description="Concise hiring summary, 3-5 sentences")


@dataclass
class CandidateReport:
    filename: str
    result: ScreeningResult | None = None
    error: str | None = None
    model_used: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result is not None

    @property
    def match(self) -> int:
        if self.result is None:
            return -1
        return max(0, min(100, int(self.result.match_percentage)))

    @property
    def status(self) -> str:
        if self.result is None:
            return "Error"
        return "Selected" if self.match >= SELECTION_THRESHOLD else "Rejected"

    @property
    def candidate_name(self) -> str:
        if self.result and self.result.candidate_name.strip():
            return self.result.candidate_name.strip()
        return "Unknown"


def describe_api_error(exc: Exception) -> str:
    """Map SDK exceptions to messages an HR user can act on."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Invalid API key. Check the key and try again."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The API key does not have permission to use this model."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found or not available to this API key."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limit reached after retries. Wait a minute and resubmit the failed resumes."
    if isinstance(exc, anthropic.BadRequestError):
        return f"Request rejected by the API: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"Claude API server error ({exc.status_code}). Try again later."
        return f"API error ({exc.status_code}): {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Network error. Check the internet connection or proxy settings."
    return f"{type(exc).__name__}: {exc}"


class ResumeAnalyzer:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, client: anthropic.Anthropic | None = None):
        if not api_key and client is None:
            raise ValueError("API key is required")
        self.model = model
        # SDK retries 408/409/429/5xx and connection errors with backoff.
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=300.0)

    def _system_blocks(self, job_description: str) -> list[dict]:
        # The JD is identical for every resume in a batch, so it sits in the
        # cached system prefix and only the resume varies per request.
        return [
            {"type": "text", "text": SYSTEM_PROMPT},
            {
                "type": "text",
                "text": f"<job_description>\n{job_description.strip()}\n</job_description>",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    @staticmethod
    def _user_content(resume: ResumeDocument) -> list[dict]:
        instruction = (
            "Screen this resume against the job description and return the structured assessment."
        )
        if resume.use_native_pdf:
            return [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(resume.pdf_bytes).decode("ascii"),
                    },
                    "title": resume.filename,
                },
                {"type": "text", "text": instruction},
            ]
        return [
            {
                "type": "text",
                "text": f"<resume filename=\"{resume.filename}\">\n{resume.text}\n</resume>\n\n{instruction}",
            }
        ]

    def analyze(self, resume: ResumeDocument, job_description: str) -> CandidateReport:
        report = CandidateReport(filename=resume.filename)
        if resume.use_native_pdf:
            report.notes.append("Little or no text layer found; PDF sent to Claude as a document.")

        kwargs: dict = dict(
            model=self.model,
            max_tokens=16000,
            system=self._system_blocks(job_description),
            messages=[{"role": "user", "content": self._user_content(resume)}],
            output_format=ScreeningResult,
        )
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            response = self.client.beta.messages.parse(**kwargs)
        except Exception as exc:  # mapped to a readable message below
            report.error = describe_api_error(exc)
            return report

        report.model_used = getattr(response, "model", self.model)
        if response.stop_reason == "refusal":
            report.error = "Claude declined to assess this resume (refusal)."
            return report
        if response.stop_reason == "max_tokens":
            report.error = "Response was cut off (max_tokens). Try again or shorten the job description."
            return report
        if response.parsed_output is None:
            report.error = "Claude returned an unreadable response."
            return report

        report.result = response.parsed_output
        return report
