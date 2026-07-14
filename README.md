<div align="center">

<img src="framedeck/web/static/icons/icon.svg" width="96" alt="FrameDeck">

# FrameDeck

**自宅の漫画と動画を、どのデバイスでも快適に。**

セルフホスト型のローカルメディアサーバ兼ビューア。
NASや自宅サーバに置いたアーカイブをブラウザから開くだけで、
最適化された漫画リーダーと動画プレーヤーになります。

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.139-009688?logo=fastapi&logoColor=white)
![PWA](https://img.shields.io/badge/PWA-ready-5A0FC8?logo=pwa&logoColor=white)
![Tests](https://img.shields.io/badge/tests-119%20passed-brightgreen)
![Local First](https://img.shields.io/badge/cloud-not%20required-1a1b26)

</div>

---

## ✨ できること

|  | 漫画 📖 | 動画 🎬 |
|---|---|---|
| **対応形式** | ZIP / CBZ / RAR / CBR / 画像フォルダ(入れ子アーカイブ対応) | MP4 / MKV / AVI / MOV / WebM / TS ほか |
| **閲覧体験** | 見開き・単ページ・右綴じ/左綴じ、巻をまたぐ連続読書 | シーク・倍速・PiP・回転ロック・続きから再生 |
| **自動最適化** | 余白トリミング、見開き自動判定と分割、端末に合わせた軽量配信 | 回線と端末に応じた自動トランスコード(fMP4 / HLS) |
| **かしこい処理** | 前後ページとの合議でトリミング位置を安定化、縮小後シャープ化 | シーク位置からの再変換、視聴終了で変換を自動停止 |

さらに——

- 🗂 **ライブラリ管理** — 複数ルート登録、★評価、絞り込み/並び替え、ゴミ箱への安全な削除
- 📱 **モバイル最適化** — タップでページ送り、スワイプ、長押し高速送り、2行コントロール
- 🔖 **読書位置の記憶** — ページ・再生位置・表示設定をサーバ側で保存、端末をまたいで再開
- ⚡ **キャッシュ設計** — 解析結果・変換画像・HLSセグメントをディスクにキャッシュ。漫画キャッシュ(既定100MB)・動画キャッシュ(既定300MB)とも上限を超えると**古い順に自動削除**
- 🖥 **デスクトップUI(おまけ)** — Tkinter + mpv によるネイティブ再生モードも同梱

## 🚀 クイックスタート

### 1. 必要なものをインストール

| 必須/任意 | パッケージ | 用途 |
|---|---|---|
| **必須** | `python3` (3.10+) / `python3-venv` / `python3-pip` | 本体の実行と依存の自動構築 |
| 任意 | `ffmpeg` | 動画のトランスコード / HLS / サムネイル(なければ直接再生のみ) |
| 任意 | `unrar` | RAR / CBR アーカイブの展開(ZIP / CBZ はPythonのみで動作) |
| 任意 | `python3-tk` / `mpv` | デスクトップUIモード(`web_desktop`)を使う場合のみ |

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv python3-pip ffmpeg unrar

# デスクトップUIも使う場合
sudo apt install python3-tk mpv
```

Pythonライブラリ(FastAPI / Uvicorn / Pillow / rarfile など)は
初回起動時に `FrameDeck_venv/` へ**自動インストール**されるため、手動での pip 操作は不要です。

### 2. 起動

```bash
git clone https://github.com/souten-yd/FrameDeck.git
cd FrameDeck
python3 FrameDeck.py
```

起動後、必要なときにブラウザで `http://127.0.0.1:9000` を開きます。
Chromeなどのブラウザは自動起動しません。
あとは画面右上の ⚙ 設定から **漫画フォルダ / 動画フォルダ** を登録すれば準備完了。

### 起動モード

`FrameDeck.py` 冒頭の設定で切り替えます。

```python
APP_MODE = "web"          # Webサーバのみ(既定)
APP_MODE = "web_desktop"  # Webサーバ + デスクトップUI(Tkinter)
WEB_HOST = "0.0.0.0"      # LAN内の他端末からアクセス可能
WEB_PORT = 9000
```

## 📖 漫画リーダーの中身

スキャン品質がまちまちな実世界のアーカイブを、そのまま読みやすくするための処理が入っています。

```
アーカイブ ──▶ ページ解析 ──▶ 余白トリミング ──▶ 見開き判定/分割 ──▶ リサイズ ──▶ シャープ化 ──▶ WebP/AVIF配信
                 │
                 └─ 前後ページとの合議で位置を安定化(1枚だけの検出失敗も救済)
```

- 白・グレー・黒の余白や、スクリーンショット由来のレターボックス帯を自動除去
- 横長の見開き画像は、単ページモードでは右→左(右綴じ時)の順に半分ずつ表示
- 解析は縮小画像で高速に行い(~150ms/頁)、結果はディスクへキャッシュ
- 配信プロファイル(高画質/標準/軽量/データ節約)を PC・モバイルで個別に設定可能

## 🎬 動画プレーヤーの中身

- ブラウザが直接再生できる形式は **Range対応ストリーミング** でそのまま配信
- 非対応形式・低速回線では **ffmpegによる逐次トランスコード**
  - PC: フラグメントMP4のパイプ配信(シークは `?start=` 再要求)
  - iOS Safari: ネイティブ **HLS**(シーク位置からの再生成、視聴終了で変換停止・掃除)
- 回線速度・省データ設定・画面サイズから解像度を自動選択(手動指定も可)

## 🏗 構成

```
FrameDeck.py              ランチャー(venv自動構築 + 起動設定)
framedeck/
├── bootstrap.py          起動シーケンス
├── config.py             設定・パス管理(settings.json)
├── core/                 ライブラリ / 評価 / SQLite / セキュリティ
├── comic/                アーカイブ読込 / ページ解析 / 画像パイプライン
├── video/                ffprobe / Range配信 / トランスコード / HLS
├── web/                  FastAPI + Web UI(PWA)
└── desktop/              Tkinter UI(mpv連携)
tests/                    pytest(119件)
```

データはすべて `FrameDeck_venv/` 配下に保存されます(設定・DB・キャッシュ・ログ)。
リポジトリ本体はステートレスなので、消してもライブラリの実ファイルには影響しません。

## 🔒 セキュリティについて

FrameDeckは **ローカル / 家庭内LANでの利用を想定** しています。
インターネットへ直接公開しないでください。LAN内での簡易保護として、
設定 `web_pin` を指定すると localhost 以外からのアクセスにPINを要求します。

## 🧪 テスト

```bash
FrameDeck_venv/bin/python -m pytest tests/
```

実装の設計判断や検証記録は [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) にまとめています。

---

<div align="center">
<sub>🖼 手元のライブラリを、手元のサーバで。クラウド不要のメディア体験を。</sub>
</div>
