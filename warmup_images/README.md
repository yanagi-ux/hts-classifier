# warmup_images

モックデモで「本物の結果・課金ゼロ」を再現するための、ウォームアップ用画像置き場。

## 使い方

1. このフォルダに、デモで実際に使う画像を入れる
   （対応拡張子: .jpg .jpeg .png .webp .gif .bmp）
2. 通常モード（有効な ANTHROPIC_API_KEY）で実行:

   ```
   python warmup_cache.py warmup_images
   ```

3. data/analysis_cache.db に実APIの解析・章推定結果が保存される
4. クラウドデモに反映する場合:

   ```
   git add -f data/analysis_cache.db
   git commit -m "Add warmed cache" && git push
   ```

## 注意
- ここに置いた画像と、デモでアップロードする画像は「同一ファイル」である必要があります
  （ハッシュ一致でキャッシュがヒットするため）。
- 社外秘の画像はコミット対象のキャッシュに含めないでください。
