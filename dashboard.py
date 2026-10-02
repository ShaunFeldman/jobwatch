#!/usr/bin/env python3
"""Local, zero-dependency web command center for Jobwatch."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import threading
import webbrowser
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import watcher
from job_intelligence import (
    analyze_job,
    board_tags,
    merged_profile,
    stable_job_key,
)


HERE = Path(__file__).parent
WEB_DIR = HERE / "web"
ALLOWED_STATUSES = {
    "saved", "applied", "interview", "offer", "rejected", "archived"
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


class JobwatchService:
    def __init__(self, db_path=watcher.DB_PATH, config_path=watcher.CONFIG,
                 subscribers_path=watcher.SUBSCRIBERS):
        self.db_path = Path(db_path) if db_path != ":memory:" else db_path
        self.config_path = Path(config_path)
        self.subscribers_path = Path(subscribers_path)
        self.config = json.loads(self.config_path.read_text())
        self.profile = merged_profile(self.config)
        self.tags = board_tags(self.config)
        self.preferred = self._preferred_pattern()
        self._jobs_cache = None
        self._cache_token = None
        self._cache_lock = threading.Lock()
        con = watcher.db_open(self.db_path)
        con.commit()
        con.close()

    def _preferred_pattern(self):
        try:
            raw = json.loads(self.subscribers_path.read_text())
            for name, sub in raw.items():
                if not name.startswith("_") and not sub.get("mute") and sub.get("watchlist"):
                    return re.compile(sub["watchlist"], re.I)
        except (OSError, ValueError, re.error):
            pass
        return None

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA busy_timeout=5000")
        con.row_factory = sqlite3.Row
        return con

    @contextmanager
    def db(self):
        con = self.connect()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _all_jobs(self) -> list[dict]:
        # Ranking tens of thousands of rows is cheap once and wasteful on
        # every keystroke. Invalidate when polling or application state moves.
        with self._cache_lock:
            with self.db() as con:
                job_token = con.execute(
                    "SELECT COUNT(*), COALESCE(MAX(last_seen), '') FROM jobs"
                ).fetchone()
                app_token = con.execute(
                    "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM applications"
                ).fetchone()
                token = tuple(job_token) + tuple(app_token)
                if self._jobs_cache is not None and token == self._cache_token:
                    return self._jobs_cache
                rows = con.execute("""
                    SELECT j.board, j.job_id, j.title, j.location, j.url,
                           j.first_seen, j.last_seen,
                           COALESCE(d.company, REPLACE(j.board, '_', ' ')) company,
                           COALESCE(d.salary, '') salary, d.posted,
                           a.status, a.priority, a.notes, a.next_step, a.due_date,
                           a.created_at application_created_at,
                           a.updated_at application_updated_at
                    FROM jobs j
                    LEFT JOIN job_details d
                      ON d.board=j.board AND d.job_id=j.job_id
                    LEFT JOIN applications a
                      ON a.job_key=(j.board || ':' || j.job_id)
                    WHERE j.title != ''
                    ORDER BY COALESCE(d.posted, 0) DESC, j.first_seen DESC
                """).fetchall()

            result = []
            seen = set()
            for row in rows:
                job = dict(row)
                # Keep one card when an aggregator and a direct board mirror a job.
                dedupe = watcher.canon_url(job["url"]) or (
                    f"{job['company'].casefold()}|{job['title'].casefold()}"
                )
                if dedupe in seen and not job.get("status"):
                    continue
                seen.add(dedupe)
                preferred = bool(self.preferred and self.preferred.search(job["company"]))
                job.update(analyze_job(
                    job, self.config, self.tags.get(job["board"], set()), preferred
                ))
                job["key"] = stable_job_key(job)
                job["status"] = job.get("status") or "untracked"
                job["priority"] = job.get("priority") or 0
                result.append(job)
            self._jobs_cache = result
            self._cache_token = token
            return result

    @staticmethod
    def _matches(job: dict, params: dict[str, list[str]]) -> bool:
        def one(name, default=""):
            return (params.get(name) or [default])[0]

        query = one("q").strip().casefold()
        if query:
            haystack = " ".join(str(job.get(k) or "") for k in (
                "company", "title", "location", "salary", "board"
            )).casefold()
            if not all(term in haystack for term in query.split()):
                return False
        if one("track") and job["track"] != one("track"):
            return False
        if one("seniority") and job["seniority"] != one("seniority"):
            return False
        if one("location") and job["location_bucket"] != one("location"):
            return False
        if one("company_type") and one("company_type") not in job["company_tags"]:
            return False
        if one("status") and job["status"] != one("status"):
            return False
        if one("min_score"):
            try:
                if job["score"] < int(one("min_score")):
                    return False
            except ValueError:
                pass
        if _bool(one("fresh")):
            epoch = watcher._posted_epoch(job.get("posted"))
            if not epoch or (datetime.now(timezone.utc).timestamp() - epoch) > 86400:
                return False
        return True

    def jobs(self, params: dict[str, list[str]]) -> dict:
        all_jobs = self._all_jobs()
        filtered = [j for j in all_jobs if self._matches(j, params)]
        sort = (params.get("sort") or ["score"])[0]
        if sort == "newest":
            filtered.sort(key=lambda j: (
                watcher._posted_epoch(j.get("posted"))
                or watcher._iso_epoch(j.get("first_seen")) or 0,
                j["score"],
            ), reverse=True)
        elif sort == "company":
            filtered.sort(key=lambda j: (j["company"].casefold(), -j["score"]))
        else:
            filtered.sort(key=lambda j: (j["score"], j.get("posted") or 0), reverse=True)

        try:
            page = max(1, int((params.get("page") or [1])[0]))
            per_page = min(100, max(1, int((params.get("per_page") or [30])[0])))
        except ValueError:
            page, per_page = 1, 30
        start = (page - 1) * per_page
        return {
            "jobs": filtered[start:start + per_page],
            "total": len(filtered),
            "page": page,
            "per_page": per_page,
            "pages": max(1, (len(filtered) + per_page - 1) // per_page),
            "facets": {
                "tracks": Counter(j["track"] for j in filtered),
                "locations": Counter(j["location_bucket"] for j in filtered),
                "seniority": Counter(j["seniority"] for j in filtered),
            },
        }

    def overview(self) -> dict:
        jobs = self._all_jobs()
        fresh = 0
        now_epoch = datetime.now(timezone.utc).timestamp()
        for job in jobs:
            epoch = watcher._posted_epoch(job.get("posted"))
            fresh += bool(epoch and now_epoch - epoch <= 86400)
        with self.db() as con:
            source_rows = con.execute(
                "SELECT board, seeded, last_ok, failures, last_poll FROM boards"
            ).fetchall()
            last_run = con.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            pipeline = Counter(dict(con.execute(
                "SELECT status, COUNT(*) FROM applications GROUP BY status"
            ).fetchall()))
        enabled = sum(
            1 for name, cfg in self.config.get("boards", {}).items()
            if not name.startswith("_") and not cfg.get("disabled")
        )
        healthy = sum(1 for row in source_rows if row["seeded"] and not row["failures"])
        seeded = sum(1 for row in source_rows if row["seeded"])
        recommendations = sorted(
            (j for j in jobs if j["status"] not in {"rejected", "archived"}),
            key=lambda j: (j["score"], j.get("posted") or 0), reverse=True,
        )[:5]
        return {
            "profile": self.profile,
            "stats": {
                "searchable_jobs": len(jobs),
                "fresh_24h": fresh,
                "high_fit": sum(j["score"] >= 80 for j in jobs),
                "active_applications": sum(
                    pipeline[s] for s in ("saved", "applied", "interview", "offer")
                ),
                "enabled_sources": enabled,
                "healthy_sources": healthy,
                "checked_sources": seeded,
            },
            "pipeline": pipeline,
            "recommendations": recommendations,
            "last_run": dict(last_run) if last_run else None,
            "generated_at": _utcnow(),
        }

    def sources(self) -> dict:
        with self.db() as con:
            rows = {r["board"]: dict(r) for r in con.execute(
                "SELECT board, seeded, last_ok, failures, last_poll FROM boards"
            )}
        sources = []
        for name, cfg in self.config.get("boards", {}).items():
            if name.startswith("_"):
                continue
            state = rows.get(name, {})
            sources.append({
                "name": name,
                "label": name.replace("_", " ").title(),
                "adapter": cfg.get("ats"),
                "disabled": bool(cfg.get("disabled")),
                "tags": sorted(self.tags.get(name, set())),
                "seeded": bool(state.get("seeded")),
                "last_ok": state.get("last_ok"),
                "failures": state.get("failures", 0),
                "last_poll": state.get("last_poll", 0),
            })
        sources.sort(key=lambda s: (s["disabled"], -s["failures"], s["label"]))
        return {"sources": sources}

    def save_application(self, payload: dict) -> dict:
        status = str(payload.get("status") or "saved")
        if status not in ALLOWED_STATUSES:
            raise ValueError("invalid application status")
        job = payload.get("job") or payload
        key = str(job.get("key") or stable_job_key(job))
        if not job.get("title") or not key or key == "unknown:":
            raise ValueError("job title and key are required")
        ts = _utcnow()
        try:
            priority = min(3, max(0, int(payload.get("priority", job.get("priority", 0)))))
        except (TypeError, ValueError):
            priority = 0
        values = (
            key, job.get("board", ""), str(job.get("job_id") or job.get("id") or ""),
            job.get("company", ""), job.get("title", ""), job.get("location", ""),
            job.get("url", ""), job.get("salary", ""), job.get("posted"), status,
            priority, str(payload.get("notes", job.get("notes", "")))[:10000],
            str(payload.get("next_step", job.get("next_step", "")))[:500],
            str(payload.get("due_date", job.get("due_date", "")))[:30], ts, ts,
        )
        with self.db() as con:
            con.execute("""
                INSERT INTO applications
                  (job_key, board, job_id, company, title, location, url, salary,
                   posted, status, priority, notes, next_step, due_date,
                   created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_key) DO UPDATE SET
                  company=excluded.company, title=excluded.title,
                  location=excluded.location, url=excluded.url,
                  salary=excluded.salary, posted=excluded.posted,
                  status=excluded.status, priority=excluded.priority,
                  notes=excluded.notes, next_step=excluded.next_step,
                  due_date=excluded.due_date, updated_at=excluded.updated_at
            """, values)
        self._cache_token = None
        return {"ok": True, "key": key, "status": status, "updated_at": ts}

    def delete_application(self, key: str) -> dict:
        with self.db() as con:
            con.execute("DELETE FROM applications WHERE job_key=?", (key,))
        self._cache_token = None
        return {"ok": True, "key": key}

    def pipeline(self) -> dict:
        with self.db() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM applications ORDER BY priority DESC, updated_at DESC"
            )]
        grouped = {status: [] for status in ALLOWED_STATUSES}
        for job in rows:
            job["key"] = job.pop("job_key")
            job.update(analyze_job(
                job, self.config, self.tags.get(job.get("board"), set()),
                bool(self.preferred and self.preferred.search(job.get("company") or "")),
            ))
            grouped[job["status"]].append(job)
        return {"pipeline": grouped, "total": len(rows)}

    def csv_export(self, params: dict[str, list[str]]) -> str:
        params = dict(params)
        params["per_page"] = ["100"]
        first = self.jobs(params)
        jobs = first["jobs"]
        for page in range(2, first["pages"] + 1):
            params["page"] = [str(page)]
            jobs.extend(self.jobs(params)["jobs"])
        stream = io.StringIO()
        fields = ["company", "title", "location", "track_label", "seniority",
                  "score", "salary", "status", "url", "first_seen"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
        return stream.getvalue()


class JobwatchHandler(BaseHTTPRequestHandler):
    service: JobwatchService

    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} {fmt % args}")

    def _send(self, body: bytes, content_type="application/json; charset=utf-8",
              status=HTTPStatus.OK, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=HTTPStatus.OK):
        self._send(json.dumps(value, default=str).encode(), status=status)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/overview":
                return self.json(self.service.overview())
            if parsed.path == "/api/jobs":
                return self.json(self.service.jobs(params))
            if parsed.path == "/api/pipeline":
                return self.json(self.service.pipeline())
            if parsed.path == "/api/sources":
                return self.json(self.service.sources())
            if parsed.path == "/api/export.csv":
                body = self.service.csv_export(params).encode()
                return self._send(body, "text/csv; charset=utf-8", extra_headers={
                    "Content-Disposition": "attachment; filename=jobwatch-jobs.csv"
                })
            return self._static(parsed.path)
        except Exception as exc:
            return self.json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        if urlparse(self.path).path != "/api/applications":
            return self.json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("request too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            return self.json(self.service.save_application(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            return self.json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/applications":
            return self.json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        key = (parse_qs(parsed.query).get("key") or [""])[0]
        if not key:
            return self.json({"error": "key is required"}, HTTPStatus.BAD_REQUEST)
        return self.json(self.service.delete_application(key))

    def _static(self, path):
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        if path not in files:
            return self.json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        filename, content_type = files[path]
        self._send((WEB_DIR / filename).read_bytes(), content_type)


def serve(host="127.0.0.1", port=8787, open_browser=True, db_path=watcher.DB_PATH,
          config_path=watcher.CONFIG, subscribers_path=watcher.SUBSCRIBERS):
    service = JobwatchService(db_path, config_path, subscribers_path)
    handler = type("ConfiguredJobwatchHandler", (JobwatchHandler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}"
    print(f"Jobwatch command center → {url}")
    print("Your application notes stay in the local SQLite database.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("Warning: this dashboard has no authentication; use a loopback host by default.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Jobwatch local command center")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db", default=str(watcher.DB_PATH))
    args = parser.parse_args()
    serve(args.host, args.port, not args.no_browser, args.db)


if __name__ == "__main__":
    main()
