#!/usr/bin/env python3
"""
jobwatch v4 — multi-user, multi-source job posting watcher.

Sources: Greenhouse / Lever / Ashby / SmartRecruiters / Workday / Eightfold
boards, amazon.jobs, Microsoft careers, LinkedIn guest search, community
GitHub listing repos (SimplifyJobs / vanshb03 / cvrve JSON, jobright-ai
markdown). Cross-source dedupe so the same job never pings twice.
Discord delivery uses embeds grouped by company (no link-preview spam).

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
PRUNE_AFTER_DAYS = 90
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
    url = f"https://api.ashbyhq.com/posting-api/job-board/{cfg['token']}?includeCompensation=false"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j.get("id")),
        "title": j.get("title", ""),
        "location": j.get("location", "") or "",
        "url": j.get("jobUrl", ""),
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


def fetch_jobright(cfg):
    """jobright-ai README repos — LinkedIn-sourced listings refreshed daily,
    published as a markdown table. cfg: repo ('jobright-ai/2026-Software-
    Engineer-Internship'), branch (default 'master'), path (default 'README.md').
    Row shape: | **[Company](site)** | **[Title](jobright.ai/jobs/info/ID?utm)** | Location | ..."""
    repo = cfg["repo"]
    branch = cfg.get("branch", "master")
    path = cfg.get("path", "README.md")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    link = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    out, prev_company = [], ""
    for row in r.text.splitlines():
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Company", "-----") or set(cells[0]) <= {"-", " "}:
            continue
        mt = link.search(cells[1])
        if not mt:
            continue
        mc = link.search(cells[0])
        company = mc.group(1) if mc else cells[0].strip("*↳ ").strip()
        company = company or prev_company
        prev_company = company
        joburl = mt.group(2).split("?")[0]
        jid = (joburl.rsplit("/info/", 1)[-1] if "/info/" in joburl
               else hashlib.md5(joburl.encode()).hexdigest()[:16])
        out.append({
            "id": jid,
            "title": mt.group(1).strip("* "),
            "location": cells[2],
            "url": joburl,
            "company": company,
        })
    return out


def fetch_linkedin(cfg):
    """LinkedIn public guest search — same postings as linkedin.com/jobs, no
    login. cfg: queries (list) or query, location ('United States'), recency
    ('r86400' = posted in last 24h), pages (default 3, ~10 results/page).
    Datacenter IPs get rate-limited sometimes; failures are quiet by design."""
    queries = cfg.get("queries") or [cfg.get("query", "software engineer intern")]
    location = cfg.get("location", "United States")
    out, seen = [], set()
    for q in queries:
        start = 0
        for _ in range(cfg.get("pages", 3)):
            r = requests.get(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params={"keywords": q, "location": location,
                        "f_TPR": cfg.get("recency", "r86400"), "start": start},
                headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                timeout=TIMEOUT)
            if r.status_code in (400, 404):     # past the end of results
                break
            r.raise_for_status()
            cards = r.text.split("<li")[1:]
            got = 0
            for card in cards:
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
            time.sleep(1.0)                     # be polite, one IP
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
    "jobright": fetch_jobright,
    "linkedin": fetch_linkedin,
    "eightfold": fetch_eightfold,
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
    for board, seeded, last_poll in con.execute(
            "SELECT board, seeded, last_poll FROM boards"):
        ids = sorted(known_ids(con, board))
        state["boards"][board] = {"seeded": seeded, "ids": ids,
                                  "lp": int(last_poll or 0)}
    state["alerted"] = [r[0] for r in con.execute("SELECT key FROM alerted")]
    tmp = STATE_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(STATE_JSON)


def import_state(con):
    state = json.loads(STATE_JSON.read_text())
    ts = now()
    for board, b in state.get("boards", {}).items():
        con.execute("INSERT OR REPLACE INTO boards(board, seeded, last_poll) "
                    "VALUES (?,?,?)", (board, b.get("seeded", 1), b.get("lp", 0)))
        con.executemany(
            "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?)",
            [(board, i, "", "", "", ts, ts) for i in b.get("ids", [])])
    con.executemany("INSERT OR IGNORE INTO alerted VALUES (?,?)",
                    [(k, ts) for k in state.get("alerted", [])])
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


def name_key(company, title):
    c = re.sub(r"[^a-z0-9]+", "", (company or "").lower())
    t = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    return f"nk:{c}|{t}" if c and t else ""


def dedupe_keys(job):
    ks = []
    cu = canon_url(job.get("url", ""))
    if cu:
        ks.append(f"url:{cu}")
    nk = name_key(job.get("company", ""), job.get("title", ""))
    if nk:
        ks.append(nk)
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
            "discord": s.get("discord_webhook", ""),
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


# ---- rendering: group by company, tag by role type ----
TAG_INTERN = re.compile(r"\bintern(ship)?\b", re.I)
TAG_GRAD = re.compile(r"new ?grad|graduate|early career|university|campus|entry.level|junior", re.I)


def _tag(title):
    if TAG_INTERN.search(title):
        return "🛠️"
    if TAG_GRAD.search(title):
        return "🎓"
    return "💼"


def _group(jobs):
    """[(board, job)] -> [(company, [job])], companies A-Z, titles A-Z."""
    groups = {}
    for b, j in jobs:
        groups.setdefault(j.get("company") or b.replace("_", " "), []).append(j)
    return sorted(((c, sorted(js, key=lambda x: x["title"]))
                   for c, js in groups.items()), key=lambda kv: kv[0].lower())


def _discord_blocks(jobs):
    blocks = []
    for company, js in _group(jobs):
        lines = [f"**{company}**"]
        for j in js:
            title = j["title"].replace("[", "(").replace("]", ")").strip()
            if len(title) > 100:
                title = title[:97] + "…"
            loc = (j["location"] or "").strip()
            if len(loc) > 45:
                loc = loc[:42] + "…"
            line = f"{_tag(j['title'])} [{title}]({j['url']})"
            if loc:
                line += f" · {loc}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return blocks


def _pack(blocks, limit):
    """Pack company blocks into strings of at most `limit` chars."""
    out, cur = [], ""
    for b in blocks:
        while len(b) > limit:           # a single huge company block
            cut = b.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            out.append(b[:cut])
            b = b[cut:].lstrip("\n")
        if cur and len(cur) + len(b) + 2 > limit:
            out.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


def send_discord(webhook, jobs):
    """One embed per message: markdown job links grouped under bold company
    names. Embeds don't unfurl, so no link-preview spam."""
    descs = _pack(_discord_blocks(jobs), 3500)   # embed desc limit is 4096
    ts = datetime.now(timezone.utc).isoformat()
    n = len(jobs)
    ok = True
    for i, desc in enumerate(descs):
        title = f"🆕 {n} new job{'s' if n != 1 else ''}"
        if len(descs) > 1:
            title += f"  ·  {i + 1}/{len(descs)}"
        payload = {"embeds": [{"title": title, "description": desc,
                               "color": 0x5865F2, "timestamp": ts,
                               "footer": {"text": "jobwatch"}}]}
        ok &= _post_with_retry(
            lambda p=payload: requests.post(webhook, json=p, timeout=10),
            "discord")
    return ok


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
            loc = (j["location"] or "").strip()
            lines.append(f"{_tag(j['title'])} {j['title']}"
                         + (f" · {loc}" if loc else ""))
            lines.append(f"   {j['url']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def deliver(sub, jobs, con):
    ok = True
    if sub["discord"]:
        ok &= send_discord(sub["discord"], jobs)
    if sub["telegram_chat"]:
        ok &= send_telegram(sub["telegram_chat"], _telegram_text(jobs))
    for b, j in jobs:
        con.execute("INSERT INTO sends VALUES (?,?,?,?,?)",
                    (now(), sub["name"], b, j["id"], int(ok)))
    return ok


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
    total_new = deduped = 0
    gated = {}
    for board, jobs in new_by_board.items():
        keep = []
        for j in jobs:
            total_new += 1
            if already_alerted(con, j):
                deduped += 1
                continue
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
    for board, jobs in gated.items():
        for j in jobs:
            mark_alerted(con, j)

    con.execute("DELETE FROM jobs WHERE last_seen < datetime('now', ?)",
                (f"-{PRUNE_AFTER_DAYS} days",))
    con.execute("DELETE FROM alerted WHERE ts < datetime('now', ?)",
                (f"-{PRUNE_AFTER_DAYS} days",))
    con.commit()

    log(f"cycle: {ok_count} ok / {fail_count} fail / {skipped} not-due | "
        f"{total_new} new, {deduped} cross-source dupes, {delivered} deliveries | "
        f"{time.time()-t0:.1f}s")

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
    fake = [
        ("test", {"id": "t0", "company": "jobwatch",
                  "title": "test message — delivery works, sample layout below",
                  "location": "everywhere", "url": "https://example.com"}),
        ("test", {"id": "t1", "company": "Stripe",
                  "title": "Software Engineer, Intern (Summer 2026)",
                  "location": "New York, NY", "url": "https://example.com/1"}),
        ("test", {"id": "t2", "company": "Stripe",
                  "title": "Software Engineer, New Grad",
                  "location": "Toronto, ON", "url": "https://example.com/2"}),
        ("test", {"id": "t3", "company": "Anthropic",
                  "title": "Software Engineer, 2026 Start",
                  "location": "Remote — US", "url": "https://example.com/3"}),
        ("test", {"id": "t4", "company": "Jane Street",
                  "title": "Quantitative Trader — Summer Internship",
                  "location": "New York, NY", "url": "https://example.com/4"}),
    ]
    con = db_open()
    print("sent ok" if deliver(sub, fake, con) else "send FAILED — check webhook/token")
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
