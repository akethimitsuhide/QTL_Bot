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
  - Wolfx と同時並行稼働。EventIDごとの最大Serial番号を管理し、同一ソース内での重複/逆行メッセージを排除する（Wolfx・P2Pそれぞれ独立に管理しており、互いの処理状況が他方の通知を抑制することはない。詳細は「EEWの重複排除について」参照）
  - 地図画像: P2Pメッセージの `id` から地震情報通知と同じ生成方法で地図画像URLを組み立て、Embed最下部に添付する（Wolfx由来のEEWには画像IDが存在しないため対象外。2026-08-19追加）
  - **PLUM法（震源要素を経由せず、観測波形から直接各地点の震度を推定する気象庁の緊急地震速報手法）による予測時、マグニチュード・深さが「推定なし」になるのは仕様通り**。PLUM法はそもそも震源の規模・位置を推定しないため、マグニチュード・深さが不明のまま、震度だけが（「7以上」のような具体的な値も含めて）観測波形から直接推定される。震源が未確定でも震度は判明する、という一見矛盾した組み合わせがPLUM法の正常な出力
  - `isAssumption`（PLUM法フラグ）の判定は、地震オブジェクト直下の`condition`（`"仮定震源要素"`）のみを見る（**2026-08-31修正**）。以前は個別エリアの`kindCode=19`（そのエリアの震度推定にPLUM法を使用）が1つでもあれば地震全体をPLUM法扱いにしていたため、辺縁部の一部エリアだけがPLUM法推定でも、既に確定しているはずのマグニチュード・震源情報全体が「推定なし」表示に潰される不具合があった
- **地震情報**: P2P 地震情報の API からの確報情報
  - 震度速報（`ScalePrompt`）: 読み上げは `prefecture_map.json` で区域名を都道府県名に変換し、重複を排除して発表（例:「熊本県天草・芦北」「熊本県熊本」→「熊本県」1回のみ）。通知本文の津波記述の下に区域別の震度一覧（■ 震度○ + 区域名）を追記
  - 震源に関する情報（`Destination`）: 震源地の横に度分秒形式の緯度経度を常に付記（例:「熊本県天草・芦北地方（32°33′39.8、130°22′43.2）」）
  - 各地の震度に関する情報（`ScaleAndDestination` / `DetailScale`）: 最大震度3以上の場合、読み上げに最大震度を観測した地点名を追加。最大震度〜1階級下までの観測点一覧を追記（震度46＝推定5弱以上は45と同階級として扱う）。1階級下の観測点数が `QUAKE_INTENSITY_COLLAPSE_THRESHOLD`（デフォルト10）件以上の場合、通知文の肥大化を防ぐため都道府県ごとに1地点だけを代表として表示し「（以下略）」を付ける
  - **【地域ごとの予想震度】の下限値／上限値のどちらかが不明なエリアの表示（2026-09-02修正）**: EEW（P2P生形式）のエリアには、推定精度上の理由で震度の下限（`scaleFrom`）と上限（`scaleTo`）のうち一方だけが判明しているケースがある（例: 全体の「予想最大震度」は`scaleTo=99`＝「7以上」由来で判明しているのに、そのエリア自体の`scaleFrom`は`-1`＝不明）。以前は下限（`Shindo1`）が「不明」のエリアを地域内訳から丸ごと除外していたため、「全体の予想最大震度は7以上なのに、地域ごとの内訳には6弱・5強までしか出てこない」という不整合が生じていた。下限・上限のうち少なくとも一方が判明していれば表示するよう`core/eew_convert.py`の`build_forecast_groups`を修正した（単独EEW通知・複数EEWサマリー通知の両方に適用される共通関数）
  - 地図画像: メッセージ送信後にバックグラウンドで P2P 地震情報 CDN をポーリングし、画像が実際に取得可能になった時点で Embed に画像を追加（詳細は下記「P2P 地図画像の添付」参照）
  - 津波の有無（`domesticTsunami`）の表記: P2P 地震情報の `domesticTsunami` は速報段階の推定値であり、`Warning` が返っても実際に気象庁から「津波警報」が正式発表されているとは限らない。誤解を招く断定的な表記を避けるため、`Watch`・`Warning` は通知本文で「津波警報・注意報を発表中」、読み上げで「現在、津波予報等を発表中です。」とまとめて表現する。`NonEffective` は通知「若干の海面変動（被害の心配なし）」、読み上げ「この地震で、若干の海面変動があるかもしれませんが、被害の心配はありません。」。`None` は読み上げで「この地震による津波の心配はありません。」を明示的に付加する（`MajorWarning` は従来通り「大津波警報」）

### EEW警報時の注意喚起文
- `isWarn=True`（緊急地震速報・警報）の場合、通知本文の注意喚起文を予想最大震度に応じて切り替える（`core/eew_convert.py`の`eew_warn_advisory_note`。単独EEW通知・複数EEWサマリー通知の両方で共通利用）
  - 予想最大震度が6弱以上: 「**⚠ 直ちに身の安全を確保してください。**」
  - 予想最大震度が5弱以上6弱未満（PLUM法等による「推定5弱以上」を含む）: 「**⚠ 強い揺れに警戒してください。**」
  - 予想最大震度が不明・5弱未満等、上記いずれにも該当しない場合: 従来通り「**⚠ 強い揺れに警戒してください。**」を安全側のデフォルトとして表示
  - 複数EEWサマリー通知では、警報が発表されている各EEWの予想最大震度のうち最も大きい値で判定する
  - `isWarn=False`（警報でない場合）はこの注意喚起文自体を表示しない、という従来の挙動を維持している

### 複数の緊急地震速報が同時に発表されている場合
- 複数の異なる地震について有効なEEW（`EewCog.recent_eews`にTTL300秒以内で保持されている件数が2件以上）を検知すると、通常の単独EEW通知の直前に「複数の緊急地震速報が発表されています」というサマリーEmbedを送信する
- サマリーEmbedには、保持中の各EEWについて「タイトル（第N報／最終報）・震源地・予想最大震度・マグニチュード・深さ」を新しい順に列挙した後、以下を続けて表示する
  1. 予想最大震度に応じた注意喚起（いずれかのEEWが該当条件を満たせば表示。詳細は下記「EEW警報時の注意喚起文」参照）
  2. **【強い揺れが予想される地域】**（`REGION_MAP`変換後の地方単位。複数EEWの警報対象地域を`merged_warn_regions`として和集合にまとめたもの）
  3. **【地域ごとの予想震度】**（2026-08-27追加。市区町村・地域単位で、単独EEW通知と同じ「震度X程度」「震度X〜Y程度」形式のグルーピング。複数EEWを横断してマージし、同一地域が複数のEEWで異なる予想震度になっている場合はより大きい方を採用する。ロジックは`core/eew_convert.py`の`build_forecast_groups`/`merge_forecast_groups`/`format_forecast_section`に共通化されており、単独EEW通知の「地域ごとの予想震度」と全く同じ関数を使う）
- Discord Embedの4096文字制限に対応した切り詰め処理を適用（地域数が多い場合は末尾に「（地域が多いため一部省略）」を付記）

- Wolfx・P2P地震情報それぞれについて、EventIDごとの最大Serial番号を独立に管理し、同一ソースからの重複/逆行メッセージ（既に処理済みのSerial以下の再送）のみをスキップする（`EewCog.eew_max_serial_seen`、ソースごとに別々の辞書で管理）
- **2026-08-23修正**: 以前はこの管理をWolfx・P2Pで単一の辞書として共有しており、「同一EventIDのEEWを両ソースがほぼ同時に配信してきた場合の二重通知防止」を意図していた。しかし実際にはWolfxとP2P地震情報は同一EventIDに対して独立にSerial番号を採番しており両者の値は対応しないため、一方のソースが先に高いSerial番号を処理すると、もう一方のソースの正当な更新（低いSerial番号）が誤って「重複/逆行」とみなされ、通知が欠落する不具合があった（実際の茨城県南部の地震で発生を確認）。ソースごとに辞書を分離し、この誤抑制を解消した

