# QTL_Bot - Discord 地震・津波・火山情報通知 Bot

気象庁（JMA）の API と複数のデータソースを使用して、地震・津波・火山情報を Discord に自動通知する Bot です。主に Raspberry Pi 5 での使用を想定しています。

---

## 機能一覧

### 地震情報通知
- **EEW（緊急地震速報）**: Wolfx WebSocket でリアルタイム受信
  - 予想される最大震度、各地の予想震度（震度4以上の場合）、推奨行動等を通知
  - 音声読み上げ対応（AquesTalkPi）
  - 警報対象の府県予報区数（`region_map.json` 変換後）が `EEW_REGION_COLLAPSE_THRESHOLD`（デフォルト10）件以上の場合、`pref_region_map.json` でさらに地方予報区名へ集約して通知・読み上げる（例:「北海道道南,北海道道央,...,千葉」14件 → 「北海道,東北,関東」）
- **P2P EEW（緊急地震速報（警報）専用）**: P2P 地震情報の WebSocket から警報のみを常時受信
  - Wolfx と同時並行稼働（EventID による重複排除あり）
- **地震情報**: P2P 地震情報の API からの確報情報
  - 震度速報（`ScalePrompt`）: 読み上げは `prefecture_map.json` で区域名を都道府県名に変換し、重複を排除して発表（例:「熊本県天草・芦北」「熊本県熊本」→「熊本県」1回のみ）。通知本文の津波記述の下に区域別の震度一覧（■ 震度○ + 区域名）を追記
  - 震源に関する情報（`Destination`）: 震源地の横に度分秒形式の緯度経度を常に付記（例:「熊本県天草・芦北地方（32°33′39.8、130°22′43.2）」）
  - 各地の震度に関する情報（`ScaleAndDestination` / `DetailScale`）: 最大震度3以上の場合、読み上げに最大震度を観測した地点名を追加。最大震度〜1階級下までの観測点一覧を追記（震度46＝推定5弱以上は45と同階級として扱う）。1階級下の観測点数が `QUAKE_INTENSITY_COLLAPSE_THRESHOLD`（デフォルト10）件以上の場合、通知文の肥大化を防ぐため都道府県ごとに1地点だけを代表として表示し「（以下略）」を付ける
  - 地図画像: メッセージ送信後にバックグラウンドで P2P 地震情報 CDN をポーリングし、画像が実際に取得可能になった時点で Embed に画像を追加（詳細は下記「P2P 地図画像の添付」参照）
  - 津波の有無（`domesticTsunami`）の表記: P2P 地震情報の `domesticTsunami` は速報段階の推定値であり、`Warning` が返っても実際に気象庁から「津波警報」が正式発表されているとは限らない。誤解を招く断定的な表記を避けるため、`Watch`・`Warning` は通知本文で「津波警報・注意報を発表中」、読み上げで「現在、津波予報等を発表中です。」とまとめて表現する。`NonEffective` は通知「若干の海面変動（被害の心配なし）」、読み上げ「この地震で、若干の海面変動があるかもしれませんが、被害の心配はありません。」。`None` は読み上げで「この地震による津波の心配はありません。」を明示的に付加する（`MajorWarning` は従来通り「大津波警報」）

### EWS（緊急警報放送）信号音
- 津波警報・大津波警報（`domesticTsunami` が `Warning` または `MajorWarning`。津波注意報・津波予報は対象外）が発表・更新された P2P 地震情報を受信した際に、昭和60年郵政省告示第405号に準拠した AFSK 方式の緊急警報信号音（第二種開始信号）を生成・再生する
- 信号音は `core/ews_signal.py` でビット列組み立て・PCM波形合成を行い、外部 WAV ファイルへは一切出力しない。生成した PCM バイト列はメモリ上のまま `core/audio.py` の `play_ews_pcm`（`pygame.mixer.Sound(buffer=...)`）に渡して直接再生する
- `pygame.mixer.Sound(buffer=...)` はリサンプリングを行わず、ミキサー初期化時のサンプルレート・チャンネル数をそのままバッファの解釈に使うため、`core/audio.py` の `pygame.mixer.init()` は `core/ews_signal.py` の生成条件（44100Hz・モノラル）に明示的に合わせて初期化している（不一致のまま再生するとピッチ・再生速度が変わってしまう。既存の `pygame.mixer.music`（MP3再生）はファイル再生時に自動リサンプリングされるためこの制約を受けない）
- `EWS_ENABLE=true` で有効化（デフォルト `false`。オプトイン機能）。地域符号・送信ブロック数・前置後置固定音の長さは環境変数で設定可能（下記「EWS 設定」参照）
- CLI テスト実行時（`is_test=True`）は実際の警報ではないため対象外

