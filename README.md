# QTL_Bot - Discord 地震・津波・火山情報通知 Bot

気象庁（JMA）の API と複数のデータソースを使用して、地震・津波・火山情報を Discord に自動通知する Bot です。Raspberry Pi での常時運用を想定した軽量実装。

---

## 機能一覧

### 地震情報通知
- **EEW（緊急地震速報）**: Wolfx WebSocket でリアルタイム受信
  - 予測震度、到達時間、推奨行動を通知
  - 音声読み上げ対応（AquesTalkPi）
- **P2P EEW（緊急地震速報（警報）専用）**: P2P 地震情報 WebSocket から警報のみを常時受信
  - Wolfx と同時並行稼働（EventID による重複排除あり）
- **地震速報**: P2P 地震情報 API からの確報情報

### 津波情報
- JMA 津波警報 API から自動取得（予報・警報・注意報）
- 警報種別・予想高さ別のエリア一覧表示（大津波警報 / 津波警報 / 津波注意報 / 津波予報）
- 本文（`Body.Text`）・解説（`Body.Comments.FreeFormComment`）を通知下部に追記
- 津波観測情報（`VTSE41/51`）を別関数で処理

### 火山情報
- JMA 火山情報 API からのリアルタイム監視（1分ごと）
- 差分検知ベース（`json` フィールドの変化で新規判定）
- 火山活動の状況・予防措置・次回発表予定を通知
- 噴火速報（VFVO50）・噴火警報（VFVO53）の独立ポーリング

### USGS 地震情報通知
- 米国地質調査所（USGS）から海外の地震情報を取得
- 対象地域・マグニチュード閾値をカスタマイズ可能
- 重複排除機能（クールダウン付き）

### エラー自動通知
- 重大エラー発生時に管理者チャンネルに自動通知（1時間に1回）
- 日次サマリー（毎日 00:00 に前日のエラー集計を通知）
- 管理者チャンネルは `ADMIN_CHANNEL_ID` で設定

### ヘルスチェック
- Wolfx WebSocket、JMA API、P2P 地震情報の疎通確認
- `/health/full` エンドポイントで詳細情報を取得（30秒キャッシュ）

### ログ管理
- `RotatingFileHandler` による自動ログローテーション（デフォルト: 10MB x 7世代）
- ファイル / コンソール独立ログレベル設定（`LOG_LEVEL_FILE` / `LOG_LEVEL_CONSOLE`）
- 重複ログ抑制: 同一メッセージを指定秒数以内は出力抑制（ERROR 以上は常に出力）

### リソース監視
- 1 時間ごとに CPU・メモリ・ディスク使用率を記録
- ディスク使用率が 80% 以上で WARNING、90% 以上で ERROR を記録

### 長周期地震動
- 長周期地震動の観測情報
- リアルタイム強震モニタ画像（3秒間隔で更新）
- 振動レベルに応じた音声アラート（該当レベルの間、3秒間隔で継続再生）
  - レベル 100〜999: `lv100.mp3`
  - レベル 1000〜1999: `lv1000.mp3`
  - レベル 2000 以上: `lv2000.mp3`
  - レベルが下降し tier が変わった場合は新しい tier の MP3 に切り替わる（100未満は無音）

### Web Dashboard / コマンド
- `GET /status` で詳細な稼働状況を JSON で取得
- `!status` コマンド（管理者専用）
- `/qtl_status` スラッシュコマンド（管理者専用）

---

## セットアップ

### 必須環境
- Python 3.11+
- discord.py 2.0+
- aiohttp（非同期 HTTP 通信）
- psutil（オプション：システムリソース監視）

### インストール
```bash
git clone https://github.com/akethimitsuhide/QTL_Bot.git
cd QTL_Bot
pip install -r requirements.txt

# AquesTalkPi インストール（オプション：音声読み上げ）
# Raspberry Pi 向け: https://www.a-quest.com/products/aquestalkpi.html
```

### 設定

#### 1. Discord Bot トークン取得
1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセス
2. 新規アプリケーション作成
3. "Bot" タブから Bot トークンをコピー
4. 必要な Intent を有効化：
   - Message Content Intent
   - Server Members Intent
5. サーバーに Bot を招待（OAuth2 URL で Administrator 権限付与）

#### 2. 環境変数設定
`.env.example` をコピーして `.env` を作成し、値を設定してください：
```bash
cp .env.example .env
# .env をエディタで開いて BOT_TOKEN と CHANNEL_ID を設定
```

全環境変数の詳細は `env.md` または README 下部の「環境変数リファレンス」を参照してください。

