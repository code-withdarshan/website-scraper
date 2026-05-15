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

from scraper import scrape_url

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
if "results"  not in st.session_state: st.session_state.results  = []
if "failed"   not in st.session_state: st.session_state.failed   = []
if "history"  not in st.session_state: st.session_state.history  = []

# ── Constants ──────────────────────────────────────────────────────
MAX_WORKERS = 8
MAX_URLS    = 500
HIST_MAX    = 5

WEBSITE_COL_RE = re.compile(
    r"^\s*(website|web\s*site|site|url|domain|homepage|web|link)s?\s*$", re.I
)
GSHEET_RE     = re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", re.I)
GSHEET_GID_RE = re.compile(r"[#?&]gid=(\d+)", re.I)
URL_LIKE_RE   = re.compile(
    r"^(https?://)?([a-z0-9][a-z0-9\-]*\.)+[a-z]{2,}(/.*)?$", re.I
)


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


def _fetch_gsheet_csv(url: str) -> bytes | None:
    m = GSHEET_RE.search(url)
    if not m:
        return None
    sid = m.group(1)
    gid = (GSHEET_GID_RE.search(url) or [None, "0"])[1] if GSHEET_GID_RE.search(url) else "0"
    export_url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    try:
        r = requests.get(export_url, timeout=20, allow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def _collect_urls(text_input: str, uploaded_file) -> tuple[list[str], str | None]:
    """Returns raw URL list (NOT deduped, NOT capped) so caller can report dupes."""
    urls: list[str] = []
    if uploaded_file is not None:
        fn = uploaded_file.name.lower()
        data = uploaded_file.read()
        try:
            if fn.endswith(".csv"):
                urls = _extract_urls_from_rows(_csv_rows(data))
            elif fn.endswith(".xlsx"):
                urls = _extract_urls_from_rows(_xlsx_rows(data))
            elif fn.endswith(".xls"):
                urls = _extract_urls_from_rows(_xls_rows(data))
            else:
                return [], "Unsupported file type."
        except Exception as exc:
            return [], f"Could not parse file: {exc}"
    if text_input:
        cands = [u.strip() for u in re.split(r"[,\n\r]+", text_input) if u.strip()]
        for c in cands:
            if GSHEET_RE.search(c):
                sheet = _fetch_gsheet_csv(c)
                if sheet is None:
                    return [], "Could not download Google Sheet. Make sure it's 'Anyone with the link can view'."
                urls.extend(_extract_urls_from_rows(_csv_rows(sheet)))
            else:
                urls.append(c)
    urls = [u.strip() for u in urls if u.strip()]
    return urls, None


def _normalize_url(u: str) -> str:
    """Lowercase host, drop trailing slash & www, for accurate duplicate detection."""
    parsed = urlparse(u if "://" in u else "https://" + u)
    host = (parsed.netloc or u).lower().lstrip("www.")
    return host.rstrip("/")


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
    raw_urls, err = _collect_urls(text_input, uploaded_file)
    if err:
        st.error(err)
    elif not raw_urls:
        st.error("No URLs detected in your input.")
    else:
        # ── Dedup + cap ──────────────────────────────────────────
        # Normalize for dedup (treat www / non-www / trailing slash as same)
        seen: dict[str, str] = {}
        for u in raw_urls:
            key = _normalize_url(u)
            seen.setdefault(key, u)
        urls = list(seen.values())

        dupes = len(raw_urls) - len(urls)
        capped = max(0, len(urls) - MAX_URLS)
        if capped:
            urls = urls[:MAX_URLS]

        # ── Pre-flight info banners ─────────────────────────────
        info_cols = st.columns(3 if (dupes or capped) else 2)
        with info_cols[0]:
            st.info(f"📋 **{len(urls)}** unique URL{'s' if len(urls) != 1 else ''} to scrape")
        secs, domains = _estimate_time(urls)
        with info_cols[1]:
            mins = secs // 60
            time_str = f"~{mins}m {secs % 60}s" if mins else f"~{secs}s"
            st.info(f"⏱️ Estimated time: **{time_str}** across {domains} domain{'s' if domains != 1 else ''}")
        if dupes or capped:
            with info_cols[2]:
                msgs = []
                if dupes:
                    msgs.append(f"**{dupes}** duplicate{'s' if dupes != 1 else ''} removed")
                if capped:
                    msgs.append(f"**{capped}** truncated (max {MAX_URLS})")
                st.warning("ℹ️ " + " · ".join(msgs))

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

        st.session_state.results = results
        st.session_state.failed  = failed

        # save to history
        st.session_state.history.insert(0, {
            "when":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count":  len(results),
            "failed": len(failed),
            "results": results,
            "failed_urls": failed,
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

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Sites",  len(results))
    m2.metric("Emails", total_emails)
    m3.metric("Phones", total_phones)
    m4.metric("Cities", total_cities)

    # Filter
    q = st.text_input("🔎 Filter by URL, email, phone, social, or city", value="", key="filter")
    df = pd.DataFrame(results)
    if q:
        ql = q.lower()
        df = df[df.apply(lambda row: any(ql in str(v).lower() for v in row.values), axis=1)]

    # Friendly column names
    df = df.rename(columns={
        "url": "Website", "emails": "Emails",
        "phones": "Phones", "socials": "Socials", "city": "City",
    })

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

    # Downloads
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)
    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)

    d1, d2, d3 = st.columns([1, 1, 4])
    with d1:
        st.download_button(
            "⬇ CSV", data=csv_buf.getvalue(),
            file_name="scrape-results.csv", mime="text/csv",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            "⬇ Excel", data=xlsx_buf.getvalue(),
            file_name="scrape-results.xlsx",
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
    st.markdown("### 🕘 History")
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
                    st.session_state.results = h["results"]
                    st.session_state.failed  = h["failed_urls"]
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
            🤖 <b>Respects robots.txt</b> — URLs disallowed by a site's
            <code>robots.txt</code> are silently skipped.
          </div>
          <div style="margin-bottom:.4rem;">
            🐢 <b>Per-domain rate limited</b> — 1 request/second per domain.
          </div>
          <div>
            📄 Only scrapes <b>homepage</b>, <b>contact</b>, and <b>about</b> pages —
            never crawls deeper.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
