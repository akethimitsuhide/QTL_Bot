# QTL_Bot - Discord 地震・津波・火山情報通知 Bot

気象庁（JMA）の API と複数のデータソースを使用して、地震・津波・火山情報を Discord に自動通知する Bot です。Raspberry Pi での常時運用を想定した軽量実装。

---

## 機能一覧

### 🔴 地震情報通知
- **EEW（緊急地震速報）**: Wolfx WebSocket で リアルタイム受信
  - 予測震度、到達時間、推奨行動を通知
  - 音声読み上げ対応
- **P2P 地震情報**: P2P 地震情報 API からの補完データソース
- **地震速報**: JMA API からの確報情報

### 🌊 津波警報
- JMA 津波警報 API から自動取得
- 警報種別別の色分け（警報 / 注意報 / 予報）
- 沿岸地域の詳細情報

### 🌋 火山情報（新機能）
- JMA 火山情報 API からのリアルタイム監視
- 噴火警戒レベル L1-L5 の色分け表示
- **警戒レベル変化検知**: 同じ eventId でも警戒レベルが変われば通知

### ⚠️ エラー自動通知機能（A-4）
- 重大エラー発生時に管理者チャンネルに自動通知
- **重複防止**: 同じエラーは 1 時間に 1 回のみ通知
- **日次サマリー**: 毎日 00:00 に前日のエラー集計を通知
- 管理者チャンネルは `ADMIN_CHANNEL_ID` で設定

### 🔍 ヘルスチェック機能（A-1）
- Wolfx WebSocket, JMA API, P2P 地震情報の疎通確認
- `/health/full` エンドポイントで詳細情報を取得
- 結果は 30 秒間キャッシュ（過度な API 呼び出しを防止）
- 詳細情報の通知：
  - 火山活動の状況
  - 予防措置（注意事項）
  - 次回発表予定
- 火山情報専用チャンネル設定可能
- 1分ごとのポーリング監視

### 📊 長周期地震動
- 長周期地震動の観測情報
- リアルタイム強震モニタ画像

### 📡 API 統合
- **Wolfx**: EEW リアルタイム配信（WebSocket）
- **JMA**: 地震・津波・火山情報（REST API）
- **P2P 地震情報**: 補完データソース

### 🔧 Web Dashboard
- HTTP ローカル API `/status` で稼働状況確認
- 各 API の受信状況、受信回数、メモリ使用量を JSON で取得

---

## セットアップ

### 必須環境
- Python 3.11+
- discord.py 2.0+
- aiohttp（非同期 HTTP 通信）
- psutil（オプション：メモリ監視）

### インストール
```bash
# リポジトリクローン
git clone https://github.com/yourusername/qtl-bot.git
cd qtl-bot

# 依存ライブラリをインストール
pip install -r requirements.txt

# AquesTalkPi インストール（オプション：音声読み上げ）
sudo apt-get install aquestalk-pi
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
`.env` ファイルを作成（`.env.example` をコピー）：
```bash
cp .env.example .env
```

`.env` の必須設定：
```bash
DISCORD_TOKEN=your_bot_token_here
CHANNEL_ID=default_notification_channel_id

# オプション：各通知種別のチャンネルを分離
EEW_CHANNEL_ID=eew_channel_id
QUAKE_CHANNEL_ID=quake_channel_id
TSUNAMI_CHANNEL_ID=tsunami_channel_id
VOLCANO_CHANNEL_ID=volcano_channel_id
```

#### 3. チャンネル作成（Discord サーバー）
Bot が通知を送信するテキストチャンネルを作成：
- `#地震` （EEW・地震情報）
- `#津波` （津波警報）
- `#火山情報` （火山情報）

**チャンネル ID の確認方法**:
1. Discord で チャンネルを右クリック → "チャンネルをコピー"
2. または詳細メニューから ID をコピー

#### 4. Bot の起動
```bash
python bot.py
```

ログに以下が出力されれば正常：
```
[INFO] ✅ ログイン完了: BotName#1234
[INFO] EEW WebSocket 接続開始
[INFO] Volcano polling started (every 1 minute)
[INFO] Web ダッシュボード起動: http://localhost:8101/status
```

---

## 環境変数リファレンス

### Discord 設定
| 変数名 | 必須 | 説明 |
|:---|:---|:---|
| `DISCORD_TOKEN` | ✅ | Discord Bot のトークン |
| `CHANNEL_ID` | ✅ | デフォルト通知チャンネル ID |
| `EEW_CHANNEL_ID` | - | EEW 専用チャンネル（未設定時は CHANNEL_ID） |
| `QUAKE_CHANNEL_ID` | - | 地震情報専用チャンネル |
| `TSUNAMI_CHANNEL_ID` | - | 津波警報専用チャンネル |
| `VOLCANO_CHANNEL_ID` | - | 火山情報専用チャンネル |
| `OTHER_CHANNEL_ID` | - | その他情報（長周期地震動等） |

### API・システム設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `WOLFX_HEARTBEAT_TIMEOUT` | 90 | Wolfx WebSocket ハートビート タイムアウト（秒） |
| `EEW_FALLBACK_TIMEOUT` | 30 | EEW フォールバック切り替え時間（秒） |
| `WEB_DASHBOARD_ENABLED` | true | Web Dashboard の有効化 |
| `WEB_DASHBOARD_PORT` | 8101 | Web Dashboard のポート |
| `AQUESTALK_PATH` | /usr/bin/aquestalk | AquesTalkPi の実行ファイルパス |
| `MP3_DIR` | ./mp3 | MP3 ファイルの格納ディレクトリ |

---

## Web Dashboard

### /status エンドポイント（JSON API）

稼働状況を JSON で取得：
```bash
curl http://localhost:8101/status | jq
```

