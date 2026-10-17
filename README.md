# QTL_Bot - Discord 地震・津波・火山情報通知 Bot

気象庁（JMA）・P2P地震情報・Wolfx・USGS などの情報源から、緊急地震速報（EEW）・地震情報・津波情報・火山情報などを取得し、Discord へ自動通知する Bot です。Raspberry Pi での常時運用を想定した軽量実装です。

---

## 機能一覧

### 緊急地震速報（EEW）
- **Wolfx WebSocket**（主）と **P2P地震情報の EEW（警報、code=556）** をリアルタイム受信して通知する。予想最大震度・地域ごとの予想震度・推奨行動を表示し、AquesTalkPi 等で読み上げる。
- EventID ごとの最大 Serial をソース別に独立管理し、同一ソース内の重複・逆行メッセージだけを除外する（Wolfx と P2P は Serial の採番が別のため、互いの通知を抑制しない）。
- 警報（`isWarn`）時の注意喚起文は予想最大震度で切り替える（6弱以上「直ちに身の安全を確保してください」、それ未満「強い揺れに警戒してください」）。
- 警報対象の府県予報区が `EEW_REGION_COLLAPSE_THRESHOLD`（既定10）件以上なら、`pref_region_map.json` で地方予報区に集約して通知・読み上げる。
- 複数の地震のEEWが有効（`recent_eews`、TTL300秒で2件以上）なときは、通常通知の直前に「複数の緊急地震速報が発表されています」のサマリーを送る。
- PLUM法（`earthquake.condition` が「仮定震源要素」）では、マグニチュード・深さが「推定なし」になるのが仕様。
- `region_map.json` に無い地域名は、同じ都道府県の既知の区域への末尾一致で解決する（`core/eew_convert.py` の `resolve_chiiki`）。解決できない場合は「その他」とし、警告ログを1回出す。国後島・択捉島・色丹島の区域は `region_map.json` に無く、地図では塗られない（既知の制約）。
- 津波警報・大津波警報の発表時は、`EWS_ENABLE=true` で緊急警報放送（EWS）の信号音を再生できる（後述）。

### 地震情報
- P2P地震情報（WebSocket code=551）から、震度速報・震源に関する情報・震度・震源に関する情報・各地の震度に関する情報・遠地地震に関する情報などを通知する。
- 震度速報は区域名を都道府県名に変換して読み上げ、本文に区域別の震度一覧を付ける。震源に関する情報は震源地に度分秒の緯度経度を併記する。各地の震度では、観測点が `QUAKE_INTENSITY_COLLAPSE_THRESHOLD`（既定10）件以上なら都道府県ごとに代表1地点だけを表示する。
- 津波の有無（`domesticTsunami`）は速報段階の推定値のため、`Watch`／`Warning` は「津波警報・注意報を発表中」とまとめて表現する。
- **P2P 障害時のフォールバック**: 後述。
- 通知は `QUAKE_MIN_SCALE`・`QUAKE_MIN_MAG`・深さ・種別ごとの有効化フラグでフィルタできる。

### P2P地震情報の障害時フォールバック
P2P地震情報 WebSocket が使えないときに、地震情報（震度速報・震源に関する情報・各地の震度に関する情報）を代替の取得元から通知する。

- **接続の一元管理**: `core/p2p_ws_hub.py` の `P2PWebSocketHub` が WebSocket 接続を1本だけ保持し、code ごとに各 Cog へ配信する（551→地震情報、552→津波、556→EEW、9611→地震感知情報）。P2P の接続数上限（IP あたり2本）に対応するため。
- **切り替え条件**（どちらか早い方）: 連続接続失敗が `P2P_FAILOVER_THRESHOLD`（既定5）回に達する、または接続できていない状態が `P2P_FAILOVER_DISCONNECT_SECONDS`（既定10）秒続く。
- **取得元**（`QUAKE_FAILOVER_SOURCES`、既定 `quake_one,jma_json`。先頭から順に試す）: Quake.One（http のみの第三者配信のため入力を検証する。震度速報・震源に関する情報の段階は無く最終形のみ届く）、気象庁HPのJSON。気象庁防災情報XML（`jma_xml`）は既定の対象外で、指定すれば使える。取得した電文は P2P の JMAQuake（code=551）互換形式に変換し、通常と同じ通知処理を通す（フッターに「P2P地震情報の障害のため〇〇から取得」と表示）。
- **復旧**: WebSocket が `P2P_FAILOVER_RECOVERY_SECONDS`（既定60）秒以上連続して接続できたらフォールバックを停止する。WebSocket の再接続はフォールバック中も続ける。
- **重複防止**（`QuakeContentDedupe`）: 震源要素（発生時刻±3分・震央地名・M・深さ）と震度分布の内容指紋で照合し、取得元をまたいでも同じ内容は通知しない（保持は最大200件・6時間）。
- **起動時**: 各取得元の現在の一覧を既知として記録し、通知しない。発表が `P2P_FAILOVER_CATCHUP_MINUTES`（既定30）分より古い情報も通知しない。
- 無効化は `P2P_FAILOVER_ENABLE=false`。取得元を増やすときは `QuakeFallbackSource` を継承して `SOURCE_FACTORIES` に登録する。
- 制約: 気象庁JSON/XMLの実データでの検証は未実施（テストは仕様から作った合成データ）。Quake.One は `list.json` の応答が最大3分キャッシュされる。

### 津波情報
- **P2P地震情報**（WebSocket code=552、`notify_tsunami`）: 警報種別・予想高さ別の区域一覧、ただちに来襲・到達予想時刻、発信元コメントを表示。読み上げは「（地域名）に（警報種別）が発表されました」。
- **気象庁HPのJSON**（`tsunami/data/list.json` を60秒間隔でポーリング）: `Head.Title` で次のとおり振り分ける。
  - 津波予報・警報・注意報（VTSE41、`notify_tsunami_forecast`）: 区域一覧・原因地震・解除時の文言を出し分ける。
  - 津波観測に関する情報（VTSE51、`notify_tsunami_observation_coastal`）: 警報区分→地域→観測点の順に、高さの高い順で表示。前報より実際に上昇した観測点にだけ「↑」を付ける。
  - 沖合の津波観測に関する情報（VTSE52、`notify_tsunami_observation_offshore`）: 観測点ごとに押し・引き・状態を表示し、新規追加の観測点を読み上げる。
  - 各地の満潮時刻・津波到達予想時刻に関する情報（`notify_tsunami_observation_tide`）。
  - 上記以外の観測系は汎用の `notify_tsunami_observation`。南海トラフ地震臨時情報・震源要素更新のお知らせは `OtherInfoCog` に委譲する。
- 読み上げの文生成・状態変化の分類・二重読み上げ防止ゲートは `core/tsunami_speech.py`（純関数）。現在 `EventGate` による重複防止は満潮時刻・沖合観測の読み上げに配線済みで、`AlertGate`（警報状態の変化判定）と沿岸観測（VTSE51）の読み上げは未配線。
- 津波情報の発表時に MP3 は再生しない（読み上げと EWS 信号音のみ）。
- **EWS（緊急警報放送）信号音**: `EWS_ENABLE=true`（既定 false）のとき、津波警報・大津波警報の発表・更新時（P2P・気象庁HPの両形式、および地震情報の `domesticTsunami` 経由）に、昭和60年郵政省告示第405号準拠の AFSK 信号音（第二種開始信号）を再生する。同一イベントの警報区分が上がったときだけ再度鳴らす。波形は `core/ews_signal.py` がメモリ上で合成し、`pygame.mixer.Sound(buffer=...)` で再生する（ミキサーは 44100Hz・モノラルで初期化）。CLI テストでは `EWS_ENABLE` に関わらず再生する。

