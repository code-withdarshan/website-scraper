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
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

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
    if not _is_safe_url(url):
        return None
    if not _allowed_by_robots(url):
        log.info("Blocked by robots.txt: %s", url)
        return None
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            # Skip non-HTML responses
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc
            time.sleep(0.5 * (attempt + 1))
        except Exception as exc:
            last_err = exc
            break
    log.debug("Fetch failed %s: %s", url, last_err)
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

    # ── 1. Homepage ─────────────────────────────────────────────
    home_html = _fetch(base_url)
    if home_html is None:
        return None
    home_soup = BeautifulSoup(home_html, "html.parser")

    emails  = _emails_from_soup(home_soup, home_html)
    phones  = _phones_from_soup(home_soup)
    socials = _socials_from_soup(home_soup, home_html)
    city    = _extract_city(home_soup, home_html)

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
            if not city:
                city = _extract_city(c_soup, c_html)

    # ── 3. About / Team page (only if no email yet) ──────────────
    clean_emails = [e for e in emails if not _is_junk_email(e)]
    if not clean_emails:
        about_url = _find_subpage(home_soup, base_url, ABOUT_LINK_RE)
        if about_url and about_url != contact_url:
            a_html = _fetch(about_url)
            if a_html:
                a_soup = BeautifulSoup(a_html, "html.parser")
                emails  |= _emails_from_soup(a_soup, a_html)
                phones  |= _phones_from_soup(a_soup)
                for k, v in _socials_from_soup(a_soup, a_html).items():
                    socials.setdefault(k, v)
                if not city:
                    city = _extract_city(a_soup, a_html)
        clean_emails = [e for e in emails if not _is_junk_email(e)]

    # ── Final filter — must have at least one real email ─────────
    if not clean_emails:
        return None

    display_url = urlparse(base_url).netloc or raw
    return {
        "url":     display_url,
        "emails":  ", ".join(sorted(clean_emails)),
        "phones":  ", ".join(_dedup_phones(phones)) if phones else "",
        "socials": ", ".join(f"{k}: {v}" for k, v in sorted(socials.items())),
        "city":    city,
    }