### /health/full エンドポイント（ヘルスチェック）

各 API（Wolfx, JMA, P2P）の疎通確認：
```bash
curl http://localhost:8101/health/full | jq
```

**レスポンス例**:
```json
{
  "overall_status": "healthy",
  "last_check_time": "2026-06-05T10:32:15+09:00",
  "api_status": {
    "wolfx": {"ok": true, "latency_ms": 125, "error": null},
    "jma": {"ok": true, "latency_ms": 340, "error": null},
    "p2p": {"ok": true, "latency_ms": 280, "error": null}
  }
}
```

### Slash Command での確認（推奨）

Discord 上で直接実行：
```
/status      - Bot 稼働状態を表示（embed 形式）
/volcano     - 火山情報を手動更新
```

**レスポンス例**:
```json
{
  "status": "online",
  "bot_user": "QTL_Bot#1234",
  "uptime": "1日 05:30:45",
  "uptime_seconds": 103845,
  "api_status": {
    "wolfx": "2026-06-04T10:59:30+09:00",
    "quake": "2026-06-04T10:55:12+09:00",
    "tsunami": null,
    "volcano": "2026-06-04T10:49:55+09:00"
  },
  "recv_count": {
    "wolfx": 12,
    "p2p_eew": 0,
    "quake": 3,
    "tsunami": 0,
    "volcano": 1
  },
  "volcano_monitoring": {
    "last_event_id": 506,
    "last_recv_time": "2026-06-04T10:49:55.123456+09:00",
    "polling_status": "active",
    "total_recv_count": 1
  },
  "memory_usage_mb": 85.4
}
```

### /health エンドポイント

ヘルスチェック（軽量）：
```bash
curl http://localhost:8101/health
# {"status": "online"}
```

---

## 火山情報機能の詳細

### 監視対象
- JMA 火山情報 API
- URL: `https://www.jma.go.jp/bosai/volcano/data/info.json`

### ポーリング間隔
- **1 分ごと**に最新情報を自動確認
- 差分検知ベース（eventId の変化で新規判定）

### 通知フォーマット
Discord embed で以下を表示：
- 火山名
- 警戒レベル（L1-L5）
- 発表機関・時刻
- 火山活動の状況
- 予防措置
- 次回発表予定

**警戒レベル別の色**:
- L1（警戒が必要）: 紫
- L2（火口周辺警報）: 赤
- L3（入山規制）: 橙
- L4（居住地域警報）: 黄
- L5（予報注視）: 青

### チャンネル設定
```bash
# 専用チャンネルを設定
VOLCANO_CHANNEL_ID=your_volcano_channel_id

# 未設定の場合は CHANNEL_ID を使用（後方互換）
# または「火山」という名前のチャンネルを自動探索
```

### Dashboard での監視
`/status` で火山監視状況を確認：
```json
"volcano_monitoring": {
  "last_event_id": 506,          // 最新 eventId
  "last_recv_time": "...",       // 最後の受信時刻
  "polling_status": "active",    // ポーリング稼働状態
  "total_recv_count": 5          // 受信回数
}
```

---

## 運用上のポイント

### ログレベル調整
```bash
# .env に追加（デフォルト: INFO）
LOG_LEVEL=DEBUG  # 詳細ログ
LOG_LEVEL=WARNING  # 警告とエラーのみ
```

### メモリ使用量監視
- Raspberry Pi での常時運用を想定
- Web Dashboard で定期的にメモリ使用量を確認
- 目安: 80-100 MB（安定稼働時）

### ネットワーク接続確認
```bash
# EEW WebSocket 接続状態確認
curl http://localhost:8101/status | jq '.api_status.wolfx'

# 火山情報の受信状況確認
curl http://localhost:8101/status | jq '.volcano_monitoring'
```

---

## トラブルシューティング

### 火山情報が通知されない
1. **チャンネルを確認**
   - `VOLCANO_CHANNEL_ID` が正しく設定されているか
   - チャンネル名に「火山」を含むか
   - Bot に送信権限があるか

2. **ログを確認**
   ```bash
   tail -f qtlbot.log | grep -i volcano
   ```

3. **Dashboard で状況確認**
   ```bash
   curl http://localhost:8101/status | jq '.volcano_monitoring'
   ```

### メモリリーク
- 定期的に Bot を再起動（例：cron で日次）
- メモリ使用量が 200 MB を超える場合は要調査

### API 接続エラー
```
[ERROR] Volcano fetch error: ClientConnectorError
```
- ネットワーク接続を確認
- JMA API の稼働状況を確認（https://www.jma.go.jp/）
- タイムアウト値を調整（.env で WOLFX_HEARTBEAT_TIMEOUT など）

---

## 開発・カスタマイズ

### コード構成
```
bot.py
├── QuakeTsunamiCog
│   ├── connect_eew_ws() - EEW WebSocket 接続
│   ├── fetch_quake_advisory() - 地震速報取得
│   ├── fetch_tsunami_advisory() - 津波警報取得
│   ├── fetch_volcano_info() - 火山情報取得 ← 新機能
│   ├── start_web_dashboard() - Web Dashboard
│   └── 各メッセージハンドラ
```

### 新機能の追加
気象警報など新しい情報源を追加する場合：
1. `fetch_*` メソッドを追加
2. `_last_recv` と `_recv_count` に キーを追加
3. `/status` レスポンスに情報を追加
4. ポーリングループで呼び出し

---

## ライセンス
MIT License

## 謝辞
- JMA（気象庁）API
- Wolfx EEW 配信サービス
- P2P 地震情報

---

**最終更新**: 2026-06-04  
**対応 Python**: 3.11+  
**対応 discord.py**: 2.0+