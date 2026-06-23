"""Haiku と Sonnet の解析結果を同一画像で突き合わせ、精度差を検証する。

各画像を両モデルで1回ずつ解析（統合プロンプト：解析＋章推定）し、
章・材質・カテゴリを比較してCSVと一致率サマリを出力する。

使い方:
    python compare_models.py [画像フォルダ] [--limit N]
前提: 通常モード（有効な ANTHROPIC_API_KEY）。MOCKは自動で無効化。
"""
import os
import sys
import csv
import base64

os.environ["MOCK_MODE"] = "0"
os.environ["HYBRID_MODE"] = "0"  # 比較のため各モデルを直接指定

from pathlib import Path
import anthropic

from config import SUPPORTED_CHAPTERS, get_api_key, CLAUDE_MODEL, CLAUDE_MODEL_FAST
import image_analyzer as ia
from category_lookup import lookup_chapters

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _run(client, data, name, model, chapters_list):
    resized, mime = ia._resize_image(data, name)
    b64 = base64.b64encode(resized).decode("utf-8")
    system = [{
        "type": "text",
        "text": (
            ia.SYSTEM_PROMPT
            + "\n\n【追加指示】上記に加えて該当する米国HTS章番号を最大3つ \"chapter_hints\" に。\n"
              f"章の選択肢:\n{chapters_list}\n"
            + '{"material":"...","function":"...","category_hint":"...","keywords":["..."],"chapter_hints":["95"]}'
        ),
        "cache_control": {"type": "ephemeral"},
    }]
    img = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
    txt = {"type": "text", "text": "補足情報なし"}
    resp = ia._api_call_with_backoff(
        client.messages.create, model=model, max_tokens=600, system=system,
        messages=[{"role": "user", "content": [img, txt]}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    parsed = ia._parse_json_response(raw)
    hints = [str(h) for h in parsed.get("chapter_hints", []) if str(h) in SUPPORTED_CHAPTERS]
    a = {"material": parsed.get("material", ""), "function": parsed.get("function", ""),
         "category_hint": parsed.get("category_hint", ""), "keywords": parsed.get("keywords", [])}
    for _ in range(3):
        if not ia._contains_non_english(a):
            break
        a = ia._translate_to_english(client, a)
    a = ia._normalize_terms(a)
    return a, hints[:3]


def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    folder = Path(args[0]) if args else Path(__file__).parent / "warmup_images"
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    if not get_api_key():
        print("ERROR: ANTHROPIC_API_KEY が未設定です。")
        return
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in _IMG_EXTS)
    if limit:
        images = images[:limit]
    if not images:
        print(f"画像が見つかりません: {folder}")
        return

    client = anthropic.Anthropic(api_key=get_api_key())
    chapters_list = "\n".join(f"  {k}: {v['label']}" for k, v in SUPPORTED_CHAPTERS.items())

    print(f"対象: {len(images)}枚 / Haiku={CLAUDE_MODEL_FAST} vs Sonnet={CLAUDE_MODEL}")
    print(f"API呼び出し: 約{len(images)*2}回\n")

    rows, top_match, any_match, mat_match = [], 0, 0, 0
    for i, p in enumerate(images, 1):
        data = p.read_bytes()
        ha, hh = _run(client, data, p.name, CLAUDE_MODEL_FAST, chapters_list)
        sa, sh = _run(client, data, p.name, CLAUDE_MODEL, chapters_list)
        top = bool(hh and sh and hh[0] == sh[0])
        anyov = bool(set(hh) & set(sh))
        matm = (ha.get("material", "").lower().strip() == sa.get("material", "").lower().strip())
        top_match += top; any_match += anyov; mat_match += matm
        rows.append({
            "ファイル": p.name,
            "Haiku材質": ha.get("material", ""), "Sonnet材質": sa.get("material", ""),
            "Haikuカテゴリ": ha.get("category_hint", ""), "Sonnetカテゴリ": sa.get("category_hint", ""),
            "Haiku章": ",".join(hh), "Sonnet章": ",".join(sh),
            "章トップ一致": "〇" if top else "×",
            "章重複あり": "〇" if anyov else "×",
            "材質一致": "〇" if matm else "×",
        })
        print(f"[{i}/{len(images)}] {p.name}  H章={hh} S章={sh}  top={'〇' if top else '×'}")

    out = "compare_models_result.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    n = len(images)
    print(f"\n=== サマリ（{n}枚） ===")
    print(f"章トップ一致率 : {top_match}/{n} = {top_match/n:.0%}")
    print(f"章いずれか一致 : {any_match}/{n} = {any_match/n:.0%}")
    print(f"材質一致率     : {mat_match}/{n} = {mat_match/n:.0%}")
    print(f"詳細: {out}")


if __name__ == "__main__":
    main()
