"""Streamlit Cloud 等での「無料デモ」公開用エントリポイント。

このファイルを起動ファイルに指定すると、Secrets の設定に関係なく
必ずモックモード（Claude API を呼ばない・課金ゼロ）で起動する。
無効な API キーが残っていても 401 にならず、安全にデモできる。

通常の実判定で使う場合は、このファイルではなく app.py を起動すること。
"""
import os
import runpy
from pathlib import Path

# app.py / config.py が読み込まれる前にモックを強制する
os.environ["MOCK_MODE"] = "1"

_app = Path(__file__).parent / "app.py"
runpy.run_path(str(_app), run_name="__main__")
