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


_ROLE_WORDS = {
    "architect", "consultant", "developer", "engineer", "lead", "manager",
    "researcher", "scientist", "specialist", "analyst",
}

_SEARCH_TITLE_ALIASES = {
    "ai engineer": "AI Engineer",
    "ml engineer": "Machine Learning Engineer",
    "machine learning engineer": "Machine Learning Engineer",
    "genai engineer": "GenAI Engineer",
    "generative ai engineer": "GenAI Engineer",
    "applied ai engineer": "Applied AI Engineer",
    "llm engineer": "LLM Engineer",
    "data scientist": "Data Scientist",
    "applied scientist": "Applied Scientist",
    "ml platform engineer": "ML Platform Engineer",
}

_SKILL_ONLY_TITLE_TERMS = {
    "agent", "agents", "airflow", "api", "apis", "dbt", "docker", "framework",
    "frameworks", "kubernetes", "langchain", "langgraph", "library", "mcp",
    "openai", "pipeline", "pipelines", "python", "rag", "reranking", "sdk",
    "semantic", "snowflake", "tool", "tools", "vector",
}


def _as_string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_valid_search_title(title: str) -> bool:
    """Return True only for role names suitable for job-board search boxes."""
    normalized = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not normalized:
        return False
    if normalized in _SEARCH_TITLE_ALIASES:
        return True

    words = re.findall(r"[a-z0-9]+", normalized)
    if len(words) < 2 or len(words) > 6:
        return False
    if not any(word in _ROLE_WORDS for word in words):
        return False

    return not any(word in _SKILL_ONLY_TITLE_TERMS for word in words)


def sanitize_search_titles(raw_titles) -> list[str]:
    titles: list[str] = []
    for raw_title in _as_string_list(raw_titles):
        normalized = re.sub(r"\s+", " ", raw_title.strip())
        alias = _SEARCH_TITLE_ALIASES.get(normalized.lower())
        candidate = alias or normalized
        if is_valid_search_title(candidate):
            titles.append(candidate)
    return _dedupe(titles)


def normalize_resume_analysis(data: dict | None) -> dict:
    data = data if isinstance(data, dict) else {}
    search_titles = _dedupe(
        sanitize_search_titles(data.get("search_titles")) + config.DEFAULT_SEARCH_TITLES
    )
    return {
        "search_titles": search_titles,
        "skill_signals": _dedupe(_as_string_list(data.get("skill_signals"))),
        "total_yoe": _safe_int(data.get("total_yoe")),
    }


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
    skill_signals = [bg for bg, _ in sorted(counts.items(), key=lambda x: -x[1])[:10]]
    return {
        "search_titles": config.DEFAULT_SEARCH_TITLES.copy(),
        "skill_signals": skill_signals,
        "total_yoe": 0,
    }


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
                        "Return ONLY valid JSON with exactly these fields: "
                        "search_titles, skill_signals, total_yoe.\n"
                        "search_titles must be generic job titles to search job boards with. "
                        "Never put tools, libraries, frameworks, vendors, concepts, or skill "
                        "phrases in search_titles. Bad search_titles include 'MCP tool', "
                        "'LangGraph OpenAI', 'OpenAI agents', 'agents SDK', and "
                        "'semantic reranking'.\n"
                        "skill_signals must contain technical skills/tools/concepts used only "
                        "for scoring relevance, such as LangGraph, OpenAI, RAG, semantic "
                        "search, vector databases, Python, Snowflake, or dbt.\n"
                        "total_yoe must be an integer estimate of total professional years of "
                        "experience, or 0 if unclear.\n\n"
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
            return normalize_resume_analysis(json.loads(msg.content[0].text))
        except Exception as e:
            logger.warning("extract_keywords failed: %s", e)
    return normalize_resume_analysis(_bigram_fallback(text))
