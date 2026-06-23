"""hts_overrides.json の棚卸し：変換リスト(hts_category_map.json)へ移行可能な
10桁overrideを判定する。

判定方法:
  各overrideについて「見出し(HS4)だけ与えてスコアラーに枝番(HS10)を選ばせたら、
  override と同じコードになるか」をシミュレートする。
    - 同じ → MIGRATABLE（カテゴリ→見出し変換リストで代替可能）
    - 違う → KEEP（枝番がページ数・価格・性別・材質%等の非テキスト基準で
              決まり、スコアリングでは復元不能 → 10桁override が正当）
    - 材質条件/複数ルール → KEEP（材質で章が分かれる等、変換リストでは表現不可）

使い方:
    python tools/audit_overrides.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from classifier import _combined_query_weights, _best_leaf_under_heading


def main() -> None:
    ov_path = Path(__file__).parent.parent / "hts_overrides.json"
    ov = json.loads(ov_path.read_text(encoding="utf-8"))

    migratable, keep, multi = [], [], []
    for kw, info in ov.items():
        if kw.startswith("_"):
            continue
        rules = info if isinstance(info, list) else [info]
        if len(rules) > 1 or any(r.get("material") for r in rules):
            multi.append((kw, [r["hts_code"] for r in rules]))
            continue
        r = rules[0]
        code, ch = r["hts_code"], r["chapter"]
        heading = code.replace(".", "")[:4]
        q = {"product_name": "", "material": "", "category_hint": kw,
             "function": "", "keywords": [kw], "spec": ""}
        best = _best_leaf_under_heading(ch, heading, _combined_query_weights([q]))
        got = best["hts_code"] if best else None
        (migratable if got == code else keep).append((kw, code, got, heading, ch))

    print(f"\n総override数: {len(migratable) + len(keep) + len(multi)}")
    print(f"  MIGRATABLE（変換リストへ移行可）: {len(migratable)}")
    print(f"  KEEP（10桁維持が正当）        : {len(keep)}")
    print(f"  材質条件/複数ルール（維持）     : {len(multi)}\n")

    print("=== MIGRATABLE：見出しだけでスコアラーが同じ枝番を選べる ===")
    for kw, code, got, h, ch in migratable:
        print(f"  {kw:<28} {code}  → 見出し{h} (ch{ch})")

    print("\n=== KEEP：枝番が非テキスト基準で10桁直指定が必要 ===")
    for kw, code, got, h, ch in keep:
        print(f"  {kw:<28} 期待{code} / scorer={got}")

    print("\n=== 材質条件・複数ルール：変換リストでは表現不可 ===")
    for kw, codes in multi:
        print(f"  {kw:<28} {codes}")


if __name__ == "__main__":
    main()
