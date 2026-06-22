import csv
import datetime
import io
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import streamlit as st

from classifier import classify_ensemble, classify_per_chapter_ensemble, apply_hts_overrides
from config import get_api_key, SUPPORTED_CHAPTERS
from image_analyzer import analyze_image_ensemble, predict_chapters, analyze_and_predict
from category_lookup import get_extra_keywords
import analysis_cache
from cpsc_hts import check_cpsc

DATA_DIR = Path(__file__).parent / "data"
_USITC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://hts.usitc.gov/",
}

AUTO_KEY = "__auto__"
MAX_BATCH = 10


def _download_chapter(chapter_key: str, data_file: str) -> bool:
    chapter_str = str(int(chapter_key)).zfill(2)
    try:
        ranges_url = f"https://hts.usitc.gov/reststop/ranges?docNumber={chapter_str}"
        req = urllib.request.Request(ranges_url, headers=_USITC_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            rng = json.loads(resp.read().decode("utf-8"))

        export_url = (
            f"https://hts.usitc.gov/reststop/exportList"
            f"?from={rng['Starting_Number']}&to={rng['Ending_Number']}&format=JSON&styles=true"
        )
        req = urllib.request.Request(export_url, headers=_USITC_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        out_path = DATA_DIR / data_file
        out_path.parent.mkdir(exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        st.error(f"ダウンロードに失敗しました: {e}")
        return False


LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
FEEDBACK_LOG = LOGS_DIR / "feedback.csv"

JP_LABELS_PATH = Path(__file__).parent / "jp_labels.json"
with open(JP_LABELS_PATH, encoding="utf-8") as f:
    JP_LABELS = json.load(f)

# ── ページ設定 ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="HTSコード判定支援ツール", layout="wide")
st.markdown("""
<style>
/* 判定結果カードのExpanderタイトル（📦で始まるもの）を大きく・色付きに */
div[data-testid="stExpander"] > details > summary p:first-child {
    font-size: 1.35rem !important;
    font-weight: 700 !important;
    color: #1a6eb5 !important;
}
/* 内側の小Expander（画像解析結果・上位分類）は通常サイズに戻す */
div[data-testid="stExpander"] div[data-testid="stExpander"] > details > summary p {
    font-size: 0.9rem !important;
    font-weight: 400 !important;
    color: inherit !important;
}
</style>
""", unsafe_allow_html=True)
st.title("米国HTSコード判定支援ツール")
st.caption("商品画像を主判断とし、テキスト情報を補助としてHTSコード候補を提示します（最大10件同時判定）。最終判断は担当者が行ってください。")

# モックモード警告（API課金ゼロのテスト用）
from config import MOCK_MODE as _MOCK_MODE, MOCK_EXPLICIT as _MOCK_EXPLICIT
if _MOCK_MODE:
    _reason = "Secrets/環境変数で有効化" if _MOCK_EXPLICIT else "APIキー未設定のため自動でモックに切替"
    st.warning(
        f"🧪 **モックモード稼働中**（{_reason}） — Claude APIを呼び出していません（課金ゼロ）。"
        "判定結果は補足テキストから生成したダミーで、精度は参考になりません。"
        "UI・バッチ処理・Excel出力など動作確認用です。"
    )

# サイドバー: キャッシュ統計
with st.sidebar:
    st.subheader("📊 キャッシュ統計（過去30日）")
    stats = analysis_cache.get_stats()
    st.metric("総リクエスト数", f"{stats['total_requests']:,}")
    col_l1, col_l2 = st.columns(2)
    col_l1.metric("L1ヒット率", f"{stats['hit_rate_l1']:.1%}", help="同一画像キャッシュ（APIスキップ）")
    col_l2.metric("L2ヒット率", f"{stats['hit_rate_l2']:.1%}", help="同じ分析結果キャッシュ（照合スキップ）")
    st.metric("推定削減コスト（API）", f"${stats['saved_usd_30d']:,.2f}")
    st.caption(f"キャッシュ済み画像: {stats['cached_images']:,} 件　分析パターン: {stats['cached_analyses']:,} 種")

# ── セッション初期化 ────────────────────────────────────────────────────────
if "batch_results" not in st.session_state:
    st.session_state["batch_results"] = []
if "form_version" not in st.session_state:
    st.session_state["form_version"] = 0

v = st.session_state["form_version"]

top_cols = st.columns([4, 1])
with top_cols[1]:
    if st.button("🔄 クリア"):
        st.session_state["form_version"] += 1
        st.session_state["batch_results"] = []
        st.rerun()

# ── 章選択 ──────────────────────────────────────────────────────────────────
chapter_options = [AUTO_KEY] + list(SUPPORTED_CHAPTERS.keys())

def _chapter_label(k):
    if k == AUTO_KEY:
        return "🔍 自動判定（章を自動で推定）"
    return SUPPORTED_CHAPTERS[k]["label"]

chapter_key = st.selectbox(
    "対象Chapter",
    options=chapter_options,
    format_func=_chapter_label,
    key=f"chapter_key_{v}",
)

if chapter_key != AUTO_KEY:
    chapter_conf = SUPPORTED_CHAPTERS[chapter_key]
    chapter_data_path = DATA_DIR / chapter_conf["data_file"]
    if not chapter_data_path.exists():
        st.warning(
            f"**{chapter_conf['label']}** のHTSデータがまだダウンロードされていません。"
            "下のボタンでUSITCから取得できます（初回のみ）。"
        )
        if st.button("📥 HTSデータをダウンロード", type="primary"):
            with st.spinner("USITCからデータを取得しています..."):
                ok = _download_chapter(chapter_key, chapter_conf["data_file"])
            if ok:
                st.success("ダウンロード完了。判定を開始できます。")
                time.sleep(1)
                st.rerun()
        st.stop()

# ── 入力フォーム ─────────────────────────────────────────────────────────────
st.subheader("商品情報（テキスト補助・全件共通）")
tcols = st.columns(4)
product_name  = tcols[0].text_input("品名", key=f"product_name_{v}")
material      = tcols[1].text_input("材質", key=f"material_{v}")
function_desc = tcols[2].text_input("用途・機能", key=f"function_desc_{v}")
spec          = tcols[3].text_input("仕様・補足", key=f"spec_{v}")
text_context  = " ".join(p for p in [product_name, material, function_desc, spec] if p)

st.subheader(f"商品画像（最大{MAX_BATCH}件）")
uploaded_images = st.file_uploader(
    "画像をアップロード（複数選択可・最大10件）",
    type=["png", "jpg", "jpeg"],
    accept_multiple_files=True,
    key=f"uploaded_images_{v}",
)

if uploaded_images:
    if len(uploaded_images) > MAX_BATCH:
        st.warning(f"最大{MAX_BATCH}件まで処理できます。先頭{MAX_BATCH}件を使用します。")
        uploaded_images = uploaded_images[:MAX_BATCH]

    # プレビュー（最大5列）
    cols_per_row = min(len(uploaded_images), 5)
    preview_cols = st.columns(cols_per_row)
    for i, img in enumerate(uploaded_images):
        preview_cols[i % cols_per_row].image(img, caption=img.name, width='stretch')

# ── ヘルパー関数 ─────────────────────────────────────────────────────────────
_HINT_EXCLUDE_CHAPTERS: dict[str, list[str]] = {
    "cutting mat": ["84", "95"],
    "ruler": ["84", "95"],
    "staples": ["95"],
    "stapler": ["95"],
}

# 画像ヒントに応じて候補章を強制的に追加する（誤った章のみに誘導されるのを防ぐ）。
# 例: バッジはプラ製→39 / 金属製→83 のどちらにもなり得るため両方を候補に含める。
_HINT_INCLUDE_CHAPTERS: dict[str, list[str]] = {
    "badge": ["39", "83", "71"],
    "pin badge": ["39", "83", "71"],
    "brooch": ["71", "39", "83"],
}


def _build_query(analysis: dict | None, suruga_keywords: list[str]) -> dict:
    if analysis:
        return {
            "product_name": "",
            "material": analysis.get("material", ""),
            "category_hint": analysis.get("category_hint", ""),
            "keywords": analysis.get("keywords", []) + suruga_keywords,
            "function": analysis.get("function", ""),
            "spec": "",
        }
    return {
        "product_name": product_name,
        "material": material,
        "category_hint": "",
        "keywords": suruga_keywords,
        "function": function_desc,
        "spec": spec,
    }


def _classify_one(img_file, text_ctx: str, ch_key: str) -> dict:
    """1件分の画像解析 + HTS照合を実行してresult dictを返す。"""
    image_bytes = img_file.getvalue()
    image_name  = img_file.name
    suruga_kw   = get_extra_keywords(text_ctx)

    if ch_key == AUTO_KEY:
        # 1回のAPIで「解析＋章推定」を同時取得（旧2回API→1回に統合）
        image_analysis, detected_all = analyze_and_predict(
            image_bytes, image_name, text_ctx, SUPPORTED_CHAPTERS
        )
        cache_hit_l1 = bool(image_analysis and image_analysis.get("_cache_hit"))
        query_list = [_build_query(image_analysis, suruga_kw)]

        # 画像ヒントに基づく章の除外フィルタを後段で適用
        img_hint_lower = " ".join(filter(None, [
            (image_analysis or {}).get("category_hint", ""),
            (image_analysis or {}).get("function", ""),
            " ".join((image_analysis or {}).get("keywords", [])),
        ])).lower()
        exclude_chs: set[str] = set()
        for hint_key, excl_list in _HINT_EXCLUDE_CHAPTERS.items():
            if hint_key in img_hint_lower:
                exclude_chs.update(excl_list)

        # ヒントに応じて候補章を強制追加（誤った単一章のみへの誘導を防ぐ）
        include_chs: list[str] = []
        for hint_key, inc_list in _HINT_INCLUDE_CHAPTERS.items():
            if hint_key in img_hint_lower:
                include_chs.extend(inc_list)

        detected = [c for c in detected_all if c not in exclude_chs]
        for c in include_chs:
            if c not in detected and c not in exclude_chs and c in SUPPORTED_CHAPTERS:
                detected.append(c)
        detected = detected[:3]
        if not detected:
            # 除外で全滅した場合は元の推定を採用（安全側）
            detected = detected_all[:3]

        if not detected:
            return {"error": "章を推定できませんでした", "filename": image_name,
                    "image_bytes": image_bytes,
                    "image_analysis": image_analysis, "cache_hit_l1": cache_hit_l1}

        # 未取得章をダウンロード
        for k in detected:
            if not (DATA_DIR / SUPPORTED_CHAPTERS[k]["data_file"]).exists():
                _download_chapter(k, SUPPORTED_CHAPTERS[k]["data_file"])

        chapter_files = [
            (k, SUPPORTED_CHAPTERS[k]["data_file"])
            for k in detected
            if (DATA_DIR / SUPPORTED_CHAPTERS[k]["data_file"]).exists()
        ]

        # L2キャッシュ
        cache_hit_l2 = False
        l2_cached = analysis_cache.get_cached_hts(image_analysis) if image_analysis else None
        if l2_cached:
            results = l2_cached
            cache_hit_l2 = True
        else:
            results = classify_per_chapter_ensemble(
                query_list, chapter_files=chapter_files, top_n_per_chapter=3
            )
            results = apply_hts_overrides(results, query_list)
            if image_analysis:
                analysis_cache.save_hts(image_analysis, results)

        return {
            "filename": image_name,
            "image_bytes": image_bytes,
            "image_analysis": image_analysis,
            "results": results,
            "detected_chapters": detected,
            "is_auto": True,
            "cache_hit_l1": cache_hit_l1,
            "cache_hit_l2": cache_hit_l2,
        }
    else:
        # 手動で章を選択 → 章推定は不要。画像解析のみ（API1回）
        analyses = analyze_image_ensemble(image_bytes, image_name, text_ctx, n=1)
        image_analysis = analyses[0] if analyses else None
        cache_hit_l1 = bool(image_analysis and image_analysis.get("_cache_hit"))
        query_list = [_build_query(image_analysis, suruga_kw)]

        cache_hit_l2 = False
        l2_cached = analysis_cache.get_cached_hts(image_analysis) if image_analysis else None
        if l2_cached:
            results = l2_cached
            cache_hit_l2 = True
        else:
            results = classify_ensemble(
                query_list, top_n=5,
                chapter_file=SUPPORTED_CHAPTERS[ch_key]["data_file"],
            )
            if image_analysis:
                analysis_cache.save_hts(image_analysis, results)

        return {
            "filename": image_name,
            "image_bytes": image_bytes,
            "image_analysis": image_analysis,
            "results": results,
            "detected_chapters": [],
            "is_auto": False,
            "cache_hit_l1": cache_hit_l1,
            "cache_hit_l2": cache_hit_l2,
        }


# ── 判定実行 ─────────────────────────────────────────────────────────────────
if st.button("判定する", type="primary"):
    if not uploaded_images and not text_context:
        st.warning("画像またはテキストを入力してください。")
    elif chapter_key == AUTO_KEY and not get_api_key() and not _MOCK_MODE:
        st.error("自動判定にはANTHROPIC_API_KEYが必要です。")
    else:
        n_items = len(uploaded_images) if uploaded_images else 1
        progress = st.progress(0, text=f"0 / {n_items} 件処理中...")

        batch_results = []

        if uploaded_images:
            completed = 0
            with ThreadPoolExecutor(max_workers=min(n_items, 2)) as executor:
                futures = {
                    executor.submit(_classify_one, img, text_context, chapter_key): img.name
                    for img in uploaded_images
                }
                for future in as_completed(futures):
                    try:
                        batch_results.append(future.result())
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        batch_results.append({"error": str(e), "filename": futures[future]})
                    completed += 1
                    progress.progress(completed / n_items, text=f"{completed} / {n_items} 件処理中...")
            # ファイル名順にソート
            order = {img.name: i for i, img in enumerate(uploaded_images)}
            batch_results.sort(key=lambda r: order.get(r.get("filename", ""), 999))
        else:
            # 画像なし・テキストのみ
            suruga_kw = get_extra_keywords(text_context)
            query_list = [_build_query(None, suruga_kw)]
            results = classify_ensemble(
                query_list, top_n=5,
                chapter_file=SUPPORTED_CHAPTERS.get(chapter_key, list(SUPPORTED_CHAPTERS.values())[0])["data_file"],
            ) if chapter_key != AUTO_KEY else []
            batch_results = [{"filename": "(テキストのみ)", "results": results,
                               "detected_chapters": [], "is_auto": False,
                               "cache_hit_l1": False, "cache_hit_l2": False}]
            progress.progress(1.0, text="完了")

        progress.empty()
        st.session_state["batch_results"] = batch_results

# ── 結果表示 ─────────────────────────────────────────────────────────────────
def _priority_mark(r: dict) -> str:
    ratio = r.get("own_match_ratio", 0.0)
    if ratio >= 0.5:
        return "◎"
    if ratio >= 0.2:
        return "〇"
    return "△"


def _build_excel(batch_results: list[dict]) -> bytes:
    """判定結果一覧をExcelファイルとして返す。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HTS判定結果"

    # ── ヘッダー ──────────────────────────────────────────────
    headers = [
        "ファイル名", "採用HTSコード", "説明（英語）", "説明（日本語）",
        "スコア", "マッチ率", "CPSC", "メモ",
        "候補2位", "候補3位",
    ]
    header_fill = PatternFill("solid", fgColor="1A6EB5")
    header_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    ws.row_dimensions[1].height = 28

    # ── データ行 ──────────────────────────────────────────────
    fv = st.session_state.get("form_version", 0)
    alt_fill = PatternFill("solid", fgColor="EEF4FB")

    for row_idx, item in enumerate(batch_results, 2):
        filename = item.get("filename", "")
        if "error" in item:
            ws.cell(row=row_idx, column=1, value=filename)
            ws.cell(row=row_idx, column=2, value=f"エラー: {item['error']}")
            continue

        if item.get("is_auto") and isinstance(item.get("results"), dict):
            flat = [r for ch_r in item["results"].values() for r in ch_r]
        elif isinstance(item.get("results"), list):
            flat = item["results"]
        else:
            flat = []

        if not flat:
            ws.cell(row=row_idx, column=1, value=filename)
            ws.cell(row=row_idx, column=2, value="候補なし")
            continue

        safe_name = filename.replace(".", "_").replace(" ", "_")
        chosen_key = f"chosen_{safe_name}_{fv}"
        note_key   = f"note_{safe_name}_{fv}"
        chosen_code = st.session_state.get(chosen_key) or flat[0]["hts_code"]
        note        = st.session_state.get(note_key, "")

        chosen_entry = next((r for r in flat if r["hts_code"] == chosen_code), flat[0])
        lbl_raw = chosen_entry.get("description") or chosen_entry["full_description"].rsplit(" > ", 1)[-1]
        lbl_jp  = JP_LABELS.get(lbl_raw.rstrip(":").strip(), "")
        score   = chosen_entry.get("effective_score", chosen_entry.get("score", 0))
        ratio   = chosen_entry.get("own_match_ratio", 0)
        cpsc    = "⚠️ CPSC" if check_cpsc(chosen_code)["is_cpsc"] else ""

        others = [r for r in flat if r["hts_code"] != chosen_code][:2]

        row_data = [
            filename, chosen_code, lbl_raw.rstrip(":").strip(), lbl_jp,
            round(score, 3), round(ratio, 3), cpsc, note,
            others[0]["hts_code"] if len(others) > 0 else "",
            others[1]["hts_code"] if len(others) > 1 else "",
        ]
        fill = alt_fill if row_idx % 2 == 0 else None
        for col, val in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=(col in (3, 4)))
            if fill:
                cell.fill = fill

    # ── 列幅 ──────────────────────────────────────────────────
    col_widths = [28, 18, 45, 35, 8, 8, 8, 20, 16, 16]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _render_result_card(r: dict, index: int):
    with st.container(border=True):
        own_label = r["description"] or r["full_description"].rsplit(" > ", 1)[-1]
        ancestor_breadcrumb = (
            r["full_description"].rsplit(" > ", 1)[0]
            if r["description"] and " > " in r["full_description"] else ""
        )
        jp_label = JP_LABELS.get(own_label.rstrip(":").strip())
        mark = _priority_mark(r)

        cpsc = check_cpsc(r["hts_code"])
        cpsc_badge = f" 🚨 CPSC対象（{cpsc['category']}）" if cpsc["is_cpsc"] else ""

        if jp_label:
            st.markdown(f"**{mark} {index}. `{r['hts_code']}` — {jp_label}**{cpsc_badge}")
            st.caption(f"原文: {own_label}")
        else:
            st.markdown(f"**{mark} {index}. `{r['hts_code']}` — {own_label}**{cpsc_badge}")

        st.caption(f"一致率: {r['match_rate']:.0%}　自身一致率: {r['own_match_ratio']:.0%}　優先度: {mark}")
        if ancestor_breadcrumb:
            with st.expander("上位分類"):
                st.caption(ancestor_breadcrumb)

        rate_cols = st.columns(3)
        for col, (label, value) in zip(rate_cols, [
            ("一般税率", r["general_rate"]),
            ("特別税率", r["special_rate"]),
            ("Column 2", r["other_rate"]),
        ]):
            col.markdown(
                f"<div style='font-size:0.8rem;color:gray;'>{label}</div>"
                f"<div style='font-size:0.95rem;'>{value or '-'}</div>",
                unsafe_allow_html=True,
            )
        st.caption("マッチキーワード: " + ", ".join(r["matched_keywords"]))


batch_results = st.session_state.get("batch_results", [])
if batch_results:
    st.divider()
    _hdr_col, _dl_col = st.columns([3, 1])
    _hdr_col.subheader(f"判定結果（{len(batch_results)}件）")
    with _dl_col:
        _xlsx = _build_excel(batch_results)
        _ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        st.download_button(
            "📥 Excelダウンロード",
            data=_xlsx,
            file_name=f"hts_results_{_ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    for item in batch_results:
        filename = item.get("filename", "")
        with st.expander(f"📦 {filename}", expanded=True):
            # エラー
            if "error" in item:
                st.error(item["error"])
                continue

            results  = item.get("results")
            is_auto  = item.get("is_auto", False)
            detected = item.get("detected_chapters", [])

            # flat を先に組み立て
            if is_auto and isinstance(results, dict):
                flat = [r for ch_r in results.values() for r in ch_r]
            elif isinstance(results, list):
                flat = results
            else:
                flat = []

            # ── 採用HTSコード（上部・画像左横） ──────────────────────────
            if flat:
                safe_name = filename.replace(".", "_").replace(" ", "_")
                best_idx  = max(
                    range(len(flat)),
                    key=lambda i: flat[i].get("effective_score", flat[i].get("score", 0)),
                )

                def _opt_label(code: str, _flat=flat) -> str:
                    if code == "該当なし/その他":
                        return code
                    e = next((r for r in _flat if r["hts_code"] == code), None)
                    if not e:
                        return code
                    lbl = e["description"] or e["full_description"].rsplit(" > ", 1)[-1]
                    label = f"{code}　{JP_LABELS.get(lbl.rstrip(':').strip()) or lbl}"
                    if check_cpsc(code)["is_cpsc"]:
                        label = f"⚠️ {label}"
                    return label

                img_col, adopt_col = st.columns([1, 4])
                with img_col:
                    if item.get("image_bytes"):
                        st.image(item["image_bytes"], width='stretch')
                with adopt_col:
                    input_cols = st.columns([6, 2, 1])
                    chosen = input_cols[0].selectbox(
                        "採用HTSコード",
                        options=[r["hts_code"] for r in flat] + ["該当なし/その他"],
                        index=best_idx,
                        format_func=_opt_label,
                        key=f"chosen_{safe_name}_{st.session_state['form_version']}",
                    )
                    note = input_cols[1].text_input(
                        "メモ", key=f"note_{safe_name}_{st.session_state['form_version']}"
                    )
                    if input_cols[2].button(
                        "保存", key=f"save_{safe_name}_{st.session_state['form_version']}"
                    ):
                        file_exists = FEEDBACK_LOG.exists()
                        with open(FEEDBACK_LOG, "a", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            if not file_exists:
                                writer.writerow(["timestamp", "filename", "chosen_code", "note"])
                            writer.writerow([
                                datetime.datetime.now().isoformat(), filename, chosen, note
                            ])
                        st.success("保存しました。")

                _chosen_entry = next((r for r in flat if r["hts_code"] == chosen), None)
                _lbl_raw = (_chosen_entry["description"] or _chosen_entry["full_description"].rsplit(" > ", 1)[-1]) if _chosen_entry else ""
                _jp = JP_LABELS.get(_lbl_raw.rstrip(":").strip()) if _lbl_raw else ""
                if _jp:
                    st.caption(f"📋 {_jp}")

                chosen_cpsc = check_cpsc(chosen)
                if chosen_cpsc["is_cpsc"]:
                    st.warning(f"⚠️ 採用コード `{chosen}` はCPSC電子証明書（eFiling）の提出が必要な可能性があります。（カテゴリ: {chosen_cpsc['category']}）")

                if item.get("image_analysis"):
                    with st.expander("画像解析結果（参考）"):
                        st.json(item["image_analysis"])

                st.divider()

            # ── キャッシュ・Chapter情報 ────────────────────────────────────
            if item.get("cache_hit_l1"):
                st.info("⚡ L1キャッシュヒット（同一画像 — APIスキップ）")
            elif item.get("cache_hit_l2"):
                st.info("⚡ L2キャッシュヒット（同じ分析結果 — 照合スキップ）")

            if detected:
                st.caption("推定Chapter: " + "、".join(SUPPORTED_CHAPTERS[k]["label"] for k in detected))

            # ── HTS候補カード（下部） ──────────────────────────────────────
            if not flat:
                st.info("候補が見つかりませんでした。")
            elif is_auto and isinstance(results, dict):
                for ch_key_r in detected:
                    ch_results = results.get(ch_key_r, [])
                    if ch_results:
                        st.markdown(f"**{SUPPORTED_CHAPTERS[ch_key_r]['label']}**")
                        for i, r in enumerate(ch_results, 1):
                            _render_result_card(r, i)
            else:
                for i, r in enumerate(flat, 1):
                    _render_result_card(r, i)

