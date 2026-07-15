#!/usr/bin/env python3
"""
jobwatch v4 — multi-user, multi-source job posting watcher.

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
import os
import random
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

HERE = Path(__file__).parent
DB_PATH = HERE / "jobwatch.db"
CONFIG = HERE / "config.json"
SUBSCRIBERS = HERE / "subscribers.json"
STATE_JSON = HERE / "state.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
TIMEOUT = 25
PRUNE_AFTER_DAYS = 90   # dedupe memory: a job re-posted after this alerts again
RECENT_DAYS = 14        # how far back the job-board page reaches
ZERO_GUARD_MIN = 5
SEND_RETRIES = 3


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"{now()} {msg}", flush=True)


# ================================================================ adapters
# Each returns list of {id, title, location, url, company}. id stable per board.

def fetch_greenhouse(cfg):
    url = f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs?content=false"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
    } for j in r.json().get("jobs", [])]


def fetch_lever(cfg):
    url = f"https://api.lever.co/v0/postings/{cfg['token']}?mode=json"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("text", ""),
        "location": (j.get("categories") or {}).get("location", "") or "",
        "url": j.get("hostedUrl", ""),
    } for j in r.json()]


def fetch_ashby(cfg):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{cfg['token']}?includeCompensation=true"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j.get("id")),
        "title": j.get("title", ""),
        "location": j.get("location", "") or "",
        "url": j.get("jobUrl", ""),
        "salary": (j.get("compensation") or {}).get("compensationTierSummary", ""),
    } for j in r.json().get("jobs", [])]


def fetch_smartrecruiters(cfg):
    t = cfg["token"]
    url = f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append({
            "id": str(j.get("id")),
            "title": j.get("name", ""),
            "location": ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
            "url": f"https://jobs.smartrecruiters.com/{t}/{j.get('id')}",
        })
    return out


def fetch_workday(cfg):
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset, total = [], 0, None
    while True:
        body = {"appliedFacets": cfg.get("facets", {}), "limit": 20,
                "offset": offset, "searchText": cfg.get("search", "")}
        r = requests.post(url, json=body, timeout=TIMEOUT,
                          headers={"User-Agent": UA, "Accept": "application/json",
                                   "Content-Type": "application/json"})
        r.raise_for_status()
        data = r.json()
        if total is None:       # many tenants only report total on page one
            total = data.get("total", 0)
        posts = data.get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            out.append({
                "id": path,
                "title": j.get("title", ""),
                "location": j.get("locationsText", "") or "",
                "url": f"https://{host}/en-US/{site}{path}",
            })
        offset += 20
        if not posts or offset >= total or offset >= 300:
            break
    return out


def fetch_amazon_jobs(cfg):
    """amazon.jobs public search JSON. cfg: query (e.g. 'software intern'),
    optionally country. Returns newest 100 matches."""
    params = {
        "base_query": cfg.get("query", "software engineer intern"),
        "result_limit": 100, "offset": 0, "sort": "recent",
    }
    if cfg.get("country"):
        params["country"] = cfg["country"]
    r = requests.get("https://www.amazon.jobs/en/search.json", params=params,
                     headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": str(j.get("id_icims") or j.get("id")),
            "title": j.get("title", ""),
            "location": j.get("normalized_location", "") or j.get("location", ""),
            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
        })
    return out


def fetch_microsoft(cfg):
    """Microsoft careers public search JSON. cfg: query."""
    params = {"q": cfg.get("query", "software engineer"), "l": "en_us",
              "pg": 1, "pgSz": 100, "o": "Relevance", "flt": "true"}
    r = requests.get("https://gcsservices.careers.microsoft.com/search/api/v1/search",
                     params=params, headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    jobs = (((r.json().get("operationResult") or {}).get("result") or {}).get("jobs")) or []
    out = []
    for j in jobs:
        jid = str(j.get("jobId", ""))
        props = j.get("properties") or {}
        locs = props.get("locations") or []
        out.append({
            "id": jid,
            "title": j.get("title", ""),
            "location": "; ".join(locs[:3]),
            "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
        })
    return out


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
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not j.get("active") or not j.get("is_visible", True):
            continue
        locs = j.get("locations") or []
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": " | ".join(locs[:3]),
            "url": j.get("url", ""),
            "company": j.get("company_name", ""),
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


def fetch_github_md(cfg):
    """Community listing repos that publish a markdown table (speedyapply,
    Canadian lists, off-season lists, ...). cfg: repo, branch (default 'main'),
    path (default 'README.md'), cols: 0-based column indexes for
    company/title/url (+ optional location/salary). Handles md links, HTML
    anchors, shields.io Apply badges, and repos that repeat/omit the company
    on continuation rows."""
    cols = cfg["cols"]
    url = (f"https://raw.githubusercontent.com/{cfg['repo']}/"
           f"{cfg.get('branch', 'main')}/{cfg.get('path', 'README.md')}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    need = max(cols.values()) + 1
    out, seen, prev_company = [], set(), ""
    for row in r.text.splitlines():
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < need:
            continue
        title = _cell_text(cells[cols["title"]])
        if (not title or set(title) <= set("-: ")
                or title.lower() in ("role", "position", "job title")):
            continue                     # header / separator rows
        joburl = _cell_url(cells[cols["url"]]) or _cell_url(cells[cols["title"]])
        if not joburl:
            continue
        company = _cell_text(cells[cols["company"]]) or prev_company
        prev_company = company
        jid = hashlib.md5(joburl.encode()).hexdigest()[:16]
        if jid in seen:
            continue
        seen.add(jid)
        job = {
            "id": jid,
            "title": title,
            "location": _cell_text(cells[cols["location"]]) if "location" in cols else "",
            "url": joburl,
            "company": company,
        }
        if "salary" in cols:
            sal = _cell_text(cells[cols["salary"]])
            if sal and sal != "-":
                job["salary"] = sal
        out.append(job)
    return out


def fetch_linkedin(cfg):
    """LinkedIn public guest search — same postings as linkedin.com/jobs, no
    login. cfg: queries (list) or query, location ('United States'), recency
    ('r86400' = posted in last 24h), pages (default 3, ~10 results/page).
    Datacenter IPs get rate-limited sometimes; failures are quiet by design."""
    queries = cfg.get("queries") or [cfg.get("query", "software engineer intern")]
    location = cfg.get("location", "United States")
    out, seen, errors = [], set(), []
    for q in queries:
        try:
            _linkedin_query(cfg, q, location, out, seen)
        except Exception as e:      # one rate-limited query shouldn't kill the rest
            errors.append(e)
    if not out and errors:
        raise errors[0]
    # Repost filter: job ids are chronological, so an id far below the batch's
    # newest while claiming to be <24h old is a repost/relist of a stale req.
    # ~1.2M ids/day globally; 4M ≈ 3 days of slack for genuinely new posts.
    if out:
        watermark = max(int(j["id"]) for j in out)
        drift = cfg.get("max_id_drift", 4_000_000)
        keep = [j for j in out if watermark - int(j["id"]) <= drift]
        if len(keep) < len(out):
            log(f"  linkedin({location}): dropped {len(out) - len(keep)} repost(s) by id drift")
        out = keep
    return out


def _linkedin_query(cfg, q, location, out, seen):
    start = 0
    for _ in range(cfg.get("pages", 3)):
        r = requests.get(
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
            params={"keywords": q, "location": location,
                    "f_TPR": cfg.get("recency", "r86400"), "start": start},
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=TIMEOUT)
        if r.status_code in (400, 404):         # past the end of results
            break
        r.raise_for_status()
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
            out.append({
                "id": jid,
                "title": html.unescape(mt.group(1)).strip(),
                "location": html.unescape(ml.group(1)).strip() if ml else "",
                "url": f"https://www.linkedin.com/jobs/view/{jid}",
                "company": html.unescape(mc.group(1)).strip() if mc else "",
            })
        if not got:
            break
        start += got
        time.sleep(1.0)                         # be polite, one IP


def fetch_janestreet(cfg):
    """Jane Street's own positions feed — no ATS, plain JSON. The
    availability field ('Full-Time: New Grad', 'Internship', ...) is folded
    into the title so intern/new-grad categorization and filters see it."""
    r = requests.get("https://www.janestreet.com/jobs/main.json",
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
    """Phenom People career sites (jobs.rbc.com etc). cfg: host, query,
    size (default 100), url ('https://host/xx/en/job/{id}' template)."""
    body = {"lang": "en_us", "deviceType": "desktop", "country": "us",
            "pageName": "search-results", "ddoKey": "refineSearch", "from": 0,
            "jobs": True, "counts": True, "size": cfg.get("size", 100),
            "clearAll": False, "jdsource": "facets", "siteType": "external",
            "keywords": cfg.get("query", ""), "global": True,
            "selected_fields": {}, "all_fields": ["category", "location"]}
    r = requests.post(f"https://{cfg['host']}/widgets", json=body,
                      headers={"User-Agent": UA, "Accept": "application/json",
                               "Content-Type": "application/json"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    url_tmpl = cfg.get("url", f"https://{cfg['host']}/en/job/{{id}}")
    out = []
    for j in r.json().get("refineSearch", {}).get("data", {}).get("jobs", []):
        jid = str(j.get("jobId") or j.get("reqId") or "")
        loc = (j.get("cityStateCountry") or j.get("cityState")
               or j.get("location") or j.get("city") or "")
        out.append({
            "id": jid,
            "title": j.get("title", ""),
            "location": loc if isinstance(loc, str) else "; ".join(loc[:3]),
            "url": j.get("applyUrl") or url_tmpl.format(id=jid),
        })
    return out


def fetch_eightfold(cfg):
    """Eightfold-powered career sites (Netflix etc). cfg: host
    ('explore.jobs.netflix.net'), domain ('netflix.com'), optional query."""
    out, start = [], 0
    while start < cfg.get("max", 100):      # API serves 10 per page
        params = {"domain": cfg["domain"], "start": start, "num": 10,
                  "sort_by": "timestamp"}
        if cfg.get("query"):
            params["query"] = cfg["query"]
        r = requests.get(f"https://{cfg['host']}/api/apply/v2/jobs", params=params,
                         headers={"User-Agent": UA, "Accept": "application/json"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        positions = data.get("positions", [])
        for p in positions:
            out.append({
                "id": str(p.get("id")),
                "title": p.get("name", ""),
                "location": p.get("location", "") or "",
                "url": p.get("canonicalPositionUrl")
                       or f"https://{cfg['host']}/careers/job/{p.get('id')}",
            })
        start += len(positions)
        if not positions or start >= data.get("count", 0):
            break
    return out


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
}


# ================================================================ storage
def db_open():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS jobs(
        board TEXT, job_id TEXT, title TEXT, location TEXT, url TEXT,
        first_seen TEXT, last_seen TEXT,
        PRIMARY KEY(board, job_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS boards(
        board TEXT PRIMARY KEY, seeded INTEGER DEFAULT 0,
        last_ok TEXT, failures INTEGER DEFAULT 0, last_poll REAL DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alerted(
        key TEXT PRIMARY KEY, ts TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sends(
        ts TEXT, subscriber TEXT, board TEXT, job_id TEXT, ok INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS recent(
        ts TEXT, company TEXT, title TEXT, url TEXT,
        location TEXT DEFAULT '', salary TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS pending(
        sub TEXT, tier TEXT, ts TEXT, company TEXT, title TEXT, url TEXT,
        location TEXT DEFAULT '', salary TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS kv(
        k TEXT PRIMARY KEY, v TEXT)""")
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
    state = {"boards": {}, "alerted": []}
    for board, seeded, last_poll, failures in con.execute(
            "SELECT board, seeded, last_poll, failures FROM boards"):
        ids = sorted(known_ids(con, board))
        state["boards"][board] = {"seeded": seeded, "ids": ids,
                                  "lp": int(last_poll or 0),
                                  "f": failures or 0}
    state["alerted"] = [r[0] for r in con.execute("SELECT key FROM alerted")]
    state["recent"] = [list(r) for r in con.execute(
        "SELECT ts, company, title, url, location, salary FROM recent")]
    state["pending"] = [list(r) for r in con.execute(
        "SELECT sub, tier, ts, company, title, url, location, salary FROM pending")]
    state["kv"] = dict(con.execute("SELECT k, v FROM kv"))
    tmp = STATE_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(STATE_JSON)


