"""Claude multimodal APIを使って商品画像から関税分類向けの特徴を抽出する。"""
import base64
import io
import json
import mimetypes
import re
import time
import random
import logging

import anthropic
from PIL import Image

from config import get_api_key, CLAUDE_MODEL, CLAUDE_MODEL_FAST, MOCK_MODE, HYBRID_MODE
from category_lookup import lookup_chapters, get_extra_keywords
import analysis_cache

_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
_logger = logging.getLogger(__name__)

# 画像リサイズ設定（API送信前に長辺をこのピクセル数以内に縮小）
_IMAGE_MAX_SIDE = 800


def _resize_image(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """長辺が _IMAGE_MAX_SIDE を超える場合にリサイズしてトークンコストを削減する。

    戻り値: (リサイズ後のbytes, 実際のmime_type)
    PNG は JPEG に変換して圧縮率を上げる（透過は白背景に合成）。
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        orig_w, orig_h = img.size
        if max(orig_w, orig_h) > _IMAGE_MAX_SIDE:
            img.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE), Image.LANCZOS)
        # JPEG変換（RGBAはRGBへ）
        if img.mode in ("RGBA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        # PILが扱えない形式はそのまま返す
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        return image_bytes, mime


def _mock_analysis(text_context: str) -> dict:
    """モックモード用のダミー解析結果。APIを呼ばず、補足テキストから
    駿河屋カテゴリの英語キーワードを引いて解析結果らしく組み立てる。
    テキストが無い場合でも汎用キーワードで結果が出るようにする。
    """
    kws = get_extra_keywords(text_context) if text_context else []
    category = (text_context.strip().split() or ["sample product"])[0] if text_context else "sample product"
    return {
        "material": "",
        "function": "",
        "category_hint": category,
        "keywords": kws or ["article", "product"],
        "_mock": True,
    }


def _is_weak_analysis(analysis: dict) -> bool:
    """解析結果が弱い（信頼できない）ときTrue。Sonnet再判定の判断に使う。"""
    if analysis.get("_translation_fallback"):
        return True
    if analysis.get("_parse_failed"):
        return True
    cat = (analysis.get("category_hint") or "").strip()
    kws = [k for k in analysis.get("keywords", []) if k and str(k).strip()]
    return not cat and not kws


# 平面の印刷キャラ物（書籍・カード・ポスター・アクスタ等）と取り違えやすい区分。
# Haikuがこれらと判定した場合はSonnetで再確認する（誤読を捕捉）。
_AMBIGUOUS_CATEGORY_TOKENS = ("figure", "doll", "statuette", "figurine")


def _should_escalate(analysis: dict, hints=None) -> bool:
    """Haikuの結果をSonnetで再判定すべきならTrue（ハイブリッドの昇格条件）。"""
    if _is_weak_analysis(analysis):
        return True
    if hints is not None and not hints:
        return True
    # Haikuが自己申告した確信度が high でなければ昇格
    if (analysis.get("_confidence") or "").lower() in ("low", "medium"):
        return True
    # 混同しやすい区分（フィギュア等）は積極的に再確認
    text = (
        (analysis.get("category_hint") or "") + " "
        + " ".join(str(k) for k in analysis.get("keywords", []))
    ).lower()
    return any(t in text for t in _AMBIGUOUS_CATEGORY_TOKENS)


# 指数バックオフの設定
_BACKOFF_MAX_RETRIES = 6       # 最大リトライ回数
_BACKOFF_BASE_SEC    = 1.0     # 初回待機秒数
_BACKOFF_MAX_SEC     = 64.0    # 最大待機秒数
_BACKOFF_JITTER      = 0.3     # ±30% のランダムジッター


def _api_call_with_backoff(fn, *args, **kwargs):
    """Anthropic API呼び出しに指数バックオフ付きリトライを適用する。

    対象エラー:
      - RateLimitError (429): レート超過
      - APIStatusError 529:   過負荷
      - APIConnectionError:   一時的な接続エラー
    """
    for attempt in range(_BACKOFF_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except anthropic.RateLimitError as e:
            # Retry-After ヘッダーがあれば優先使用
            retry_after = getattr(e, "response", None)
            retry_after = (
                float(retry_after.headers.get("retry-after", 0))
                if retry_after else 0
            )
            wait = retry_after or min(_BACKOFF_BASE_SEC * (2 ** attempt), _BACKOFF_MAX_SEC)
            wait *= 1 + random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
            if attempt >= _BACKOFF_MAX_RETRIES:
                raise
            _logger.warning("RateLimitError: %.1f秒後にリトライ (attempt %d/%d)",
                            wait, attempt + 1, _BACKOFF_MAX_RETRIES)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code != 529 or attempt >= _BACKOFF_MAX_RETRIES:
                raise
            wait = min(_BACKOFF_BASE_SEC * (2 ** attempt), _BACKOFF_MAX_SEC)
            wait *= 1 + random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
            _logger.warning("APIStatusError 529 (過負荷): %.1f秒後にリトライ (attempt %d/%d)",
                            wait, attempt + 1, _BACKOFF_MAX_RETRIES)
            time.sleep(wait)
        except anthropic.APIConnectionError:
            if attempt >= _BACKOFF_MAX_RETRIES:
                raise
            wait = min(_BACKOFF_BASE_SEC * (2 ** attempt), _BACKOFF_MAX_SEC)
            wait *= 1 + random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
            _logger.warning("APIConnectionError: %.1f秒後にリトライ (attempt %d/%d)",
                            wait, attempt + 1, _BACKOFF_MAX_RETRIES)
            time.sleep(wait)

SYSTEM_PROMPT = (
    "あなたは貿易・通関の専門家です。アップロードされた商品画像と、付随するテキスト情報"
    "(品名・仕様・材質など)を見て、米国HTS(関税分類)に役立つ特徴を抽出してください。\n\n"
    "重要な原則1: 関税分類は「その商品が物理的に何であるか」だけで決まります。"
    "商品が描いている作品・キャラクター・原作(アニメ、ビデオゲーム、映画など)が何であるかは"
    "関税分類とは無関係なので、material/function/category_hint/keywordsには絶対に含めないでください。\n"
    "例: ビデオゲームのキャラクターを基にしたアクションフィギュアは、物理的には「フィギュア(玩具)」であり"
    "「ビデオゲーム機」ではありません。\n\n"
    "重要な原則2: materialには「主素材(dominant material)」のみを記載してください。"
    "副素材・コーティング・印刷素材など付随的な素材は含めないでください。\n"
    "例: 金属製ピンバッジに紙のインサートが入っている場合、materialは'metal'のみ。\n"
    "例: プラスチックが主体の容器に金属の蓋が付いている場合、materialは'plastic'のみ。\n\n"
    "重要な原則3: 記録媒体(CD・DVD・Blu-ray等)と再生機器(プレーヤー・レコーダー)を明確に区別してください。\n"
    "例: 音楽CDはcategory_hintを'recorded compact disc'または'music cd'とし、"
    "'compact disc player'や'CD player'は絶対に使わないでください。\n"
    "例: DVDソフトはcategory_hintを'recorded DVD'とし、'DVD player'は使わないでください。\n\n"
    "重要な原則4: 印刷物・書籍・同人誌・漫画・雑誌は category_hint を正確に区別してください。\n"
    "例: 同人誌・ファンブック・コミック・漫画 → category_hint='printed book' または 'comic book' または 'self-published book'。"
    "keywordsには 'printed book', 'booklet', 'illustrated book', 'publication' 等を使用してください。\n"
    "例: パンフレット・チラシ・リーフレット → category_hint='pamphlet' または 'leaflet'。\n"
    "例: 写真集・アートブック → category_hint='photobook' または 'art book'。\n\n"
    "重要な原則5: オフィス用品・文具類は category_hint を正確に区別してください。\n"
    "例: ホッチキス針・ステープル → category_hint='staples' または 'staples in strips', material='metal'。\n"
    "例: ホッチキス本体(ステープラー) → category_hint='stapler', material='metal' または 'plastic'。\n"
    "例: クリップ → category_hint='paper clip', material='metal'。\n\n"
    "重要な原則6: カッティングマット・定規・クラフト用品は category_hint を正確に区別してください。\n"
    "例: カッティングマット → category_hint='cutting mat', material='plastic' または 'rubber'。\n"
    "  カッティングマットは受動的な作業面(プラスチックシート)であり、機械・刃・動力部を持ちません。\n"
    "  'cutting machine'/'cutting board'/'board' 単体を category_hint や keywords に絶対に使わないでください。\n"
    "  keywordsには 'cutting mat', 'self-healing mat', 'craft mat', 'plastic mat' 等を使用してください。\n"
    "例: 定規・スケール → category_hint='ruler' または 'scale ruler', material='plastic' または 'metal'。\n"
    "例: カッター・スクレーパー → category_hint='cutter' または 'utility knife', material='metal'/'plastic'。\n\n"
    "【最重要】すべてのフィールド値は必ず英語で出力してください。"
    "日本語・中国語・その他の非英語文字を一切含めないでください。"
    "material/function/category_hint/keywords はすべて英単語のみで記述すること。\n"
    "出力は必ず次のJSON形式のみとし、説明文や前置きは含めないでください:\n"
    '{"material": "primary/dominant material only in English", '
    '"function": "physical function/use in English (not the fictional theme)", '
    '"category_hint": "the physical product category in English (e.g. pin badge, action figure, trading card) — '
    'never the franchise/game/anime title or genre", '
    '"keywords": ["English keywords describing only the physical product type, never the fictional source material"]}'
)


def analyze_image(image_bytes: bytes, filename: str, text_context: str = "") -> dict:
    mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    client = anthropic.Anthropic(api_key=get_api_key())

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": b64_image,
            },
        },
        {
            "type": "text",
            "text": f"商品の補足情報: {text_context}" if text_context else "補足情報なし",
        },
    ]

    response = _api_call_with_backoff(
        client.messages.create,
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )

    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    analysis = _parse_json_response(raw_text)

    # 非英語が含まれる場合は翻訳。最大3回リトライ。
    for _ in range(3):
        if not _contains_non_english(analysis):
            break
        analysis = _translate_to_english(client, analysis)

    # 翻訳リトライ後も非英語が残っている場合は surugaya_lookup で英語キーワードを補完し、
    # 日本語フィールドを空文字に置き換えてマッチング汚染を防ぐ。
    if _contains_non_english(analysis):
        combined_text = " ".join([
            analysis.get("material", ""),
            analysis.get("function", ""),
            analysis.get("category_hint", ""),
            " ".join(analysis.get("keywords", [])),
            text_context,
        ])
        fallback_keywords = get_extra_keywords(combined_text)
        analysis = {
            "material": analysis.get("material", "") if not _NON_ASCII_RE.search(analysis.get("material", "")) else "",
            "function": analysis.get("function", "") if not _NON_ASCII_RE.search(analysis.get("function", "")) else "",
            "category_hint": analysis.get("category_hint", "") if not _NON_ASCII_RE.search(analysis.get("category_hint", "")) else "",
            "keywords": [k for k in analysis.get("keywords", []) if not _NON_ASCII_RE.search(k)] + fallback_keywords,
            "_translation_fallback": True,
        }

    return _normalize_terms(analysis)


# 翻訳後に誤った用語をHTS適切な用語に正規化するマッピング
# 例: "cutting board" はまな板/カッティングマット両義で曖昧なため "cutting mat" に統一
_TERM_NORMALIZE: list[tuple[str, str]] = [
    ("cutting board", "cutting mat"),
    ("chopping board", "cutting mat"),
    ("chopping mat", "cutting mat"),
    ("mat board", "cutting mat"),
]


def _normalize_terms(analysis: dict) -> dict:
    """翻訳後の英語テキストに含まれる曖昧な用語を正規化する。"""
    def _fix(text: str) -> str:
        lower = text.lower()
        for wrong, correct in _TERM_NORMALIZE:
            if wrong in lower:
                text = re.sub(re.escape(wrong), correct, text, flags=re.IGNORECASE)
        return text

    return {
        "material": _fix(analysis.get("material", "")),
        "function": _fix(analysis.get("function", "")),
        "category_hint": _fix(analysis.get("category_hint", "")),
        "keywords": [_fix(kw) if isinstance(kw, str) else kw for kw in analysis.get("keywords", [])],
        **{k: v for k, v in analysis.items() if k not in ("material", "function", "category_hint", "keywords")},
    }


def _contains_non_english(analysis: dict) -> bool:
    fields = [analysis.get("material", ""), analysis.get("function", ""), analysis.get("category_hint", "")]
    fields += analysis.get("keywords", [])
    return any(_NON_ASCII_RE.search(v) for v in fields if isinstance(v, str))


def _translate_to_english(client: "anthropic.Anthropic", analysis: dict) -> dict:
    """material/function/category_hint/keywordsに日本語など非英語が混じっている場合、
    フィールドごとに個別翻訳してHTSキーワード照合に使える英語テキストへ変換する。
    JSON丸ごとのパースに依存しないため失敗しにくい。
    """
    def _translate_field(text: str) -> str:
        if not text or not _NON_ASCII_RE.search(text):
            return text
        resp = _api_call_with_backoff(
            client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=128,
            system=(
                "Translate the following text into English for US HTS customs classification. "
                "Output only the English translation — no explanations, no quotes, no punctuation changes."
            ),
            messages=[{"role": "user", "content": text}],
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()

    keywords_raw = analysis.get("keywords", [])
    translated_keywords = [
        _translate_field(kw) if isinstance(kw, str) else kw
        for kw in keywords_raw
    ]

    return {
        "material": _translate_field(analysis.get("material", "")),
        "function": _translate_field(analysis.get("function", "")),
        "category_hint": _translate_field(analysis.get("category_hint", "")),
        "keywords": translated_keywords,
    }


def _parse_json_response(raw_text: str) -> dict:
    """Claudeの応答からJSONを取り出す。```json ... ``` のようなコードブロックで
    返ってくる場合や前後に説明文が付く場合に備えて、最初の{から最後の}までを抜き出す。
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # JSON完全パース失敗: _parse_failed フラグで _should_escalate に伝える
    return {
        "material": "",
        "function": "",
        "category_hint": "",
        "keywords": [],
        "_raw_response": raw_text,
        "_parse_failed": True,
    }


def analyze_image_ensemble(
    image_bytes: bytes,
    filename: str,
    text_context: str = "",
    n: int = 2,
) -> list[dict]:
    """画像解析を n 回独立して実行し、すべて英語に変換した analysis dict のリストを返す。

    同一画像（SHA256一致）はキャッシュから返却してAPIコールをスキップする。
    各 analysis は analyze_image() と同じ構造 {"material", "function", "category_hint", "keywords"}。
    """
    # まず過去の実API解析結果（L1キャッシュ）を優先する。
    # モードに関係なく、同一画像なら本物の解析結果を返す（課金ゼロ）。
    cached = analysis_cache.get_cached_analysis(image_bytes)
    if cached:
        return [cached]

    # モックモード: 未知の画像はダミー解析結果（課金ゼロ・精度は参考）
    if MOCK_MODE:
        return [_mock_analysis(text_context)]

    # APIに送信する前に長辺800px以内にリサイズしてトークンコストを削減
    resized_bytes, mime_type = _resize_image(image_bytes, filename)
    b64_image = base64.b64encode(resized_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=get_api_key())

    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": b64_image},
    }
    text_block = {
        "type": "text",
        "text": f"商品の補足情報: {text_context}" if text_context else "補足情報なし",
    }

    _cached_system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    def _one(model: str) -> dict:
        """指定モデルで1回解析し、翻訳・正規化済みの analysis を返す。"""
        response = _api_call_with_backoff(
            client.messages.create,
            model=model,
            max_tokens=512,
            system=_cached_system,
            messages=[{"role": "user", "content": [image_block, text_block]}],
        )
        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        analysis = _parse_json_response(raw_text)
        for _ in range(3):
            if not _contains_non_english(analysis):
                break
            analysis = _translate_to_english(client, analysis)
        if _contains_non_english(analysis):
            combined_text = " ".join([
                analysis.get("material", ""), analysis.get("function", ""),
                analysis.get("category_hint", ""),
                " ".join(analysis.get("keywords", [])), text_context,
            ])
            fallback_keywords = get_extra_keywords(combined_text)
            analysis = {
                "material": analysis.get("material", "") if not _NON_ASCII_RE.search(analysis.get("material", "")) else "",
                "function": analysis.get("function", "") if not _NON_ASCII_RE.search(analysis.get("function", "")) else "",
                "category_hint": analysis.get("category_hint", "") if not _NON_ASCII_RE.search(analysis.get("category_hint", "")) else "",
                "keywords": [k for k in analysis.get("keywords", []) if not _NON_ASCII_RE.search(k)] + fallback_keywords,
                "_translation_fallback": True,
            }
        return _normalize_terms(analysis)

    results: list[dict] = []
    for _ in range(n):
        # 一次解析はHaiku、弱い／混同しやすい区分ならSonnetで再判定（ハイブリッド）
        a = _one(CLAUDE_MODEL_FAST if HYBRID_MODE else CLAUDE_MODEL)
        if HYBRID_MODE and _should_escalate(a):
            a = _one(CLAUDE_MODEL)
        results.append(a)

    # L1キャッシュに保存（翻訳・正規化済みの状態）
    if results:
        analysis_cache.save_analysis(image_bytes, results[0])

    return results