### P2P 地図画像の添付
- 地震情報・津波情報の通知には、P2P 地震情報 CDN が生成する震源地図画像を Embed に添付する（`core/p2p_image.py` の `P2PImageMixin`。`QuakeInfoCog` / `TsunamiCog` が多重継承）
- CDN 側の画像生成には数秒〜数十秒のタイムラグがあるため、通知メッセージ送信後にバックグラウンドで最大約2分間（20回 × 6秒間隔）ポーリングし、画像が実際に利用可能になった時点でメッセージを編集して画像を追加する
- 判定は HTTP ステータス 200 だけでなく、レスポンスボディが PNG として妥当か（マジックバイト `\x89PNG\r\n\x1a\n`・最小サイズ `MIN_VALID_IMAGE_BYTES`=1024 バイト）まで検証し、CDN が「200 は返すが実体は生成中」の状態を誤って成功と判定しないようにしている
- `P2P_IMAGE_ATTACH_ENABLED=false` にすると、この画像添付処理（CDN ポーリング）自体を無効化し、通知本文に画像 URL をテキストとして含める簡易方式に切り替わる（Discord のリンクプレビュー機能により自動展開。原因切り分け用）

### 津波情報
- 気象庁 HP の JSON から自動取得（大津波警報 / 津波警報 / 津波注意報 / 津波予報）
- 警報種別・予想高さ別のエリア一覧表示
- 読み上げは「（地域名）に（警報種別）が発表されました」の形式（例:「有明・八代海に津波注意報が発表されました」「宮城県に大津波警報が発表されました」）。複数地域該当時は最重要度（大津波警報 > 津波警報 > 津波注意報）の地域を優先し、4件以上は代表3件＋「等」で丸める
- 本文（`Body.Text`）・解説（`Body.Comments.FreeFormComment`）を通知下部に追記
- 津波観測情報（`VTSE41/51`）を別関数で処理
- 地震情報通知と同じ地図画像添付方式（下記「P2P 地図画像の添付」参照）を使用

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
- 強震モニタ画像（`jma_s` 系統）をHSVマスク処理・グリッド分割で解析し、数値APIを使わず画像のみから揺れを検知する独立機能（`KyoshinMonitorCog`）
- 検知は「基準値（過去10〜25秒平均）との差分による上昇トリガー」＋「8近傍のうち一定数以上が同時に上昇トリガー成立」の空間クロスバリデーション方式（ingen084氏の記事の実装方針を採用）。震度の絶対値のみによる無条件判定は行わない
- イベントの生死は「最後に本物の上昇トリガーが立ってから `KYOSHIN_EVENT_TIMEOUT_SEC` 秒経過したか」の1点のみで判定（複数条件を組み合わせない単純な状態機械）
- 周囲が無反応のまま単独でフラット（変化なし）かつ高震度が続く観測点は機器異常とみなし自動的にブラックリスト化し、以後の判定から除外する
- 画像の時刻決定は `latest.json` API（実際に配信されている最新時刻）を優先取得し、失敗時のみ従来のリトライ探索方式にフォールバック
- 通知には `jma_s` 系統・`abrspmx_s` 系統の両画像と振動レベルを含める
- 通知の色は `jma_s` 系統の実震度に基づく独自カラーマップで決定（EEW発表時の強震モニタ通知と共通仕様）
- Pillow（PIL）が未インストールの場合は自動的に機能をスキップする