def import_state(con):
    state = json.loads(STATE_JSON.read_text())
    ts = now()
    for board, b in state.get("boards", {}).items():
        con.execute(
            "INSERT OR REPLACE INTO boards(board, seeded, last_poll, failures) "
            "VALUES (?,?,?,?)",
            (board, b.get("seeded", 1), b.get("lp", 0), b.get("f", 0)))
        con.executemany(
            "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?)",
            [(board, i, "", "", "", ts, ts) for i in b.get("ids", [])])
    con.executemany("INSERT OR IGNORE INTO alerted VALUES (?,?)",
                    [(k, ts) for k in state.get("alerted", [])])
    con.executemany("INSERT INTO recent VALUES (?,?,?,?,?,?)",
                    [tuple(r) + ("",) * (6 - len(r))
                     for r in state.get("recent", [])])
    con.executemany("INSERT INTO pending VALUES (?,?,?,?,?,?,?,?)",
                    [tuple(r) for r in state.get("pending", [])])
    con.executemany("INSERT OR REPLACE INTO kv VALUES (?,?)",
                    list(state.get("kv", {}).items()))
    con.commit()
    log(f"imported state.json: {len(state.get('boards', {}))} boards")


# ================================================================ dedupe
def canon_url(url):
    if not url:
        return ""
    p = urlsplit(url.strip().lower())
    host = p.netloc.replace("www.", "")
    path = p.path.rstrip("/")
    return f"{host}{path}"


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
    """LinkedIn hides direct apply URLs from guests, so a LinkedIn posting and
    its ATS twin never share a URL — company|title keys (exact + normalized
    company) do the cross-source matching instead."""
    ks = []
    cu = canon_url(job.get("url", ""))
    if cu:
        ks.append(f"url:{cu}")
    nk = name_key(job.get("company", ""), job.get("title", ""))
    if nk:
        ks.append(nk)
    nk2 = name_key(job.get("company", ""), job.get("title", ""), normalize=True)
    if nk2 and nk2 != nk:
        ks.append(nk2)
    return ks


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
            "ops": bool(s.get("ops")),
            "digest": bool(s.get("digest")),
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
                time.sleep(min(wait, 15))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            if attempt == SEND_RETRIES - 1:
                log(f"! send failed ({what}): {e}")
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


