# FrameDeck 2.0 実装メモ

改修指示書に基づく全面改修の実装記録。**指示書と異なる判断をした箇所**と
その理由をここに記録する。

## 起動方法

```bash
python3 FrameDeck.py
```

- `FrameDeck.py` 冒頭の `APP_MODE` で起動モードを切替:
  - `"web"`(既定): Webサーバのみ。`http://127.0.0.1:9000`
  - `"web_desktop"`: Webサーバ + Tkinter UI 同時起動
- 依存パッケージは初回起動時に `FrameDeck_venv/` へ自動インストール。
  失敗時は無限再起動せず、手動コマンドを表示して終了する。

## テスト

```bash
FrameDeck_venv/bin/python -m pytest tests/
```

57件(シーケンス構築 / 漫画ナビゲーション / アーカイブ安全性 /
Range配信 / ストレージ / Web API / コンテナ判定)。

## 構成

```
FrameDeck.py              ランチャー(venvブートストラップ + 起動設定)
framedeck/
├── bootstrap.py          起動シーケンス(web / web_desktop)
├── config.py             設定・パス管理(settings.json)
├── models/               MediaItem / ComicEntry / ComicSession / VideoInfo…
├── core/                 storage(SQLite) / library / rating / security / services
├── comic/                archive_backend / nested_cache / source /
│                         sequence_builder / reader_engine / image_pipeline
├── video/                probe(ffprobe) / stream(Range) / transcode(fMP4) /
│                         playback_service / mpv_controller(JSON IPC)
├── web/                  FastAPIアプリ + routers + static Web UI(PWA)
└── desktop/              Tkinter UI(共通サービスへ接続)
tests/                    pytest一式
FrameDeck_legacy.py.bak   旧実装のバックアップ(参照用)
```

永続データはすべて `FrameDeck_venv/` 配下:
`config/settings.json`、`data/framedeck.db`、`cache/`(comic_pages /
nested_archives / thumbnails / transcodes)、`logs/framedeck.log`、`runtime/mpv`。

---

## 指示書からの逸脱と理由

### 1. ReadingSequence は「遅延実体化」(指示書7.3の変形)

指示書どおりルート直下全体を即時に一次元化すると、実ライブラリ
(/mnt/Download/Manga、726項目・NAS上)で**シーケンス構築に2分以上**
かかることが実測で判明した(全アーカイブを開いて子アーカイブを検出する
ため)。そこで:

- ルート直下の**トップ項目の順序だけを先に確定**(listdir + 自然順)
- 各トップ項目内の `ComicEntry` 列挙は**必要時に展開してキャッシュ**
- 現在位置は `(top_index, sub_index)` カーソルで管理

読書順の定義(自然順・親直接画像→子アーカイブ)は完全実体化と同一。
`ComicViewState.entry_index / entry_count` は「現在のライブラリ項目内」
の値になる(例: フォルダ内28冊の3冊目 → 3/28)。セッション作成は
実測 0.35秒 に短縮。

### 2. 非対応動画は HLS ではなく fMP4 プログレッシブ変換(指示書15.4)

指示書はHLS生成を第一候補としているが、hls.js の同梱が必要になり
「ビルド不要のフロントエンド」方針と衝突するため、初期実装は
ffmpeg による **フラグメントMP4のパイプ配信**
(`/api/videos/{id}/stream-transcode?start=秒`)とした。
シークはクライアントが `start` を付けて再要求する方式で実現済み。
HLS・完全変換キャッシュは将来拡張として `transcodes/` ディレクトリを
確保してある。

### 3. 内部IDは「評価タグを除いた正規化パス」のハッシュ

`media_id = sha1(canonical_path)`(`{zpi$r=N}` を除去したパス)とした。
これにより**評価の変更(リネーム)でIDが変わらず**、読書位置・再生位置が
評価操作で失われない。`ComicEntry.id` も同様に正規化して計算する。

### 4. mpv のnavファイル150msポーリングを廃止(指示書15.5どおり)

Luaスクリプトは `script-message framedeck-nav prev/next` を送り、
JSON IPC の `client-message` イベントとして受信する。プロパティ購読
(`time-pos` / `duration` / `pause` 等)で再生位置を保存する。
Windows named pipe は未実装(既存機能もLinux前提のため)。

### 5. RARバックエンドの実状

このマシンでは bsdtar / 7z が無く `rarfile + unrar` が使われる
(優先順位 bsdtar → 7z → rarfile は実装済み)。パスワード付き
アーカイブは明示エラー。アーカイブ内エントリ名は Zip Slip 検査
(`../`・絶対パス・ドライブ・NUL を拒否)を通過したものだけを扱う。

### 6. P1機能の未実装分(見かけ上のダミーは作っていない)

- 縦スクロール / 横連続表示モード(単ページ・見開きのみ実装)
- 字幕トラックのWeb側切替(トラック情報の取得・表示APIはあり)
- 外部字幕読み込み、A-Bリピート、シークプレビュー、ページブックマークUI
- PDF対応(`ComicSource` プロトコルで拡張可能な構造のみ)
- AIアップスケール(未着手。パイプラインは差し込み可能な構造)

### 7. セキュリティ

- Web APIは登録済み `library_roots` 配下のみアクセス可
  (resolve + is_relative_to、シンボリックリンク脱出も拒否)
- 削除は「確認トークン発行 → トークン付きDELETE」の2段階。
  既定は send2trash によるゴミ箱移動
- `web_pin` 設定時、localhost以外からのアクセスにPINを要求
- クライアントへ絶対パスは返さない(内部IDとルート相対表示のみ)

## 実データでの検証結果(2026-07-11)

- 漫画: 726項目のライブラリでセッション作成0.35秒、ページ配信38ms、
  漫画間移動14ms。ETag/304、リサイズ配信、サムネイル動作確認
- 動画: 4.4GBのMP4でRange(206/416/HEAD)動作確認。
  MKVはfMP4変換ストリーミングへフォールバック
- 評価: 実ファイルで `{zpi$r=N}` リネーム往復・ID安定を確認
- ポート9000競合時は明確なエラーで終了(exit 1)
- Tkinter UIは web_desktop 相当の経路で起動確認(726項目読込)
