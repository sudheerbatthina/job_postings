"""Normalize posting freshness and applicant signals from heterogeneous sources."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pandas as pd


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed = pd.to_datetime(text, errors="coerce", utc=True)
        if pd.isna(parsed):
            return None
        dt = parsed.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_date_only(value) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    text = str(value or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text))


def parse_relative_posted_at(text: str | None, reference_time: datetime | None = None) -> dict:
    reference_time = _as_utc(reference_time) or datetime.now(timezone.utc)
    raw = str(text or "").strip()
    match = re.search(r"(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\s+ago", raw, re.I)
    if not match:
        return {
            "posted_at_raw": raw or None,
            "posted_at_ts": None,
            "posted_age_minutes": None,
            "posted_precision": "unknown",
        }

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("minute", "min")):
        delta = timedelta(minutes=amount)
        precision = "minute"
    elif unit.startswith(("hour", "hr")):
        delta = timedelta(hours=amount)
        precision = "hour"
    else:
        delta = timedelta(days=amount)
        precision = "day"
    posted = reference_time - delta
    return {
        "posted_at_raw": raw,
        "posted_at_ts": posted.isoformat(),
        "posted_age_minutes": int(delta.total_seconds() // 60),
        "posted_precision": precision,
    }


def build_posted_age_label(
    posted_at_ts,
    posted_precision: str | None,
    reference_time: datetime | None = None,
) -> str | None:
    posted = _as_utc(posted_at_ts)
    if posted is None:
        return None
    reference_time = _as_utc(reference_time) or datetime.now(timezone.utc)
    minutes = max(0, int((reference_time - posted).total_seconds() // 60))
    precision = posted_precision or "unknown"

    if precision == "minute" and minutes < 60:
        unit = "min"
        value = max(1, minutes)
        return f"Posted {value} {unit} ago"
    if precision in {"minute", "hour"} and minutes < 24 * 60:
        hours = max(1, round(minutes / 60))
        unit = "hr" if hours == 1 else "hrs"
        return f"Posted {hours} {unit} ago"

    days = max(0, (reference_time.date() - posted.date()).days)
    if days <= 0:
        return "Posted today"
    if days == 1:
        return "Posted yesterday"
    return f"Posted {days} days ago"


def freshness_bucket(posted_at_ts) -> str:
    posted = _as_utc(posted_at_ts)
    if posted is None:
        return "unknown"
    minutes = max(0, int((datetime.now(timezone.utc) - posted).total_seconds() // 60))
    if minutes <= 24 * 60:
        return "24h"
    if minutes <= 72 * 60:
        return "72h"
    return "old"


def _walk_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def _find_field(raw: dict, names: set[str]):
    if not isinstance(raw, dict):
        return None
    for key, value in raw.items():
        if str(key).lower() in names:
            return value
        nested = _find_field(value, names) if isinstance(value, dict) else None
        if nested is not None:
            return nested
    return None


def extract_applicant_signal(raw_json=None, description: str | None = None, extensions: dict | None = None) -> dict:
    raw = raw_json if isinstance(raw_json, dict) else {}
    search_values = []
    if extensions:
        search_values.extend(list(_walk_values(extensions)))
    search_values.extend(list(_walk_values(raw)))
    if description:
        search_values.append(description)

    explicit = _find_field(raw, {"applicant_count", "applicants", "num_applicants"})
    if explicit is not None:
        text = str(explicit)
        less_than = re.search(r"less than\s+(\d+)\s+applicants", text, re.I)
        plus = re.search(r"(\d+)\+\s+applicants", text, re.I)
        if less_than:
            n = int(less_than.group(1))
            return {
                "applicants_count": None,
                "applicants_label": f"Less than {n} applicants",
                "applicant_precision": "range",
                "early_applicant": False,
            }
        if plus:
            n = int(plus.group(1))
            return {
                "applicants_count": n,
                "applicants_label": f"{n}+ applicants",
                "applicant_precision": "range",
                "early_applicant": False,
            }
        if re.search(r"\bbe an early applicant\b", text, re.I):
            return {
                "applicants_count": None,
                "applicants_label": "Be an early applicant",
                "applicant_precision": "label",
                "early_applicant": True,
            }
        match = re.search(r"\d+", text)
        if match:
            count = int(match.group(0))
            label = f"{count}+ applicants" if "+" in text else f"{count} applicants"
            return {
                "applicants_count": count,
                "applicants_label": label,
                "applicant_precision": "range" if "+" in text else "exact",
                "early_applicant": False,
            }

    haystack = " | ".join(str(value) for value in search_values if value is not None)
    early = re.search(r"\bbe an early applicant\b", haystack, re.I)
    less_than = re.search(r"less than\s+(\d+)\s+applicants", haystack, re.I)
    plus = re.search(r"(\d+)\+\s+applicants", haystack, re.I)
    if less_than:
        n = int(less_than.group(1))
        return {
            "applicants_count": None,
            "applicants_label": f"Less than {n} applicants",
            "applicant_precision": "range",
            "early_applicant": False,
        }
    if plus:
        n = int(plus.group(1))
        return {
            "applicants_count": n,
            "applicants_label": f"{n}+ applicants",
            "applicant_precision": "range",
            "early_applicant": False,
        }
    if early:
        return {
            "applicants_count": None,
            "applicants_label": "Be an early applicant",
            "applicant_precision": "label",
            "early_applicant": True,
        }
    return {
        "applicants_count": None,
        "applicants_label": None,
        "applicant_precision": "unknown",
        "early_applicant": False,
    }


def normalize_posted_fields(row: dict, reference_time: datetime | None = None) -> dict:
    reference_time = _as_utc(reference_time) or datetime.now(timezone.utc)
    posted_at_raw = row.get("posted_at_raw") or row.get("date_posted")
    posted_at_ts = row.get("posted_at_ts")
    precision = row.get("posted_precision")

    if not posted_at_ts and posted_at_raw:
        raw_text = str(posted_at_raw)
        if "ago" in raw_text.lower():
            parsed = parse_relative_posted_at(raw_text, reference_time)
            posted_at_ts = parsed["posted_at_ts"]
            precision = parsed["posted_precision"]
            posted_at_raw = parsed["posted_at_raw"]
        else:
            dt = _as_utc(posted_at_raw)
            if dt is not None:
                posted_at_ts = dt.isoformat()
                precision = "day" if _is_date_only(posted_at_raw) else "hour"

    posted = _as_utc(posted_at_ts)
    age_minutes = (
        max(0, int((reference_time - posted).total_seconds() // 60))
        if posted is not None
        else None
    )
    precision = precision or ("unknown" if posted is None else "hour")
    label = build_posted_age_label(posted_at_ts, precision, reference_time)
    bucket = freshness_bucket(posted_at_ts)
    return {
        "posted_at_raw": posted_at_raw if posted_at_raw is not None else None,
        "posted_at_ts": posted.isoformat() if posted is not None else None,
        "posted_age_minutes": age_minutes,
        "posted_age_label": label,
        "posted_precision": precision,
        "freshness_bucket": bucket,
    }


def is_recent_enough(job, max_age_hours: int = 72) -> bool:
    posted = _as_utc(job.get("posted_at_ts") if hasattr(job, "get") else None)
    if posted is None:
        return True
    return datetime.now(timezone.utc) - posted <= timedelta(hours=max_age_hours)
