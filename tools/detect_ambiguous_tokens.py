"""HTSデータ全章を走査し、複数章に出現する汎用語を検出する（U1: 後追い→予防）。

目的:
  HTS記述に含まれる汎用語は、文脈を無視してクエリと誤マッチし誤分類を生む。
  従来はユーザーが誤分類を報告してから CONTEXT_DEPENDENT_TOKENS に登録する
  「後追い保守」だった。本ツールは多章出現語を機械的に洗い出し、
  「未レビューの語が増えたら検知する」ゲート（test_ambiguous_tokens.py）の
  基盤を提供することで保守を予防的にする。

トークンの3分類:
  1. REGISTERED      … classifier.py の CONTEXT_DEPENDENT_TOKENS。
                       危険なので文脈条件付きでのみスコアする（ここから自動取得）。
  2. REVIEWED_BENIGN … レビュー済みで現状無害と判断した多章語（このファイルで管理）。
  3. 上記いずれでもない … 「未レビュー」。新たにこれが出たら人間が要判断。

使い方:
    python tools/detect_ambiguous_tokens.py [--min-chapters N] [--top K]
    python tools/detect_ambiguous_tokens.py --unreviewed-only   # ゲート相当
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import SUPPORTED_CHAPTERS
from classifier import CONTEXT_DEPENDENT_TOKENS

DATA_DIR = Path(__file__).parent.parent / "data"

# 既定の判定閾値（何章以上に出現したら「多章語」とみなすか）。
# ゲートテストもこの値を共有する。
DEFAULT_MIN_CHAPTERS = 8

# (1) classifier.py で文脈条件付きにした語（自動取得・重複定義しない）
REGISTERED = set(CONTEXT_DEPENDENT_TOKENS.keys())

# 法令・統計上の定型句。HTS記述に必ず出るがスコアリング上は無害。
STOPWORDS = {
    # 接続詞・前置詞・冠詞など
    "and", "or", "for", "the", "of", "with", "in", "on", "to", "a", "an",
    "is", "are", "be", "by", "as", "at", "from", "other", "all", "thereof",
    "not", "no", "into", "such", "whether", "than", "their", "its", "which",
    "also", "only", "more", "less", "over", "under", "each", "per", "used",
    "made", "use", "good", "part", "type", "kind", "form", "similar",
    "including", "exclude", "except", "contain", "consist", "having",
    "suitable", "designed", "intended", "without",
    "heading", "chapter", "note", "section",
    "unit", "weight", "number", "piece", "set",
    # 関税分類特有の定型句（数量・割合・参照表現）
    "containing", "any", "this", "but", "like", "those", "foregoing",
    "valued", "percent", "specified", "one", "exceeding", "described",
    "elsewhere", "example", "included", "weighing", "put", "statistical",
    "otherwise", "wholly", "excluding", "combination", "subheading",
    "sale", "purposes", "single", "non", "that", "being", "cross",
    "additional", "fixed", "consisting", "two", "together", "diameter",
    "incorporating", "measuring", "operated", "covered", "coated",
    "laminated", "length", "width", "retail", "mechanical", "surface",
    "outer",
}

# (2) レビュー済みで現状無害と判断した多章語。
# ここに載っている語は、誤分類の原因になることが確認されるまでは
# CONTEXT_DEPENDENT_TOKENS に追加しない（安易な追加は回帰の温床）。
# 実害が出たら REGISTERED 側（CONTEXT_DEPENDENT_TOKENS）へ昇格させ、
# この集合からは外す。
REVIEWED_BENIGN = {
    "accessories", "animal", "articles", "artificial", "attached", "base",
    "boxes", "clothing", "containers", "cotton", "covers", "cut", "electric",
    "equipment", "fiber", "fibers", "flexible", "food", "frames", "glass",
    "goods", "hair", "hand", "household", "leather", "machine", "machines",
    "man", "material", "materials", "parts", "plastics", "plates", "printed",
    "products", "rubber", "sets", "surgical", "synthetic", "table", "textile",
    "tubes", "vegetable", "vehicles", "wall", "waste", "water", "wire",
    "wood", "yarn",
}

_TOK_RE = re.compile(r"[a-z]{3,}")


def tokenize(text: str) -> set[str]:
    return {t for t in _TOK_RE.findall(text.lower()) if t not in STOPWORDS}


def build_token_chapters() -> dict[str, set[str]]:
    """token → 出現章セット を構築する。"""
    token_chapters: dict[str, set[str]] = defaultdict(set)
    for ch, info in SUPPORTED_CHAPTERS.items():
        data_file = DATA_DIR / info["data_file"]
        if not data_file.exists():
            continue
        try:
            entries = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in entries:
            for tok in tokenize(entry.get("description", "") or ""):
                token_chapters[tok].add(ch)
    return token_chapters


def find_unreviewed(min_chapters: int = DEFAULT_MIN_CHAPTERS) -> list[tuple[str, set[str]]]:
    """未レビューの多章語（REGISTERED でも REVIEWED_BENIGN でもない）を返す。

    ゲートテストはこの戻り値が空であることを検証する。
    空でなければ「人間が判断していない多章語が増えた」ことを意味する。
    """
    tc = build_token_chapters()
    out = [
        (tok, chs)
        for tok, chs in tc.items()
        if len(chs) >= min_chapters
        and tok not in REGISTERED
        and tok not in REVIEWED_BENIGN
    ]
    out.sort(key=lambda x: -len(x[1]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect ambiguous HTS tokens")
    parser.add_argument("--min-chapters", type=int, default=DEFAULT_MIN_CHAPTERS,
                        help=f"何章以上に出現したら報告するか (default: {DEFAULT_MIN_CHAPTERS})")
    parser.add_argument("--top", type=int, default=60, help="表示件数 (default: 60)")
    parser.add_argument("--unreviewed-only", action="store_true",
                        help="未レビュー語のみ表示（ゲート相当）")
    args = parser.parse_args()

    tc = build_token_chapters()
    print(f"\n読み込み章数: {len(SUPPORTED_CHAPTERS)}  ユニークトークン数: {len(tc):,}")
    print(f"REGISTERED={len(REGISTERED)}  REVIEWED_BENIGN={len(REVIEWED_BENIGN)}\n")

    unreviewed = find_unreviewed(args.min_chapters)

    if args.unreviewed_only:
        if not unreviewed:
            print(f"OK: {args.min_chapters}章以上の未レビュー語はありません。")
            return 0
        print(f"未レビューの多章語 {len(unreviewed)} 件（要判断）:")
        for tok, chs in unreviewed[: args.top]:
            print(f"  {tok:<20} {len(chs):>3}章   ch{','.join(sorted(chs, key=int))}")
        return 1

    # 通常レポート: 多章語を分類表示
    cands = [(t, c) for t, c in tc.items() if len(c) >= args.min_chapters]
    cands.sort(key=lambda x: -len(x[1]))
    print(f"{'トークン':<20} {'章数':>4}  区分  出現章")
    print("-" * 72)
    for tok, chs in cands[: args.top]:
        if tok in REGISTERED:
            tag = "[登録済]"
        elif tok in REVIEWED_BENIGN:
            tag = "[無害]  "
        else:
            tag = "[未判断]← 要注意"
        print(f"  {tok:<20} {len(chs):>3}章 {tag} ch{','.join(sorted(chs, key=int))}")

    print(f"\n未レビュー: {len(unreviewed)} 件")
    if unreviewed:
        print("→ 各語が誤分類を起こすなら classifier.py の CONTEXT_DEPENDENT_TOKENS へ、")
        print("  無害なら本ファイルの REVIEWED_BENIGN へ追加してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
