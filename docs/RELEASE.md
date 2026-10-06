# FrameDeck のリリース手順

## ブラウザを使わない公開経路

通常のCIは `.github/workflows/ci.yml` の workflow_dispatch 専用です。
公開・dispatch用APIが現在の接続ツールにない場合でも、従来成功した
**リリース専用ブランチの一度限りのworkflow**を使用できます。
ブラウザ操作が必要と決めつけず、まずこの経路を確認してください。

1. 対象の実装PRをレビュー・テストし、マージする。
2. `framedeck/__init__.py` の `__version__` と
   `packaging/qnap/qpkg.cfg` の `QPKG_VER` を同じ新バージョンにする。
3. 公開対象のmainコミットSHAを確定し、`ci/release-<version>` ブランチをそのSHAから作る。
4. 専用ブランチだけに `.github/workflows/release-<version>-one-shot.yml` を追加する。
   過去の成功workflowをテンプレートにし、版数・ブランチ・checkout SHA・
   release target SHA・説明を更新する。
5. 専用ブランチへのworkflow追加pushをトリガーにして、テスト、QDK導入、
   QPKGビルド、成果物保存、GitHub Release作成を一度だけ実行する。
6. Actionsの成功、Releaseの公開状態、タグの対象SHA、QPKG添付を確認する。
   開始しただけで「リリース完了」と報告しない。

## Workflow の要点

- push条件は `ci/release-<version>` と専用workflowのpathだけに限定する。
- `actions/checkout@v4` の `ref` は公開対象のmain SHAに固定する。
  ビルド対象と `gh release create --target` のSHAを一致させる。
- `permissions: contents: write` を使い、公開stepに
  `GH_TOKEN: ${{ github.token }}` を渡す。個人トークンやブラウザログインは不要。
- Python 3.12で以下を実行する。
  `python -m pip install -r packaging/qnap/requirements-qnap.txt -r requirements-test.txt`
- テストは `IMAGEIO_FFMPEG_EXE=/bin/true python -m pytest -q tests/`。
- QDK v2.5.3を公式releaseから導入し、`qbuild` と `qpkg_encrypt` が使えることを確認する。
- ビルドは `bash packaging/qnap/build.sh`。既存の固定ffmpegとSHA検証を維持する。
- `actions/upload-artifact@v4` で `dist/*.qpkg` を保存する。
- 公開は `gh release create v<version> dist/*.qpkg --target <SHA> --title "FrameDeck v<version>" --notes "<変更内容>"`。
- タイムアウトは45分。通常push/PR/tagでCIを起動する設定変更はしない。
  一度限りのworkflowをmainへマージしない。
- 失敗時はログを確認する。タグ・Release・添付の有無を確認してから再試行し、
  既存の公開物を無条件に上書きしない。

## 利用する接続ツール

`github_create_branch` で専用ブランチを作り、`github_create_file` でworkflowを追加できます。
`github_fetch` のGETでActions run/jobs、Release、tag refを確認できます。
これにより、release createやworkflow dispatchが直接露出していない接続でも公開できます。

## 実行記録

- v2.4.9: 成功済み。
  - ブランチ: `ci/release-2.4.9`
  - Workflow: `.github/workflows/release-2.4.9-one-shot.yml`
  - Workflowコミット: `343074ae34d10f982afd1be4716d440a3aff3aae`
  - [成功したActions run](https://github.com/souten-yd/FrameDeck/actions/runs/36823549155)
- v2.4.11: 成功済み。GitHub上の全294テスト・QPKGビルド・Release公開が成功し、タグとQPKG添付を確認。
  - 公開対象main: `6be0de9a94f8e5c981a8aca94bdab37ae97e1c7c` (PR #51)
  - ブランチ: `ci/release-2.4.11`
  - Workflow: `.github/workflows/release-2.4.11-one-shot.yml`
  - Workflowコミット: `3a95d068ea2480155a49c2ab053ce28b11283af3`
  - [Actions run](https://github.com/souten-yd/FrameDeck/actions/runs/37398937893)

  - [公開Release](https://github.com/souten-yd/FrameDeck/releases/tag/v2.4.11)
  - 成果物: `FrameDeck_2.4.11_TS-253Be_x86_64.qpkg`
