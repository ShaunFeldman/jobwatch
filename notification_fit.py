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
# Broader business roles can be worthwhile at an exceptional employer. These
# are not automatic rejects merely because they are outside software/quant.
BUSINESS = r'\b(product|program|project|operations|strategy|business|recruit\w*|human resources|hr|marketing|sales|account executive|customer success|client services|communications|commercial)\b'
SPECIALIST = r'\b(physician|nurse|surgeon|lawyer|attorney|legal counsel|electrician|plumber|mechanic|warehouse|truck driver|animator|animation|graphic designer)\b'

NY_SF = r'\b(new york|nyc|manhattan|brooklyn|jersey city|san francisco|sf|bay area|palo alto|menlo park|mountain view|sunnyvale|redwood city|san jose|los gatos|san mateo|cupertino|oakland|berkeley)\b'
US = r'\b(united states|california|us|usa|u\.s\.?a?\.?|seattle|bellevue|redmond|chicago|boston|austin|dallas|houston|los angeles|los gatos|washington|denver|atlanta|miami|san diego|philadelphia|pittsburgh|portland|raleigh|durham|charlotte|arlington|norfolk|laurel|fayetteville|honolulu|boulder|columbus|detroit|minneapolis|nashville|salt lake city|phoenix)\b'
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
            else 'finance' if matches(FINANCE, title) else 'business' if matches(BUSINESS, title) else 'other')
    reasons = []
    # Prestige and location never rescue a clearly unsuitable role or seniority.
    if matches(SENIOR, title) or (matches(r'\bmanager\b', title) and not early):
        return {'feed': False, 'ping': False, 'score': 0, 'reasons': ['senior/management title'], 'role': role}
    if matches(SPECIALIST, title) or matches(r'\b(ph\.?d|doctoral|postdoc)\b', title):
        return {'feed': False, 'ping': False, 'score': 0, 'reasons': ['outside target roles/education'], 'role': role}
    priority = matches(policy.get('priority_companies', r'(?!)'), company)
    selective = matches(policy.get('selective_companies', r'(?!)'), company)
    if region == 'other' or (role == 'other' and not (priority and early)):
        return {'feed': False, 'ping': False, 'score': 0, 'reasons': ['outside target locations or roles'], 'role': role}
    # Company, role and geography can compensate for one another. A bank does
    # not penalize strong technical/quant work; an exceptional employer expands
    # the range of business roles worth considering.
    company_points = 45 if priority else 0 if selective else 12 if watch and watch.search(company) else 0
    role_points = {'software': 60, 'data_ml': 60, 'quant': 67,
                   'finance': 30, 'adjacent': 30, 'business': 20, 'other': 15}[role]
    score = role_points + company_points + location_points + (15 if early else 0)
    reasons.append({'software': 'software/backend fit', 'data_ml': 'ML/data fit', 'quant': 'quant/investing fit',
                    'finance': 'finance opportunity', 'adjacent': 'technical/business crossover',
                    'business': 'broader business opportunity', 'other': 'early-career opportunity'}[role])
    if priority:
        reasons.append('exceptional company broadens role interest')
    elif selective:
        reasons.append('role fit carries the decision at this company')
    elif company_points:
        reasons.append('watchlist company')
    else:
        reasons.append('company neutral')
    reasons.append({'nyc_sf': 'NYC/SF preferred', 'us': 'U.S. preferred', 'canada': 'Canada',
                    'unknown': 'location unconfirmed', 'remote': 'global remote'}[region])
    if early:
        reasons.append('early-career title')
    ping = score >= policy.get('threshold', 75)
    # Location is a gate for important alerts, not an additive score bonus.
    # Keep discovery feeds complete even when a move is not worthwhile.
    preferred = policy.get('ping_locations')
    if preferred and not matches(preferred, job.get('location', '')):
        exceptional = matches(policy.get('location_exception_companies', r'(?!)'), company)
        if not exceptional:
            ping = False
            reasons.append('full feed only: outside preferred ping locations')
        else:
            reasons.append('exceptional employer allows broader location')
    return {'feed': True, 'ping': ping, 'score': score, 'reasons': reasons, 'role': role}
