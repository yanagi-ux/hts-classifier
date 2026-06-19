"""駿河屋カテゴリDBを使ってHTS章番号を日本語テキストから推定するモジュール。

surugaya_genres.json の jp_name を入力テキストで部分マッチして
対応する HTS 章番号と英語キーワードを返す。
"""
import json
import re
from pathlib import Path

_DB_PATH = Path(__file__).parent / "data" / "surugaya_genres.json"
_db: list[dict] | None = None


def _load_db() -> list[dict]:
    global _db
    if _db is None:
        with open(_DB_PATH, encoding="utf-8") as f:
            _db = json.load(f)["categories"]
    return _db


def lookup_chapters(text: str) -> list[tuple[str, list[str]]]:
    """日本語テキストに駿河屋カテゴリ名が含まれていれば、
    対応する (hts_chapter, en_keywords) のリストを返す。
    同じ章が複数マッチする場合は最初の1件のみ返す。
    優先度: jp_name が長い(=詳細な)エントリを先に返す。
    """
    if not text:
        return []

    categories = _load_db()
    # jp_name の長いもの(より詳細なカテゴリ)を優先してマッチ
    sorted_cats = sorted(categories, key=lambda c: len(c["jp_name"]), reverse=True)

    seen_chapters: set[str] = set()
    results: list[tuple[str, list[str]]] = []
    for cat in sorted_cats:
        jp = cat["jp_name"]
        if not jp:
            continue
        if jp in text:
            ch = cat["hts_chapter"]
            if ch not in seen_chapters:
                seen_chapters.add(ch)
                results.append((ch, cat["en_keywords"]))

    return results


def get_extra_keywords(text: str) -> list[str]:
    """マッチしたカテゴリの英語キーワードを平坦化して返す(重複除去)。"""
    seen: set[str] = set()
    keywords: list[str] = []
    for _, kws in lookup_chapters(text):
        for kw in kws:
            if kw not in seen:
                seen.add(kw)
                keywords.append(kw)
    return keywords
