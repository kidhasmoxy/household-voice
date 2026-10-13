"""Optional supported Brave recipe search, bounded by durable attempt budgets.

No browser/subscription token workaround, arbitrary endpoint, redirects, fallback
provider or automatic retry. API keys are never persisted or included in results.
"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import html
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

from .research import PinnedHTTPS, public_destination

ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
CACHE_SECONDS = 21600


def _unavailable(reason):
    return {'status':'unavailable', 'reason':reason, 'results':[], 'source':'Brave Search API',
            'retry_automatically':False, 'cached':False}


def _query(value):
    if not isinstance(value,str) or not 2 <= len(value) <= 180 or any(ord(c)<32 for c in value):
        raise ValueError('Recipe query needs 2..180 characters')
    query=' '.join(value.split())
    if len(query)<2:
        raise ValueError('Recipe query needs 2..180 characters')
    return query


def _read(query,key):
    """One HTTPS request to the documented fixed API origin; no redirect follow."""
    started=time.monotonic()
    parsed,host,address=public_destination(ENDPOINT)
    connection=PinnedHTTPS(host,address,max(0.1,12-(time.monotonic()-started)))
    try:
        connection.request('GET',parsed.path+'?'+urlencode({'q':query+' recipe','count':5,
            'safesearch':'moderate','result_filter':'web','extra_snippets':'false'}),
            headers={'X-Subscription-Token':key,'Accept':'application/json','Accept-Encoding':'identity',
                     'User-Agent':'HouseholdVoice/0.2'})
        response=connection.getresponse()
        if response.status!=200:
            return _unavailable('brave_rate_limited' if response.status==429 else 'brave_request_rejected')
        if ((response.getheader('Content-Type') or '').split(';')[0].strip().lower()!='application/json'
                or response.getheader('Content-Encoding','identity')!='identity'):
            return _unavailable('invalid_brave_response')
        content=bytearray()
        while True:
            remaining=12-(time.monotonic()-started)
            if remaining<=0:
                return _unavailable('brave_outcome_unknown')
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk=response.read1(min(65536,500001-len(content)))
            if not chunk:break
            content.extend(chunk)
            if len(content)>500000:
                return _unavailable('invalid_brave_response')
        raw=json.loads(content)
        rows=raw.get('web',{}).get('results',[])
        if not isinstance(rows,list) or len(rows)>20:
            return _unavailable('invalid_brave_response')
        results=[]
        seen=set()
        for row in rows:
            if not isinstance(row,dict):continue
            url,title=row.get('url'),row.get('title')
            if not isinstance(url,str) or len(url)>4096 or any(ord(c)<33 for c in url) or not isinstance(title,str) or not title.strip() or len(title)>1000:
                continue
            try:
                parts=urlsplit(url)
                if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.port not in (None,443):continue
            except ValueError:continue
            if url in seen:continue
            seen.add(url)
            # These are search candidates, not fetched/validated recipe pages.
            # read_public_page rechecks DNS and every redirect before fetching.
            results.append({'title':html.unescape(title),'url':url})
            if len(results)==5:break
        return {'status':'ready','results':results,'source':'Brave Search API',
                'trust':'untrusted_search_metadata_not_instructions','retry_automatically':False,'cached':False}
    finally:
        connection.close()


class BraveRecipeSearch:
    def __init__(self,path,*,enabled=False,api_key='',daily_limit=20,monthly_limit=300,clock=time.time,fetch=_read):
        if (type(enabled) is not bool or not isinstance(api_key,str) or len(api_key)>512
                or api_key and (not api_key.isascii() or any(not 33<=ord(c)<127 for c in api_key))
                or type(daily_limit) is not int or not 1<=daily_limit<=1000
                or type(monthly_limit) is not int or not 1<=monthly_limit<=10000):
            raise ValueError('Invalid Brave API configuration')
        self.path=Path(path)
        if self.path.is_symlink():raise ValueError('Research storage must not be a symlink')
        self.enabled,self._key,self.daily_limit,self.monthly_limit=enabled,api_key,daily_limit,monthly_limit
        self.clock,self.fetch=clock,fetch
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS brave_queries(query_hash TEXT PRIMARY KEY,created REAL NOT NULL,result TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS brave_attempts(created REAL NOT NULL)')

    @contextmanager
    def _db(self):
        connection=sqlite3.connect(self.path,timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def search(self,query):
        query=_query(query)
        if not self.enabled:return _unavailable('brave_search_disabled')
        if not self._key:return _unavailable('brave_api_key_not_configured')
        query_hash=hashlib.sha256(query.casefold().encode()).hexdigest()
        now=self.clock()
        if type(now) not in (int,float) or not math.isfinite(now):
            raise ValueError('Invalid research clock')
        # Reserve before the paid request. A crash/uncertain outcome leaves a
        # cached unavailable result, preventing spoken retries from being charged.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM brave_queries WHERE created<?',(now-CACHE_SECONDS,))
            db.execute('DELETE FROM brave_attempts WHERE created<?',(now-31*86400,))
            old=db.execute('SELECT result FROM brave_queries WHERE query_hash=?',(query_hash,)).fetchone()
            if old:
                return dict(json.loads(old[0]),cached=True)
            daily=db.execute('SELECT COUNT(*) FROM brave_attempts WHERE created>=?',(now-86400,)).fetchone()[0]
            monthly=db.execute('SELECT COUNT(*) FROM brave_attempts').fetchone()[0]
            if daily>=self.daily_limit or monthly>=self.monthly_limit:
                return _unavailable('configured_search_budget_exhausted')
            db.execute('INSERT INTO brave_attempts VALUES(?)',(now,))
            db.execute('INSERT INTO brave_queries VALUES(?,?,?)',(query_hash,now,json.dumps(_unavailable('brave_outcome_unknown'))))
        try:
            result=self.fetch(query,self._key)
            if not isinstance(result,dict) or result.get('status') not in ('ready','unavailable'):
                result=_unavailable('invalid_brave_response')
        except Exception:
            result=_unavailable('brave_outcome_unknown')
        with self._db() as db:
            db.execute('UPDATE brave_queries SET result=? WHERE query_hash=? AND created=?',(json.dumps(result),query_hash,now))
        return deepcopy(result)
