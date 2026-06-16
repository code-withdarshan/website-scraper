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

# Playwright is optional — used only as a fallback for JS-rendered sites
# where the email isn't in the raw HTML the server sends
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

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
DEFAULT_TIMEOUT = 20  # was 12 — more forgiving for slow servers
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
    """Return True if robots.txt allows fetching this URL with our UA.

    Set SCRAPER_IGNORE_ROBOTS=1 to bypass the robots.txt check entirely.
    """
    import os
    if os.environ.get("SCRAPER_IGNORE_ROBOTS") == "1":
        return True
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


# ── Playwright fallback — for JS-rendered sites ────────────────────
# Many modern sites (Vue/React/custom SPAs) inject the contact email into the
# DOM via JavaScript after page load. `requests` only sees the raw server HTML,
# so the email is invisible. Playwright runs a real browser and returns the
# DOM after JS executes. Used only when the normal pipeline finds no email.
#
# Memory: Chromium uses ~300-400 MB per running instance. To avoid OOM crashes
# on Streamlit Cloud (~1 GB free tier) when many worker threads need JS rendering
# at once, we (a) cap concurrent renders globally with a semaphore, and
# (b) start + stop Playwright per call so no Chromium is left running idle.
_RENDER_SEMAPHORE = threading.Semaphore(1)  # max 1 concurrent Chromium — bounds peak memory


def _scraper_chrome_profile_dir():
    """Return path to the dedicated scraper Chrome profile. Created on first use."""
    import os
    from pathlib import Path
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        profile = Path(base) / "scraper-chrome-profile"
    else:
        profile = Path.home() / ".scraper-chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _is_captcha_challenge(page) -> bool:
    """Detect whether the page is currently blocked by a CAPTCHA / challenge.

    Tries to distinguish "this page HAS a reCAPTCHA somewhere" (e.g. on a
    contact form) from "this page IS a CAPTCHA challenge blocking content".
    """
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    challenge_titles = (
        "just a moment", "attention required", "verify you are human",
        "checking your browser", "cloudflare", "access denied",
        "please verify you are a human",
    )
    if any(kw in title for kw in challenge_titles):
        return True
    # Cloudflare / Turnstile / generic challenge containers
    blocking_selectors = (
        "#challenge-form", ".challenge-form", ".cf-challenge",
        ".cf-browser-verification", "#cf-challenge-running",
        "iframe[src*='challenges.cloudflare.com']",
    )
    for sel in blocking_selectors:
        try:
            if page.query_selector(sel):
                return True
        except Exception:
            pass
    return False


def _wait_for_captcha_solved(page, timeout_sec: int = 90) -> bool:
    """If a CAPTCHA is blocking the page, inject a banner and poll until solved.

    Returns True if the page is clear (no CAPTCHA), False on timeout.
    Only used in Local-Chrome mode where the user can see and solve it.
    """
    if not _is_captcha_challenge(page):
        return True  # No CAPTCHA, proceed
    log.info("CAPTCHA detected on %s — waiting up to %ss for manual solving", page.url, timeout_sec)
    # Inject a visible banner so the user knows the scraper is waiting
    try:
        page.evaluate(
            """
            (() => {
              if (document.getElementById('__scraper_wait_banner')) return;
              const d = document.createElement('div');
              d.id = '__scraper_wait_banner';
              d.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#fbbf24;color:#000;padding:14px;text-align:center;font:bold 16px system-ui,sans-serif;z-index:2147483647;box-shadow:0 2px 8px rgba(0,0,0,.3)';
              d.textContent = 'Scraper paused — please solve the CAPTCHA below. Scraping will continue automatically once the real page loads.';
              document.body.appendChild(d);
            })();
            """
        )
    except Exception:
        pass
    # Poll every 2s until the challenge is gone
    elapsed = 0
    while elapsed < timeout_sec:
        time.sleep(2)
        elapsed += 2
        try:
            if not _is_captcha_challenge(page):
                log.info("CAPTCHA cleared after %ss on %s", elapsed, page.url)
                return True
        except Exception:
            # Page may have navigated; assume clear
            return True
    log.warning("CAPTCHA still present after %ss — giving up on %s", timeout_sec, page.url)
    return False


