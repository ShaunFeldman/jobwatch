"""Role classification and explainable fit scoring for Jobwatch.

The watcher deliberately collects broadly.  This module is the opinionated
layer that turns that firehose into a useful daily queue without hiding why a
job ranked where it did.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime


TRACKS = {
    "quant": {
        "label": "Quant",
        "pattern": r"\b(quant(itative)?|trading|trader|systematic|alpha|strat(s|egist)?|portfolio|market making)\b",
    },
    "data_ml": {
        "label": "AI & data",
        "pattern": r"\b(machine learning|deep learning|artificial intelligence|ai engineer|ml engineer|data scientist|data engineer|analytics engineer|research scientist|applied scientist)\b",
    },
    "software": {
        "label": "Software",
        "pattern": r"\b(software|developer|backend|back-end|front.?end|full.?stack|mobile|ios|android|platform|infrastructure|systems?|site reliability|sre|devops|cloud|security engineer|embedded|firmware|database|distributed)\b",
    },
    "hardware": {
        "label": "Hardware",
        "pattern": r"\b(hardware|electrical|silicon|fpga|asic|semiconductor|robotics|mechanical|manufacturing engineer)\b",
    },
    "product": {
        "label": "Product",
        "pattern": r"\b(product manager|product management|program manager|technical program|product analyst)\b",
    },
    "design": {
        "label": "Design",
        "pattern": r"\b(design|designer|user experience|ux|user interface|ui|creative)\b",
    },
    "finance": {
        "label": "Finance",
        "pattern": r"\b(finance|financial|investment|risk|actuar|accounting|treasury|credit|banking|economist)\b",
    },
    "sales": {
        "label": "Sales & growth",
        "pattern": r"\b(sales|account executive|business development|growth|marketing|partnerships)\b",
    },
    "operations": {
        "label": "Operations",
        "pattern": r"\b(operations|strategy|business analyst|consultant|supply chain|procurement|chief of staff)\b",
    },
    "people_legal": {
        "label": "People & legal",
        "pattern": r"\b(recruit|people|human resources|\bhr\b|legal|counsel|compliance|policy)\b",
    },
}

_TRACK_RE = {key: re.compile(value["pattern"], re.I) for key, value in TRACKS.items()}

_INTERN = re.compile(r"\b(intern(ship)?|co.?op|placement|summer analyst)\b", re.I)
_NEW_GRAD = re.compile(
    r"\b(new ?grad|graduate|university|campus|early career|entry.?level|junior|associate engineer|20(26|27|28))\b",
    re.I,
)
_SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president|distinguished|\biii\b|\biv\b)\b",
    re.I,
)
_REMOTE = re.compile(r"\b(remote|anywhere|distributed)\b", re.I)
_CANADA = re.compile(
    r"\b(canada|toronto|vancouver|montreal|ottawa|waterloo|calgary|edmonton|quebec|ontario|british columbia|\bON\b|\bBC\b|\bQC\b|\bAB\b)\b",
    re.I,
)
_US = re.compile(
    r"\b(united states|u\.?s\.?a?\.?|new york|nyc|seattle|bellevue|redmond|san francisco|bay area|chicago|boston|austin|los angeles|washington,? dc|miami|denver|atlanta|palo alto|mountain view|sunnyvale|menlo park|brooklyn|jersey city|\bNY\b|\bWA\b|\bCA\b|\bIL\b|\bMA\b|\bTX\b|\bNJ\b)\b",
    re.I,
)

DEFAULT_PROFILE = {
    "name": "Shaun's search",
    "headline": "2027 SWE, quant & startup roles · U.S. first, Canada too",
    "track_weights": {
        "software": 18,
        "quant": 20,
        "data_ml": 15,
        "finance": 6,
        "hardware": 4,
        "product": 3,
        "operations": 2,
        "design": 0,
        "sales": -4,
        "people_legal": -6,
        "other": 0,
    },
    "seniority_weights": {
        "internship": 20,
        "new_grad": 16,
        "entry": 8,
        "experienced": -5,
        "senior": -30,
    },
    "location_weights": {"us": 12, "canada": 8, "remote": 9, "other": -15, "unknown": 0},
    "company_type_weights": {"startup": 7, "quant": 7, "fintech": 3},
    "year_weights": {"2027": 7, "2026": -15, "2028": -3},
    "education_penalties": {"phd": -25, "masters": -8},
    "skills": {
        "rust": 8,
        "distributed": 7,
        "database": 6,
        "backend": 5,
        "cloud": 4,
        "aws": 5,
        "python": 4,
        "c++": 5,
        "machine learning": 4,
        "systems": 4,
        "infrastructure": 4,
        "trading": 4,
        "research": 3,
    },
}


def merged_profile(config: dict | None) -> dict:
    """Merge user profile values without mutating the defaults."""
    supplied = (config or {}).get("profile") or {}
    result = dict(DEFAULT_PROFILE)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


def board_tags(config: dict | None) -> dict[str, set[str]]:
    """Infer useful company/source tags from the human sections in config."""
    tags: dict[str, set[str]] = {}
    section = ""
    for name in ((config or {}).get("boards") or {}):
        if name.startswith("_"):
            section = name
            continue
        current: set[str] = set()
        if "quant" in section:
            current.add("quant")
        if "startup" in section or "ai_and_startups" in section:
            current.add("startup")
        if "fintech" in section:
            current.add("fintech")
        if "aggregator" in section or "linkedin" in section:
            current.add("aggregator")
        if "canadian" in section:
            current.update(("canada", "finance"))
        tags[name] = current
    return tags


def role_track(title: str) -> str:
    title = title or ""
    for key, pattern in _TRACK_RE.items():
        if pattern.search(title):
            return key
    return "other"


def seniority(title: str) -> str:
    title = title or ""
    if _INTERN.search(title):
        return "internship"
    if _NEW_GRAD.search(title):
        return "new_grad"
    if _SENIOR.search(title):
        return "senior"
    if re.search(r"\b(associate|analyst i|engineer i|developer i)\b", title, re.I):
        return "entry"
    return "experienced"


def location_bucket(location: str) -> str:
    location = location or ""
    if _REMOTE.search(location):
        # Remote within a target country is still represented as remote; the
        # UI exposes the original location so visa/geography remains clear.
        return "remote"
    if _US.search(location):
        return "us"
    if _CANADA.search(location):
        return "canada"
    return "other" if location.strip() else "unknown"


def _epoch(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def freshness(posted, seen=None) -> tuple[int, str]:
    # A board being seeded today does not mean every undated role was posted
    # today. Unknown dates stay neutral rather than receiving a false boost.
    epoch = _epoch(posted)
    if not epoch:
        return 0, ""
    days = max(0.0, (time.time() - epoch) / 86400)
    points = max(0, round(8 - math.log2(days + 1) * 2))
    if days < 1:
        return points, "posted today"
    if days < 3:
        return points, "posted recently"
    return points, ""


def analyze_job(
    job: dict,
    config: dict | None = None,
    tags: set[str] | None = None,
    preferred_company: bool = False,
) -> dict:
    """Return normalized facets, a 0–100 score, and human score reasons."""
    profile = merged_profile(config)
    title = job.get("title") or ""
    location = job.get("location") or ""
    track = role_track(title)
    level = seniority(title)
    place = location_bucket(location)
    tags = set(tags or ())

    score = 25
    reasons: list[str] = []

    track_points = int(profile["track_weights"].get(track, 0))
    score += track_points
    if track_points >= 8:
        reasons.append(f"strong {TRACKS.get(track, {'label': 'general'})['label'].lower()} fit")

    level_points = int(profile["seniority_weights"].get(level, 0))
    score += level_points
    if level_points >= 8:
        reasons.append("target career stage")

    location_points = int(profile["location_weights"].get(place, 0))
    score += location_points
    if location_points >= 7:
        reasons.append({"us": "U.S. location", "canada": "Canada location", "remote": "remote-friendly"}.get(place, "target location"))

    for tag in sorted(tags):
        points = int(profile["company_type_weights"].get(tag, 0))
        score += points
        if points >= 8:
            reasons.append(f"{tag} company")

    haystack = f"{title} {job.get('description') or ''}".lower()
    skill_points = 0
    matched_skills = []
    for skill, points in profile.get("skills", {}).items():
        if skill.lower() in haystack:
            skill_points += int(points)
            matched_skills.append(skill)
    score += min(skill_points, 14)
    if matched_skills:
        reasons.append("skills: " + ", ".join(matched_skills[:3]))

    years = re.findall(r"\b20\d{2}\b", title)
    if years:
        year = years[0]
        year_points = int(profile.get("year_weights", {}).get(year, 0))
        score += year_points
        if year_points > 0:
            reasons.append(f"target {year} season")

    education = profile.get("education_penalties", {})
    if re.search(r"\b(ph\.?d|doctoral|doctorate)\b", title, re.I):
        score += int(education.get("phd", -25))
        reasons.append("PhD-focused")
    elif re.search(r"\b(master'?s|masters|graduate student)\b", title, re.I):
        score += int(education.get("masters", -8))
        reasons.append("graduate-degree focused")

    fresh_points, fresh_reason = freshness(job.get("posted"), job.get("first_seen") or job.get("last_seen"))
    score += fresh_points
    if fresh_reason:
        reasons.append(fresh_reason)

    if preferred_company:
        score += 8
        reasons.append("priority company")

    return {
        "score": max(0, min(100, score)),
        "reasons": reasons[:5],
        "track": track,
        "track_label": TRACKS.get(track, {"label": "Other"})["label"],
        "seniority": level,
        "location_bucket": place,
        "company_tags": sorted(tags),
    }


def stable_job_key(job: dict) -> str:
    board = str(job.get("board") or "unknown")
    job_id = str(job.get("job_id") or job.get("id") or "")
    return f"{board}:{job_id}"
