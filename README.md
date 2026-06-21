# QTL_Bot - Discord 地震・津波・火山情報通知 Bot

気象庁（JMA）の API と複数のデータソースを使用して、地震・津波・火山情報を Discord に自動通知する Bot です。Raspberry Pi での常時運用を想定した軽量実装。

---

## 機能一覧

### 地震情報通知
- **EEW（緊急地震速報）**: Wolfx WebSocket でリアルタイム受信
  - 予測震度・推奨行動を通知
  - 警報地域をすべて読み上げ（AquesTalkPi）
  - 警報地域追加時に高優先で再読み上げ
- **P2P 地震情報**: P2P 地震情報 API からの確報情報（震度速報・震源情報・各地の震度情報）
- **EEW フォールバック**: Wolfx WebSocket が無応答になると P2P EEW / LMoni EEW に自動切り替え

### 津波警報
- JMA 津波警報 API から自動取得
- 警報種別別の色分け（大津波警報 / 津波警報 / 津波注意報）
- 通知フォーマット: `{Head.Title}（{Head.InfoType}）` / 発表日時 / 有効期間 / 原因地震 / `{Head.Headline.Text}` / 津波観測値 / `{Body.Text}` / `{Body.Comments.WarningComment.Text}`
- `Head.ValidDateTime` が存在する場合のみ「有効期間」を表示（ISO 8601 → 日本語形式に変換）
- 有効期限切れの情報は通知しない
- 原因地震に `Earthquake.Source` が存在する場合は出典を付記

### 火山情報
- JMA 火山情報 API からのリアルタイム監視
- **更新検知と通知処理を分離した2タスク構成**:
  - `poll_volcano` (@tasks.loop, 1分ごと): info.json を取得し差分を検知してキューに投入
  - `process_volcano_queue` (asyncio.Task): キューから eventId を取り出し詳細取得・通知
- 通知フォーマット: `{controlTitle}（{infoType}）` / 発表機関 / 発表日時 / 概要 / 詳細 / 防災上の注意
- 噴火警戒レベル L1–L5 の色分け表示
- 警戒レベル L1–L3 は音声読み上げ

### USGS 地震情報
- 米国地質調査所（USGS）の `all_day.geojson`（過去24時間）から海外の地震情報を取得
- 対象地域・マグニチュード閾値をカスタマイズ可能
- 重複通知防止（通知済み ID を `USGS_NOTIFICATION_COOLDOWN` 秒保持）
- Bot 起動時に最新の対象地震を1件通知

### エラー自動通知
- 重大エラー発生時に管理者チャンネルに自動通知（1時間以内の重複を抑制）
- 毎日 00:00 に前日のエラー集計を通知

### ヘルスチェック
- Wolfx WebSocket / JMA API / P2P 地震情報の疎通確認
- `/health/full` エンドポイントで詳細情報を取得（30秒キャッシュ）

### ログ管理
- `RotatingFileHandler` による自動ログローテーション（デフォルト: 10MB × 7世代）
- ファイル / コンソールで独立したログレベル設定
- 同一メッセージを指定秒数以内は出力抑制（ERROR 以上は常に出力）
- aiohttp.access の 2xx 成功ログを自動除外

### リソース監視
- 1時間ごとに CPU・メモリ・ディスク使用率を記録
- ディスク使用率が閾値を超えると WARNING / ERROR を記録

### 長周期地震動
- 長周期地震動の観測情報（1分ごとのポーリング）
- リアルタイム強震モニタ画像

### Web Dashboard / コマンド
- `GET /status`: 詳細な稼働状況 JSON（タスク稼働状態・USGS 設定・システムリソース等）
- `GET /health/full`: API 疎通確認
- `!status`: プレフィックスコマンド（管理者専用）
- `/qtl_status`: スラッシュコマンド（管理者専用、`!status` と同内容）

---

## セットアップ

### 必須環境
- Python 3.11+
- discord.py 2.0+
- aiohttp / websockets / pygame / python-dotenv
- psutil（推奨：リソース監視・Web Dashboard 用）

### インストール
```bash
git clone https://github.com/akethimitsuhide/QTL_Bot.git
cd QTL_Bot
pip install -r requirements.txt
```

### 設定
```bash
cp .env.example .env   # example.md の bash コードブロック内容を .env としてコピー
```

必須設定:
```bash
BOT_TOKEN=your_bot_token_here
CHANNEL_ID=your_channel_id
```

### 起動
```bash
python bot.py
```

正常起動時のログ:
```
[INFO] ロギングをセットアップしました (FILE=INFO/CONSOLE=INFO, ...)
[INFO] スラッシュコマンドを同期しました（N件）
[INFO] ログイン完了: BotName#1234
[INFO] Volcano poll_volcano タスク開始 (every 1 minute)
[INFO] Web ダッシュボード起動: http://localhost:8080/status
```