def _fetch_rendered_local_chrome(url: str, timeout_ms: int = 90000) -> str | None:
    """Open URL in a visible Chrome window using a persistent profile.

    The dedicated profile means the user can log into Google (or anywhere
    else) once, and their session persists across scrapes. Used when the
    SCRAPER_LOCAL_CHROME=1 env var is set. Falls back gracefully if Chrome
    or Playwright can't launch — returns None.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = strip_tracking_params(url)
    if not _is_safe_url(url) or not _allowed_by_robots(url):
        return None
    with _RENDER_SEMAPHORE:
        pw = None
        ctx = None
        try:
            pw = _sync_playwright().start()
            profile_dir = str(_scraper_chrome_profile_dir())
            launch_kwargs = dict(
                user_data_dir=profile_dir,
                headless=False,
                args=["--no-default-browser-check", "--no-first-run", "--disable-blink-features=AutomationControlled"],
            )
            # Prefer the user's installed Chrome (more authentic fingerprint than
            # bundled Chromium). Fall back to Chromium if Chrome isn't installed.
            try:
                ctx = pw.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            except Exception:
                ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="load")
            # If a CAPTCHA is blocking, give the user 90s to solve it manually
            _wait_for_captcha_solved(page, timeout_sec=90)
            # Now wait briefly for mailto links (post-CAPTCHA hydration)
            try:
                page.wait_for_selector('a[href^="mailto:"]', timeout=3000)
            except Exception:
                pass
            return page.content()
        except Exception as exc:
            log.debug("Local Chrome render failed for %s: %s", url, exc)
            return None
        finally:
            if ctx:
                try: ctx.close()
                except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass


def _fetch_rendered(url: str, timeout_ms: int = 12000) -> str | None:
    """Fetch a URL after JavaScript executes. Returns rendered HTML or None.

    Env-var controls:
      - SCRAPER_SKIP_JS=1     → skip JS rendering entirely (Conservative mode).
      - SCRAPER_LOCAL_CHROME=1 → use a visible local Chrome with a persistent
        profile (Local Chrome mode) so the user can log in once and solve any
        CAPTCHAs manually. Local-only feature.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    import os
    if os.environ.get("SCRAPER_SKIP_JS") == "1":
        return None
    if os.environ.get("SCRAPER_LOCAL_CHROME") == "1":
        return _fetch_rendered_local_chrome(url, timeout_ms=90000)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = strip_tracking_params(url)
    if not _is_safe_url(url) or not _allowed_by_robots(url):
        return None

    # Bound peak memory: at most 2 Chromiums running anywhere in the process
    with _RENDER_SEMAPHORE:
        pw = None
        browser = None
        try:
            pw = _sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            ctx = browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            # `load` waits for the window.load event — slower than
            # `domcontentloaded` but ~3x faster than `networkidle`, and it
            # catches SPAs whose footer email is injected during initial
            # hydration (which often finishes after DOMContentLoaded).
            page.goto(url, timeout=timeout_ms, wait_until="load")
            # Then give the page up to 4s to inject the mailto element. On
            # heavy SPAs this is the difference between getting the email
            # and missing it entirely.
            try:
                page.wait_for_selector('a[href^="mailto:"]', timeout=4000)
            except Exception:
                pass
            return page.content()
        except Exception as exc:
            log.debug("Rendered fetch failed for %s: %s", url, exc)
            return None
        finally:
            # Tear down EVERYTHING so memory is freed before next call
            if browser:
                try: browser.close()
                except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass


# ── Email extraction ────────────────────────────────────────────────
def _decode_cfemail(hex_blob: str) -> str | None:
    """
    Decode a Cloudflare email-protection 'data-cfemail' value.
    First byte is the XOR key; remaining bytes are XOR-encoded chars.
    """
    try:
        if len(hex_blob) < 4 or len(hex_blob) % 2 != 0:
            return None
        key = int(hex_blob[:2], 16)
        out = bytes(int(hex_blob[i:i + 2], 16) ^ key
                    for i in range(2, len(hex_blob), 2)).decode("ascii")
        return out if "@" in out and "." in out else None
    except Exception:
        return None


def _add_email(emails: set, raw: str) -> None:
    cleaned = _clean_email(raw)
    if EMAIL_RE.match(cleaned):
        emails.add(cleaned)


