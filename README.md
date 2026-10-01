# bear-sky-map

熊の出没リスクが低く、夜空が暗い（SQM 20以上）「安全な天体観測スポット」を地図上で探すWebアプリ。

- **frontend/** 地図UI (Vite + TypeScript + MapLibre GL JS)
- **backend/** データ取得・ジオコーディング・空間演算 (Python)
- **data/** raw=取得した生データ / processed=加工済みGeoJSON

## 起動

```powershell
.\start-app.ps1          # dev server を起動してブラウザを開く
```

## データ更新

```powershell
.\run_refresh.ps1        # 最新データの取得 → 再計算 → フロントへ反映 → Discord通知
```

内部では `backend/refresh_data.py` が以下を順に実行する。

1. **取得** 東京都・宮城県の公開ページから最新ファイルのリンクを**動的に解決**してダウンロード
   （ファイル名に更新日が入るため固定URLは使えない）。中身がCSV/xlsxであることを検証する。
2. **変換** 東京はCSVに緯度経度があるのでそのまま変換。宮城は住所テキストを
   国土地理院APIでジオコーディング。結果は `data/processed/geocode_cache.json` に
   永続キャッシュされ、次回以降は新規レコードのみAPIを叩く（初回 約8分 → 2回目以降 数秒）。
3. **空間演算** 暗空エリア（SQM≥20）から熊出没2kmバッファを差し引いて安全エリアを算出。
4. **公開** `frontend/public/data/` へGeoJSONをコピー。
5. **通知** 実行結果とデータ品質の指摘をDiscordへ送信。

### データ品質の指摘について

提供元（自治体）のデータには未来日付や表記ゆれなどの誤りが混ざることがある。
本パイプラインはそれらを**黙って補正せず必ず報告する**方針。過去の実例:

- 宮城: 地区欄に市町村名が重複（「登米市登米市中田町浅水」）→ 自動除去するが報告する
- 宮城: 公表日より後の日付のレコード（8/13公表の資料に8/31付）→ 報告のみ

## Discord通知の設定（任意）

1. Discordで通知を受けたいサーバーのチャンネルを開く
2. チャンネル名の横の⚙️（チャンネルの編集）→ **連携サービス** → **ウェブフック** → **新しいウェブフック**
3. **ウェブフックURLをコピー**
4. そのURLを `backend/discord_webhook.txt` に貼り付けて保存（1行だけ）

未設定でもパイプラインは動作する（通知がスキップされるだけ）。

> ウェブフックURLは「それを知っている人は誰でもそのチャンネルに投稿できる」秘密情報。
> `.gitignore` で除外済みなので、GitHubに公開する際も漏れない。

### 通知テスト

```powershell
cd backend; .\venv\Scripts\python.exe notify.py
```

## 定期実行（Windowsタスクスケジューラ）

「タスクスケジューラ」を開き、**基本タスクの作成**から:

- トリガー: 毎日 6:00 など
- 操作: プログラムの開始
  - プログラム: `powershell.exe`
  - 引数: `-NoProfile -ExecutionPolicy Bypass -File "C:\ClaudeCode\bear-sky-map\run_refresh.ps1"`

実行ログは `logs/` に残る（直近30件を保持）。

## データ出典

- 東京都環境局「ツキノワグマ目撃等情報」
- 宮城県「クマ目撃情報」
- Light Pollution Atlas 2025 (David J. Lorenz) — VIIRS衛星データに基づく夜空の明るさ
- 地図: OpenStreetMap contributors

## 免責

「熊リスクが低い」は絶対的な安全を意味しない。出没情報は報告されたものに限られ、
報告のない場所に熊がいないことを保証しない。実際の観測時は各自で安全対策を行うこと。