---

## 環境変数リファレンス

### Discord 設定
| 変数名 | 必須 | 既定値 | 説明 |
|:---|:---:|:---|:---|
| `BOT_TOKEN` | ✅ | — | Discord Bot トークン |
| `CHANNEL_ID` | ✅ | — | デフォルト通知チャンネル ID（全通知のフォールバック先） |
| `EEW_CHANNEL_ID` | | CHANNEL_ID | EEW 専用チャンネル |
| `P2P_EEW_CHANNEL_ID` | | EEW_CHANNEL_ID | P2P EEW 専用チャンネル |
| `LMONI_EEW_CHANNEL_ID` | | EEW_CHANNEL_ID | LMoni EEW 専用チャンネル |
| `QUAKE_CHANNEL_ID` | | CHANNEL_ID | 地震情報専用チャンネル |
| `TSUNAMI_CHANNEL_ID` | | CHANNEL_ID | 津波警報専用チャンネル |
| `VOLCANO_CHANNEL_ID` | | CHANNEL_ID | 火山情報専用チャンネル |
| `USGS_CHANNEL_ID` | | QUAKE_CHANNEL_ID | USGS 通知専用チャンネル |
| `OTHER_CHANNEL_ID` | | CHANNEL_ID | その他情報（長周期地震動等） |
| `KYOSHIN_CHANNEL_ID` | | OTHER_CHANNEL_ID | 強震モニタ専用チャンネル |
| `ADMIN_CHANNEL_ID` | | 0（無効） | エラー通知用管理者チャンネル |

### 通知フィルター設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `QUAKE_MIN_SCALE` | 0 | 地震通知の震度下限（0=全て / 10=震度1以上 / 30=震度3以上 / 50=震度5弱以上） |
| `QUAKE_MIN_MAG` | 0.0 | 地震通知のマグニチュード下限 |
| `QUAKE_MIN_DEPTH` | 0 | 震源の深さ下限（km） |
| `QUAKE_MAX_DEPTH` | 9999 | 震源の深さ上限（km） |
| `EEW_MIN_INTENSITY` | 0 | EEW 通知の最低震度 |
| `QUAKE_ENABLE_SCALE_PROMPT` | true | 震度速報の通知 |
| `QUAKE_ENABLE_DESTINATION` | true | 震源に関する情報の通知 |
| `QUAKE_ENABLE_SCALE_AND_DEST` | true | 震度・震源情報の通知 |
| `QUAKE_ENABLE_DETAIL_SCALE` | true | 各地の震度情報の通知 |
| `QUAKE_ENABLE_FOREIGN` | true | 海外地震の通知 |
| `QUAKE_ENABLE_OTHER` | true | その他地震情報の通知 |
| `TSUNAMI_ENABLE` | true | 津波情報通知の有効化 |
| `ENABLE_LONG_PERIOD` | true | 長周期地震動通知の有効化 |
| `ENABLE_ADVISORY` | true | 気象庁その他情報の有効化 |
| `ENABLE_TSUNAMI_OBS` | true | 津波観測情報通知の有効化 |
| `ENABLE_KYOSHIN` | true | 強震モニタ通知の有効化 |

### USGS 地震情報設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `USGS_ENABLED` | true | USGS 地震通知機能の有効化 |
| `USGS_MAGNITUDE_MIN` | 5.0 | 通知対象のマグニチュード下限 |
| `USGS_FETCH_INTERVAL` | 600 | ポーリング間隔（秒） |
| `USGS_REGION_LAT_MIN` | 20 | 対象地域の緯度下限 |
| `USGS_REGION_LAT_MAX` | 50 | 対象地域の緯度上限 |
| `USGS_REGION_LON_MIN` | 120 | 対象地域の経度下限 |
| `USGS_REGION_LON_MAX` | 180 | 対象地域の経度上限 |
| `USGS_NOTIFICATION_COOLDOWN` | 86400 | 通知済み ID の保持時間（秒）。all_day の収録期間に合わせ24時間 |

### EEW・フォールバック設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `WOLFX_HEARTBEAT_TIMEOUT` | 90 | Wolfx heartbeat タイムアウト（秒） |
| `EEW_FALLBACK_TIMEOUT` | 30 | フォールバック切り替え閾値（秒） |
| `FETCH_FAILURE_THRESHOLD` | 3 | API 連続失敗でバックオフする回数 |
| `FETCH_BACKOFF_SECONDS` | 60 | API 失敗時のバックオフ待機時間（秒） |

