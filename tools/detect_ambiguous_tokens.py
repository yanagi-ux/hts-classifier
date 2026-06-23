"""HTSデータ全章を走査し、複数章に出現する汎用語を検出する。

8章以上に出現するトークンは「汎用すぎて誤スコアの原因になりやすい」候補。
現在の CONTEXT_DEPENDENT_TOKENS に未登録のものを優先表示する。

使い方:
    python tools/detect_ambiguous_tokens.py [--min-chapters N] [--top K]
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import SUPPORTED_CHAPTERS

DATA_DIR = Path(__file__).parent.parent / "data"

# classifier.py の CONTEXT_DEPENDENT_TOKENS に既登録のトークン
ALREADY_REGISTERED = {
    "video", "plastic", "metal", "imitation", "decorative", "paper",
    "sorted", "rags", "figures", "figure", "tank", "reservoir", "vat",
    "bed", "instrument", "folding", "container",
}

# スコアリングでも問題ない汎用語（HTSに必ず出る接続詞・前置詞・記号類）
STOPWORDS = {
    "and", "or", "for", "the", "of", "with", "in", "on", "to", "a", "an",
    "is", "are", "be", "by", "as", "at", "from", "other", "all", "thereof",
    "not", "no", "into", "such", "whether", "than", "their", "its", "which",
    "also", "only", "more", "less", "over", "under", "each", "per", "used",
    "made", "use", "good", "part", "type", "kind", "form", "similar",
    "including", "exclude", "except", "contain", "consist", "having",
    "suitable", "designed", "intended", "without",
    "heading", "chapter", "note", "section",
    "unit", "weight", "number", "piece", "set",
}

_TOK_RE = re.compile(r"[a-z]{3,}")


def tokenize(text: str) -> set[str]:
    return {t for t in _TOK_RE.findall(text.lower()) if t not in STOPWORDS}


def stem(word: str) -> str:
    """Porter-light stemmer の代替（suffix剥ぎのみ）。"""
    for suffix in ("tion", "sion", "ing", "ness", "ment", "ies", "ers", "ings", "ated", "ous", "ful", "al", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect ambiguous HTS tokens")
    parser.add_argument("--min-chapters", type=int, default=8,
                        help="何章以上に出現したら報告するか (default: 8)")
    parser.add_argument("--top", type=int, default=40,
                        help="表示件数 (default: 40)")
    args = parser.parse_args()

    # token → 出現章セット
    token_chapters: dict[str, set[str]] = defaultdict(set)

    loaded = 0
    for ch, info in SUPPORTED_CHAPTERS.items():
        data_file = DATA_DIR / info["data_file"]
        if not data_file.exists():
            continue
        try:
            entries = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        loaded += 1
        for entry in entries:
            desc = entry.get("description", "") or ""
            for tok in tokenize(desc):
                token_chapters[tok].add(ch)

    print(f"\n読み込み章数: {loaded} / {len(SUPPORTED_CHAPTERS)}")
    print(f"ユニークトークン数: {len(token_chapters):,}\n")
    print(f"{'トークン':<20} {'出現章数':>6}  {'出現章'}")
    print("-" * 70)

    candidates = [
        (tok, chs)
        for tok, chs in token_chapters.items()
        if len(chs) >= args.min_chapters and tok not in ALREADY_REGISTERED
    ]
    candidates.sort(key=lambda x: -len(x[1]))

    for tok, chs in candidates[: args.top]:
        chs_sorted = ",".join(sorted(chs, key=int))
        flag = "  ← 要注意" if len(chs) >= 15 else ""
        print(f"  {tok:<20} {len(chs):>4}章   ch{chs_sorted}{flag}")

    print(f"\n合計候補: {len(candidates)} 件（{args.min_chapters}章以上）")
    print("\n【使い方】上記トークンが誤分類の原因になっている場合は")
    print("  classifier.py の CONTEXT_DEPENDENT_TOKENS に追加してください。")
    print("  例: \"printed\": {\"book\", \"paper\", \"publication\"}")


if __name__ == "__main__":
    main()
