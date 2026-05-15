"""
Email & Contact Scraper — Streamlit edition.
Re-uses scraper.py from the Flask version.

Run locally:
    streamlit run streamlit_app.py

Deploy:
    Push this repo to GitHub, then connect at https://share.streamlit.io
    Streamlit Cloud auto-installs requirements.txt and starts the app.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import openpyxl
import pandas as pd
import requests
import streamlit as st
import xlrd

from scraper import scrape_url, strip_tracking_params

# ── Page config ────────────────────────────────────────────────────
st.set_page_config(
    page_title="Email & Contact Scraper",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide default Streamlit chrome
st.markdown(
    """
    <style>
      #MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; }
      .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
      .stMetric { background:#f8fafc; border:1px solid #e2e8f0; border-radius:.75rem; padding:.75rem; }
      .hero {
        background: linear-gradient(135deg, #1d4ed8, #1e3a8a);
        color: white; padding: 1.5rem 2rem; border-radius: 1rem;
        margin-bottom: 1.5rem;
      }
      .hero h1 { margin:0; font-size:1.75rem; font-weight:800; }
      .hero p  { margin:.25rem 0 0; opacity:.85; font-size:.9rem; }
      .gsheet-tip {
        background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46;
        border-radius:.5rem; padding:.6rem .85rem; font-size:.82rem;
        margin-bottom:.75rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Session state ──────────────────────────────────────────────────
if "results"     not in st.session_state: st.session_state.results     = []
if "failed"      not in st.session_state: st.session_state.failed      = []
if "history"     not in st.session_state: st.session_state.history     = []
if "source_name" not in st.session_state: st.session_state.source_name = None

# ── Constants ──────────────────────────────────────────────────────
MAX_WORKERS = 8
MAX_URLS    = 500
HIST_MAX    = 5

# Public usage counter (abacus.jasoncameron.dev — free, no auth, no PII)
COUNTER_NS  = "email-scraper-darshan"   # unique to this app
COUNTER_KEY = "scrapes"
COUNTER_URL_BUMP = f"https://abacus.jasoncameron.dev/hit/{COUNTER_NS}/{COUNTER_KEY}"
COUNTER_URL_GET  = f"https://abacus.jasoncameron.dev/get/{COUNTER_NS}/{COUNTER_KEY}"


@st.cache_data(ttl=60, show_spinner=False)
def _get_counter() -> int | None:
    """Fetch current counter value. Cached 60s so we don't hammer the API."""
    try:
        r = requests.get(COUNTER_URL_GET, timeout=3)
        if r.ok:
            return int(r.json().get("value", 0))
    except Exception:
        pass
    return None


def _bump_counter() -> int | None:
    """Increment counter and return new value. Fail silently if offline."""
    try:
        r = requests.get(COUNTER_URL_BUMP, timeout=3)
        if r.ok:
            _get_counter.clear()   # invalidate cache so display refreshes
            return int(r.json().get("value", 0))
    except Exception:
        pass
    return None

WEBSITE_COL_RE = re.compile(
    r"^\s*(website|web\s*site|site|url|domain|homepage|web|link)s?\s*$", re.I
)
GSHEET_RE     = re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", re.I)
GSHEET_GID_RE = re.compile(r"[#?&]gid=(\d+)", re.I)
URL_LIKE_RE   = re.compile(
    r"^(https?://)?([a-z0-9][a-z0-9\-]*\.)+[a-z]{2,}(/.*)?$", re.I
)

# TLDs to skip by default — government, education, non-profit organisations
# Matches: .gov, .gov.uk, .gov.in, .edu, .edu.au, .ac.uk, .org, .org.uk, etc.
SKIP_TLD_RE = re.compile(r"(?:^|\.)(gov|edu|ac|org)(\.[a-z]{2,3})?$", re.IGNORECASE)


def _matched_skip_tld(url: str) -> str | None:
    """Return the matched TLD suffix (e.g. '.gov', '.edu.au') if URL should be skipped."""
    try:
        host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
        m = SKIP_TLD_RE.search(host)
        return m.group(0) if m else None
    except Exception:
        return None


# ── Smart column / URL extraction (same logic as Flask app.py) ─────
def _looks_like_url(v: str) -> bool:
    return bool(URL_LIKE_RE.match(v.strip()))


def _extract_urls_from_rows(rows):
    if not rows:
        return []
    header = rows[0]
    target_col = next(
        (i for i, c in enumerate(header) if WEBSITE_COL_RE.match(c or "")),
        None,
    )
    if target_col is None:
        cols = max(len(r) for r in rows)
        scores = [0] * cols
        for r in rows:
            for i, c in enumerate(r):
                if c and _looks_like_url(c):
                    scores[i] += 1
        if max(scores, default=0) >= 2:
            target_col = scores.index(max(scores))

    urls = []
    if target_col is not None:
        start = 1 if WEBSITE_COL_RE.match(header[target_col] or "") else 0
        for r in rows[start:]:
            if target_col < len(r):
                v = (r[target_col] or "").strip()
                if v and _looks_like_url(v):
                    urls.append(v)
    if not urls:
        for r in rows:
            for c in r:
                v = (c or "").strip()
                if v and _looks_like_url(v):
                    urls.append(v)
    return urls


def _csv_rows(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    return [list(row) for row in csv.reader(io.StringIO(text))]


def _xlsx_rows(data: bytes):
    wb = openpyxl.load_workbook(filename=io.BytesIO(data), data_only=True)
    out = []
    for s in wb.worksheets:
        for r in s.iter_rows(values_only=True):
            out.append([("" if c is None else str(c)).strip() for c in r])
    return out


def _xls_rows(data: bytes):
    wb = xlrd.open_workbook(file_contents=data)
    out = []
    for s in wb.sheets():
        for r in range(s.nrows):
            out.append([str(s.cell_value(r, c)).strip() for c in range(s.ncols)])
    return out


def _fetch_gsheet_csv(url: str) -> tuple[bytes | None, str | None]:
    """Returns (csv_bytes, sheet_title). Sheet title is read from Content-Disposition."""
    m = GSHEET_RE.search(url)
    if not m:
        return None, None
    sid = m.group(1)
    gid = (GSHEET_GID_RE.search(url) or [None, "0"])[1] if GSHEET_GID_RE.search(url) else "0"
    export_url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    try:
        r = requests.get(export_url, timeout=20, allow_redirects=True)
        r.raise_for_status()
        # Parse filename from Content-Disposition: 'attachment; filename="Sheet Name - Tab.csv"'
        title = None
        cd = r.headers.get("Content-Disposition", "")
        m2 = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        if m2:
            raw = m2.group(1).strip()
            # URL-decode and strip extension
            try:
                from urllib.parse import unquote as _uq
                raw = _uq(raw)
            except Exception:
                pass
            title = re.sub(r"\.csv$", "", raw, flags=re.I).strip()
        return r.content, title
    except Exception:
        return None, None


def _collect_urls(text_input: str, uploaded_file) -> tuple[list[str], str | None, str | None]:
    """
    Returns (urls, error, source_name).
    `source_name` is set when the input was a Google Sheet or uploaded file,
    so exports can be named after the source.
    """
    urls: list[str] = []
    source_name: str | None = None

    if uploaded_file is not None:
        fn = uploaded_file.name
        data = uploaded_file.read()
        # Strip extension for the source name
        source_name = re.sub(r"\.(csv|xls|xlsx)$", "", fn, flags=re.I).strip()
        fn_lower = fn.lower()
        try:
            if fn_lower.endswith(".csv"):
                urls = _extract_urls_from_rows(_csv_rows(data))
            elif fn_lower.endswith(".xlsx"):
                urls = _extract_urls_from_rows(_xlsx_rows(data))
            elif fn_lower.endswith(".xls"):
                urls = _extract_urls_from_rows(_xls_rows(data))
            else:
                return [], "Unsupported file type.", None
        except Exception as exc:
            return [], f"Could not parse file: {exc}", None
    if text_input:
        cands = [u.strip() for u in re.split(r"[,\n\r]+", text_input) if u.strip()]
        for c in cands:
            if GSHEET_RE.search(c):
                sheet_bytes, title = _fetch_gsheet_csv(c)
                if sheet_bytes is None:
                    return [], "Could not download Google Sheet. Make sure it's 'Anyone with the link can view'.", None
                urls.extend(_extract_urls_from_rows(_csv_rows(sheet_bytes)))
                if title and not source_name:
                    source_name = title
            else:
                urls.append(c)
    urls = [u.strip() for u in urls if u.strip()]
    return urls, None, source_name


def _safe_filename(name: str) -> str:
    """Strip filesystem-unsafe chars and collapse whitespace for a filename."""
    name = re.sub(r"[^\w\s.\-]", "", name).strip()
    name = re.sub(r"\s+", "-", name)
    return (name or "scrape-results")[:80]


def _normalize_url(u: str) -> str:
    """Lowercase host, drop trailing slash, www, and tracking params for dedup."""
    u = strip_tracking_params(u)
    parsed = urlparse(u if "://" in u else "https://" + u)
    host = (parsed.netloc or u).lower()
    if host.startswith("www."):
        host = host[4:]
    return (host + (parsed.path or "")).rstrip("/")


def _has_gmail(result: dict) -> bool:
    """True if any email in this row ends with @gmail.com or @googlemail.com."""
    emails = (result.get("emails") or "").lower()
    return "@gmail.com" in emails or "@googlemail.com" in emails


def _sort_gmail_first(results: list[dict]) -> list[dict]:
    """Stable sort: rows whose emails include any Gmail address go to the top."""
    return sorted(results, key=lambda r: 0 if _has_gmail(r) else 1)


def _estimate_time(unique_urls: list[str]) -> tuple[int, int]:
    """
    Rough wall-clock estimate in seconds + unique domain count.
    Assumes ~4s per URL, MAX_WORKERS in parallel, throttled to 1 req/sec per domain.
    """
    if not unique_urls:
        return 0, 0
    domains_count: dict[str, int] = {}
    for u in unique_urls:
        d = _normalize_url(u)
        domains_count[d] = domains_count.get(d, 0) + 1
    max_per_domain = max(domains_count.values())
    n = len(unique_urls)
    # Two bounding factors:
    #   a) worker capacity: n / MAX_WORKERS * 4s per URL
    #   b) per-domain serial scheduling: max_per_domain * 4s
    est = max(n / MAX_WORKERS * 4, max_per_domain * 4, 3)
    return int(est), len(domains_count)


# ── Sidebar filter (defined early so it's available before scrape logic) ──
st.sidebar.markdown("### ⚙️ Filters")
skip_noncommercial = st.sidebar.checkbox(
    "Skip non-commercial domains",
    value=True,
    help="When ON, .gov / .edu / .ac / .org (and country variants like .gov.uk, "
         ".edu.au) are filtered out before scraping. Turn OFF to scrape them too.",
)
st.sidebar.markdown("---")


# ── Hero ───────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="hero">
      <h1>📧 Email &amp; Contact Scraper</h1>
      <p>Pulls emails, phone numbers, social links &amp; city info from any website's footer, contact, or about page.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Input section ──────────────────────────────────────────────────
tab_paste, tab_upload = st.tabs(["📝 Paste URLs", "📂 Upload File"])

text_input = ""
uploaded_file = None

with tab_paste:
    st.markdown(
        '<div class="gsheet-tip"><b>Tip:</b> You can also paste a <b>Google Sheet URL</b> here — '
        'the app will find the <code>Website</code> column and scrape every URL in it. '
        'Sheet must be set to <em>"Anyone with the link can view"</em>.</div>',
        unsafe_allow_html=True,
    )
    text_input = st.text_area(
        label="Enter URLs (one per line or comma-separated) or a Google Sheet link",
        height=180,
        placeholder=(
            "https://example.com\n"
            "businesssite.com, anothersite.co.uk\n\n"
            "Or a Google Sheet link:\n"
            "https://docs.google.com/spreadsheets/d/..."
        ),
        label_visibility="visible",
        key="text_input",
    )

with tab_upload:
    uploaded_file = st.file_uploader(
        "Upload a CSV / XLS / XLSX file",
        type=["csv", "xls", "xlsx"],
        accept_multiple_files=False,
    )
    if uploaded_file:
        st.success(f"📄 {uploaded_file.name} ready to scrape")

col_a, col_b = st.columns([1, 5])
with col_a:
    scrape_clicked = st.button("🔍 Scrape", type="primary", use_container_width=True)


# ── Scrape ─────────────────────────────────────────────────────────
if scrape_clicked:
    raw_urls, err, source_name = _collect_urls(text_input, uploaded_file)
    if err:
        st.error(err)
    elif not raw_urls:
        st.error("No URLs detected in your input.")
    else:
        st.session_state.source_name = source_name
        # ── Dedup + cap ──────────────────────────────────────────
        # Normalize for dedup (treat www / non-www / trailing slash as same)
        seen: dict[str, str] = {}
        for u in raw_urls:
            key = _normalize_url(u)
            seen.setdefault(key, u)
        urls = list(seen.values())

        dupes = len(raw_urls) - len(urls)

        # ── Filter out .gov / .edu / .org / .ac if toggle is on ─
        skipped_tld: list[tuple[str, str]] = []
        if skip_noncommercial:
            kept = []
            for u in urls:
                tld = _matched_skip_tld(u)
                if tld:
                    skipped_tld.append((u, tld))
                else:
                    kept.append(u)
            urls = kept

        capped = max(0, len(urls) - MAX_URLS)
        if capped:
            urls = urls[:MAX_URLS]

        # ── Pre-flight info banners ─────────────────────────────
        # Build the list of side-banner messages dynamically
        warning_msgs = []
        if dupes:
            warning_msgs.append(f"**{dupes}** duplicate{'s' if dupes != 1 else ''} removed")
        if skipped_tld:
            # Group by TLD for a clean summary
            by_tld: dict[str, int] = {}
            for _, t in skipped_tld:
                by_tld[t] = by_tld.get(t, 0) + 1
            tld_summary = ", ".join(f"{n} `{t}`" for t, n in sorted(by_tld.items()))
            warning_msgs.append(f"**{len(skipped_tld)}** non-commercial skipped ({tld_summary})")
        if capped:
            warning_msgs.append(f"**{capped}** truncated (max {MAX_URLS})")

        info_cols = st.columns(3 if warning_msgs else 2)
        with info_cols[0]:
            st.info(f"📋 **{len(urls)}** unique URL{'s' if len(urls) != 1 else ''} to scrape")
        secs, domains = _estimate_time(urls)
        with info_cols[1]:
            mins = secs // 60
            time_str = f"~{mins}m {secs % 60}s" if mins else f"~{secs}s"
            st.info(f"⏱️ Estimated time: **{time_str}** across {domains} domain{'s' if domains != 1 else ''}")
        if warning_msgs:
            with info_cols[2]:
                st.warning("ℹ️ " + " · ".join(warning_msgs))

        # If filtering left us with nothing, stop here
        if not urls:
            st.error("All URLs were filtered out. Untick *Skip non-commercial domains* in the sidebar to include them.")
            st.stop()

        results, failed = [], []
        progress = st.progress(0.0, text=f"Scraping 0 / {len(urls)}…")
        ticker = st.empty()
        ticker_lines: list[str] = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(scrape_url, u): u for u in urls}
            done = 0
            for f in as_completed(futures):
                u = futures[f]
                done += 1
                try:
                    r = f.result()
                except Exception:
                    r = None
                if r:
                    results.append(r)
                    ticker_lines.insert(0, f"✅ **{u}** — {r['emails'][:80]}")
                else:
                    failed.append(u)
                    ticker_lines.insert(0, f"❌ **{u}** — no email")

                progress.progress(done / len(urls), text=f"Scraping {done} / {len(urls)}…")
                ticker.markdown("\n".join(ticker_lines[:15]))

        progress.empty()
        ticker.empty()

        # ── Post-processing: Gmail addresses first ────────────────
        results = _sort_gmail_first(results)
        gmail_count = sum(1 for r in results if _has_gmail(r))
        if gmail_count:
            st.success(
                f"✉️ {gmail_count} site{'s' if gmail_count != 1 else ''} "
                f"with Gmail address{'es' if gmail_count != 1 else ''} sorted to the top."
            )

        st.session_state.results = results
        st.session_state.failed  = failed

        # Bump public usage counter (no PII, just a tick per scrape submission)
        _bump_counter()

        # save to history
        st.session_state.history.insert(0, {
            "when":        datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count":       len(results),
            "failed":      len(failed),
            "results":     results,
            "failed_urls": failed,
            "source_name": source_name,
        })
        st.session_state.history = st.session_state.history[:HIST_MAX]


# ── Results ────────────────────────────────────────────────────────
results = st.session_state.results
failed  = st.session_state.failed

if results:
    st.markdown("---")
    # Stats
    total_emails = sum(len([e for e in (r["emails"] or "").split(",") if e.strip()]) for r in results)
    total_phones = sum(len([p for p in (r["phones"] or "").split(",") if p.strip()]) for r in results)
    total_cities = sum(1 for r in results if r["city"])

    total_companies = sum(1 for r in results if r.get("company"))
    total_forms     = sum(1 for r in results if r.get("contact_form"))
    total_people    = sum(1 for r in results if r.get("people"))
    total_niches    = len({r.get("niche") for r in results if r.get("niche")})

    m1, m2, m3, m4, m5, m6, m7, m8 = st.columns(8)
    m1.metric("Sites",      len(results))
    m2.metric("Emails",     total_emails)
    m3.metric("Phones",     total_phones)
    m4.metric("Companies",  total_companies)
    m5.metric("Niches",     total_niches)
    m6.metric("Key people", total_people)
    m7.metric("Cities",     total_cities)
    m8.metric("Has form",   total_forms)

    # Filter row — text search + niche dropdown
    fc1, fc2 = st.columns([3, 1])
    with fc1:
        q = st.text_input("🔎 Filter by URL, email, phone, social, or city",
                          value="", key="filter")
    with fc2:
        all_niches = sorted({r.get("niche") for r in results if r.get("niche")})
        niche_pick = st.selectbox(
            "Niche",
            options=["All"] + all_niches,
            key="niche_filter",
        )

    # Normalize results — make sure every row has every column, even if the result
    # was scraped before a new column (like "tech" or "niche") was added
    ALL_FIELDS = ["url", "company", "niche", "people", "emails", "phones",
                  "socials", "city", "language", "tech", "contact_form"]
    norm_results = [{f: r.get(f, "") for f in ALL_FIELDS} for r in results]
    df = pd.DataFrame(norm_results)

    if q:
        ql = q.lower()
        df = df[df.apply(lambda row: any(ql in str(v).lower() for v in row.values), axis=1)]
    if niche_pick != "All":
        df = df[df["niche"] == niche_pick]

    # Friendly column names & order
    df = df.rename(columns={
        "url":          "Website",
        "company":      "Company",
        "niche":        "Niche",
        "people":       "Key People",
        "emails":       "Emails",
        "phones":       "Phones",
        "socials":      "Socials",
        "city":         "City",
        "language":     "Lang",
        "tech":         "Built With",
        "contact_form": "Has form",
    })
    preferred = ["Website", "Company", "Niche", "Key People", "Emails", "Phones",
                 "City", "Built With", "Lang", "Has form", "Socials"]
    df = df[[c for c in preferred if c in df.columns]]

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Website": st.column_config.LinkColumn(
                "Website",
                display_text=r"^(?:https?://)?(.+)$",
            ),
        },
    )

    # Downloads — name file after source (Google Sheet title or uploaded filename)
    base_name = _safe_filename(st.session_state.source_name) if st.session_state.source_name else "scrape-results"

    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)
    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)

    if st.session_state.source_name:
        st.caption(f"📁 Downloads will be named after: **{st.session_state.source_name}**")

    d1, d2, d3 = st.columns([1, 1, 4])
    with d1:
        st.download_button(
            "⬇ CSV", data=csv_buf.getvalue(),
            file_name=f"{base_name}.csv", mime="text/csv",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            "⬇ Excel", data=xlsx_buf.getvalue(),
            file_name=f"{base_name}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    # Copy-all emails (display as a code block for easy copy)
    with st.expander("📋 Copy all emails as one string"):
        all_emails = ", ".join(r["emails"] for r in results if r["emails"])
        st.code(all_emails or "(none)")


# ── Failed list ────────────────────────────────────────────────────
if failed:
    with st.expander(f"⚠ Skipped — {len(failed)} URL(s) had no email"):
        for u in failed:
            st.markdown(f"- ❌ `{u}`")


# ── Sidebar: history ───────────────────────────────────────────────
with st.sidebar:
    # ── Public usage counter ─────────────────────────────────────
    total = _get_counter()
    if total is not None:
        st.markdown(
            f"<div style='padding:.6rem .85rem; background:#eff6ff; border:1px solid #bfdbfe; "
            f"border-radius:.5rem; margin-bottom:1rem; text-align:center;'>"
            f"<div style='font-size:1.4rem; font-weight:800; color:#1d4ed8;'>{total:,}</div>"
            f"<div style='font-size:.7rem; color:#64748b; text-transform:uppercase; letter-spacing:.05em;'>"
            f"total scrapes worldwide</div></div>",
            unsafe_allow_html=True,
        )

    st.markdown("### 🕘 History")

    # ── Download / Restore history ──────────────────────────────
    with st.expander("💾 Save / Restore"):
        # Save
        if st.session_state.history:
            hist_json = json.dumps(
                st.session_state.history,
                indent=2, default=str, ensure_ascii=False,
            )
            st.download_button(
                "⬇ Download history (.json)",
                data=hist_json,
                file_name=f"scraper-history-{datetime.now().strftime('%Y%m%d-%H%M')}.json",
                mime="application/json",
                use_container_width=True,
            )
        else:
            st.caption("Nothing to save yet.")

        # Restore
        restored_file = st.file_uploader(
            "Restore from .json",
            type=["json"],
            key="hist_restore",
            label_visibility="collapsed",
        )
        if restored_file is not None:
            try:
                loaded = json.loads(restored_file.read())
                if isinstance(loaded, list) and all(isinstance(h, dict) for h in loaded):
                    # Merge: new history first, dedup by 'when' timestamp
                    existing_keys = {h.get("when") for h in st.session_state.history}
                    incoming = [h for h in loaded if h.get("when") not in existing_keys]
                    st.session_state.history = (incoming + st.session_state.history)[:HIST_MAX * 4]
                    st.success(f"Restored {len(incoming)} new entries (total: {len(st.session_state.history)})")
                else:
                    st.error("Invalid history file format.")
            except json.JSONDecodeError:
                st.error("Could not parse JSON file.")
            except Exception as e:
                st.error(f"Could not restore: {e}")

    if not st.session_state.history:
        st.caption("No previous scrapes yet.")
    else:
        for i, h in enumerate(st.session_state.history):
            with st.container(border=True):
                st.markdown(
                    f"**{h['count']}** results · "
                    f"{('<span style=\"color:#b45309\">' + str(h['failed']) + ' skipped</span>') if h['failed'] else ''} "
                    f"<br><small style='color:#94a3b8'>{h['when']}</small>",
                    unsafe_allow_html=True,
                )
                c1, c2 = st.columns(2)
                if c1.button("Restore", key=f"restore_{i}", use_container_width=True):
                    st.session_state.results     = h["results"]
                    st.session_state.failed      = h["failed_urls"]
                    st.session_state.source_name = h.get("source_name")
                    st.rerun()
                if c2.button("Delete", key=f"delete_{i}", use_container_width=True):
                    st.session_state.history.pop(i)
                    st.rerun()
        if st.button("Clear history", type="secondary", use_container_width=True):
            st.session_state.history = []
            st.rerun()

    st.markdown("---")
    st.markdown(
        """
        <div style="font-size:.78rem; color:#64748b; line-height:1.5;">
          <div style="margin-bottom:.4rem;">
            🤖 <b>Respects robots.txt</b> — disallowed URLs silently skipped.
          </div>
          <div style="margin-bottom:.4rem;">
            🐢 <b>Per-domain rate limited</b> — 1 req/sec per domain.
          </div>
          <div style="margin-bottom:.4rem;">
            🧹 <b>Tracking params stripped</b> — <code>utm_*</code>, <code>fbclid</code>, etc.
          </div>
          <div style="margin-bottom:.4rem;">
            🛡 <b>Auto-retries</b> with browser headers on 403 / Cloudflare.
          </div>
          <div>
            📄 Only scrapes <b>homepage</b>, <b>contact</b>, <b>about</b> pages.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