#### 3. チャンネル設定
Bot が通知を送信するテキストチャンネルを作成し、ID を `.env` に設定：
- `EEW_CHANNEL_ID` : EEW（緊急地震速報）
- `P2P_EEW_CHANNEL_ID` : P2P EEW（緊急地震速報（警報））
- `QUAKE_CHANNEL_ID` : 地震情報
- `TSUNAMI_CHANNEL_ID` : 津波警報
- `VOLCANO_CHANNEL_ID` : 火山情報
- `USGS_CHANNEL_ID` : USGS 海外地震情報（未設定時は `QUAKE_CHANNEL_ID`）

未設定のチャンネルはすべて `CHANNEL_ID` にフォールバックします。

#### 4. Bot の起動
```bash
python bot.py
```

---

## 環境変数リファレンス

### Discord 設定
| 変数名 | 必須 | 既定値 | 説明 |
|:---|:---:|:---|:---|
| `BOT_TOKEN` | 必須 | — | Discord Bot のトークン |
| `CHANNEL_ID` | 必須 | — | デフォルト通知チャンネル ID |
| `EEW_CHANNEL_ID` | | CHANNEL_ID | EEW 専用チャンネル |
| `P2P_EEW_CHANNEL_ID` | | EEW_CHANNEL_ID | P2P EEW（警報）専用チャンネル |
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
| `QUAKE_MIN_SCALE` | 0 | 地震通知の震度下限（0=全て / 10=震度1以上 / 30=震度3以上 / 45=震度4以上 / 50=震度5弱以上） |
| `QUAKE_MIN_MAG` | 0.0 | 地震通知のマグニチュード下限 |
| `QUAKE_MIN_DEPTH` | 0 | 地震通知の深さ下限（km） |
| `QUAKE_MAX_DEPTH` | 9999 | 地震通知の深さ上限（km） |
| `EEW_MIN_INTENSITY` | 0 | EEW 通知の最低震度（0=全て） |
| `QUAKE_ENABLE_DESTINATION` | true | 震度情報付き地震の通知 |
| `QUAKE_ENABLE_SCALE_AND_DEST` | true | 震度・震源情報付き地震の通知 |
| `QUAKE_ENABLE_SCALE_PROMPT` | true | 震度速報の通知 |
| `QUAKE_ENABLE_DETAIL_SCALE` | true | 詳細震度情報の通知 |
| `QUAKE_ENABLE_FOREIGN` | true | 海外地震の通知 |
| `QUAKE_ENABLE_OTHER` | true | その他地震情報の通知 |
| `TSUNAMI_ENABLE` | true | 津波情報通知の有効化 |
| `ENABLE_ADVISORY` | true | 気象庁その他情報の有効化 |
| `ENABLE_LONG_PERIOD` | true | 長周期地震動通知の有効化 |
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
| `USGS_NOTIFICATION_COOLDOWN` | 300 | 重複通知防止クールダウン（秒） |

### EEW 設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `WOLFX_HEARTBEAT_TIMEOUT` | 90 | Wolfx heartbeat タイムアウト（秒） |
| `FETCH_FAILURE_THRESHOLD` | 3 | API 連続失敗でエラー通知する回数 |
| `FETCH_BACKOFF_SECONDS` | 60 | API 失敗時のバックオフ待機時間（秒） |

### 音声設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `AQUESTALK_PATH` | （空） | AquesTalkPi の実行ファイルパス（未設定で音声無効） |
| `AQUESTALK_SPEED` | 150 | AquesTalkPi の読み上げ速度 |
| `AUDIO_PLAYER` | aplay | 音声再生コマンド（`aplay` / `mpg123` 等） |
| `SPEECH_QUEUE_MAXSIZE` | 200 | 音声読み上げキューの最大サイズ |
| `MP3_QUEUE_MAXSIZE` | 50 | MP3 再生キューの最大サイズ |

### ログ設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `LOG_LEVEL` | INFO | ログレベル（後方互換。FILE/CONSOLE 未設定時の既定値として使用） |
| `LOG_LEVEL_FILE` | LOG_LEVEL | ファイルへの出力ログレベル |
| `LOG_LEVEL_CONSOLE` | LOG_LEVEL | コンソールへの出力ログレベル |
| `LOG_MAX_BYTES` | 10485760 | ログファイルの最大サイズ（バイト、デフォルト 10MB） |
| `LOG_BACKUP_COUNT` | 7 | ローテーション保持ファイル数 |
| `LOG_DUPLICATE_THRESHOLD` | 60 | 同一メッセージの重複抑制時間（秒）。ERROR 以上は常に出力 |
| `LOG_SUPPRESS_HTTP_SUCCESS` | true | aiohttp.access の 2xx 成功ログを抑制 |