def _emails_from_soup(soup: BeautifulSoup, raw_html: str = "") -> set[str]:
    emails: set[str] = set()

    # 1. mailto: links
    for tag in soup.find_all("a", href=True):
        if tag["href"].lower().startswith("mailto:"):
            _add_email(emails, tag["href"][7:].split("?")[0])

    # 2. Visible text regex
    for m in EMAIL_RE.finditer(soup.get_text(" ")):
        _add_email(emails, m.group())

    # 3. Raw HTML regex (catches JS-embedded etc.)
    if raw_html:
        for m in EMAIL_RE.finditer(raw_html):
            _add_email(emails, m.group())

    # 4. Cloudflare-protected emails (data-cfemail="…")
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        decoded = _decode_cfemail(tag["data-cfemail"])
        if decoded:
            _add_email(emails, decoded)
    if raw_html:
        for m in re.finditer(r'data-cfemail=["\']([a-fA-F0-9]+)["\']', raw_html):
            decoded = _decode_cfemail(m.group(1))
            if decoded:
                _add_email(emails, decoded)

    # 5. HTML-entity obfuscation (&#64; for @, &#46; for .)
    try:
        from html import unescape
        decoded_html = unescape(raw_html or soup.get_text(" "))
        for m in EMAIL_RE.finditer(decoded_html):
            _add_email(emails, m.group())
    except Exception:
        pass

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


def _extract_people(soup: BeautifulSoup, allow_full_page: bool = False) -> list[tuple[str, str]]:
    """
    Extract key people.
      • JSON-LD structured data (Organization.founder, .employee, .ceo) is always
        scanned — it's authoritative, low false-positive risk.
      • Text-pattern matching is restricted to About / Team / Leadership / Staff
        / Founder sections (by id or class). If no such section exists and
        `allow_full_page=False`, text matching is skipped entirely.
      • Pass `allow_full_page=True` only when the soup IS an About/Team page
        already (i.e. we navigated to a /about URL).
    """
    people: dict[str, str] = {}

    def _add(name: str, title: str):
        name = re.sub(r"\s+", " ", name).strip(" ,.;:")
        if not _is_real_name(name):
            return
        title = title.strip(" ,.;:").title() if title else ""
        key = name.lower()
        if key not in people or (title and "|" in people[key] and not people[key].split("|", 1)[1]):
            people[key] = f"{name}|{title}"

    # ── 1. JSON-LD (always, anywhere on page — authoritative) ────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        for node in _walk_jsonld_objects(data):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if t == "Person" and node.get("name"):
                _add(str(node["name"]), str(node.get("jobTitle", "") or ""))
            for role_key in ("founder", "founders", "employee", "ceo", "owner"):
                for person in _as_list(node.get(role_key)):
                    if isinstance(person, dict) and person.get("name"):
                        title = person.get("jobTitle") or role_key.capitalize()
                        _add(str(person["name"]), str(title))
                    elif isinstance(person, str):
                        _add(person, role_key.capitalize())

    # ── 2. Text patterns — STRICTLY only About/Team sections ─────
    about_team_sections = (
        soup.find_all(class_=re.compile(r"\b(team|founder|about|leadership|staff)\b", re.I)) +
        soup.find_all(id=re.compile(r"\b(team|founder|about|leadership|staff)\b", re.I))
    )

    if about_team_sections:
        targets = about_team_sections
    elif allow_full_page:
        # Caller asserted this is the About page → whole page is fair game
        targets = [soup]
    else:
        # No about/team section on this page → don't scan text at all
        targets = []

    for region in targets:
        text = region.get_text(" ", strip=True)
        for m in TITLE_THEN_NAME_RE.finditer(text):
            full = m.group(0)
            name = m.group(1)
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

    out = []
    for entry in people.values():
        n, t = entry.split("|", 1)
        out.append(f"{n} ({t})" if t else n)
    return [(p,) for p in out]


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


