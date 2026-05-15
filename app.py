import csv
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl
import requests
import xlrd
from flask import Flask, jsonify, render_template, request, Response

from scraper import scrape_url

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0           # static files: no cache
app.config["TEMPLATES_AUTO_RELOAD"] = True            # reload templates on change
app.jinja_env.auto_reload = True


@app.after_request
def _no_cache(resp):
    """Force browser to always fetch the freshest HTML / JS / CSS."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp

MAX_WORKERS = 8
MAX_URLS = 500

# Header cell labels that should be treated as the "website" column
WEBSITE_COL_RE = re.compile(
    r"^\s*(website|web\s*site|site|url|domain|homepage|web|link)s?\s*$",
    re.IGNORECASE,
)

GSHEET_RE = re.compile(
    r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", re.IGNORECASE
)
GSHEET_GID_RE = re.compile(r"[#?&]gid=(\d+)", re.IGNORECASE)

URL_LIKE_RE = re.compile(
    r"^(https?://)?([a-z0-9][a-z0-9\-]*\.)+[a-z]{2,}(/.*)?$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Smart column detection
# ---------------------------------------------------------------------------

def _looks_like_url(value: str) -> bool:
    return bool(URL_LIKE_RE.match(value.strip()))


def _extract_urls_from_rows(rows: list[list[str]]) -> list[str]:
    """
    Given a list of rows (each a list of cell strings), find the column that
    holds website URLs and return all values from it.
      1. If a header row has a cell matching WEBSITE_COL_RE, use that column.
      2. Otherwise pick the column with the highest count of URL-like values.
      3. Fall back to "any cell that looks like a URL" across the sheet.
    """
    if not rows:
        return []

    # Strategy 1 — labelled header
    header = rows[0]
    target_col = None
    for idx, cell in enumerate(header):
        if WEBSITE_COL_RE.match(cell or ""):
            target_col = idx
            break

    # Strategy 2 — column with most URL-like values
    if target_col is None:
        col_count = max(len(r) for r in rows)
        scores = [0] * col_count
        for r in rows:
            for idx, cell in enumerate(r):
                if cell and _looks_like_url(cell):
                    scores[idx] += 1
        if max(scores, default=0) >= 2:
            target_col = scores.index(max(scores))

    urls: list[str] = []
    if target_col is not None:
        start = 1 if WEBSITE_COL_RE.match(header[target_col] or "") else 0
        for r in rows[start:]:
            if target_col < len(r):
                val = (r[target_col] or "").strip()
                if val and _looks_like_url(val):
                    urls.append(val)

    # Strategy 3 — last resort scan
    if not urls:
        for r in rows:
            for cell in r:
                val = (cell or "").strip()
                if val and _looks_like_url(val):
                    urls.append(val)

    return urls


# ---------------------------------------------------------------------------
# File parsers
# ---------------------------------------------------------------------------

def _csv_to_rows(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    return [list(row) for row in csv.reader(io.StringIO(text))]


def _xls_to_rows(data: bytes) -> list[list[str]]:
    wb = xlrd.open_workbook(file_contents=data)
    rows: list[list[str]] = []
    for sheet in wb.sheets():
        for rx in range(sheet.nrows):
            rows.append([str(sheet.cell_value(rx, cx)).strip() for cx in range(sheet.ncols)])
    return rows


def _xlsx_to_rows(data: bytes) -> list[list[str]]:
    wb = openpyxl.load_workbook(filename=io.BytesIO(data), data_only=True)
    rows: list[list[str]] = []
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            rows.append([("" if cell is None else str(cell)).strip() for cell in row])
    return rows


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def _is_gsheet_url(value: str) -> bool:
    return bool(GSHEET_RE.search(value))


def _fetch_gsheet_as_csv(url: str) -> bytes | None:
    """Convert a Google Sheets share URL to its CSV export and download it."""
    m = GSHEET_RE.search(url)
    if not m:
        return None
    sheet_id = m.group(1)
    gid_match = GSHEET_GID_RE.search(url)
    gid = gid_match.group(1) if gid_match else "0"
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    try:
        resp = requests.get(export_url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------

def _scrape_all(urls: list[str]) -> list[dict]:
    urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))[:MAX_URLS]
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(scrape_url, u): u for u in urls}
        for future in as_completed(future_map):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
def scrape():
    urls: list[str] = []

    # ── File upload path ─────────────────────────────────────────
    if "file" in request.files:
        f = request.files["file"]
        filename = f.filename.lower() if f.filename else ""
        data = f.read()
        try:
            if filename.endswith(".csv"):
                rows = _csv_to_rows(data)
            elif filename.endswith(".xlsx"):
                rows = _xlsx_to_rows(data)
            elif filename.endswith(".xls"):
                rows = _xls_to_rows(data)
            else:
                return jsonify({"error": "Unsupported file type. Use CSV or XLS/XLSX."}), 400
            urls = _extract_urls_from_rows(rows)
        except Exception as exc:
            return jsonify({"error": f"Could not parse file: {exc}"}), 400

    # ── Text / JSON body path ────────────────────────────────────
    else:
        body = request.get_json(silent=True) or {}
        raw_text = body.get("urls", "") or request.form.get("urls", "")

        candidates = [u.strip() for u in re.split(r"[,\n\r]+", raw_text) if u.strip()]

        for c in candidates:
            if _is_gsheet_url(c):
                # Fetch the Google Sheet and extract URLs from the website column
                sheet_csv = _fetch_gsheet_as_csv(c)
                if sheet_csv is None:
                    return jsonify({
                        "error": "Could not download the Google Sheet. "
                                 "Make sure it's shared as 'Anyone with the link can view'."
                    }), 400
                try:
                    rows = _csv_to_rows(sheet_csv)
                    urls.extend(_extract_urls_from_rows(rows))
                except Exception as exc:
                    return jsonify({"error": f"Could not parse Google Sheet: {exc}"}), 400
            else:
                urls.append(c)

    if not urls:
        return jsonify({"error": "No website URLs found in your input."}), 400

    results = _scrape_all(urls)
    return jsonify({"results": results, "count": len(results), "scanned": len(urls)})


@app.route("/export", methods=["POST"])
def export():
    body = request.get_json(silent=True) or {}
    rows = body.get("results", [])

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["url", "emails", "city"])
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="results.csv"'},
    )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
