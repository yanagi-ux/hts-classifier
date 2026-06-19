"""surugaya_lookup と mercari_lookup を統合して使うファサード。

import 元を category_lookup に一本化することで、
将来の DB 追加も1箇所だけ修正すれば済む。
"""
from surugaya_lookup import lookup_chapters as _suruga_lookup
from mercari_lookup import lookup_chapters as _mercari_lookup


def lookup_chapters(text: str) -> list[tuple[str, list[str]]]:
    """駿河屋 + メルカリ両DBでテキストをマッチし、
    (hts_chapter, en_keywords) のリストを返す。
    同一章の重複は除去し、最初にマッチしたものを採用する。
    """
    seen: set[str] = set()
    results: list[tuple[str, list[str]]] = []

    for ch, kws in _suruga_lookup(text) + _mercari_lookup(text):
        if ch not in seen:
            seen.add(ch)
            results.append((ch, kws))
        else:
            # 同じ章が複数DBでマッチした場合はキーワードをマージ
            for i, (existing_ch, existing_kws) in enumerate(results):
                if existing_ch == ch:
                    merged = list(existing_kws)
                    for kw in kws:
                        if kw not in merged:
                            merged.append(kw)
                    results[i] = (ch, merged)
                    break

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
