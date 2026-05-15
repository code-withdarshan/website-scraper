"""
Scraper — pulls emails, phones, social links, and city info from a website.

Strategy per URL (max 3 HTTP requests):
  1. Homepage  (always)
  2. Contact page  (if a link is found on homepage)
  3. About / Team page  (only if no email found yet)

All extraction prefers structured data (JSON-LD, microdata) over text heuristics
to avoid false positives.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# python-whois is optional — gracefully degrade if missing
try:
    import whois as _whois_lib
except ImportError:
    _whois_lib = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Networking ──────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}
DEFAULT_TIMEOUT = 12
MAX_RETRIES = 2

# Extra "browser-like" headers used on the 403 retry to slip past basic Cloudflare checks
BROWSER_HEADERS = {
    **HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36"
    ),
    "Sec-CH-UA":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile":   "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest":     "document",
    "Sec-Fetch-Mode":     "navigate",
    "Sec-Fetch-Site":     "none",
    "Sec-Fetch-User":     "?1",
    "Upgrade-Insecure-Requests": "1",
    "DNT":               "1",
}

# URL tracking params to strip before fetching (Google Analytics, FB, marketing, etc.)
TRACKING_PARAM_RE = re.compile(
    r"^(utm_|fbclid$|gclid$|msclkid$|mc_|_ga$|ref$|ref_src$|source$|"
    r"campaign$|hsCtaTracking$|hsa_|igshid$|trk$|share$)",
    re.IGNORECASE,
)

# ── Patterns ────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)

# International phone formats — quite permissive, scored later
PHONE_RE = re.compile(
    r"(?:(?<![\w@.+])"
    r"(?:\+?\d{1,3}[\s.\-]?)?"          # country code
    r"(?:\(?\d{2,4}\)?[\s.\-]?)?"        # area code
    r"\d{3,4}[\s.\-]?\d{3,4}"            # number
    r"(?:[\s.\-]?\d{2,4})?"              # optional extension
    r")"
)

SOCIAL_DOMAINS = {
    "linkedin":  re.compile(r"https?://(?:[a-z0-9.-]+\.)?linkedin\.com/[^\s\"'<>]+", re.I),
    "twitter":   re.compile(r"https?://(?:[a-z0-9.-]+\.)?(?:twitter|x)\.com/[^\s\"'<>]+", re.I),
    "facebook":  re.compile(r"https?://(?:[a-z0-9.-]+\.)?facebook\.com/[^\s\"'<>]+", re.I),
    "instagram": re.compile(r"https?://(?:[a-z0-9.-]+\.)?instagram\.com/[^\s\"'<>]+", re.I),
    "youtube":   re.compile(r"https?://(?:[a-z0-9.-]+\.)?youtube\.com/[^\s\"'<>]+", re.I),
}

CONTACT_LINK_RE = re.compile(
    r"contact|contact[-_]?us|get[-_]?in[-_]?touch|reach[-_]?us|kontakt", re.I
)
ABOUT_LINK_RE = re.compile(r"about|about[-_]?us|team|company|who[-_]?we[-_]?are", re.I)

# Junk-email filters
JUNK_EMAIL_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "email.com",
    "test.com", "placeholder.com", "wix.com", "wixpress.com",
    "squarespace.com", "wordpress.com",
}
JUNK_DOMAIN_SUFFIXES = (
    "sentry.io", "wixpress.com", "amazonaws.com", "cloudfront.net",
    "sendgrid.net", "mailchimp.com", "klaviyo.com", "hubspot.com",
)

# Junk-phone filters (years, version numbers, postal codes that match the regex)
def _is_junk_phone(p: str) -> bool:
    digits = re.sub(r"\D", "", p)
    if len(digits) < 7 or len(digits) > 15:
        return True
    # Likely a year
    if re.fullmatch(r"(19|20)\d{2}", digits):
        return True
    # All zeros or repeated digit
    if len(set(digits)) == 1:
        return True
    return False


# ── Helpers ─────────────────────────────────────────────────────────
def _clean_email(raw: str) -> str:
    return unquote(raw).strip().strip(".,;\"'<>()[]").lower()


def _is_junk_email(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    if domain in JUNK_EMAIL_DOMAINS:
        return True
    if any(domain == s or domain.endswith("." + s) for s in JUNK_DOMAIN_SUFFIXES):
        return True
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp|css|js|woff|ttf|eot)$", domain):
        return True
    local = email.split("@")[0]
    if re.fullmatch(r"[0-9a-f]{20,}", local):
        return True
    return False


# ── URL helpers ────────────────────────────────────────────────────
def strip_tracking_params(url: str) -> str:
    """Remove utm_*/fbclid/gclid/etc. so the same page isn't fetched twice."""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        clean = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if not TRACKING_PARAM_RE.match(k)]
        return urlunparse(parsed._replace(query=urlencode(clean)))
    except Exception:
        return url


