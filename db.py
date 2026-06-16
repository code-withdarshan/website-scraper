"""
SQLite persistence layer for the scraper.

Stores every scrape as a row in `scrapes` plus one row per URL in `urls`. The
URL-level granularity is what lets the user resume after a crash, an internet
drop, or a Streamlit Cloud process restart.

Schema is created lazily on first connect.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "scraper.db"

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    """Open a connection with the schema initialized. Thread-safe."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    with _lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scrapes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                total         INTEGER NOT NULL,
                source_name   TEXT,
                skip_chinese  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS urls (
                scrape_id     INTEGER NOT NULL,
                url           TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                result_json   TEXT,
                completed_at  TEXT,
                PRIMARY KEY (scrape_id, url),
                FOREIGN KEY (scrape_id) REFERENCES scrapes(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_urls_status
                ON urls(scrape_id, status);
            """
        )
        conn.commit()


# ── Mutations ──────────────────────────────────────────────────────
def create_scrape(
    conn: sqlite3.Connection,
    urls: list[str],
    source_name: str | None,
    skip_chinese: bool,
) -> int:
    """Insert a new scrape + its pending URLs. Returns the scrape_id."""
    started = datetime.now().isoformat(timespec="seconds")
    with _lock:
        cur = conn.execute(
            "INSERT INTO scrapes (started_at, total, source_name, skip_chinese) "
            "VALUES (?, ?, ?, ?)",
            (started, len(urls), source_name, 1 if skip_chinese else 0),
        )
        scrape_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO urls (scrape_id, url, status) VALUES (?, ?, 'pending')",
            [(scrape_id, u) for u in urls],
        )
        conn.commit()
    return scrape_id


def mark_url_done(
    conn: sqlite3.Connection, scrape_id: int, url: str, result: dict
) -> None:
    completed = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "UPDATE urls SET status='done', result_json=?, completed_at=? "
            "WHERE scrape_id=? AND url=?",
            (json.dumps(result, ensure_ascii=False), completed, scrape_id, url),
        )
        conn.commit()


def mark_url_failed(conn: sqlite3.Connection, scrape_id: int, url: str) -> None:
    completed = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "UPDATE urls SET status='failed', completed_at=? "
            "WHERE scrape_id=? AND url=?",
            (completed, scrape_id, url),
        )
        conn.commit()


def finish_scrape(conn: sqlite3.Connection, scrape_id: int) -> None:
    finished = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn.execute(
            "UPDATE scrapes SET finished_at=? WHERE id=?", (finished, scrape_id)
        )
        conn.commit()


def delete_scrape(conn: sqlite3.Connection, scrape_id: int) -> None:
    with _lock:
        conn.execute("DELETE FROM urls WHERE scrape_id=?", (scrape_id,))
        conn.execute("DELETE FROM scrapes WHERE id=?", (scrape_id,))
        conn.commit()


# ── Queries ────────────────────────────────────────────────────────
def get_active_scrape(conn: sqlite3.Connection) -> dict | None:
    """Return the most recent scrape that still has pending URLs (= unfinished)."""
    row = conn.execute(
        """
        SELECT s.* FROM scrapes s
        WHERE s.finished_at IS NULL
          AND EXISTS (
              SELECT 1 FROM urls u
              WHERE u.scrape_id = s.id AND u.status = 'pending'
          )
        ORDER BY s.started_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return _scrape_summary(conn, row)


def get_scrape(conn: sqlite3.Connection, scrape_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM scrapes WHERE id=?", (scrape_id,)
    ).fetchone()
    return _scrape_summary(conn, row) if row else None


def _scrape_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """Augment a scrapes row with counts and URL lists for one scrape."""
    counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM urls WHERE scrape_id=? GROUP BY status",
        (row["id"],),
    ).fetchall()
    by_status = {c["status"]: c["n"] for c in counts}
    return {
        "id":           row["id"],
        "started_at":   row["started_at"],
        "finished_at":  row["finished_at"],
        "total":        row["total"],
        "source_name":  row["source_name"],
        "skip_chinese": bool(row["skip_chinese"]),
        "done":         by_status.get("done", 0),
        "failed":       by_status.get("failed", 0),
        "pending":      by_status.get("pending", 0),
    }


def get_remaining_urls(conn: sqlite3.Connection, scrape_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT url FROM urls WHERE scrape_id=? AND status='pending' ORDER BY rowid",
        (scrape_id,),
    ).fetchall()
    return [r["url"] for r in rows]


def get_results(conn: sqlite3.Connection, scrape_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT result_json FROM urls WHERE scrape_id=? AND status='done' "
        "AND result_json IS NOT NULL ORDER BY rowid",
        (scrape_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["result_json"]))
        except Exception:
            continue
    return out


def get_failed_urls(conn: sqlite3.Connection, scrape_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT url FROM urls WHERE scrape_id=? AND status='failed' ORDER BY rowid",
        (scrape_id,),
    ).fetchall()
    return [r["url"] for r in rows]


def get_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return recent scrapes (newest first), with done/failed/pending counts."""
    rows = conn.execute(
        "SELECT * FROM scrapes ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_scrape_summary(conn, r) for r in rows]