def send_discord_ping(webhook, jobs, kind, mention=""):
    """The apply-now tier: one rich embed PER JOB (clickable title, location,
    salary) so each ping is a self-contained application card. kind is
    'internship' or 'new grad role'. The header line is plain content, so
    pings are also findable via Discord search."""
    color = 0xF1C40F if kind == "internship" else 0xE67E22
    ts = datetime.now(timezone.utc).isoformat()
    n = len(jobs)
    header = f"🎯 **{n} watchlist {kind}{'s' if n != 1 else ''} just dropped**"
    if mention:
        header = f"{mention} {header}"
    embeds = []
    for b, j in jobs:
        company = j.get("company") or b.replace("_", " ")
        parts = []
        loc = _short_loc(j["location"])
        if loc:
            parts.append(f"📍 {loc}")
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
        ok &= _post_with_retry(
            lambda p=payload: requests.post(webhook, json=p, timeout=10),
            "discord")
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


def send_discord_feed(webhook, label, jobs, color, watch=None):
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
        ok &= _post_with_retry(
            lambda p=payload: requests.post(webhook, json=p, timeout=10),
            "discord")
    return ok


def send_discord_note(webhook, title, desc, color):
    """Single small embed for digests / ops notices."""
    payload = {"embeds": [{"title": title, "description": desc[:3900],
                           "color": color, "footer": {"text": "jobwatch"}}]}
    return _post_with_retry(
        lambda: requests.post(webhook, json=payload, timeout=10), "discord")