### EWS（緊急警報放送）信号音
- 津波警報・大津波警報（地震情報側の`domesticTsunami`が`Warning`/`MajorWarning`。津波注意報・津波予報は対象外）が発表・更新された P2P 地震情報を受信した際に、昭和60年郵政省告示第405号に準拠した AFSK 方式の緊急警報信号音（第二種開始信号）を生成・再生する
- **2026-09-01追加**: 津波情報そのもの（`notify_tsunami`・`notify_tsunami_forecast`）が津波警報・大津波警報を発表・更新した際にも再生する。以前は地震情報側（`domesticTsunami`経由）のみで、津波情報が単独で発表・更新されたタイミングでは再生されないリグレッションがあった。詳細は上記「EWS（緊急警報放送）信号音（津波）」参照
- 信号音は `core/ews_signal.py` でビット列組み立て・PCM波形合成を行い、外部 WAV ファイルへは一切出力しない。生成した PCM バイト列はメモリ上のまま `core/audio.py` の `play_ews_pcm`（`pygame.mixer.Sound(buffer=...)`）に渡して直接再生する
- `pygame.mixer.Sound(buffer=...)` はリサンプリングを行わず、ミキサー初期化時のサンプルレート・チャンネル数をそのままバッファの解釈に使うため、`core/audio.py` の `pygame.mixer.init()` は `core/ews_signal.py` の生成条件（44100Hz・モノラル）に明示的に合わせて初期化している（不一致のまま再生するとピッチ・再生速度が変わってしまう。既存の `pygame.mixer.music`（MP3再生）はファイル再生時に自動リサンプリングされるためこの制約を受けない）
- `EWS_ENABLE=true` で有効化（デフォルト `false`。オプトイン機能）。地域符号・送信ブロック数・前置後置固定音の長さは環境変数で設定可能（下記「EWS 設定」参照）
- CLI テスト実行時（`is_test=True`）は実際の警報ではないため対象外