def analyze_and_predict(
    image_bytes: bytes,
    filename: str,
    text_context: str,
    chapters: dict,
) -> tuple[dict, list[str]]:
    """1回のAPIで「画像解析結果」と「対象章の推定」を同時に取得する。

    従来の analyze_image_ensemble + predict_chapters（2回API）を1回に統合し、
    画像送信とリクエストを半減してコスト・処理時間を削減する。
    戻り値: (analysis dict, chapter_hints list)
    chapters は安定キャッシュのため SUPPORTED_CHAPTERS 全体を渡すこと
    （除外フィルタは呼び出し側で後段適用する）。
    """
    def _suruga_chapters(analysis: dict) -> list[str]:
        text = " ".join([
            text_context,
            analysis.get("category_hint", ""),
            analysis.get("function", ""),
            " ".join(analysis.get("keywords", [])),
        ])
        return [c for c, _ in lookup_chapters(text) if c in chapters][:3]

    # L1キャッシュ: 解析が同一画像でヒットすれば API を呼ばない
    cached = analysis_cache.get_cached_analysis(image_bytes)
    if cached:
        chs = analysis_cache.get_cached_chapters(image_bytes) or _suruga_chapters(cached)
        return cached, chs[:3]

    # モックモード: API を呼ばずダミー解析＋章推定
    if MOCK_MODE:
        analysis = _mock_analysis(text_context)
        chs = _suruga_chapters(analysis)
        if not chs:
            chs = [c for c in ["95", "49", "39", "42", "61"] if c in chapters][:3]
        return analysis, chs

    # ── 1回のAPI呼び出しで解析＋章推定 ──────────────────────────
    resized_bytes, mime_type = _resize_image(image_bytes, filename)
    b64_image = base64.b64encode(resized_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=get_api_key())

    chapters_list = "\n".join(f"  {k}: {v['label']}" for k, v in chapters.items())
    combined_system = [{
        "type": "text",
        "text": (
            SYSTEM_PROMPT
            + "\n\n【追加指示】上記の判断に加えて、この商品が該当する米国HTSの章番号を"
              "最大3つ選び \"chapter_hints\" に含めてください。\n"
              f"章の選択肢:\n{chapters_list}\n"
            + "また、material/function/category_hint/keywords それぞれの"
              "日本語訳を material_ja/function_ja/category_hint_ja/keywords_ja に入れてください"
              "（英語フィールドは分類用なので必ず英語のまま残すこと）。\n"
            + "さらに、画像から商品の物理的な種類をどれだけ確信できるかを "
              "\"confidence\" に high/medium/low で入れてください。"
              "平面の印刷物（書籍・カード・ポスター）と立体物（フィギュア等）の区別が"
              "曖昧な場合は必ず low か medium にしてください。\n"
            + "最終的な出力JSONは必ず次の形式にしてください（説明・前置き不要）:\n"
            + '{"material": "...", "function": "...", "category_hint": "...", '
              '"keywords": ["..."], "chapter_hints": ["95"], '
              '"material_ja": "...", "function_ja": "...", "category_hint_ja": "...", '
              '"keywords_ja": ["..."], "confidence": "high"}'
        ),
        "cache_control": {"type": "ephemeral"},
    }]

    image_block = {"type": "image", "source": {
        "type": "base64", "media_type": mime_type, "data": b64_image}}
    text_block = {"type": "text",
                  "text": f"商品の補足情報: {text_context}" if text_context else "補足情報なし"}

    def _run(model: str) -> tuple[dict, list[str]]:
        """指定モデルで1回呼び出し、解析4項目＋章ヒントを処理して返す。"""
        response = _api_call_with_backoff(
            client.messages.create,
            model=model,
            max_tokens=1024,
            system=combined_system,
            messages=[{"role": "user", "content": [image_block, text_block]}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        raw = "".join(b.text for b in response.content
                      if getattr(b, "type", None) == "text").strip()
        parsed = _parse_json_response(raw)
        hints_ = [str(h) for h in parsed.get("chapter_hints", []) if str(h) in chapters]
        # 表示用の日本語フィールド（分類には使わない・同じ呼び出しで取得＝追加課金なし）
        ja = {
            "material_ja": parsed.get("material_ja", ""),
            "function_ja": parsed.get("function_ja", ""),
            "category_hint_ja": parsed.get("category_hint_ja", ""),
            "keywords_ja": parsed.get("keywords_ja", []),
        }
        a = {
            "material": parsed.get("material", ""),
            "function": parsed.get("function", ""),
            "category_hint": parsed.get("category_hint", ""),
            "keywords": parsed.get("keywords", []),
        }
        for _ in range(3):
            if not _contains_non_english(a):
                break
            a = _translate_to_english(client, a)
        if _contains_non_english(a):
            combined_text = " ".join([
                a.get("material", ""), a.get("function", ""),
                a.get("category_hint", ""),
                " ".join(a.get("keywords", [])), text_context,
            ])
            fallback_keywords = get_extra_keywords(combined_text)
            a = {
                "material": a.get("material", "") if not _NON_ASCII_RE.search(a.get("material", "")) else "",
                "function": a.get("function", "") if not _NON_ASCII_RE.search(a.get("function", "")) else "",
                "category_hint": a.get("category_hint", "") if not _NON_ASCII_RE.search(a.get("category_hint", "")) else "",
                "keywords": [k for k in a.get("keywords", []) if not _NON_ASCII_RE.search(k)] + fallback_keywords,
                "_translation_fallback": True,
            }
        conf = {"_confidence": str(parsed.get("confidence", "")).strip()}
        return {**_normalize_terms(a), **ja, **conf}, hints_

    # ① 一次解析は低単価のHaikuで実行
    analysis, hints = _run(CLAUDE_MODEL_FAST if HYBRID_MODE else CLAUDE_MODEL)
    # ② 弱い／確信度が非high／混同しやすい区分（フィギュア等）はSonnetで再判定
    if HYBRID_MODE and _should_escalate(analysis, hints):
        analysis, hints = _run(CLAUDE_MODEL)

    # 駿河屋カテゴリDBで章を補完
    for c in _suruga_chapters(analysis):
        if c not in hints:
            hints.append(c)
    hints = hints[:3]

    # キャッシュ保存（L1解析 + 章）
    analysis_cache.save_analysis(image_bytes, analysis)
    if hints:
        analysis_cache.save_chapters(image_bytes, hints)

    return analysis, hints


def keywords_to_query_text(analysis: dict) -> str:
    parts = [
        analysis.get("material", ""),
        analysis.get("function", ""),
        analysis.get("category_hint", ""),
        " ".join(analysis.get("keywords", [])),
    ]
    return " ".join(p for p in parts if p)


def predict_chapters(
    text_context: str,
    chapters: dict,
    image_bytes: bytes | None = None,
    filename: str = "",
) -> list[str]:
    """商品情報(テキスト+任意で画像)からHTSの対象章番号を最大3つ推定して返す。

    chapters: {"84": {"label": "第84章: ..."}, ...} の形式。
    戻り値: ["84", "85"] のようなchapter keyのリスト(スコア降順)。
    """
    # モックモード: APIを呼ばずに章を推定
    if MOCK_MODE:
        # ① 過去の実API実績（章キャッシュ）があれば本物の章を返す
        if image_bytes:
            cached_ch = analysis_cache.get_cached_chapters(image_bytes)
            if cached_ch:
                return [c for c in cached_ch if c in chapters][:3]
        # ② 無ければ駿河屋カテゴリDBのテキスト照合で推定
        hints = [ch for ch, _ in lookup_chapters(text_context) if ch in chapters]
        if not hints:
            # ③ それも取れない（画像のみ等）場合はデモが止まらないよう代表章を返す
            _DEMO_DEFAULT = ["95", "49", "39", "42", "61"]
            hints = [c for c in _DEMO_DEFAULT if c in chapters][:3]
        return hints[:3]

    chapters_list = "\n".join(f"  {k}: {v['label']}" for k, v in chapters.items())
    # 固定部分をキャッシュ対象に、章リスト（変動しない）を続けてキャッシュ
    system = [
        {
            "type": "text",
            "text": (
                "あなたは米国HTS(関税分類)の専門家です。商品情報を見て、最も可能性の高い"
                "HTS章番号を最大3つ選んでください。\n\n"
                f"選択肢:\n{chapters_list}\n\n"
                "出力は必ず次のJSONのみ(説明・前置き不要):\n"
                '{"chapter_hints": ["84", "85"]}'
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    client = anthropic.Anthropic(api_key=get_api_key())

    user_content: list[dict] = []
    if image_bytes and filename:
        resized_bytes, mime_type = _resize_image(image_bytes, filename)
        b64_image = base64.b64encode(resized_bytes).decode("utf-8")
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime_type, "data": b64_image},
        })
    user_content.append({
        "type": "text",
        "text": f"商品情報: {text_context}" if text_context else "情報なし",
    })

    response = _api_call_with_backoff(
        client.messages.create,
        model=CLAUDE_MODEL,
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    parsed = _parse_json_response(raw)
    hints = [str(h) for h in parsed.get("chapter_hints", []) if str(h) in chapters]

    # 駿河屋カテゴリDBでテキストから章番号を補完する。
    # Claudeが見落とした章でも日本語カテゴリ名が含まれていれば追加する。
    suruga_matches = lookup_chapters(text_context)
    for ch, _ in suruga_matches:
        if ch in chapters and ch not in hints:
            hints.append(ch)

    result = hints[:3]
    # 章キャッシュに保存（モードdemoで本物の章を課金ゼロ再現するため）
    if image_bytes and result:
        analysis_cache.save_chapters(image_bytes, result)
    return result