def send_telegram(chat_id, text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log("! telegram configured but TELEGRAM_BOT_TOKEN not set")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in _chunks(text, 3900):
        ok &= _post_with_retry(
            lambda c=chunk: requests.post(url, json={
                "chat_id": chat_id, "text": c, "disable_web_page_preview": True
            }, timeout=10),
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
    """The feeds (🛠️ intern / 💼 full-time) receive EVERY matching job — they
    are the complete archive, queued in `pending` and posted @silent.
    ANY job at a watchlist company ALSO fires an instant loud ping (rich card
    per job + optional mention): internships to the apply-now-intern channel,
    everything else to apply-now-full-time. Citadel drops anything → ping."""
    watch = sub.get("watch")
    tiers = {"ping_intern": [], "ping_ft": []}
    ts = now()
    for b, j in jobs:
        cat = _category(j["title"])
        # watchlist matches company only — titles like 'Salesforce Developer
        # Intern' at a consultancy must not count
        if watch and watch.search(j.get("company") or b):
            tiers["ping_intern" if cat == "intern" else "ping_ft"].append((b, j))
        con.execute(
            "INSERT INTO pending VALUES (?,?,?,?,?,?,?,?)",
            (sub["name"], "intern" if cat == "intern" else "ft", ts,
             j.get("company") or b.replace("_", " "), j["title"],
             j["url"], j.get("location") or "", j.get("salary") or ""))

    main = sub["discord"]
    ok = True
    if tiers["ping_intern"]:
        hook = sub["ping_hooks"].get("intern") or main
        if hook:
            ok &= send_discord_ping(hook, tiers["ping_intern"],
                                    "internship", mention=sub["mention"])
    if tiers["ping_ft"]:
        hook = sub["ping_hooks"].get("full_time") or main
        if hook:
            ok &= send_discord_ping(hook, tiers["ping_ft"],
                                    "role", mention=sub["mention"])
    hot_all = tiers["ping_intern"] + tiers["ping_ft"]
    if sub["telegram_chat"] and hot_all:
        ok &= send_telegram(sub["telegram_chat"],
                            "🎯 apply now\n\n" + _telegram_text(hot_all))
    for b, j in jobs:
        con.execute("INSERT INTO sends VALUES (?,?,?,?,?)",
                    (now(), sub["name"], b, j["id"], int(ok)))
    return ok


def flush_feeds(config, con, subs, force=False):
    """Send queued feed jobs as one quiet digest per stream once the oldest
    queued job is feed_flush_minutes old (or the queue is huge). Pings never
    wait — this only paces the browse-later material."""
    flush_min = config.get("feed_flush_minutes", 180)
    for sub in subs:
        rows = con.execute(
            "SELECT tier, ts, company, title, url, location, salary "
            "FROM pending WHERE sub=?", (sub["name"],)).fetchall()
        if not rows:
            continue
        oldest = min(r[1] for r in rows)
        age_min = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(oldest)).total_seconds() / 60
        if not force and age_min < flush_min and len(rows) < 80:
            continue
        by_tier = {"intern": [], "ft": []}
        seen = set()
        for tier, ts, company, title, url, location, salary in rows:
            key = (canon_url(url), company.casefold(), title.casefold())
            if key in seen:     # same job queued twice via near-identical rows
                continue
            seen.add(key)
            by_tier.setdefault(tier, []).append(
                ("", {"company": company, "title": title, "url": url,
                      "location": location, "salary": salary}))
        main = sub["discord"]
        watch = sub.get("watch")
        ok = True
        if by_tier["intern"]:
            hook = sub["feeds"].get("intern") or main
            if hook:
                ok &= send_discord_feed(hook, "🛠️ {n} new internship{s}",
                                        by_tier["intern"], 0x5865F2, watch)
        if by_tier["ft"]:
            hook = sub["feeds"].get("full_time") or main
            if hook:
                ok &= send_discord_feed(hook,
                                        "💼 {n} new-grad / full-time role{s}",
                                        by_tier["ft"], 0x95A5A6, watch)
        if sub["telegram_chat"]:
            everything = by_tier["intern"] + by_tier["ft"]
            ok &= send_telegram(sub["telegram_chat"], _telegram_text(everything))
        if ok:
            con.execute("DELETE FROM pending WHERE sub=?", (sub["name"],))
            log(f"  -> {sub['name']}: flushed {len(rows)} queued feed job(s)")


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
        "SELECT board FROM boards WHERE failures >= 10 ORDER BY board")]

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
def fetch_board(name, cfg):
    jobs = ADAPTERS[cfg["ats"]](cfg)
    for j in jobs:
        j.setdefault("company", name.replace("_", " "))
    return name, jobs


