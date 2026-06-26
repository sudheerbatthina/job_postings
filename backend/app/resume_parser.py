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
    "ai/ml engineer": "AI/ML Engineer",
    "ml engineer": "ML Engineer",
    "machine learning engineer": "Machine Learning Engineer",
    "genai engineer": "GenAI Engineer",
    "generative ai engineer": "GenAI Engineer",
    "applied ai engineer": "Applied AI Engineer",
    "llm engineer": "LLM Engineer",
    "agentic ai engineer": "Agentic AI Engineer",
    "rag engineer": "RAG Engineer",
    "ai platform engineer": "AI Platform Engineer",
    "mlops engineer": "MLOps Engineer",
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


def _optional_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _message_text(message) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if isinstance(block, dict):
            text = block.get("text")
        else:
            text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _first_json_object(raw_text: str) -> dict | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _log_parse_failure(reason: str, raw_text: str, message=None) -> None:
    content = getattr(message, "content", None)
    block_count = len(content) if isinstance(content, list) else None
    logger.warning(
        "extract_keywords parse failed: reason=%s response_len=%s content_blocks=%s",
        reason,
        len(raw_text or ""),
        block_count,
    )


def parse_resume_analysis_response(raw_text: str, message=None) -> dict | None:
    data = _first_json_object(raw_text)
    if data is None:
        _log_parse_failure("no_valid_json_object", raw_text, message)
        return None
    if not any(key in data for key in ("search_titles", "skill_signals", "total_yoe")):
        _log_parse_failure("missing_expected_fields", raw_text, message)
        return None
    return data


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
    target_profile = normalize_target_profile(data.get("target_profile"), search_titles)
    return {
        "search_titles": search_titles,
        "skill_signals": _dedupe(_as_string_list(data.get("skill_signals"))),
        "total_yoe": _optional_int(data.get("total_yoe")),
        "target_profile": target_profile,
    }


def normalize_target_profile(raw_profile, search_titles: list[str] | None = None) -> dict:
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    primary_track = str(profile.get("primary_track") or "applied_ai_ml").strip() or "applied_ai_ml"
    if primary_track != "applied_ai_ml":
        primary_track = "applied_ai_ml"

    target_titles = sanitize_search_titles(profile.get("target_titles"))
    if search_titles:
        target_titles = _dedupe(target_titles + [
            title for title in search_titles
            if title in config.APPLIED_AI_TARGET_TITLES or title in config.DEFAULT_SEARCH_TITLES
        ])
    target_titles = _dedupe(target_titles + config.APPLIED_AI_TARGET_TITLES)

    must_have = _dedupe(
        _as_string_list(profile.get("must_have_signals")) + config.APPLIED_AI_MUST_HAVE_SIGNALS
    )
    secondary = _dedupe(
        _as_string_list(profile.get("secondary_signals")) + config.APPLIED_AI_SECONDARY_SIGNALS
    )
    return {
        "primary_track": primary_track,
        "target_titles": target_titles,
        "must_have_signals": must_have,
        "secondary_signals": secondary,
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


def _estimate_total_yoe(text: str) -> int | None:
    patterns = [
        r"(\d{1,2})\+?\s*(?:years|yrs)\s+(?:of\s+)?(?:professional\s+)?experience",
        r"(?:experience|experienced).{0,40}?(\d{1,2})\+?\s*(?:years|yrs)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            years = _optional_int(match.group(1))
            if years is not None and years <= 60:
                return years
    return None


def _fallback_skill_signals(text: str) -> list[str]:
    lowered = (text or "").lower()
    found = [
        skill for skill in config.RESUME_SKILL_KEYWORDS
        if re.search(rf"\b{re.escape(skill.lower())}\b", lowered)
    ]
    return _dedupe(found) or config.DEFAULT_SKILL_SIGNALS.copy()


def fallback_resume_analysis(text: str) -> dict:
    return {
        "search_titles": config.DEFAULT_SEARCH_TITLES.copy(),
        "skill_signals": _fallback_skill_signals(text),
        "total_yoe": _estimate_total_yoe(text),
        "target_profile": config.DEFAULT_TARGET_PROFILE.copy(),
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
                        "Return JSON only. Do not wrap it in markdown. Do not include commentary.\n"
                        'Use exactly this shape: {"search_titles": [], "skill_signals": [], '
                        '"total_yoe": null, "target_profile": {"primary_track": "applied_ai_ml", '
                        '"target_titles": [], "must_have_signals": [], "secondary_signals": []}}\n'
                        "search_titles must be real job titles to search job boards with. "
                        "Never put tools, libraries, frameworks, vendors, concepts, or skill phrases "
                        "in search_titles. Bad search_titles include MCP tool, semantic reranking, "
                        "LangGraph OpenAI, OpenAI agents, and agents SDK.\n"
                        "skill_signals must be tools, skills, platforms, or technical concepts only. "
                        "total_yoe must be an integer estimate of professional years of experience, "
                        "or null if unclear. target_profile must describe the candidate's intended "
                        "role family. For Applied AI, AI/ML, LLM, GenAI, Agentic AI, RAG, or MLOps "
                        "engineering resumes, use primary_track applied_ai_ml and target_titles such "
                        "as Applied AI Engineer, AI Engineer, AI/ML Engineer, Machine Learning "
                        "Engineer, ML Engineer, GenAI Engineer, LLM Engineer, Agentic AI Engineer, "
                        "RAG Engineer, AI Platform Engineer, MLOps Engineer, and Applied Scientist. "
                        "Do not make generic Data Engineer a primary target unless the resume is "
                        "clearly targeting AI/ML data platform work.\n\n"
                        "Resume:\n"
                        + text[:3000]
                    ),
                }],
            )
            raw_text = _message_text(msg)
            data = parse_resume_analysis_response(raw_text, msg)
            if data is not None:
                return normalize_resume_analysis(data)
        except Exception as e:
            logger.warning("extract_keywords failed: %s", e)
    return normalize_resume_analysis(fallback_resume_analysis(text))
