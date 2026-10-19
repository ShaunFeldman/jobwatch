#!/usr/bin/env python3
"""
jobwatch v5 — broad-market watcher and local career command center.

Sources: Greenhouse / Lever / Ashby / SmartRecruiters / Workday / Eightfold
boards, amazon.jobs, Microsoft careers, LinkedIn guest search, community
GitHub listing repos (SimplifyJobs / vanshb03 / cvrve JSON + markdown-table
repos like speedyapply). Cross-source dedupe so the same job never pings
twice. Discord delivery uses embeds grouped by company (no link-preview spam).

    pip install requests
    python watcher.py --check            # validate config + regexes
    python watcher.py --verify           # hit every board once, report ok/404
    python watcher.py --list stripe      # dump one board (token debug)
    python watcher.py --test shaun       # send test message to a subscriber
    python watcher.py --serve            # open the local dashboard
    python watcher.py --once             # one cycle (GitHub Actions mode; exports state.json)
    python watcher.py                    # loop forever (VPS/systemd mode)

Files: config.json, subscribers.json, jobwatch.db (created), state.json
(portable state for Actions mode; auto-imported if DB is missing).
Env: TELEGRAM_BOT_TOKEN, HEALTHCHECK_URL (optional).
"""

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from email.utils import parsedate_to_datetime
import threading

import requests

HERE = Path(__file__).parent
DB_PATH = HERE / "jobwatch.db"
CONFIG = HERE / "config.json"
SUBSCRIBERS = HERE / "subscribers.json"
STATE_JSON = HERE / "state.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
TIMEOUT = 25
PRUNE_AFTER_DAYS = 90   # dedupe memory: a job re-posted after this alerts again
RECENT_DAYS = 14        # compact digest history; full jobs live for 90 days
ZERO_GUARD_MIN = 5
SEND_RETRIES = 3


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"{now()} {msg}", flush=True)


# ---- posting freshness: adapters set job["posted"] = epoch seconds ----
def _iso_epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Amazon publishes calendar dates rather than ISO timestamps.
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                parsed = datetime.strptime(str(value), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _posted_epoch(value):
    """Normalize dates without letting invalid or millisecond values look new."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        epoch = _iso_epoch(value)
    if epoch is None or not math.isfinite(epoch) or epoch <= 0:
        return None
    return epoch / 1000 if epoch > 100_000_000_000 else epoch


def _workday_posted_epoch(text):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' -> epoch."""
    if not text:
        return None
    t = text.lower()
    if "today" in t:
        return time.time()
    if "yesterday" in t:
        return time.time() - 86400
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return time.time() - int(m.group(1)) * 86400
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return time.time() - int(m.group(1)) * 30 * 86400
    return None


def _rel_age(posted):
    """Compact freshness badge; 🔥 for <24h, plain for up to 21d, else ''."""
    if not posted:
        return ""
    epoch = _posted_epoch(posted)
    if epoch is None:
        return ""
    age = time.time() - epoch
    if age < 0:
        return ""
    hours = age / 3600
    if hours < 2:
        return "🔥 just posted"
    if hours < 24:
        return f"🔥 {int(hours)}h ago"
    days = int(hours // 24)
    if days <= 21:
        return f"{days}d ago"
    return ""


# ================================================================ adapters
# Each returns list of {id, title, location, url, company}. id stable per board.

class FetchResult(list):
    """Usable postings plus explicit evidence of incomplete coverage."""
    def __init__(self, rows=(), warnings=()):
        super().__init__(rows)
        self.warnings = list(warnings)


def _retry_delay(response, default=2):
    value = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default


def _source_request(method, url, **kwargs):
    for attempt in range(3):
        try:
            response = getattr(requests, method)(url, **kwargs)
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            delay = _retry_delay(exc.response, 2 ** attempt)
            # Long cooldowns belong to the scheduler, not a sleeping worker.
            if attempt == 2 or (status != 429 and status < 500) or delay > 10:
                raise
            time.sleep(delay)


def _pages(fetch, identity, cfg):
    """Follow all pages; keep recovered rows and flag any incomplete read."""
    out = FetchResult()
    seen, offset, total = set(), 0, None
    deadline = time.monotonic() + cfg.get("fetch_budget_seconds", 180)
    for _ in range(cfg.get("max_pages", 1000)):
        try:
            rows, reported = fetch(offset)
            if not isinstance(rows, list):
                raise ValueError("expected a job list")
            if total is None and reported is not None:
                total = int(reported)
            if not rows:
                if total is not None and offset < total:
                    out.warnings.append(f"empty page at {offset} of {total} postings")
                return out
            added = invalid = 0
            for row in rows:
                key = identity(row) if isinstance(row, dict) else None
                if not key:
                    invalid += 1
                    out.warnings.append(f"posting without a stable ID at offset {offset}")
                    continue
                if key not in seen:
                    seen.add(key)
                    out.append(row)
                    added += 1
            offset += len(rows)
            if not added and not invalid:
                out.warnings.append(f"pagination repeated at offset {offset}")
                return out
            if total is not None and offset >= total:
                return out
            if time.monotonic() >= deadline:
                out.warnings.append(f"fetch time budget reached after {offset} postings")
                return out
        except Exception as exc:
            if not out:
                raise
            out.warnings.append(f"page at offset {offset}: {type(exc).__name__}: {exc}")
            return out
    out.warnings.append(f"page safety limit reached after {offset} postings")
    return out


def _mapped(rows, convert):
    return FetchResult((convert(row) for row in rows), getattr(rows, "warnings", ()))


def fetch_greenhouse(cfg):
    url = f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs?content=false"
    r = _source_request("get", url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "posted": _iso_epoch(j.get("first_published")),
    } for j in r.json()["jobs"]]


def fetch_lever(cfg):
    url = f"https://api.lever.co/v0/postings/{cfg['token']}?mode=json"
    r = _source_request("get", url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("text", ""),
        "location": (j.get("categories") or {}).get("location", "") or "",
        "url": j.get("hostedUrl", ""),
        "posted": (j.get("createdAt") or 0) / 1000 or None,
    } for j in r.json()]


def fetch_ashby(cfg):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{cfg['token']}?includeCompensation=true"
    r = _source_request("get", url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j.get("id")),
        "title": j.get("title", ""),
        "location": " | ".join(dict.fromkeys(filter(None, [j.get("location")]
                    + [loc.get("location") for loc in j.get("secondaryLocations", [])]
                    + (["Remote"] if j.get("isRemote") else [])))),
        "url": j.get("jobUrl", ""),
        "salary": (j.get("compensation") or {}).get("compensationTierSummary", ""),
        "posted": _iso_epoch(j.get("publishedAt")),
    } for j in r.json()["jobs"]]


def fetch_smartrecruiters(cfg):
    token = cfg["token"]
    def page(offset):
        r = _source_request("get", f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                            params={"limit": 100, "offset": offset},
                            headers={"User-Agent": UA}, timeout=TIMEOUT)
        data = r.json()
        return data["content"], data.get("totalFound")
    rows = _pages(page, lambda j: j.get("id"), cfg)
    return _mapped(rows, lambda j: {
        "id": str(j["id"]), "title": j.get("name", ""),
        "location": ", ".join(x for x in [(j.get("location") or {}).get("city"),
                                          (j.get("location") or {}).get("country")] if x),
        "url": f"https://jobs.smartrecruiters.com/{token}/{j['id']}",
        "posted": _iso_epoch(j.get("releasedDate")),
    })


def fetch_workday(cfg):
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    def page(offset):
        r = _source_request("post", f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                            json={"appliedFacets": cfg.get("facets", {}), "limit": 20,
                                  "offset": offset, "searchText": cfg.get("search", "")},
                            headers={"User-Agent": UA, "Accept": "application/json"},
                            timeout=TIMEOUT)
        data = r.json()
        return data["jobPostings"], data.get("total")
    rows = _pages(page, lambda j: j.get("externalPath"), cfg)
    return _mapped(rows, lambda j: {
        "id": j["externalPath"], "title": j.get("title", ""),
        "location": j.get("locationsText", "") or "",
        "url": f"https://{host}/en-US/{site}{j['externalPath']}",
        "posted": _workday_posted_epoch(j.get("postedOn", "")),
    })


def fetch_amazon_jobs(cfg):
    def page(offset):
        params = {"base_query": cfg.get("query", "software engineer intern"),
                  "result_limit": 100, "offset": offset, "sort": "recent"}
        if cfg.get("country"):
            params["country"] = cfg["country"]
        data = _source_request("get", "https://www.amazon.jobs/en/search.json", params=params,
                               headers={"User-Agent": UA}, timeout=TIMEOUT).json()
        if data.get("error"):
            raise ValueError(str(data["error"]))
        return data["jobs"], data.get("hits")
    rows = _pages(page, lambda j: j.get("id_icims") or j.get("id"), cfg)
    return _mapped(rows, lambda j: {
        "id": str(j.get("id_icims") or j["id"]), "title": j.get("title", ""),
        "location": j.get("normalized_location") or j.get("location", ""),
        "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
        "posted": _iso_epoch(j.get("posted_date")),
    })


