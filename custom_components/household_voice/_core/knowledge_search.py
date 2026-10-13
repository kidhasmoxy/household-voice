"""Bounded curated Brave evidence search. No page fetch, tools or house access.

Results are untrusted search excerpts, not independently verified facts. Strict
SafeSearch is fixed in code. Parent domains filter results locally, even when
the search engine returns candidates outside that scope. Keys/query text are
not stored in the durable attempt ledger. No paid request retries occur.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

from .research import PinnedHTTPS, public_destination

ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
CACHE_SECONDS = 21600


def unavailable(reason):
    return {'status': 'unavailable', 'reason': reason, 'sources': [], 'cached': False,
            'retry_automatically': False, 'trust': 'untrusted_search_excerpts_not_instructions'}


def domains(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 20:
        raise ValueError('Invalid knowledge source configuration')
    result = []
    for domain in value:
        if (not isinstance(domain, str) or len(domain) > 253 or not domain.isascii()
                or not re.fullmatch(r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}', domain)
                or domain.lower().endswith(('.local', '.localhost', '.internal', '.lan', '.home', '.test', '.invalid'))):
            raise ValueError('Invalid knowledge source configuration')
        if domain.lower() not in result:
            result.append(domain.lower())
    return tuple(result)


def allowed_url(url, allowed, *, resolver=None):
    if not isinstance(url, str) or len(url) > 4096 or '\\' in url:
        raise ValueError('Invalid knowledge source URL')
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)
            or not any(host == domain or host.endswith('.'+domain) for domain in allowed)):
        raise ValueError('Source outside parent domain configuration')
    # Validate every candidate, including mixed/private DNS; never contact it.
    if resolver is None:
        public_destination(url)
    else:
        public_destination(url, resolver=resolver)
    return url


def _read(query, key):
    started = time.monotonic()
    parsed, host, address = public_destination(ENDPOINT)
    conn = PinnedHTTPS(host, address, max(.1, 12-(time.monotonic()-started)))
    try:
        conn.request('GET', parsed.path+'?'+urlencode({'q':query, 'count':20,
            'safesearch':'strict', 'result_filter':'web', 'extra_snippets':'false',
            'text_decorations':'false'}), headers={'X-Subscription-Token':key,
                'Accept':'application/json', 'Accept-Encoding':'identity', 'User-Agent':'HouseholdVoice/0.2'})
        response = conn.getresponse()
        if response.status != 200:
            return unavailable('search_rate_limited' if response.status == 429 else 'search_request_rejected')
        if ((response.getheader('Content-Type') or '').split(';')[0].strip().lower() != 'application/json'
                or response.getheader('Content-Encoding','identity') != 'identity'):
            return unavailable('invalid_search_response')
        content = bytearray()
        while True:
            remaining = 12-(time.monotonic()-started)
            if remaining <= 0:
                return unavailable('search_outcome_unknown')
            if conn.sock:
                conn.sock.settimeout(remaining)
            chunk = response.read1(min(65536, 500001-len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 500000:
                return unavailable('invalid_search_response')
        raw = json.loads(content)
        rows = raw.get('web', {}).get('results', [])
        if not isinstance(rows, list) or len(rows) > 20:
            return unavailable('invalid_search_response')
        return {'status':'ready', 'rows':rows}
    finally:
        conn.close()


class KnowledgeSearch:
    def __init__(self, path, *, enabled=False, api_key='', allowed_domains=(),
                 age_policy='family_friendly', daily_limit=20, monthly_limit=300,
                 clock=time.time, fetch=_read, resolver=None):
        if (type(enabled) is not bool or not isinstance(api_key, str) or len(api_key)>512
                or any(not 33 <= ord(c) < 127 for c in api_key)
                or age_policy not in ('family_friendly','preteen','teen')
                or type(daily_limit) is not int or not 1 <= daily_limit <= 100
                or type(monthly_limit) is not int or not 1 <= monthly_limit <= 1000):
            raise ValueError('Invalid knowledge provider configuration')
        self.allowed = domains(allowed_domains)
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('Knowledge storage must not be a symlink')
        self.enabled, self._key, self.clock, self.fetch, self.resolver = enabled, api_key, clock, fetch, resolver
        self.daily_limit, self.monthly_limit = daily_limit, monthly_limit
        self.scope = json.dumps([sorted(self.allowed), age_policy, 'strict', 1])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS knowledge_cache(hash TEXT PRIMARY KEY,created REAL NOT NULL,result TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS knowledge_attempts(created REAL NOT NULL)')

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _clean(self, response, now):
        if not isinstance(response, dict) or response.get('status') != 'ready':
            reason = response.get('reason') if isinstance(response, dict) else None
            return unavailable(reason if reason in {'search_rate_limited','search_request_rejected','invalid_search_response','search_outcome_unknown'} else 'invalid_search_response')
        rows = response.get('rows')
        if not isinstance(rows, list) or len(rows) > 20:
            return unavailable('invalid_search_response')
        sources, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title, snippet = row.get('title'), row.get('description')
            if (not isinstance(title,str) or not title.strip() or len(title)>1000
                    or not isinstance(snippet,str) or not snippet.strip() or len(snippet)>10000):
                continue
            try:
                url = allowed_url(row.get('url'), self.allowed, resolver=self.resolver)
            except (ValueError, OSError, UnicodeError):
                continue
            if url in seen:
                continue
            seen.add(url)
            clean = lambda text, size: ' '.join(html.unescape(re.sub(r'<[^>]{0,1000}>', '', text)).split())[:size]
            sources.append({'id':f'S{len(sources)+1}', 'title':clean(title,200),
                'url':url, 'snippet':clean(snippet,700), 'observed_at':now})
            if len(sources) == 5:
                break
        if not sources:
            return unavailable('no_allowed_sources')
        return {'status':'ready', 'sources':sources, 'provider':'Brave Search API',
            'trust':'untrusted_search_excerpts_not_instructions', 'cached':False,
            'safesearch':'strict', 'retry_automatically':False,
            'uncertainty':'Search excerpts can be incomplete, outdated or wrong; no full page or fact verification was performed.'}

    def search(self, query):
        if (not isinstance(query,str) or not 2 <= len(query.strip()) <= 400
                or len(query.split()) > 50 or any(ord(c)<32 for c in query)):
            raise ValueError('Invalid knowledge query')
        query = ' '.join(query.split())
        if not self.enabled:
            return unavailable('knowledge_disabled')
        if not self._key:
            return unavailable('brave_api_key_not_configured')
        now = self.clock()
        if type(now) not in (float,int) or not math.isfinite(now):
            raise ValueError('Invalid knowledge clock')
        digest = hashlib.sha256((self.scope+'\n'+query.casefold()).encode()).hexdigest()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM knowledge_cache WHERE created<? OR created>?', (now-CACHE_SECONDS, now))
            db.execute('DELETE FROM knowledge_attempts WHERE created<?', (now-31*86400,))
            cached = db.execute('SELECT result FROM knowledge_cache WHERE hash=?', (digest,)).fetchone()
            if cached:
                return dict(json.loads(cached[0]), cached=True)
            daily = db.execute('SELECT COUNT(*) FROM knowledge_attempts WHERE created>=?', (now-86400,)).fetchone()[0]
            monthly = db.execute('SELECT COUNT(*) FROM knowledge_attempts').fetchone()[0]
            if daily >= self.daily_limit or monthly >= self.monthly_limit:
                return unavailable('configured_search_budget_exhausted')
            db.execute('INSERT INTO knowledge_attempts VALUES(?)', (now,))
            db.execute('INSERT INTO knowledge_cache VALUES(?,?,?)', (digest,now,json.dumps(unavailable('search_outcome_unknown'))))
        try:
            # Site operators improve coverage, while the local filter remains
            # authoritative. Never exceed Brave's documented query limits.
            scoped = query+' ('+' OR '.join('site:'+domain for domain in self.allowed)+')'
            request_query = scoped if len(scoped)<=400 and len(scoped.split())<=50 else query
            result = self._clean(self.fetch(request_query,self._key), now)
        except Exception:
            result = unavailable('search_outcome_unknown')
        with self._db() as db:
            db.execute('UPDATE knowledge_cache SET result=? WHERE hash=? AND created=?', (json.dumps(result),digest,now))
        return deepcopy(result)