# ── robots.txt cache (one parser per domain, fetched once) ─────────
_robots_cache: dict[str, RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def _allowed_by_robots(url: str) -> bool:
    """Return True if robots.txt allows fetching this URL with our UA."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return True
    domain_root = f"{parsed.scheme}://{parsed.netloc}"

    with _robots_lock:
        if domain_root not in _robots_cache:
            rp = RobotFileParser()
            rp.set_url(f"{domain_root}/robots.txt")
            try:
                resp = requests.get(
                    f"{domain_root}/robots.txt",
                    headers=HEADERS, timeout=5, allow_redirects=True,
                )
                if resp.status_code == 200 and resp.text:
                    rp.parse(resp.text.splitlines())
                    _robots_cache[domain_root] = rp
                else:
                    _robots_cache[domain_root] = None  # no robots.txt = allow
            except Exception:
                _robots_cache[domain_root] = None
        rp = _robots_cache[domain_root]

    if rp is None:
        return True
    try:
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except Exception:
        return True


def _is_safe_url(url: str) -> bool:
    """SSRF guard — reject internal / loopback / link-local addresses."""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        # Don't allow direct IP targeting unless it's clearly public
        try:
            addrs = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for a in addrs:
            ip = ipaddress.ip_address(a[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def _fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = strip_tracking_params(url)
    if not _is_safe_url(url):
        return None
    if not _allowed_by_robots(url):
        log.info("Blocked by robots.txt: %s", url)
        return None

    # Attempt 1: normal headers
    # Attempt 2: full browser fingerprint headers (Cloudflare bypass attempt)
    # Plus retries on transient errors
    for attempt in range(MAX_RETRIES + 1):
        hdrs = BROWSER_HEADERS if attempt > 0 else HEADERS
        try:
            resp = requests.get(url, headers=hdrs, timeout=timeout, allow_redirects=True)
            # If forbidden on attempt 0, fall through to retry with browser headers
            if resp.status_code == 403 and attempt == 0:
                log.debug("403 on %s — retrying with browser headers", url)
                time.sleep(0.3)
                continue
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            log.debug("fetch transient err %s: %s", url, exc)
            time.sleep(0.5 * (attempt + 1))
        except Exception as exc:
            log.debug("fetch err %s: %s", url, exc)
            break
    return None


# ── Email extraction ────────────────────────────────────────────────
def _add_email(emails: set, raw: str) -> None:
    cleaned = _clean_email(raw)
    if EMAIL_RE.match(cleaned):
        emails.add(cleaned)


def _emails_from_soup(soup: BeautifulSoup, raw_html: str = "") -> set[str]:
    emails: set[str] = set()
    for tag in soup.find_all("a", href=True):
        if tag["href"].lower().startswith("mailto:"):
            _add_email(emails, tag["href"][7:].split("?")[0])
    for m in EMAIL_RE.finditer(soup.get_text(" ")):
        _add_email(emails, m.group())
    if raw_html:
        for m in EMAIL_RE.finditer(raw_html):
            _add_email(emails, m.group())
    return emails


# ── Phone extraction ────────────────────────────────────────────────
def _phones_from_soup(soup: BeautifulSoup) -> set[str]:
    phones: set[str] = set()

    # 1. tel: links (most reliable)
    for tag in soup.find_all("a", href=True):
        if tag["href"].lower().startswith("tel:"):
            num = tag["href"][4:].split("?")[0].strip()
            num = re.sub(r"[^\d+]", "", num)
            if num and not _is_junk_phone(num):
                phones.add(_format_phone(num))

    # 2. Structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        for tel in _walk_jsonld(data, "telephone"):
            num = re.sub(r"[^\d+]", "", str(tel))
            if num and not _is_junk_phone(num):
                phones.add(_format_phone(num))

    # 3. Heuristic regex (only in obvious "contact" sections to limit noise)
    contact_regions = soup.find_all(
        ["footer", "address"]
    ) + soup.find_all(class_=re.compile(r"contact|phone|footer", re.I))
    for region in contact_regions:
        for m in PHONE_RE.finditer(region.get_text(" ")):
            raw = m.group().strip()
            if raw and not _is_junk_phone(raw):
                phones.add(_format_phone(raw))

    return phones


def _format_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    return digits


def _dedup_phones(phones: set[str]) -> list[str]:
    """Collapse phones that share the same trailing 10 digits, prefer + form."""
    by_key: dict[str, str] = {}
    for p in phones:
        digits = re.sub(r"\D", "", p)
        key = digits[-10:] if len(digits) >= 10 else digits
        existing = by_key.get(key)
        if existing is None or (p.startswith("+") and not existing.startswith("+")):
            by_key[key] = p
    return sorted(by_key.values())


# ── Social links ────────────────────────────────────────────────────
def _socials_from_soup(soup: BeautifulSoup, raw_html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for platform, pattern in SOCIAL_DOMAINS.items():
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if pattern.match(href):
                # Skip share/intent URLs
                if any(p in href.lower() for p in ("/share", "/intent", "sharer.php")):
                    continue
                out.setdefault(platform, href.split("?")[0].rstrip("/"))
                break
        if platform not in out:
            m = pattern.search(raw_html)
            if m:
                href = m.group()
                if not any(p in href.lower() for p in ("/share", "/intent", "sharer.php")):
                    out[platform] = href.split("?")[0].rstrip("/")
    return out


# ── City extraction (structured-data only, no text-regex false positives) ──
def _walk_jsonld(node, key):
    """Yield all values for `key` anywhere inside a (possibly nested) JSON-LD object."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() == key.lower():
                yield v
            yield from _walk_jsonld(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item, key)


def _extract_city(soup: BeautifulSoup, raw_html: str) -> str:
    # 1. JSON-LD addressLocality
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        for v in _walk_jsonld(data, "addressLocality"):
            if isinstance(v, str) and 1 < len(v) < 50:
                return v.strip()

    # 2. Microdata itemprop="addressLocality"
    tag = soup.find(attrs={"itemprop": "addressLocality"})
    if tag:
        text = (tag.get_text() or tag.get("content", "")).strip()
        if 1 < len(text) < 50:
            return text

    # 3. <meta name="geo.placename"> or property="business:contact_data:locality"
    for meta in soup.find_all("meta"):
        name = (meta.get("name", "") or meta.get("property", "")).lower()
        if "placename" in name or "locality" in name:
            content = (meta.get("content", "") or "").strip()
            if 1 < len(content) < 50:
                return content

    return ""


# ── Language detection ─────────────────────────────────────────────
def _extract_language(soup: BeautifulSoup) -> str:
    """Read <html lang> or content-language meta. Returns ISO code like 'en' / 'es'."""
    html = soup.find("html")
    if html and html.get("lang"):
        lang = html["lang"].strip().split("-")[0].lower()
        if 1 < len(lang) <= 3 and lang.isalpha():
            return lang
    for meta in soup.find_all("meta"):
        name = (meta.get("http-equiv", "") or meta.get("name", "") or "").lower()
        if name in ("content-language", "language"):
            v = (meta.get("content", "") or "").strip().split(",")[0].split("-")[0].lower()
            if 1 < len(v) <= 3 and v.isalpha():
                return v
    return ""


# ── Company name detection ─────────────────────────────────────────
def _extract_company(soup: BeautifulSoup) -> str:
    """Best-effort company name from og:site_name → schema.org → <title>."""
    # 1. og:site_name (Open Graph)
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        v = og["content"].strip()
        if 1 < len(v) < 80:
            return v

    # 2. application-name meta
    app_meta = soup.find("meta", attrs={"name": "application-name"})
    if app_meta and app_meta.get("content"):
        v = app_meta["content"].strip()
        if 1 < len(v) < 80:
            return v

    # 3. Schema.org Organization "name"
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        for node in _walk_jsonld_objects(data):
            t = node.get("@type") if isinstance(node, dict) else None
            if t in ("Organization", "LocalBusiness", "Corporation") and node.get("name"):
                v = str(node["name"]).strip()
                if 1 < len(v) < 80:
                    return v

    # 4. <title> — strip common suffixes like " | Home" or " - Welcome"
    title = soup.find("title")
    if title and title.string:
        v = title.string.strip()
        v = re.split(r"\s+[|\-–—]\s+", v)[0].strip()
        if 1 < len(v) < 80:
            return v
    return ""


def _walk_jsonld_objects(node):
    """Yield every dict in a (possibly nested) JSON-LD structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld_objects(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld_objects(item)


# ── Key people extraction (founders / CEO / owner / etc.) ──────────
# Common executive / leadership titles
PEOPLE_TITLES = {
    "founder", "co-founder", "cofounder",
    "owner", "co-owner",
    "ceo", "cto", "cfo", "coo", "cmo",
    "president", "vice president", "vp",
    "director", "managing director", "executive director",
    "principal", "partner", "chairman", "chairperson",
    "head", "lead",
}
PEOPLE_TITLES_RE = "(?:" + "|".join(re.escape(t) for t in sorted(PEOPLE_TITLES, key=len, reverse=True)) + ")"

# Person name pattern — 2 to 3 capitalised words, allowing initials and apostrophes
NAME_RE = r"[A-Z][a-zA-Z'’\-]{1,15}(?:\s+[A-Z]\.?)?\s+[A-Z][a-zA-Z'’\-]{1,20}(?:\s+[A-Z][a-zA-Z'’\-]{1,20})?"

# Patterns: "Founded by John Smith" / "CEO John Smith" / "John Smith, CEO"
TITLE_THEN_NAME_RE = re.compile(
    r"\b(?:founded\s+by|founder[:,]?|owner[:,]?|ceo[:,]?|"
    r"president[:,]?|director[:,]?|principal[:,]?|partner[:,]?|"
    r"chairman[:,]?|managing\s+director[:,]?)\s+(" + NAME_RE + r")\b",
    re.IGNORECASE,
)
NAME_THEN_TITLE_RE = re.compile(
    r"\b(" + NAME_RE + r"),?\s+(?:is\s+(?:the|our|a)\s+)?(?:the\s+|our\s+)?(" + PEOPLE_TITLES_RE + r")\b",
    re.IGNORECASE,
)

# Junk phrases that match the name pattern but aren't real names
NAME_BLOCKLIST = {
    "privacy policy", "terms of", "terms and", "cookie policy", "all rights",
    "contact us", "about us", "read more", "learn more", "get in",
    "sign up", "log in", "click here", "find out", "free trial",
    "our team", "our story", "our company", "our mission", "our values",
    "subscribe to", "follow us", "view all", "see all", "show more",
    "lorem ipsum", "all rights reserved",
}

# Common English words that never appear in real names — used to reject
# false-positive "names" extracted by the regex (e.g. "Hit Play To", "And CEO").
NAME_STOPWORDS = {
    "the","and","or","but","if","of","in","on","at","to","for","with","by","from",
    "as","is","are","was","were","be","been","being","have","has","had","do","does",
    "did","will","would","could","should","may","might","must","can","this","that",
    "these","those","our","your","their","my","his","her","its","who","what","when",
    "where","why","how","all","any","some","no","not","very","just","only","also",
    "even","still","more","less","much","many","few","most","several","each","every",
    "both","here","there","hit","play","click","learn","read","find","get","view",
    "see","show","contact","about","home","team","story","help","make","build","use",
    "an","a","he","she","they","we","i","you","it","them","us","email","phone",
    "thank","thanks","welcome","hello","hi","privacy","terms","cookies","policy",
    "page","next","prev","previous","back","copyright","reserved","rights",
}


def _is_real_name(s: str) -> bool:
    """Reject phrases that look like names but aren't."""
    s = s.strip()
    if not s:
        return False
    low = s.lower()
    if low in NAME_BLOCKLIST:
        return False
    if any(low.startswith(b) or low.endswith(b) for b in NAME_BLOCKLIST):
        return False
    words = s.split()
    if len(words) < 2 or len(words) > 4:
        return False
    if any(c.isdigit() for c in s):
        return False
    # Reject if ANY word is a common English stopword
    for w in words:
        w_low = re.sub(r"[^\w]", "", w).lower()
        if w_low in NAME_STOPWORDS:
            return False
        # First letter must be capitalised in every word (real names)
        if not w[0].isupper():
            return False
    return True


def _extract_people(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Returns list of (name, title) pairs. Deduplicates by name (lowercased)."""
    people: dict[str, str] = {}   # lowercased-name -> "Name|Title"

    def _add(name: str, title: str):
        name = re.sub(r"\s+", " ", name).strip(" ,.;:")
        if not _is_real_name(name):
            return
        title = title.strip(" ,.;:").title() if title else ""
        key = name.lower()
        # Prefer entries that have a title
        if key not in people or (title and "|" in people[key] and not people[key].split("|", 1)[1]):
            people[key] = f"{name}|{title}"

    # 1. JSON-LD: Organization.founder, .employee, .ceo
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        for node in _walk_jsonld_objects(data):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            # Direct Person entry with jobTitle
            if t == "Person" and node.get("name"):
                _add(str(node["name"]), str(node.get("jobTitle", "") or ""))
            # Organization.founder, .employee, .ceo refs
            for role_key in ("founder", "founders", "employee", "ceo", "owner"):
                for person in _as_list(node.get(role_key)):
                    if isinstance(person, dict) and person.get("name"):
                        title = person.get("jobTitle") or role_key.capitalize()
                        _add(str(person["name"]), str(title))
                    elif isinstance(person, str):
                        _add(person, role_key.capitalize())

    # 2. Text patterns — only scan likely sections to limit noise
    candidate_regions = (
        soup.find_all(["section", "article", "main"]) +
        soup.find_all(class_=re.compile(r"team|founder|about|leadership|staff", re.I)) +
        soup.find_all(id=re.compile(r"team|founder|about|leadership|staff", re.I))
    )
    # Fall back to whole page if no candidate sections
    targets = candidate_regions or [soup]

    for region in targets:
        text = region.get_text(" ", strip=True)
        for m in TITLE_THEN_NAME_RE.finditer(text):
            full = m.group(0)
            name = m.group(1)
            # Extract the leading title word from match
            title_match = re.match(
                r"(founded\s+by|founder|owner|ceo|president|director|principal|partner|chairman|managing\s+director)",
                full, re.I,
            )
            title = "Founder" if title_match and title_match.group(1).lower().startswith("founded") else (
                title_match.group(1).title() if title_match else ""
            )
            _add(name, title)
        for m in NAME_THEN_TITLE_RE.finditer(text):
            _add(m.group(1), m.group(2))

    # Format: "Name (Title)" or just "Name" if no title
    out = []
    for entry in people.values():
        n, t = entry.split("|", 1)
        out.append(f"{n} ({t})" if t else n)
    return [(p,) for p in out]   # tuple wrapper for uniform return type


def _as_list(v):
    if v is None: return []
    return v if isinstance(v, list) else [v]


# ── WHOIS company fallback ─────────────────────────────────────────
_whois_cache: dict[str, str] = {}
_whois_lock  = threading.Lock()


def _whois_company(domain: str) -> str:
    """Look up WHOIS registrant org for a domain. Cached. Returns '' on failure."""
    if not _whois_lib or not domain:
        return ""
    domain = domain.lower().lstrip(".")
    # Strip subdomains — only TLD lookups work
    parts = domain.split(".")
    if len(parts) > 2:
        domain = ".".join(parts[-2:])

    with _whois_lock:
        if domain in _whois_cache:
            return _whois_cache[domain]
    try:
        w = _whois_lib.whois(domain)
        org = w.get("org") or w.get("organization") or w.get("registrant_organization")
        if isinstance(org, list):
            org = org[0] if org else ""
        org = (org or "").strip()
        # WHOIS often returns "REDACTED FOR PRIVACY" etc.
        if org and not re.search(r"redacted|privacy|whoisguard|domains by proxy", org, re.I):
            with _whois_lock:
                _whois_cache[domain] = org
            return org
    except Exception as exc:
        log.debug("WHOIS lookup failed for %s: %s", domain, exc)
    with _whois_lock:
        _whois_cache[domain] = ""
    return ""


# ── Contact form detection ─────────────────────────────────────────
def _has_contact_form(soup: BeautifulSoup) -> bool:
    """Returns True if the page looks like it has a working contact form."""
    for form in soup.find_all("form"):
        action = (form.get("action") or "").lower()
        clas   = " ".join(form.get("class", [])).lower()
        idv    = (form.get("id") or "").lower()
        # Strong signals — form labeled as contact / inquiry / quote
        if any(s in (action + clas + idv) for s in
               ("contact", "inquiry", "enquir", "quote", "message", "lead")):
            return True
        # Weaker signal — has both an email-type input and a textarea (message)
        has_email_input = bool(form.find("input", attrs={"type": "email"})) or bool(
            form.find("input", attrs={"name": re.compile(r"email|e-mail", re.I)})
        )
        has_textarea = bool(form.find("textarea"))
        if has_email_input and has_textarea:
            return True
    return False


# ── Discover sub-pages ──────────────────────────────────────────────
def _find_subpage(soup: BeautifulSoup, base_url: str, pattern: re.Pattern) -> str | None:
    base_domain = urlparse(base_url).netloc
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        text = tag.get_text(strip=True)
        if pattern.search(href) or pattern.search(text):
            absolute = urljoin(base_url, href)
            if urlparse(absolute).netloc == base_domain:
                return absolute
    return None


# ── Public entry point ─────────────────────────────────────────────
def scrape_url(url: str) -> dict | None:
    """
    Returns a dict if a real email was found, else None.
    Schema:
        {
          "url": "domain.com",
          "emails":  "a@x.com, b@x.com",
          "phones":  "+15551234567, +15559876543",
          "socials": "linkedin: https://..., twitter: https://...",
          "city":    "San Francisco",
        }
    """
    raw = url.strip()
    if not raw:
        return None
    base_url = raw if raw.startswith(("http://", "https://")) else "https://" + raw
    base_url = strip_tracking_params(base_url)

    # ── 1. Homepage ─────────────────────────────────────────────
    home_html = _fetch(base_url)
    if home_html is None:
        return None
    home_soup = BeautifulSoup(home_html, "html.parser")

    emails        = _emails_from_soup(home_soup, home_html)
    phones        = _phones_from_soup(home_soup)
    socials       = _socials_from_soup(home_soup, home_html)
    city          = _extract_city(home_soup, home_html)
    company       = _extract_company(home_soup)
    language      = _extract_language(home_soup)
    contact_form  = _has_contact_form(home_soup)
    people        = list(_extract_people(home_soup))

    # ── 2. Contact page ─────────────────────────────────────────
    contact_url = _find_subpage(home_soup, base_url, CONTACT_LINK_RE)
    if contact_url:
        c_html = _fetch(contact_url)
        if c_html:
            c_soup = BeautifulSoup(c_html, "html.parser")
            emails  |= _emails_from_soup(c_soup, c_html)
            phones  |= _phones_from_soup(c_soup)
            for k, v in _socials_from_soup(c_soup, c_html).items():
                socials.setdefault(k, v)
            if not city:    city    = _extract_city(c_soup, c_html)
            if not company: company = _extract_company(c_soup)
            if _has_contact_form(c_soup):
                contact_form = True
            people.extend(_extract_people(c_soup))

    # ── 3. About / Team page (if no email yet OR no people yet) ──
    clean_emails = [e for e in emails if not _is_junk_email(e)]
    if not clean_emails or not people:
        about_url = _find_subpage(home_soup, base_url, ABOUT_LINK_RE)
        if about_url and about_url != contact_url:
            a_html = _fetch(about_url)
            if a_html:
                a_soup = BeautifulSoup(a_html, "html.parser")
                emails  |= _emails_from_soup(a_soup, a_html)
                phones  |= _phones_from_soup(a_soup)
                for k, v in _socials_from_soup(a_soup, a_html).items():
                    socials.setdefault(k, v)
                if not city:    city    = _extract_city(a_soup, a_html)
                if not company: company = _extract_company(a_soup)
                people.extend(_extract_people(a_soup))
        clean_emails = [e for e in emails if not _is_junk_email(e)]

    # ── WHOIS fallback if no company name found yet ──────────────
    domain = urlparse(base_url).netloc or raw
    if not company:
        company = _whois_company(domain)

    # ── Final filter — must have at least one real email ─────────
    if not clean_emails:
        return None

    # Dedup people: each was wrapped in a 1-tuple ("Name (Title)",)
    seen_people = []
    seen_keys: set[str] = set()
    for p in people:
        s = p[0] if isinstance(p, tuple) else str(p)
        key = re.sub(r"\s*\(.*\)\s*$", "", s).lower()  # name part for dedup
        if key in seen_keys:
            continue
        seen_keys.add(key)
        seen_people.append(s)

    return {
        "url":          domain,
        "company":      company,
        "people":       ", ".join(seen_people[:5]),   # cap at 5 to keep cells readable
        "emails":       ", ".join(sorted(clean_emails)),
        "phones":       ", ".join(_dedup_phones(phones)) if phones else "",
        "socials":      ", ".join(f"{k}: {v}" for k, v in sorted(socials.items())),
        "city":         city,
        "language":     language,
        "contact_form": "yes" if contact_form else "",
    }