def fetch_microsoft(cfg):
    def page(offset):
        data = _source_request("get", "https://gcsservices.careers.microsoft.com/search/api/v1/search",
            params={"q": cfg.get("query", "software engineer"), "l": "en_us",
                    "pg": offset // 100 + 1, "pgSz": 100, "o": "Relevance", "flt": "true"},
            headers={"User-Agent": UA}, timeout=TIMEOUT).json()["operationResult"]["result"]
        return data["jobs"], data.get("totalJobs")
    rows = _pages(page, lambda j: j.get("jobId"), cfg)
    return _mapped(rows, lambda j: {
        "id": str(j["jobId"]), "title": j.get("title", ""),
        "location": "; ".join((j.get("properties") or {}).get("locations") or []),
        "url": f"https://jobs.careers.microsoft.com/global/en/job/{j['jobId']}",
    })


def fetch_github_listings(cfg):
    """Community-maintained listing repos (SimplifyJobs and forks).
    cfg: repo ('SimplifyJobs/New-Grad-Positions'), branch (default 'dev'),
    path (default '.github/scripts/listings.json').
    These aggregate hundreds of companies incl. bespoke boards (Jane Street,
    Google, Meta, Apple) — the machine-readable version of the IG job accounts."""
    repo = cfg["repo"]
    branch = cfg.get("branch", "dev")
    path = cfg.get("path", ".github/scripts/listings.json")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    r = _source_request("get", url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not j.get("active") or not j.get("is_visible", True):
            continue
        locs = j.get("locations") or []
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": " | ".join(locs),
            "url": j.get("url", ""),
            "company": j.get("company_name", ""),
            "posted": _posted_epoch(j.get("date_posted")),
        })
    return out


_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HREF = re.compile(r'href="(https?://[^"]+)"')
_RAW_URL = re.compile(r"https?://[^)\s\"'<>\]]+")
_HTML_TAG = re.compile(r"<[^>]+>")
_DECOR = re.compile(r"[✓✅🆕🔥⭐🎯🛂🔒🇺🇸🇨🇦↳*_`]")


def _cell_text(cell):
    s = _MD_IMG.sub("", cell)
    m = _MD_LINK.search(s)
    if m and m.group(1).strip():
        s = m.group(1)
    else:
        s = _HTML_TAG.sub("", s)
    s = _DECOR.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -—")


def _cell_url(cell):
    s = _MD_IMG.sub("", cell)       # shields.io badges hide the real link
    m = _HREF.search(s)
    if m:
        return m.group(1)
    m = _RAW_URL.search(s)
    return m.group(0).rstrip(".,;") if m else ""


def fetch_marqeta(cfg):
    """Marqeta's official careers page exposes its live openings as a
    semantic HTML table.  Their former Greenhouse board is no longer public,
    so parsing that first-party table is more reliable than a stale token."""
    base = cfg.get("url", "https://www.marqeta.com")
    path = cfg.get("path", "/company/careers")
    r = _source_request("get", base.rstrip("/") + path, headers={"User-Agent": UA},
                     timeout=TIMEOUT)
    r.raise_for_status()
    out, seen = [], set()
    pattern = re.compile(
        r'<a[^>]+href="(/careers/([0-9a-f-]{20,}))"[^>]*>(.*?)</a>', re.I | re.S)
    for href, job_id, body in pattern.findall(r.text):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", body, re.I | re.S)
        if len(cells) < 3 or job_id in seen:
            continue
        seen.add(job_id)
        out.append({
            "id": job_id,
            "title": _cell_text(cells[0]),
            "location": _cell_text(cells[2]),
            "url": base.rstrip("/") + href,
            "company": "Marqeta",
        })
    if not out:
        raise ValueError("official Marqeta careers page contained no job rows")
    return out


def fetch_hightouch(cfg):
    """The official site embeds its complete live listing in Next.js page data."""
    text = _source_request("get", "https://hightouch.com/careers",
                           headers={"User-Agent": UA}, timeout=TIMEOUT).text
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not match:
        raise ValueError("Hightouch careers page data missing")
    rows = json.loads(match.group(1))["props"]["pageProps"]["jobPostings"]
    return [{"id": j["id"], "title": j["title"], "location": j.get("locationName", ""),
             "company": "Hightouch", "url": f"https://hightouch.com/careers/{j['id']}"}
            for j in rows]


def fetch_github_md(cfg):
    """Read named table columns, tolerating reordered columns and multiple roles."""
    url = (f"https://raw.githubusercontent.com/{cfg['repo']}/"
           f"{cfg.get('branch', 'main')}/{cfg.get('path', 'README.md')}")
    text = _source_request("get", url, headers={"User-Agent": UA}, timeout=60).text
    cols = dict(cfg["cols"])
    aliases = {"company": {"company", "name"}, "title": {"role", "position", "job title", "title"},
               "location": {"location", "locations"}, "url": {"apply", "application", "link", "links", "application/link", "posting"},
               "salary": {"salary", "pay", "salary ($)"}}
    out, seen, prev_company = [], set(), ""
    for row in text.splitlines():
        if row.startswith("#"):
            prev_company = ""
        if not row.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]
        labels = [_cell_text(c).lower() for c in cells]
        detected = {key: i for key, names in aliases.items() for i, label in enumerate(labels) if label in names}
        if "company" in detected and "title" in detected:
            cols = detected
            cols.setdefault("url", cols["title"])
            prev_company = ""
            continue
        if len(cells) < max(cols.values()) + 1:
            continue
        title = _cell_text(cells[cols["title"]])
        if not title or set(title) <= set("-: "):
            continue
        company = _cell_text(cells[cols["company"]]) or prev_company
        prev_company = company
        if not company or "🔒" in cells[cols["url"]] or "~~" in cells[cols["title"]]:
            continue
        cell = cells[cols["url"]]
        urls = [html.unescape(m.group(1)) for m in re.finditer(r'\]\((https?://[^)\s]+)\)', _MD_IMG.sub("", cell))]
        urls += [html.unescape(u) for u in _HREF.findall(cell)]
        if not urls:
            fallback = _cell_url(cell) or _cell_url(cells[cols["title"]])
            urls = [html.unescape(fallback)] if fallback else []
        for joburl in dict.fromkeys(urls):
            if any(host in urlsplit(joburl).netloc for host in ("shields.io", "githubusercontent.com")):
                continue
            jid = hashlib.md5(joburl.encode()).hexdigest()[:16]
            if jid in seen:
                continue
            seen.add(jid)
            job = {"id": jid, "title": title, "company": company, "url": joburl,
                   "location": _cell_text(cells[cols["location"]]) if "location" in cols else ""}
            if "salary" in cols:
                job["salary"] = _cell_text(cells[cols["salary"]])
            out.append(job)
    if not out and not cfg.get("allow_empty", False):
        raise ValueError("no job rows parsed; listing table may have changed")
    return out


def fetch_linkedin(cfg):
    """LinkedIn public guest search — same postings as linkedin.com/jobs, no
    login. cfg: queries (list) or query, location ('United States'), recency
    ('r604800' = posted in last week), pages (default 10).
    Query failures and page limits are exposed as partial coverage."""
    cfg = {**cfg, "_deadline": time.monotonic() + cfg.get("fetch_budget_seconds", 180)}
    queries = cfg.get("queries") or [cfg.get("query", "software engineer intern")]
    location = cfg.get("location", "United States")
    out, seen, errors = FetchResult(), set(), []
    for q in queries:
        if time.monotonic() >= cfg["_deadline"]:
            out.warnings.append("LinkedIn fetch time budget reached; remaining queries deferred")
            break
        try:
            _linkedin_query(cfg, q, location, out, seen)
        except Exception as e:      # one rate-limited query shouldn't kill the rest
            errors.append(e)
    if not out and errors:
        raise errors[0]
    return FetchResult(out, out.warnings + [f"query failed: {type(e).__name__}: {e}" for e in errors])


def _linkedin_query(cfg, q, location, out, seen):
    start = 0
    for _ in range(cfg.get("pages", 10)):
        if time.monotonic() >= cfg.get("_deadline", float("inf")):
            raise requests.Timeout("LinkedIn fetch time budget reached")
        try:
            r = _source_request("get",
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params={"keywords": q, "location": location,
                        "f_TPR": cfg.get("recency", "r604800"), "start": start, "sortBy": "DD"},
                headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                timeout=TIMEOUT)
        except requests.HTTPError as exc:
            if start and exc.response is not None and exc.response.status_code in (400, 404):
                break
            raise
        got = 0
        for card in r.text.split("<li")[1:]:
            m = re.search(r"urn:li:jobPosting:(\d+)", card)
            mt = re.search(r"base-search-card__title[^>]*>\s*([^<]+)", card)
            if not m or not mt:
                continue
            got += 1
            jid = m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            mc = re.search(r"base-search-card__subtitle[^>]*>\s*<a[^>]*>\s*([^<]+)", card)
            ml = re.search(r"job-search-card__location[^>]*>\s*([^<]+)", card)
            md = re.search(r'datetime="([^"]+)"', card)
            out.append({
                "id": jid,
                "title": html.unescape(mt.group(1)).strip(),
                "location": html.unescape(ml.group(1)).strip() if ml else "",
                "url": f"https://www.linkedin.com/jobs/view/{jid}",
                "company": html.unescape(mc.group(1)).strip() if mc else "",
                "posted": _iso_epoch(md.group(1)) if md else None,
            })
        if not got:
            if "<html" in r.text.lower() or "urn:li:jobPosting:" in r.text:
                raise ValueError("LinkedIn returned an unexpected page instead of job cards")
            break
        start += got
        time.sleep(1.0)                         # be polite, one IP
    else:
        out.warnings.append(f"LinkedIn query {q!r} reached its page limit")


