"""画像解析結果・HTS分類結果の2段SQLiteキャッシュ。

L1: image_hash → analysis       同一画像 → API呼び出しスキップ
L2: analysis_fingerprint → HTS結果  同じ分析結果 → HTS照合スキップ
"""
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

_DB_PATH = Path(__file__).parent / "data" / "analysis_cache.db"
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not getattr(_local, "conn", None):
        _DB_PATH.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS image_analysis_cache (
                image_hash    TEXT PRIMARY KEY,
                analysis_json TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hit_count     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS hts_result_cache (
                analysis_fp   TEXT PRIMARY KEY,
                results_json  TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hit_count     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cache_stats (
                date          TEXT PRIMARY KEY,
                api_calls     INTEGER DEFAULT 0,
                l1_hits       INTEGER DEFAULT 0,
                l2_hits       INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chapter_cache (
                image_hash    TEXT PRIMARY KEY,
                chapters_json TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # 旧スキーマ（cache_hits列）からのマイグレーション
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cache_stats)")}
        if "cache_hits" in cols and "l1_hits" not in cols:
            conn.executescript("""
                ALTER TABLE cache_stats ADD COLUMN l1_hits INTEGER DEFAULT 0;
                ALTER TABLE cache_stats ADD COLUMN l2_hits INTEGER DEFAULT 0;
                UPDATE cache_stats SET l1_hits = cache_hits;
            """)
        conn.commit()
        _local.conn = conn
    return _local.conn


# ── ユーティリティ ──────────────────────────────────────────────────────────

def _image_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def analysis_fingerprint(analysis: dict) -> str:
    """category_hint・material・keywords の正規化表現からフィンガープリントを生成する。"""
    key = json.dumps({
        "category_hint": (analysis.get("category_hint") or "").lower().strip(),
        "material": (analysis.get("material") or "").lower().strip(),
        "keywords": sorted(
            (kw.lower().strip() for kw in analysis.get("keywords", []) if kw),
        ),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()


# ── L1: image_hash → analysis ───────────────────────────────────────────────

def get_cached_analysis(image_bytes: bytes) -> dict | None:
    """L1: 同一画像のキャッシュを返す。なければ None。"""
    h = _image_hash(image_bytes)
    conn = _get_conn()
    row = conn.execute(
        "SELECT analysis_json FROM image_analysis_cache WHERE image_hash = ?", (h,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE image_analysis_cache SET hit_count = hit_count + 1 WHERE image_hash = ?", (h,)
        )
        _record_stat(conn, level="l1")
        conn.commit()
        result = json.loads(row[0])
        result["_cache_hit"] = "L1"
        return result
    return None


def save_analysis(image_bytes: bytes, analysis: dict) -> None:
    """L1: 画像解析結果を保存する。"""
    h = _image_hash(image_bytes)
    clean = {k: v for k, v in analysis.items() if not k.startswith("_")}
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO image_analysis_cache (image_hash, analysis_json, hit_count) "
        "VALUES (?, ?, 0)",
        (h, json.dumps(clean, ensure_ascii=False)),
    )
    _record_stat(conn, level="api")
    conn.commit()


# ── 章キャッシュ: image_hash → chapter_hints ────────────────────────────────

def get_cached_chapters(image_bytes: bytes) -> list[str] | None:
    """同一画像の章推定結果（実API実績）を返す。なければ None。"""
    h = _image_hash(image_bytes)
    conn = _get_conn()
    row = conn.execute(
        "SELECT chapters_json FROM chapter_cache WHERE image_hash = ?", (h,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_chapters(image_bytes: bytes, chapters: list[str]) -> None:
    """章推定結果を画像ハッシュで保存する。"""
    h = _image_hash(image_bytes)
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chapter_cache (image_hash, chapters_json) VALUES (?, ?)",
        (h, json.dumps(chapters, ensure_ascii=False)),
    )
    conn.commit()


# ── L2: analysis_fingerprint → HTS結果 ──────────────────────────────────────

def get_cached_hts(analysis: dict) -> list[dict] | None:
    """L2: 同じ分析結果のHTS分類キャッシュを返す。なければ None。"""
    fp = analysis_fingerprint(analysis)
    conn = _get_conn()
    row = conn.execute(
        "SELECT results_json FROM hts_result_cache WHERE analysis_fp = ?", (fp,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE hts_result_cache SET hit_count = hit_count + 1 WHERE analysis_fp = ?", (fp,)
        )
        _record_stat(conn, level="l2")
        conn.commit()
        return json.loads(row[0])
    return None


def save_hts(analysis: dict, results) -> None:
    """L2: HTS分類結果を分析フィンガープリントで保存する。

    results は list[dict]（手動モード）または dict[str, list[dict]]（自動モード）。
    """
    fp = analysis_fingerprint(analysis)
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO hts_result_cache (analysis_fp, results_json, hit_count) "
        "VALUES (?, ?, 0)",
        (fp, json.dumps(results, ensure_ascii=False)),
    )
    conn.commit()


# ── キャッシュクリア ────────────────────────────────────────────────────────

def clear_hts_cache() -> int:
    """L2: HTS分類結果キャッシュを全削除（再判定でAPI不要）。削除件数を返す。"""
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM hts_result_cache").fetchone()[0]
    conn.execute("DELETE FROM hts_result_cache")
    conn.commit()
    return n


def clear_chapter_cache() -> int:
    """章推定キャッシュを全削除。削除件数を返す。"""
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM chapter_cache").fetchone()[0]
    conn.execute("DELETE FROM chapter_cache")
    conn.commit()
    return n


def clear_analysis_cache() -> int:
    """L1: 画像解析キャッシュを全削除（再判定でAPI再課金が発生する）。削除件数を返す。"""
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM image_analysis_cache").fetchone()[0]
    conn.execute("DELETE FROM image_analysis_cache")
    conn.commit()
    return n


def clear_all() -> dict:
    """全キャッシュ（L1・L2・章）を削除。削除件数を返す。"""
    return {
        "l1": clear_analysis_cache(),
        "l2": clear_hts_cache(),
        "chapters": clear_chapter_cache(),
    }


# ── 統計 ──────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """直近30日間の統計と推定削減コストを返す。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT SUM(api_calls), SUM(l1_hits), SUM(l2_hits) FROM cache_stats "
        "WHERE date >= date('now', '-30 days')"
    ).fetchone()
    api_calls = row[0] or 0
    l1_hits   = row[1] or 0
    l2_hits   = row[2] or 0
    total = api_calls + l1_hits + l2_hits

    cost_per_call = 0.024
    # L1ヒット: API + scoring 両方スキップ
    # L2ヒット: API は発生するが scoring スキップ（コスト変わらず、速度向上のみ）
    saved_usd = l1_hits * cost_per_call

    l1_entries = conn.execute("SELECT COUNT(*) FROM image_analysis_cache").fetchone()[0]
    l2_entries = conn.execute("SELECT COUNT(*) FROM hts_result_cache").fetchone()[0]

    return {
        "total_requests": total,
        "api_calls": api_calls,
        "l1_hits": l1_hits,
        "l2_hits": l2_hits,
        "hit_rate_l1": l1_hits / total if total else 0.0,
        "hit_rate_l2": l2_hits / total if total else 0.0,
        "saved_usd_30d": saved_usd,
        "cached_images": l1_entries,
        "cached_analyses": l2_entries,
    }


def _record_stat(conn: sqlite3.Connection, level: str) -> None:
    conn.execute(
        "INSERT INTO cache_stats (date) VALUES (date('now')) ON CONFLICT(date) DO NOTHING"
    )
    if level == "api":
        conn.execute("UPDATE cache_stats SET api_calls = api_calls + 1 WHERE date = date('now')")
    elif level == "l1":
        conn.execute("UPDATE cache_stats SET l1_hits = l1_hits + 1 WHERE date = date('now')")
    elif level == "l2":
        conn.execute("UPDATE cache_stats SET l2_hits = l2_hits + 1 WHERE date = date('now')")