### Web Dashboard / コマンド
- `GET /status` で詳細な稼働状況を JSON で取得
- `GET /status/history` でシステムリソース・受信件数の推移スナップショット履歴を JSON で取得（`STATUS_HISTORY_INTERVAL` 秒ごとに記録、メモリ上のリングバッファのみで保持しBot再起動でリセットされる）
- `GET /dashboard` で上記履歴を Chart.js によるグラフとして表示する HTML ページ（CPU/メモリ/ディスク使用率・各種受信件数の推移）
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
| `QUAKE_INTENSITY_COLLAPSE_THRESHOLD` | 10 | 各地の震度に関する情報で、最大震度より1階級小さい震度の観測点数がこの件数以上の場合、都道府県ごとに1地点だけを表示して「（以下略）」を付ける |
| `EEW_REGION_COLLAPSE_THRESHOLD` | 10 | EEWの警報対象府県予報区数がこの件数以上の場合、`pref_region_map.json` で地方予報区へ集約して通知・読み上げる |
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

### P2P 地図画像設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `P2P_IMAGE_ATTACH_ENABLED` | true | P2P 地震情報の地図画像を Embed に添付するか。`false` で CDN ポーリングを無効化し、本文への画像 URL テキスト追記方式にフォールバック（詳細は「P2P 地図画像の添付」参照） |

### EWS（緊急警報放送）設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `EWS_ENABLE` | false | 津波警報・大津波警報の発表・更新時に AFSK 緊急警報信号音を再生するか（詳細は「EWS（緊急警報放送）信号音」参照） |
| `EWS_REGION` | 全国共通 | 告示別表第1号の地域符号キー（`core/ews_signal.py` の `REGION_CODES` 参照。未定義キーは「全国共通」にフォールバック） |
| `EWS_BLOCKS` | 6 | 信号送信ブロック数（1ブロック=58bit、約0.9秒） |
| `EWS_PRETONE_SEC` | 0.2 | 前置固定音（1024Hz）の長さ（秒） |
| `EWS_POSTTONE_SEC` | 0.2 | 後置固定音（1024Hz）の長さ（秒） |

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
| `KYOSHIN_MIN_ACTIVE_PIXELS` | 2 | 1セル内でアクティブとみなす最小の揺れ候補ピクセル数（HSVマスクの一次フィルタ） |
| `KYOSHIN_ACTIVE_SHINDO_FLOOR` | 0.5 | 揺れ候補ピクセルとみなす実震度の下限。下げるとGIF圧縮ノイズを誤検知しやすくなるため非推奨 |
| `KYOSHIN_RISE_THRESHOLD` | 0.5 | 「上昇トリガー」とみなす基準値との差分幅。震度の絶対値ではなく変化量で判定する |
| `KYOSHIN_BASELINE_WINDOW_START_SEC` | 10.0 | 基準値計算に使う過去サンプルの開始位置（秒前） |
| `KYOSHIN_BASELINE_WINDOW_END_SEC` | 25.0 | 基準値計算に使う過去サンプルの終了位置（秒前） |
| `KYOSHIN_HISTORY_WINDOW_SEC` | 25.0 | 観測点ごとに保持する震度履歴の長さ（秒）。BASELINE_WINDOW_END_SEC以上を推奨 |
| `KYOSHIN_NEIGHBOR_TRIGGER_COUNT` | 2 | 上昇トリガー確定に必要な、8近傍のうち同時に上昇トリガーが立っている観測点数 |
| `KYOSHIN_EVENT_TIMEOUT_SEC` | 45.0 | 最後の上昇トリガーからこの秒数経過でイベント終了。上げるほど余韻の通知が長く続く |
| `KYOSHIN_MIN_NOTIFY_PHASE` | Weaker | 通知を送信する最小フェーズ（Weaker &lt; Weak &lt; Medium &lt; Strong &lt; Stronger） |
| `KYOSHIN_MIN_STATIONS_SHINDO0` | 4 | 実震度が震度0相当（1.0未満）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_MIN_STATIONS_SHINDO1` | 2 | 実震度が震度1相当以上（1.0以上）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_DEBUG_SAVE_IMAGE` | false | イベント確定時の元画像をローカル保存するか（事後検証用） |
| `KYOSHIN_DEBUG_IMAGE_DIR` | ./kyoshin_debug_images | デバッグ画像の保存先ディレクトリ |