# ── Business niche / category detection ────────────────────────────
# Schema.org LocalBusiness subtypes mapped to friendly labels
SCHEMA_TYPE_TO_NICHE = {
    "dentist":               "Dentist",
    "dentistoffice":         "Dentist",
    "physician":             "Doctor / Medical",
    "medicalclinic":         "Doctor / Medical",
    "medicalorganization":   "Doctor / Medical",
    "hospital":              "Doctor / Medical",
    "pharmacy":              "Pharmacy",
    "optician":              "Optometrist",
    "veterinarycare":        "Veterinarian",
    "legalservice":          "Lawyer / Legal",
    "attorney":              "Lawyer / Legal",
    "notary":                "Lawyer / Legal",
    "realestateagent":       "Real Estate",
    "restaurant":            "Restaurant / Food",
    "cafeorcoffeeshop":      "Restaurant / Food",
    "fastfoodrestaurant":    "Restaurant / Food",
    "bakery":                "Restaurant / Food",
    "bar":                   "Restaurant / Food",
    "barorpub":              "Restaurant / Food",
    "architectoffice":       "Architect",
    "architect":             "Architect",
    "autorepair":            "Auto Repair",
    "autobodyshop":          "Auto Repair",
    "autopartsstore":        "Auto Parts",
    "autodealer":            "Car Dealer",
    "automotivebusiness":    "Auto / Automotive",
    "plumber":               "Plumber",
    "electrician":           "Electrician",
    "roofingcontractor":     "Roofer",
    "hvacbusiness":          "HVAC",
    "beautysalon":           "Salon / Spa",
    "hairsalon":             "Salon / Spa",
    "dayspa":                "Salon / Spa",
    "nailsalon":             "Salon / Spa",
    "accountingservice":     "Accountant",
    "financialservice":      "Financial Services",
    "insuranceagency":       "Insurance",
    "school":                "Education / School",
    "preschool":             "Education / School",
    "highschool":            "Education / School",
    "collegeoruniversity":   "Education / School",
    "educationalorganization":"Education / School",
    "hotel":                 "Hotel / Lodging",
    "lodgingbusiness":       "Hotel / Lodging",
    "bedandbreakfast":       "Hotel / Lodging",
    "resort":                "Hotel / Lodging",
    "generalcontractor":     "Construction",
    "housekeeper":           "Cleaning Services",
    "cleaning":              "Cleaning Services",
    "exerciseplaceholder":   "Fitness / Gym",
    "healthclub":            "Fitness / Gym",
    "gym":                   "Fitness / Gym",
    "sportsactivitylocation":"Fitness / Gym",
    "petstore":              "Pet Services",
    "funeralservices":       "Funeral Services",
    "tattooparlor":          "Tattoo / Body Art",
    "movietheater":          "Entertainment",
    "stadiumorarena":        "Entertainment",
    "casino":                "Entertainment",
    "store":                 "Retail",
    "shoppingcenter":        "Retail",
    "groceryorsupermarket":  "Grocery",
    "electricalservicelocator":"Electrician",
    "softwareapplication":   "Tech / SaaS",
}

