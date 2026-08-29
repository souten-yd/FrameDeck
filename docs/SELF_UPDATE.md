# FrameDeck self-update

FrameDeck 2.2.0 以降は、Web UI の **設定 → アプリ更新** から GitHub Releases の安定版を確認して更新できます。

## 動作

更新確認は設定画面を開いただけでは GitHub へアクセスしません。`更新を確認` を押した時だけ `souten-yd/FrameDeck` の latest stable Release を取得し、現在のバージョンと比較します。prerelease / draft は対象外です。

更新を実行する前に確認ダイアログを表示します。ダウンロードは永続データ領域の `runtime/updates/` に `.part` として保存し、完了後に置き換えます。Release asset に SHA-256 digest がある場合は照合します。更新ログは `logs/update.log` に保存されます。

## QNAP QTS

QNAP は `QNAP_QPKG` と実行環境から判定します。CPU architecture に一致する `.qpkg` Release asset のみを候補にし、TS-253Be では `x86_64` を選択します。複数の x86_64 QPKG がある場合は `TS-253Be` を名前に含む asset を優先します。

推奨 asset 名:

```text
FrameDeck_<version>_TS-253Be_x86_64.qpkg
```

ダウンロード後は QPKG を別プロセスからインストールするため、FrameDeck サービス自身が停止・更新されても updater helper は永続領域に残ります。QPKG の更新には QTS 側で FrameDeck サービスが管理者権限で実行されている必要があります。

## Ubuntu / Linux source installation

通常の `FrameDeck.py` 配置は Ubuntu/Linux として判定し、GitHub Release の source tarball を使用します。展開時に path traversal、symbolic link、hard link、device entry を拒否し、展開された `framedeck/__init__.py` の version が Release tag と一致することを確認します。

適用時は現在の `framedeck/` と `FrameDeck.py` を同じ配置先の `.framedeck-backup-<version>-<timestamp>/` に退避してから、新しいソースへ切り替えます。切替中に失敗した場合は旧コードへ戻します。成功後は同じ Python 環境で FrameDeck を再起動します。

## Release 作成時の注意

QNAP の自動更新を成立させるには、GitHub Release に対象 architecture の QPKG asset が必要です。GitHub Actions は自動実行せず、既存の manual validation workflow を必要な時だけ手動起動して QPKG を生成・Release へ登録してください。

FrameDeck 2.1.0 以前には updater 自体が入っていないため、2.2.0 への最初の移行だけは従来どおり手動インストールが必要です。それ以降は設定画面から更新できます。
