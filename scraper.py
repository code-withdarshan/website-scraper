import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)

JUNK_EMAIL_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "email.com",
    "test.com", "placeholder.com", "wix.com", "wixpress.com",
    "squarespace.com", "wordpress.com",
}

# Any email whose domain ends with one of these suffixes is junk
JUNK_DOMAIN_SUFFIXES = (
    ".sentry.io", "sentry.io",
    ".wixpress.com", "wixpress.com",
    ".amazonaws.com",
    ".cloudfront.net",
    ".sendgrid.net",
    ".mailchimp.com",
    ".klaviyo.com",
    ".hubspot.com",
)

# Link text / href patterns that indicate a contact page
CONTACT_LINK_RE = re.compile(
    r"contact|contact[-_]?us|get[-_]?in[-_]?touch|reach[-_]?us|kontakt",
    re.IGNORECASE,
)

JSONLD_CITY_RE = re.compile(
    r'"addressLocality"\s*:\s*"([^"]{2,50})"', re.IGNORECASE
)
META_GEO_RE = re.compile(r'geo\.placename', re.IGNORECASE)
CITY_LABEL_RE = re.compile(
    r"(?:city|location|address|based in|serving|headquartered)[:\s]+([A-Z][a-zA-Z\s]{2,30})",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_email(raw: str) -> str:
    """URL-decode, strip whitespace/punctuation noise from an email candidate."""
    return unquote(raw).strip().strip(".,;\"'<>()[]").lower()


def _is_junk_email(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    if domain in JUNK_EMAIL_DOMAINS:
        return True
    if any(domain == s.lstrip(".") or domain.endswith(s) for s in JUNK_DOMAIN_SUFFIXES):
        return True
    # Skip image/asset filenames mistaken for emails
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp|css|js|woff|ttf|eot)$", domain):
        return True
    # Skip hex-only local parts (tracking IDs like Sentry DSNs)
    local = email.split("@")[0]
    if re.fullmatch(r"[0-9a-f]{20,}", local):
        return True
    return False


def _fetch(url: str, timeout: int = 12) -> str | None:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


def _add_email(emails: set, raw: str) -> None:
    """Clean and add one email candidate to the set, discarding invalid ones."""
    cleaned = _clean_email(raw)
    if EMAIL_RE.match(cleaned):
        emails.add(cleaned)


def _emails_from_footer(soup: BeautifulSoup, raw_html: str) -> set[str]:
    emails: set[str] = set()

    footer = soup.find("footer")
    if not footer:
        footer = (
            soup.find(attrs={"role": "contentinfo"})
            or soup.find(id=re.compile(r"footer", re.I))
            or soup.find(class_=re.compile(r"footer", re.I))
        )

    target = footer if footer else soup

    for tag in target.find_all("a", href=True):
        href = tag["href"]
        if href.lower().startswith("mailto:"):
            _add_email(emails, href[7:].split("?")[0])

    for match in EMAIL_RE.finditer(target.get_text(" ")):
        _add_email(emails, match.group())

    return emails


def _emails_from_soup(soup: BeautifulSoup) -> set[str]:
    emails: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.lower().startswith("mailto:"):
            _add_email(emails, href[7:].split("?")[0])
    for match in EMAIL_RE.finditer(soup.get_text(" ")):
        _add_email(emails, match.group())
    return emails


def _find_contact_url(soup: BeautifulSoup, base_url: str) -> str | None:
    """Return the absolute URL of the contact page, or None."""
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        text = tag.get_text(strip=True)
        if CONTACT_LINK_RE.search(href) or CONTACT_LINK_RE.search(text):
            absolute = urljoin(base_url, href)
            # Stay on the same domain
            if urlparse(absolute).netloc == urlparse(base_url).netloc:
                return absolute
    return None


def _extract_city(soup: BeautifulSoup, raw_html: str) -> str:
    # 1. JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        m = JSONLD_CITY_RE.search(script.string or "")
        if m:
            return m.group(1).strip()

    # 2. meta geo.placename
    for meta in soup.find_all("meta"):
        name = meta.get("name", "") or meta.get("property", "")
        if META_GEO_RE.search(name):
            content = meta.get("content", "").strip()
            if content:
                return content

    # 3. Microdata addressLocality
    tag = soup.find(attrs={"itemprop": "addressLocality"})
    if tag:
        city = (tag.get_text() or tag.get("content", "")).strip()
        if city:
            return city

    # 4. Text label patterns
    text = soup.get_text(" ", strip=True)
    m = CITY_LABEL_RE.search(text)
    if m:
        candidate = m.group(1).strip().rstrip(".,;")
        if 2 < len(candidate) < 40:
            return candidate

    # 5. Raw HTML fallback
    m = JSONLD_CITY_RE.search(raw_html)
    if m:
        return m.group(1).strip()

    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_url(url: str) -> dict | None:
    raw = url.strip()
    if not raw:
        return None

    base_url = raw if raw.startswith(("http://", "https://")) else "https://" + raw

    # ── Step 1: load the homepage ────────────────────────────────
    home_html = _fetch(base_url)
    if home_html is None:
        return None

    home_soup = BeautifulSoup(home_html, "html.parser")

    # ── Step 2: emails from the entire homepage ──────────────────
    # (footer first for prioritisation, then the rest of the page,
    #  plus a raw-HTML regex sweep to catch obfuscated/encoded emails)
    emails: set[str] = _emails_from_footer(home_soup, home_html)
    emails |= _emails_from_soup(home_soup)
    for m in EMAIL_RE.finditer(home_html):
        _add_email(emails, m.group())

    # ── Step 3: city from homepage (JSON-LD / meta / microdata) ──
    city = _extract_city(home_soup, home_html)

    # ── Step 4: find & fetch the contact page ────────────────────
    contact_url = _find_contact_url(home_soup, base_url)
    if contact_url:
        contact_html = _fetch(contact_url)
        if contact_html:
            contact_soup = BeautifulSoup(contact_html, "html.parser")
            emails |= _emails_from_soup(contact_soup)
            for m in EMAIL_RE.finditer(contact_html):
                _add_email(emails, m.group())
            if not city:
                city = _extract_city(contact_soup, contact_html)

    # ── Filter junk ──────────────────────────────────────────────
    clean_emails = [e for e in emails if not _is_junk_email(e)]

    # Skip sites with no email found
    if not clean_emails:
        return None

    display_url = urlparse(base_url).netloc or raw

    return {
        "url": display_url,
        "emails": ", ".join(sorted(clean_emails)),
        "city": city,
    }