# Keyword-based niche scoring — used when no schema.org type is present
NICHE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Dentist":           ("dentist", "dental", "tooth", "teeth", "cavity", "crown", "implant",
                          "orthodontic", "invisalign", "veneer", "hygienist", "dds", "dmd",
                          "root canal", "wisdom tooth", "smile", "oral surgery"),
    "Doctor / Medical":  ("physician", "doctor", "clinic", "patient", "medical practice",
                          "primary care", "specialist", "diagnosis", "appointment",
                          "healthcare", "general practitioner", "internal medicine"),
    "Veterinarian":      ("veterinarian", "veterinary", "vet clinic", "animal hospital",
                          "puppy", "kitten", "pet care", "spay", "neuter"),
    "Lawyer / Legal":    ("attorney", "lawyer", "law firm", "litigation", "esquire", "esq.",
                          "legal counsel", "law office", "criminal defense", "personal injury",
                          "divorce attorney"),
    "Real Estate":       ("realtor", "real estate", "mls", "homes for sale", "buyer agent",
                          "seller agent", "listing", "property for sale", "open house",
                          "real estate agent", "brokerage"),
    "Restaurant / Food": ("restaurant", "menu", "reservation", "cuisine", "chef", "bistro",
                          "cafe", "diner", "takeout", "delivery menu", "wine list",
                          "appetizer", "entree", "dessert menu"),
    "Architect":         ("architect", "architecture", "architectural", "aia member",
                          "design firm", "building design", "residential design",
                          "commercial architecture", "blueprint", "schematic design"),
    "Auto Repair":       ("auto repair", "mechanic", "automotive service", "transmission",
                          "brake repair", "engine repair", "oil change", "muffler",
                          "tire rotation", "diagnostics"),
    "Car Dealer":        ("car dealer", "used car", "new car", "auto sales", "test drive",
                          "trade-in", "financing", "pre-owned", "dealership", "vin"),
    "Plumber":           ("plumber", "plumbing", "drain cleaning", "leak repair",
                          "water heater", "sewer", "faucet", "burst pipe", "sump pump"),
    "Electrician":       ("electrician", "electrical service", "wiring", "circuit breaker",
                          "panel upgrade", "outlet", "voltage", "rewiring"),
    "Roofer":            ("roofer", "roofing", "shingles", "gutter", "roof repair",
                          "roof replacement", "asphalt shingle", "metal roof"),
    "HVAC":              ("hvac", "heating", "cooling", "air conditioning", "ac repair",
                          "furnace", "heat pump", "thermostat", "ductwork"),
    "Salon / Spa":       ("salon", "spa", "haircut", "barber", "stylist", "massage",
                          "facial", "manicure", "pedicure", "waxing", "blowout"),
    "Accountant":        ("accountant", "accounting firm", "cpa", "bookkeeping",
                          "tax preparation", "tax planning", "audit service", "payroll"),
    "Photographer":      ("photographer", "photography", "portrait session", "wedding photo",
                          "headshot", "engagement shoot", "family portrait", "boudoir"),
    "Fitness / Gym":     ("gym", "fitness center", "personal trainer", "yoga studio",
                          "pilates", "crossfit", "workout", "bootcamp", "spinning class"),
    "Education / School":("school", "academy", "tutor", "curriculum", "teacher", "education",
                          "course catalog", "enroll", "tuition"),
    "Hotel / Lodging":   ("hotel", "resort", "lodge", "inn", "guest room", "amenities",
                          "check-in", "book a room", "rate per night", "bed and breakfast"),
    "Construction":      ("construction", "general contractor", "remodeling", "renovation",
                          "home improvement", "building contractor"),
    "Cleaning Services": ("cleaning service", "house cleaning", "janitorial", "maid service",
                          "office cleaning", "deep clean"),
    "Landscaping":       ("landscaping", "lawn care", "garden design", "tree service",
                          "mulch", "irrigation", "sod"),
    "Insurance":         ("insurance", "policy", "premium", "coverage", "auto insurance",
                          "home insurance", "life insurance", "claim"),
    "Pet Services":      ("dog walker", "pet grooming", "kennel", "pet boarding",
                          "doggy daycare", "pet sitter"),
    "Funeral Services":  ("funeral", "cremation", "memorial service", "obituary", "burial",
                          "funeral home"),
    "Tech / SaaS":       ("saas", "api", "developer", "developers", "documentation",
                          "software platform", "integration", "sdk", "webhook",
                          "open source", "rest api", "developer tools", "no-code"),
    "Fintech / Payments":("payments", "payment processing", "fintech", "merchant",
                          "subscription billing", "financial infrastructure",
                          "fraud prevention", "billing platform", "credit card processing",
                          "stripe", "paypal", "ach"),
    "Agency / Marketing":("marketing agency", "advertising agency", "seo service",
                          "creative agency", "branding agency", "social media management",
                          "ppc", "lead generation"),
    "E-commerce / Retail":("add to cart", "buy now", "free shipping", "checkout",
                          "shopping cart", "in stock", "out of stock", "product catalog"),
    "Travel Agency":     ("travel agency", "tour package", "vacation package", "guided tour",
                          "itinerary", "destination"),
}