### 音声設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `TTS_ENGINE` | aquestalk | 読み上げエンジン（`aquestalk` / `scratchtts`） |
| `AQUESTALK_PATH` | （空） | AquesTalkPi の実行ファイルパス（未設定で音声無効。`TTS_ENGINE=aquestalk` 時のみ使用） |
| `AQUESTALK_SPEED` | 150 | AquesTalkPi の読み上げ速度 |
| `SCRATCHTTS_URL` | Scratch公式API | ScratchTTS のエンドポイント（`TTS_ENGINE=scratchtts` 時のみ使用） |
| `SCRATCHTTS_LOCALE` | ja-JP | ScratchTTS の言語ロケール |
| `SCRATCHTTS_GENDER` | female | ScratchTTS の声の性別（`female` / `male`） |
| `SCRATCHTTS_TIMEOUT_SEC` | 10 | ScratchTTS APIリクエストのタイムアウト秒数 |
| `FFMPEG_PATH` | ffmpeg | ScratchTTSのピッチシフト（`core/tts_engines.py`）で使用する ffmpeg の実行コマンド／パス。デフォルトはOSのPATHから解決。PATHが通っていない環境では絶対パスを指定。入力音声のサンプルレート取得はPython標準の`wave`モジュールで完結し、外部コマンド（ffprobe等）には依存しない。WAV形式でないレスポンスの場合はピッチシフト自体を行わず元音声のまま再生する |
| `AUDIO_PLAYER` | aplay | 音声再生コマンド（`aplay` / `mpg123` 等） |
| `SPEECH_QUEUE_MAXSIZE` | 200 | 音声読み上げキューの最大サイズ |
| `MP3_QUEUE_MAXSIZE` | 50 | MP3 再生キューの最大サイズ |

`TTS_ENGINE=scratchtts` を選択した場合、取得した音声は ffmpeg で約3セミトーン（周波数比 約1.19倍）ピッチアップしてから再生されます（再生時間は変わりません）。ピッチシフトには `ffmpeg` コマンドが必要です。

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
| `WEB_DASHBOARD_HOST` | 127.0.0.1 | Web Dashboard がバインドするアドレス。このマシン以外からアクセスさせたい場合のみ `0.0.0.0` 等を指定する |
| `WEB_DASHBOARD_ALLOWED_IPS` | （空） | アクセスを許可するクライアントIPのカンマ区切りリスト（CIDR表記可）。空の場合はIP制限なし |
| `STATUS_HISTORY_INTERVAL` | 300 | `GET /dashboard`・`GET /status/history` 用のスナップショット記録間隔（秒） |
| `STATUS_HISTORY_MAXLEN` | 288 | 保持するスナップショットの最大件数（古いものから自動破棄。デフォルトは300秒間隔で24時間分） |

