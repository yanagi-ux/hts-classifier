"""USITC HTS APIから指定Chapterのデータをダウンロードして data/ に保存するスクリプト。

使い方:
    python fetch_chapter.py 84        # Chapter 84のみ
    python fetch_chapter.py 84 85 87  # 複数Chapter
    python fetch_chapter.py --all     # config.pyのSUPPORTED_CHAPTERSすべて
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

USITC_RANGES = "https://hts.usitc.gov/reststop/ranges?docNumber={chapter}"
USITC_EXPORT = "https://hts.usitc.gov/reststop/exportList?from={frm}&to={to}&format=JSON&styles=true"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://hts.usitc.gov/",
}


def _get_ranges(chapter_str: str) -> tuple[str, str]:
    url = USITC_RANGES.format(chapter=chapter_str)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        rng = json.loads(resp.read().decode("utf-8"))
    return rng["Starting_Number"], rng["Ending_Number"]


def fetch_chapter(chapter_num: str) -> list[dict]:
    chapter_str = str(int(chapter_num)).zfill(2)
    frm, to = _get_ranges(chapter_str)
    url = USITC_EXPORT.format(frm=frm, to=to)
    print(f"  GET {url}")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def save_chapter(chapter_num: str, data: list[dict]) -> Path:
    chapter_str = str(int(chapter_num)).zfill(2)
    out_path = DATA_DIR / f"hts_ch{chapter_str}_raw.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="USITC HTSデータをダウンロードする")
    parser.add_argument("chapters", nargs="*", help="対象Chapter番号 (例: 84 85 87)")
    parser.add_argument("--all", action="store_true", help="config.pyのSUPPORTED_CHAPTERSをすべてダウンロード")
    parser.add_argument("--force", action="store_true", help="既存ファイルを上書きする")
    args = parser.parse_args()

    if args.all:
        from config import SUPPORTED_CHAPTERS
        chapters = list(SUPPORTED_CHAPTERS.keys())
    elif args.chapters:
        chapters = args.chapters
    else:
        parser.print_help()
        sys.exit(1)

    success, skipped, failed = 0, 0, 0
    for ch in chapters:
        chapter_str = str(int(ch)).zfill(2)
        out_path = DATA_DIR / f"hts_ch{chapter_str}_raw.json"
        if out_path.exists() and not args.force:
            print(f"[SKIP] Chapter {chapter_str} — {out_path.name} は既に存在します (--force で上書き)")
            skipped += 1
            continue
        print(f"[DL]  Chapter {chapter_str}...")
        try:
            data = fetch_chapter(ch)
            path = save_chapter(ch, data)
            print(f"[OK]  {path.name} ({len(data)} entries)")
            success += 1
        except Exception as e:
            print(f"[ERR] Chapter {chapter_str}: {e}")
            failed += 1
        time.sleep(0.5)

    print(f"\n完了: {success}件取得, {skipped}件スキップ, {failed}件エラー")


if __name__ == "__main__":
    main()
