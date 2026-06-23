"""テキスト+画像由来キーワードを用いたルールベースHTSコード照合。"""
import json
import re
from pathlib import Path

from hts_db import load_classifiable_entries

SYNONYMS_PATH = Path(__file__).parent / "synonyms.json"
HTS_OVERRIDES_PATH = Path(__file__).parent / "hts_overrides.json"
CATEGORY_MAP_PATH = Path(__file__).parent / "hts_category_map.json"

# カテゴリ→見出し(HS4)変換リストでブーストするスコア。
# 10桁override(999)より低くし、明示overrideが優先されるようにする。
HEADING_BOOST_SCORE = 900.0


def _load_overrides() -> dict:
    if not HTS_OVERRIDES_PATH.exists():
        return {}
    with open(HTS_OVERRIDES_PATH, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


# AIが日本語で返すケース対策：代表的な日本語カテゴリ語 → 英語オーバーライドキーのマッピング
_JA_TO_EN_HINT: dict[str, str] = {
    "ショーツ": "shorts", "短パン": "shorts", "ハーフパンツ": "shorts",
    "ズボン": "trousers", "パンツ": "trousers", "スラックス": "trousers",
    "ジーンズ": "jeans", "デニム": "denim",
    "tシャツ": "t-shirt", "ｔシャツ": "t-shirt",
    "スニーカー": "sneaker", "運動靴": "athletic shoe", "スポーツシューズ": "sports shoe",
    "ピンバッジ": "pin badge", "缶バッジ": "button badge", "バッジ": "badge",
    "アクリルスタンド": "acrylic stand", "アクスタ": "acrylic stand",
    "チャーム": "charm", "キーチェーン": "charm",
    "トレーディングカード": "trading card", "トレカ": "trading card",
    "ゲーム機": "game console", "ゲームソフト": "game console",
    "テレビ": "television", "テレビ受像機": "television",
    "dvd": "recorded dvd", "ブルーレイ": "recorded blu-ray",
    "同人誌": "doujinshi", "同人": "self-published",
    "漫画": "manga", "コミック": "comic book",
    "ゴム印": "rubber stamp", "スタンプ": "rubber stamp",
    "造花": "artificial flower", "プレスフラワー": "pressed flower",
    "ドライフラワー": "dried flower",
    "レンチ": "wrench", "スパナ": "spanner",
    "綿生地": "woven cotton fabric", "プリント生地": "printed cotton fabric",
    "綿布": "woven cotton fabric",
    "ステアリングカバー": "steering wheel cover",
    "カッティングマット": "cutting mat",
    "風呂敷": "furoshiki wrapping cloth",
    "ラッピングクロス": "wrapping cloth",
    "パッチワーク": "patchwork fabric",
    "装飾用織物": "decorative cloth",
    "装飾的な布地": "decorative cloth",
}


def _normalize_hint(text: str) -> str:
    """日本語カテゴリ語を英語に正規化して返す（元テキストも保持）。"""
    result = text.lower()
    for ja, en in _JA_TO_EN_HINT.items():
        if ja.lower() in result:
            result += f" {en}"
    return result


def apply_hts_overrides(results: dict[str, list[dict]], queries: list[dict]) -> dict[str, list[dict]]:
    """category_hint が hts_overrides.json のキーに一致する場合、
    対象 HTS コードを該当章の先頭に移動する。
    コードが results に存在しない場合はスキップ。
    """
    overrides = _load_overrides()
    if not overrides:
        return results

    # クエリの category_hint + keywords を結合してチェック（判別語がキーワード側に
    # あるケース（例: 同人誌の "self-published book"）も拾えるようにする）
    # 日本語で返った場合も英語キーに正規化して照合する
    def _hint_text(q: dict) -> str:
        kws = q.get("keywords", [])
        kw_str = " ".join(kws) if isinstance(kws, list) else str(kws)
        return f"{q.get('category_hint', '')} {kw_str}"
    combined_hint = _normalize_hint(" ".join(
        _hint_text(q) for q in queries if isinstance(q, dict)
    ))
    combined_material = " ".join(
        q.get("material", "") for q in queries if isinstance(q, dict)
    ).lower()

    for keyword, info in overrides.items():
        if keyword not in combined_hint:
            continue
        # info は単一dict、または材質条件つきの複数ルール(list)
        rules = info if isinstance(info, list) else [info]
        for rule in rules:
            # material 条件があり、クエリ材質に一致しなければスキップ
            mat = rule.get("material", "")
            if mat and mat.lower() not in combined_material:
                continue
            ch = rule["chapter"]
            target_code = rule["hts_code"]
            label_ja = rule.get("label_ja", "")
            ch_results = results.get(ch, [])
            idx = next((i for i, r in enumerate(ch_results) if r["hts_code"] == target_code), None)
            if idx is not None:
                if idx != 0:
                    ch_results.insert(0, ch_results.pop(idx))
                # 採用（全章中の最高スコア）として選ばれるようスコアを最上位に底上げ
                ch_results[0]["effective_score"] = 999.0
                ch_results[0]["score"] = 999.0
                if label_ja:
                    ch_results[0]["_label_ja"] = label_ja
                results[ch] = ch_results
            else:
                # top-N結果に無い場合は章データから該当コードを読み込んで先頭に注入
                injected = _build_override_entry(ch, target_code)
                if injected:
                    if label_ja:
                        injected["_label_ja"] = label_ja
                    results[ch] = [injected] + ch_results
            break  # 最初に材質一致したルールのみ適用

    return results


def _load_category_map() -> dict:
    """カテゴリ→見出し(HS4)変換リストを読み込む。"""
    if not CATEGORY_MAP_PATH.exists():
        return {}
    with open(CATEGORY_MAP_PATH, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def _combined_query_weights(queries: list) -> dict[str, float]:
    """複数クエリを結合し、シノニム展開＋文脈フィルタ適用後の重みを返す。"""
    synonyms = _load_synonyms()
    weights: dict[str, float] = {}
    for q in queries:
        if isinstance(q, dict):
            qw = build_query_weights(q)
        else:
            qw = {t: 1.0 for t in _tokenize(str(q))}
        for t, w in qw.items():
            weights[t] = max(weights.get(t, 0.0), w)
    weights = _expand_weights_with_synonyms(weights, synonyms)
    weights = _filter_context_dependent_tokens(weights)
    return weights


def _best_leaf_under_heading(chapter: str, heading: str,
                             query_weights: dict[str, float]) -> dict | None:
    """指定見出し(HS4)配下の末端コードをスコアリングし、最良の1件を返す。

    「カテゴリ→見出し」が分かれば末尾の枝番(HS10)はスコアラーに選ばせる、
    という変換リストの中核。見出し配下で一切マッチしない場合は最も浅い
    （代表的な）末端を採用する。
    """
    try:
        from config import SUPPORTED_CHAPTERS
        data_file = SUPPORTED_CHAPTERS[chapter]["data_file"]
        entries = [e for e in load_classifiable_entries(data_file)
                   if str(e["hts_code"]).replace(".", "").startswith(heading.replace(".", ""))]
    except Exception:
        return None
    if not entries:
        return None
    scored = _score_entries(query_weights, entries)
    if scored:
        return max(scored.values(),
                   key=lambda e: (e["score"], e["match_rate"], e["own_match_ratio"], e["indent"]))
    base = min(entries, key=lambda e: e.get("indent", 99))
    return {**base, "score": 0.0, "match_rate": 0.0,
            "own_match_ratio": 0.0, "matched_keywords": []}


def apply_category_heading_map(results: dict[str, list[dict]],
                               queries: list[dict]) -> dict[str, list[dict]]:
    """カテゴリ→見出し(HS4)変換リストを適用する。

    AIの画像解析は正確でも、表層の単語一致では正しい見出しに結び付かない
    ことがある（例: "electric guitar" が "Fretted stringed instruments" に
    一致しない）。category_hint/keywords がカテゴリ語に一致したら、対応する
    見出し配下の最良の末端コードを先頭に昇格させる。10桁override より保守が軽く、
    枝番はスコアリングで自動選択するため類似品にも汎化する。
    """
    cmap = _load_category_map()
    if not cmap:
        return results

    def _hint_text(q: dict) -> str:
        kws = q.get("keywords", [])
        kw_str = " ".join(kws) if isinstance(kws, list) else str(kws)
        return f"{q.get('category_hint', '')} {q.get('function', '')} {kw_str}"

    combined_hint = _normalize_hint(" ".join(
        _hint_text(q) for q in queries if isinstance(q, dict)
    ))
    query_weights = _combined_query_weights(queries)

    # 長いキー優先（"electric guitar" を "guitar" より先に評価）
    for keyword in sorted(cmap, key=len, reverse=True):
        if keyword not in combined_hint:
            continue
        info = cmap[keyword]
        ch = info["chapter"]
        heading = info["heading"]
        best = _best_leaf_under_heading(ch, heading, query_weights)
        if not best:
            continue
        target_code = best["hts_code"]
        ch_results = [r for r in results.get(ch, []) if r["hts_code"] != target_code]
        boosted = {
            **best,
            "chapter_key": ch,
            "effective_score": HEADING_BOOST_SCORE,
            "score": best.get("score", 0.0) or HEADING_BOOST_SCORE,
            "_ensemble_hit_count": 1,
            "_ensemble_n": 1,
            "_heading_map": True,
        }
        if info.get("label_ja"):
            boosted["_label_ja"] = info["label_ja"]
        results[ch] = [boosted] + ch_results
        break  # 最初に一致した（最長）カテゴリのみ適用
    return results


# ① AI章ヒント優遇:
# 画像解析AIは商品区分(=章)を高精度に当てる。confidence が high のときは、AIが
# 推定した章(順位順)のエントリを優遇し、章をまたぐスコア競合でAIの判断を尊重する。
# 越境マッチ(別章のおとりが表面スコアで勝つ)を、手作業の include/exclude 辞書に
# 頼らず抑える。high でないときは従来どおりスコア任せ(安全側)。
# heading-map / override より前に適用するため、それらの確定スコア(900/999)とは競合しない。
CHAPTER_HINT_BOOST = {0: 1.5, 1: 1.15}  # 推定順位 → 倍率（それ以外は等倍）


def apply_chapter_hint_boost(results: dict, ordered_chapters: list, confidence) -> dict:
    """confidence が high のとき、ordered_chapters(AI推定章の順位順)の上位章の
    effective_score を優遇する。順位0が最も強く、CHAPTER_HINT_BOOST に無い順位は等倍。"""
    if (confidence or "").lower() != "high":
        return results
    rank = {ch: i for i, ch in enumerate(ordered_chapters)}
    for ch, rows in results.items():
        factor = CHAPTER_HINT_BOOST.get(rank.get(ch, 99))
        if not factor:
            continue
        for r in rows:
            r["effective_score"] = r.get("effective_score", 0.0) * factor
    return results


def _build_override_entry(chapter: str, target_code: str) -> dict | None:
    """章データから target_code のエントリを読み込み、結果dict形式に整える。"""
    try:
        from config import SUPPORTED_CHAPTERS
        from hts_db import load_classifiable_entries
        data_file = SUPPORTED_CHAPTERS[chapter]["data_file"]
        entry = next(
            (e for e in load_classifiable_entries(data_file) if e["hts_code"] == target_code),
            None,
        )
    except Exception:
        return None
    if not entry:
        return None
    return {
        **entry,
        "score": 999.0,
        "match_rate": 1.0,
        "own_match_ratio": 1.0,
        "matched_keywords": [],
        "chapter_key": chapter,
        "effective_score": 999.0,
        "_ensemble_hit_count": 1,
        "_ensemble_n": 1,
        "_override": True,
    }

STOPWORDS = {
    "and", "or", "for", "the", "of", "with", "in", "on", "to", "a", "an",
    "is", "are", "be", "by", "as", "at", "from", "other", "all", "thereof",
}


def _load_synonyms() -> dict:
    with open(SYNONYMS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _stem(word: str) -> str:
    """単数/複数などの語形差を吸収する簡易ステミング。"""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    # "-es" を取るのは sibilant(s/x/z/ch/sh)+es のときだけ。
    # それ以外の "...es"(bicycles/cycles/vehicles/tables 等)は単に "s" を取り、
    # 単数形(bicycle 等)と一致させる。旧実装は "bicycles"→"bicycl" として
    # 単数 "bicycle" と不一致になっていた。
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokenize(text: str) -> set[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    return {_stem(t) for t in tokens}


def _expand_with_synonyms(tokens: set[str], synonyms: dict) -> set[str]:
    expanded = set(tokens)
    joined = " ".join(tokens)
    for term, alts in synonyms.items():
        term_tokens = {_stem(t) for t in re.findall(r"[a-z0-9]+", term.lower())}
        # 単語キーはトークン一致のみ。フレーズ(スペース含む)のみ部分文字列照合を許可。
        # （"cd" が "lcd" に部分一致する等の誤展開を防ぐ）
        if term_tokens <= tokens or (" " in term and term.lower() in joined):
            for alt in alts:
                expanded |= {_stem(t) for t in re.findall(r"[a-z0-9]+", alt.lower())}
    return expanded


# HTSは「商品が物理的に何であるか(材質・形状・名称)」で分類するルールのため、
# 用途・使い方の説明より、材質や物の名称(物理的な区分)を強く評価する。
FIELD_WEIGHTS = {
    "product_name": 3.0,     # 品名(物の名称)
    "material": 3.0,         # 材質
    "category_hint": 3.0,    # 物理的な商品区分(画像解析)
    "keywords": 2.5,         # 物理的特徴を表すキーワード(画像解析)
    "spec": 1.5,              # 仕様・補足
    "function": 1.0,          # 用途・機能(最も重みを下げる)
}


def build_query_weights(fields: dict) -> dict[str, float]:
    """fields: {"product_name": "...", "material": "...", "function": "...",
    "spec": "...", "category_hint": "...", "keywords": ["...", ...]} のような
    フィールド別テキストを受け取り、トークンごとの重みを返す。
    同じトークンが複数フィールドに出た場合は最も高い重みを採用する。
    """
    weights: dict[str, float] = {}
    for field_name, weight in FIELD_WEIGHTS.items():
        value = fields.get(field_name)
        if not value:
            continue
        text = " ".join(value) if isinstance(value, list) else str(value)
        for token in _tokenize(text):
            weights[token] = max(weights.get(token, 0.0), weight)
    return weights


def _expand_weights_with_synonyms(weights: dict[str, float], synonyms: dict) -> dict[str, float]:
    expanded = dict(weights)
    tokens = set(weights.keys())
    joined = " ".join(tokens)
    for term, alts in synonyms.items():
        term_tokens = {_stem(t) for t in re.findall(r"[a-z0-9]+", term.lower())}
        # 単語キーはトークン一致のみ。フレーズ(スペース含む)のみ部分文字列照合を許可。
        if term_tokens <= tokens or (" " in term and term.lower() in joined):
            source_weight = max((weights[t] for t in term_tokens if t in weights), default=1.0)
            for alt in alts:
                for t in {_stem(x) for x in re.findall(r"[a-z0-9]+", alt.lower())}:
                    expanded[t] = max(expanded.get(t, 0.0), source_weight)
    return expanded


OWN_DESC_WEIGHT = 3  # そのコード自身の説明文への一致の重み
ANCESTOR_DESC_WEIGHT = 1  # 継承された上位階層(見出し文)への一致の重み

# 主名詞/修飾語ペナルティ:
# 「自転車速度計(Bicycle speedometers)」のように、商品語(bicycle)が説明文の
# 主名詞ではなく"修飾語/目的語"位置でしか一致していないエントリは、その商品その
# ものではなく別物(速度計)の分類なので越境マッチである。一致が修飾語位置のみの
# とき score を減点して、正しい章の本来エントリ(主名詞一致)に順位を譲らせる。
HEAD_MODIFIER_PENALTY = 0.3
# 主名詞の前に来る節/前置詞マーカー。これがある場合は主名詞がマーカーの直前まで
# にあり(例 "Bicycles having both wheels..."→bicycle が主名詞)、無い場合は単純な
# 複合名詞句なので末尾語が主名詞(例 "Bicycle speedometers"→speedometers)。
_CLAUSE_MARKERS = {
    "having", "of", "with", "for", "used", "containing", "incorporating",
    "whether", "including", "than", "designed",
}


def _is_modifier_only_match(own_desc: str, own_matched: set[str]) -> bool:
    """own_desc の主名詞が own_matched(クエリ一致語)に含まれず、一致が修飾語位置の
    みのとき True。商品語が説明文の主役でない=越境マッチを示す。"""
    if not own_desc or not own_matched:
        return False
    raw = re.findall(r"[a-z0-9]+", own_desc.lower())
    stems = [_stem(t) for t in raw]
    meaningful = [s for s in stems if len(s) > 2 and s not in STOPWORDS]
    if not meaningful:
        return False
    head: str | None = None
    for i, t in enumerate(raw):
        if t in _CLAUSE_MARKERS:
            before = [s for s in stems[:i] if len(s) > 2 and s not in STOPWORDS]
            if before:
                head = before[-1]
            break
    if head is None:
        head = meaningful[-1]
    return head not in own_matched

# あるトークンが「副素材・副成分」として含まれるだけで無関係な章に誤マッチするのを防ぐ。
# required_context のいずれかが同時に存在しない限り、そのトークンをクエリから除外する。
#
# "video": ゲームキャラクターの説明で使われると9504(ビデオゲーム機)に誤マッチ。
#          実際の機器を示す語が伴う場合のみ有効。
# "plastic": ピンバッジ等で副素材として含まれるだけでChapter 39に引っ張られる。
#            "plastic"が主素材である場合(container/sheet/film/tube/pipe/bag等)のみ有効。
CONTEXT_DEPENDENT_TOKENS = {
    "video": {"consol", "machin", "cartridg", "arcad", "system"},
    "plastic": {
        "contain", "sheet", "film", "tube", "pipe", "bag", "box", "bottl",
        "tank", "tray", "cup", "plate", "panel", "foam", "sponge", "mat",
    },
    # "metal"は極めて汎用的で多数のHTSエントリに登場する。
    # 金属素材を特定する語(steel/iron/copper等)か金属製品を示す語が
    # 同時に存在する場合のみクエリに含める。
    "metal": {
        "steel", "iron", "copper", "aluminum", "aluminium", "zinc", "tin",
        "nickel", "chrome", "brass", "bronz", "tool", "machin", "structur",
        "construct", "wire", "rod", "bar", "sheet", "pipe", "tube",
    },
    # "imitation"は"imitation jewelry"の文脈でシノニム展開されるが、
    # Ch39「Imitation gemstones」にも誤マッチする。
    # "jewelry"か"gem"が同時に存在する場合のみ有効にする。
    "imitation": {"jewelry", "jewelleri", "gem", "jewel", "brooch"},
    # "decorative"はフィギュア・雑貨の説明でよく使われるが、
    # Ch85「8539.22.80.30 Decorative」（電球の分類）に誤マッチする。
    # 照明・電球・ランプ系の語が伴う場合のみ有効にする。
    "decorative": {
        "lamp", "bulb", "light", "lighting", "luminair", "filament",
        "discharg", "fluoresc", "led", "illumin", "chandelier",
    },
    # "paper"は印刷物クエリで material として含まれるが、
    # Ch39「Reinforced with paper」やCh48の紙製品に誤マッチしやすい。
    # 紙が主素材であることを示す語（container/bag/cup/sheet等）が
    # 伴う場合のみ有効にする。
    "paper": {
        "bag", "box", "cup", "contain", "label", "tag", "card", "sheet",
        "tube", "towel", "tissu", "napkin", "wrapper", "envelop", "sack",
    },
    # "sorted"/"rags"はCh63の6310（ぼろ布・古着くず）にしか現れない。
    # 明示的に「ぼろ布・古着」の文脈が無ければスコアに寄与させない。
    "sorted": {"rag", "scrap", "worn", "used", "waste", "cordage"},
    "rags": {"sorted", "scrap", "worn", "waste"},
    # "figure(s)" alone matches 9505.10.30.00 ("nativity scenes and figures").
    # Only score when christmas/nativity context is explicitly present.
    "figures": {"nativiti", "christma", "festiv", "creche", "manger"},
    "figure": {"nativiti", "christma", "festiv", "creche", "manger"},
    # "tank"/"reservoir"/"vat" in 3925 require large-capacity context.
    # Small storage boxes use 3924; only score 3925 for genuine tanks/silos.
    "tank": {"reserv", "capacit", "liter", "litre", "gallon", "silo", "cistern", "vat"},
    "reservoir": {"capacit", "liter", "litre", "gallon", "silo", "cistern", "tank"},
    "vat": {"capacit", "liter", "litre", "gallon", "cistern", "tank", "brew"},
    # "bed" alone scores 9402 (hospital/medical beds). Only score ch94
    # when medical/hospital context is present; pet/floor beds belong elsewhere.
    "bed": {"hospit", "medic", "patient", "surgic", "clinic", "nurs"},
    # "instrument" alone matches 4202.92.50.00 (musical instrument cases).
    # Only score when musical context is present.
    "instrument": {"music", "guitar", "violin", "piano", "trumpet", "flute", "drum"},
    # "folding" alone matches 8211.93 (multi-tool folding knives).
    # Fixed-blade kitchen knives should not score there.
    "folding": {"multi", "tool", "pocket", "swiss", "blade", "hunting"},
}


def _filter_context_dependent_tokens(weights: dict[str, float]) -> dict[str, float]:
    filtered = dict(weights)
    tokens = set(filtered.keys())
    for token, required_context in CONTEXT_DEPENDENT_TOKENS.items():
        if token in filtered and not (required_context & tokens):
            del filtered[token]
    return filtered


def classify(query, top_n: int = 5, chapter_file: str = "hts_ch95_raw.json") -> list[dict]:
    """query: フィールド別に重み付けされたdict({"product_name": ..., "material": ...,
    "function": ..., "category_hint": ..., "keywords": [...]}) を渡すと、材質・物の名称・
    物理的特徴を用途より重く評価する。単純な文字列を渡した場合は全フィールド同じ重みで扱う
    (後方互換用)。
    HTS各エントリのfull_descriptionとのキーワード一致度でスコアリングする。
    見出し文(上位階層から継承した部分)は全ての子コードに共通して含まれるため、
    そのコード自身の説明文に一致した場合より低い重みを与える。
    """
    synonyms = _load_synonyms()
    if isinstance(query, dict):
        query_weights = build_query_weights(query)
    else:
        query_weights = {t: 1.0 for t in _tokenize(str(query))}
    query_weights = _expand_weights_with_synonyms(query_weights, synonyms)
    query_weights = _filter_context_dependent_tokens(query_weights)
    query_tokens = set(query_weights.keys())

    entries = load_classifiable_entries(chapter_file)
    scored = []
    for entry in entries:
        own_desc = entry["description"]
        ancestor_desc, _, _ = entry["full_description"].rpartition(" > ") if own_desc else (entry["full_description"], "", "")

        own_tokens = _tokenize(own_desc)
        ancestor_tokens = _tokenize(ancestor_desc) - own_tokens

        own_meaningful_tokens = {t for t in own_tokens if len(t) > 2 and t not in STOPWORDS}
        ancestor_meaningful_tokens = {t for t in ancestor_tokens if len(t) > 2 and t not in STOPWORDS}
        own_matched = {t for t in (query_tokens & own_tokens) if len(t) > 2 and t not in STOPWORDS}
        ancestor_matched = {t for t in (query_tokens & ancestor_tokens) if len(t) > 2 and t not in STOPWORDS}

        own_matched_weight = sum(query_weights[t] for t in own_matched)
        ancestor_matched_weight = sum(query_weights[t] for t in ancestor_matched)

        score = own_matched_weight * OWN_DESC_WEIGHT + ancestor_matched_weight * ANCESTOR_DESC_WEIGHT
        if score == 0:
            continue

        # 「説明文全体のうち一致した語の割合」を一致度として表示する。
        # 自身の説明文(own)と継承文(ancestor)で重みを変え、短い説明文がたった1語の
        # 偶然の一致だけで100%になってしまわないよう、全体(分母)も同じ重みで計算する。
        own_match_ratio = len(own_matched) / len(own_meaningful_tokens) if own_meaningful_tokens else 0.0
        max_field_weight = max(FIELD_WEIGHTS.values())
        total_possible = (len(own_meaningful_tokens) * OWN_DESC_WEIGHT
                           + len(ancestor_meaningful_tokens) * ANCESTOR_DESC_WEIGHT) * max_field_weight
        match_rate = min(score / total_possible, 1.0) if total_possible else 0.0

        scored.append({
            **entry,
            "score": score,
            "match_rate": match_rate,
            "own_match_ratio": own_match_ratio,
            "matched_keywords": sorted(own_matched | ancestor_matched),
        })

    scored.sort(key=lambda e: (e["score"], e["match_rate"], e["own_match_ratio"], e["indent"]), reverse=True)
    return scored[:top_n]


def _score_entries(query_weights: dict[str, float], entries: list[dict]) -> dict[str, dict]:
    """エントリのリストをスコアリングして {hts_code: scored_entry} を返す内部ヘルパー。"""
    query_tokens = set(query_weights.keys())
    scored: dict[str, dict] = {}
    for entry in entries:
        own_desc = entry["description"]
        ancestor_desc, _, _ = entry["full_description"].rpartition(" > ") if own_desc else (entry["full_description"], "", "")

        own_tokens = _tokenize(own_desc)
        ancestor_tokens = _tokenize(ancestor_desc) - own_tokens

        own_meaningful_tokens = {t for t in own_tokens if len(t) > 2 and t not in STOPWORDS}
        ancestor_meaningful_tokens = {t for t in ancestor_tokens if len(t) > 2 and t not in STOPWORDS}
        own_matched = {t for t in (query_tokens & own_tokens) if len(t) > 2 and t not in STOPWORDS}
        ancestor_matched = {t for t in (query_tokens & ancestor_tokens) if len(t) > 2 and t not in STOPWORDS}

        own_matched_weight = sum(query_weights[t] for t in own_matched)
        ancestor_matched_weight = sum(query_weights[t] for t in ancestor_matched)

        score = own_matched_weight * OWN_DESC_WEIGHT + ancestor_matched_weight * ANCESTOR_DESC_WEIGHT
        if score == 0:
            continue

        # 商品語が own_desc の主名詞でなく修飾語位置のみで一致している越境エントリを減点。
        # (own一致が全く無く ancestor のみの一致の場合は対象外: own_matched が空なら影響なし)
        if own_matched and _is_modifier_only_match(own_desc, own_matched):
            score *= HEAD_MODIFIER_PENALTY

        own_match_ratio = len(own_matched) / len(own_meaningful_tokens) if own_meaningful_tokens else 0.0
        max_field_weight = max(FIELD_WEIGHTS.values())
        total_possible = (len(own_meaningful_tokens) * OWN_DESC_WEIGHT
                           + len(ancestor_meaningful_tokens) * ANCESTOR_DESC_WEIGHT) * max_field_weight
        match_rate = min(score / total_possible, 1.0) if total_possible else 0.0

        scored[entry["hts_code"]] = {
            **entry,
            "score": score,
            "match_rate": match_rate,
            "own_match_ratio": own_match_ratio,
            "matched_keywords": sorted(own_matched | ancestor_matched),
        }
    return scored


def classify_ensemble(
    queries: list[dict],
    top_n: int = 5,
    chapter_file: str = "hts_ch95_raw.json",
) -> list[dict]:
    """複数クエリのスコアを平均してランク付けすることでマッチングのブレを低減する。

    queries: [{"product_name": ..., "material": ..., ...}, ...] の複数クエリリスト。
    各クエリで独立してスコアリングし、同一HTSコードのスコアを平均して最終ランクを決定する。
    """
    synonyms = _load_synonyms()
    entries = load_classifiable_entries(chapter_file)

    # クエリごとのスコアマップを蓄積
    accumulated: dict[str, list[float]] = {}
    entry_cache: dict[str, dict] = {}

    for query in queries:
        if isinstance(query, dict):
            query_weights = build_query_weights(query)
        else:
            query_weights = {t: 1.0 for t in _tokenize(str(query))}
        query_weights = _expand_weights_with_synonyms(query_weights, synonyms)
        query_weights = _filter_context_dependent_tokens(query_weights)

        scored = _score_entries(query_weights, entries)
        for code, entry in scored.items():
            if code not in accumulated:
                accumulated[code] = []
                entry_cache[code] = entry
            accumulated[code].append(entry["score"])

    # スコアが0のクエリは平均の分母に含める(照合できなかった = 0点)
    n_queries = len(queries)
    merged: list[dict] = []
    for code, scores in accumulated.items():
        avg_score = sum(scores) / n_queries
        base = entry_cache[code]
        own_match_ratio = base["own_match_ratio"]
        effective_score = avg_score * max(own_match_ratio, 0.05)
        merged.append({
            **base,
            "score": avg_score,
            "effective_score": effective_score,
            "match_rate": base["match_rate"],
            "_ensemble_hit_count": len(scores),
            "_ensemble_n": n_queries,
        })

    merged.sort(key=lambda e: (e["effective_score"], e["match_rate"], e["own_match_ratio"], e["indent"]), reverse=True)
    return merged[:top_n]


def classify_per_chapter_ensemble(
    queries: list[dict],
    chapter_files: list[tuple[str, str]],
    top_n_per_chapter: int = 3,
) -> dict[str, list[dict]]:
    """複数クエリ × 複数章のアンサンブル版。

    chapter_files: [(chapter_key, data_file), ...]
    戻り値: {chapter_key: [top_n results...]}
    """
    synonyms = _load_synonyms()

    # クエリごとにweightsを事前計算
    all_weights: list[dict[str, float]] = []
    for query in queries:
        if isinstance(query, dict):
            qw = build_query_weights(query)
        else:
            qw = {t: 1.0 for t in _tokenize(str(query))}
        qw = _expand_weights_with_synonyms(qw, synonyms)
        qw = _filter_context_dependent_tokens(qw)
        all_weights.append(qw)

    n_queries = len(queries)
    result: dict[str, list[dict]] = {}

    for chapter_key, chapter_file in chapter_files:
        entries = load_classifiable_entries(chapter_file)
        accumulated: dict[str, list[float]] = {}
        entry_cache: dict[str, dict] = {}

        for query_weights in all_weights:
            scored = _score_entries(query_weights, entries)
            for code, entry in scored.items():
                if code not in accumulated:
                    accumulated[code] = []
                    entry_cache[code] = entry
                accumulated[code].append(entry["score"])

        eff_floor = 0.20 if chapter_key == "49" else 0.05
        merged: list[dict] = []
        for code, scores in accumulated.items():
            avg_score = sum(scores) / n_queries
            base = entry_cache[code]
            own_match_ratio = base["own_match_ratio"]
            effective_score = avg_score * max(own_match_ratio, eff_floor)
            merged.append({
                **base,
                "chapter_key": chapter_key,
                "score": avg_score,
                "effective_score": effective_score,
                "match_rate": base["match_rate"],
                "_ensemble_hit_count": len(scores),
                "_ensemble_n": n_queries,
            })

        merged.sort(
            key=lambda e: (e["effective_score"], e["match_rate"], e["own_match_ratio"], e["indent"]),
            reverse=True,
        )
        result[chapter_key] = merged[:top_n_per_chapter]

    return result


def classify_per_chapter(
    query, chapter_files: list[tuple[str, str]], top_n_per_chapter: int = 3
) -> dict[str, list[dict]]:
    """章ごとに独立してスコアリングし、{chapter_key: [results...]} の辞書を返す。

    chapter_files の順序が表示順になる（predict_chapters の信頼度順を想定）。
    各章内では effective_score（score × own_match_ratio）で降順ソートする。
    """
    synonyms = _load_synonyms()
    if isinstance(query, dict):
        query_weights = build_query_weights(query)
    else:
        query_weights = {t: 1.0 for t in _tokenize(str(query))}
    query_weights = _expand_weights_with_synonyms(query_weights, synonyms)
    query_weights = _filter_context_dependent_tokens(query_weights)
    query_tokens = set(query_weights.keys())

    result: dict[str, list[dict]] = {}
    for chapter_key, chapter_file in chapter_files:
        scored = []
        entries = load_classifiable_entries(chapter_file)
        for entry in entries:
            own_desc = entry["description"]
            ancestor_desc, _, _ = entry["full_description"].rpartition(" > ") if own_desc else (entry["full_description"], "", "")

            own_tokens = _tokenize(own_desc)
            ancestor_tokens = _tokenize(ancestor_desc) - own_tokens

            own_meaningful_tokens = {t for t in own_tokens if len(t) > 2 and t not in STOPWORDS}
            ancestor_meaningful_tokens = {t for t in ancestor_tokens if len(t) > 2 and t not in STOPWORDS}
            own_matched = {t for t in (query_tokens & own_tokens) if len(t) > 2 and t not in STOPWORDS}
            ancestor_matched = {t for t in (query_tokens & ancestor_tokens) if len(t) > 2 and t not in STOPWORDS}

            own_matched_weight = sum(query_weights[t] for t in own_matched)
            ancestor_matched_weight = sum(query_weights[t] for t in ancestor_matched)

            score = own_matched_weight * OWN_DESC_WEIGHT + ancestor_matched_weight * ANCESTOR_DESC_WEIGHT
            if score == 0:
                continue

            own_match_ratio = len(own_matched) / len(own_meaningful_tokens) if own_meaningful_tokens else 0.0
            max_field_weight = max(FIELD_WEIGHTS.values())
            total_possible = (len(own_meaningful_tokens) * OWN_DESC_WEIGHT
                               + len(ancestor_meaningful_tokens) * ANCESTOR_DESC_WEIGHT) * max_field_weight
            match_rate = min(score / total_possible, 1.0) if total_possible else 0.0
            # Ch49（印刷物）は末端コードの説明文がページ数等の物理情報のみで、
            # 商品カテゴリは祖先テキスト("Printed books, brochures...")に集中する。
            # 他の章と同じ own_match_ratio ペナルティを適用すると正しいコードが
            # 上位に来なくなるため、祖先マッチのみでも評価できるよう下限を緩和する。
            eff_floor = 0.20 if chapter_key == "49" else 0.05
            effective_score = score * max(own_match_ratio, eff_floor)

            scored.append({
                **entry,
                "chapter_key": chapter_key,
                "score": score,
                "effective_score": effective_score,
                "match_rate": match_rate,
                "own_match_ratio": own_match_ratio,
                "matched_keywords": sorted(own_matched | ancestor_matched),
            })

        scored.sort(
            key=lambda e: (e["effective_score"], e["match_rate"], e["own_match_ratio"], e["indent"]),
            reverse=True,
        )
        result[chapter_key] = scored[:top_n_per_chapter]

    return result
