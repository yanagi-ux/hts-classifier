"""駿河屋のジャンル・カテゴリ情報をスクレイピングしてDBを構築するスクリプト。

出力: data/surugaya_genres.json
  {
    "categories": [
      {
        "jp_name": "フィギュア",
        "parent": "おもちゃ・ホビー",
        "hts_chapter": "95",
        "en_keywords": ["figure", "toy figure", "doll"]
      }, ...
    ]
  }
"""
import json
import re
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_PATH = DATA_DIR / "surugaya_genres.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_text_links(html: str, base_url: str = "") -> list[tuple[str, str]]:
    """href + テキストのペアを抽出する。"""
    pattern = r'href="(/[^"]*)"[^>]*>\s*([^\s<][^<]{0,40}?)\s*</a>'
    results = []
    for href, text in re.findall(pattern, html):
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text and len(text) >= 2:
            results.append((href, text))
    return results


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


# ===== カテゴリ定義 =====
# (日本語親カテゴリ名, URL, HTS章, 英語キーワード候補)
TOP_CATEGORIES = [
    ("フィギュア",              "/toreka/figure_menu.html",    "95", ["figure", "toy figure", "doll", "action figure"]),
    ("プラモデル",              "/toreka/plamodel_menu.html",  "95", ["plastic model", "scale model", "model kit"]),
    ("トレーディングカード",     "/toreka/toreka_menu.html",    "95", ["trading card", "playing card", "card game"]),
    ("グッズ・ファッション",     "/toreka/goods_menu.html",     "95", ["goods", "merchandise", "novelty"]),
    ("ぬいぐるみ",              "/hobby/nuigurumi/nuigurumi.html", "95", ["stuffed animal", "plush toy", "stuffed toy"]),
    ("ボードゲーム",            "/hobby/boardgame/index.html", "95", ["board game", "game", "puzzle"]),
    ("おもちゃ・ホビー全般",    "/hobby.html",                 "95", ["toy", "hobby"]),
    ("ゲーム(現行機)",          "/game.html",                  "95", ["video game", "game software", "game cartridge"]),
    ("レトロゲーム",            "/vintagegame.html",           "95", ["video game", "retro game", "game cartridge"]),
    ("書籍・コミック",          "/books.html",                 "49", ["printed book", "comic book", "manga", "publication"]),
    ("同人誌",                  "/dozin.html",                 "49", ["printed book", "doujinshi", "self-published book", "booklet"]),
    ("BL・TL",                  "/boyslove.html",              "49", ["printed book", "comic book", "publication"]),
    ("音楽ソフト(CD)",          "/cd.html",                    "85", ["music cd", "compact disc", "recorded compact disc"]),
    ("映像ソフト(DVD/Blu-ray)", "/avsoft.html",                "85", ["dvd", "blu-ray", "recorded optical disc"]),
    ("家電・カメラ・AV機器",    "/kaden.html",                 "85", ["electronic equipment", "camera", "audio visual"]),
    ("パソコン・スマホ",        "/pcsp.html",                  "84", ["computer", "laptop", "smartphone"]),
]

# サブカテゴリリンクを取得してカテゴリを細分化するページ
DETAIL_PAGES = {
    "/toreka/figure_menu.html": ("95", ["figure", "toy figure", "doll"]),
    "/toreka/goods_menu.html":  ("95", ["goods", "merchandise"]),
    "/hobby.html":              ("95", ["toy", "hobby"]),
    "/game.html":               ("95", ["video game", "game"]),
    "/books.html":              ("49", ["printed book", "publication"]),
    "/dozin.html":              ("49", ["printed book", "booklet", "doujinshi"]),
    "/cd.html":                 ("85", ["music cd", "recorded disc"]),
    "/avsoft.html":             ("85", ["dvd", "recorded disc"]),
    "/kaden.html":              ("85", ["electronic equipment"]),
}


def scrape_subcategories(path: str, parent_jp: str, hts_chapter: str, base_keywords: list[str]) -> list[dict]:
    url = "https://www.suruga-ya.jp" + path
    try:
        html = fetch(url)
        links = extract_text_links(html)
        entries = []
        for href, text in links:
            # ジャンル名らしいもの（カタカナ・漢字・ひらがな混じりのテキスト）
            if not re.search(r"[぀-ヿ一-鿿]", text):
                continue
            if len(text) < 2 or len(text) > 30:
                continue
            entries.append({
                "jp_name": text,
                "parent": parent_jp,
                "url": href,
                "hts_chapter": hts_chapter,
                "en_keywords": list(base_keywords),
            })
        return entries[:50]
    except Exception as e:
        print(f"  [WARN] {path}: {e}")
        return []


def main():
    all_categories = []

    # トップカテゴリを追加
    for jp_name, path, hts_ch, keywords in TOP_CATEGORIES:
        all_categories.append({
            "jp_name": jp_name,
            "parent": None,
            "url": path,
            "hts_chapter": hts_ch,
            "en_keywords": keywords,
        })

    # サブカテゴリをスクレイピング
    for path, (hts_ch, keywords) in DETAIL_PAGES.items():
        parent_name = next((c["jp_name"] for c in all_categories if c["url"] == path), path)
        print(f"Scraping {path} ...")
        subs = scrape_subcategories(path, parent_name, hts_ch, keywords)
        print(f"  -> {len(subs)} subcategories")
        all_categories.extend(subs)
        time.sleep(0.5)

    # 重複除去（jp_name + parent でユニーク）
    seen = set()
    unique = []
    for c in all_categories:
        key = (c["jp_name"], c.get("parent"))
        if key not in seen:
            seen.add(key)
            unique.append(c)

    result = {"categories": unique}
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n完了: {len(unique)} カテゴリ -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
