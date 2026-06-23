"""HTS分類ロジックの回帰テスト（API不要・ローカルのみ・課金ゼロ）。

「解析結果(material/category_hint/keywords) → 採用HTSコード」を検証する。
画像解析(API)は使わず、分類ロジック(classifier + hts_overrides)だけをテストするため
何度でも無料・一瞬で実行できる。分類ルールを変更したら本スクリプトを回して
デグレ（過去の修正が壊れていないか）を確認する。

使い方:
    python test_classification.py
終了コード: 全件合格=0 / 失敗あり=1
"""
import sys

from config import SUPPORTED_CHAPTERS
from classifier import classify_per_chapter_ensemble, apply_hts_overrides


def _adopt(query: dict, chapters: list[str]) -> str | None:
    """指定章で分類＋オーバーライドを適用し、採用（最高スコア）のHTSコードを返す。"""
    files = [(c, SUPPORTED_CHAPTERS[c]["data_file"]) for c in chapters if c in SUPPORTED_CHAPTERS]
    res = apply_hts_overrides(
        classify_per_chapter_ensemble([query], files, top_n_per_chapter=3), [query]
    )
    flat = [r for rs in res.values() for r in rs]
    if not flat:
        return None
    return max(flat, key=lambda r: r.get("effective_score", 0))["hts_code"]


def _q(material="", category_hint="", function="", keywords=None) -> dict:
    return {
        "product_name": "", "material": material, "category_hint": category_hint,
        "function": function, "keywords": keywords or [], "spec": "",
    }


# (テスト名, クエリ, 期待コード, 照合する章[誤誘導先も含める])
CASES = [
    ("カッティングマット", _q("plastic", "cutting mat", "craft work surface",
        ["cutting mat", "self-healing mat", "plastic mat"]), "3926.90.99.89", ["39", "95", "84"]),
    ("ピンバッジ(プラ)", _q("plastic", "pin badge", "decorative badge",
        ["pin badge", "plastic badge", "decorative pin"]), "3926.40.00.90", ["39", "83", "71"]),
    ("ピンバッジ(金属)", _q("metal", "pin badge", "decorative badge",
        ["pin badge", "metal badge"]), "8308.90.90.00", ["39", "83", "71"]),
    ("テレビ", _q("plastic and metal", "television receiver", "video display for tv reception",
        ["television", "TV set", "flat panel display", "LCD TV"]), "8528.72.64.60", ["85"]),
    ("同人誌", _q("paper", "printed book", "reading",
        ["printed book", "self-published book", "booklet", "manga"]), "4901.99.00.92", ["49"]),
    ("市販コミック", _q("paper", "comic book", "reading",
        ["comic book", "manga", "publication"]), "4901.99.00.92", ["49"]),
    ("トレカ", _q("paperboard", "trading card", "card game",
        ["trading card", "game card", "collectible card"]), "9504.40.00.00", ["95", "49"]),
    ("携帯ゲーム機", _q("plastic", "handheld game console", "portable game machine",
        ["handheld gaming device", "portable game console", "game console"]), "9504.50.00.00", ["95", "85"]),
    ("アーケード機", _q("plastic", "arcade game machine", "arcade game",
        ["arcade cabinet", "game console", "tabletop arcade"]), "9504.50.00.00", ["95", "85"]),
    ("カートリッジ機", _q("plastic", "retro video game console", "cartridge games",
        ["video game console", "cartridge game console", "game machine"]), "9504.50.00.00", ["95", "85"]),
    ("録画DVD", _q("polycarbonate", "recorded DVD", "video playback",
        ["DVD", "recorded DVD", "movie DVD", "video disc"]), "8523.49.50.00", ["85"]),
    ("音楽CD", _q("polycarbonate", "music cd", "audio playback",
        ["music cd", "recorded cd", "audio disc"]), "8523.49.30.00", ["85"]),
    ("アクリルスタンド", _q("acrylic", "acrylic stand figure", "decorative display",
        ["acrylic stand", "acrylic figure", "standee"]), "3926.40.00.90", ["39"]),
    ("レンチ", _q("metal", "wrench", "tightening nuts and bolts",
        ["wrench", "hand tool", "spanner", "open-end wrench"]), "8204.11.00.30", ["82"]),
    ("スニーカー", _q("textile rubber", "athletic shoe", "running shoes",
        ["sneaker", "running shoe", "athletic footwear"]), "6404.11.90.20", ["64"]),
    ("スポーツ靴", _q("textile", "sports shoes", "running",
        ["sports shoes", "sneaker", "footwear"]), "6404.11.90.20", ["64"]),
    ("Tシャツ", _q("cotton", "t-shirt", "casual wear",
        ["t-shirt", "printed cotton shirt", "short sleeve shirt"]), "6109.10.00.27", ["61"]),
    ("ズボン", _q("cotton", "trousers", "worn as pants",
        ["trousers", "pants", "denim"]), "6203.42.07.16", ["62"]),
    ("ショーツ", _q("cotton", "shorts", "daily wear",
        ["shorts", "cotton shorts", "men's shorts"]), "6203.42.07.51", ["62"]),
    ("チャーム", _q("plastic", "character charm", "decorative charm",
        ["charm", "acrylic charm", "keychain charm", "pendant"]), "3926.40.00.90", ["39", "71"]),
    ("ステアリングカバー", _q("plastic", "steering wheel cover", "vehicle accessory",
        ["steering wheel cover", "car accessory"]), "8708.99.81.80", ["87"]),
    ("ゴム印", _q("plastic", "rubber stamp set", "marking and printing",
        ["rubber stamp", "self-inking stamp", "office stamp"]), "9611.00.00.00", ["96"]),
    ("造花/プレスフラワー", _q("plant material resin", "pressed flower, dried flower", "decorative craft",
        ["pressed flower", "dried flower", "decorative flower"]), "6702.90.65.00", ["67", "39"]),
]


def main() -> int:
    passed = failed = 0
    print(f"回帰テスト（API不要）: {len(CASES)} ケース\n")
    for name, query, expected, chapters in CASES:
        got = _adopt(query, chapters)
        ok = (got == expected)
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name:<18} 期待={expected}"
        if not ok:
            line += f"  実際={got}"
        print(line)
        passed += ok
        failed += (not ok)
    print(f"\n結果: {passed} PASS / {failed} FAIL（全{len(CASES)}件）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