def _detect_niche(soup: BeautifulSoup, raw_html: str) -> str:
    # ── 1. Schema.org @type (highest confidence) ─────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        for node in _walk_jsonld_objects(data):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            for tname in (t if isinstance(t, list) else [t]):
                if isinstance(tname, str):
                    label = SCHEMA_TYPE_TO_NICHE.get(tname.lower())
                    if label:
                        return label

    # ── 2. Keyword scoring with weighted page zones ──────────────
    # High-signal zone: <title>, <h1>, meta description, meta keywords, og:description.
    # A site is much more likely to be ABOUT a niche if its title/H1 says so —
    # body text alone is misleading (e.g. a template marketplace listing medical templates).
    high_text = []
    if (t := soup.find("title")) and t.string:
        high_text.append(t.string)
    for h1 in soup.find_all("h1"):
        high_text.append(h1.get_text(" ", strip=True))
    for meta in soup.find_all("meta"):
        name = (meta.get("name", "") or meta.get("property", "") or "").lower()
        if name in ("description", "keywords", "og:description", "og:title"):
            high_text.append(meta.get("content", "") or "")
    high_blob = " ".join(high_text).lower()

    body_blob = re.sub(r"\s+", " ", soup.get_text(" ", strip=True).lower())
    if not body_blob:
        return ""

    best_niche = ""
    best_total = 0
    best_high  = 0
    for niche, keywords in NICHE_KEYWORDS.items():
        hi_hits = sum(1 for kw in keywords if kw in high_blob)
        lo_hits = sum(1 for kw in keywords if kw in body_blob)
        # Weighted total: each title/meta match counts as 3 body matches
        total = hi_hits * 3 + lo_hits
        if total > best_total:
            best_total = total
            best_high  = hi_hits
            best_niche = niche

    # Confidence rules:
    #   • at least 1 keyword in title/h1/meta AND total ≥ 3   → confident
    #   • OR very strong body signal (no high-zone hit but ≥ 6 in body)
    if (best_high >= 1 and best_total >= 3) or best_total >= 6:
        return best_niche
    return ""


