"""Bounded public web reads with DNS pinning and no provider secrets."""
import html
import http.client
import ipaddress
import json
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit


class FetchError(ValueError):
    pass


def public_destination(url, resolver=socket.getaddrinfo):
    p = urlsplit(url)
    if (len(url) > 4096 or p.scheme != "https" or not p.hostname or
            p.username or p.password or p.port not in (None, 443) or
            any(ord(c) < 33 for c in url)):
        raise FetchError("Only credential-free public HTTPS URLs on port 443 are allowed")
    host = p.hostname.encode("idna").decode("ascii")
    records = resolver(host, 443, type=socket.SOCK_STREAM)
    addresses = sorted({record[4][0] for record in records})
    if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
        raise FetchError("Private, loopback, reserved or mixed DNS destinations are blocked")
    return p, host, addresses[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_public(url, max_bytes=1_000_000, timeout=12):
    deadline = time.monotonic() + timeout
    for attempt in range(4):
        parsed, host, address = public_destination(url)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise FetchError("Fetch deadline exceeded")
        conn = PinnedHTTPS(host, address, remaining)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.request("GET", path, headers={"User-Agent": "HouseholdVoiceStaging/0.1", "Accept-Encoding": "identity"})
            res = conn.getresponse()
            if res.status in (301,302,303,307,308):
                location = res.getheader("Location")
                if not location:
                    raise FetchError("Redirect missing location")
                url = urljoin(url, location)
                continue
            if res.status != 200:
                raise FetchError(f"Source returned HTTP {res.status}")
            ctype = (res.getheader("Content-Type") or "").split(";")[0].lower()
            if ctype not in ("text/html", "text/plain", "application/json", "application/ld+json"):
                raise FetchError("Unsupported document type")
            if res.getheader("Content-Encoding", "identity") != "identity":
                raise FetchError("Compressed responses are not accepted")
            chunks, size = [], 0
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise FetchError("Fetch deadline exceeded")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = res.read1(min(65536, max_bytes+1-size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError("Document exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace"), url, ctype
        finally:
            conn.close()
    raise FetchError("Too many redirects")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.scripts, self.buf = [], [], []
        self.hidden, self.json_script = 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "script" and dict(attrs).get("type","").lower() == "application/ld+json":
            self.json_script = True
            self.buf = []
        if tag in ("p","div","li","h1","h2","h3","br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self.json_script:
            try:
                self.scripts.append(json.loads("".join(self.buf)))
            except (ValueError, RecursionError):
                pass
            self.json_script = False
        if tag in ("script","style","noscript"):
            self.hidden = max(0, self.hidden-1)

    def handle_data(self, data):
        if self.json_script:
            self.buf.append(data)
        elif not self.hidden:
            self.parts.append(data)


def recipes_from_json(value, depth=0):
    if depth > 12:
        return []
    found = []
    if isinstance(value, list):
        for item in value:
            found.extend(recipes_from_json(item, depth+1))
    elif isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "Recipe" or isinstance(kind, list) and "Recipe" in kind:
            keys = ["name","recipeYield","prepTime","cookTime","totalTime","recipeIngredient","recipeInstructions"]
            found.append({k:value[k] for k in keys if k in value})
        for key in ("@graph","mainEntity","itemListElement"):
            if key in value:
                found.extend(recipes_from_json(value[key], depth+1))
    return found[:5]


def extract_page(url):
    body, final_url, ctype = fetch_public(url)
    parser = PageParser()
    if ctype in ("application/json", "application/ld+json"):
        try:
            parser.scripts.append(json.loads(body))
        except (ValueError, RecursionError) as exc:
            raise FetchError("Invalid JSON document") from exc
        parser.parts.append(body)
    else:
        parser.feed(body)
    recipes = []
    for script in parser.scripts:
        recipes.extend(recipes_from_json(script))
    return {"source":final_url, "trust":"untrusted_web_content", "text":html.unescape(" ".join(parser.parts))[:14000],
            "recipes":recipes[:3], "fetched_at":time.time(), "truncated":len(" ".join(parser.parts))>14000}


def fetch_json(url):
    text, _, _ = fetch_public(url)
    return json.loads(text)


def find_places(query, country_code=""):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 160:
        raise ValueError("Location needs 2..160 characters")
    args = {"name":query.strip(),"count":5,"language":"en","format":"json"}
    if country_code:
        if len(country_code)!=2 or not country_code.isalpha():
            raise ValueError("Use a two-letter country code")
        args["countryCode"] = country_code.upper()
    raw = fetch_json("https://geocoding-api.open-meteo.com/v1/search?"+urlencode(args))
    keys = ("id","name","admin1","country","country_code","latitude","longitude","timezone")
    places = [{k:p[k] for k in keys if k in p} for p in raw.get("results", [])]
    return {"candidates":places,"requires_selection":len(places)!=1,"source":"Open-Meteo / GeoNames"}


class Forecasts:
    def __init__(self, clock=time.time):
        self.clock, self.cache = clock, {}

    def get(self, latitude, longitude, days=3, units="fahrenheit"):
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or not 1 <= days <= 7:
            raise ValueError("Invalid coordinates or day count")
        if units not in ("fahrenheit", "celsius"):
            raise ValueError("Use fahrenheit or celsius")
        key=(latitude,longitude,days,units)
        cached=self.cache.get(key)
        if cached and self.clock()-cached["fetched_at"]<900:
            return dict(cached, cached=True)
        args={"latitude":latitude,"longitude":longitude,"forecast_days":days,"timezone":"auto",
              "temperature_unit":units,"daily":"temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"}
        result=fetch_json("https://api.open-meteo.com/v1/forecast?"+urlencode(args))
        answer={k:result[k] for k in ("latitude","longitude","timezone","utc_offset_seconds","daily","daily_units") if k in result}
        answer.update(source="Open-Meteo",source_url="https://open-meteo.com/",fetched_at=self.clock(),cached=False)
        if len(self.cache)>=256:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key]=answer
        return answer

