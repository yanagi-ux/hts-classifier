"""U4: AI画像解析の品質スナップショットテスト（要API・課金あり・低頻度実行）。

test_classification.py が「解析結果→HTSコード」の分類ロジックだけを無料で検証するのに対し、
本テストは代表サンプル画像を実際にClaude APIで解析し、エンドツーエンドで
採用HTSコードを検証する。プロンプト変更・モデル差し替え・APIの振る舞い変化で
画像解析の品質が劣化したことを検知するのが目的。

LLMは非決定的なので採用コードを「前方一致(expect_prefix)」で判定する
（章2桁・見出し4桁など、フィクスチャ側で厳しさを調整できる）。

API課金が発生するため既定では実行されない。明示的に有効化する:
    HTS_RUN_API_TESTS=1 python test_image_analysis_quality.py

キャッシュは劣化を隠してしまう（画像バイトが同一ならプロンプト変更後も
古い解析結果が返る）ため、本テストはL1/L2キャッシュ読み出しを無効化して
毎回ライブ解析する。

フィクスチャ: test_fixtures_api.json（誤分類を直すたびに1件追加して網を広げる）
終了コード: 全件合格=0 / 失敗あり=1 / 実行スキップ=0
"""
import io
import json
import os
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "test_fixtures_api.json"


class _FileLike(io.BytesIO):
    """_classify_one が期待する getvalue()/name を持つ簡易ファイルオブジェクト。"""
    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


def _adopted_code(result: dict) -> str | None:
    """result dict から採用（最高 effective_score）の HTS コードを取り出す。"""
    if result.get("error"):
        return None
    results = result.get("results") or {}
    flat = [r for rs in results.values() for r in rs]
    if not flat:
        return None
    best = max(flat, key=lambda r: r.get("effective_score", r.get("score", 0)))
    return best.get("hts_code")


def _disable_cache_reads() -> None:
    """劣化検知のため L1/L2 キャッシュの読み出しを無効化する。"""
    import analysis_cache
    analysis_cache.get_cached_analysis = lambda *a, **k: None
    analysis_cache.get_cached_chapters = lambda *a, **k: None
    analysis_cache.get_cached_hts = lambda *a, **k: None


def main() -> int:
    if os.environ.get("HTS_RUN_API_TESTS") != "1":
        print("[SKIP] APIテストは既定で無効です（課金あり）。")
        print("       実行するには HTS_RUN_API_TESTS=1 を設定してください。")
        return 0

    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    img_dir = Path(data["image_dir"])
    cases = data["cases"]

    _disable_cache_reads()
    from app import _classify_one, AUTO_KEY

    passed = failed = skipped = 0
    print(f"AI解析品質テスト（要API）: {len(cases)} ケース\n")
    for case in cases:
        name = case["image"]
        expect = case["expect_prefix"]
        path = img_dir / name
        if not path.exists():
            print(f"[SKIP] {name:<22} 画像が見つかりません: {path}")
            skipped += 1
            continue
        img = _FileLike(path.read_bytes(), name)
        try:
            result = _classify_one(img, "", AUTO_KEY)
        except Exception as e:
            print(f"[FAIL] {name:<22} 例外: {e}")
            failed += 1
            continue
        got = _adopted_code(result)
        ok = bool(got) and got.startswith(expect)
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name:<22} 期待={expect}*  実際={got}"
        if not ok:
            line += f"   ({case.get('note','')})"
        print(line)
        passed += ok
        failed += (not ok)

    print(f"\n結果: {passed} PASS / {failed} FAIL / {skipped} SKIP（全{len(cases)}件）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