def cycle(config, con):
    t0 = time.time()
    all_boards = {k: v for k, v in config["boards"].items()
                  if not k.startswith("_") and not v.get("disabled")}
    subs = load_subscribers(config.get("filters", {}))
    new_by_board = {}
    broke = []
    ok_count = fail_count = skipped = 0

    # respect per-board min_interval (big feeds like the 11MB listings repos
    # only change every ~15 min; no point fetching them every 45s)
    due = {}
    for name, cfg in all_boards.items():
        _, _, last_poll = board_row(con, name)
        if time.time() - last_poll < cfg.get("min_interval", 0):
            skipped += 1
            continue
        due[name] = cfg

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_board, n, c): n for n, c in due.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            seeded, failures, _ = board_row(con, name)
            con.execute("UPDATE boards SET last_poll=? WHERE board=?",
                        (time.time(), name))
            try:
                _, jobs = fut.result()
            except Exception as e:
                failures += 1
                con.execute("UPDATE boards SET failures=? WHERE board=?", (failures, name))
                fail_count += 1
                if failures == 10:      # persisted streak → fires once per outage
                    broke.append((name, type(e).__name__))
                if failures in (1, 3, 10) or failures % 50 == 0:
                    log(f"! {name}: {type(e).__name__}: {e} (streak {failures})")
                continue

            ok_count += 1
            known = known_ids(con, name)

            if not jobs and len(known) >= ZERO_GUARD_MIN:
                log(f"! {name}: returned 0 jobs but {len(known)} known — ignoring read")
                continue

            fresh = [j for j in jobs if j["id"] and j["id"] not in known]
            ts = now()
            con.executemany(
                """INSERT INTO jobs VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(board, job_id) DO UPDATE SET last_seen=excluded.last_seen""",
                [(name, j["id"], j["title"], j["location"], j["url"], ts, ts)
                 for j in jobs])
            con.execute("UPDATE boards SET failures=0, last_ok=?, seeded=1 WHERE board=?",
                        (ts, name))

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
                     key=lambda kv: all_boards.get(kv[0], {}).get("ats") == "linkedin")
    for board, jobs in ordered:
        keep = []
        for j in jobs:
            total_new += 1
            if excl_co and excl_co.search(j.get("company") or ""):
                agencies += 1       # staffing-agency spam: drop, don't alert
                continue
            ks = dedupe_keys(j)
            if already_alerted(con, j) or any(k in seen_keys for k in ks):
                deduped += 1
                continue
            seen_keys.update(ks)
            keep.append(j)
        if keep:
            gated[board] = keep

    # ---- fanout
    delivered = 0
    alerted_jobs = set()
    for sub in subs:
        mine = []
        for board, jobs in gated.items():
            if "all" not in sub["companies"] and board not in sub["companies"]:
                continue
            mine.extend((board, j) for j in jobs if sub["filt"].ok(j))
        if mine:
            mine.sort(key=lambda t: (t[1].get("company", ""), t[1]["title"]))
            deliver(sub, mine, con)
            delivered += len(mine)
            alerted_jobs.update(id(j) for _, j in mine)
            log(f"  -> {sub['name']}: {len(mine)} job(s)")

    # mark every gated job as alerted (even if no subscriber matched, so a
    # later echo from another source can't ping something already evaluated)
    ts = now()
    for board, jobs in gated.items():
        for j in jobs:
            mark_alerted(con, j)
            con.execute("INSERT INTO recent VALUES (?,?,?,?,?,?)",
                        (ts, j.get("company") or board.replace("_", " "),
                         j["title"], j["url"], j.get("location") or "",
                         j.get("salary") or ""))

    flush_feeds(config, con, subs)

    if broke:
        detail = ", ".join(f"{n} ({err})" for n, err in broke)
        for sub in subs:
            if sub["ops"] and sub["discord"]:
                send_discord_note(
                    sub["discord"], "⚠️ jobwatch: board trouble",
                    f"{len(broke)} board(s) failing 10 polls in a row: {detail}\n"
                    f"Check the token / run `python watcher.py --verify`.",
                    0xE74C3C)

    maybe_digest(config, con, subs)

    con.execute("DELETE FROM jobs WHERE last_seen < datetime('now', ?)",
                (f"-{PRUNE_AFTER_DAYS} days",))
    con.execute("DELETE FROM alerted WHERE ts < datetime('now', ?)",
                (f"-{PRUNE_AFTER_DAYS} days",))
    con.execute("DELETE FROM recent WHERE ts < datetime('now', ?)",
                (f"-{RECENT_DAYS} days",))
    con.execute("DELETE FROM pending WHERE ts < datetime('now', '-7 days')")
    con.commit()

    log(f"cycle: {ok_count} ok / {fail_count} fail / {skipped} not-due | "
        f"{total_new} new, {deduped} cross-source dupes, {agencies} agency-spam, "
        f"{delivered} deliveries | {time.time()-t0:.1f}s")

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
    for name, b in config["boards"].items():
        if name.startswith("_"):
            continue
        if b.get("ats") not in ADAPTERS:
            print(f"✗ board {name}: unknown ats {b.get('ats')}")
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
                print(f"✓ {name:22} {len(jobs):>5} postings")
                good += 1
            except Exception as e:
                print(f"✗ {name:22} {type(e).__name__}: {str(e)[:90]}")
                bad += 1
    print(f"\n{good} working, {bad} broken. Fix tokens for broken boards "
          f"(careers page URL) or set \"disabled\": true.")


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
            "How this works, in one line: **anything from a watchlist "
            "company buzzes you; everything lands quietly in the feeds.**"
            "\n\nThe next messages demo it with fake jobs:\n\n"
            "**1. 🎯 apply-now intern (LOUD, gold cards)** — internships at "
            "watchlist companies → Stripe, Jane Street\n"
            "**2. 🎯 apply-now full-time (LOUD, orange cards)** — ANY other "
            "role at a watchlist company → Databricks (new grad), "
            "Anthropic (2026 start)\n"
            "**3. 🛠️ internships feed (@silent)** — EVERY internship incl. "
            "the apply-now ones (⭐ = watchlist company) → ⭐ Stripe, "
            "⭐ Jane Street, SomeCo\n"
            "**4. 💼 full-time feed (@silent)** — every other role, same "
            "idea → ⭐ Databricks, ⭐ Anthropic, RandomCorp\n"
            "\nThat's the whole system.",
            0x9B59B6)
    fake = [
        ("test", {"id": "t1", "company": "Stripe",
                  "title": "Software Engineer, Intern (Summer 2026)",
                  "location": "New York, NY", "salary": "$55 – $62/hr",
                  "url": "https://example.com/1"}),
        ("test", {"id": "t2", "company": "Jane Street",
                  "title": "Quantitative Trader — Summer Internship",
                  "location": "New York, NY", "url": "https://example.com/2"}),
        ("test", {"id": "t3", "company": "Databricks",
                  "title": "New Grad Software Engineer",
                  "location": "Toronto, ON", "salary": "$140K – $180K",
                  "url": "https://example.com/3"}),
        ("test", {"id": "t4", "company": "Anthropic",
                  "title": "Software Engineer, 2026 Start",
                  "location": "Remote — US", "url": "https://example.com/4"}),
        ("test", {"id": "t5", "company": "SomeCo",
                  "title": "Software Engineer Intern",
                  "location": "Chicago, IL", "url": "https://example.com/5"}),
        ("test", {"id": "t6", "company": "RandomCorp",
                  "title": "Machine Learning Engineer",
                  "location": "Seattle, WA", "url": "https://example.com/6"}),
    ]
    con = db_open()
    ok = deliver(sub, fake, con)
    flush_feeds(config, con, [sub], force=True)   # demo digests immediately
    print("sent ok" if ok else "send FAILED — check webhook/token")
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--list", metavar="BOARD")
    ap.add_argument("--test", metavar="SUBSCRIBER")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text())

    if args.check:
        cmd_check(config)
    if args.verify:
        cmd_verify(config)
        return
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
            log(f"! cycle crashed: {type(e).__name__}: {e}")
        time.sleep(max(5, interval) + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    main()
