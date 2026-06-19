"""HTSデータのロードと階層構造の解決を行うモジュール。"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def load_raw_entries(chapter_file: str = "hts_ch95_raw.json") -> list[dict]:
    path = DATA_DIR / chapter_file
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_hierarchy(entries: list[dict]) -> list[dict]:
    """USITC APIのindent値を使い、各エントリに祖先の説明文を継承させた
    full_description と、実際に課税対象となる末端コード(htsno に桁がある行)
    を判定する is_leaf フラグを付与する。
    """
    stack: list[tuple[int, str]] = []  # (indent, description)
    result = []
    for e in entries:
        indent = int(e["indent"])
        desc = (e["description"] or "").strip()

        # 現在のindent以上の階層をスタックから取り除く
        while stack and stack[-1][0] >= indent:
            stack.pop()

        ancestor_descs = [d for _, d in stack]
        full_description = " > ".join(ancestor_descs + [desc]) if desc else " > ".join(ancestor_descs)

        stack.append((indent, desc))

        htsno = e.get("htsno") or ""
        is_leaf = bool(htsno) and htsno.count(".") >= 2  # 例: 9503.00.00.10 のような末端コード

        result.append({
            "hts_code": htsno,
            "indent": indent,
            "description": desc,
            "full_description": full_description,
            "general_rate": e.get("general", ""),
            "special_rate": e.get("special", ""),
            "other_rate": e.get("other", ""),
            "is_leaf": is_leaf,
        })
    return result


def load_classifiable_entries(chapter_file: str = "hts_ch95_raw.json") -> list[dict]:
    """照合対象とする末端コード(8桁以上、ドット2つ以上)のエントリのみを返す。
    4桁の見出し(例: 7117)や6桁の小見出し(例: 7117.19)は上位階層であり
    実際の関税番号ではないため除外する。
    """
    entries = build_hierarchy(load_raw_entries(chapter_file))
    return [e for e in entries if e["is_leaf"]]