### Web Dashboard 設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `WEB_DASHBOARD_ENABLED` | true | Web Dashboard の有効化 |
| `WEB_DASHBOARD_PORT` | 8080 | Web Dashboard のポート番号 |

### ステータス表示設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `STATUS_SHOW_CPU` | true | !status / /qtl_status で CPU 使用率を表示 |
| `STATUS_SHOW_MEM` | true | !status / /qtl_status でメモリ使用量を表示 |
| `STATUS_SHOW_DISK` | true | !status / /qtl_status でディスク使用率を表示 |
| `STATUS_SHOW_UPTIME` | true | !status / /qtl_status で稼働時間を表示 |

### リソース監視設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `RESOURCE_MONITORING_ENABLED` | true | リソース監視の有効化 |
| `RESOURCE_CHECK_INTERVAL` | 3600 | 監視間隔（秒） |
| `DISK_WARNING_THRESHOLD` | 80 | ディスク WARNING 閾値（%） |
| `DISK_ERROR_THRESHOLD` | 90 | ディスク ERROR 閾値（%） |

---

## Web Dashboard

### GET /status（詳細ステータス JSON）

```bash
curl http://localhost:8080/status | jq
```

**レスポンス例（主要フィールド）**:
```json
{
  "status": "online",
  "timestamp": "2026-06-26T12:00:00.000000",
  "bot_user": "QTL_Bot#1234",
  "uptime": "1日 05時間 30分 00秒",
  "uptime_seconds": 106200,
  "ping_ms": 45,
  "system": {
    "cpu_percent": 3.1,
    "memory_mb": 95.2,
    "memory_total_mb": 8192.0,
    "memory_percent": 1.2,
    "disk_percent": 42.5,
    "disk_free_gb": 27.3
  },
  "eew": {
    "wolfx": {
      "ws_status": "online",
      "heartbeat_elapsed_sec": 12.4,
      "heartbeat_timeout_sec": 90,
      "last_eew_id": "20260626120000",
      "last_recv_time": "2026-06-26T11:59:00.000000",
      "recv_count": 3
    },
    "p2p_eew": { "last_recv_time": null, "recv_count": 0 }
  },
  "monitoring": {
    "quake":          { "last_recv_time": "...", "recv_count": 12 },
    "tsunami":        { "last_recv_time": null,  "recv_count": 0  },
    "long_period":    { "last_recv_time": "...", "recv_count": 2  },
    "tsunami_obs":    { "last_recv_time": null,  "recv_count": 0  },
    "quake_advisory": { "last_recv_time": "...", "recv_count": 5  },
    "volcano": {
      "last_event_id": "20260626_volcano_XX.json",
      "polling_status": "running",
      "last_recv_time": "...",
      "recv_count": 1,
      "total_recv_count": 1
    },
    "usgs": {
      "enabled": true,
      "magnitude_min": 5.0,
      "fetch_interval_sec": 600,
      "region": { "lat": [20, 50], "lon": [120, 180] },
      "last_event_ids": ["us1000abcd"],
      "last_recv_time": "...",
      "recv_count": 2
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
    "volcano_poller": "running",
    "eruption_poller": "running",
    "warning_poller": "running"
  }
}
```

### GET /health/full（API 疎通確認）

```bash
curl http://localhost:8080/health/full | jq
```

```json
{
  "overall_status": "healthy",
  "last_check_time": "2026-06-26T12:00:00+09:00",
  "api_status": {
    "wolfx":  { "ok": true, "latency_ms": 125, "error": null },
    "jma":    { "ok": true, "latency_ms": 340, "error": null },
    "p2p":    { "ok": true, "latency_ms": 280, "error": null }
  }
}
```

### GET /health（軽量ヘルスチェック）

```bash
curl http://localhost:8080/health
# {"status": "online"}
```

---

## Discord コマンド

| コマンド | 種別 | 権限 | 説明 |
|:---|:---|:---|:---|
| `!status` | プレフィックス | 管理者 | Bot 稼働状態を Embed で表示 |
| `/qtl_status` | スラッシュ | 管理者 | `!status` と同じ内容（スラッシュコマンド版） |

表示内容：システムリソース / EEW 状態 / API 受信状況 / タスク稼働状態 / USGS 設定 / フィルター設定

---

## 火山情報の仕様

### 監視対象 API
| 種別 | URL |
|:---|:---|
| 火山情報リスト | `https://www.jma.go.jp/bosai/volcano/data/info.json` |
| 火山情報詳細 | `https://www.jma.go.jp/bosai/volcano/data/{json_filename}` |
| 噴火速報リスト | `https://www.jma.go.jp/bosai/volcano/data/eruption.json` |
| 噴火警報リスト | `https://www.jma.go.jp/bosai/volcano/data/warning.json` |

