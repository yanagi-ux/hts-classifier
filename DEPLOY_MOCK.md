# モックモードを Streamlit Cloud で他ユーザーに提供する手順

モックモードは **Claude API を呼ばない＝課金ゼロ・APIキー不要** です。
UI・バッチ処理・Excel出力・操作性などの動作テストを、URLを共有するだけで
他ユーザーに触ってもらえます（判定結果はダミーなので精度評価には使えません）。

## 手順

1. 最新コードを GitHub に push 済みであることを確認
   （リポジトリ: `yanagi-ux/hts-classifier`）。

2. ブラウザで <https://share.streamlit.io> を開き、GitHub アカウントでサインイン。

3. **「Create app」→「Deploy a public app from GitHub」** を選択。
   - Repository: `yanagi-ux/hts-classifier`
   - Branch: `main`
   - Main file path: `app.py`

4. **「Advanced settings」→「Secrets」** に以下を貼り付ける（これがモード切替）:

   ```toml
   MOCK_MODE = "1"
   ```

   ※ モックモードでは `ANTHROPIC_API_KEY` は不要です。
   　 本番（実判定）として公開する場合のみ、ここに API キーを追加し
   　 `MOCK_MODE` を削除（または `"0"`）してください。

5. **「Deploy」** を押す。数分でビルドが完了し、
   `https://<アプリ名>.streamlit.app` の公開URLが発行される。

6. このURLをテスターに共有。インストール不要・ブラウザだけで利用可能。
   画面上部に「🧪 モックモード稼働中」のバナーが出ていれば成功。

## 補足

- 公開URLは誰でもアクセス可能です。社外秘の画像はアップロードしないよう
  テスターに周知してください（モックでもアップロード自体は行われます）。
- 本番デプロイに切り替える際は、レート制限（現状 Tier 1）と費用に注意。
- ローカルでモックを起動する場合:
  - PowerShell: `$env:MOCK_MODE = "1"; streamlit run app.py`
  - Git Bash:  `MOCK_MODE=1 streamlit run app.py`