# ── Tech-stack detection ───────────────────────────────────────────
# Pattern → display name. First match wins inside each category to keep
# the output short (e.g. "WordPress" without all 50 plugins).
# Order: CMS / site builder → e-commerce → JS framework → analytics
TECH_FINGERPRINTS: list[tuple[str, re.Pattern]] = [
    # ── CMS / site builders ─────────────────────────────────────
    # WordPress — require actual WP file paths, not just the word "wordpress"
    # (which appears in many sites' docs)
    ("WordPress",     re.compile(r"/wp-(?:content|includes|json)/|wp-emoji-release|wp-block-library", re.I)),
    ("Wix",           re.compile(r"static\.wixstatic\.com|_wix|wix\.com/_partials|wixCIDX", re.I)),
    ("Squarespace",   re.compile(r"static1\.squarespace\.com|squarespace-cdn|Static\.SQUARESPACE", re.I)),
    ("Webflow",       re.compile(r"data-wf-page|webflow\.com|wf-loaded|wf-domain", re.I)),
    ("Shopify",       re.compile(r"cdn\.shopify\.com|shopify\.com|Shopify\.theme|shopify-section", re.I)),
    ("Drupal",        re.compile(r"drupal-settings-json|sites/default/files|Drupal\.settings", re.I)),
    ("Joomla",        re.compile(r"/components/com_|joomla\!|/templates/.+joomla", re.I)),
    ("Ghost",         re.compile(r"ghost\.io|generator.*ghost|ghost-version", re.I)),
    ("Magento",       re.compile(r"Magento_|mage/cookies|magento-init", re.I)),
    ("HubSpot CMS",   re.compile(r"hs-scripts\.com|js\.hsforms\.net|hubspot\.com/_hcms", re.I)),
    ("Weebly",        re.compile(r"weebly\.com|cdn2\.editmysite", re.I)),
    ("GoHighLevel",   re.compile(r"msgsndr\.com|gohighlevel|highlevel\.app", re.I)),
    ("GoDaddy Studio",re.compile(r"img1\.wsimg\.com|godaddysites|secureserver\.net/css", re.I)),
    ("Duda",          re.compile(r"irp\.cdn-website\.com|lirp\.cdn-website|duda\.co", re.I)),
    ("Framer",        re.compile(r"framerusercontent\.com|framer\.com/m/", re.I)),
    ("Notion",        re.compile(r"notion\.so|super\.so/notion", re.I)),
    ("Carrd",         re.compile(r"carrd\.co", re.I)),
    ("Bubble.io",     re.compile(r"bubble\.io|run\.bubbleapps", re.I)),

    # ── E-commerce ──────────────────────────────────────────────
    # WooCommerce — require actual WC plugin/asset paths or body classes,
    # not just the word "woocommerce" (which payment-processor docs mention)
    ("WooCommerce",   re.compile(r"wp-content/plugins/woocommerce|woocommerce-(?:page|js|cart|checkout|product|loop|store|block)|wc-block-", re.I)),
    ("BigCommerce",   re.compile(r"bigcommerce\.com|cdn\d?\.bcapps", re.I)),
    ("PrestaShop",    re.compile(r"prestashop|var\s+prestashop\s*=", re.I)),
    ("OpenCart",      re.compile(r"opencart|index\.php\?route=", re.I)),
    ("Salesforce Commerce", re.compile(r"demandware\.net|sfcc-cdn", re.I)),

    # ── JS frameworks ──────────────────────────────────────────
    ("Next.js",       re.compile(r"__NEXT_DATA__|/_next/static|next/router", re.I)),
    ("Nuxt",          re.compile(r"__NUXT__|/_nuxt/", re.I)),
    ("Gatsby",        re.compile(r"gatsby-|/page-data\.json|___gatsby", re.I)),
    ("Astro",         re.compile(r"astro-island|astro-slot", re.I)),
    ("React",         re.compile(r"data-reactroot|data-reactid|react-dom", re.I)),
    ("Vue.js",        re.compile(r"data-v-[0-9a-f]{8}|__vue__|/vue\.runtime", re.I)),
    ("Angular",       re.compile(r"ng-version=|_nghost-|ng-app=|angular\.js", re.I)),
    ("Svelte",        re.compile(r"svelte-[a-z0-9]{6}|__svelte", re.I)),

    # ── Forms / interactive ─────────────────────────────────────
    ("Mailchimp",     re.compile(r"mc\.us\d+\.list-manage\.com|mailchimp\.com/mc", re.I)),
    ("HubSpot Forms", re.compile(r"js\.hsforms\.net|hbspt\.forms\.create", re.I)),
    ("Typeform",      re.compile(r"typeform\.com/embed|tf-v1-widget", re.I)),
    ("Calendly",      re.compile(r"calendly\.com/assets|calendly\.com/embed", re.I)),

    # ── Analytics / tag managers ────────────────────────────────
    ("Google Analytics", re.compile(r"google-analytics\.com|gtag\(.*G-|ga\(.*UA-", re.I)),
    ("GTM",              re.compile(r"googletagmanager\.com/gtm", re.I)),
    ("Meta Pixel",       re.compile(r"connect\.facebook\.net/.*fbevents|fbq\(.*track", re.I)),
    ("Hotjar",           re.compile(r"static\.hotjar\.com|hotjar-", re.I)),

    # ── CDN / infra ─────────────────────────────────────────────
    ("Cloudflare",    re.compile(r"cdn-cgi/|__cf_email__|cloudflare-static", re.I)),
    ("jQuery",        re.compile(r"jquery[.\-]\d", re.I)),
    ("Bootstrap",     re.compile(r"bootstrap(?:\.min)?\.css|bootstrap@", re.I)),
    ("Tailwind",      re.compile(r"tailwindcss|cdn\.tailwindcss\.com", re.I)),
    ("Elementor",     re.compile(r"elementor|elementskit", re.I)),
]

# Category each fingerprint belongs to — we keep at most one CMS + one e-commerce
# + one framework etc. so the column doesn't explode
TECH_CATEGORY = {
    "WordPress": "cms", "Wix": "cms", "Squarespace": "cms", "Webflow": "cms",
    "Shopify": "cms", "Drupal": "cms", "Joomla": "cms", "Ghost": "cms",
    "Magento": "cms", "HubSpot CMS": "cms", "Weebly": "cms",
    "GoHighLevel": "cms", "GoDaddy Studio": "cms", "Duda": "cms",
    "Framer": "cms", "Notion": "cms", "Carrd": "cms", "Bubble.io": "cms",
    "WooCommerce": "ecom", "BigCommerce": "ecom", "PrestaShop": "ecom",
    "OpenCart": "ecom", "Salesforce Commerce": "ecom",
    "Next.js": "fw", "Nuxt": "fw", "Gatsby": "fw", "Astro": "fw",
    "React": "fw", "Vue.js": "fw", "Angular": "fw", "Svelte": "fw",
}