### 差分検知
`info.json` の先頭エントリの `json` フィールドを前回値と比較し、変化があれば詳細を取得して通知します。

### 警戒レベル別の色
- L1（活火山であることに留意）: 紫
- L2（火口周辺規制）: 赤
- L3（入山規制）: 橙
- L4（居住地域避難準備）: 黄
- L5（居住地域への避難）: 青

---

## 津波情報の仕様

### データソース
- `https://www.jma.go.jp/bosai/tsunami/data/list.json` から最新 JSON を取得
- 種別（タイトル）でルーティング：
  - 観測情報（`津波観測に関する情報` 等）: `notify_tsunami_observation`
  - 予報・警報（`津波予報` / `津波警報` / `大津波警報` 等）: `notify_tsunami_forecast`

### 予想高さの表示フォーマット
```
■ 大津波警報
予想高さ 10m以上
　北海道太平洋沿岸東部
予想高さ 5m
　北海道太平洋沿岸西部
■ 津波警報
予想高さ 3m
　宮城県
```

### 警報コードと色
| コード | 種別 | 色 |
|:---|:---|:---|
| 52/53 | 大津波警報 | 紫 (#C800FF) |
| 51 | 津波警報 | 赤 (#FF2800) |
| 62 | 津波注意報 | 黄 (#FAF500) |
| 71/72/73 | 津波予報 | 水色 (#80FFFF) |
| 50/00/60 | 解除・なし | 緑 |

---

## ログ管理

```bash
# .env に追加
LOG_LEVEL_FILE=DEBUG     # ファイルには詳細を残す
LOG_LEVEL_CONSOLE=INFO   # コンソールは INFO 以上のみ
```

同一内容のログは `LOG_DUPLICATE_THRESHOLD`（デフォルト 60秒）以内なら出力しません。ERROR・CRITICAL は常に出力されます。

---

## トラブルシューティング

### 火山情報が通知されない
1. `VOLCANO_CHANNEL_ID` が正しく設定されているか確認
2. ログで差分検知の状態を確認
   ```bash
   tail -f qtlbot.log | grep -i volcano
   # "Volcano: no change" → 変化なし（正常）
   ```
3. Dashboard で確認
   ```bash
   curl http://localhost:8080/status | jq '.monitoring.volcano'
   ```

### EEW が届かない
```bash
curl http://localhost:8080/status | jq '.eew'
# wolfx の ws_status が timeout → Wolfx WebSocket の再接続待ち
# p2p_eew の recv_count が増えていれば P2P EEW は正常動作中
```

### タスクが停止している
```bash
curl http://localhost:8080/status | jq '.tasks'
# "[NG] エラー停止" → Bot を再起動してください
```

### USGS 地震情報が届かない
```bash
curl http://localhost:8080/status | jq '.monitoring.usgs'
# enabled が false → USGS_ENABLED=true を設定
```

---

## コード構成
```
bot.py
└── QuakeTsunamiCog
    ├── connect_eew_ws()             - Wolfx EEW WebSocket 接続
    ├── connect_p2p_eew_ws()         - P2P EEW WebSocket（緊急地震速報（警報）専用・常時稼働）
    ├── fetch_quake()                - 地震情報ポーリング（P2P）
    ├── fetch_tsunami()              - 津波情報ポーリング
    ├── fetch_tsunami_observation()  - 津波観測/予報情報ポーリング（JMA）
    ├── fetch_long_period()          - 長周期地震動ポーリング
    ├── fetch_quake_advisory()       - 気象庁その他情報ポーリング
    ├── fetch_usgs_quake()           - USGS ポーリング
    ├── fetch_volcano_info()         - 火山情報ポーリング
    ├── fetch_eruption_info()        - 噴火速報ポーリング（VFVO50）
    ├── fetch_warning_info()         - 噴火警報ポーリング（VFVO53）
    ├── vibration_monitor_loop()     - EEW 発生時の強震モニタ監視
    ├── speech_worker()              - AquesTalkPi 音声再生ワーカー
    ├── mp3_worker()                 - MP3 再生ワーカー
    ├── start_web_dashboard()        - Web Dashboard（aiohttp）
    ├── _build_status_embed()        - !status / /qtl_status 共通 Embed 生成
    └── notify_*()                   - 各通知関数
```

---

## ライセンス
MIT License

## 謝辞
- 気象庁（JMA）API
- Wolfx EEW 配信サービス
- P2P 地震情報
- 米国地質調査所（USGS）

---

**最終更新**: 2026-06-27
**対応 Python**: 3.11+