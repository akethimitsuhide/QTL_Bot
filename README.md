# QTL_Bot - Discord 地震・津波・火山情報通知 Bot

気象庁（JMA）の API と複数のデータソースを使用して、地震・津波・火山情報を Discord に自動通知する Bot です。主に Raspberry Pi 5 での使用を想定しています。

---

## 機能一覧

### 地震情報通知
- **EEW（緊急地震速報）**: Wolfx WebSocket でリアルタイム受信
  - 予想される最大震度、各地の予想震度（震度4以上の場合）、推奨行動等を通知
  - 音声読み上げ対応（AquesTalkPi）
- **P2P EEW（緊急地震速報（警報）専用）**: P2P 地震情報の WebSocket から警報のみを常時受信
  - Wolfx と同時並行稼働（EventID による重複排除あり）
- **地震情報**: P2P 地震情報の API からの確報情報

### 津波情報
- 気象庁 HP の JSON から自動取得（大津波警報 / 津波警報 / 津波注意報 / 津波予報）
- 警報種別・予想高さ別のエリア一覧表示
- 本文（`Body.Text`）・解説（`Body.Comments.FreeFormComment`）を通知下部に追記
- 津波観測情報（`VTSE41/51`）を別関数で処理

### 火山情報
- 気象庁 HP の JSON から自動取得（1分ごとのポーリング）
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
- `RotatingFileHandler` による自動ログローテーション（デフォルト: 10MB × 7世代）
- ファイル / コンソール独立ログレベル設定（`LOG_LEVEL_FILE` / `LOG_LEVEL_CONSOLE`）
- 重複ログ抑制: 同一メッセージを指定秒数以内は出力抑制（ERROR 以上は常に出力）

### リソース監視
- 1時間ごとに CPU・メモリ・ディスク使用率を記録
- ディスク使用率が 80% 以上で WARNING、90% 以上で ERROR を記録