### 音声設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `AQUESTALK_PATH` | （空） | AquesTalkPi の実行ファイルパス（未設定で音声無効） |
| `AQUESTALK_SPEED` | 150 | 読み上げ速度 |
| `AUDIO_PLAYER` | aplay | 音声再生コマンド |
| `SPEECH_QUEUE_MAXSIZE` | 200 | 音声読み上げキューの最大サイズ |
| `MP3_QUEUE_MAXSIZE` | 50 | MP3 再生キューの最大サイズ |

### ログ設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `LOG_LEVEL` | INFO | ログレベル（後方互換。FILE/CONSOLE 未設定時の既定値） |
| `LOG_LEVEL_FILE` | LOG_LEVEL | ファイルへの出力ログレベル |
| `LOG_LEVEL_CONSOLE` | LOG_LEVEL | コンソールへの出力ログレベル |
| `LOG_MAX_BYTES` | 10485760 | ログファイルの最大サイズ（バイト） |
| `LOG_BACKUP_COUNT` | 7 | ローテーション保持ファイル数 |
| `LOG_DUPLICATE_THRESHOLD` | 60 | 同一メッセージの重複抑制時間（秒）。ERROR 以上は常に出力 |
| `LOG_SUPPRESS_HTTP_SUCCESS` | true | aiohttp.access の 2xx ログを抑制 |

### Web Dashboard 設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `WEB_DASHBOARD_ENABLED` | false | Web Dashboard の有効化 |
| `WEB_DASHBOARD_PORT` | 8080 | ポート番号 |

### ステータス表示設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `STATUS_SHOW_CPU` | true | CPU 使用率を表示 |
| `STATUS_SHOW_MEM` | true | メモリ使用量を表示 |
| `STATUS_SHOW_DISK` | true | ディスク使用率を表示 |
| `STATUS_SHOW_UPTIME` | true | フィルター設定を表示 |

### リソース監視設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `RESOURCE_MONITORING_ENABLED` | true | リソース監視の有効化 |
| `RESOURCE_CHECK_INTERVAL` | 3600 | 監視間隔（秒） |
| `DISK_WARNING_THRESHOLD` | 80 | ディスク WARNING 閾値（%） |
| `DISK_ERROR_THRESHOLD` | 90 | ディスク ERROR 閾値（%） |

---

## Web Dashboard

### GET /status

```bash
curl http://localhost:8080/status | jq
```

主要フィールド:
```json
{
  "status": "online",
  "timestamp": "2026-06-20T12:00:00.000000",
  "bot_user": "QTL_Bot#1234",
  "uptime": "1d 5h 30m",
  "ping_ms": 45,
  "system": {
    "cpu_percent": 3.1,
    "memory_mb": 95.2,
    "disk_percent": 42.5
  },
  "eew": {
    "wolfx": { "ws_status": "online", "heartbeat_elapsed_sec": 12.4 },
    "fallback_active": false
  },
  "monitoring": {
    "volcano": {
      "last_event_id": "20260620120000",
      "polling_status": "running",
      "notify_status": "running"
    },
    "usgs": {
      "enabled": true,
      "magnitude_min": 5.0,
      "last_event_ids": ["us7000std7"]
    }
  },
  "tasks": {
    "fetch_quake": "running",
    "fetch_tsunami": "running",
    "fetch_long_period": "running",
    "fetch_tsunami_observation": "running",
    "fetch_quake_advisory": "running",
    "fetch_usgs_quake": "running",
    "speech_worker": "running",
    "mp3_worker": "running",
    "poll_volcano": "running",
    "process_volcano_queue": "running"
  }
}
```

後方互換フィールド（`api_status` / `recv_count` / `volcano_monitoring` / `memory_usage_mb`）も引き続き含まれます。

### GET /health/full

```bash
curl http://localhost:8080/health/full | jq
```

### GET /health

```bash
curl http://localhost:8080/health
# {"status": "online"}
```

---

## Discord コマンド

| コマンド | 種別 | 権限 | 説明 |
|:---|:---|:---|:---|
| `!status` | プレフィックス | 管理者 | Bot 稼働状態を Embed で表示 |
| `/qtl_status` | スラッシュ | 管理者 | `!status` と同内容 |

---

## コード構成

