"""Parses an uploaded resume (PDF, DOCX, or TXT) into:
  - a token set, used for resume<->job overlap scoring (today's feature)
  - a few structured fields (email, phone, raw text), kept for the future
    autofill tool so it can reuse this parser instead of writing a new one.
Nothing here is persisted — call sites use the result and discard it.
"""

from __future__ import annotations
import io
import re

from . import config


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
    # fall back to plain text
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