### P2P 地震感知情報（2026-08〜）
- P2P 地震情報が code=9611 で配信する「地震感知情報」（JMA発表の公式情報ではなく、P2P地震情報が独自に推定する速報値）を受信・通知する（`JishinKanchiCog`。デフォルト無効、`JISHIN_KANCHI_ENABLE=true` で有効化）
- 全体の信頼度（`confidence`）をレベル1〜4（数値が大きいほど信頼度が低い）に変換して表示。レベルの判定は範囲ではなく既知の定数値との最近傍マッチングで行う（詳細は `core/jishin_kanchi_convert.py`）
- 地域ごとの信頼度（A〜F、Aが最高）でグルーピングし、信頼度が高い順に地域名・件数を一覧表示。地域コード→地域名の変換には `epsp_area.csv`（[p2pquake/epsp-specifications](https://github.com/p2pquake/epsp-specifications) 配布の `epsp-area.csv` を同梱）を使用
- 地図画像はP2P地震情報通知と同じ生成方法（`cdn.p2pquake.net/app/images/{id}_trim_big.png`）でEmbed最下部に添付。地震感知情報（code=9611）は quake/tsunami（code=551/552）と異なり、画像IDには `id` ではなく `_id` フィールドの値を使う必要があることが実機ログで判明したため、`_id` を優先し `id` にフォールバックする実装に修正した（2026-08-27）
- `JISHIN_KANCHI_MAX_LEVEL`（通知する最大レベル）・`JISHIN_KANCHI_MIN_COUNT`（通知する最小件数）で閾値フィルタリング可能
- 音声読み上げ（`JISHIN_KANCHI_SPEECH_ENABLE`）・効果音再生（`JISHIN_KANCHI_SOUND_ENABLE`、ファイル名は `JISHIN_KANCHI_SOUND_FILE` で変更可能）をそれぞれ個別に無効化可能
- **音声トリガー・地図画像添付は第一報のみ（2026-08-23追加、2026-08-27仕様変更、2026-08-30に画像添付も統合）**: 同一イベント（`started_at` で識別。EEWのEventIDに相当）について更新が届くたびに音声読み上げ・効果音が毎回鳴ると煩わしいため、音声読み上げ・効果音とも「そのイベントを初めて検知したとき（＝第一報、EEWの第一報相当）」の1回のみ再生する。以前は読み上げのみ件数（`count`）増加ごとに再トリガーしていたが、繰り返し鳴ってうるさいとの指摘を受けて統一した
  - **地図画像の添付タスクも第一報のみに間引く（2026-08-30追加）**: 感知報告が増えるたびにP2P側から `_id` が異なる新しい更新レコードが配信され続けるため、従来は更新のたびに新規の画像取得タスクを起動していた。実機ログで、関東で広く感知された地震において短時間に70件近い異なる `_id` で画像取得タスクが同時多発し、`P2P_IMAGE_CDN_CONCURRENCY`（下記「P2P 地図画像の添付」参照）のセマフォを占有し尽くし、`QuakeInfoCog` 側の正当な画像取得まで巻き添えで大幅に遅延・失敗させる不具合を実機ログで確認した（本来6秒程度で成功するはずの画像取得が103秒後まで成功しなかった）。これが大規模・関東の地震で地図画像が表示されない不具合の直接原因の一つだったため、画像添付タスクの起動可否も「第一報かどうか」の判定に統一した
- **テキスト通知（Embed送信）は最短10秒間隔に間引き（2026-08-30追加）**: 画像添付とは異なり、テキスト通知自体は従来更新のたびに毎回新規メッセージが送信されていたため、感知報告が短時間に大量発生する地震では同じチャンネルに数十件規模の「地震感知情報」Embedが数秒おきに連続投稿される実害が確認された。`JISHIN_KANCHI_MIN_UPDATE_INTERVAL_SEC`（既定10秒）を新設し、前回の実際の送信からこの秒数未満の更新は送信自体をスキップするようにした。第一報は必ず送信する。間引かれた更新の情報は、次に間隔条件を満たした更新が届いた時点でその時点までの最新の件数・地域状況としてまとめて送信されるため、実質的には後続の更新に引き継がれる
  - 上記の「第一報かどうか」「送信すべきか」の判定はすべて `JishinKanchiCog._judge_event_notification`（旧名 `_judge_audio_triggers`）に一本化されており、テキスト送信・音声・効果音・画像添付のすべてが同じ判定結果を参照する
  - イベント単位の管理状態は `JISHIN_KANCHI_EVENT_STATE_TTL_SEC`（デフォルト3600秒）以上更新がなければ自動的に破棄される
  - `JISHIN_KANCHI_SPEECH_COUNT_STEP` は現在どこからも参照されない（既存 `.env` との後方互換のため設定項目のみ残置。将来的に削除予定）

### P2P 地図画像の添付
- 地震情報・津波情報・EEW（P2P由来）・地震感知情報の通知には、P2P 地震情報 CDN が生成する震源地図画像を Embed に添付する（`core/p2p_image.py` の `P2PImageMixin`。`QuakeInfoCog` / `TsunamiCog` / `EewCog` / `JishinKanchiCog` が多重継承）
- CDN 側の画像生成には数秒〜数十秒のタイムラグがあるため、通知メッセージ送信後にバックグラウンドで最大約2分間（20回 × 6秒間隔）ポーリングし、画像が実際に利用可能になった時点でメッセージを編集して画像を追加する
- 判定は HTTP ステータス 200 だけでなく、レスポンスボディが完成した PNG ファイルとして妥当か（先頭のマジックバイト `\x89PNG\r\n\x1a\n` と、末尾の IEND チャンク `\x00\x00\x00\x00IEND...` の両方）まで検証し、CDN が「200 は返すが実体はまだ書き込み中」の状態を誤って成功と判定しないようにしている（2026-08-19: 判定基準を「ファイルサイズが `MIN_VALID_IMAGE_BYTES` 以上か」から「PNG構造として完結しているか」に変更。震度分布を持たない「震源に関する情報」の地図画像は内容がシンプルで正当に軽量になり、旧・サイズ閾値では常に「生成中」と誤判定され地図画像が一切表示されない不具合があったため）
- **CDNへの同時アクセス制限（2026-08-23追加）**: 大規模地震で短時間に複数のP2P地震情報レポート（震度速報→各地の震度に関する情報等）が連続発表されると、それぞれが独立した画像添付タスクとして並行実行され、同一CDNへの同時多発リクエストにより成功率が大幅に低下する不具合を確認した（実際の震度5弱の地震で5件中3件が失敗、成功した2件も90〜100秒要した事例あり）。`P2PImageMixin` を使う全Cogを横断したモジュールレベルの `asyncio.Semaphore` でCDNへの同時アクセス数を制限し（`P2P_IMAGE_CDN_CONCURRENCY`、デフォルト3）、成功率を改善している。セマフォは実際のHTTPリクエスト送信中のみ確保し、リトライ間の待機（6秒）中は保持しない
- `P2P_IMAGE_ATTACH_ENABLED=false` にすると、この画像添付処理（CDN ポーリング）自体を無効化し、通知本文に画像 URL をテキストとして含める簡易方式に切り替わる（Discord のリンクプレビュー機能により自動展開。原因切り分け用）

### 津波情報
2つの独立した情報源から通知する（それぞれ通知文のテンプレートが異なる）。

- **P2P 地震情報**（`notify_tsunami`、P2P WebSocket code=552）
  - 警報種別・予想高さ別のエリア一覧表示
  - **エリアごとに`immediate`（ただちに来襲）・`firstHeight.condition`・`firstHeight.arrivalTime`（到達予想時刻）を表示**（2026-08-30復元。Cog分割時に実装から欠落していたリグレッションを修正したもの。P2P公式仕様のこれらのフィールドは気象庁の生XML形式には存在しないため、この情報量の多さはP2P経由の通知だけの特徴）
  - 読み上げは「（地域名）に（警報種別）が発表されました」の形式（例:「有明・八代海に津波注意報が発表されました」「宮城県に大津波警報が発表されました」）。複数地域該当時は最重要度（大津波警報 > 津波警報 > 津波注意報）の地域を優先し、4件以上は代表3件＋「等」で丸める
  - 発信元コメント（`comments.warningComment.text`）を通知下部に追記
- **気象庁 HP の JSON**（`tsunami/data/list.json`を60秒間隔でポーリング）
  - `notify_tsunami_forecast`：津波警報・注意報・予報（VTSE41）。警報種別・予想高さ別のエリア一覧、原因地震情報、解除時は解除レベル別の文言を出し分け
  - `notify_tsunami_observation`：津波観測に関する情報（VTSE41/51/52）。観測点ごとの**実測**津波高さ・到達状況を表示（P2P側・予報側にはない実測値情報）
  - 顕著な地震の震源要素更新のお知らせ／南海トラフ地震臨時情報は `cogs/other.py`（`OtherInfoCog`）へ委譲（下記「気象庁その他特別情報」参照）
- 地震情報通知と同じ地図画像添付方式（下記「P2P 地図画像の添付」参照）を使用（P2P経由のみ）

### EWS（緊急警報放送）信号音（津波）
**2026-09-01追加。** 津波警報・大津波警報（`Warning`/`MajorWarning`）が発表・更新された際に、`EWS_ENABLE=true`でEWS信号音（下記「EWS（緊急警報放送）信号音」参照）を再生する。P2P形式（`notify_tsunami`）・気象庁HP形式（`notify_tsunami_forecast`）の両方に対応。以前は地震情報側（`domesticTsunami`フィールド経由）からのみ再生され、津波情報そのものの発表・更新時には再生されないリグレッションがあったため追加した。同一イベントでの警報区分のエスカレーション時（例: 津波警報→大津波警報）は再度再生し、同一区分の繰り返し更新では再生しない。

**2026-09-06修正: 津波情報発表時のMP3再生を廃止。** 以前は`notify_tsunami`・`notify_tsunami_forecast`が警報グレードに応じて`vxse51`/`vxse52`/`vxse53`/`vxse5c`のMP3（本来は震度速報等・地震情報向けに用意されたチャイム）を再生していたが、津波情報の発表時に地震情報用のチャイムが鳴るのは文脈として不適切かつ紛らわしいため廃止した。津波情報発表時に再生する音声は「読み上げ（`speak_local`によるTTS）」と「EWS警告音（上記）」の2つのみとする。

### 気象庁その他特別情報
- 北海道・三陸沖後発地震注意情報・南海トラフ地震臨時情報・顕著な地震の震源要素更新のお知らせを`OtherInfoCog`（`cogs/other.py`）で通知する
- 南海トラフ地震臨時情報・顕著な地震の震源要素更新のお知らせは、気象庁が`tsunami/data/list.json`と`quake/data/list.json`の両方に重複して掲載しているため、本Botでも2経路から独立して通知される（フェッチは`cogs/tsunami.py`側の`fetch_tsunami_observation`が担当し、通知処理のみ`OtherInfoCog.notify_hypocenter_update`/`notify_nankai_trough`に委譲。**2026-09-01移設**、送信先は`OTHER_CHANNEL_ID`に統一）
- 北海道・三陸沖後発地震注意情報／南海トラフ地震臨時情報には区域図画像（`hokkaido_bosaitaiou_area.png`／`nankai_bosaitaiou_area.png`。**プロジェクトルート**＝`bot.py`と同じディレクトリに配置）を添付

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
- 強震モニタ画像（`jma_s` 系統）を実観測点データに基づいてサンプリング・解析し、数値APIを使わず画像のみから揺れを検知する独立機能（`KyoshinMonitorCog`）
- 観測点データは ingen084氏の [kyoshin-monitor-observation-points](https://github.com/ingen084/kyoshin-monitor-observation-points)（`intensity-points.json`）を起動時に取得・キャッシュし、`KYOSHIN_STATIONS_REFRESH_SEC`（既定1時間）ごとにバックグラウンドで再取得・追従する（観測点データは経年で「やや古く」なりうる前提のため）。色→震度への変換式は [t0729/kyoshin-monitor-python](https://github.com/t0729/kyoshin-monitor-python)の実装を移植
- 近隣観測点は緯度経度に基づく地理的K近傍（既定6件、`KYOSHIN_NEIGHBOR_K`）で決定する。実観測点の分布密度は地域差が大きい（都市部は密、山間部・離島は疎）ため、固定半径ではなく件数固定のK近傍方式を採用
- 検知は「基準値（過去10〜25秒平均）との差分による上昇トリガー」＋「K近傍のうち一定数以上が同時に上昇トリガー成立」の空間クロスバリデーション方式（ingen084氏の記事の実装方針を採用）。震度の絶対値のみによる無条件判定は行わない
- **起動ウォームアップ**: 起動直後は観測点の震度履歴が浅く、基準値が「過去10〜25秒平均」ではなく「履歴中の最古の1点」にフォールバックせざるを得ないため、起動から `KYOSHIN_STARTUP_WARMUP_SEC`（既定値: `KYOSHIN_BASELINE_WINDOW_END_SEC` と同じ＝25秒）が経過するまではイベントの新規生成・確定を抑制する。観測点ごとの震度履歴の蓄積（ingest）自体は起動直後から通常通り継続するため、ウォームアップ終了時には基準値計算に必要な実サンプルが揃っている。`0` 以下を指定するとウォームアップを無効化し、従来通り起動直後から検知を行う
- イベントの生死は「最後に本物の上昇トリガーが立ってから `KYOSHIN_EVENT_TIMEOUT_SEC` 秒経過したか」の1点のみで判定（複数条件を組み合わせない単純な状態機械）
- 周囲が無反応のまま単独でフラット（変化なし）かつ高震度が続く観測点は機器異常とみなし自動的にブラックリスト化し、以後の判定から除外する（観測点メタデータが実態と乖離していた場合の実行時セーフティネットとしても機能する）
- 画像上でカラースケール外（背景・地図色等）と判定されたピクセルは、地震発生を意味しない固定の「静穏相当」代表値として扱う（無効値をそのまま検知ロジックへ注入しない安全設計）
- **パッチサンプリング（恒久対策、2026-09-09追加）**: `KYOSHIN_RISE_THRESHOLD=1.0`・`KYOSHIN_NEIGHBOR_TRIGGER_COUNT=3`への引き上げは「観測点座標周辺の複数ピクセルを平均・中央値で平滑化するパッチサンプリング等の恒久対策を検討するまでの一時的な緩和措置」とされていたが、これを実装した。`KYOSHIN_PATCH_RADIUS`（既定0＝従来通りの単一ピクセル方式）を1以上にすると、観測点のピクセル位置を中心とした`(2*radius+1)^2`の正方形パッチ内の各ピクセルを個別に震度へ変換し、その中央値（`KYOSHIN_PATCH_AGGREGATION=median`、既定）または平均（`mean`）をその観測点の震度として採用する。RGBを直接平均せず「各ピクセルを震度に変換してから集約」する設計にしているのは、色空間上でのRGB平均がカラースケール外の色と混ざって無意味な値に化ける危険を避けるため。パッチが大きいほど計算量が`(2r+1)^2`倍に増えるため、既定では無効のままとし、`KYOSHIN_SLOW_FETCH_THRESHOLD_SEC`のログを見ながら段階的に有効化・チューニングする想定
- 画像デコード失敗（配信元画像が生成途中のタイミングで捕まる、想定内の一時的事象）のログは、`KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC`（既定300秒）ごとに件数をまとめて1回だけWARNINGへ集約する（詳細は毎回DEBUGに出る）。1秒間隔ポーリングでは頻発しうるため、毎回WARNINGを出すログノイズを避け、本当に見るべき異常が埋もれないようにした
- 画像の時刻決定は `latest.json` API（実際に配信されている最新時刻）を優先取得し、失敗時のみ従来のリトライ探索方式にフォールバック
- 通知には `jma_s` 系統・`abrspmx_s` 系統の両画像と振動レベルを含める
- 通知の色は `jma_s` 系統の実震度に基づく独自カラーマップで決定（EEW発表時の強震モニタ通知と共通仕様）
- Pillow（PIL）が未インストールの場合は自動的に機能をスキップする

### Web Dashboard / コマンド
- `GET /status` で詳細な稼働状況を JSON で取得
- `GET /status/history` でシステムリソース・受信件数の推移スナップショット履歴を JSON で取得（`STATUS_HISTORY_INTERVAL` 秒ごとに記録、メモリ上のリングバッファのみで保持しBot再起動でリセットされる）。`?format=csv` を付けると同じデータを CSV（UTF-8 BOM付き）でダウンロード可能
- `GET /status/notifications` で実際にDiscordへ送信した通知（種別・タイトル・時刻）の直近履歴を JSON で取得（最大50件、メモリ上のリングバッファのみで保持）。`?limit=N` で件数を絞り込み可能
- 配信成功率の可視化: 各Cogの通知送信箇所（`channel.send()`）の成功/失敗を `core/delivery_stats.py` に記録し、直近24時間の成功率を `!status` Embed・`GET /status` の `delivery` フィールドで確認できる
- 週間/月間ダイジェスト: `DIGEST_ENABLED=true` で有効化。累積受信カウントの差分から「先週/先月の通知件数」を集計し、指定した曜日・時刻（または毎月1日）にEmbedで自動投稿する（`SystemCog.digest_worker`）
- `GET /dashboard` で上記データを可視化する HTML ページ。ステータスサマリー（Bot状態・稼働時間・Ping・Wolfx EEW接続状態・CPU/メモリ・ディスク使用率を色付きインジケーターで表示）、CPU/メモリ/ディスク使用率・各種受信件数の推移グラフ（Chart.js）、種別フィルタ付きの直近通知履歴テーブル、CSVダウンロードボタン、手動更新ボタンを備える。15〜60秒間隔で自動更新
- **地震情報履歴の地図・表閲覧（2026-09-06追加、2026-09-09にEEW対応）**: `GET /quake_map` で、地震情報（P2P地震情報 code=551）・EEW（緊急地震速報）の通知履歴を地図（Leaflet + 国土地理院タイル、震度に応じた色分けマーカー）と表の両方で、ページ内の種別セレクタで切り替えながら閲覧できる。データ取得元は `GET /status/quake_history` / `GET /status/eew_history`（`?limit=N` で件数を絞り込み可能。省略時は `QUAKE_HISTORY_DEFAULT_LIMIT`）で、`core/quake_history_log.py` / `core/eew_history_log.py` が qtlbot.log（ローテーション含む全世代）をスキャンして復元した内容をそのまま返す。詳細は下記「地震情報履歴（qtlbot.logベース）」参照
- `!status` コマンド（管理者専用）
- `/qtl_status` スラッシュコマンド（管理者専用）

### 地震情報履歴（qtlbot.logベース）
- **背景**: `core/notification_log.py`（`/status/notifications`）は「直近の通知履歴」をメモリ上のリングバッファ（最大50件）のみで保持しており、Bot再起動でリセットされ、緯度経度等の座標も保持しない。一方 `qtlbot.log` は `LOG_BACKUP_COUNT`（既定7）世代分が既に永続化されている。新たなDB等の永続化レイヤーを追加するのではなく、この既存のログ基盤をそのまま履歴ストアとしても使う設計とした（軽量・低依存の方針に合わせるため）
- **記録形式**: `QuakeInfoCog.notify_quake()` が地震情報の通知（テスト通知を除く）を送信するたびに、`QUAKE_RECORD_V1 {json}` という機械可読な1行を `record_notification`（メモリ上履歴）とは別に `logger.info()` でqtlbot.logへ追記する（`core/quake_history_log.py` の `build_quake_record` / `format_quake_record_log_line`）。JSONには震源地名・緯度経度・マグニチュード・深さ・最大震度・発生時刻等を含む
- **読み出し**: `core/quake_history_log.load_quake_history()` が `qtlbot.log` + `qtlbot.log.1`〜`.<LOG_BACKUP_COUNT>` を古い世代→新しい世代の順にスキャンし、`QUAKE_RECORD_V1` 行のみを抽出、`id` で重複排除（新しい世代を優先）した上で発生時刻の降順で返す。Web Dashboard（`cogs/system.py`）はこの結果を `QUAKE_HISTORY_CACHE_TTL_SEC` 秒キャッシュしてから返す（ログファイル全体のスキャンは相応にコストがかかるため）
- **過去分の遡り（バックフィル）**: この記録形式を追加する前に発生した地震には `QUAKE_RECORD_V1` 行が存在せず、遡って自動復元することはできない。`python3 bot.py --backfill_quake_history` を実行すると、P2P地震情報 API（`GET /v2/history?codes=551`）を新しい順にページ送りして取得し、まだ記録されていない `id` があれば同じ形式でqtlbot.logへ書き足す（`core/quake_history_backfill.py`）。ただしP2P地震情報 APIの `/v2/history` は特定の `id` を直接指定して取得するAPIではなく、無制限に過去へ遡れるAPIでもないため、`QUAKE_HISTORY_BACKFILL_MAX_ITEMS`（既定1000件）より古い地震はこの方法でも復元できない（ベストエフォート）
- **EEW履歴（2026-09-09追加）**: 同じ設計をEEW（`core/eew_history_log.py`）にも拡張した。`EEW_RECORD_V1 {json}` 形式でqtlbot.logへ記録し、`GET /status/eew_history` ・`GET /quake_map`（種別セレクタで「EEW」を選択）から閲覧できる。EEWは同一地震（EventID）について第1報〜最終報まで複数回更新されるため、レコードの一意キーはEventIDのみとし、Serialを重ねるたびに同じキーで上書きする（＝地図・表には常にその時点での最新の報が1行表示される）。キャンセル報（isCancel=True）は震源情報を持たないため記録しない。EEWの過去分バックフィルコマンドは現時点では未実装（地震情報のバックフィルとは異なり、Wolfx API側に同等の履歴取得APIがないため）

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

**方法A: 対話式セットアップウィザード（初めての場合はこちらを推奨）**
```bash
python3 bot.py --starter
```
Bot トークン・チャンネル ID など最低限の項目だけを対話形式で質問し、`.env.example` をベースに `.env` を自動生成します。それ以外の詳細設定（通知フィルターや音声設定など）は `.env.example` に書かれているデフォルト値のまま反映されるため、後から `nano .env` で必要な箇所だけ調整できます。既に `.env` が存在する場合は上書き前に確認し、`.env.bak.<タイムスタンプ>` として自動的にバックアップします。

**方法B: `.env.example` を手動でコピー**
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

### ファイル構成（2026-08-27〜）
QTL_Bot の設定は次の2種類のファイルで構成される。

| ファイル | 役割 | 通常運用での要否 |
|:---|:---|:---|
| `.env`（`.env.example`をコピー） | Discordトークン・チャンネルID・通知フィルター等、本体設定 | 必須 |
| `.env.kyoshin`（`.env.kyoshin.example`をコピー） | 強震モニタの誤検知対策アルゴリズム調整値（上級者向け） | 通常は不要（無くても既定値で動作する） |

`.env` 本体の読み込み後、`.env.kyoshin` が存在すれば自動的に追加読み込みされる（`core/env_loader.py`）。同じ変数を両方に書いた場合は `.env` 本体側が優先される。`python3 bot.py --starter` で対話的に作成する場合、強震モニタの詳細チューニングを希望した場合のみ `.env.kyoshin` の作成も案内される。

設定内容がコードの実際の参照箇所と食い違っていないか（廃止した変数の消し忘れ・追記漏れ等）を機械的にチェックしたい場合:
```bash
python3 bot.py --check_env
```

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
| `JISHIN_KANCHI_CHANNEL_ID` | | QUAKE_CHANNEL_ID | 地震感知情報専用チャンネル |
| `OTHER_CHANNEL_ID` | | CHANNEL_ID | その他情報（長周期地震動等） |
| `KYOSHIN_CHANNEL_ID` | | OTHER_CHANNEL_ID | 強震モニタ専用チャンネル |
| `ADMIN_CHANNEL_ID` | | 0（無効） | エラー通知用管理者チャンネル |

多段フォールバックの解決結果（結局どのチャンネルに何が送られるか）は、`.env`を読むよりも `!status`/`/qtl_status` の「通知先チャンネルマッピング」欄で確認する方が確実（`.env整理案⑤`、2026-08-27追加。同じ宛先になっているものはグルーピングして表示される）。

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
| `P2P_IMAGE_CDN_CONCURRENCY` | 3 | CDN（cdn.p2pquake.net）への同時アクセス数の上限。`P2PImageMixin` を使う全Cog（QuakeInfoCog/TsunamiCog/EewCog/JishinKanchiCog）を横断して共有制限する |

### 地震情報履歴設定（qtlbot.logベース、地図・表での閲覧機能）
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `QUAKE_HISTORY_DEFAULT_LIMIT` | 500 | `GET /status/quake_history` がデフォルトで返す最大件数（`?limit=`で上書き可） |
| `QUAKE_HISTORY_CACHE_TTL_SEC` | 30 | qtlbot.log*のスキャン結果をキャッシュする秒数 |
| `QUAKE_HISTORY_BACKFILL_MAX_ITEMS` | 1000 | `--backfill_quake_history` がP2P地震情報APIから取得する最大件数（APIの実質的な遡り可能範囲に合わせた安全上限） |
| `QUAKE_HISTORY_BACKFILL_PAGE_SIZE` | 100 | `--backfill_quake_history` の1回あたりのAPI取得件数 |
| `QUAKE_HISTORY_BACKFILL_REQUEST_DELAY_SEC` | 0.5 | `--backfill_quake_history` のページ取得ごとの待機秒数 |

### P2P 地震感知情報設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `JISHIN_KANCHI_ENABLE` | false | 地震感知情報（code=9611）通知の有効化 |
| `JISHIN_KANCHI_MAX_LEVEL` | 4 | 通知する最大レベル（1〜4、数値が大きいほど信頼度が低い）。この値以下のレベルのみ通知する |
| `JISHIN_KANCHI_MIN_COUNT` | 1 | 通知する最小件数（`count`）。これ未満は通知しない |
| `JISHIN_KANCHI_SPEECH_ENABLE` | true | 音声読み上げの有効化 |
| `JISHIN_KANCHI_SOUND_ENABLE` | true | 効果音再生の有効化 |
| `JISHIN_KANCHI_SOUND_FILE` | vxse53.mp3 | 再生する効果音ファイル名（Bot実行ディレクトリ直下に配置） |
| `JISHIN_KANCHI_SPEECH_COUNT_STEP` | 50 | **現在未使用**（2026-08-27〜。音声読み上げは第一報のみに統一されたため。既存`.env`との後方互換のため項目のみ残置） |
| `JISHIN_KANCHI_EVENT_STATE_TTL_SEC` | 3600 | イベント単位の状態管理（音声トリガー・画像添付・テキスト送信間引きの判定に共通利用）を、最終更新からこの秒数以上経過したら破棄する |
| `JISHIN_KANCHI_MIN_UPDATE_INTERVAL_SEC` | 10 | **2026-08-30追加**。同一イベントのテキスト通知（Embed送信）を実際に行う最短間隔（秒）。第一報は必ず送信するが、2回目以降の更新は前回の実際の送信からこの秒数未満しか経っていない場合は送信自体をスキップする（詳細は「P2P 地震感知情報」参照）。0以下を指定すると事実上無効化（毎回送信）される |

### 週間/月間ダイジェスト設定
| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `DIGEST_ENABLED` | false | ダイジェスト機能の有効化 |
| `DIGEST_INTERVAL` | weekly | `weekly`（毎週） または `monthly`（毎月1日） |
| `DIGEST_WEEKDAY` | 0 | weekly時のみ使用。投稿曜日（0=月曜〜6=日曜） |
| `DIGEST_HOUR` | 9 | 投稿時刻（24時間制、0〜23） |
| `DIGEST_CHANNEL_ID` | CHANNEL_ID | ダイジェスト投稿先チャンネル |

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
| `KYOSHIN_STATIONS_SOURCE_URL` | ingen084氏のリポジトリURL | 実観測点データ（`intensity-points.json`）の取得元。通常変更不要 |
| `KYOSHIN_STATIONS_CACHE_PATH` | kyoshin_stations_cache.json | 観測点データのキャッシュファイルパス（`.gitignore`対象） |
| `KYOSHIN_STATIONS_REFRESH_SEC` | 3600 | 観測点データの再取得間隔（秒）。起動時はキャッシュ優先、以後この間隔で追従する |
| `KYOSHIN_NEIGHBOR_K` | 6 | 近隣観測点として扱う件数（地理的K近傍）。`.env.kyoshin`の`KYOSHIN_NEIGHBOR_TRIGGER_COUNT`より大きい値にすること |
| `KYOSHIN_IMAGE_DELAY_SEC` | 6 | `latest.json` 取得失敗時のフォールバック探索で遡る基準秒数 |
| `KYOSHIN_IMAGE_STEP_SEC` | 3 | フォールバック探索で画像が見つからない場合に遡るステップ幅（秒） |
| `KYOSHIN_IMAGE_MAX_RETRY` | 4 | フォールバック探索の最大リトライ回数 |
| `KYOSHIN_POLL_INTERVAL_SEC` | 1.0 | 観測値取り込み〜イベント判定のポーリング間隔（秒）。EEW発表中（`EewCog.monitored_event_id`が設定されている間）はポーリング自体をスキップし、`EewCog.vibration_monitor_loop`に画像取得を一本化する（防災科研への負荷軽減） |
| `KYOSHIN_NOTIFY_INTERVAL_SEC` | 1.0 | イベント継続中の通知再送間隔（秒）。EEW発表中は同様に通知をスキップする |
| `KYOSHIN_EVENT_TIMEOUT_SEC` | 45.0 | 最後の上昇トリガーからこの秒数経過でイベント終了。上げるほど余韻の通知が長く続く |
| `KYOSHIN_MIN_NOTIFY_PHASE` | Weaker | 通知を送信する最小フェーズ（Weaker &lt; Weak &lt; Medium &lt; Strong &lt; Stronger） |
| `KYOSHIN_MIN_STATIONS_SHINDO0` | 4 | 実震度が震度0相当（1.0未満）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_MIN_STATIONS_SHINDO1` | 2 | 実震度が震度1相当以上（1.0以上）の場合に通知に必要な最小検出観測点数 |
| `KYOSHIN_DEBUG_SAVE_IMAGE` | false | イベント確定時の元画像をローカル保存するか（事後検証用） |
| `KYOSHIN_DEBUG_IMAGE_DIR` | ./kyoshin_debug_images | デバッグ画像の保存先ディレクトリ |
| `KYOSHIN_SLOW_FETCH_THRESHOLD_SEC` | 0.5 | **2026-08-29追加**。`_fetch_current_shindo_map`（画像ダウンロード・デコード・観測点サンプリング。`KYOSHIN_POLL_INTERVAL_SEC`ごとに常時実行される）のうち、デコード+サンプリング部分の所要時間がこの秒数を超えた場合にWARNINGログを出す。通常はDEBUGログに毎回の所要時間が出るのみ。Raspberry Pi等でのCPU負荷を実測ベースで把握するための計測用設定であり、通常運用では変更不要 |
| `KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC` | 300 | **2026-09-09追加**。画像デコード失敗（想定内の一時的事象）のログをこの秒数ごとに件数集約して1回だけWARNINGに出す（詳細は毎回DEBUGに出る） |

**【2026-08-27】誤検知対策アルゴリズム調整値は `.env.kyoshin` に分離**
以下は、実機ログを見ながらチューニングする上級者向けパラメータのため、`.env.example`（本体）ではなく `.env.kyoshin.example` に分離されている（`.env整理案③・⑩`）。何も設定しなくても以下と同じデフォルト値で動作するため、通常運用では `.env.kyoshin` を作る必要はない。詳細チューニングをしたい場合のみ `cp .env.kyoshin.example .env.kyoshin` して編集する（`python3 bot.py --starter` の詳細設定メニューからも作成できる）。

| 変数名 | 既定値 | 説明 |
|:---|:---|:---|
| `KYOSHIN_ACTIVE_SHINDO_FLOOR` | 0.5 | 揺れ候補とみなす実震度の下限 |
| `KYOSHIN_RISE_THRESHOLD` | 1.0 | 「上昇トリガー」とみなす基準値との差分幅。震度の絶対値ではなく変化量で判定する。実観測点方式（1ピクセルサンプリング、平滑化なし）移行後の誤検知対策として0.5から引き上げ済み（一時的な緩和措置） |
| `KYOSHIN_PATCH_RADIUS` | 0 | **2026-09-09追加**。パッチサンプリング（恒久対策）の半径。0＝従来通りの単一ピクセル方式、1以上で`(2r+1)^2`パッチの中央値/平均を採用。パッチが大きいほど計算量が`(2r+1)^2`倍に増えるため、`KYOSHIN_SLOW_FETCH_THRESHOLD_SEC`のログを見ながら段階的に有効化すること |
| `KYOSHIN_PATCH_AGGREGATION` | median | **2026-09-09追加**。パッチ内の集約方式。`median`（外れ値に強い）または`mean` |
| `KYOSHIN_BASELINE_WINDOW_START_SEC` | 10.0 | 基準値計算に使う過去サンプルの開始位置（秒前） |
| `KYOSHIN_BASELINE_WINDOW_END_SEC` | 25.0 | 基準値計算に使う過去サンプルの終了位置（秒前） |
| `KYOSHIN_HISTORY_WINDOW_SEC` | 25.0 | 観測点ごとに保持する震度履歴の長さ（秒）。BASELINE_WINDOW_END_SEC以上を推奨 |
| `KYOSHIN_STARTUP_WARMUP_SEC` | `KYOSHIN_BASELINE_WINDOW_END_SEC`と同じ（既定25.0） | 起動から検知（イベント新規生成・確定）を有効化するまでのウォームアップ秒数。0以下で無効化 |
| `KYOSHIN_NEIGHBOR_TRIGGER_COUNT` | 3 | 上昇トリガー確定に必要な、K近傍のうち同時に上昇トリガーが立っている観測点数。実観測点のK近傍は画像上で数ピクセルしか離れていないことが多く色ノイズが相関しやすいため、2から引き上げ済み（一時的な緩和措置） |

**【2026-08-27 完全削除】** `KYOSHIN_GRID_SIZE` / `KYOSHIN_MIN_ACTIVE_PIXELS` は旧・画像ピクセルグリッド疑似観測点方式（〜2026-08）で使用していた設定で、実観測点データ方式への移行に伴い未使用化していたが、`.env整理案①`によりコード側の定義自体を完全に削除した（既存`.env`にこれらの変数が残っていてもエラーにはならない）。

### 音声設定
`python3 bot.py --starter` で音声読み上げを設定すると、選ばなかった方のエンジンの設定行は `.env` 内で自動的にコメントアウトされる（`.env整理案④`、2026-08-27追加）。手動で `.env` を編集する場合は、使わない方のブロックは無視して構わない（読み込まれても`TTS_ENGINE`で選ばれていない方は使用されない）。

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
| `LOG_FILE_PATH` | （空＝プロジェクトルート/qtlbot.log） | ログファイルの出力先パス。**2026-08-29追加**。未設定時は `bot.py` と同じディレクトリ基準の絶対パスが使われるため、実行時のカレントディレクトリ（systemdの`WorkingDirectory`設定等）に依存しない。`/var/log/qtlbot/qtlbot.log` 等、別の場所に出力したい場合のみ絶対パスを指定する |
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

**【2026-08-27 修正】** `cogs/system.py` 側の実装で `WEB_DASHBOARD_ENABLED` 未設定時のデフォルト値が `true` になっており、上表のドキュメント上のデフォルト（`false`）と食い違っていた。`.env` に `WEB_DASHBOARD_ENABLED` を書き忘れると、意図せず Web Dashboard が起動しポートを待ち受けてしまう不具合だったため、コード側をドキュメント通り `false` に修正した。

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
    "jishin_kanchi":  { "last_recv_time": "...", "recv_count": 8  },
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
    "eruption": {
      "last_event_id": "20260804120000",
      "polling_status": "running",
      "last_recv_time": null,
      "recv_count": 0
    },
    "warning": {
      "last_event_id": "20260804120000",
      "polling_status": "running",
      "last_recv_time": null,
      "recv_count": 0
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
      "active_event_ids": [],
      "last_recv_time": "...",
      "recv_count": 3
    },
    "long_period_monitor": {
      "active": false,
      "last_recv_time": "...",
      "recv_count": 1
    }
  },
  "tasks": {
    "p2p_ws_hub": "running",
    "p2p_ws_hub_recv_count": { "eew": 0, "quake": 12, "tsunami": 0, "jishin_kanchi": 8 },
    "fetch_tsunami_observation": "running",
    "fetch_quake_advisory": "running",
    "fetch_usgs_quake": "running",
    "speech_worker_audio": "running",
    "mp3_worker_audio": "running",
    "volcano_poller": "running",
    "eruption_poller": "running",
    "warning_poller": "running",
    "fetch_long_period": "running",
    "kyoshin_monitor": "running",
    "vibration_monitor_loop": "stopped"
  }
}
```

> `quake`（地震情報）・`tsunami`（津波情報）・`eew`（緊急地震速報）・`jishin_kanchi`（地震感知情報）は、
> `core/p2p_ws_hub.py` の `P2PWebSocketHub` が単一の WebSocket 接続から一元的に受信・振り分けを行う
> （2026-08 の REST ポーリング → WebSocket 移行以降。`tasks.p2p_ws_hub` の稼働状態と
> `tasks.p2p_ws_hub_recv_count` の各種別ごとの受信件数を参照）。
>
> `kyoshin`（強震モニタ画像解析検知。KyoshinMonitorCogによる常時検知）と
> `long_period_monitor`（長周期地震動モニタ。EewCogのvibration_monitor_loopによる
> EEW発表時のみの一時的な検知）は名前が似ているが別機能。前者は`ENABLE_KYOSHIN`が
> 有効な限り常時稼働し、後者はEEWが発表されている間だけ`active: true`になる。

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

### GET /status/history（システムリソース・受信件数の推移履歴）

`STATUS_HISTORY_INTERVAL` 秒（既定300秒）ごとに記録されたスナップショットを
新しい順ではなく記録順（古い→新しい）の配列で返す。メモリ上のリングバッファ
（最大 `STATUS_HISTORY_MAXLEN` 件）のみで保持し、Bot再起動でリセットされる。

```bash
curl http://localhost:8080/status/history | jq
```

```json
{
  "interval_sec": 300,
  "max_points": 288,
  "count": 42,
  "history": [
    {
      "timestamp": "2026-08-17T10:00:00.000000",
      "cpu_percent": 3.2,
      "memory_mb": 85.1,
      "disk_percent": 42.5,
      "recv_count": { "wolfx": 1, "p2p_eew": 0, "quake": 5, "tsunami": 0, "usgs": 2, "volcano": 1 }
    }
  ]
}
```

**CSVエクスポート**: `?format=csv` を付けると同じデータを CSV（UTF-8 BOM付き、
Excelでの文字化け対策済み）でダウンロードできる。`recv_count` の各キーは
`recv_<key>` 列にフラット化される。

```bash
curl "http://localhost:8080/status/history?format=csv" -o history.csv
```

`GET /dashboard` の「CSVをダウンロード」ボタンからも取得可能。

### GET /status/notifications（直近の通知履歴）

各Cog（EEW/地震情報/津波情報/火山情報/噴火速報/噴火警報/USGS/長周期地震動/
気象庁その他）が実際にDiscordへ送信した通知を、新しい順に最大50件保持する
メモリ上のリングバッファ（`core/notification_log.py`）の内容を返す。
`?limit=N` で取得件数を絞り込める。

```bash
curl "http://localhost:8080/status/notifications?limit=10" | jq
```

```json
{
  "count": 2,
  "notifications": [
    { "timestamp": "2026-08-17T10:02:00.000000", "kind": "EEW", "title": "緊急地震速報（第2報）", "detail": "能登半島沖" },
    { "timestamp": "2026-08-17T10:00:00.000000", "kind": "地震情報", "title": "震度速報", "detail": "" }
  ]
}
```

> EEW・地震情報・津波系・長周期地震動・気象庁その他の通知は、CLIテスト
> （`is_test=True`）による通知は記録されない。一方、火山情報・噴火速報・
> 噴火警報・USGSの通知メソッドは `is_test` 引数を持たない設計のため、
> テスト実行時の通知も記録される（`--test_all` 実行時など）。

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

### 自動判定実行（`--test_auto`）

情報ソースによってJSONの形式が細かく異なり、どの `--test_<対象>` を使えば
よいか一見して分かりにくい場合がある（特に気象庁XML由来の
`Control`/`Head`/`Body` 形式は、津波観測情報・津波予報・南海トラフ情報等で
外側の形を共有しており、区別しにくい）。実際に、津波警報・注意報・予報
（VTSE41形式）のJSONを `--test_tsunami`（P2P地震情報API形式用）に誤って
渡してしまい、正しく読み込めなかった事例があったため、**2026-08-30に
`--test_auto` を追加した**。

```bash
python3 bot.py --test_auto path/to/downloaded.json
```

`core/test_runner.py` の `sniff_test_target()` がJSONの構造（`Body`配下の
具体的なキーの有無等、各`notify_*`関数の実際のフィールドアクセス箇所に
基づくフィンガープリント）から対象を判定し、以下のように振る舞う。

- **候補が1件に絞れた場合**: その `--test_<対象>` 相当の処理を自動的に実行する
- **候補が0件（未知の形式）の場合**: 「判定不能」と表示し、何も実行しない
- **候補が2件以上（区別不能）の場合**: 全候補を表示し、どの `--test_<対象>`
  を使うべきかの判断をユーザーに委ねる（誤った対象で実行してしまうことを
  避けるため、この場合は自動実行しない）

```
[TEST] 判定結果: --test_tsunami_forecast 相当と判断しました (TsunamiCog.notify_tsunami_forecast)
```

**【2026-08-31 追加】P2P生EEW（code=556）と地震情報（code=551）の判定改善**
以前は`issue`+`earthquake`という同じトップレベルキーを共有するため、
P2P地震情報の緊急地震速報（生形式）が誤って「地震情報」と判定されて
いた。`issue`オブジェクトの中身（EEWは`eventId`、地震情報は`type`を
持つ、`points`/`areas`の有無等）まで見て区別するよう修正し、正しく
`eew_p2p`と判定されるようになった。

**【2026-08-31 追加】北海道・三陸沖後発地震注意情報と南海トラフ地震
臨時情報の判定改善**
`Body.EarthquakeInfo`を持つJSONを無条件に`nankai_trough`（津波チャンネル
宛て）と判定していたが、「北海道・三陸沖後発地震注意情報」も同じBody
構造を共有しており、本来は`other_quake_advisory_detail`（otherチャンネル
宛て）専用だった。`Head.Title`のテキスト内容で区別するよう修正し、
「北海道」「三陸沖」「後発地震」を含む場合は`other_quake_advisory_detail`
のみに絞り込む。「南海トラフ」「顕著な地震の震源要素更新」は気象庁の
意図的な重複配信（下記「気象庁その他特別情報」参照）により本番でも
2経路が独立して動作しうるため、どちらか一方に決め打ちせず両方を候補
として提示する。

### 一括実行（`--test_all`）

デプロイ前の一括疎通確認用に、`TEST_TARGETS` に登録された全対象を順に
実行するモードも用意している。個別に `--test_<対象> <JSONパス>` を指定する
代わりに、fixture（サンプルJSON）を集めたディレクトリを1つ指定する。

```bash
python3 bot.py --test_all tests/fixtures/
```

`"<fixtures_dir>/<対象>_sample.json"`（例: `tests/fixtures/eew_sample.json`）
という命名規則でファイルを探索し、存在するものだけ実行する。存在しない
対象は SKIP として扱われ、実行を中断せず次の対象へ進む（1つのJSONが
壊れている場合も同様にその対象だけスキップし、全体は継続する）。
`ews`（JSONファイル不要な対象）はfixtureの有無に関わらず常に実行される。

全対象の実行が終わると、OK / NG / SKIP のサマリーが表示される。

```
============================================================
[TEST] 一括テスト完了 — 結果サマリー
[TEST]   ✅ OK   eew
[TEST]   ✅ OK   quake
[TEST]   ✅ OK   tsunami
[TEST]   ⚪ SKIP tsunami_observation
[TEST]   ✅ OK   volcano
[TEST]   ✅ OK   usgs
[TEST]   ✅ OK   ews
[TEST] 合計: 17 件 / OK=6 NG=0 SKIP=11
============================================================
```

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
| `eew` | `EewCog.notify_eew` | `tests/fixtures/eew_sample.json`（Wolfx形式＝変換済みフラット形式） |
| `eew_p2p` | `EewCog.notify_eew`（`core.eew_convert.convert_p2p_eew_to_wolfx`で変換後） | （P2P地震情報の緊急地震速報＝生のcode=556形式。**2026-08-30追加**） |
| `quake` | `QuakeInfoCog.notify_quake` | `tests/fixtures/quake_sample.json` |
| `tsunami` | `TsunamiCog.notify_tsunami` | `tests/fixtures/tsunami_sample.json` |
| `tsunami_observation` | `TsunamiCog.notify_tsunami_observation` | （気象庁 VTSE51 形式のJSONを用意） |
| `tsunami_forecast` | `TsunamiCog.notify_tsunami_forecast` | （気象庁 VTSE41 形式のJSONを用意） |
| `nankai_trough` | `OtherInfoCog.notify_nankai_trough` | （南海トラフ地震臨時情報の詳細JSON。**2026-08-30追加、2026-09-01にcogs/tsunami.pyから移設**） |
| `hypocenter_update` | `OtherInfoCog.notify_hypocenter_update` | （顕著な地震の震源要素更新のお知らせの詳細JSON。**2026-08-30追加、2026-09-01にcogs/tsunami.pyから移設**） |
| `jishin_kanchi` | `JishinKanchiCog.notify_jishin_kanchi` | （P2P地震感知情報 code=9611 形式のJSON。**2026-08-30追加**。これまでCLIテスト対象が存在しなかった） |
| `volcano` | `VolcanoCog._notify_volcano` | `tests/fixtures/volcano_sample.json` |
| `volcano_eruption` | `VolcanoCog._notify_eruption` | （eruption.json の1エントリ形式） |
| `volcano_warning` | `VolcanoCog._notify_warning` | （warning.json の1エントリ形式） |
| `usgs` | `UsgsCog.notify_usgs_quake` | `tests/fixtures/usgs_sample.json` |
| `other_long_period` | `OtherInfoCog.notify_long_period` | （長周期地震動情報の list item 形式） |
| `other_quake_advisory` | `OtherInfoCog.notify_quake_advisory` | （その他地震情報の list item 形式。`quake/data/list.json`の1エントリ） |
| `other_quake_advisory_detail` | `OtherInfoCog.notify_quake_advisory` | （気象庁HPから直接ダウンロードした完全な詳細JSON＝Control/Head/Body形式。**2026-08-31追加**。list_item形式との違いはREADME下記参照） |

**【2026-08-30 修正】** `tsunami_observation` / `tsunami_forecast` の
`validate_expected_fields()` チェック対象フィールドが、P2P地震情報API形式
（`"areas"`）のまま誤って設定されており、実際に期待される気象庁XML由来
（`Control`/`Head`/`Body`形式）の正しいJSONを渡しても「フィールド不足」
という誤った警告が出ていた不具合を修正した（`["Control", "Head", "Body"]`
に修正）。

**【2026-08-30 追加】`--test_eew_p2p`（生のP2P EEW形式への対応）**

P2P地震情報の緊急地震速報（生のcode=556形式。トップレベルに `issue`
（`eventId`/`serial`を持つ）・`earthquake`・`areas` を持つ）は、
`--test_eew` が期待するWolfx形式（変換済みのフラットな `EventID`/
`MaxIntensity` 形式）とは全く異なる。実機でダウンロードした過去データを
`--test_eew` にそのまま渡すと、フィールドが一致せず「不明の緊急地震速報」
という結果になる不具合があった。また `--test_auto` でも、生のP2P EEWは
`issue`+`earthquake` を共有するため地震情報（quake）と誤判定されていた。

本番のWebSocketハンドラは、受信した生データを
`core/eew_convert.py` の `convert_p2p_eew_to_wolfx()` で変換してから
`notify_eew()` に渡している。`--test_eew_p2p` はこの変換ステップを
CLIテストにも組み込み、本番と全く同じ変換関数を通してから通知するように
した。`sniff_test_target()` も、各コードの `issue` オブジェクトが実際に
持つキー（EEWは `eventId`、地震情報は `type`、津波は `earthquake` を
持たない等）まで見て判定するよう修正し、`--test_auto` でも正しく
`eew_p2p` と判定されるようにした。

この仕組み（`TEST_TARGETS` の `data_converter` フック）は汎用的な拡張点
として設計されており、今後別の情報ソースで「生データ→変換してから通知
関数に渡す」ケースが見つかった場合も同様に対応できる。

実行例：

```bash
python3 bot.py --test_eew tests/fixtures/eew_sample.json
python3 bot.py --test_quake tests/fixtures/quake_sample.json
python3 bot.py --test_tsunami tests/fixtures/tsunami_sample.json
python3 bot.py --test_jishin_kanchi tests/fixtures/jishin_kanchi_sample.json
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

表示内容：システムリソース / EEW 状態 / API 受信状況（地震・津波・地震感知情報・長周期地震動・火山・USGS・強震モニタ画像解析検知・長周期地震動モニタ 等） / タスク稼働状態 / 通知先チャンネルマッピング / フィルター設定

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
2. 実観測点データが正しく取得できているか確認
   ```bash
   tail -f qtlbot.log | grep -i "KyoshinStations"
   # "観測点データを取得しました" / "キャッシュから観測点データを読み込みました" が
   # 出ていない場合はネットワーク接続、または KYOSHIN_STATIONS_SOURCE_URL の
   # 到達性を確認する。取得に一度も成功していない場合、キャッシュファイル
   # （KYOSHIN_STATIONS_CACHE_PATH）も存在せず検知機能が実質無効化される
   ```
3. ログで検知の状態を確認
   ```bash
   tail -f qtlbot.log | grep -i kyoshin
   # イベントが生成されているのに通知が来ない場合は
   # KYOSHIN_MIN_STATIONS_SHINDO0 / SHINDO1、KYOSHIN_MIN_NOTIFY_PHASE の閾値を確認
   # 特定の観測点の警告ログが繰り返し出る場合は、その観測点が機器異常として
   # ブラックリスト化されている可能性がある（"ブラックリスト化しました" で検索）
   ```
4. `KYOSHIN_DEBUG_SAVE_IMAGE=true` にして `KYOSHIN_DEBUG_IMAGE_DIR` に保存された画像で誤検知・未検知の状況を事後確認
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
│   ├── tsunami.py            - TsunamiCog: 津波観測・予報・警報通知、EWS信号音（津波）
│   ├── jishin_kanchi.py      - JishinKanchiCog: P2P地震感知情報（code=9611、デフォルト無効）
│   ├── volcano.py            - VolcanoCog: 火山情報・噴火速報・噴火警報
│   ├── usgs.py               - UsgsCog: USGS 海外地震情報
│   ├── other.py              - OtherInfoCog: 長周期地震動・気象庁その他情報（後発地震注意情報・
│   │                            南海トラフ・震源要素更新のお知らせ。2026-09-01にtsunami.pyから
│   │                            notify_hypocenter_update/notify_nankai_troughを移設）
│   ├── system.py             - SystemCog: !status・Web Dashboard・エラー監視・リソース監視
│   └── kyoshin_monitor.py    - KyoshinMonitorCog: 強震モニタ画像解析による揺れ検知
└── core/
    ├── config.py                  - 環境変数読み込み・定数定義
    ├── env_loader.py              - .env本体＋カテゴリ別envファイル（.env.kyoshin等）の
    │                                 読み込み一元管理（2026-08-27〜、.env整理案⑩）
    ├── env_starter.py             - `python3 bot.py --starter` 対話式セットアップウィザード
    │                                 （2026-08-27〜）
    ├── env_audit.py               - `python3 bot.py --check_env` .env整合性チェック
    │                                 （2026-08-27〜、.env整理案⑦）
    ├── test_runner.py             - `python3 bot.py --test_<対象>` CLIテスト実行機能
    │                                 （TEST_TARGETS定義・入力JSON検証・自動判定
    │                                 sniff_test_target。2026-08-30に--test_auto追加）
    ├── logging_setup.py           - ログ設定（RotatingFileHandler・重複抑制）
    ├── audio.py                   - AudioMixin（キュー実体を持つCog用）/ AudioClientMixin（AudioCog参照用）
    ├── tts_engines.py             - TTSエンジン（AquesTalkPi/ScratchTTS）の切り替え・音声合成
    ├── ws_helpers.py              - WebSocket自動再接続の共通ループ（EewCog等が使用）
    ├── fetch_backoff.py           - HTTPポーリングのCircuit Breaker（連続失敗時のバックオフ）
    ├── p2p_image.py               - P2PImageMixin（P2P地震情報CDNの地図画像をEmbedに添付。
    │                                 内容検証付きリトライで QuakeInfoCog/TsunamiCog/EewCog/
    │                                 JishinKanchiCog が共有）
    ├── eew_convert.py             - P2P地震情報（EEW code=556）→ Wolfx形式変換の純粋関数
    │                                 （EewCog._convert_p2p_eew_to_wolfx等から分離。状態非依存）
    ├── notification_log.py        - 実際にDiscordへ送信した通知の履歴記録（全Cog共有の
    │                                 メモリ上リングバッファ、最大50件。Web Dashboardの
    │                                 「直近の通知履歴」表示・GET /status/notifications 用）
    ├── delivery_stats.py          - 通知送信の成功/失敗を記録する軽量統計モジュール
    │                                 （全Cog共有。Web Dashboard「配信成功率」表示用）
    ├── epsp_area.py                - P2P地震情報の地域コード（epsp_area.csv）→地域名変換
    │                                 （地震感知情報の地域表示に使用）
    ├── jishin_kanchi_convert.py    - 地震感知情報の信頼度→レベル変換等の純粋関数
    │                                 （JishinKanchiCogから分離。状態非依存）
    ├── ews_signal.py               - EWS（緊急警報放送）AFSK信号音のビット列組み立て・PCM波形合成
    │                                 （外部ファイル出力なし、生成したPCMをメモリ上のまま再生に渡す）
    ├── kyoshin_shared.py          - 震度色分け・両画像取得・振動レベル取得の共通ロジック
    │                                 （EEW発表時通知・画像解析検知通知の両方から利用）
    ├── kyoshin_stations.py        - 実観測点データ（intensity-points.json）の取得・
    │                                 キャッシュ・K近傍計算・色→震度変換（2026-08〜）
    ├── kyoshin_image_analyzer.py  - HSVマスク処理による画像→震度変換（EEW発表時トリガーの
    │                                 振動モニタ機能 estimate_max_shindo_from_image 用。
    │                                 画像解析検知本体は kyoshin_stations.py に移行済み）
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
| `TsunamiCog` | `cogs/tsunami.py` | JMA 津波 API ポーリング・観測情報・予報 / 警報通知、EWS信号音（津波） |
| `VolcanoCog` | `cogs/volcano.py` | JMA 火山 API ポーリング・噴火速報・噴火警報 |
| `UsgsCog` | `cogs/usgs.py` | USGS API ポーリング・海外地震フィルタリング・通知 |
| `OtherInfoCog` | `cogs/other.py` | 長周期地震動・気象庁その他情報（後発地震注意情報・南海トラフ・震源要素更新のお知らせ） |
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
| `_sample_resource_usage()` | CPU/メモリ/ディスク使用率の計測共通ヘルパー（`_build_status_embed`・`resource_monitor`・`status_history_recorder`が共有。起動時にプライミング済みの永続psutilインスタンス経由でノンブロッキング計測する） |
| `_build_status_embed()` | !status / /qtl_status 共通 Embed 生成 |
| `record_notification()` | 通知（種別・タイトル・時刻）を`core/notification_log.py`のリングバッファへ記録（各Cogのnotify_*から呼び出し） |
| `convert_p2p_eew_to_wolfx()` | P2P地震情報（EEW code=556）→ Wolfx形式変換（`core/eew_convert.py`、状態非依存の純粋関数） |
| `notify_*()` | 各通知関数 |
| `StationStore.load_or_fetch()` / `refresh()` | 実観測点データの取得・キャッシュ・定期更新（`core/kyoshin_stations.py`） |
| `build_k_nearest_neighbors()` | 緯度経度から地理的K近傍を計算（グリッドバケット法で高速化、`core/kyoshin_stations.py`） |
| `color2position()` / `make_shindo_decoder()` | 強震モニタ画像の色→実震度変換（`core/kyoshin_stations.py`。akethimitsuhide/kyoshin-monitor-pythonから移植） |
| `EventManager.ingest()` | 観測点ごとに基準値との差分から上昇トリガーを判定し、ブラックリスト仮判定も行う |
| `EventManager.tick()` | K近傍同時上昇の確認・ブラックリスト確定・イベントの生成/マージ/終了判定を行う |
| `shindo_to_color()` | 実震度から独自カラーマップに基づく通知色を決定（`core/kyoshin_shared.py`） |
| `estimate_max_shindo_from_image()` | `jma_s` 画像から画面内の最大実震度を推定（`core/kyoshin_shared.py`、`KyoshinImageAnalyzer.analyze()`を使用） |

---

## ライセンス
MIT License

## 謝辞
- 気象庁（JMA）API
- Wolfx EEW 配信サービス
- P2P 地震情報
- 米国地質調査所（USGS）

---

**最終更新**: 2026-09-02（EEWの`isAssumption`（PLUM法）判定を地震全体の`condition`のみに限定し、辺縁部エリアの`kindCode=19`だけで震源・マグニチュード全体が「推定なし」表示に潰される不具合を修正／地図画像の探索先ディレクトリ誤り（`cogs/`を指していた）を修正し、北海道・三陸沖後発地震注意情報・南海トラフ地震臨時情報の区域図画像が添付されない不具合を解消／`notify_hypocenter_update`・`notify_nankai_trough`を`cogs/tsunami.py`から`cogs/other.py`（`OtherInfoCog`）へ移設し送信先を`OTHER_CHANNEL_ID`に統一／津波警報・大津波警報の発表・更新時にEWS信号音が再生されない不具合を修正（`TsunamiCog`にEWSトリガーを追加）／`--test_auto`のP2P生EEW誤判定（地震情報と混同）・北海道/南海トラフ誤判定（津波チャンネルに誤送信）を修正／EEWの地域ごとの震度内訳で、下限（`scaleFrom`）が不明だが上限（`scaleTo`、「7以上」等）が判明しているエリアが表示から丸ごと消える不具合を修正（`build_forecast_groups`）／津波情報（気象庁HP系）の通知文でタイトルの重複表示・原因地震行の改行崩れを修正）
**対応 Python**: 3.11+