```
bot.py
└── QuakeTsunamiCog
    ├── @tasks.loop
    │   ├── fetch_quake()                  P2P地震情報（3秒ごと）
    │   ├── fetch_tsunami()                P2P津波情報（10秒ごと）
    │   ├── fetch_long_period()            長周期地震動（60秒ごと）
    │   ├── fetch_tsunami_observation()    津波観測情報（60秒ごと）
    │   ├── fetch_quake_advisory()         気象庁その他情報（60秒ごと）
    │   ├── fetch_usgs_quake()             USGS地震情報（USGS_FETCH_INTERVAL秒ごと）
    │   └── poll_volcano()                 火山情報更新検知（60秒ごと）
    │
    ├── asyncio.Task
    │   ├── connect_eew_ws()               Wolfx EEW WebSocket
    │   ├── _eew_fallback_monitor()        EEWフォールバック監視
    │   ├── process_volcano_queue()        火山情報通知処理
    │   ├── speech_worker()                AquesTalkPi 読み上げ
    │   └── mp3_worker()                   MP3 再生
    │
    ├── notify_eew()                       EEW 通知
    ├── notify_quake()                     地震情報通知
    ├── notify_tsunami()                   津波情報通知
    ├── notify_tsunami_observation()       津波観測情報通知
    ├── notify_long_period()               長周期地震動通知
    ├── notify_quake_advisory()            気象庁その他情報通知
    ├── notify_usgs_quake()                USGS地震情報通知
    ├── _notify_volcano()                  火山情報通知
    └── notify_error()                     エラー自動通知
```

---

## 火山情報の仕様

### 差分検知の仕組み
- `poll_volcano()` が毎分 `info.json` をフェッチし、全エントリを `{eventId: item}` の dict として保持
- 前回 dict に存在しない `eventId` を新規・更新として `volcano_event_queue` に投入
- 初回起動時は先頭1件のみ（大量通知防止）
- `process_volcano_queue()` がキューから順番に取り出し、`info/{eventId}.json` を取得して通知

### 通知フォーマット
Embed に以下を表示（キーが存在しない場合は省略）:
- `{controlTitle}（{infoType}）`
- 発表機関: `{publishingOffice}`
- 発表日時: `{reportDatetime}`（ISO 8601 → 日本語形式に変換）
- 概要: `{volcanoHeadline}`
- 詳細: `{volcanoActivity}`
- 防災上の注意: `{volcanoPrevention}`

### 警戒レベルの色
| コード | レベル | 色 |
|:---|:---|:---|
| 01 | 活火山であることに留意 | 紫 |
| 02 | 火口周辺規制 | 赤 |
| 03 | 入山規制 | 橙 |
| 04 | 居住地域避難準備 | 黄 |
| 05 | 居住地域への避難 | 青 |

---

## 津波情報の仕様

### 通知条件
- `Head.ValidDateTime` が存在し現在時刻がその時刻を過ぎている場合は通知しない
- `Head.ValidDateTime` が存在しない場合は通知する

### 通知フォーマット
- `{Head.Title}（{Head.InfoType}）`
- 発表日時
- 有効期間（存在する場合のみ・日本語形式に変換）
- 原因地震（`Earthquake.Source` が存在する場合は出典を付記）
- `{Head.Headline.Text}`
- 津波観測値
- `{Body.Text}`
- `{Body.Comments.WarningComment.Text}`

---

## ログ管理の推奨設定

通常運用（Raspberry Pi）での推奨 `.env` 設定:
```bash
LOG_LEVEL_FILE=INFO
LOG_LEVEL_CONSOLE=INFO
LOG_DUPLICATE_THRESHOLD=60
LOG_SUPPRESS_HTTP_SUCCESS=true
```

Mackerel 等で `/status` を定期ポーリングしている場合、`LOG_SUPPRESS_HTTP_SUCCESS=true` により 200 ログが記録されず、ディスク書き込みが抑制されます。

---

## トラブルシューティング

### 火山情報が通知されない
```bash
curl http://localhost:8080/status | jq '.tasks.poll_volcano, .tasks.process_volcano_queue'

grep -i volcano qtlbot.log | tail -20
# "Volcano: no change"       → 変化なし（正常）
# "Volcano: N件の新規/更新"   → 検知して通知処理へ
# "Volcano: 初回起動"        → 起動後の初回フェッチ
```

### EEW 読み上げで地域名が出ない
- `AQUESTALK_PATH` が正しく設定されているか確認

### USGS 地震情報が重複通知される
- `USGS_NOTIFICATION_COOLDOWN` のデフォルトは 86400 秒（24時間）です
- 古いバージョンから移行した場合は `.env` の設定値を確認してください

### EEW フォールバックが頻発する
```bash
curl http://localhost:8080/status | jq '.eew'
# wolfx.ws_status が "timeout" → Wolfx WebSocket の再接続待ち
# fallback_active が true → P2P EEW / LMoni で受信中
```

### ディスク書き込みが増加している
- 大地震発生時は EEW 更新・地震通知・音声ログが集中して増加します
- `LOG_LEVEL_FILE=INFO`（DEBUG にしない）と `LOG_SUPPRESS_HTTP_SUCCESS=true` を確認してください

---

## ライセンス
MIT License

## 謝辞
- 気象庁（JMA）API
- Wolfx EEW 配信サービス
- P2P 地震情報
- 米国地質調査所（USGS）

---

**最終更新**: 2026-06-20
**対応 Python**: 3.11+
**対応 discord.py**: 2.0+