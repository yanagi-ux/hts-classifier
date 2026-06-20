"""jp_labels.json の未翻訳エントリを Claude API で一括翻訳するスクリプト。

使い方:
    python translate_labels.py

途中で止めても再実行すれば続きから再開できます。
"""
import json
import re
import time
from pathlib import Path

import anthropic

BASE = Path(__file__).parent
JP_LABELS_PATH = BASE / "jp_labels.json"

# ── 設定 ────────────────────────────────────────────────────────────────────
BATCH_SIZE = 50       # 1回のAPIコールで翻訳する件数
SAVE_INTERVAL = 5     # 何バッチごとにファイル保存するか
MODEL = "claude-haiku-4-5-20251001"  # コスト削減のためHaikuを使用


def load_jp_labels() -> dict:
    if JP_LABELS_PATH.exists():
        with open(JP_LABELS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_jp_labels(jp: dict) -> None:
    with open(JP_LABELS_PATH, "w", encoding="utf-8") as f:
        json.dump(jp, f, ensure_ascii=False, indent=2)


def collect_missing(jp: dict) -> list[str]:
    missing = set()
    for data_file in (BASE / "data").glob("hts_ch*_raw.json"):
        with open(data_file, encoding="utf-8") as f:
            data = json.load(f)
        for e in data:
            lbl = (e.get("description") or "").rstrip(":").strip()
            # HTMLタグを除去してキーとして使う
            clean = re.sub(r"<[^>]+>", "", lbl).strip()
            if clean and clean not in jp:
                missing.add(clean)
    return sorted(missing)


def translate_batch(client: anthropic.Anthropic, labels: list[str]) -> dict[str, str]:
    numbered = "\n".join(f"{i+1}. {lbl}" for i, lbl in enumerate(labels))
    prompt = (
        "以下は米国HTS（関税分類）の英語ラベルです。\n"
        "各ラベルを簡潔な日本語に翻訳してください。\n"
        "専門用語は通関・貿易で使われる正式な日本語表現を使ってください。\n"
        "出力は番号付きリスト形式のみ（説明不要）:\n\n"
        f"{numbered}\n\n"
        "出力形式:\n"
        "1. 日本語訳\n"
        "2. 日本語訳\n"
        "..."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    result = {}
    for line in raw.splitlines():
        m = re.match(r"^\s*(\d+)\.\s+(.+)$", line.strip())
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(labels):
                result[labels[idx]] = m.group(2).strip()
    return result


def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()

    try:
        import streamlit as st
        api_key = st.secrets.get("ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    except Exception:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません")
        return

    client = anthropic.Anthropic(api_key=api_key)
    jp = load_jp_labels()
    missing = collect_missing(jp)

    print(f"既存翻訳: {len(jp)}件")
    print(f"未翻訳: {len(missing)}件")
    print(f"バッチサイズ: {BATCH_SIZE}件 / モデル: {MODEL}")
    print("翻訳を開始します...\n")

    total_batches = (len(missing) + BATCH_SIZE - 1) // BATCH_SIZE
    translated = 0

    for batch_idx in range(total_batches):
        batch = missing[batch_idx * BATCH_SIZE: (batch_idx + 1) * BATCH_SIZE]
        print(f"バッチ {batch_idx + 1}/{total_batches} ({len(batch)}件)...", end=" ", flush=True)

        try:
            result = translate_batch(client, batch)
            jp.update(result)
            translated += len(result)
            print(f"完了 ({len(result)}件翻訳)")
        except anthropic.RateLimitError:
            print("レート制限 - 30秒待機...")
            time.sleep(30)
            try:
                result = translate_batch(client, batch)
                jp.update(result)
                translated += len(result)
                print(f"完了 ({len(result)}件翻訳)")
            except Exception as e:
                print(f"スキップ: {e}")
        except Exception as e:
            print(f"エラー: {e}")

        if (batch_idx + 1) % SAVE_INTERVAL == 0:
            save_jp_labels(jp)
            print(f"  -> 保存済み（累計 {translated}件）")

        time.sleep(0.5)

    save_jp_labels(jp)
    print(f"\n完了！ 合計 {translated}件翻訳。jp_labels.json を更新しました。")


if __name__ == "__main__":
    main()
