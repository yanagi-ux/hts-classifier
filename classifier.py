"""テキスト+画像由来キーワードを用いたルールベースHTSコード照合。"""
import json
import re
from pathlib import Path

from hts_db import load_classifiable_entries

SYNONYMS_PATH = Path(__file__).parent / "synonyms.json"
HTS_OVERRIDES_PATH = Path(__file__).parent / "hts_overrides.json"


def _load_overrides() -> dict:
    if not HTS_OVERRIDES_PATH.exists():
        return {}
    with open(HTS_OVERRIDES_PATH, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


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
    def _hint_text(q: dict) -> str:
        kws = q.get("keywords", [])
        kw_str = " ".join(kws) if isinstance(kws, list) else str(kws)
        return f"{q.get('category_hint', '')} {kw_str}"
    combined_hint = " ".join(
        _hint_text(q) for q in queries if isinstance(q, dict)
    ).lower()
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
    if len(word) > 4 and word.endswith("es") and not word.endswith("ses"):
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