### 火山情報・USGS・その他
- **火山情報**（`VolcanoCog`）: 気象庁HPのJSONを1分間隔でポーリング。`info.json` の各エントリの `eventId` と `reportDatetime` の差分で新規・更新を判定し、活動状況・予防措置・次回発表予定を通知する。噴火速報（`eruption.json`）・噴火警報（`warning.json`）は独立にポーリングする。
- **USGS 地震情報**（`UsgsCog`）: USGS の GeoJSON フィードから海外の地震を取得。対象領域・M下限・クールダウンを設定できる（通知済みIDはフィードに載っている間は保持）。
- **気象庁その他特別情報**（`OtherInfoCog`）: 北海道・三陸沖後発地震注意情報・南海トラフ地震臨時情報・顕著な地震の震源要素更新のお知らせ・長周期地震動に関する観測情報。後発地震注意情報と南海トラフ臨時情報には区域図（`hokkaido_bosaitaiou_area.png`／`nankai_bosaitaiou_area.png`、`bot.py` と同じディレクトリ）を添付する。南海トラフ臨時情報と震源要素更新は気象庁が2つの `list.json` に重複掲載するため、2経路から独立に通知される。
- **P2P 地震感知情報**（`JishinKanchiCog`、code=9611。既定無効、`JISHIN_KANCHI_ENABLE=true` で有効化）: P2P が独自に推定する速報値。信頼度をレベル1〜4に変換し、地域ごとの信頼度（A〜F）でグルーピングして表示する。音声・効果音は同一イベント（`started_at`）の第一報のみ、テキスト通知は `JISHIN_KANCHI_MIN_UPDATE_INTERVAL_SEC`（既定10）秒未満の更新を間引く。地域コードの変換には `epsp_area.csv`（[p2pquake/epsp-specifications](https://github.com/p2pquake/epsp-specifications) 配布）を使う。地図画像は添付しない。

### 起動時の「既存情報」の扱い
JMA の `list.json` 等をポーリングする処理は、起動直後の最初の取得結果を「起動前から存在した情報」として記録のみ行い、通知しない（再起動のたびに過去の情報を再通知しないため）。

- 平常時に空リストが返る場合も「既存情報なし」として初期化済みにする（空のまま初期化されないと、最初の本物の発表が握りつぶされる）。
- 起動直後の取得失敗で初期化が遅れた場合に備え、発表時刻（`rdt`／`reportDatetime`）が**Botの起動時刻以降**の情報は、初回取得でも新規として通知する（`core/startup_guard.py`。発表時刻が解析できなければ既存扱い）。USGS は発表時刻を持たないため対象外。

### 強震モニタ・長周期地震動モニタ・振動レベル（`KyoshinMonitorCog`）
- **受信**: 強震モニタ `jma_s`・長周期地震動モニタ（LMoni, `abrspmx_s`）の画像を `KYOSHIN_RECV_IMAGE_INTERVAL_SEC`（既定1.0、最小1.0）秒間隔で、振動レベル（kwatch-24h.net）を `KYOSHIN_RECV_VIBRATION_INTERVAL_SEC`（既定2.0、最小2.0）秒間隔で受信する（それぞれ有効/無効を設定可能）。振動レベルが `KYOSHIN_VIBRATION_DETECT_LEVEL`（既定100）以上なら「揺れを検知」と判定する。
- **通知**（`KYOSHIN_NOTIFY_ENABLE`）: 通知間隔は有効な受信のうち最短の間隔。EEW発表時（第一報〜最終報・キャンセル報、最長300秒）と、検知時（画像解析または振動レベル）に通知する。EEW通知中は検知通知を中断し、EEW通知の終了後に検知が続いていれば再開する。通知色は `jma_s` の実震度から決める独自カラーマップ（画像が無ければ振動レベル）。
- **画像解析による揺れ検知**: 実観測点データ（[kyoshin-monitor-observation-points](https://github.com/ingen084/kyoshin-monitor-observation-points) の `intensity-points.json`）で `jma_s` 画像をサンプリングし、基準値（過去10〜25秒平均）との差分による上昇トリガーと、K近傍（既定6）のうち一定数の同時上昇で判定する。起動直後は `KYOSHIN_STARTUP_WARMUP_SEC` の間イベント生成を抑制する。周囲が無反応のまま高震度が続く観測点は機器異常とみなして自動除外する。誤検知対策の調整値は `.env.kyoshin` に分離している。検知までの遅延は、起動判定を意図的に厳しめにしていること（P波の立ち上がりが緩い小地震では10〜25秒程度）が主因で、既定値は変更していない。Pillow が無い環境では画像解析検知だけが無効になる。
- 振動レベルに応じた音声（`lv100`／`lv1000`／`lv2000`）は、EEW通知では常に、検知通知では `KYOSHIN_DETECT_VIBRATION_SOUND` が有効なときに鳴らす。

### GIS地図描画（既定無効）
気象庁の区域データ（有志によりGeoJSON化、CC-BY-4.0）を使い、震源・震度分布・津波予報区などの地図を Pillow だけで描画して通知に添付する。`EewCog`・`QuakeInfoCog`・`TsunamiCog`・`OtherInfoCog`・`UsgsCog` が利用し、データ取得・キャッシュは `core/gis_data.py`、描画は `core/gis_render.py`、複数画像の Embed 組み立ては `core/gis_discord.py` が担当する。

- **有効化**: `GIS_MAP_ENABLE=true`。各 Cog のロード時に外部データが無ければ自動ダウンロードする（強制再取得は `python3 bot.py --refresh_gis_data`）。無効時・データ未取得時は画像なしで通知する。
- **使用データ**（キャッシュ先は `GIS_MAP_DATA_DIR`、既定 `<プロジェクトルート>/data/gis`）: 府県予報区（EEW警報の地域）・細分区域（震度分布）・津波予報区（沿岸線）の GeoJSON（[Ichihai1415/JMA-GIS-GeoJSON](https://github.com/Ichihai1415/JMA-GIS-GeoJSON)）、震度観測点一覧 `stations.json`（気象庁）、世界の国境 `countries.geojson`（[datasets/geo-countries](https://github.com/datasets/geo-countries)）と、そこから生成する簡略化版 `countries_simplified.geojson`。
- **描画内容**: EEW警報は発表地域の塗りつぶし＋震源のバツ印（警報時は震度分布の地図も2枚目に添付、PLUM法は震源の代わりにドーナツ型）。地震情報・EEW予報は震度の区域塗り（震度速報は区域中央に震度アイコン、観測点情報があれば観測点マーカー）。津波は予報区の沿岸線を警報種別の色で、VTSE51 は観測点を観測値に応じた色の円で、VTSE52 は押し（リング）・引き（円）で描く。長周期地震動は観測点の階級を色分けした地図を2枚目に添付する。USGS・遠地地震は国外向けの地図を使う。
- **EEWキャンセル報**: Wolfx・P2P地震情報ともに、キャンセル報は直前の報と同じ Serial で届くため、Serial の大小ではなく EventID 単位（取得元ごとに最初の1回だけ）で通知する（`core/eew_convert.should_process_eew`）。通知時は取消音（`eewC`）も再生する。P2P地震情報のキャンセル報には発生時刻が含まれないため、その行は省く。
- **遠地地震向けの地図**: 震源が日本の表示範囲外、または表示範囲に陸地が無い場合は、`render_overseas_map` に自動で切り替える（世界の国境を陸地色で塗り、日本の区域線と震源を重ねる。日付変更線をまたぐ震源も近い側の範囲で描く）。国境データの読み込みは `EewCog` 起動時にワーカースレッドで先に済ませる。震度の区域・観測点がある地震情報（各地の震度に関する情報など）では、観測点マーカーを縮小サイズで重ねて描く。
- **自動ズーム・見た目**: 描画対象に合わせて表示範囲を自動で決め、2倍で描いて縮小するアンチエイリアスをかける。投影は簡易正距円筒図法で、`shapely` などの追加依存は使わない。`GIS_MAP_THEME`（`light`／`dark`／`highlight`）で配色を切り替えられ、震度アイコンの角丸・縁取り・文字色も設定できる。
- **描画はワーカースレッドで実行する**: 各 `render_*` は CPU を使う同期処理（Raspberry Pi では1枚あたり数百ms〜1秒超）のため、`await asyncio.to_thread(...)` 経由で呼ぶ。描画倍率（`_scale`）はスレッドごとの値（`threading.local`）なので、同時に描画しても混ざらない。

### 運用・監視
- **ログ**: `RotatingFileHandler`（既定 10MB × 7世代）。ファイル／コンソールのログレベルを別々に設定でき、同一メッセージは `LOG_DUPLICATE_THRESHOLD`（既定60）秒以内なら抑制する（ERROR 以上は常に出力）。
- **エラー自動通知**: `ADMIN_CHANNEL_ID` を設定すると、ERROR 以上のログを管理者チャンネルへ自動通知する（同じエラーは1時間に1回）。毎日 00:00 に前日のエラー集計を投稿する。`ADMIN_CHANNEL_ID` 未設定（0）なら無効。
- **ヘルスチェック**: Wolfx・JMA・P2P の疎通確認（`/health/full`、結果は30秒キャッシュ）。
- **リソース監視**: 既定で1時間ごとに CPU・メモリ・ディスク使用率を記録し、ディスク使用率が80%以上で WARNING、90%以上で ERROR を出す。
- **配信成功率**: 各 Cog の `channel.send()` の成否を記録し、直近24時間の成功率を `!status`・`GET /status` で確認できる。
- **週間／月間ダイジェスト**: `DIGEST_ENABLED=true` で、先週／先月の通知件数を指定の曜日・時刻（または毎月1日）に投稿する。
- **APM（Mackerel 連携、既定無効）**: `APM_ENABLED=true` で OpenTelemetry（OTLP）経由で aiohttp の全 HTTP リクエストをトレースする。想定内の `TimeoutError`／`CancelledError` はエラー扱いにしない。実際のエンドポイント・ヘッダー名は [Mackerel 公式ドキュメント](https://mackerel.io/ja/docs/entry/tracing/installations/python)で必ず確認すること。
- **通知履歴**: 地震情報・EEW・通知履歴は `qtlbot.log` に機械可読な1行（`QUAKE_RECORD_V1`／`EEW_RECORD_V1`／`NOTIF_RECORD_V1`）として記録し、再起動後も `GET /status/quake_history`・`/status/eew_history`・`/status/notifications`・`/quake_map` から閲覧できる（`LOG_BACKUP_COUNT` 世代分）。記録前の過去分は `python3 bot.py --backfill_quake_history` で P2P API から遡って補完できる。

---

## セットアップ

### 必須環境
- Python 3.11+
- 必須パッケージ（`requirements.txt`）: discord.py 2.0+、aiohttp、websockets、python-dotenv、Pillow（GIS地図描画・強震モニタ画像解析）
- 任意: pygame（MP3・EWS信号音の再生。無い場合は音声再生が無効）、psutil（リソース監視・`!status` のシステム欄）、opentelemetry 系（APM。無効時は不要）、AquesTalkPi または ScratchTTS（読み上げ）

### インストール
```bash
git clone https://github.com/akethimitsuhide/QTL_Bot.git
cd QTL_Bot
pip install -r requirements.lock   # 本番機で動作確認した版を固定（推奨）
# pip install -r requirements.txt  # 下限指定のみ（最新版を入れたい場合・開発用ツールも入る）

# AquesTalkPi（任意：音声読み上げ）
# Raspberry Pi 向け: https://www.a-quest.com/products/aquestalkpi.html
```

### 1. Discord Bot の作成と招待
1. [Discord Developer Portal](https://discord.com/developers/applications) で「New Application」から Bot のアプリケーションを作成する。
2. 左メニュー「Bot」でトークンを取得する（「Reset Token」）。**`.env` の `BOT_TOKEN` そのものなので、パスワード同様に扱い、公開しない。** 漏洩した疑いがあれば同じ画面で再発行する。
3. 同じページの「Privileged Gateway Intents」で **MESSAGE CONTENT INTENT**（`!status` 等の認識に必要）と **SERVER MEMBERS INTENT** を ON にして保存する。
4. 「OAuth2」→「URL Generator」で SCOPES に `bot`、BOT PERMISSIONS に必要な権限（簡単にするなら `Administrator`）を選び、生成された URL を開いて自分のサーバーに追加する（プルダウンにサーバーが出ない場合は、そのサーバーの「サーバー管理」権限が必要）。

### 2. 環境変数の設定
**方法A: 対話式ウィザード（初めての場合に推奨）**
```bash
python3 bot.py --starter
```
トークン・チャンネルIDなど最低限の項目だけを質問し、`.env.example` をベースに `.env` を生成する（既存の `.env` は `.env.bak.<タイムスタンプ>` にバックアップ）。

**方法B: 手動**
```bash
cp .env.example .env
# BOT_TOKEN と CHANNEL_ID を設定する
```

既存の `.env` に新しい項目を足したいとき:
```bash
python3 bot.py --merge_env          # .env.example にあって .env に無い項目を1つずつ確認しながら追加
python3 bot.py --merge_env --yes    # 確認せず、不足項目をすべて既定値で追加
```
追加は末尾への追記のみで、既存の値は変更しない（書き込み前にバックアップ。コメントアウトされた項目は不足に含めない）。

`.env` と `.env.example` の整合性（変数名の書き忘れ・消し忘れ）は `python3 bot.py --check_env` で確認できる（既定値の一致までは検証しない）。

### 3. チャンネルの設定
通知先のテキストチャンネルを作り、ID を `.env` に設定する。ID は Discord の設定「詳細設定」で「開発者モード」を ON にし、チャンネルを右クリック（スマホは長押し）→「IDをコピー」で取得する。未設定のチャンネルはすべて `CHANNEL_ID` にフォールバックする（詳細は「環境変数リファレンス」の Discord 設定）。

### 4. 起動
```bash
python3 bot.py
```

---

## 環境変数リファレンス

### 設定ファイル
| ファイル | 役割 | 要否 |
|:---|:---|:---|
| `.env`（`.env.example` をコピー） | トークン・チャンネルID・通知フィルター等の本体設定 | 必須 |
| `.env.kyoshin`（`.env.kyoshin.example` をコピー） | 強震モニタの誤検知対策の調整値（上級者向け） | 任意（無くても既定値で動作） |

`.env` の読み込み後、`.env.kyoshin` があれば追加で読み込む（`core/env_loader.py`。同じ変数は `.env` が優先）。以下の「既定値」は `core/config.py` の値。

### Discord 設定
| 変数名 | 必須 | 既定値 | 説明 |
| :--- | :---: | :--- | :--- |
| `BOT_TOKEN` | 必須 | — | Discord Bot のトークン |
| `CHANNEL_ID` | 必須 | — | デフォルト通知チャンネル ID |
| `EEW_CHANNEL_ID` |  | CHANNEL_ID | EEW 専用チャンネル |
| `P2P_EEW_CHANNEL_ID` |  | EEW_CHANNEL_ID | P2P EEW（警報）専用チャンネル |
| `QUAKE_CHANNEL_ID` |  | CHANNEL_ID | 地震情報専用チャンネル |
| `TSUNAMI_CHANNEL_ID` |  | CHANNEL_ID | 津波警報専用チャンネル |
| `VOLCANO_CHANNEL_ID` |  | CHANNEL_ID | 火山情報専用チャンネル |
| `USGS_CHANNEL_ID` |  | QUAKE_CHANNEL_ID | USGS 通知専用チャンネル |
| `JISHIN_KANCHI_CHANNEL_ID` |  | QUAKE_CHANNEL_ID | 地震感知情報専用チャンネル |
| `OTHER_CHANNEL_ID` |  | CHANNEL_ID | その他情報（長周期地震動等） |
| `KYOSHIN_CHANNEL_ID` |  | OTHER_CHANNEL_ID | 強震モニタ専用チャンネル |
| `ADMIN_CHANNEL_ID` |  | 0（無効） | エラー通知用管理者チャンネル |

実際にどのチャンネルへ何が送られるかは、`!status`／`/qtl_status` の「通知先チャンネルマッピング」欄で確認できる。

### 通知フィルター設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `QUAKE_MIN_SCALE` | 0 | 地震通知の震度下限（0=全て / 10=震度1以上 / 30=震度3以上 / 45=震度4以上 / 50=震度5弱以上） |
| `QUAKE_MIN_MAG` | 0.0 | 地震通知のマグニチュード下限 |
| `QUAKE_MIN_DEPTH` | 0 | 地震通知の深さ下限（km） |
| `QUAKE_MAX_DEPTH` | 9999 | 地震通知の深さ上限（km） |
| `EEW_MIN_INTENSITY` | 0 | **現在未使用**（設定しても効果なし。EEWは常にすべて通知される） |
| `QUAKE_INTENSITY_COLLAPSE_THRESHOLD` | 10 | 各地の震度に関する情報で、最大震度より1階級小さい震度の観測点数がこの件数以上の場合、都道府県ごとに1地点だけを表示して「（以下略）」を付ける |
| `EEW_REGION_COLLAPSE_THRESHOLD` | 10 | EEWの警報対象府県予報区数がこの件数以上の場合、`pref_region_map.json` で地方予報区へ集約して通知・読み上げる |
| `QUAKE_ENABLE_DESTINATION` | true | 震源に関する情報（`Destination`）の通知 |
| `QUAKE_ENABLE_SCALE_AND_DEST` | true | 震度・震源に関する情報（`ScaleAndDestination`）の通知 |
| `QUAKE_ENABLE_SCALE_PROMPT` | true | 震度速報（`ScalePrompt`）の通知 |
| `QUAKE_ENABLE_DETAIL_SCALE` | true | 各地の震度に関する情報（`DetailScale`）の通知 |
| `QUAKE_ENABLE_FOREIGN` | true | 遠地地震に関する情報（`Foreign`）の通知 |
| `QUAKE_ENABLE_OTHER` | true | その他の情報（`Other`）の通知 |
| `TSUNAMI_ENABLE` | true | 津波情報通知の有効化 |
| `ENABLE_ADVISORY` | true | 気象庁その他情報の有効化 |
| `ENABLE_LONG_PERIOD` | true | 長周期地震動通知の有効化 |
| `ENABLE_TSUNAMI_OBS` | true | **現在未使用**（設定しても効果なし。津波観測情報は常に通知される） |

### USGS 地震情報設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `USGS_ENABLED` | true | USGS 地震通知機能の有効化 |
| `USGS_MAGNITUDE_MIN` | 5.0 | 通知対象のマグニチュード下限 |
| `USGS_FETCH_INTERVAL` | 600 | ポーリング間隔（秒） |
| `USGS_REGION_LAT_MIN` | 20 | 対象地域の緯度下限 |
| `USGS_REGION_LAT_MAX` | 50 | 対象地域の緯度上限 |
| `USGS_REGION_LON_MIN` | 120 | 対象地域の経度下限 |
| `USGS_REGION_LON_MAX` | 180 | 対象地域の経度上限 |
| `USGS_NOTIFICATION_COOLDOWN` | 300 | 通知済みIDを最低限保持する時間（秒）。フィードから消えたIDは、この時間の経過後に削除する |

### EEW・API 取得設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `WOLFX_HEARTBEAT_TIMEOUT` | 90 | Wolfx heartbeat タイムアウト（秒） |
| `FETCH_FAILURE_THRESHOLD` | 3 | API の連続失敗がこの回数に達するとバックオフに入る |
| `FETCH_BACKOFF_SECONDS` | 60 | API 失敗時のバックオフ待機時間（秒） |
| `DISCORD_SEND_RETRY_ATTEMPTS` | 3 | Discord への通知送信が一時的に失敗したときの総試行回数（初回を含む。1〜5、1 で再送なし）。詳細は下記「Discord 送信の再送」 |
| `WEBHOOK_URLS` | （空） | 追加の通知先 Webhook URL（カンマ区切りで複数可。https の Discord / Slack のみ）。**秘密情報**。空なら無効。詳細は下記「Webhook 通知」 |
| `WEBHOOK_EVENTS` | all | Webhook に送る種別。`all` または `eew,quake,tsunami,volcano,usgs,kanchi,other` のカンマ区切り |

### 震度色設定（Embed配色／GIS地図で共通）
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `SHINDO_COLOR_UNKNOWN` | `0x62626B` | 不明・震度0のEmbed色 |
| `SHINDO_COLOR_1` | `0x3098BD` | 震度1のEmbed色 |
| `SHINDO_COLOR_2` | `0x4CD0A7` | 震度2のEmbed色 |
| `SHINDO_COLOR_3` | `0xF6CB51` | 震度3のEmbed色 |
| `SHINDO_COLOR_4` | `0xFF9939` | 震度4のEmbed色 |
| `SHINDO_COLOR_5_LOWER` | `0xE52A18` | 震度5弱のEmbed色 |
| `SHINDO_COLOR_5_UPPER` | `0xC31B1B` | 震度5強のEmbed色 |
| `SHINDO_COLOR_6_LOWER` | `0xA30A6B` | 震度6弱のEmbed色 |
| `SHINDO_COLOR_6_UPPER` | `0x86046E` | 震度6強のEmbed色 |
| `SHINDO_COLOR_7` | `0x54068E` | 震度7のEmbed色 |

色は `0x3098BD`／`#3098BD`／`3098BD` のいずれの表記でも指定できる（不正な値は警告ログを出して既定値にフォールバックする）。

### 津波色設定（Embed配色／GIS地図で共通）
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `TSUNAMI_COLOR_MAJOR_WARNING` | `0xD344FC` | 大津波警報のEmbed色・GIS地図の沿岸線色 |
| `TSUNAMI_COLOR_WARNING` | `0xF93022` | 津波警報のEmbed色・GIS地図の沿岸線色 |
| `TSUNAMI_COLOR_WATCH` | `0xEEDB2D` | 津波注意報のEmbed色・GIS地図の沿岸線色 |
| `TSUNAMI_COLOR_FORECAST` | `0x80FFFF` | 津波予報のEmbed色・GIS地図の沿岸線色 |
| `TSUNAMI_COLOR_UNKNOWN` | `0x56BCFC` | 不明・その他のEmbed色・GIS地図の沿岸線色 |

### GIS地図描画設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `GIS_MAP_ENABLE` | false | GIS地図描画機能を有効化するか（試験導入のためデフォルト無効） |
| `GIS_MAP_DATA_DIR` | `<プロジェクトルート>/data/gis` | GeoJSON・観測点一覧のキャッシュ先ディレクトリ（`.gitignore`登録済み） |
| `GIS_MAP_WARNING_COLOR` | `0xFF0000` | EEW警報の発表地域の塗り色・輪郭色、およびPLUM法時の震源ドーナツマークの色 |
| `GIS_MAP_THEME` | `light` | 地図の配色テーマ。不正な値は`light` |
| `GIS_ICON_CORNER_RADIUS_RATIO` | `0.15` | 震度アイコン（観測点マーカー・震度速報の区域アイコン）の角の丸さ。一辺に対する角丸半径の比率（0=四角、0.5=円） |
| `GIS_ICON_BORDER_RATIO` | `0.1` | 震度アイコンの縁の太さ。一辺に対する比率（既定=アイコンの1/10）。縁はアイコンの**外側**に付く。0=縁なし |
| `GIS_ICON_BORDER_SATURATION` | `0.6` | 縁の色＝アイコン色の彩度に掛ける係数（0〜1。小さいほど灰色寄り。1=アイコンと同じ彩度） |
| `GIS_ICON_TEXT_COLORS` | （空） | 震度ごとの文字色。`震度:RRGGBB`をカンマ区切り（震度は`0,1,2,3,4,5-,5+,6-,6+,7`）。例: `6-:FFFFFF,6+:FFFFFF,7:FFFFFF`。 |

### 地震情報履歴設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `QUAKE_HISTORY_DEFAULT_LIMIT` | 500 | `GET /status/quake_history` がデフォルトで返す最大件数（`?limit=`で上書き可） |
| `QUAKE_HISTORY_CACHE_TTL_SEC` | 30 | qtlbot.log*のスキャン結果をキャッシュする秒数 |
| `QUAKE_HISTORY_BACKFILL_MAX_ITEMS` | 1000 | `--backfill_quake_history` がP2P地震情報APIから取得する最大件数（APIの実質的な遡り可能範囲に合わせた安全上限） |
| `QUAKE_HISTORY_BACKFILL_PAGE_SIZE` | 100 | `--backfill_quake_history` の1回あたりのAPI取得件数 |
| `QUAKE_HISTORY_BACKFILL_REQUEST_DELAY_SEC` | 0.5 | `--backfill_quake_history` のページ取得ごとの待機秒数 |

### P2P地震情報 障害時フォールバック設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `P2P_FAILOVER_ENABLE` | true | P2P WebSocket の障害時に気象庁データから代替取得する機能の有効化 |
| `P2P_FAILOVER_THRESHOLD` | 5 | 連続で何回接続に失敗したらフォールバックに入るか（最小1） |
| `P2P_FAILOVER_DISCONNECT_SECONDS` | 10 | 接続できていない状態（再接続の待ち時間中を含む）がこの秒数続いたら、失敗回数に関わらずフォールバックに入る（最小0=切断した瞬間に開始） |
| `P2P_FAILOVER_RECOVERY_SECONDS` | 60 | WebSocket が何秒以上連続して接続できたら復旧とみなすか（最小5） |
| `QUAKE_FAILOVER_POLL_SECONDS` | 10 | フォールバック中の取得間隔（秒、最小5） |
| `QUAKE_ONE_POLL_SECONDS` | 6 | Quake.One の `list.json` の取得間隔（秒）。既定6秒＝10回/分。6秒未満（10回/分を超える頻度）は指定できず6に切り上げる。詳細の取得は数えない |
| `P2P_FAILOVER_CATCHUP_MINUTES` | 30 | フォールバックが通知対象にする情報の新しさ（分、最小1）。発表がこれより古い情報は通知済みとみなして通知しない |
| `QUAKE_FAILOVER_SOURCES` | quake_one,jma_json | 取得元の優先順（カンマ区切り。`quake_one`＝Quake.One、`jma_json`＝気象庁HP JSON、`jma_xml`＝気象庁XML〔既定の対象外〕） |

### P2P 地震感知情報設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `JISHIN_KANCHI_ENABLE` | false | 地震感知情報（code=9611）通知の有効化 |
| `JISHIN_KANCHI_MAX_LEVEL` | 4 | 通知する最大レベル（1〜4、数値が大きいほど信頼度が低い）。この値以下のレベルのみ通知する |
| `JISHIN_KANCHI_MIN_COUNT` | 1 | 通知する最小件数（`count`）。これ未満は通知しない |
| `JISHIN_KANCHI_SPEECH_ENABLE` | true | 音声読み上げの有効化 |
| `JISHIN_KANCHI_SOUND_ENABLE` | true | 効果音再生の有効化 |
| `JISHIN_KANCHI_SOUND_FILE` | vxse53.mp3 | 再生する効果音ファイル名（Bot実行ディレクトリ直下に配置） |
| `JISHIN_KANCHI_EVENT_STATE_TTL_SEC` | 3600 | イベント単位の状態管理（音声トリガー・画像添付・テキスト送信間引きの判定に共通利用）を、最終更新からこの秒数以上経過したら破棄する |
| `JISHIN_KANCHI_MIN_UPDATE_INTERVAL_SEC` | 10 | 同一イベントのテキスト通知（Embed送信）を実際に行う最短間隔（秒）。第一報は必ず送信するが、2回目以降の更新は前回の実際の送信からこの秒数未満しか経っていない場合は送信自体をスキップする（詳細は「P2P 地震感知情報」参照）。0以下を指定すると事実上無効化（毎回送信）される |

### 週間/月間ダイジェスト設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `DIGEST_ENABLED` | false | ダイジェスト機能の有効化 |
| `DIGEST_INTERVAL` | weekly | `weekly`（毎週） または `monthly`（毎月1日） |
| `DIGEST_WEEKDAY` | 0 | weekly時のみ使用。投稿曜日（0=月曜〜6=日曜） |
| `DIGEST_HOUR` | 9 | 投稿時刻（24時間制、0〜23） |
| `DIGEST_CHANNEL_ID` | CHANNEL_ID | ダイジェスト投稿先チャンネル |

### EWS（緊急警報放送）設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `EWS_ENABLE` | false | 津波警報・大津波警報の発表・更新時に AFSK 緊急警報信号音を再生するか（詳細は「EWS（緊急警報放送）信号音」参照） |
| `EWS_REGION` | 全国共通 | 告示別表第1号の地域符号キー（`core/ews_signal.py` の `REGION_CODES` 参照。未定義キーは「全国共通」にフォールバック） |
| `EWS_BLOCKS` | 6 | 信号送信ブロック数（1ブロック=58bit、約0.9秒） |
| `EWS_PRETONE_SEC` | 0.2 | 前置固定音（1024Hz）の長さ（秒） |
| `EWS_POSTTONE_SEC` | 0.2 | 後置固定音（1024Hz）の長さ（秒） |

### 強震モニタ設定（`.env`）
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `ENABLE_KYOSHIN` | true | 強震モニタ機能（受信・通知・画像解析検知）の有効化 |
| `KYOSHIN_STATIONS_SOURCE_URL` | ingen084氏のリポジトリURL | 実観測点データ（`intensity-points.json`）の取得元。通常変更不要 |
| `KYOSHIN_STATIONS_CACHE_PATH` | kyoshin_stations_cache.json | 観測点データのキャッシュファイルパス（`.gitignore`対象） |
| `KYOSHIN_STATIONS_REFRESH_SEC` | 3600 | 観測点データの再取得間隔（秒）。起動時はキャッシュ優先、以後この間隔で追従する |
| `KYOSHIN_NEIGHBOR_K` | 6 | 近隣観測点として扱う件数（地理的K近傍）。`.env.kyoshin`の`KYOSHIN_NEIGHBOR_TRIGGER_COUNT`より大きい値にすること |
| `KYOSHIN_IMAGE_DELAY_SEC` | 6 | `latest.json` 取得失敗時のフォールバック探索で遡る基準秒数 |
| `KYOSHIN_IMAGE_STEP_SEC` | 3 | フォールバック探索で画像が見つからない場合に遡るステップ幅（秒） |
| `KYOSHIN_IMAGE_MAX_RETRY` | 4 | フォールバック探索の最大リトライ回数 |
| `KYOSHIN_RECV_JMA_S_ENABLE` | true | 強震モニタ（`jma_s`）を受信するか。無効にすると画像解析による揺れ検知もできない |
| `KYOSHIN_RECV_LMONI_ENABLE` | true | 長周期地震動モニタ（LMoni, `abrspmx_s`）の画像を受信するか（通知の表示用） |
| `KYOSHIN_RECV_VIBRATION_ENABLE` | true | 振動レベル（kwatch-24h.net）を受信するか。有効だと常時受信し、`KYOSHIN_VIBRATION_DETECT_LEVEL`以上で揺れを検知とみなす |
| `KYOSHIN_RECV_IMAGE_INTERVAL_SEC` | 1.0 | モニタ系（`jma_s`・LMoni）の受信間隔（秒）。**最小1.0**（未満は1.0に切り上げ） |
| `KYOSHIN_RECV_VIBRATION_INTERVAL_SEC` | 2.0 | 振動レベルの受信間隔（秒）。**最小2.0**（未満は2.0に切り上げ） |
| `KYOSHIN_VIBRATION_DETECT_LEVEL` | 100 | 振動レベルがこの値以上のとき、揺れを検知したと判定する（最小1） |
| `KYOSHIN_NOTIFY_ENABLE` | true | 強震モニタの通知機能の有効化。通知間隔＝有効な受信のうち最短の受信間隔 |
| `KYOSHIN_NOTIFY_ON_EEW` | true | EEW発表時（第一報〜最終報・キャンセル報、最長300秒）に通知するか。通知中は検知による通知を中断する（検知は継続） |
| `KYOSHIN_NOTIFY_ON_DETECT` | true | 揺れ検知時（画像解析・振動レベル）に通知するか。検知が終わるまで通知を続ける |
| `KYOSHIN_EVENT_TIMEOUT_SEC` | 45.0 | 最後の上昇トリガーからこの秒数経過でイベント終了。上げるほど余韻の通知が長く続く |
| `KYOSHIN_MIN_NOTIFY_PHASE` | Weak | 通知を送信する最小フェーズ（Weak＝弱い揺れ < Medium＝揺れ < Strong＝強い揺れ < Stronger＝非常に強い揺れ。旧設定の `Weaker` は `Weak` と同じ扱い） |
| `KYOSHIN_MIN_STATIONS_SHINDO0` | 4 | 揺れの強さが「弱い揺れ」（Weak）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_MIN_STATIONS_SHINDO1` | 2 | 揺れの強さが「揺れ」（Medium）以上の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_DEBUG_SAVE_IMAGE` | false | イベント確定時の元画像をローカル保存するか（事後検証用） |
| `KYOSHIN_DEBUG_IMAGE_DIR` | ./kyoshin_debug_images | デバッグ画像の保存先ディレクトリ |
| `KYOSHIN_SLOW_FETCH_THRESHOLD_SEC` | 0.5 | 画像のデコード＋観測点サンプリングの所要時間がこの秒数を超えたらWARNINGログを出す（Raspberry Pi等でのCPU負荷の把握用） |
| `KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC` | 300 | 画像デコード失敗（想定内の一時的事象）のログをこの秒数ごとに件数集約して1回だけWARNINGに出す（詳細は毎回DEBUGに出る） |
| `KYOSHIN_DETECT_VIBRATION_SOUND` | true | 強震モニタの揺れ検知の通知時に、EEW発表時の通知と同じ振動レベル音（`lv100`/`lv1000`/`lv2000`）を鳴らすか（EEW発表時の通知では常に鳴らす）。判定基準は`core.kyoshin_shared.vibration_tier`で共通化している。既に音声再生待ちがある間は積み増さない |

### 強震モニタ 誤検知対策の調整値（`.env.kyoshin`）
通常は変更不要。調整したい場合のみ `cp .env.kyoshin.example .env.kyoshin` して編集する。
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `KYOSHIN_ACTIVE_SHINDO_FLOOR` | 0.5 | 揺れ候補とみなす実震度の下限 |
| `KYOSHIN_RISE_THRESHOLD` | 1.0 | 「上昇トリガー」とみなす基準値との差分幅（震度の絶対値ではなく変化量で判定する） |
| `KYOSHIN_PATCH_RADIUS` | 0 | パッチサンプリング（恒久対策）の半径。パッチが大きいほど計算量が`(2r+1)^2`倍に増えるため、`KYOSHIN_SLOW_FETCH_THRESHOLD_SEC`のログを見ながら段階的に有効化すること |
| `KYOSHIN_PATCH_AGGREGATION` | median | パッチ内の集約方式。`median`（外れ値に強い）または`mean` |
| `KYOSHIN_BASELINE_WINDOW_START_SEC` | 10.0 | 基準値計算に使う過去サンプルの開始位置（秒前） |
| `KYOSHIN_BASELINE_WINDOW_END_SEC` | 25.0 | 基準値計算に使う過去サンプルの終了位置（秒前） |
| `KYOSHIN_HISTORY_WINDOW_SEC` | 25.0 | 観測点ごとに保持する震度履歴の長さ（秒）。BASELINE_WINDOW_END_SEC以上を推奨 |
| `KYOSHIN_STARTUP_WARMUP_SEC` | `KYOSHIN_BASELINE_WINDOW_END_SEC`と同じ（既定25.0） | 起動から検知（イベント新規生成・確定）を有効化するまでのウォームアップ秒数。0以下で無効化 |
| `KYOSHIN_NEIGHBOR_TRIGGER_COUNT` | 3 | 上昇トリガー確定に必要な、K近傍のうち同時に上昇トリガーが立っている観測点数 |

### 音声設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `TTS_ENGINE` | aquestalk | 読み上げエンジン（`aquestalk` / `scratchtts`） |
| `AQUESTALK_PATH` | （空） | AquesTalkPi の実行ファイルパス（未設定で音声無効。`TTS_ENGINE=aquestalk` 時のみ使用） |
| `AQUESTALK_SPEED` | 150 | AquesTalkPi の読み上げ速度 |
| `SCRATCHTTS_URL` | `https://synthesis-service.scratch.mit.edu/synth` | ScratchTTS のエンドポイント（`TTS_ENGINE=scratchtts` 時のみ使用） |
| `SCRATCHTTS_LOCALE` | ja-JP | ScratchTTS の言語ロケール |
| `SCRATCHTTS_GENDER` | female | ScratchTTS の声の性別（`female` / `male`） |
| `SCRATCHTTS_TIMEOUT_SEC` | 10 | ScratchTTS APIリクエストのタイムアウト秒数 |
| `FFMPEG_PATH` | ffmpeg | ScratchTTS のピッチシフトに使う ffmpeg のコマンド／パス（PATH が通っていない場合は絶対パス） |
| `AUDIO_PLAYER` | aplay | 音声再生コマンド（`aplay` / `mpg123` 等） |
| `SPEECH_QUEUE_MAXSIZE` | 200 | 音声読み上げキューの最大サイズ |
| `MP3_QUEUE_MAXSIZE` | 50 | MP3 再生キューの最大サイズ |

`TTS_ENGINE=scratchtts` では、取得した音声を ffmpeg で約3セミトーンピッチアップして再生する（`ffmpeg` が必要）。`--starter` で選ばなかった方のエンジンの設定行は `.env` 内でコメントアウトされる。

### ログ設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `LOG_LEVEL` | INFO | ログレベル（後方互換。FILE/CONSOLE 未設定時の既定値として使用） |
| `LOG_LEVEL_FILE` | LOG_LEVEL | ファイルへの出力ログレベル |
| `LOG_LEVEL_CONSOLE` | LOG_LEVEL | コンソールへの出力ログレベル |
| `LOG_FILE_PATH` | （空＝プロジェクトルート/qtlbot.log） | ログファイルの出力先パス。未設定時は `bot.py` と同じディレクトリの `qtlbot.log`（実行時のカレントディレクトリに依存しない） |
| `LOG_MAX_BYTES` | 10485760 | ログファイルの最大サイズ（バイト、デフォルト 10MB） |
| `LOG_BACKUP_COUNT` | 7 | ローテーション保持ファイル数 |
| `LOG_DUPLICATE_THRESHOLD` | 60 | 同一メッセージの重複抑制時間（秒）。ERROR 以上は常に出力 |
| `LOG_SUPPRESS_HTTP_SUCCESS` | true | aiohttp.access の 2xx 成功ログを抑制 |

### Web Dashboard 設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `WEB_DASHBOARD_ENABLED` | false | Web Dashboard の有効化 |
| `WEB_DASHBOARD_PORT` | 8080 | Web Dashboard のポート番号 |
| `WEB_DASHBOARD_HOST` | 127.0.0.1 | Web Dashboard がバインドするアドレス。このマシン以外からアクセスさせたい場合のみ `0.0.0.0` 等を指定する |
| `WEB_DASHBOARD_ALLOWED_IPS` | （空） | アクセスを許可するクライアントIPのカンマ区切りリスト（CIDR表記可）。空の場合はIP制限なし |
| `WEB_DASHBOARD_TOKEN` | （空） | アクセストークン。設定すると `/health` 以外の全ページ・APIで認証が必要になる。空の場合は認証なし |
| `STATUS_HISTORY_INTERVAL` | 300 | `GET /dashboard`・`GET /status/history` 用のスナップショット記録間隔（秒） |
| `STATUS_HISTORY_MAXLEN` | 288 | 保持するスナップショットの最大件数（古いものから自動破棄。デフォルトは300秒間隔で24時間分） |

`WEB_DASHBOARD_HOST=0.0.0.0` にする場合は、`WEB_DASHBOARD_ALLOWED_IPS` で許可する IP を絞ることを強く推奨する（絞らないと、ネットワークによっては外部から誰でも `/status` 等にアクセスできる）。

#### トークン認証（`WEB_DASHBOARD_TOKEN`）
- ブラウザ: 未ログインで `/dashboard` などを開くと `/login` に移動する。トークンを入力するとセッションクッキーが発行され、7 日間ログイン状態が続く。`/logout` でログアウトできる。
- `curl` など: `Authorization: Bearer <トークン>` ヘッダーを付ける。
  ```bash
  curl -H "Authorization: Bearer $WEB_DASHBOARD_TOKEN" http://localhost:8080/status | jq
  ```
- 認証不要なのは `/health`（`{"status": "online"}` のみ。外部の死活監視用）と `/login`・`/logout` だけ。`/health/full` を含む他はすべて認証が必要。
- クッキーにトークン自体は入らない（トークンから導出した値）。トークンを変更すると既存のセッションはすべて無効になる。
- 同一 IP からログインに 5 回失敗すると、5 分間そのIPからのログインを受け付けない。
- 16 文字以上のランダムな文字列を推奨する（例: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`）。
- クッキーは HttpOnly・SameSite=Lax。HTTPS 経由（リバースプロキシ等）のときだけ Secure も付く。素の HTTP で LAN 外へ公開する場合、トークンは平文で流れるため、`WEB_DASHBOARD_ALLOWED_IPS` や VPN との併用を推奨する。
- `WEB_DASHBOARD_ALLOWED_IPS` と併用できる（IP 制限が先に適用される）。

### Discord 送信の再送
通知（EEW・地震情報・津波・火山・USGS・長周期地震動など気象庁その他・地震感知情報）の送信が一時的に失敗したとき、自動で再送する（`core/discord_send.py`）。

- **再送する**: HTTP 503 などの Discord 側サーバーエラー、DNS 失敗・接続失敗・タイムアウトなどのネットワークエラー。待機は 2 秒 → 4 秒 → … の指数バックオフ（上限 10 秒）。
- **再送しない**: 権限不足（403）・チャンネル削除（404）・その他の 4xx、実装上の誤り。再送しても直らないため、従来どおり即座にエラーとして扱う。
- discord.py 自身も、レート制限（429）と HTTP 500・502・504・524 は内部で最大 5 回再試行している。ここでの再送は、それでカバーされない範囲を補うもの。
- 再送時も同じ `nonce` を使う。タイムアウトで失敗扱いになったが実際には届いていた場合でも、同じ通知が二重に投稿されない（Discord 側で重複排除される）。
- 最終的に失敗した場合は、従来どおりの例外・失敗記録（配信成功率に反映）になる。
- 対象外: `!status`・スラッシュコマンドの応答、管理者チャンネルへのエラー通知・ダイジェスト、強震モニタ画像（周期的に送り続けるため、古い画像の再送に意味がない）。
- 通知に添付する画像は `BytesIO` で渡すこと（パス指定の `discord.File` は送信後に閉じられ、再送できない）。

### Webhook 通知（Discord / Slack）
通知を、Discord チャンネルに加えて Webhook にも送れる（複数宛先に同時送信可）。別サーバー・別ワークスペースへの転送や、Discord が不調のときの別経路として使える。

```bash
# .env
WEBHOOK_URLS=https://discord.com/api/webhooks/xxx/yyy,https://hooks.slack.com/services/T/B/zzz
WEBHOOK_EVENTS=all            # または eew,tsunami のように種別を絞る
```

| 種別名 | 対象 |
|:---|:---|
| `eew` | 緊急地震速報 |
| `quake` | 地震情報 |
| `tsunami` | 津波 |
| `volcano` | 火山（噴火速報・噴火警報・火山情報） |
| `usgs` | USGS（海外の地震） |
| `kanchi` | 地震感知情報 |
| `other` | 長周期地震動・後発地震注意情報など気象庁のその他の情報 |

- **送る内容**: Discord の Embed に近い形。宛先のホストで自動判別する。Discord には `{"embeds": [...]}` のまま、Slack（`hooks.slack.com`）には Slack が受け付ける attachment 形式に変換して送る（太字・リンクも Slack 形式へ変換）。
- **Discord への通知とは独立**: 別タスクで送るため、Webhook の失敗・遅延が Discord への通知や EEW の音声に影響しない。逆に、Discord が不調で送信に失敗しても Webhook には届く。
- **送らないもの**: 地図などの添付画像（Webhook 側から参照できないため）。画像を除くと中身が無い通知は送らない。
- **テスト通知**: `--test_*` などのテスト通知も、通常の通知と同じく Webhook に送られる（タイトルに「【テスト】」が付く）。設定の動作確認に使える。
- **再送**: 429（`Retry-After` を尊重）・5xx・ネットワークエラーは最大 3 回（2 秒 → 4 秒、上限 10 秒）。それ以外の 4xx は再送しない。最終的に失敗しても WARNING ログのみで、Bot の動作は継続する。複数の通知が続くと、再送の都合で受信側に届く順序が前後することがある（EEW は「第N報」の表記で判別できる）。
- **宛先の指定は https の Discord / Slack の Webhook URL のみ**。他のホスト・http・認証情報付き・ポート指定付きの URL は、起動時に警告を出して無視する。
- **URL は秘密情報**（知っていれば誰でもその宛先へ投稿できる）。ログには一切出さず、宛先は「discord#1」「slack#2」のように番号で表示する。`.env` を公開しないこと。URL が漏れた場合は、Discord / Slack 側で Webhook を削除して作り直す。
- 起動時のログで、有効な宛先の数と種別を確認できる（例: `Webhook 通知: 有効（宛先2件: discord#1, slack#2 / 種別: all）`）。
- Slack 向けの送信は、テスト用の HTTP サーバーでの動作確認のみで、実際の Slack ワークスペースでの確認は行っていない。初回は `--test_*` などで実際に届くことを確認すること。

### 設定の変更（`/qtl_config`・再起動不要）
運用中に調整したい設定を、Discord から変更できる（管理者のみ）。変更は即座に反映され、`.env` にも書き戻されるため、再起動後も保持される。

```
/qtl_config list                              # 変更できる設定と現在値の一覧
/qtl_config set key:地震情報: 震度の下限 value:4   # 震度4未満の地震情報を通知しない
/qtl_config set key:津波情報 value:off          # 津波情報（P2P）の通知を止める
```

| 設定（`.env` のキー） | 値の指定 |
|:---|:---|
| `QUAKE_MIN_SCALE`（震度の下限） | `全て` / `1`〜`4` / `5弱` / `5強` / `6弱` / `6強` / `7`（`震度4`・`40` のような書き方も可） |
| `QUAKE_MIN_MAG`（Mの下限） | 0〜10 の数値（例: `3.5`） |
| `QUAKE_MIN_DEPTH` / `QUAKE_MAX_DEPTH`（深さの下限/上限・km） | 0〜9999 の整数（下限 ≤ 上限） |
| `QUAKE_ENABLE_SCALE_PROMPT` / `_DESTINATION` / `_SCALE_AND_DEST` / `_DETAIL_SCALE` / `_FOREIGN` / `_OTHER`（地震情報の種別ごと） | `on` / `off` |
| `TSUNAMI_ENABLE`（津波情報） | `on` / `off` |
| `ENABLE_LONG_PERIOD`（長周期地震動） | `on` / `off` |
| `ENABLE_ADVISORY`（後発地震注意情報等） | `on` / `off` |
| `GIS_MAP_THEME`（地図の配色。次に描画する地図から反映） | `light` / `dark` / `highlight` |

- **対象は、通知のたびに値を読む設定だけ。** 起動時にタスクやチャンネルを組み立てる設定（`ENABLE_KYOSHIN`・各チャンネルID・WebSocket 設定など）は、反映に再起動が必要なため対象外。
- **`.env` への保存に失敗した場合（権限不足・ディスク満杯など）は、設定を変更しない。** 「画面上の値」と「再起動後の値」がずれないようにするため。
- `.env` への書き込みは、変更したキーの行だけを置き換える（他の行・コメント・順序、行末のコメントは保持）。書き込みはアトミックに行い、元のパーミッション（600 など）を引き継ぐ。書き込み前に `.env.bak`（1世代・上書き）へバックアップする（`.gitignore` 済み）。
- `systemd` の `EnvironmentFile` や環境変数で同じキーを直接渡している場合、再起動後はそちらが優先され、`.env` への書き戻しは反映されない。
- `!status` / `/qtl_status` の「フィルター」欄は、変更後の値を表示する。

### ステータス表示設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `STATUS_SHOW_CPU` | true | !status / /qtl_status で CPU 使用率を表示 |
| `STATUS_SHOW_MEM` | true | !status / /qtl_status でメモリ使用量を表示 |
| `STATUS_SHOW_DISK` | true | !status / /qtl_status でディスク使用率を表示 |
| `STATUS_SHOW_UPTIME` | true | !status / /qtl_status で稼働時間を表示 |

### !status / /qtl_status の実行権限
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `QTL_STATUS_ACCESS` | admin | `!status` と `/qtl_status`（稼働状態）を実行できる範囲。`admin`＝サーバー管理者のみ / `all`＝全ユーザー / `role`＝指定ロール（＋管理者） |
| `QTL_STATUS_ROLE_IDS` | （空） | `role` モードで許可するロール ID（カンマ区切り） |
| `QTL_STATUS_HISTORY_ACCESS` | admin | `/qtl_status history:...`（履歴表示）を実行できる範囲。上記とは独立して設定できる |
| `QTL_STATUS_HISTORY_ROLE_IDS` | （空） | 履歴表示で `role` モードのとき許可するロール ID |

- `role` モードでも管理者は常に実行できる。`role` なのにロール ID が未設定の場合や、不正な値の場合は、警告ログを出して `admin` として扱う。
- 履歴オプションを指定したときは `QTL_STATUS_HISTORY_ACCESS` だけで判定する（`QTL_STATUS_ACCESS` が `admin` でも、履歴だけ全員に許可できる）。
- ダイレクトメッセージ上では管理者とみなさないため、`admin` / `role` モードでは実行できない。
- 権限の設定を変更した場合は、Bot の再起動で反映される（起動時にスラッシュコマンドを再同期する）。

### リソース監視設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `RESOURCE_MONITORING_ENABLED` | true | リソース監視の有効化 |
| `RESOURCE_CHECK_INTERVAL` | 3600 | 監視間隔（秒） |
| `DISK_WARNING_THRESHOLD` | 80 | ディスク WARNING 閾値（%） |
| `DISK_ERROR_THRESHOLD` | 90 | ディスク ERROR 閾値（%） |

### APM (Mackerel 連携) 設定
| 変数名 | 既定値 | 説明 |
| :--- | :--- | :--- |
| `APM_ENABLED` | false | APM（トレーシング）連携の有効化。デフォルト無効 |
| `APM_SERVICE_NAME` | QTL_Bot | Mackerel 上で表示されるサービス名 |
| `APM_MACKEREL_API_KEY` | （空） | Mackerel の API キー。`APM_ENABLED=true` 時は必須 |
| `APM_OTLP_ENDPOINT` | `https://otlp-vaxila.mackerelio.com/v1/traces` | OTLP 送信先エンドポイント。`core/apm.py` はパスを追加結合しないため、変更する場合は末尾を必ず`/v1/traces`にすること |
| `APM_OTLP_API_KEY_HEADER` | `Mackerel-Api-Key` | API キーを送る HTTP ヘッダー名（公式ドキュメントで要確認） |

> `APM_OTLP_ENDPOINT` と `APM_OTLP_API_KEY_HEADER` の既定値は、OTLP の慣例に基づく暫定値。`APM_ENABLED=true` にする前に公式ドキュメントで実際の値を確認し、異なる場合は `.env` で上書きすること。

---

## 依存パッケージの管理

| ファイル | 役割 |
|:---|:---|
| `requirements.txt` | 最低限必要な版（下限）の指定。開発用ツール（pytest・flake8 など）を含む |
| `requirements.lock` | 版を推移的依存まで固定したもの（開発用ツールは含まない）。`pip-audit` で既知の脆弱性が無いことを確認した版（更新履歴はファイル冒頭） |

- 本番機のセットアップは `pip install -r requirements.lock` を使う（版が変わって挙動が変わるのを防ぐ）。
- `requirements.lock` の行末の `; python_version >= "3.13"` は環境マーカーで、該当しない Python では無視される。

### 脆弱性・版ずれの確認（`--audit_deps`）
```bash
pip install pip-audit              # 初回のみ（requirements には含まない任意ツール）
python3 bot.py --audit_deps
```
- `.env` が無くても実行できる。Bot 本体は起動しない。
- 実際にインストールされている版が `requirements.lock` と異なる場合は一覧表示する（情報表示のみ）。
- `pip-audit` で、ロックされた各パッケージを既知の脆弱性情報（PyPI の脆弱性DB）と照合する。PyPI への問い合わせを行うため、数十秒かかる。
- 終了コード: `0`=問題なし / `1`=脆弱性あり / `2`=確認できなかった（`pip-audit` 未導入・通信失敗など）。cron で定期実行して終了コードを確認する使い方ができる。
  ```bash
  # 例: 毎週月曜 9:00 に確認し、結果をファイルへ
  0 9 * * 1 cd ~/QTL_Bot && python3 bot.py --audit_deps >> ~/qtl_audit.log 2>&1
  ```

### 版を更新するとき
1. 新しい版を試し、`pytest` と実機で動作確認する。
2. `requirements.lock` の版番号を書き換える（推移的依存も、`pip install` で入った版に合わせる）。
3. `python3 bot.py --audit_deps` で脆弱性が無いことを確認する。
4. コミットする。

---

## Web Dashboard

`WEB_DASHBOARD_ENABLED=true` で有効化する（既定 `127.0.0.1:8080`）。

| エンドポイント | 内容 |
|:---|:---|
| `GET /status` | 稼働状況の JSON（`system`・`eew`・`api_status`・`recv_count`・`monitoring`・`tasks`・`delivery`・`memory_usage_mb` など） |
| `GET /health` | 軽量ヘルスチェック（`{"status": "online"}`） |
| `GET /health/full` | Wolfx・JMA・P2P（・USGS）の疎通確認（`overall_status`・各 API の `ok`／`latency_ms`／`error`）。30秒キャッシュ |
| `GET /status/history` | CPU・メモリ・ディスク・受信件数の推移（`STATUS_HISTORY_INTERVAL` 秒ごと、メモリ上のみ）。`?format=csv` で CSV（UTF-8 BOM付き） |
| `GET /status/notifications` | 実際に送信した通知の履歴（`?limit=N`。`qtlbot.log` にも記録され再起動後も残る） |
| `GET /status/quake_history`・`/status/eew_history` | `qtlbot.log` ベースの地震情報・EEW の履歴（座標つき） |
| `GET /quake_map` | 地震情報・EEW の履歴を地図（Leaflet）と表で閲覧（日付・震度で絞り込み。国境・区域データ `GET /status/gis_countries`・`/status/gis_local_areas` を背景に使う） |
| `GET /dashboard` | 上記を可視化する HTML ページ（ステータス、推移グラフ、通知履歴、CSV ダウンロード） |

```bash
curl http://localhost:8080/status | jq
curl http://localhost:8080/health/full | jq
```

### Discord コマンド
| コマンド | 種別 | 権限 | 説明 |
|:---|:---|:---|:---|
| `!status` | プレフィックス | `QTL_STATUS_ACCESS`（既定: 管理者） | Bot の稼働状態を Embed で表示 |
| `/qtl_status` | スラッシュ | `QTL_STATUS_ACCESS`（既定: 管理者） | `!status` と同じ内容 |
| `/qtl_status history:地震情報の履歴` | スラッシュ | `QTL_STATUS_HISTORY_ACCESS`（既定: 管理者） | 直近の地震情報を表示（`count` で 1〜10 件、既定 5 件）。実行者本人にのみ表示 |
| `/qtl_status history:緊急地震速報(EEW)の履歴` | スラッシュ | `QTL_STATUS_HISTORY_ACCESS`（既定: 管理者） | 直近の EEW を表示（同一イベントは最新の報にまとめる）。実行者本人にのみ表示 |
| `/qtl_config list` | スラッシュ | 管理者のみ | 再起動なしで変更できる設定と現在値を表示（実行者本人にのみ表示） |
| `/qtl_config set` | スラッシュ | 管理者のみ | 設定を変更し `.env` に保存する。再起動は不要（実行者本人にのみ表示） |

履歴は `qtlbot.log*` から復元する（Web ダッシュボードの `/status/quake_history`・`/status/eew_history` と同じデータ）。

表示内容: システムリソース／EEW 状態／各 API の受信状況／タスク稼働状態／通知先チャンネルマッピング／フィルター設定。

### 起動オプション
| オプション | 内容 |
|:---|:---|
| `--starter` | 対話式セットアップウィザード（`.env` を生成） |
| `--merge_env [--yes]` | `.env` へ不足項目を追加 |
| `--check_env` | `.env.example` とコードの整合性チェック |
| `--audit_deps` | `requirements.lock` の版ずれ確認と既知の脆弱性の照合（要 `pip install pip-audit`） |
| `--refresh_gis_data` | GIS データの強制再取得 |
| `--backfill_quake_history` | 過去の地震情報を P2P API から `qtlbot.log` へ補完 |
| `--test_*` | CLI テスト（次節） |

---

## CLIテスト

実際の Bot プロセス上で、サンプル JSON を使って特定の Cog の通知ロジック（Embed 生成・読み上げ・効果音）だけを動かし、実チャンネルで確認できる。

```bash
python3 bot.py --test_<対象> <JSONファイルパス>
```

Bot は通常通り起動し、全 Cog の `on_ready` 完了後にテストを1回実行して終了する。通知には「【テスト】」の接頭辞と「※これはテスト通知です。」のフッターが付く（`is_test` 未対応の関数は JSON のタイトル系フィールドへ接頭辞を動的に付与する）。Web Dashboard は起動しないため、本番プロセスとのポート競合は起きない。

### 対象の自動判定（`--test_auto`）
どの `--test_<対象>` を使うか分からない JSON は、構造から判定して実行できる。

```bash
python3 bot.py --test_auto path/to/downloaded.json
```

候補が1件なら自動実行、0件なら「判定不能」と表示して何もせず、2件以上なら候補を表示して実行しない。

### 一括実行（`--test_all`）
```bash
python3 bot.py --test_all tests/fixtures/
```
`<fixtures_dir>/<対象>_sample.json` を探して存在するものだけ順に実行する（無い対象は SKIP、壊れた JSON もその対象だけスキップ）。最後に OK／NG／SKIP のサマリーを表示する。`ews` は JSON 不要で常に実行される。

### 入力 JSON の扱い
- **配列形式**: P2P の `/v2/history` や気象庁の `list.json` の応答は `[ {...} ]` の配列。そのまま保存した JSON は、先頭の1件を取り出して使う（2件以上なら注意を表示）。空配列・オブジェクト以外はエラー終了する。
- **取り違えの検知**: 対象が期待するフィールドが JSON に無い場合は警告を出す（実行は続ける）。
- **打ち間違いの防止**: `--test` で始まる未知の引数は、Bot を起動せず候補付きで終了する（終了コード2）。引数の前方一致（省略形）は使えないので、`--test_eew_p2p` などは正式名で指定する。
- **fixtures**: `*.json` は `.gitignore` の対象のため `tests/fixtures/` はリポジトリに含まれない。気象庁の実データ（`list.json` から辿れる詳細 JSON）や過去の通知の元データを自分で保存して使う。

### 対応している `--test_<対象>`
| 対象 | 呼び出し先 | 入力 JSON |
|:---|:---|:---|
| `eew` | `EewCog.notify_eew` | Wolfx 形式（変換済みのフラット形式） |
| `eew_p2p` | `EewCog.notify_eew`（`convert_p2p_eew_to_wolfx` で変換後） | P2P の生EEW（code=556） |
| `quake` | `QuakeInfoCog.notify_quake` | P2P 地震情報（code=551）／気象庁HP JSON の地震情報（震度速報・震源・各地の震度等）／Quake.One の `info.json`（同じフォルダに `smallScalePoints.json` があれば細分区域別の震度も読む）。後ろ2つは代替取得と同じ変換を通す |
| `tsunami` | `TsunamiCog.notify_tsunami` | P2P 津波情報（code=552） |
| `tsunami_forecast` | `TsunamiCog.notify_tsunami_forecast` | 気象庁 VTSE41（津波予報・警報） |
| `tsunami_observation_coastal` | `TsunamiCog.notify_tsunami_observation_coastal` | 気象庁 VTSE51（津波観測） |
| `tsunami_observation_offshore` | `TsunamiCog.notify_tsunami_observation_offshore` | 気象庁 VTSE52（沖合の津波観測） |
| `tsunami_observation_tide` | `TsunamiCog.notify_tsunami_observation_tide` | 各地の満潮時刻・津波到達予想時刻 |
| `tsunami_observation` | `TsunamiCog.notify_tsunami_observation` | 上記以外の観測系（汎用フォールバック） |
| `nankai_trough` | `OtherInfoCog.notify_nankai_trough` | 南海トラフ地震臨時情報の詳細 JSON |
| `hypocenter_update` | `OtherInfoCog.notify_hypocenter_update` | 震源要素更新のお知らせの詳細 JSON |
| `other_long_period` / `other_long_period_detail` | `OtherInfoCog.notify_long_period` | 長周期地震動の list の1エントリ／詳細 JSON |
| `other_quake_advisory` / `other_quake_advisory_detail` | `OtherInfoCog.notify_quake_advisory` | `quake/data/list.json` の1エントリ／詳細 JSON |
| `jishin_kanchi` | `JishinKanchiCog.notify_jishin_kanchi` | P2P 地震感知情報（code=9611） |
| `volcano` | `VolcanoCog._notify_volcano` | 火山情報の詳細 JSON |
| `volcano_eruption` / `volcano_warning` | `VolcanoCog._notify_eruption` / `_notify_warning` | `eruption.json`／`warning.json` の1エントリ |
| `usgs` | `UsgsCog.notify_usgs_quake` | USGS GeoJSON の feature |
| `ews` | `QuakeInfoCog._play_ews_signal` | 不要。`--test_ews [Warning\|MajorWarning]`（既定 MajorWarning） |

```bash
python3 bot.py --test_eew tests/fixtures/eew_sample.json
python3 bot.py --test_ews Warning
```

火山情報・噴火速報・噴火警報・USGS の通知関数は `is_test` を持たないため、テスト実行時の通知も通知履歴に記録される。

### 単体テスト（pytest）
Discord 接続や環境変数なしで動くテストが `tests/` にある（テストデータは `*.json` が `.gitignore` 対象のため、テストコード内の辞書で持つ）。

```bash
python3 -m pytest tests/ -q
```

| ファイル | 対象 |
|:---|:---|
| `test_apm_expected_errors.py` | APM の想定内エラー（Timeout／Cancelled）の扱い |
| `test_eew_forecast_label.py` | EEW 予想震度の見出し |
| `test_eew_region_resolve.py` | EEW の未知の地域名の解決・GIS 区域名の読み替え |
| `test_env_merge.py` | `--merge_env` |
| `test_gis_overseas_switch.py` | 遠地地震向け地図への自動切り替え |
| `test_gis_theme_icon.py` | 地図の配色テーマ・震度アイコン |
| `test_gis_thread_safety.py` | 描画倍率のスレッド分離 |
| `test_kyoshin_notify.py` | 強震モニタの受信・通知の集約 |
| `test_quake_failover.py` | P2P 障害時の代替取得 |
| `test_kyoshin_phase.py` | 強震モニタ検知の揺れの強さ（4段階）・`latest.json` の配信元 |
| `test_quake_test_input.py` | `--test_quake` の気象庁JSON／Quake.One入力・観測点名の「＊」照合・代替取得の既定の取得元 |
| `test_dependency_audit.py` | `--audit_deps`（ロックファイルのパース・版ずれ検出・pip-audit の終了コード・`requirements.lock` の整合） |
| `test_env_writer.py` | `.env` の指定キーだけの安全な書き換え（行の保持・アトミック書き込み・拒否する値） |
| `test_webhook_notify.py` | Webhook 通知（URL 検証・Embed 変換・再送・複数宛先・種別フィルター・Discord 送信との独立性） |
| `test_runtime_settings.py` | 再起動不要の設定変更（入力解釈・保存失敗時・再起動後の読み戻し）と `/qtl_config` コマンド |
| `test_discord_send.py` | Discord 送信の再送（再送対象・nonce 固定・指数バックオフ・添付ファイルの巻き戻し） |
| `test_dashboard_auth.py` | Web ダッシュボードのトークン認証（Bearer・ログイン/クッキー・リダイレクト対策・総当たり対策） |
| `test_command_access.py` | `!status` / `/qtl_status` の実行権限（モード・ロール・DM・既定権限） |
| `test_status_history_view.py` | `/qtl_status` 履歴オプションの表示整形 |
| `test_error_notify_handler.py` | ERROR ログの管理者通知（対象レベル・再帰防止・スレッド・例外抽出） |
| `test_startup_guard.py` | 起動時の既存情報の扱い |
| `test_test_runner_input.py` | CLI テストの入力 JSON・引数の扱い |
| `test_tsunami_speech.py` | 津波の読み上げ文・二重読み上げ防止 |
| `test_usgs_dedupe.py` | USGS の重複通知防止・`--test_quake` の入力検証 |

---

## 仕様メモ

### 火山情報
| 種別 | URL |
|:---|:---|
| 火山情報リスト | `https://www.jma.go.jp/bosai/volcano/data/info.json` |
| 火山情報詳細 | `https://www.jma.go.jp/bosai/volcano/data/info/{eventId}.json` |
| 噴火速報リスト | `https://www.jma.go.jp/bosai/volcano/data/eruption.json` |
| 噴火警報リスト | `https://www.jma.go.jp/bosai/volcano/data/warning.json` |

- **差分検知**: `info.json` の各エントリの `eventId` が新規、または `reportDatetime` が更新されたものを通知する。噴火速報・噴火警報はリストの末尾（最新）の `eventId` の変化で判定する。
- **噴火警戒レベルの色**: L1（活火山であることに留意）紫、L2（火口周辺規制）赤、L3（入山規制）橙、L4（居住地域避難準備）黄、L5（居住地域への避難）青。

### 津波情報の色
Embed の色と GIS 地図の沿岸線色は `TSUNAMI_COLOR_*` で設定できる（既定: 大津波警報 `0xD344FC`、津波警報 `0xF93022`、津波注意報 `0xEEDB2D`、津波予報 `0x80FFFF`、不明 `0x56BCFC`、解除は緑）。

---

## トラブルシューティング

### 火山情報が通知されない
1. `VOLCANO_CHANNEL_ID`（未設定なら `CHANNEL_ID`）と Bot の送信権限を確認する。
2. 状態を確認する（`Volcano: no change` は DEBUG レベルのログのため、`LOG_LEVEL_FILE=DEBUG` が必要）。
   ```bash
   tail -f qtlbot.log | grep -i volcano
   curl http://localhost:8080/status | jq '.monitoring.volcano'
   ```

### EEW が届かない
```bash
curl http://localhost:8080/status | jq '.eew'
# wolfx の ws_status が timeout → Wolfx WebSocket の再接続待ち
# p2p_eew の recv_count が増えていれば P2P EEW は正常
```

### タスクが停止している
```bash
curl http://localhost:8080/status | jq '.tasks'
# "[NG] エラー停止" → Bot を再起動する
```

### USGS 地震情報が届かない
```bash
curl http://localhost:8080/status | jq '.monitoring.usgs'
# enabled が false → USGS_ENABLED=true を設定
```

### 強震モニタ画像解析検知が通知されない
1. `ENABLE_KYOSHIN=true` であること、Pillow が入っていること（`pip list | grep -i pillow`）を確認する。
2. 観測点データの取得を確認する: `grep -i KyoshinStations qtlbot.log`（取得に一度も成功していないとキャッシュ `KYOSHIN_STATIONS_CACHE_PATH` も無く、検知が実質無効になる。ネットワークと `KYOSHIN_STATIONS_SOURCE_URL` の到達性を確認）。
3. 検知の状態を確認する: `grep -i kyoshin qtlbot.log`。イベントが生成されているのに通知が来ない場合は `KYOSHIN_MIN_STATIONS_SHINDO0`／`SHINDO1`・`KYOSHIN_MIN_NOTIFY_PHASE` を確認する。特定の観測点の警告が繰り返し出るときは、機器異常としてブラックリスト化されている可能性がある（「ブラックリスト化しました」で検索）。
4. 通知設定を確認する: `KYOSHIN_NOTIFY_ENABLE`・`KYOSHIN_NOTIFY_ON_DETECT`・`KYOSHIN_NOTIFY_ON_EEW`、`KYOSHIN_RECV_*` が1つ以上有効か。EEW 通知中は検知通知が中断される。
5. 誤検知・未検知を後から確認したいときは `KYOSHIN_DEBUG_SAVE_IMAGE=true` にして `KYOSHIN_DEBUG_IMAGE_DIR` の画像を見る。通知が続く時間は `KYOSHIN_EVENT_TIMEOUT_SEC`（既定45）で調整する。

### メモリ・ネットワーク
- メモリ使用量は `/status` の `memory_usage_mb` や `!status` で確認できる（目安は 80〜100MB 程度。大きく超える場合は要調査）。
- API 接続エラーが続く場合は、ネットワークと各 API（気象庁・P2P・Wolfx・USGS）の稼働状況を確認する。

---

## コード構成

### ディレクトリ構成
```
QTL_Bot/
├── bot.py                    エントリーポイント（Cog 登録・起動）
├── requirements.txt
├── .env.example / .env.kyoshin.example
├── region_map.json           EEWの警報地域名 → 表示用地域名
├── prefecture_map.json       区域名 → 都道府県名（震度速報の読み上げ等）
├── pref_region_map.json      府県予報区名 → 地方予報区名（EEWの集約表示）
├── epsp_area.csv             P2P地震情報の地域コード → 地域名
├── hokkaido_bosaitaiou_area.png / nankai_bosaitaiou_area.png   後発地震・南海トラフ情報の区域図
├── cogs/
│   ├── apm.py                ApmCog: Mackerel APM 連携
│   ├── audio_shared.py       AudioCog: 音声読み上げ・MP3再生の実体（EewCog/QuakeInfoCog が共有）
│   ├── eew.py                EewCog: 緊急地震速報（Wolfx/P2P）
│   ├── quake.py              QuakeInfoCog: 地震情報・P2P障害時のフォールバック
│   ├── tsunami.py            TsunamiCog: 津波情報・観測・予報、EWS 信号音
│   ├── jishin_kanchi.py      JishinKanchiCog: P2P地震感知情報
│   ├── volcano.py            VolcanoCog: 火山情報・噴火速報・噴火警報
│   ├── usgs.py               UsgsCog: USGS 海外地震情報
│   ├── other.py              OtherInfoCog: 長周期地震動・気象庁その他情報
│   ├── system.py             SystemCog: !status・Web Dashboard・エラー監視・リソース監視・ダイジェスト
│   └── kyoshin_monitor.py    KyoshinMonitorCog: 強震モニタ・振動レベルの受信・通知・画像解析検知
├── core/
│   ├── config.py             環境変数の読み込み・定数
│   ├── constants.py          震度色・各種ラベル
│   ├── env_loader.py         .env と .env.kyoshin の読み込み
│   ├── env_starter.py        --starter（対話式セットアップ）
│   ├── env_merge.py          --merge_env
│   ├── env_audit.py          --check_env
│   ├── test_runner.py        --test_*（CLI テスト）
│   ├── logging_setup.py      ログ設定（ローテーション・重複抑制）
│   ├── command_access.py        !status / /qtl_status の実行権限判定
│   ├── dashboard_auth.py        Web ダッシュボードのトークン認証（Bearer・ログイン・クッキー）
│   ├── discord_send.py          Discord 送信の再送ヘルパー（一時的な失敗のみ再送）
│   ├── webhook_notify.py        Webhook 通知（Discord / Slack・複数宛先）
│   ├── runtime_settings.py      再起動不要で変更できる設定の定義・現在値・変更処理（/qtl_config）
│   ├── env_writer.py            .env の指定キーだけを安全に書き換える
│   ├── dependency_audit.py      --audit_deps（版ずれ確認・脆弱性照合）
│   ├── status_history_view.py   /qtl_status 履歴オプションの表示整形
│   ├── error_notify_handler.py  ERROR ログ → 管理者チャンネル通知のログハンドラー
│   ├── qtlbot_log_files.py   qtlbot.log とローテーション世代の列挙
│   ├── pid_guard.py          二重起動の防止
│   ├── cog_utils.py          Cog 共通のユーティリティ
│   ├── helpers.py            共通ヘルパー（時刻整形・震度変換など）
│   ├── audio.py              AudioMixin／AudioClientMixin（読み上げ・MP3・EWS のキュー）
│   ├── tts_engines.py        TTS エンジン（AquesTalkPi／ScratchTTS）
│   ├── ews_signal.py         EWS の AFSK 信号音の合成
│   ├── ws_helpers.py         WebSocket 自動再接続の共通ループ
│   ├── p2p_ws_hub.py         P2P WebSocket の一元接続・code 別配信
│   ├── quake_failover.py     P2P 障害時の代替取得（気象庁JSON/XML・Quake.One）と重複防止
│   ├── fetch_backoff.py      HTTP ポーリングの Circuit Breaker（現在どこからも使われていない）
│   ├── startup_guard.py      起動時の「既存情報」判定の補助
│   ├── eew_convert.py        P2P EEW → Wolfx 形式の変換、予想震度の整形（純関数）
│   ├── tsunami_speech.py     津波の読み上げ文・状態変化の分類・二重読み上げ防止（純関数）
│   ├── jishin_kanchi_convert.py  地震感知情報の信頼度変換（純関数）
│   ├── epsp_area.py          地域コード → 地域名
│   ├── gis_data.py           GIS データの取得・キャッシュ・簡略化国境の生成
│   ├── gis_render.py         GIS 地図の描画（Pillow）
│   ├── gis_discord.py        複数の地図画像を Embed にする kwargs 組み立て
│   ├── notification_log.py   通知履歴（メモリ＋qtlbot.log）
│   ├── quake_history_log.py / eew_history_log.py   地震情報・EEW 履歴の記録と読み出し
│   ├── quake_history_backfill.py   --backfill_quake_history
│   ├── delivery_stats.py     通知送信の成功率の統計
│   ├── kyoshin_shared.py     震度色分け・画像取得・振動レベル取得の共通処理
│   ├── kyoshin_stations.py   実観測点データの取得・K近傍・色→震度変換
│   ├── kyoshin_image_analyzer.py   画像→震度変換（EEW 時の最大震度推定）
│   ├── kyoshin_detector.py   揺れ検知イベントの状態機械（EventManager）
│   ├── kyoshin_cluster_tracker.py  旧グリッド方式の連結成分解析・持続確認（現在どこからも使われていない）
│   └── kyoshin_image_monitor.py    イベントと連動した画像監視ループ
└── tests/                    pytest（上記「単体テスト」）
```

### Cog 責務一覧
| Cog | ファイル | 主な責務 |
|:---|:---|:---|
| `ApmCog` | `cogs/apm.py` | OpenTelemetry 計装・Mackerel OTLP 送信（既定無効） |
| `AudioCog` | `cogs/audio_shared.py` | 読み上げ・MP3 再生の実体 |
| `EewCog` | `cogs/eew.py` | Wolfx WebSocket・P2P EEW の受信・通知、EEW 発表中の状態管理（`monitored_event_id`） |
| `QuakeInfoCog` | `cogs/quake.py` | P2P 地震情報の受信・通知、障害時の代替取得 |
| `JishinKanchiCog` | `cogs/jishin_kanchi.py` | P2P 地震感知情報（既定無効） |
| `TsunamiCog` | `cogs/tsunami.py` | P2P 津波情報の受信、気象庁津波 JSON のポーリング、EWS 信号音 |
| `VolcanoCog` | `cogs/volcano.py` | 火山情報・噴火速報・噴火警報のポーリング |
| `UsgsCog` | `cogs/usgs.py` | USGS のポーリング・海外地震の通知 |
| `OtherInfoCog` | `cogs/other.py` | 長周期地震動・後発地震注意情報・南海トラフ・震源要素更新 |
| `SystemCog` | `cogs/system.py` | Web Dashboard・`!status`・エラー通知・リソース監視・ダイジェスト |
| `KyoshinMonitorCog` | `cogs/kyoshin_monitor.py` | 強震モニタ・LMoni・振動レベルの受信と通知、画像解析検知 |

### 主要な処理
| 関数・クラス | 説明 |
|:---|:---|
| `EewCog.connect_eew_ws()` / `handle_p2p_eew()` | Wolfx WebSocket の受信／P2P EEW の受信（ハブから配信） |
| `QuakeInfoCog.handle_p2p_quake()` / `TsunamiCog.handle_p2p_tsunami()` | P2P の地震情報・津波情報の受信 |
| `TsunamiCog.fetch_tsunami_observation()` | 気象庁津波 JSON のポーリング（60秒） |
| `OtherInfoCog.fetch_long_period()` / `fetch_quake_advisory()` | 長周期地震動・気象庁その他情報のポーリング |
| `UsgsCog.fetch_usgs_quake()` | USGS のポーリング |
| `VolcanoCog.fetch_volcano_info()` / `fetch_eruption_info()` / `fetch_warning_info()` | 火山情報・噴火速報・噴火警報のポーリング |
| `EewCog._eew_monitor_timeout()` | EEW 第一報から最長300秒で `monitored_event_id` を解除 |
| `notify_*()` | 各通知（Embed 生成・読み上げ・効果音） |
| `P2PWebSocketHub` | P2P WebSocket の一元管理と code 別配信 |
| `QuakeFailoverController` | P2P 障害時の代替取得 |
| `convert_p2p_eew_to_wolfx()` | P2P EEW → Wolfx 形式の変換（`core/eew_convert.py`） |
| `record_notification()` | 通知履歴への記録（各 Cog の `notify_*` から呼ぶ） |
| `reported_since_start()` | 発表時刻が Bot 起動後かの判定（`core/startup_guard.py`） |
| `EventManager.ingest()` / `tick()` | 強震モニタの上昇トリガー判定／K近傍の確認・イベントの生成・終了 |

---

## ライセンス
MIT License

## 謝辞
- 気象庁（JMA）API・防災情報
- Wolfx EEW 配信サービス
- P2P 地震情報
- 米国地質調査所（USGS）
- Quake.One
- [Ichihai1415/JMA-GIS-GeoJSON](https://github.com/Ichihai1415/JMA-GIS-GeoJSON)・[datasets/geo-countries](https://github.com/datasets/geo-countries)
- [ingen084/kyoshin-monitor-observation-points](https://github.com/ingen084/kyoshin-monitor-observation-points)

---

**最終更新**: 2026-10-10
**対応 Python**: 3.11+
