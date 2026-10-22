"""Bounded, best-effort employer-link verification. No model/API costs."""
import html
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import requests


CACHE = {}
ATS_HOSTS = ('greenhouse.io', 'lever.co', 'ashbyhq.com', 'myworkdayjobs.com',
             'icims.com', 'smartrecruiters.com', 'jobs.workable.com')


def linkedin(url):
    host = (urlsplit(url).hostname or '').lower()
    return host == 'linkedin.com' or host.endswith('.linkedin.com')


def _get(url):
    # Employer links are untrusted external input, including redirect targets.
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username:
            raise ValueError('unsupported application URL')
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('non-public application URL')
        response = requests.get(url, timeout=3, allow_redirects=False,
                                headers={'User-Agent': 'Mozilla/5.0'})
        if response.is_redirect:
            url = urljoin(url, response.headers['Location'])
            continue
        response.raise_for_status()
        return url, response.text[:2_000_000]
    raise ValueError('too many redirects')


def employer_link(text):
    # Only use the explicitly marked external application link, not arbitrary
    # links to advertisers, employer homepages, or related jobs.
    for tag in re.findall(r'<a\b[^>]*>', text, re.I):
        if not re.search(r'(?:apply-link|apply-button)', tag, re.I):
            continue
        match = re.search(r'href=["\']([^"\']+)', tag, re.I)
        if not match:
            continue
        url = html.unescape(match[1])
        if linkedin(url):
            url = parse_qs(urlsplit(url).query).get('url', [''])[0]
        if url.startswith('https://') and not linkedin(url):
            return url
    return None


def _normalized(value):
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def official_date(text, url, job):
    host = (urlsplit(url).hostname or '').lower()
    # Aggregators never establish original publication, even if a scraped
    # company name accidentally matches the aggregator's own domain.
    if linkedin(url) or host == 'jobright.ai' or host.endswith('.jobright.ai'):
        return None
    company = _normalized(re.sub(r'\b(inc|corp|corporation|llc|ltd)\b', '',
                                job.get('company', ''), flags=re.I))
    employer_host = len(company) >= 4 and company in host.split('.')[:-1]
    if not employer_host and not any(host == h or host.endswith('.' + h) for h in ATS_HOSTS):
        return None
    found = []
    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            kind = node.get('@type', [])
            if 'JobPosting' in (kind if isinstance(kind, list) else [kind]):
                if _normalized(node.get('title')) == _normalized(job.get('title')):
                    found.append(node)
            for key in ('@graph', 'mainEntity'):
                visit(node.get(key))
    for raw in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.I | re.S):
        try:
            visit(json.loads(raw))
        except (ValueError, TypeError):
            pass
    # Ambiguous multi-job pages cannot prove the date of this opening.
    return found[0].get('datePosted') if len(found) == 1 else None


def enrich(jobs, max_requests=4, budget_seconds=15):
    deadline = time.monotonic() + budget_seconds
    attempted = 0
    for job in sorted(jobs, key=lambda j: j['url'] in CACHE):
        source_url = job['url']
        cached = CACHE.get(source_url)
        if not cached or cached.get('retry_at', 0) <= time.time():
            if attempted >= max_requests or time.monotonic() >= deadline:
                if cached and cached.get('url'):
                    job['_verification'] = dict(cached, source_url=source_url)
                    job['_discovery_url'] = source_url
                    job['url'] = cached['url']
                    job['posted'] = cached.get('posted')
                    job['_official_posted'] = bool(cached.get('posted'))
                continue
            attempted += 1
            cached = {'retry_at': time.time() + 3600}
            try:
                target = source_url
                if linkedin(source_url):
                    _, detail = _get('https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/' + job['id'])
                    target = employer_link(detail)
                if target:
                    url, text = _get(target)
                    if not linkedin(url):
                        cached.update(url=url, posted=official_date(text, url, job))
                        if cached['posted']:
                            cached['retry_at'] = time.time() + 86400
            except (requests.RequestException, ValueError, OSError, KeyError):
                pass  # Verification failures retain the discovery, without a ping.
            CACHE[source_url] = cached
        job['_verification'] = dict(cached, source_url=source_url)
        if cached.get('url'):
            job['_discovery_url'] = source_url
            job['url'] = cached['url']
            job['_source_posted'] = job.get('posted')
            job['posted'] = cached.get('posted')
            job['_official_posted'] = bool(cached.get('posted'))