`WEB_DASHBOARD_HOST=0.0.0.0` にする場合は、`WEB_DASHBOARD_ALLOWED_IPS` で許可するIPを明示的に絞ることを強く推奨する（未設定のまま `0.0.0.0` にすると、ネットワーク環境によっては外部から誰でも `/status` 等にアクセスできる状態になる）。

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
  "timestamp": "2026-08-04T12:00:00.000000",
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
      "last_eew_id": "20260804120000",
      "last_recv_time": "2026-08-04T11:59:00.000000",
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
      "last_event_id": "20260804_volcano_XX.json",
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
    },
    "kyoshin": {
      "enabled": true,
      "active_event_count": 0,
      "active_event_ids": []
    }
  },
  "tasks": {
    "p2p_ws_hub": "running",
    "p2p_ws_hub_recv_count": { "eew": 0, "quake": 12, "tsunami": 0 },
    "fetch_tsunami_observation": "running",
    "fetch_quake_advisory": "running",
    "fetch_usgs_quake": "running",
    "speech_worker_audio": "running",
    "mp3_worker_audio": "running",
    "volcano_poller": "running",
    "eruption_poller": "running",
    "warning_poller": "running",
    "fetch_long_period": "running",
    "kyoshin_monitor": "running"
  }
}
```

> `quake`（地震情報）・`tsunami`（津波情報）・`eew`（緊急地震速報）は、`core/p2p_ws_hub.py` の
> `P2PWebSocketHub` が単一の WebSocket 接続から一元的に受信・振り分けを行う
> （2026-08 の REST ポーリング → WebSocket 移行以降。`tasks.p2p_ws_hub` の稼働状態と
> `tasks.p2p_ws_hub_recv_count` の各種別ごとの受信件数を参照）。

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

## CLIテスト実行機能

自動テスト（pytest等）は現時点で整備されていないが、代わりに実際のBotプロセス上で
サンプルJSONデータを使って特定Cogの通知ロジック（Embed生成・読み上げ・効果音）だけを
動かし、実チャンネルで目視・耳で確認できるCLIテスト実行機能を用意している。

### 使い方

```bash
python3 bot.py --test_<対象> <JSONファイルパス>
```

Botは通常通り起動し、全Cogの `on_ready` が完了した後に指定したテストを1回実行し、
完了後に自動的にプロセスを終了する。

### EWS（緊急警報放送）信号音の単体テスト

`--test_ews` はJSONファイルの指定を必要としない特殊なテスト対象で、
`core/ews_signal.py` によるAFSK信号音の生成・再生のみを単体で確認できる。

```bash
python3 bot.py --test_ews                # MajorWarning相当の信号音を再生
python3 bot.py --test_ews Warning         # Warning相当の信号音を再生
python3 bot.py --test_ews MajorWarning    # 明示的にMajorWarning相当を指定
```

`EWS_ENABLE` の設定値に関わらず、CLIテスト実行時は常に信号音が再生される
（動作確認自体が目的のため）。

### 入力JSONの検証（誤指定の検知）

`--test_<対象>` に指定したJSONファイルが、対象の通知関数が本来期待する
フィールドを含んでいるかを起動時に検証する（`validate_expected_fields()`）。
例えば `--test_eew` に地震情報用のJSON（`quake_sample.json` 等）を誤って
指定した場合、以下のような警告がコンソール・ログの両方に出力される。

```
[TEST] 警告: --test_eew に指定したJSONに想定フィールドが見つかりません: ['EventID']
[TEST]       (対象JSONファイルを取り違えている可能性があります。 eew が期待するフィールド: ['EventID', 'Hypocenter', 'MaxIntensity'])
```

警告はあくまで注意喚起であり、テスト実行自体は中断されず継続する
（意図的に一部フィールドを欠いたJSONで異常系を確認したい場合もあるため）。

### 本番プロセスとの Web Dashboard ポート競合回避

CLIテストモード（`--test_*` 付きで起動した場合）では、Web Dashboard の
起動自体を自動的にスキップする。これは、systemd 等で稼働中の本番プロセスが
既に `WEB_DASHBOARD_PORT` を使用している状態でCLIテストを実行しても、
ポート衝突（`OSError: [Errno 98] Address already in use`）が発生しないようにするため。
（本番プロセスを止めずに気軽にCLIテストを実行できる）

### 対応している `--test_<対象>`

| 対象 | 呼び出し先 | サンプルJSON |
|:---|:---|:---|
| `eew` | `EewCog.notify_eew` | `tests/fixtures/eew_sample.json` |
| `quake` | `QuakeInfoCog.notify_quake` | `tests/fixtures/quake_sample.json` |
| `tsunami` | `TsunamiCog.notify_tsunami` | `tests/fixtures/tsunami_sample.json` |
| `tsunami_observation` | `TsunamiCog.notify_tsunami_observation` | （気象庁 VTSE51 形式のJSONを用意） |
| `tsunami_forecast` | `TsunamiCog.notify_tsunami_forecast` | （気象庁 VTSE41 形式のJSONを用意） |
| `volcano` | `VolcanoCog._notify_volcano` | `tests/fixtures/volcano_sample.json` |
| `volcano_eruption` | `VolcanoCog._notify_eruption` | （eruption.json の1エントリ形式） |
| `volcano_warning` | `VolcanoCog._notify_warning` | （warning.json の1エントリ形式） |
| `usgs` | `UsgsCog.notify_usgs_quake` | `tests/fixtures/usgs_sample.json` |
| `other_long_period` | `OtherInfoCog.notify_long_period` | （長周期地震動情報の list item 形式） |
| `other_quake_advisory` | `OtherInfoCog.notify_quake_advisory` | （その他地震情報の list item 形式） |

実行例：

```bash
python3 bot.py --test_eew tests/fixtures/eew_sample.json
python3 bot.py --test_quake tests/fixtures/quake_sample.json
python3 bot.py --test_tsunami tests/fixtures/tsunami_sample.json
```

### テストであることの明記

`notify_eew` / `notify_quake` / `notify_tsunami` 等、`is_test` 引数に対応している関数は、
テスト実行時に以下の形でテストであることを明示する：
- Embedタイトルの先頭に **「【テスト】」** を付与
- Embedフッターに **「※これはテスト通知です。」** を表示

`_notify_volcano` 等、`is_test` 引数に未対応の関数（既存実装の都合）は、
`core/test_runner.py` の `_inject_test_marker()` により、JSON内のタイトル系フィールド
（`headTitle` 等）へ動的に「【テスト】」を付与してから呼び出す。

さらに、コンソール出力とBotログの両方に以下のような明示的なテストバナーが出力される：

```
============================================================
[TEST] これはテスト実行です — 対象: eew (EewCog.notify_eew)
[TEST] 入力ファイル: tests/fixtures/eew_sample.json
============================================================
```

### サンプルJSONの追加

`tests/fixtures/` に用意されていない対象（`tsunami_observation` 等）は、
対応する `notify_*` 関数が受け取る `data` 引数と同じ構造のJSONファイルを
自分で用意すれば動作する。気象庁の実データ（`list.json` から辿れる詳細JSON）や
過去にDiscordへ送信された通知の元データを保存しておくと、回帰確認用の
サンプルとして再利用しやすい。

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
2. ログで検知の状態を確認
   ```bash
   tail -f qtlbot.log | grep -i kyoshin
   # イベントが生成されているのに通知が来ない場合は
   # KYOSHIN_MIN_STATIONS_SHINDO0 / SHINDO1、KYOSHIN_MIN_NOTIFY_PHASE の閾値を確認
   # 特定の観測点の警告ログが繰り返し出る場合は、その観測点が機器異常として
   # ブラックリスト化されている可能性がある（"ブラックリスト化しました" で検索）
   ```
3. `KYOSHIN_DEBUG_SAVE_IMAGE=true` にして `KYOSHIN_DEBUG_IMAGE_DIR` に保存された画像で誤検知・未検知の状況を事後確認
4. 揺れが収まった後も通知が続く時間が長い／短いと感じる場合は `KYOSHIN_EVENT_TIMEOUT_SEC`（デフォルト45秒）を調整

---

## コード構成

### ディレクトリ構成
```
QTL_Bot/
├── bot.py                  - エントリーポイント（Cog 登録・起動のみ）
├── region_map.json          - 緊急地震速報の警報地域名 → 表示用地域名マッピング
├── prefecture_map.json      - 緊急地震速報や震度情報で用いる区域名 → 都道府県名マッピング（震度速報の読み上げ等で使用）
├── pref_region_map.json     - 府県予報区名 → 地方予報区名マッピング（EEWの警報対象が広範囲な場合の集約表示に使用）
├── cogs/
│   ├── apm.py                - ApmCog: Mackerel APM 連携（OpenTelemetry OTLP）
│   ├── audio_shared.py       - AudioCog: 音声読み上げ・MP3再生の実体（EewCog/QuakeInfoCogが共有）
│   ├── eew.py                - EewCog: 緊急地震速報（Wolfx/P2P EEW）専用
│   ├── quake.py              - QuakeInfoCog: 地震情報（震度速報等）・P2P地震情報ポーリング
│   ├── tsunami.py            - TsunamiCog: 津波観測・予報
│   ├── volcano.py            - VolcanoCog: 火山情報・噴火速報・噴火警報
│   ├── usgs.py               - UsgsCog: USGS 海外地震情報
│   ├── other.py              - OtherInfoCog: 長周期地震動・気象庁その他情報
│   ├── system.py             - SystemCog: !status・Web Dashboard・エラー監視・リソース監視
│   └── kyoshin_monitor.py    - KyoshinMonitorCog: 強震モニタ画像解析による揺れ検知
└── core/
    ├── config.py                  - 環境変数読み込み・定数定義
    ├── logging_setup.py           - ログ設定（RotatingFileHandler・重複抑制）
    ├── audio.py                   - AudioMixin（キュー実体を持つCog用）/ AudioClientMixin（AudioCog参照用）
    ├── tts_engines.py             - TTSエンジン（AquesTalkPi/ScratchTTS）の切り替え・音声合成
    ├── ws_helpers.py              - WebSocket自動再接続の共通ループ（EewCog等が使用）
    ├── fetch_backoff.py           - HTTPポーリングのCircuit Breaker（連続失敗時のバックオフ）
    ├── p2p_image.py               - P2PImageMixin（P2P地震情報CDNの地図画像をEmbedに添付。
    │                                 内容検証付きリトライで QuakeInfoCog/TsunamiCog が共有）
    ├── ews_signal.py               - EWS（緊急警報放送）AFSK信号音のビット列組み立て・PCM波形合成
    │                                 （外部ファイル出力なし、生成したPCMをメモリ上のまま再生に渡す）
    ├── kyoshin_shared.py          - 震度色分け・両画像取得・振動レベル取得の共通ロジック
    │                                 （EEW発表時通知・画像解析検知通知の両方から利用）
    ├── kyoshin_image_analyzer.py  - HSVマスク処理による画像→震度グリッド変換
    ├── kyoshin_detector.py        - 揺れ検知イベントのライフサイクル管理（EventManager による状態機械）
    └── kyoshin_image_monitor.py   - EventManager と連動し、イベント継続中の画像通知ループを制御
```

### Cog 責務一覧
| Cog | ファイル | 主な責務 |
|:---|:---|:---|
| `ApmCog` | `cogs/apm.py` | OpenTelemetry 計装・Mackerel OTLP 送信（デフォルト無効） |
| `AudioCog` | `cogs/audio_shared.py` | 音声読み上げ・MP3再生の実体（EewCog・QuakeInfoCogが共有） |
| `EewCog` | `cogs/eew.py` | Wolfx WebSocket（EEW）・P2P WebSocket（EEW 警報）・EEW発表時の強震モニタ通知 |
| `QuakeInfoCog` | `cogs/quake.py` | P2P API（地震速報・各地の震度等）ポーリング・通知 |
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
| `KyoshinImageAnalyzer.analyze_all()` | 強震モニタ画像をグリッド分割し、HSVマスクで各セルの実震度を推定 |
| `EventManager.ingest()` | 観測点ごとに基準値との差分から上昇トリガーを判定し、ブラックリスト仮判定も行う |
| `EventManager.tick()` | 近隣同時上昇の確認・ブラックリスト確定・イベントの生成/マージ/終了判定を行う |
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

**最終更新**: 2026-08-04
**対応 Python**: 3.11+