### APM (Mackerel 連携)
- OpenTelemetry (OTLP) 経由で Mackerel にトレース情報を送信するオプション機能
- **デフォルトは無効**（`APM_ENABLED=false`）。Mackerel 等で Bot を監視したい運用者向け
- 有効化すると aiohttp クライアント（JMA/USGS/P2P 等への全 HTTP リクエスト）が自動計装され、レイテンシ・失敗状況を可視化できる
- 必要パッケージは `requirements.txt` の "APM (Mackerel 連携)" セクションを参照（デフォルト無効時は未インストールでも動作に影響しない）
- `!status` / `/qtl_status` で現在の稼働状況を確認可能
- ⚠️ 実際の OTLP エンドポイント URL・API キーのヘッダー名は [Mackerel 公式ドキュメント](https://mackerel.io/ja/docs/entry/tracing/installations/python) で必ず確認してください。`.env` の `APM_OTLP_ENDPOINT` / `APM_OTLP_API_KEY_HEADER` はデフォルト値のままだと正しく送信できない可能性があります

### 長周期地震動
- 長周期地震動の観測情報を通知

### 長周期地震動モニタ
- EEW 発表時に強震モニタ画像（`jma_s` 系統）・長周期地震動モニタ画像（`abrspmx_s` 系統）・振動レベルを通知（2秒間隔で更新）
- 通知の色は `jma_s` 系統の画像から推定した実震度に基づく独自カラーマップで決定（強震モニタ画像解析検知と共通仕様）
- 振動レベルに応じた音声アラート（該当レベルの間、2秒間隔で継続再生）
  - レベル 100〜999: `lv100.mp3`
  - レベル 1000〜1999: `lv1000.mp3`
  - レベル 2000 以上: `lv2000.mp3`
  - レベルが下降し tier が変わった場合は新しい tier の MP3 に切り替わる（100未満は無音）

### 強震モニタ画像解析（画像解析検知）
- 強震モニタ画像（`jma_s` 系統）をHSVマスク処理・グリッド分割・クラスタリング・複数フレーム検証の4段階パイプラインで解析し、数値APIを使わず画像のみから揺れを検知する独立機能（`KyoshinMonitorCog`）
- 画像の時刻決定は `latest.json` API（実際に配信されている最新時刻）を優先取得し、失敗時のみ従来のリトライ探索方式にフォールバック
- 通知に必要な最小検出観測点数は実震度によって切り替え（震度0相当は4件以上、震度1相当以上は2件以上を要求。誤検知抑制のため）
- 通知には `jma_s` 系統・`abrspmx_s` 系統の両画像と振動レベルを含める（検出観測点数そのものは通知本文には表示しない）
- 通知の色は `jma_s` 系統の実震度に基づく独自カラーマップで決定（EEW発表時の強震モニタ通知と共通仕様）
- Pillow（PIL）が未インストールの場合は自動的に機能をスキップする

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
- Pillow（強震モニタ画像解析検知機能。未インストール時は当該機能のみ自動スキップ）
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

全環境変数の詳細は README 下部の「環境変数リファレンス」を参照してください。

#### 3. チャンネル設定
Bot が通知を送信するテキストチャンネルを作成し、ID を `.env` に設定：
- `EEW_CHANNEL_ID` : EEW（緊急地震速報）
- `P2P_EEW_CHANNEL_ID` : P2P EEW（緊急地震速報（警報））
- `QUAKE_CHANNEL_ID` : 地震情報
- `TSUNAMI_CHANNEL_ID` : 津波警報
- `VOLCANO_CHANNEL_ID` : 火山情報
- `USGS_CHANNEL_ID` : USGS 海外地震情報（未設定時は `QUAKE_CHANNEL_ID`）
- `KYOSHIN_CHANNEL_ID` : 強震モニタ画像解析検知（未設定時は `OTHER_CHANNEL_ID`）

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

### 強震モニタ画像解析（Kyoshin）設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `ENABLE_KYOSHIN` | true | 強震モニタ画像解析検知機能の有効化 |
| `KYOSHIN_GRID_SIZE` | 10 | 画像を何 px 四方の疑似観測点セルに分割するか |
| `KYOSHIN_IMAGE_DELAY_SEC` | 6 | `latest.json` 取得失敗時のフォールバック探索で遡る基準秒数 |
| `KYOSHIN_IMAGE_STEP_SEC` | 3 | フォールバック探索で画像が見つからない場合に遡るステップ幅（秒） |
| `KYOSHIN_IMAGE_MAX_RETRY` | 4 | フォールバック探索の最大リトライ回数 |
| `KYOSHIN_POLL_INTERVAL_SEC` | 2.0 | 観測値取り込み〜イベント判定のポーリング間隔（秒） |
| `KYOSHIN_NOTIFY_INTERVAL_SEC` | 2.0 | イベント継続中の通知再送間隔（秒） |
| `KYOSHIN_MIN_CLUSTER_SIZE` | 3 | クラスタとして認める最小メンバー（セル）数 |
| `KYOSHIN_REQUIRED_FRAMES` | 2 | クラスタを確定（confirmed）とみなすために必要な連続フレーム数 |
| `KYOSHIN_MIN_ACTIVE_PIXELS` | 2 | 1セル内でアクティブとみなす最小の揺れ候補ピクセル数 |
| `KYOSHIN_MIN_NOTIFY_PHASE` | Weaker | 通知を送信する最小フェーズ（Weaker &lt; Weak &lt; Medium &lt; Strong &lt; Stronger） |
| `KYOSHIN_MIN_STATIONS_SHINDO0` | 4 | 実震度が震度0相当（1.0未満）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_MIN_STATIONS_SHINDO1` | 2 | 実震度が震度1相当以上（1.0以上）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_DEBUG_SAVE_IMAGE` | false | confirmed 判定時の元画像をローカル保存するか（事後検証用） |
| `KYOSHIN_DEBUG_IMAGE_DIR` | ./kyoshin_debug_images | デバッグ画像の保存先ディレクトリ |

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
| `WEB_DASHBOARD_ENABLED` | false | Web Dashboard の有効化 |
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

### APM (Mackerel 連携) 設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `APM_ENABLED` | false | APM（トレーシング）連携の有効化。デフォルト無効 |
| `APM_SERVICE_NAME` | QTL_Bot | Mackerel 上で表示されるサービス名 |
| `APM_MACKEREL_API_KEY` | （空） | Mackerel の API キー。`APM_ENABLED=true` 時は必須 |
| `APM_OTLP_ENDPOINT` | `https://otlp-vmagent.mackerelio.com` | OTLP 送信先エンドポイント。**公式ドキュメントで要確認** |
| `APM_OTLP_API_KEY_HEADER` | `Mackerel-Api-Key` | API キーを送る HTTP ヘッダー名。**公式ドキュメントで要確認** |

> ⚠️ **注意**: `APM_OTLP_ENDPOINT` と `APM_OTLP_API_KEY_HEADER` のデフォルト値は、実装時に
> [Mackerel 公式ドキュメント](https://mackerel.io/ja/docs/entry/tracing/installations/python)
> へのアクセスができなかったため、一般的な OpenTelemetry OTLP の慣例に基づく暫定値です。
> `APM_ENABLED=true` にする前に、必ず公式ドキュメントで実際の値を確認し、
> 異なる場合は `.env` で上書きしてください。

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
- `https://www.jma.go.jp/bosai/tsunami/data/list.json`（気象庁 HP の JSON）から取得
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

### 強震モニタ画像解析検知（Kyoshin）が通知されない
1. `ENABLE_KYOSHIN=true` になっているか、Pillow がインストールされているか確認
   ```bash
   pip list | grep -i pillow
   ```
2. ログで検知パイプラインの状態を確認
   ```bash
   tail -f qtlbot.log | grep -i kyoshin
   # "クラスタを確定(confirmed)しました" → 検知自体は成功している
   # 検知後に通知が来ない場合は KYOSHIN_MIN_STATIONS_SHINDO0 / SHINDO1 の閾値を確認
   ```
3. `KYOSHIN_DEBUG_SAVE_IMAGE=true` にして `KYOSHIN_DEBUG_IMAGE_DIR` に保存された画像で誤検知・未検知の状況を事後確認

---

## コード構成

### ディレクトリ構成
```
QTL_Bot/
├── bot.py                  - エントリーポイント（Cog 登録・起動のみ）
├── cogs/
│   ├── apm.py                - ApmCog: Mackerel APM 連携（OpenTelemetry OTLP）
│   ├── quake.py              - QuakeEewCog: 地震・EEW・P2P EEW
│   ├── tsunami.py            - TsunamiCog: 津波観測・予報
│   ├── volcano.py            - VolcanoCog: 火山情報・噴火速報・噴火警報
│   ├── usgs.py               - UsgsCog: USGS 海外地震情報
│   ├── other.py              - OtherInfoCog: 長周期地震動・気象庁その他情報
│   ├── system.py             - SystemCog: !status・Web Dashboard・エラー監視・リソース監視
│   └── kyoshin_monitor.py    - KyoshinMonitorCog: 強震モニタ画像解析による揺れ検知
└── core/
    ├── config.py                  - 環境変数読み込み・定数定義
    ├── logging_setup.py           - ログ設定（RotatingFileHandler・重複抑制）
    ├── kyoshin_shared.py          - 震度色分け・両画像取得・振動レベル取得の共通ロジック
    │                                 （EEW発表時通知・画像解析検知通知の両方から利用）
    ├── kyoshin_image_analyzer.py  - HSVマスク処理による画像→震度グリッド変換
    ├── kyoshin_cluster_tracker.py - 検出グリッドセルのクラスタリング・複数フレーム検証
    └── kyoshin_detector.py        - 揺れ検知イベントのライフサイクル管理（状態機械）
```

### Cog 責務一覧
| Cog | ファイル | 主な責務 |
|:---|:---|:---|
| `ApmCog` | `cogs/apm.py` | OpenTelemetry 計装・Mackerel OTLP 送信（デフォルト無効） |
| `QuakeEewCog` | `cogs/quake.py` | Wolfx WebSocket（EEW）・P2P WebSocket（EEW 警報）・P2P API（地震速報）・EEW発表時の強震モニタ通知 |
| `TsunamiCog` | `cogs/tsunami.py` | JMA 津波 API ポーリング・観測情報・予報 / 警報通知 |
| `VolcanoCog` | `cogs/volcano.py` | JMA 火山 API ポーリング・噴火速報・噴火警報 |
| `UsgsCog` | `cogs/usgs.py` | USGS API ポーリング・海外地震フィルタリング・通知 |
| `OtherInfoCog` | `cogs/other.py` | 長周期地震動・気象庁その他情報 |
| `SystemCog` | `cogs/system.py` | Web Dashboard・`!status`・エラー自動通知・リソース監視 |
| `KyoshinMonitorCog` | `cogs/kyoshin_monitor.py` | 強震モニタ画像の解析による揺れ検知・通知（Pillow が必要） |

### 主要関数
| 関数 | 説明 |
|:---|:---|
| `connect_eew_ws()` | Wolfx EEW WebSocket 接続 |
| `connect_p2p_eew_ws()` | P2P EEW WebSocket（緊急地震速報（警報）専用・常時稼働） |
| `fetch_quake()` | 地震情報ポーリング（P2P） |
| `fetch_tsunami()` | 津波情報ポーリング |
| `fetch_tsunami_observation()` | 津波観測 / 予報情報ポーリング（JMA） |
| `fetch_long_period()` | 長周期地震動ポーリング |
| `fetch_quake_advisory()` | 気象庁その他情報ポーリング |
| `fetch_usgs_quake()` | USGS ポーリング |
| `fetch_volcano_info()` | 火山情報ポーリング |
| `fetch_eruption_info()` | 噴火速報ポーリング（VFVO50） |
| `fetch_warning_info()` | 噴火警報ポーリング（VFVO53） |
| `vibration_monitor_loop()` | EEW 発生時の強震モニタ監視（`jma_s`・`abrspmx_s` 両画像＋振動レベル、2秒間隔） |
| `speech_worker()` | AquesTalkPi 音声再生ワーカー |
| `mp3_worker()` | MP3 再生ワーカー |
| `start_web_dashboard()` | Web Dashboard（aiohttp） |
| `_build_status_embed()` | !status / /qtl_status 共通 Embed 生成 |
| `notify_*()` | 各通知関数 |
| `KyoshinImageAnalyzer.analyze()` | 強震モニタ画像をグリッド分割し、HSVマスクで揺れ候補セルを抽出 |
| `ClusterTracker.update()` | アクティブセルを連結成分クラスタリングし、複数フレーム検証で confirmed 判定 |
| `EventManager.ingest_confirmed()` / `tick()` | 揺れ検知イベントの生成・更新・終了（状態機械） |
| `shindo_to_color()` | 実震度から独自カラーマップに基づく通知色を決定（`core/kyoshin_shared.py`） |
| `estimate_max_shindo_from_image()` | `jma_s` 画像から画面内の最大実震度を推定（`core/kyoshin_shared.py`） |

---

## ライセンス
MIT License

## 謝辞
- 気象庁（JMA）API
- Wolfx EEW 配信サービス
- P2P 地震情報
- 米国地質調査所（USGS）

---

**最終更新**: 2026-07-20
**対応 Python**: 3.11+
