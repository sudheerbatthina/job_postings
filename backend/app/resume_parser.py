"""Parses an uploaded resume (PDF, DOCX, or TXT) and extracts keywords
via Claude (or a bigram fallback if the API is unavailable)."""

from __future__ import annotations
import io
import json
import logging
import re
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from . import config

if TYPE_CHECKING:
    import anthropic as _anthropic


def _extract_text(filename: str, content: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join(pg.extract_text() or "" for pg in pdf.pages)
    if name.endswith(".docx"):
        import docx
        doc = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
    return content.decode(errors="ignore")


def parse_resume(filename: str, content: bytes) -> dict:
    """Returns {"tokens": set[str], "text": str, "email": str|None, "phone": str|None}"""
    text = _extract_text(filename, content)

    toks = re.findall(r"[a-zA-Z][a-zA-Z+#.\-]{2,}", text.lower())
    tokens = {t for t in toks if t not in config.STOPWORDS}

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    phone_match = re.search(r"(\+?\d[\d\s().-]{8,}\d)", text)

    return {
        "tokens": tokens,
        "text": text,
        "email": email_match.group(0) if email_match else None,
        "phone": phone_match.group(0).strip() if phone_match else None,
    }


def _bigram_fallback(text: str) -> dict:
    words = [w for w in re.findall(r"[a-zA-Z]{3,}", text.lower()) if w not in config.STOPWORDS]
    counts: dict[str, int] = {}
    for i in range(len(words) - 1):
        bg = f"{words[i]} {words[i + 1]}"
        counts[bg] = counts.get(bg, 0) + 1
    titles = [bg for bg, _ in sorted(counts.items(), key=lambda x: -x[1])[:6]]
    return {"search_titles": titles, "skill_signals": [], "total_yoe": 0}


def extract_keywords(text: str, client: "_anthropic.Anthropic | None") -> dict:
    """Call Claude to extract job-board search titles and skill signals from resume text.
    Returns {"search_titles": [...], "skill_signals": [...]}.
    Falls back to top bigrams (search_titles only) if the API is unavailable."""
    if client is not None:
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": (
                        "From this resume, return JSON with three fields:\n"
                        "search_titles: 4-6 generic, job-board-searchable role titles this person is "
                        "qualified for (e.g. 'AI Engineer', 'Senior Machine Learning Engineer') — these "
                        "get typed into a job board search box, so they must look like real job titles, "
                        "not skill names.\n"
                        "skill_signals: 6-10 specific technical phrases that differentiate this candidate "
                        "(e.g. 'RAG', 'LangGraph', 'agentic AI', 'MCP') — used for scoring fit, not for searching.\n"
                        "total_yoe: integer total years of professional work experience (0 if unclear).\n"
                        'Return only valid JSON: {"search_titles": [...], "skill_signals": [...], "total_yoe": <int>}\n\n'
                        + text[:3000]
                    ),
                }],
            )
            return json.loads(msg.content[0].text)
        except Exception as e:
            logger.warning("extract_keywords failed: %s", e)
    return _bigram_fallback(text)
