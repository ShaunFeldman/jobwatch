"""Explainable notification preferences; no resume text or external model calls.

Scores represent interest, not verified eligibility. Title-only feeds cannot
establish experience requirements, graduation windows, or work authorization.
"""
import re


def matches(pattern, text):
    return bool(re.search(pattern, text or '', re.I))


TECH = r'\b(software|swe|sde|developer|backend|back.end|full.stack|front.end|distributed systems|database|infrastructure|platform engineer|site reliability|sre|devops|security engineer|systems engineer|research engineer|forward deployed engineer)\b'
DATA = r'\b(machine learning|ml engineer|ai engineer|artificial intelligence|data scien\w*|data engineer|applied scientist|research scientist|statistic\w*)\b'
QUANT = r'\b(quant\w*|trader|trading|systematic|portfolio research|investment research|strat(s|egist)?)\b'
FINANCE = r'\b(investment banking|investment|banking|financial|finance|capital markets|equity research|asset management|private equity|risk|analyst)\b'
ADJACENT = r'\b(deployment strategist|technical product|technical program|product analyst|business intelligence)\b'
EARLY = r'\b(intern(ship)?|co.op|new.?grad|graduate|early.career|entry.level|junior|university|campus|summer analyst|engineer i|analyst i)\b'
SENIOR = r'\b(senior|sr\.?|staff|principal|lead|director|vice president|vp|head of|distinguished|[ivx]{2,}|l[5-9]|level [5-9])\b'
UNRELATED = r'\b(recruit\w*|human resources|hr|payroll|legal|counsel|nurse|warehouse|retail|teller|customer service|administrative|executive assistant|marketing|sales|account executive|animation|animator|designer)\b'
NY_SF = r'\b(new york|nyc|manhattan|brooklyn|jersey city|san francisco|sf|bay area|palo alto|menlo park|mountain view|sunnyvale|redwood city|san jose)\b'
US = r'\b(united states|us|usa|u\.s\.?a?\.?|seattle|bellevue|redmond|chicago|boston|austin|dallas|houston|los angeles|los gatos|washington|denver|atlanta|miami|san diego|philadelphia|pittsburgh|portland|raleigh|durham|charlotte|arlington|norfolk|laurel|fayetteville|honolulu|boulder|columbus|detroit|minneapolis|nashville|salt lake city|phoenix)\b'
STATES = r'(?:,\s*|^)(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)(?:\b|$)'
CANADA = r'\b(canada|toronto|waterloo|vancouver|montreal|ottawa|calgary|edmonton|ontario|quebec|british columbia)\b'


def geography(location):
    if matches(NY_SF, location):
        return 'nyc_sf', 25
    if matches(US, location) or re.search(STATES, location or ''):
        return 'us', 18
    if matches(CANADA, location):
        return 'canada', 8
    # Remote is not a pass for London/Singapore/etc. Only unqualified/global
    # remote or explicitly US/Canada remote locations are accepted.
    if not location or matches(r'^\s*(remote|hybrid|multiple locations|various locations|locations? \d+|\d+ locations?)\s*$', location):
        return 'unknown', 3
    if matches(r'\b(worldwide|anywhere|global remote)\b', location):
        return 'remote', 8
    return 'other', 0


def assess(job, policy, watch=None):
    title = job.get('title', '')
    company = job.get('company', '')
    region, location_points = geography(job.get('location', ''))
    early = matches(EARLY, title)
    role = ('data_ml' if matches(DATA, title) else 'software' if matches(TECH, title)
            else 'adjacent' if matches(ADJACENT, title) else 'quant' if matches(QUANT, title)
            else 'finance' if matches(FINANCE, title) else 'other')
    reasons = []
    # Prestige and location never rescue a clearly unsuitable role or seniority.
    if matches(SENIOR, title) or (matches(r'\bmanager\b', title) and not early):
        return {'feed': False, 'ping': False, 'score': 0, 'reasons': ['senior/management title'], 'role': role}
    if matches(UNRELATED, title) or matches(r'\b(ph\.?d|doctoral|postdoc)\b', title):
        return {'feed': False, 'ping': False, 'score': 0, 'reasons': ['outside target roles/education'], 'role': role}
    if region == 'other' or role == 'other':
        return {'feed': False, 'ping': False, 'score': 0, 'reasons': ['outside target locations or roles'], 'role': role}
    priority = matches(policy.get('priority_companies', r'(?!)'), company)
    selective = matches(policy.get('selective_companies', r'(?!)'), company)
    company_points = -15 if selective else 30 if priority else 12 if watch and watch.search(company) else 0
    role_points = {'software': 50, 'data_ml': 50, 'quant': 50, 'finance': 30, 'adjacent': 30}[role]
    score = role_points + company_points + location_points + (15 if early else 0)
    reasons.append({'software': 'software/backend fit', 'data_ml': 'ML/data fit', 'quant': 'quant/investing fit',
                    'finance': 'finance opportunity', 'adjacent': 'technical/business crossover'}[role])
    reasons.append('selective bank preference' if selective else 'priority company' if priority else 'watchlist company' if company_points else 'company neutral')
    reasons.append({'nyc_sf': 'NYC/SF preferred', 'us': 'U.S. preferred', 'canada': 'Canada',
                    'unknown': 'location unconfirmed', 'remote': 'global remote'}[region])
    if early:
        reasons.append('early-career title')
    ping = score >= policy.get('threshold', 75)
    # Routine finance at lower-priority banks stays quiet even in NYC.
    if selective and role in {'finance', 'adjacent'}:
        ping = False
    return {'feed': True, 'ping': ping, 'score': score, 'reasons': reasons, 'role': role}