def _detect_tech(soup: BeautifulSoup, raw_html: str) -> list[str]:
    """Return a small ordered list of detected technologies for the page."""
    found: list[str] = []
    seen_cats: set[str] = set()
    found_lower_blob = ""   # lowercased "found so far" for substring dedup

    # 1. <meta name="generator"> — most authoritative
    gen = soup.find("meta", attrs={"name": "generator"})
    if gen and gen.get("content"):
        g = gen["content"].strip()[:60]
        found.append(g)
        # Mark category to prevent the bare-name duplicate
        for cat_name, cat in TECH_CATEGORY.items():
            if cat_name.lower() in g.lower():
                seen_cats.add(cat)
        found_lower_blob = g.lower()

    # 2. Fingerprint scan
    for name, pattern in TECH_FINGERPRINTS:
        cat = TECH_CATEGORY.get(name)
        # Skip if we already have one in this category (keeps list short)
        if cat and cat in seen_cats:
            continue
        # Skip if its name already appears in any earlier entry
        if name.lower() in found_lower_blob:
            continue
        if pattern.search(raw_html):
            found.append(name)
            found_lower_blob += " " + name.lower()
            if cat:
                seen_cats.add(cat)

    return found[:5]


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
    # Key people: only extract from about/team SECTIONS on the homepage
    # (not from the full page — too many false positives)
    people        = list(_extract_people(home_soup, allow_full_page=False))
    tech          = _detect_tech(home_soup, home_html)
    niche         = _detect_niche(home_soup, home_html)

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
            # Contact page: still only labeled about/team sections — never the
            # whole contact page (it's a contact page, not a team page)
            people.extend(_extract_people(c_soup, allow_full_page=False))

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
                # About page: whole page is fair game — it IS the about page
                people.extend(_extract_people(a_soup, allow_full_page=True))
        clean_emails = [e for e in emails if not _is_junk_email(e)]

    # ── JS-rendering fallback (only if no email found yet) ───────
    # Modern sites often inject the footer email via JavaScript. Re-fetch
    # the homepage (and contact page if any) with a real browser.
    if not clean_emails and _PLAYWRIGHT_AVAILABLE:
        for js_url in [base_url, contact_url]:
            if not js_url:
                continue
            rendered = _fetch_rendered(js_url)
            if not rendered:
                continue
            r_soup = BeautifulSoup(rendered, "html.parser")
            emails  |= _emails_from_soup(r_soup, rendered)
            phones  |= _phones_from_soup(r_soup)
            for k, v in _socials_from_soup(r_soup, rendered).items():
                socials.setdefault(k, v)
            if not city:    city    = _extract_city(r_soup, rendered)
            if not company: company = _extract_company(r_soup)
            clean_emails = [e for e in emails if not _is_junk_email(e)]
            if clean_emails:
                break

    # ── WHOIS fallback if no company name found yet ──────────────
    domain = urlparse(base_url).netloc or raw
    if not company:
        company = _whois_company(domain)

    # ── Final filter — must have at least one real email ─────────
    if not clean_emails:
        return None

    # Dedup people + drop any "name" that matches the company itself
    seen_people = []
    seen_keys: set[str] = set()
    company_norm = re.sub(r"[^\w]+", "", (company or "")).lower()
    for p in people:
        s = p[0] if isinstance(p, tuple) else str(p)
        name_only = re.sub(r"\s*\(.*\)\s*$", "", s)
        key = name_only.lower()
        if key in seen_keys:
            continue
        # Skip if the "person name" is actually the company name
        if company_norm and re.sub(r"[^\w]+", "", name_only).lower() == company_norm:
            continue
        seen_keys.add(key)
        seen_people.append(s)

    return {
        "url":          domain,
        "company":      company,
        "niche":        niche,
        "people":       ", ".join(seen_people[:5]),
        "emails":       ", ".join(sorted(clean_emails)),
        "phones":       ", ".join(_dedup_phones(phones)) if phones else "",
        "socials":      ", ".join(f"{k}: {v}" for k, v in sorted(socials.items())),
        "city":         city,
        "language":     language,
        "tech":         ", ".join(tech),
        "contact_form": "yes" if contact_form else "",
    }
