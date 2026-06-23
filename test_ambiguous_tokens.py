"""汎用語ゲートテスト（U1: 後追い→予防）。API不要・ローカルのみ・課金ゼロ。

HTSデータに「多章にまたがる汎用語」が新たに出現したのに、まだ人間が
危険(CONTEXT_DEPENDENT_TOKENS)とも無害(REVIEWED_BENIGN)とも判断していない場合に
FAILする。これにより、誤分類が起きてから対処する後追い保守ではなく、
データ更新時に未判断語を機械的に検知する予防保守へ転換する。

新しい未判断語が出た場合の対応:
  - その語が誤分類を起こすなら classifier.py の CONTEXT_DEPENDENT_TOKENS に
    文脈条件付きで追加する。
  - 無害なら tools/detect_ambiguous_tokens.py の REVIEWED_BENIGN に追加する。

使い方:
    python test_ambiguous_tokens.py
終了コード: 未判断語なし=0 / あり=1
"""
import sys

from tools.detect_ambiguous_tokens import find_unreviewed, DEFAULT_MIN_CHAPTERS


def main() -> int:
    unreviewed = find_unreviewed(DEFAULT_MIN_CHAPTERS)
    if not unreviewed:
        print(f"[PASS] {DEFAULT_MIN_CHAPTERS}章以上の未判断トークンはありません。")
        return 0

    print(f"[FAIL] 未判断の多章トークンが {len(unreviewed)} 件あります"
          f"（{DEFAULT_MIN_CHAPTERS}章以上）:\n")
    for tok, chs in unreviewed:
        print(f"  {tok:<20} {len(chs):>3}章   ch{','.join(sorted(chs, key=int))}")
    print("\n対応: 誤分類を起こす語は classifier.py の CONTEXT_DEPENDENT_TOKENS に、")
    print("      無害な語は tools/detect_ambiguous_tokens.py の REVIEWED_BENIGN に追加。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
