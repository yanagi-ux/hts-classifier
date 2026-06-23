import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def get_api_key() -> str:
    """Streamlit Secrets → 環境変数の順で取得する。"""
    try:
        import streamlit as st
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        return ANTHROPIC_API_KEY


def _explicit_mock_flag() -> bool:
    """環境変数 MOCK_MODE / Streamlit Secrets の MOCK_MODE を読む。"""
    val = os.environ.get("MOCK_MODE", "")
    if not val:
        try:
            import streamlit as st
            val = str(st.secrets.get("MOCK_MODE", ""))
        except Exception:
            val = ""
    return val.strip().lower() in ("1", "true", "yes", "on")


# モックモード判定:
#   ① MOCK_MODE が明示的に有効、または
#   ② APIキーが未設定（フェイルセーフ。誤デプロイでも401を出さずモックで動く）
# のいずれかで有効になる。判定結果はダミーで課金ゼロ。
MOCK_EXPLICIT = _explicit_mock_flag()
MOCK_NO_KEY   = not get_api_key()
MOCK_MODE     = MOCK_EXPLICIT or MOCK_NO_KEY

CLAUDE_MODEL = "claude-sonnet-4-6"          # 高精度モデル（フォールバック）
CLAUDE_MODEL_FAST = "claude-haiku-4-5"       # 低単価モデル（一次解析）

# ハイブリッド判定: 一次解析をHaikuで行い、結果が弱い場合のみSonnetで再判定する。
# 無効化したい場合は HYBRID_MODE=0 を環境変数/Secretsで設定。
def _resolve_hybrid_mode() -> bool:
    import os as _os
    val = _os.environ.get("HYBRID_MODE", "")
    if not val:
        try:
            import streamlit as st
            val = str(st.secrets.get("HYBRID_MODE", ""))
        except Exception:
            val = ""
    # 既定は有効（未設定なら True）
    return val.strip().lower() not in ("0", "false", "no", "off")


HYBRID_MODE = _resolve_hybrid_mode()

# 対応Chapter一覧。data_file が存在しない場合はアプリ上でダウンロードを案内する。
SUPPORTED_CHAPTERS = {
    "9": {
        "label": "第9章: コーヒー・茶・マテ茶及び香辛料",
        "data_file": "hts_ch09_raw.json",
    },
    "16": {
        "label": "第16章: 肉・魚・甲殻類等の調製品",
        "data_file": "hts_ch16_raw.json",
    },
    "21": {
        "label": "第21章: 各種の食料品調製品（ラーメン・調味料等）",
        "data_file": "hts_ch21_raw.json",
    },
    "33": {
        "label": "第33章: 精油・化粧品・香水・スキンケア製品",
        "data_file": "hts_ch33_raw.json",
    },
    "39": {
        "label": "第39章: プラスチック及びその製品",
        "data_file": "hts_ch39_raw.json",
    },
    "42": {
        "label": "第42章: 革製品・旅行用品・ハンドバッグ等",
        "data_file": "hts_ch42_raw.json",
    },
    "71": {
        "label": "第71章: 宝石・貴金属・模造装飾品・貨幣",
        "data_file": "hts_ch71_raw.json",
    },
    "48": {
        "label": "第48章: 紙・板紙及びその製品",
        "data_file": "hts_ch48_raw.json",
    },
    "49": {
        "label": "第49章: 印刷物・書籍・新聞・絵画・その他の印刷産業製品",
        "data_file": "hts_ch49_raw.json",
    },
    "52": {
        "label": "第52章: 綿及び綿織物",
        "data_file": "hts_ch52_raw.json",
    },
    "61": {
        "label": "第61章: 衣類及び衣類附属品(ニット・クロシェ製)",
        "data_file": "hts_ch61_raw.json",
    },
    "62": {
        "label": "第62章: 衣類及び衣類附属品(織物製等)",
        "data_file": "hts_ch62_raw.json",
    },
    "63": {
        "label": "第63章: その他の紡織用繊維製品",
        "data_file": "hts_ch63_raw.json",
    },
    "64": {
        "label": "第64章: 履物・ゲートル等",
        "data_file": "hts_ch64_raw.json",
    },
    "65": {
        "label": "第65章: 帽子及びその部分品",
        "data_file": "hts_ch65_raw.json",
    },
    "67": {
        "label": "第67章: 羽毛・造花・人髪製品",
        "data_file": "hts_ch67_raw.json",
    },
    "69": {
        "label": "第69章: 陶磁製品",
        "data_file": "hts_ch69_raw.json",
    },
    "82": {
        "label": "第82章: 卑金属製の工具・刃物・スプーン・フォーク",
        "data_file": "hts_ch82_raw.json",
    },
    "83": {
        "label": "第83章: 卑金属製の雑品(バッジ・錠・金具等)",
        "data_file": "hts_ch83_raw.json",
    },
    "84": {
        "label": "第84章: 原子炉・ボイラー・機械類及びその部分品",
        "data_file": "hts_ch84_raw.json",
    },
    "85": {
        "label": "第85章: 電気機器及びその部分品・音響/映像機器",
        "data_file": "hts_ch85_raw.json",
    },
    "87": {
        "label": "第87章: 鉄道以外の車両及びその部分品",
        "data_file": "hts_ch87_raw.json",
    },
    "90": {
        "label": "第90章: 光学機器・写真用機器・測定機器・医療用機器",
        "data_file": "hts_ch90_raw.json",
    },
    "91": {
        "label": "第91章: 時計及びその部分品",
        "data_file": "hts_ch91_raw.json",
    },
    "92": {
        "label": "第92章: 楽器及びその部分品・附属品",
        "data_file": "hts_ch92_raw.json",
    },
    "94": {
        "label": "第94章: 家具・寝具・照明器具等",
        "data_file": "hts_ch94_raw.json",
    },
    "95": {
        "label": "第95章: 玩具・遊戯用具・運動用具(ホビー類)",
        "data_file": "hts_ch95_raw.json",
    },
    "96": {
        "label": "第96章: 雑品(ブラシ・ボタン・スタンプ・筆記具等)",
        "data_file": "hts_ch96_raw.json",
    },
}
