"""指定フォルダの画像を実APIで一括判定し、キャッシュへ溜めるウォームアップ。

モックデモで「本物の解析・章推定結果」を課金ゼロ再現するための事前処理。
実行後、data/analysis_cache.db に L1解析・章推定の実績が保存される。
これをコミットすればStreamlitCloudのモックデモでもキャッシュHit分は本物の結果になる。

使い方:
    python warmup_cache.py [画像フォルダ]      # 既定は ./warmup_images

前提:
    - 通常モード（MOCK_MODE未設定/0）で実行すること
    - 有効な ANTHROPIC_API_KEY が .env か環境変数に設定されていること
"""
import os
import sys
from pathlib import Path

# 実APIで溜めるためモックを無効化（config 読み込み前に設定）
os.environ["MOCK_MODE"] = "0"

from config import SUPPORTED_CHAPTERS, get_api_key  # noqa: E402
import image_analyzer as ia  # noqa: E402

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_EST_PER_CALL_USD = 0.012  # 概算（リサイズ済み画像 + Sonnet）


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "warmup_images"

    if not get_api_key():
        print("ERROR: ANTHROPIC_API_KEY が未設定です。通常モードの有効なキーを設定してください。")
        return
    if not folder.exists():
        print(f"ERROR: フォルダが見つかりません: {folder}")
        print("　画像を入れたフォルダを引数で指定するか、./warmup_images を作成してください。")
        return

    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in _IMG_EXTS)
    if not images:
        print(f"画像が見つかりません: {folder}")
        return

    print(f"対象画像: {len(images)} 件 / フォルダ: {folder}")
    print("実APIで解析＋章推定を実行し、キャッシュに保存します...\n")

    api_calls = 0
    for i, p in enumerate(images, 1):
        data = p.read_bytes()

        # ① 画像解析（L1キャッシュへ保存される）
        analyses = ia.analyze_image_ensemble(data, p.name, "", n=1)
        analysis = analyses[0] if analyses else {}
        cached = bool(analysis.get("_cache_hit"))
        if not cached:
            api_calls += 1

        # ② 章推定（AUTO相当：全章対象。章キャッシュへ保存される）
        img_hint = " ".join(filter(None, [
            analysis.get("category_hint", ""),
            analysis.get("function", ""),
            " ".join(analysis.get("keywords", [])),
        ]))
        chapters = ia.predict_chapters(
            img_hint, SUPPORTED_CHAPTERS, image_bytes=data, filename=p.name
        )
        api_calls += 1  # predict_chapters は通常API1回

        mark = "(L1ヒット)" if cached else ""
        print(f"[{i}/{len(images)}] {p.name}  解析{mark}  推定章={chapters or '—'}")

    est = api_calls * _EST_PER_CALL_USD
    print(f"\n完了。API呼び出し概算: {api_calls} 回（推定 約 ${est:.2f}）")
    print("保存先: data/analysis_cache.db")
    print("クラウドのモックデモで使うには、キャッシュをコミットしてください:")
    print("    git add -f data/analysis_cache.db && git commit -m 'Add warmed cache' && git push")


if __name__ == "__main__":
    main()