def fetch_janestreet(cfg):
    """Jane Street's own positions feed — no ATS, plain JSON. The
    availability field ('Full-Time: New Grad', 'Internship', ...) is folded
    into the title so intern/new-grad categorization and filters see it."""
    r = _source_request("get", "https://www.janestreet.com/jobs/main.json",
                     headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    cities = {"NYC": "New York, NY", "LDN": "London, UK", "HKG": "Hong Kong",
              "SGP": "Singapore", "AMS": "Amsterdam", "CHI": "Chicago, IL"}
    out = []
    for p in r.json():
        avail = (p.get("availability") or "").strip()
        title = p.get("position", "")
        if avail and avail.lower() not in title.lower():
            title = f"{title} — {avail}"
        sal = ""
        if p.get("min_salary") and p.get("max_salary"):
            try:
                lo = int(str(p["min_salary"]).replace(",", "").split(".")[0])
                hi = int(str(p["max_salary"]).replace(",", "").split(".")[0])
                sal = f"${lo:,} – ${hi:,}"
            except ValueError:
                sal = f"${p['min_salary']} – ${p['max_salary']}"
        out.append({
            "id": str(p.get("id")),
            "title": title,
            "location": cities.get(p.get("city"), p.get("city") or ""),
            "url": f"https://www.janestreet.com/join-jane-street/position/{p.get('id')}/",
            "company": "Jane Street",
            "salary": sal,
        })
    return out


def fetch_phenom(cfg):
    def page(offset):
        body = {"lang": "en_us", "deviceType": "desktop", "country": "us",
                "pageName": "search-results", "ddoKey": "refineSearch", "from": offset,
                "jobs": True, "counts": True, "size": cfg.get("size", 100),
                "clearAll": False, "jdsource": "facets", "siteType": "external",
                "keywords": cfg.get("query", ""), "global": True,
                "selected_fields": {}, "all_fields": ["category", "location"]}
        data = _source_request("post", f"https://{cfg['host']}/widgets", json=body,
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=TIMEOUT).json()["refineSearch"]["data"]
        return data["jobs"], data.get("totalHits")
    rows = _pages(page, lambda j: j.get("jobId") or j.get("reqId"), cfg)
    url_tmpl = cfg.get("url", f"https://{cfg['host']}/en/job/{{id}}")
    def convert(j):
        jid = str(j.get("jobId") or j.get("reqId"))
        loc = j.get("cityStateCountry") or j.get("cityState") or j.get("location") or j.get("city") or ""
        return {"id": jid, "title": j.get("title", ""),
                "location": loc if isinstance(loc, str) else "; ".join(loc),
                "url": j.get("applyUrl") or url_tmpl.format(id=jid)}
    return _mapped(rows, convert)


def fetch_eightfold(cfg):
    def page(offset):
        params = {"domain": cfg["domain"], "start": offset, "num": 10, "sort_by": "timestamp"}
        if cfg.get("query"):
            params["query"] = cfg["query"]
        data = _source_request("get", f"https://{cfg['host']}/api/apply/v2/jobs", params=params,
            headers={"User-Agent": UA}, timeout=TIMEOUT).json()
        return data["positions"], data.get("count")
    rows = _pages(page, lambda j: j.get("id"), cfg)
    return _mapped(rows, lambda j: {
        "id": str(j["id"]), "title": j.get("name", ""),
        "location": j.get("location", "") or "",
        "url": j.get("canonicalPositionUrl") or f"https://{cfg['host']}/careers/job/{j['id']}",
    })


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "amazon_jobs": fetch_amazon_jobs,
    "microsoft": fetch_microsoft,
    "github_listings": fetch_github_listings,
    "github_md": fetch_github_md,
    "linkedin": fetch_linkedin,
    "eightfold": fetch_eightfold,
    "janestreet": fetch_janestreet,
    "phenom": fetch_phenom,
    "marqeta": fetch_marqeta,
    "hightouch": fetch_hightouch,
}


# ================================================================ storage
def db_open(path=None):
    con = sqlite3.connect(path if path is not None else DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS jobs(
        board TEXT, job_id TEXT, title TEXT, location TEXT, url TEXT,
        first_seen TEXT, last_seen TEXT,
        PRIMARY KEY(board, job_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS job_details(
        board TEXT, job_id TEXT, company TEXT DEFAULT '',
        salary TEXT DEFAULT '', posted REAL,
        PRIMARY KEY(board, job_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS boards(
        board TEXT PRIMARY KEY, seeded INTEGER DEFAULT 0,
        last_ok TEXT, failures INTEGER DEFAULT 0, last_poll REAL DEFAULT 0)""")
    columns = {r[1] for r in con.execute("PRAGMA table_info(boards)")}
    for column, definition in {"last_error": "TEXT DEFAULT ''", "last_count": "INTEGER DEFAULT 0",
                               "next_poll": "REAL DEFAULT 0"}.items():
        if column not in columns:
            con.execute(f"ALTER TABLE boards ADD COLUMN {column} {definition}")
    con.execute("""CREATE TABLE IF NOT EXISTS outbox(
        id TEXT PRIMARY KEY, sub TEXT, route TEXT, payload TEXT,
        created_at TEXT, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alerted(
        key TEXT PRIMARY KEY, ts TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sends(
        ts TEXT, subscriber TEXT, board TEXT, job_id TEXT, ok INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS recent(
        ts TEXT, company TEXT, title TEXT, url TEXT,
        location TEXT DEFAULT '', salary TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS pending(
        sub TEXT, tier TEXT, ts TEXT, company TEXT, title TEXT, url TEXT,
        location TEXT DEFAULT '', salary TEXT DEFAULT '', posted REAL)""")
    for table, additions in {
        "pending": {"expires_at": "REAL", "freshness_note": "TEXT DEFAULT ''"},
        "outbox": {"expires_at": "REAL"},
    }.items():
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for column, definition in additions.items():
            if column not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    con.execute("""CREATE TABLE IF NOT EXISTS kv(
        k TEXT PRIMARY KEY, v TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS applications(
        job_key TEXT PRIMARY KEY, board TEXT, job_id TEXT, company TEXT,
        title TEXT, location TEXT DEFAULT '', url TEXT, salary TEXT DEFAULT '',
        posted REAL, status TEXT DEFAULT 'saved', priority INTEGER DEFAULT 0,
        notes TEXT DEFAULT '', next_step TEXT DEFAULT '', due_date TEXT DEFAULT '',
        created_at TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS runs(
        started_at TEXT, finished_at TEXT, ok_count INTEGER, fail_count INTEGER,
        skipped_count INTEGER, new_count INTEGER, deduped_count INTEGER,
        delivered_count INTEGER, duration REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs(last_seen)")
    con.execute("CREATE INDEX IF NOT EXISTS details_posted ON job_details(posted)")
    con.execute("CREATE INDEX IF NOT EXISTS applications_status ON applications(status)")

    return con


def known_ids(con, board):
    return {r[0] for r in con.execute("SELECT job_id FROM jobs WHERE board=?", (board,))}


def board_row(con, board):
    r = con.execute("SELECT seeded, failures, last_poll FROM boards WHERE board=?",
                    (board,)).fetchone()
    if r is None:
        con.execute("INSERT INTO boards(board) VALUES (?)", (board,))
        return (0, 0, 0.0)
    return r


# ---- portable state (GitHub Actions mode) ----
def export_state(con):
    """Export the durable subset of SQLite without refreshing record ages.

    Actions starts with a new database on every run.  Persisting only IDs (as
    older state files did) made every imported row look newly seen, so the
    90-day pruning window could never expire and state.json grew forever.
    """
    state = {"version": 4, "boards": {}, "alerted": []}
    for board, seeded, last_poll, failures, last_ok, last_error, last_count, next_poll in con.execute(
            "SELECT board, seeded, last_poll, failures, last_ok, last_error, last_count, next_poll FROM boards"):
        jobs = [list(r) for r in con.execute(
            "SELECT job_id, last_seen FROM jobs WHERE board=? ORDER BY job_id",
            (board,))]
        state["boards"][board] = {"seeded": seeded, "jobs": jobs,
                                  "lp": int(last_poll or 0),
                                  "f": failures or 0, "last_ok": last_ok,
                                  "last_error": last_error, "last_count": last_count,
                                  "next_poll": next_poll}
    state["alerted"] = [list(r) for r in con.execute(
        "SELECT key, ts FROM alerted ORDER BY key")]
    state["recent"] = [list(r) for r in con.execute(
        "SELECT ts, company, title, url, location, salary FROM recent")]
    state["pending"] = [list(r) for r in con.execute(
        "SELECT sub, tier, ts, company, title, url, location, salary, posted, expires_at, freshness_note "
        "FROM pending")]
    state["outbox"] = [list(r) for r in con.execute(
        "SELECT id, sub, route, payload, created_at, attempts, next_try, expires_at FROM outbox")]
    state["kv"] = dict(con.execute("SELECT k, v FROM kv"))
    tmp = STATE_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(STATE_JSON)


def import_state(con):
    state = json.loads(STATE_JSON.read_text())
    ts = now()
    for board, b in state.get("boards", {}).items():
        con.execute(
            "INSERT OR REPLACE INTO boards(board, seeded, last_poll, failures, last_ok, last_error, last_count, next_poll) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (board, b.get("seeded", 1), b.get("lp", 0), b.get("f", 0),
             b.get("last_ok"), b.get("last_error", ""), b.get("last_count", 0), b.get("next_poll", 0)))
        # v1 stored only ``ids``.  Accept it once for a seamless migration;
        # v2 retains last_seen so stale jobs can actually age out.
        jobs = b.get("jobs")
        if jobs is None:
            jobs = [(job_id, ts) for job_id in b.get("ids", [])]
        con.executemany(
            """INSERT OR IGNORE INTO jobs
               (board, job_id, title, location, url, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?)""",
            [(board, str(r[0]), "", "", "", r[1] or ts, r[1] or ts)
             for r in jobs if r])
    alerted = state.get("alerted", [])
    con.executemany(
        "INSERT OR IGNORE INTO alerted VALUES (?,?)",
        [(str(r[0]), (r[1] if len(r) > 1 else ts))
         if isinstance(r, (list, tuple)) else (str(r), ts)
         for r in alerted if r])
    con.executemany("INSERT INTO recent VALUES (?,?,?,?,?,?)",
                    [tuple(r) + ("",) * (6 - len(r))
                     for r in state.get("recent", [])])
    con.executemany("INSERT INTO pending VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [tuple(r) + (None,) * (11 - len(r))
                     for r in state.get("pending", [])])
    con.executemany("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?,?)",
                    [tuple(r) + (None,) * (8 - len(r)) for r in state.get("outbox", [])])
    con.executemany("INSERT OR REPLACE INTO kv VALUES (?,?)",
                    list(state.get("kv", {}).items()))
    con.commit()
    log(f"imported state.json: {len(state.get('boards', {}))} boards")


# ================================================================ dedupe
def canon_url(url):
    if not url:
        return ""
    p = urlsplit(html.unescape(url.strip()))
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    query_items = dict(parse_qsl(p.query))
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}:
        host = host.replace("boards.", "job-boards.", 1) if host.startswith("boards.") else host
        # gh_jid is redundant only when the same ID is already in the job path.
        if path.rsplit("/", 1)[-1] == query_items.get("gh_jid"):
            query_items.pop("gh_jid")
    if host.endswith("myworkdayjobs.com"):
        path = re.sub(r"^/[a-z]{2}-[A-Z]{2}/", "/", path)
    if host == "amazon.jobs":
        match = re.search(r"/(?:[a-z]{2}/)?jobs/(\d+)", path)
        if match:
            path = "/jobs/" + match.group(1)
    # Keep identity-bearing query parameters (jobId, gh_jid, requisitionId, ...).
    tracking = {"source", "src", "ref", "referrer", "gh_src", "lever-source", "lever-origin", "trk", "trackingId", "fbclid", "gclid"}
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not (k == "gh_jid" and k not in query_items)
             if k not in tracking and not k.lower().startswith("utm_")
             and not (host == "zapply.jobs" and k == "s")]
    return f"{host}{path}" + ("?" + urlencode(sorted(query)) if query else "")


_CO_NOISE = re.compile(r"\(.*?\)|\b(incorporated|corporation|company|inc|llc|ltd|corp|co)\b\.?",
                       re.I)


def name_key(company, title, normalize=False):
    c = company or ""
    if normalize:               # 'Stripe, Inc.' / 'Stripe (YC S10)' -> 'Stripe'
        c = _CO_NOISE.sub("", c)
    c = re.sub(r"[^a-z0-9]+", "", c.lower())
    t = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    return f"nk:{c}|{t}" if c and t else ""


def dedupe_keys(job):
    """Distinct requisitions must survive even when company/title/location match.

    A company/title heuristic cannot prove two apply links are the same job.
    Prefer an occasional cross-site duplicate to hiding an actual opening.
    Legacy name-only keys in portable state deliberately no longer suppress URLs.
    """
    cu = canon_url(job.get("url", ""))
    if cu:
        return [f"url:{cu}"]
    nk = name_key(job.get("company", ""), job.get("title", ""), normalize=True)
    location = re.sub(r"\s+", " ", job.get("location", "").strip().casefold())
    return [f"{nk}|{location}"] if nk else []


def already_alerted(con, job):
    for k in dedupe_keys(job):
        if con.execute("SELECT 1 FROM alerted WHERE key=?", (k,)).fetchone():
            return True
    return False


def mark_alerted(con, job):
    ts = now()
    for k in dedupe_keys(job):
        con.execute("INSERT OR IGNORE INTO alerted VALUES (?,?)", (k, ts))


# ================================================================ matching
class Filt:
    def __init__(self, d, fallback=None):
        fb = fallback or {}
        self.include = self._c(d.get("include", fb.get("include")))
        self.exclude = self._c(d.get("exclude", fb.get("exclude")))
        self.location = self._c(d.get("location", fb.get("location")))

    @staticmethod
    def _c(pat):
        return re.compile(pat, re.I) if pat else None

    def ok(self, job):
        if self.include and not self.include.search(job["title"]):
            return False
        if self.exclude and self.exclude.search(job["title"]):
            return False
        if self.location and job["location"] and not self.location.search(job["location"]):
            return False
        return True


def _resolve_secret(v, quiet=False):
    """'env:NAME' in subscribers.json reads NAME from the environment, so
    webhook URLs never live in the repo (GitHub Actions secrets). quiet=True
    for optional per-tier hooks that silently fall back to the main one."""
    if isinstance(v, str) and v.startswith("env:"):
        name = v[4:]
        v = os.environ.get(name, "")
        if not v and not quiet:
            log(f"! secret {name} not set — delivery channel disabled")
    return v


def load_subscribers(global_filters):
    subs = json.loads(SUBSCRIBERS.read_text())
    out = []
    for name, s in subs.items():
        if name.startswith("_") or s.get("mute"):
            continue
        out.append({
            "name": name,
            "companies": set(s.get("companies", ["all"])),
            "filt": Filt(s.get("filters", {}), fallback=global_filters),
            "watch": Filt._c(s.get("watchlist", "")),
            "mention": s.get("discord_mention", ""),
            "ping_categories": set(s.get(
                "ping_categories", ["intern", "new_grad", "other"])),
            "max_ping_jobs": max(0, int(s.get("max_ping_jobs", 10))),
            "ops": bool(s.get("ops")),
            "digest": bool(s.get("digest")),
            "discord_configured": bool(s.get("discord_webhook") or any((s.get("ping_webhooks") or {}).values()) or any((s.get("feeds") or {}).values())),
            "discord": _resolve_secret(s.get("discord_webhook", "")),
            "ping_hooks": {k: _resolve_secret(v, quiet=True)
                           for k, v in (s.get("ping_webhooks") or {}).items()},
            "feeds": {k: _resolve_secret(v, quiet=True)
                      for k, v in (s.get("feeds") or {}).items()},
            "telegram_chat": str(s.get("telegram_chat_id", "") or ""),
        })
    return out


# ================================================================ delivery
def _post_with_retry(fn, what):
    for attempt in range(SEND_RETRIES):
        try:
            r = fn()
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("Retry-After") or
                                 r.json().get("retry_after", 2))
                except Exception:
                    wait = 2.0
                if wait > 15:
                    return False
                time.sleep(max(0, wait))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            if attempt == SEND_RETRIES - 1:
                log(f"! send failed ({what}): {type(e).__name__}")
                return False
            time.sleep(2 ** attempt)
    return False


# ---- rendering: group by company, tag/tier by role type ----
TAG_INTERN = re.compile(r"\bintern(ship)?\b|\bco.?op\b", re.I)
TAG_GRAD = re.compile(r"new ?grad|graduate|early career|university|campus|entry.level|junior", re.I)


def _category(title):
    if TAG_INTERN.search(title):
        return "intern"
    if TAG_GRAD.search(title):
        return "new_grad"
    return "other"


def _tag(title):
    return {"intern": "🛠️", "new_grad": "🎓", "other": "💼"}[_category(title)]


def _short_loc(loc):
    """Trim noisy country suffixes for display ('Austin, TX, United States'
    -> 'Austin, TX'). Filters still see the full string."""
    return re.sub(r"\s*,?\s*(united states of america|united states|usa)\s*$",
                  "", (loc or "").strip(), flags=re.I)


def _group(jobs):
    """[(board, job)] -> [(company, [job])], companies A-Z, titles A-Z.
    Groups case-insensitively — direct boards say 'stripe' while aggregators
    say 'Stripe' — and displays the best-cased variant."""
    groups = {}
    for b, j in jobs:
        raw = (j.get("company") or b.replace("_", " ")).strip()
        g = groups.setdefault(raw.casefold(), {"name": raw, "jobs": []})
        if raw != raw.lower() and g["name"] == g["name"].lower():
            g["name"] = raw            # prefer 'Stripe' over 'stripe'
        g["jobs"].append(j)
    return sorted(((g["name"], sorted(g["jobs"], key=lambda x: x["title"]))
                   for g in groups.values()), key=lambda kv: kv[0].lower())


def send_discord_ping(webhook, jobs, kind, mention="", omitted=0, enqueue=None):
    """The apply-now tier: one rich embed PER JOB (clickable title, location,
    salary) so each ping is a self-contained application card. kind is
    'internship' or 'new grad role'. The header line is plain content, so
    pings are also findable via Discord search."""
    color = 0xF1C40F if kind == "internship" else 0xE67E22
    ts = datetime.now(timezone.utc).isoformat()
    # content is what phone push previews show — plain text (markdown would
    # render as literal ** in the notification), one compact line per job so
    # you know company/role/location without opening Discord
    lines = []
    for b, j in jobs:
        company = j.get("company") or b.replace("_", " ")
        line = f"🎯 {company} — {j['title']}"
        loc = _short_loc(j["location"])
        if loc:
            line += f" · {loc}"
        age = _rel_age(j.get("posted"))
        if age:
            line += f" · {age}"
        lines.append(line[:170])
    shown = lines[:5]
    if len(lines) > 5:
        shown.append(f"…and {len(lines) - 5} more")
    if omitted:
        shown.append(f"📥 {omitted} lower-priority match{'es' if omitted != 1 else ''} "
                     "saved to the quiet feeds")
    header = "\n".join(shown)
    if mention:
        header = f"{mention}\n{header}"
    embeds = []
    for b, j in jobs:
        company = j.get("company") or b.replace("_", " ")
        parts = []
        loc = _short_loc(j["location"])
        if loc:
            parts.append(f"📍 {loc}")
        age = _rel_age(j.get("posted"))
        if age:
            parts.append(f"🕒 {age}")
        if j.get("_freshness_note"):
            parts.append(j["_freshness_note"])
        sal = (j.get("salary") or "").strip()
        if sal:
            parts.append(f"💰 {sal[:60]}")
        embeds.append({
            "title": f"{company} — {j['title']}"[:256],
            "url": j["url"],
            "description": "\n".join(parts),
            "color": color,
            "timestamp": ts,
            "footer": {"text": "jobwatch"},
        })
    ok = True
    for i in range(0, len(embeds), 10):     # 10 embeds max per message
        payload = {"embeds": embeds[i:i + 10]}
        if i == 0:
            payload["content"] = header
        if enqueue:
            enqueue(payload)
        else:
            ok &= _post_with_retry(
                lambda p=payload: requests.post(webhook, json=p, timeout=10), "discord")
    return ok


def _feed_blocks(jobs, watch=None):
    """[(header, [lines])] — one block per company; ⭐ marks watchlist
    companies so they pop while scrolling."""
    blocks = []
    for company, js in _group(jobs):
        star = "⭐ " if watch and watch.search(company) else ""
        lines = []
        for j in js:
            title = j["title"].replace("[", "(").replace("]", ")").strip()
            if len(title) > 90:
                title = title[:87] + "…"
            loc = _short_loc(j["location"])
            if len(loc) > 40:
                loc = loc[:37] + "…"
            line = f"{_tag(j['title'])} [{title}]({j['url']})"
            if loc:
                line += f" · {loc}"
            age = _rel_age(j.get("posted"))
            if age:
                line += f" · {age}"
            if j.get("_freshness_note"):
                line += " · " + j["_freshness_note"]
            sal = (j.get("salary") or "").strip()
            if sal:
                line += f" · 💰 {sal[:40]}"
            lines.append(line)
        blocks.append((f"{star}**{company}**", lines))
    return blocks


def _pack(blocks, limit):
    """Pack (header, lines) blocks into strings ≤ limit chars. A company
    split across chunks repeats its header with '(cont.)' so no job ever
    floats under the wrong company."""
    descs, cur = [], ""
    for header, lines in blocks:
        block = "\n".join([header] + lines)
        if len(block) <= limit - (len(cur) + 2 if cur else 0):
            cur = f"{cur}\n\n{block}" if cur else block
            continue
        if cur:
            descs.append(cur)
            cur = ""
        buf = header
        for line in lines:
            if len(buf) + len(line) + 1 > limit:
                descs.append(buf)
                buf = f"{header} *(cont.)*"
            buf += "\n" + line
        cur = buf
    if cur:
        descs.append(cur)
    return descs


def send_discord_feed(webhook, label, jobs, color, watch=None, enqueue=None):
    """Silent digest embed(s): jobs grouped under bold company names, @silent
    flag so the channel fills up quietly for browsing whenever."""
    descs = _pack(_feed_blocks(jobs, watch), 3500)
    ts = datetime.now(timezone.utc).isoformat()
    n = len(jobs)
    ok = True
    for i, desc in enumerate(descs):
        title = label.format(n=n, s="s" if n != 1 else "")
        if len(descs) > 1:
            title += f"  ·  {i + 1}/{len(descs)}"
        payload = {"embeds": [{"title": title, "description": desc,
                               "color": color, "timestamp": ts,
                               "footer": {"text": "jobwatch · quiet digest"}}],
                   "flags": 4096}
        if enqueue:
            enqueue(payload)
        else:
            ok &= _post_with_retry(
                lambda p=payload: requests.post(webhook, json=p, timeout=10), "discord")
    return ok


def send_discord_note(webhook, title, desc, color):
    """Single small embed for digests / ops notices."""
    payload = {"embeds": [{"title": title, "description": desc[:3900],
                           "color": color, "footer": {"text": "jobwatch"}}]}
    return _post_with_retry(
        lambda: requests.post(webhook, json=payload, timeout=10), "discord")


def send_telegram(chat_id, text, enqueue=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token and not enqueue:
        log("! telegram configured but TELEGRAM_BOT_TOKEN not set")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in _chunks(text, 3900):
        payload = {"text": chunk, "disable_web_page_preview": True}
        if enqueue:
            enqueue(payload)
        else:
            ok &= _post_with_retry(
                lambda p=payload: requests.post(url, json={"chat_id": chat_id, **p}, timeout=10),
                "telegram")
    return ok


def _chunks(s, n):
    parts, cur = [], ""
    for block in s.split("\n\n"):
        if len(cur) + len(block) + 2 > n and cur:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return parts


def _telegram_text(jobs):
    parts = [f"🆕 {len(jobs)} new job{'s' if len(jobs) != 1 else ''}"]
    for company, js in _group(jobs):
        lines = [f"— {company} —"]
        for j in js:
            loc = _short_loc(j["location"])
            lines.append(f"{_tag(j['title'])} {j['title']}"
                         + (f" · {loc}" if loc else ""))
            lines.append(f"   {j['url']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def deliver(sub, jobs, con):
    """Queue only freshness-approved jobs; dashboard collection remains broad."""
    jobs = [(b, j) for b, j in jobs if (j.get("_alert_expires_at") or 0) > time.time()]
    watch = sub.get("watch")
    ping_candidates = []
    ts = now()
    for b, j in jobs:
        cat = _category(j["title"])
        # watchlist matches company only — titles like 'Salesforce Developer
        # Intern' at a consultancy must not count
        if (watch and watch.search(j.get("company") or b)
                and cat in sub["ping_categories"]):
            ping_candidates.append((b, j))
        con.execute(
            "INSERT INTO pending VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sub["name"], "intern" if cat == "intern" else "ft", ts,
             j.get("company") or b.replace("_", " "), j["title"],
             j["url"], j.get("location") or "", j.get("salary") or "",
             j.get("posted"), j["_alert_expires_at"], j.get("_freshness_note", "")))

    # Keep loud alerts scarce and useful. Early-career roles sort first, then
    # the freshest postings; every omitted match is still retained in the
    # complete quiet feeds above.
    priority = {"intern": 0, "new_grad": 1, "other": 2}
    ping_candidates.sort(key=lambda pair: (
        priority[_category(pair[1]["title"])],
        -(_posted_epoch(pair[1].get("posted")) or 0),
        (pair[1].get("company") or pair[0]).casefold(),
        pair[1]["title"].casefold()))
    limit = sub["max_ping_jobs"]
    selected = ping_candidates[:limit]
    omitted = len(ping_candidates) - len(selected)
    tiers = {"ping_intern": [], "ping_ft": []}
    for pair in selected:
        cat = _category(pair[1]["title"])
        tiers["ping_intern" if cat == "intern" else "ping_ft"].append(pair)

    for tier, kind in (("ping_intern", "internship"), ("ping_ft", "role")):
        if tiers[tier] and sub.get("discord_configured", True):
            route = "ping:intern" if tier == "ping_intern" else "ping:full_time"
            expiry = min(j["_alert_expires_at"] for _, j in tiers[tier])
            send_discord_ping("", tiers[tier], kind, mention=sub["mention"], omitted=omitted,
                enqueue=lambda payload, route=route, expiry=expiry: _enqueue(con, sub["name"], route, payload, expiry))
            omitted = 0
    hot_all = tiers["ping_intern"] + tiers["ping_ft"]
    if sub["telegram_chat"] and hot_all:
        send_telegram(sub["telegram_chat"], "🎯 apply now\n\n" + _telegram_text(hot_all),
            enqueue=lambda payload: _enqueue(con, sub["name"], "telegram", payload,
                min(j["_alert_expires_at"] for _, j in hot_all)))
    return True


def _enqueue(con, subscriber, route, payload, expires_at=None):
    # Store channel references, never webhook URLs or bot tokens in portable state.
    encoded = json.dumps(payload, sort_keys=True)
    key = hashlib.sha256(f"{subscriber}|{route}|{encoded}".encode()).hexdigest()
    con.execute("INSERT OR IGNORE INTO outbox(id, sub, route, payload, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (key, subscriber, route, encoded, now(), expires_at))


def flush_outbox(con, subs, checkpoint=None):
    """Acknowledge each message independently; retry failures across restarts."""
    expire_notifications(con)
    con.commit()  # Collected IDs and their notification queues become durable together.
    if checkpoint:
        checkpoint()
    subscribers = {s["name"]: s for s in subs}
    blocked = set(con.execute("SELECT sub, route FROM outbox WHERE next_try > ?", (time.time(),)))
    deadline = time.monotonic() + 90
    rows = con.execute("SELECT id, sub, route, payload, attempts, expires_at FROM outbox WHERE next_try <= ? ORDER BY CASE WHEN route LIKE 'ping:%' THEN 0 ELSE 1 END, created_at, rowid LIMIT 100",
                       (time.time(),)).fetchall()
    for key, name, route, encoded, attempts, expires_at in rows:
        if time.monotonic() >= deadline:
            break
        if expires_at <= time.time():
            con.execute("DELETE FROM outbox WHERE id=?", (key,))
            con.commit()
            continue
        sub = subscribers.get(name)
        if not sub or (name, route) in blocked:
            continue
        payload = json.loads(encoded)
        if route == "telegram":
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not token or not sub["telegram_chat"]:
                blocked.add((name, route))
                continue
            endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
            payload["chat_id"] = sub["telegram_chat"]
        else:
            kind, tier = route.split(":", 1)
            mapping = sub["ping_hooks"] if kind == "ping" else sub["feeds"]
            endpoint = mapping.get(tier) or sub["discord"]
            if not endpoint:
                blocked.add((name, route))
                continue
        retry_after = 0
        def send():
            nonlocal retry_after
            if time.time() >= expires_at:
                raise ValueError("notification freshness window expired")
            response = requests.post(endpoint, json=payload, timeout=10)
            if response.status_code == 429:
                retry_after = max(retry_after, _retry_delay(response, 60))
                try:
                    retry_after = max(retry_after, float(response.json().get("retry_after", 0)))
                except (TypeError, ValueError):
                    pass
            return response
        ok = _post_with_retry(send, route)
        if ok:
            con.execute("DELETE FROM outbox WHERE id=?", (key,))
        else:
            blocked.add((name, route))
            con.execute("UPDATE outbox SET attempts=attempts+1, next_try=? WHERE id=?",
                        (time.time() + max(retry_after, min(3600, 60 * 2 ** min(attempts, 6))), key))
        con.commit()
    if checkpoint:
        checkpoint()


def flush_feeds(config, con, subs, force=False):
    """Send queued feed jobs as one quiet digest per stream once the oldest
    queued job is feed_flush_minutes old (or the queue is huge). Pings never
    wait — this only paces the browse-later material."""
    expire_notifications(con)
    flush_min = config.get("feed_flush_minutes", 5)
    for sub in subs:
        rows = con.execute(
            "SELECT tier, ts, company, title, url, location, salary, posted, expires_at, freshness_note "
            "FROM pending WHERE sub=?", (sub["name"],)).fetchall()
        if not rows:
            continue
        oldest = min(r[1] for r in rows)
        age_min = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(oldest)).total_seconds() / 60
        expires_soon = min(r[8] for r in rows) <= time.time() + flush_min * 60
        if not force and not expires_soon and age_min < flush_min and len(rows) < 80:
            continue
        by_tier = {"intern": [], "ft": []}
        seen = set()
        for tier, ts, company, title, url, location, salary, posted, expires_at, freshness_note in rows:
            key = (canon_url(url), company.casefold(), title.casefold())
            if key in seen:     # same job queued twice via near-identical rows
                continue
            seen.add(key)
            by_tier.setdefault(tier, []).append(
                ("", {"company": company, "title": title, "url": url,
                      "location": location, "salary": salary, "posted": posted,
                      "_alert_expires_at": expires_at, "_freshness_note": freshness_note}))
        for tier, route_tier, label, color in (
            ("intern", "intern", "🛠️ {n} new internship{s}", 0x5865F2),
            ("ft", "full_time", "💼 {n} new-grad / full-time role{s}", 0x95A5A6),
        ):
            if by_tier[tier] and sub.get("discord_configured", True):
                expiry = min(j["_alert_expires_at"] for _, j in by_tier[tier])
                send_discord_feed("", label, by_tier[tier], color, sub.get("watch"),
                    enqueue=lambda payload, route_tier=route_tier, expiry=expiry: _enqueue(con, sub["name"], "feed:" + route_tier, payload, expiry))
        if sub["telegram_chat"]:
            everything = by_tier["intern"] + by_tier["ft"]
            send_telegram(sub["telegram_chat"], _telegram_text(everything),
                enqueue=lambda payload: _enqueue(con, sub["name"], "telegram", payload,
                    min(j["_alert_expires_at"] for _, j in everything)))
        con.execute("DELETE FROM pending WHERE sub=?", (sub["name"],))
        log(f"  -> {sub['name']}: queued {len(rows)} feed job(s) for delivery")


def maybe_digest(config, con, subs):
    """Once a day (after digest_hour_utc), send subscribers with digest:true a
    summary of the last 24h — doubles as an is-it-alive heartbeat."""
    hour = config.get("digest_hour_utc")
    if hour is None:
        return
    now_dt = datetime.now(timezone.utc)
    if now_dt.hour < hour:
        return
    today = now_dt.date().isoformat()
    row = con.execute("SELECT v FROM kv WHERE k='last_digest'").fetchone()
    if row and row[0] >= today:
        return
    con.execute("INSERT OR REPLACE INTO kv VALUES ('last_digest', ?)", (today,))

    rows = con.execute(
        "SELECT company, COUNT(*) FROM recent WHERE ts > datetime('now','-1 day') "
        "GROUP BY company ORDER BY COUNT(*) DESC, company").fetchall()
    total = sum(n for _, n in rows)
    n_boards = con.execute(
        "SELECT COUNT(*) FROM boards WHERE seeded=1").fetchone()[0]
    broken = [b for (b,) in con.execute(
        "SELECT board FROM boards WHERE failures >= 10 ORDER BY board")
              if b in config["boards"] and not config["boards"][b].get("disabled")]

    if rows:
        top = "\n".join(f"• **{c}** — {n}" for c, n in rows[:10])
        more = f"\n…plus {len(rows) - 10} more companies" if len(rows) > 10 else ""
        desc = (f"**{total}** new job{'s' if total != 1 else ''} across "
                f"**{len(rows)}** compan{'ies' if len(rows) != 1 else 'y'} "
                f"in the last 24h:\n{top}{more}")
    else:
        desc = "Quiet day — no new matching jobs in the last 24h."
    desc += f"\n\nWatching {n_boards} boards."
    if broken:
        desc += f"\n⚠️ failing: {', '.join(broken)}"

    for sub in subs:
        if not sub["digest"]:
            continue
        if sub["discord"]:
            send_discord_note(sub["discord"], f"📊 daily digest — {today}",
                              desc, 0x2ECC71)
        if sub["telegram_chat"]:
            send_telegram(sub["telegram_chat"],
                          f"📊 daily digest — {today}\n\n"
                          + desc.replace("**", ""))


# ================================================================ core cycle
_HOST_LOCKS = {}
_HOST_LOCKS_GUARD = threading.Lock()


def fetch_board(name, cfg):
    host = cfg.get("host") or ("linkedin.com" if cfg["ats"] == "linkedin" else name)
    with _HOST_LOCKS_GUARD:
        lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    with lock:
        return _fetch_board(name, cfg)


def _fetch_board(name, cfg):
    for attempt in range(2):
        try:
            jobs = ADAPTERS[cfg["ats"]](cfg)
            break
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise
            time.sleep(0.75)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if attempt or status == 429 or status < 500:
                raise
            time.sleep(0.75)
    result = FetchResult(warnings=getattr(jobs, "warnings", ()))
    seen = set()
    for j in jobs:
        if (not isinstance(j, dict) or j.get("id") in (None, "", "None")
                or not isinstance(j.get("title"), str) or not j["title"].strip()
                or not isinstance(j.get("url"), str)
                or urlsplit(j["url"]).scheme not in ("http", "https")):
            result.warnings.append("invalid posting omitted (missing ID, title or apply URL)")
            continue
        j["id"] = str(j["id"])
        j["location"] = j.get("location") or ""
        j["company"] = j.get("company") or cfg.get("company") or name.replace("_", " ")
        if j["id"] not in seen:
            seen.add(j["id"])
            result.append(j)
    return name, result


DISCOVERY_ONLY_ATS = {"linkedin", "github_md", "github_listings"}


def _collection_signature(cfg):
    # Bump this version if collection semantics change. Expanded coverage must
    # establish a fresh baseline before undated jobs can generate notifications.
    return hashlib.sha256(("freshness-v1:" + json.dumps(cfg, sort_keys=True)).encode()).hexdigest()


def alert_freshness(job, cfg, baseline, settings, timestamp):
    """Return a conservative expiry and explanation, or a suppression reason."""
    if cfg["ats"] in DISCOVERY_ONLY_ATS:
        return None, "discovery source (original publication unverified)"
    hours = settings.get("max_age_hours", 24)
    max_age = hours * 3600
    posted = _posted_epoch(job.get("posted"))
    if posted is not None:
        age = timestamp - posted
        if age < 0:
            return None, "future posting date"
        if age >= max_age:
            return None, "older than alert window"
        return posted + max_age, ""
    if job.get("posted") not in (None, ""):
        return None, "invalid posting date"
    if (baseline and baseline.get("signature") == _collection_signature(cfg)
            and 0 <= timestamp - baseline.get("checked_at", 0)
            < min(max_age, settings.get("observation_max_gap_hours", 6) * 3600)):
        return baseline["checked_at"] + max_age, "New on company board · original posting date unavailable"
    return None, "no recent comparable company-board baseline"


def expire_notifications(con):
    """Legacy queued payloads have no freshness evidence: retire them too."""
    count = 0
    for table in ("pending", "outbox"):
        count += con.execute(f"DELETE FROM {table} WHERE expires_at IS NULL OR expires_at <= ?",
                             (time.time(),)).rowcount
    if count:
        log(f"  freshness: retired {count} expired or unverified queued notification(s)")
    return count


def _record_failure(con, name, cfg, failures, error, broke, retry_after=0, backoff=True):
    failures += 1
    delay = max(retry_after, min(21600, max(60, cfg.get("min_interval", 0)) * (2 ** min(failures - 1, 8) if backoff else 1)))
    con.execute("UPDATE boards SET failures=?, last_error=?, next_poll=? WHERE board=?",
                (failures, error[:1000], time.time() + delay, name))
    if failures == 10:
        broke.append((name, error[:180]))
    if failures in (1, 3, 10) or failures % 50 == 0:
        log(f"! {name}: {error[:300]} (streak {failures}; retry in {int(delay)}s)")


def cycle(config, con):
    t0 = time.time()
    started_at = now()
    all_boards = {k: v for k, v in config["boards"].items()
                  if not k.startswith("_") and not v.get("disabled")}
    subs = load_subscribers(config.get("filters", {}))
    new_by_board = {}
    freshness_settings = config.get("alert_freshness", {})
    freshness_suppressed = 0
    expire_notifications(con)
    broke = []
    ok_count = fail_count = skipped = 0

    # respect per-board min_interval (big feeds like the 11MB listings repos
    # only change every ~15 min; no point fetching them every 45s)
    due = {}
    for name, cfg in all_boards.items():
        _, _, last_poll = board_row(con, name)
        next_poll = con.execute("SELECT next_poll FROM boards WHERE board=?", (name,)).fetchone()[0]
        if time.time() < (next_poll or 0) or time.time() - last_poll < cfg.get("min_interval", 0):
            skipped += 1
            continue
        due[name] = cfg

    con.commit()  # Do not hold a SQLite write lock while waiting on remote boards.
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_board, n, c): n for n, c in due.items()}
        completed = [(futs[fut], fut) for fut in as_completed(futs)]
        for name, fut in completed:
            seeded, failures, _ = board_row(con, name)
            baseline_key = "freshness_baseline:" + name
            saved = con.execute("SELECT v FROM kv WHERE k=?", (baseline_key,)).fetchone()
            baseline = json.loads(saved[0]) if saved else None
            if failures:
                baseline = None
            con.execute("UPDATE boards SET last_poll=? WHERE board=?",
                        (time.time(), name))
            try:
                _, jobs = fut.result()
            except Exception as e:
                retry_after = _retry_delay(getattr(e, "response", None), 0)
                _record_failure(con, name, due[name], failures,
                                f"{type(e).__name__}: {e}", broke, retry_after)
                fail_count += 1
                con.execute("DELETE FROM kv WHERE k=?", (baseline_key,))
                continue

            known = known_ids(con, name)
            warnings = list(getattr(jobs, "warnings", ()))
            if not jobs and len(known) >= ZERO_GUARD_MIN:
                warnings.append(f"returned 0 jobs with {len(known)} known; read ignored")
            con.execute("UPDATE boards SET last_count=? WHERE board=?", (len(jobs), name))
            if warnings:
                baseline = None
                con.execute("DELETE FROM kv WHERE k=?", (baseline_key,))
                _record_failure(con, name, due[name], failures, "; ".join(dict.fromkeys(warnings)), broke, backoff=not bool(jobs))
                fail_count += 1
                if not jobs:
                    continue
            else:
                ok_count += 1
                con.execute("UPDATE boards SET failures=0, last_ok=?, last_error='', next_poll=0 WHERE board=?",
                            (now(), name))

            fresh = [j for j in jobs if j["id"] and j["id"] not in known]
            for j in fresh:
                expiry, note = alert_freshness(j, due[name], baseline, freshness_settings, time.time())
                j["_alert_expires_at"] = expiry
                j["_freshness_note"] = note
            if not warnings:
                con.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (baseline_key, json.dumps({
                    "signature": _collection_signature(due[name]), "checked_at": t0})))
            ts = now()
            con.executemany(
                """INSERT INTO jobs
                   (board, job_id, title, location, url, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(board, job_id) DO UPDATE SET
                     title=excluded.title, location=excluded.location,
                     url=excluded.url, last_seen=excluded.last_seen""",
                [(name, j["id"], j["title"], j["location"], j["url"], ts, ts)
                 for j in jobs])
            con.executemany(
                """INSERT INTO job_details(board, job_id, company, salary, posted)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(board, job_id) DO UPDATE SET
                     company=excluded.company, salary=excluded.salary,
                     posted=excluded.posted""",
                [(name, j["id"], j.get("company") or name.replace("_", " "),
                  j.get("salary") or "", _posted_epoch(j.get("posted")))
                 for j in jobs])
            con.execute("UPDATE boards SET seeded=1 WHERE board=?", (name,))

            if not seeded:
                log(f"  {name}: seeded {len(jobs)} postings silently")
            elif fresh:
                new_by_board[name] = fresh

    # ---- cross-source dedupe gate (same job via ATS + repo = one alert)
    excl_co = Filt._c((config.get("filters") or {}).get("exclude_companies"))
    total_new = deduped = agencies = 0
    gated = {}
    seen_keys = set()               # same job from two sources in ONE cycle
    # direct boards first so when LinkedIn and the company's own ATS surface
    # the same job in one cycle, the alert carries the direct apply link
    ordered = sorted(new_by_board.items(),
                     key=lambda kv: (all_boards[kv[0]]["ats"] in {"linkedin", "github_md", "github_listings"}, kv[0]))
    for board, jobs in ordered:
        keep = []
        for j in jobs:
            total_new += 1
            if not j.get("_alert_expires_at") or j["_alert_expires_at"] <= time.time():
                freshness_suppressed += 1
                continue
            if excl_co and excl_co.search(j.get("company") or ""):
                agencies += 1       # staffing-agency spam: drop, don't alert
                continue
            ks = dedupe_keys(j)
            if already_alerted(con, j):
                deduped += 1
                continue
            if any(k in seen_keys for k in ks):
                deduped += 1
            seen_keys.update(ks)
            keep.append(j)
        if keep:
            gated[board] = keep

    # ---- fanout
    delivered = 0
    for sub in subs:
        mine = []
        subscriber_seen = set()
        for board, jobs in gated.items():
            if "all" not in sub["companies"] and board not in sub["companies"]:
                continue
            for j in jobs:
                keys = dedupe_keys(j)
                if sub["filt"].ok(j) and not any(k in subscriber_seen for k in keys):
                    mine.append((board, j))
                    subscriber_seen.update(keys)
        if mine:
            mine.sort(key=lambda t: (t[1].get("company", ""), t[1]["title"]))
            deliver(sub, mine, con)
            delivered += len(mine)
            log(f"  -> {sub['name']}: {len(mine)} job(s)")

    # Only freshness-qualified jobs enter dedupe memory. A discovery-only
    # echo must not suppress a later verified alert from a company board.
    ts = now()
    for board, jobs in gated.items():
        for j in jobs:
            if already_alerted(con, j):
                continue
            mark_alerted(con, j)
            con.execute("INSERT INTO recent VALUES (?,?,?,?,?,?)",
                        (ts, j.get("company") or board.replace("_", " "),
                         j["title"], j["url"], j.get("location") or "",
                         j.get("salary") or ""))

    flush_feeds(config, con, subs)
    flush_outbox(con, subs, checkpoint=lambda: export_state(con))

    if broke:
        detail = ", ".join(f"{n} ({err})" for n, err in broke)
        for sub in subs:
            if sub["ops"] and sub["discord"]:
                send_discord_note(
                    sub["discord"], "⚠️ jobwatch: board trouble",
                    f"{len(broke)} board(s) failing 10 polls in a row: {detail}\n"
                    f"Coverage is incomplete; recovered postings are still retained. "
                    f"Check Sources or run `python watcher.py --verify`.",
                    0xE74C3C)

    maybe_digest(config, con, subs)

    con.execute("DELETE FROM jobs WHERE last_seen < datetime('now', ?)",
                (f"-{PRUNE_AFTER_DAYS} days",))
    con.execute("""DELETE FROM job_details WHERE NOT EXISTS (
                   SELECT 1 FROM jobs WHERE jobs.board=job_details.board
                   AND jobs.job_id=job_details.job_id)""")
    con.execute("DELETE FROM alerted WHERE ts < datetime('now', ?)",
                (f"-{PRUNE_AFTER_DAYS} days",))
    con.execute("DELETE FROM recent WHERE ts < datetime('now', ?)",
                (f"-{RECENT_DAYS} days",))
    # Retry only while the application opportunity still meets the freshness window.
    con.execute("DELETE FROM sends WHERE ts < datetime('now', '-90 days')")
    duration = time.time() - t0
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
        (started_at, now(), ok_count, fail_count, skipped, total_new,
         deduped, delivered, duration))
    con.execute("""DELETE FROM runs WHERE rowid NOT IN
                   (SELECT rowid FROM runs ORDER BY rowid DESC LIMIT 1000)""")
    con.commit()

    log(f"cycle: {ok_count} ok / {fail_count} fail / {skipped} not-due | "
        f"{total_new} new, {deduped} cross-source dupes, {agencies} agency-spam, "
        f"{freshness_suppressed} old/unverified (dashboard only), "
        f"{delivered} queued matches | {duration:.1f}s")

    hc = os.environ.get("HEALTHCHECK_URL", config.get("healthcheck_url", ""))
    if hc and (ok_count > 0):
        try:
            requests.get(hc, timeout=10)
        except Exception:
            pass


# ================================================================ cli
def cmd_check(config):
    errs = 0
    try:
        Filt(config.get("filters", {}))
        Filt._c(config.get("filters", {}).get("exclude_companies"))
    except re.error as e:
        print(f"✗ global filters: bad regex: {e}")
        errs += 1
    freshness = config.get("alert_freshness", {})
    for field in ("max_age_hours", "observation_max_gap_hours"):
        value = freshness.get(field, 24 if field == "max_age_hours" else 6)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            print(f"✗ alert_freshness {field}: must be a positive finite number")
            errs += 1
    profile = config.get("profile", {})
    for section in ("track_weights", "seniority_weights", "location_weights",
                    "company_type_weights", "year_weights", "skills",
                    "education_penalties"):
        values = profile.get(section, {})
        if not isinstance(values, dict) or any(
                isinstance(v, bool) or not isinstance(v, (int, float))
                for v in values.values()):
            print(f"✗ profile {section}: values must be numeric weights")
            errs += 1
    for name, b in config["boards"].items():
        if name.startswith("_"):
            continue
        if b.get("ats") not in ADAPTERS:
            print(f"✗ board {name}: unknown ats {b.get('ats')}")
            errs += 1
    required = {
        "greenhouse": ("token",), "lever": ("token",), "ashby": ("token",),
        "smartrecruiters": ("token",), "workday": ("host", "tenant", "site"),
        "eightfold": ("host", "domain"), "phenom": ("host",),
        "github_listings": ("repo",), "github_md": ("repo",),
    }
    for name, board in config["boards"].items():
        if name.startswith("_") or board.get("disabled"):
            continue
        for field in required.get(board.get("ats"), ()):
            if not isinstance(board.get(field), str) or not board[field].strip():
                print(f"✗ board {name}: {field} must be a nonempty string")
                errs += 1
        for field in ("max_pages", "fetch_budget_seconds", "pages", "size", "min_interval"):
            if field in board and (type(board[field]) is not int or board[field] < (0 if field == "min_interval" else 1)):
                print(f"✗ board {name}: invalid {field}")
                errs += 1
        if board.get("ats") == "github_md":
            cols = board.get("cols", {})
            if (not {"company", "title", "url"} <= cols.keys()
                    or any(type(v) is not int or v < 0 for v in cols.values())):
                print(f"✗ board {name}: invalid markdown column mapping")
                errs += 1
    subs = json.loads(SUBSCRIBERS.read_text())
    n_subs = 0
    for name, s in subs.items():
        if name.startswith("_"):
            continue
        n_subs += 1
        try:
            Filt(s.get("filters", {}), fallback=config.get("filters", {}))
            Filt._c(s.get("watchlist", ""))
        except re.error as e:
            print(f"✗ subscriber {name}: bad regex: {e}")
            errs += 1
        for c in s.get("companies", ["all"]):
            if c != "all" and c not in config["boards"]:
                print(f"✗ subscriber {name}: unknown company '{c}'")
                errs += 1
        categories = s.get("ping_categories", ["intern", "new_grad", "other"])
        invalid = set(categories) - {"intern", "new_grad", "other"}
        if invalid:
            print(f"✗ subscriber {name}: unknown ping categories "
                  f"{', '.join(sorted(invalid))}")
            errs += 1
        cap = s.get("max_ping_jobs", 10)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            print(f"✗ subscriber {name}: max_ping_jobs must be a non-negative integer")
            errs += 1
        if not s.get("discord_webhook") and not s.get("telegram_chat_id"):
            print(f"⚠ subscriber {name}: no delivery channel configured")
    n_boards = sum(1 for k in config["boards"] if not k.startswith("_"))
    print("config INVALID" if errs else f"config ok: {n_boards} boards, {n_subs} subscribers")
    sys.exit(1 if errs else 0)


def cmd_verify(config):
    """Hit every enabled board once. The fastest way to fix wrong tokens."""
    boards = {k: v for k, v in config["boards"].items()
              if not k.startswith("_") and not v.get("disabled")}
    good = bad = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_board, n, c): n for n, c in boards.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                _, jobs = fut.result()
                if jobs.warnings:
                    print(f"⚠ {name:22} {len(jobs):>5} postings; PARTIAL: {'; '.join(dict.fromkeys(jobs.warnings))[:240]}")
                    bad += 1
                else:
                    print(f"✓ {name:22} {len(jobs):>5} postings")
                    good += 1
            except Exception as e:
                print(f"✗ {name:22} {type(e).__name__}: {str(e)[:90]}")
                bad += 1
    if bad:
        print(f"\n{good} complete, {bad} failed or partial. Review the errors above; "
              f"partial reads still retain recovered postings.")
    else:
        print(f"\n{good} working, 0 broken — every enabled source is healthy.")
    return 1 if bad else 0


def cmd_test(config, who):
    subs = load_subscribers(config.get("filters", {}))
    sub = next((s for s in subs if s["name"] == who), None)
    if not sub:
        sys.exit(f"no active subscriber '{who}'")
    if not sub["discord"] and not sub["telegram_chat"]:
        sys.exit(f"'{who}' has no delivery channel — if the webhook is an "
                 f"env: secret, export it first")
    if sub["discord"]:
        send_discord_note(
            sub["discord"], "🧪 jobwatch test — every delivery scenario",
            "How this works, in one line: **high-signal watchlist roles "
            "buzz you; every match lands quietly in the feeds.**"
            "\n\nThe next messages demo it with fake jobs:\n\n"
            "**1. 🎯 apply-now intern (LOUD, gold cards)** — internships at "
            "watchlist companies → Stripe, Jane Street\n"
            "**2. 🎯 apply-now full-time (LOUD, orange cards)** — new-grad "
            "roles at watchlist companies → Databricks\n"
            "**3. 🛠️ internships feed (@silent)** — EVERY internship incl. "
            "the apply-now ones (⭐ = watchlist company) → ⭐ Stripe, "
            "⭐ Jane Street, SomeCo\n"
            "**4. 💼 full-time feed (@silent)** — every other role, same "
            "idea → ⭐ Databricks, ⭐ Anthropic, RandomCorp\n"
            "Loud cards are capped; overflow stays in these feeds.\n"
            "\nThat's the whole system.",
            0x9B59B6)
    nowt = time.time()
    fake = [
        ("test", {"id": "t1", "company": "Stripe",
                  "title": "Software Engineer, Intern (Summer 2026)",
                  "location": "New York, NY", "salary": "$55 – $62/hr",
                  "posted": nowt - 3600, "url": "https://example.com/1"}),
        ("test", {"id": "t2", "company": "Jane Street",
                  "title": "Quantitative Trader — Summer Internship",
                  "location": "New York, NY", "posted": nowt - 5 * 3600,
                  "url": "https://example.com/2"}),
        ("test", {"id": "t3", "company": "Databricks",
                  "title": "New Grad Software Engineer",
                  "location": "Toronto, ON", "salary": "$140K – $180K",
                  "posted": nowt - 2 * 86400, "url": "https://example.com/3"}),
        ("test", {"id": "t4", "company": "Anthropic",
                  "title": "Software Engineer, 2026 Start",
                  "location": "Remote — US", "url": "https://example.com/4"}),
        ("test", {"id": "t5", "company": "SomeCo",
                  "title": "Software Engineer Intern",
                  "location": "Chicago, IL", "posted": nowt - 30 * 60,
                  "url": "https://example.com/5"}),
        ("test", {"id": "t6", "company": "RandomCorp",
                  "title": "Machine Learning Engineer",
                  "location": "Seattle, WA", "url": "https://example.com/6"}),
    ]
    con = db_open()
    for _, job in fake:
        job["_alert_expires_at"] = nowt + 3600
    ok = deliver(sub, fake, con)
    flush_feeds(config, con, [sub], force=True)   # demo digests immediately
    flush_outbox(con, [sub])
    print("examples queued; check outbox for any delivery failures" if ok else "send FAILED")
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--list", metavar="BOARD")
    ap.add_argument("--test", metavar="SUBSCRIBER")
    ap.add_argument("--serve", action="store_true",
                    help="open the local daily-use dashboard")
    ap.add_argument("--host", default="127.0.0.1",
                    help="dashboard host (default: loopback only)")
    ap.add_argument("--port", type=int, default=8787,
                    help="dashboard port (default: 8787)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open the dashboard in a browser")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text())

    if args.check:
        cmd_check(config)
    if args.verify:
        sys.exit(cmd_verify(config))
    if args.test:
        cmd_test(config, args.test)
        return
    if args.list:
        _, jobs = fetch_board(args.list, config["boards"][args.list])
        for j in sorted(jobs, key=lambda x: (x.get("company", ""), x["title"])):
            print(f"{(j.get('company') or '')[:18]:18} | {j['title']!r:55.55} | "
                  f"{j['location']!r:28.28} | {j['url'][:60]}")
        print(f"\n{len(jobs)} live postings")
        return
    if args.serve:
        from dashboard import serve
        serve(args.host, args.port, not args.no_browser,
              DB_PATH, Path(args.config), SUBSCRIBERS)
        return

    # single-instance lock (POSIX)
    try:
        import fcntl
        lockf = open(HERE / ".watcher.lock", "w")
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another watcher instance is already running")
    except ImportError:
        pass  # windows: rely on the user not double-launching

    fresh_db = not DB_PATH.exists()
    con = db_open()
    if fresh_db and STATE_JSON.exists():
        import_state(con)

    interval = config.get("interval_seconds", 45)

    if args.once:
        cycle(config, con)
        export_state(con)      # portable state for Actions runners
        return

    log(f"watching {sum(1 for k in config['boards'] if not k.startswith('_'))} "
        f"boards every ~{interval}s")
    while True:
        try:
            cycle(config, con)
        except KeyboardInterrupt:
            break
        except Exception as e:
            con.rollback()
            log(f"! cycle crashed: {type(e).__name__}: {e}")
        time.sleep(max(5, interval) + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    main()
