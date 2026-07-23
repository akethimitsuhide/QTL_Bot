"""
core/kyoshin_detector.py
========================
強震モニタ（リアルタイム震度画像）の色相解析結果から、
数値APIを使わずに「揺れの検知・広がり・終了」を管理するコアロジック。

【設計方針】
このモジュールは discord.py / aiohttp に一切依存しない、
純粋な状態機械（ステートマシン）として実装する。
実際の画像デコード（ピクセル色→リアルタイム震度）は含まない
（別モジュールでの実装、または既存の画像取得ロジックとの統合を想定）。

呼び出し側は、一定間隔（例: 1〜2秒）ごとに
  1. 各観測点の現在のリアルタイム震度を得る（画像解析結果）
  2. EventManager.ingest(station_id, shindo, now) を呼ぶ
  3. EventManager.tick(now) を呼び、イベントの生成・更新・終了を検知する
という流れで使用する。

【三段構えの誤検知対策】
A. 時間軸: 過去10秒分の震度をリングバッファで保持し、
   「現在値 - 10秒前の値」の上昇幅がしきい値を超えたら「上昇トリガー」
B. 空間軸: 上昇トリガーが立った観測点は、あらかじめ静的に持たせた
   近隣観測点リストのうち何点が同時に上昇しているかで real/noise を判定
C. 状態管理: 揺れを検知した観測点群を SeismicEvent としてまとめ、
   別イベントの観測点と隣接したら「より古い(=震源に近い)イベント」へマージ

【ライフサイクル】
- 動的タイマー: 観測点は現在の震度が大きいほど「検知終了までの猶予」が延びる
- イベント終了: 所属観測点が0件になった瞬間にイベントを破棄
- ブラックリスト: 「周囲無反応 + 過去10秒でほぼ変化なし + 現在値が異常に高い」
  観測点は機器異常とみなし、以後の検知対象から除外する
"""
from __future__ import annotations

import time
import uuid
import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("QTLBot")


# ===============================
# 設定値（呼び出し側からオーバーライド可能）
# ===============================
@dataclass
class DetectorConfig:
    history_window_sec: float = 25.0         # 過去何秒分の履歴を保持するか
    poll_interval_sec: float = 1.0           # ingest() が呼ばれる想定間隔（バッファサイズ計算に使用）

    # ⚠️ 重要: 以下の震度関連のしきい値は全て「実震度値」（10倍していない値。
    # 例: 震度3なら 3.0）で統一している。
    # 過去バージョンでは「震度の10倍値」を前提にした設計になっており、
    # 実データ較正済みの KyoshinImageAnalyzer が返す実震度値と単位が
    # 一致していなかった（rise_threshold=0.5 のつもりが実質 震度0.05 の
    # 変化で発火する、という過敏すぎる設定になっていた）。
    # 過敏な誤検知の一因だったため、実震度スケールに統一した。
    rise_threshold: float = 0.5              # 「上昇トリガー」とみなす実震度の上昇幅
    rise_threshold_overrides: dict = field(default_factory=lambda: {
        # ノイズの多い大都市圏は個別にしきい値を上げる（city_group名で指定）
        "tokyo":     0.8,
        "kanagawa":  0.8,
    })

    # 【2026-07-22 追加】基準値(baseline)の計算方法をサンプル平均方式に変更。
    # 参考: https://qiita.com/ingen084/items/82985e8d3227c97c608d
    #       のHTML実装(p_s(14).html)が採用している手法。
    # 従来は「ちょうどhistory_window_sec秒前の1点」を基準値としていたが、
    # これはノイズ1点に基準値が左右されやすく不安定だった。
    # baseline_window_start_sec 〜 baseline_window_end_sec 秒前の
    # 範囲内にあるサンプルの平均値を基準値として使うことで、
    # より安定した基準値を得られるようにする
    # （例: 10〜25秒前の平均。直近すぎる10秒未満は基準値の計算に含めない
    #   ことで、ゆっくり立ち上がる揺れ自体を「基準値の一部」として
    #   吸収してしまうのを防ぐ）。
    baseline_window_start_sec: float = 10.0
    baseline_window_end_sec: float = 25.0

    neighbor_trigger_count: int = 2          # 近隣で同時に何点上昇していれば「本物」とみなすか

    # 【2026-07-22 追加】現在の実震度がこの値以上であれば、上昇幅・速度の
    # 条件を満たしていなくても候補（上昇トリガー）とみなす救済ロジック。
    # 参考HTML実装の `|| latest.value >= 8`（震度1相当）に相当する。
    # ポーリング間隔の谷間で「上昇の瞬間」を捉えきれず、たまたま基準値との
    # 差分が閾値未満になってしまうケースを救済する狙い。
    high_value_bypass_shindo: float = 1.0

    base_timeout_sec: float = 15.0           # 観測点がイベントに留まる基本の猶予時間
    timeout_per_shindo: float = 5.0          # 実震度1につき追加される猶予時間（秒）

    # 【2026-07-22 追加】バグ修正: 「一度検知すると通知が止まらない」対策。
    # 従来は tick() のたびに「現在の震度が active_floor_shindo 以上なら
    # 無条件で base_timeout_sec + shindo*timeout_per_shindo 分だけ延長」
    # という設計だったため、震度が下がらずに高止まりしたまま新規の
    # 上昇（_rose_this_tick）が全く無い状態が続いても、際限なく
    # 延長され続けてしまっていた（画像取得元が同一画像を繰り返し
    # 返してしまうケース等で顕在化する）。
    # 最後に上昇トリガーが立った時刻(last_rise_at)から
    # stale_after_sec 秒以上が経過した観測点は、震度の絶対値に
    # 関わらず「動的延長」を打ち切り、base_timeout_sec のみの
    # 短い延長に切り替えることで、確実にイベントが終了に向かうようにする。
    stale_after_sec: float = 20.0

    blacklist_shindo_threshold: float = 3.0  # 震度3以上。ブラックリスト判定の下限（ingen084氏の記事の基準に準拠）
    blacklist_shindo_threshold_island: float = 4.5  # 離島は震度5弱以上
    blacklist_flat_diff: float = 0.3         # 「ほぼ変化なし」とみなす実震度の上昇幅の上限
    active_floor_shindo: float = 1.0         # 実震度1相当。これ未満に落ち着いたら
                                              # 動的タイマーの延長を止める（平常domain扱い）

    # フェーズ境界（実震度値）。ingen084氏の記事の基準にそのまま準拠:
    # https://qiita.com/ingen084/items/82985e8d3227c97c608d
    phase_bounds: dict = field(default_factory=lambda: {
        "Weaker":   -1.5,  # 実震度-1.5以上-1.0未満
        "Weak":     -1.0,  # 実震度-1.0以上1未満
        "Medium":    1.0,  # 実震度1以上3未満
        "Strong":    3.0,  # 実震度3以上5弱未満
        "Stronger":  4.5,  # 実震度5弱以上
    })


def _phase_from_shindo(shindo: float, config: DetectorConfig) -> str:
    """実震度値（10倍していない値。例: 震度3なら3.0）からフェーズ名を判定する。"""
    phase = "Weaker"
    for name, bound in sorted(config.phase_bounds.items(), key=lambda kv: kv[1]):
        if shindo >= bound:
            phase = name
    return phase


@dataclass
class Station:
    """観測点1件分の状態。"""
    station_id: str
    neighbors: list[str] = field(default_factory=list)
    city_group: str | None = None  # 大都市圏判定用（しきい値個別調整）
    is_island: bool = False        # 離島判定（ブラックリスト閾値切り替え用）

    history: deque = field(default_factory=deque)  # [(timestamp, shindo), ...]
    event_id: str | None = None
    blacklisted: bool = False
    _rose_this_tick: bool = False   # このtickで上昇トリガーが立ったか（内部フラグ）
    _seen_this_tick: bool = False   # このtickでingest()による新規観測値を受け取ったか（内部フラグ）
    _pre_confirmed_this_tick: bool = False  # ClusterTracker等、上位層で既にクラスタ確定済みか（内部フラグ）
    expire_at: float | None = None  # このstationがイベントに留まれる期限（monotonic time）
    last_rise_at: float | None = None  # 最後に上昇トリガー（_rose_this_tick=True）が
                                        # 立った時刻。震度が高止まりしたまま新規の
                                        # 上昇が無い状態が続いても際限なく延長され
                                        # 続けないようにするための基準時刻。

    def push(self, now: float, shindo: float, window_sec: float) -> None:
        self.history.append((now, shindo))
        cutoff = now - window_sec * 1.5  # 少し余裕を持って古いものを捨てる
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def value_at_or_before(self, target_time: float) -> float | None:
        """target_time 以前で最も新しい記録の震度を返す（10秒前の値の取得用）。"""
        result = None
        for t, v in self.history:
            if t <= target_time:
                result = v
            else:
                break
        return result

    def baseline_average(self, now: float, window_start_sec: float, window_end_sec: float) -> float | None:
        """
        (now - window_end_sec) <= t <= (now - window_start_sec) の範囲に
        存在するサンプルの平均値を返す。範囲内にサンプルが1つも無い場合は
        履歴中の最古の値（存在すれば）にフォールバックする。全く履歴が
        無い場合は None を返す。

        参考: https://qiita.com/ingen084/items/82985e8d3227c97c608d
              の参考HTML実装が採用する baselineAvg の計算方法に準拠。
              単一の「N秒前ちょうどの値」ではなく範囲内サンプルの平均を
              取ることで、基準値がノイズ1点に左右されにくくなる。
        """
        if not self.history:
            return None
        lower = now - window_end_sec
        upper = now - window_start_sec
        samples = [v for t, v in self.history if lower <= t <= upper]
        if samples:
            return sum(samples) / len(samples)
        return self.history[0][1]

    @property
    def current_shindo(self) -> float | None:
        return self.history[-1][1] if self.history else None


@dataclass
class SeismicEvent:
    """揺れ検知イベント（1つの地震に対応する観測点群のまとまり）。"""
    event_id: str
    created_at: float
    member_station_ids: set[str] = field(default_factory=set)
    max_shindo: float = -999.0
    phase: str = "Weaker"
    last_updated_at: float = 0.0

    def update_max(self, shindo: float, config: DetectorConfig, now: float) -> None:
        if shindo > self.max_shindo:
            self.max_shindo = shindo
            self.phase = _phase_from_shindo(shindo, config)
        self.last_updated_at = now


class EventManager:
    """
    観測点群を管理し、震度上昇の検知・イベントの生成/更新/マージ/終了を制御する。

    使い方:
        mgr = EventManager(config)
        mgr.register_station("tokyo-001", neighbors=["tokyo-002", "tokyo-003"], city_group="tokyo")
        ...
        while True:
            for sid, shindo in get_current_readings().items():
                mgr.ingest(sid, shindo, now=time.monotonic())
            changes = mgr.tick(now=time.monotonic())
            for change in changes:
                if change.kind == "created":
                    start_kyoshin_image_loop(change.event)
                elif change.kind == "ended":
                    stop_kyoshin_image_loop(change.event_id)
    """

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()
        self.stations: dict[str, Station] = {}
        self.events: dict[str, SeismicEvent] = {}

    # ===============================
    # 観測点登録（静的データ）
    # ===============================
    def register_station(
        self,
        station_id: str,
        neighbors: list[str] | None = None,
        city_group: str | None = None,
        is_island: bool = False,
    ) -> None:
        self.stations[station_id] = Station(
            station_id=station_id,
            neighbors=neighbors or [],
            city_group=city_group,
            is_island=is_island,
        )

    def _threshold_for(self, station: Station) -> float:
        if station.city_group and station.city_group in self.config.rise_threshold_overrides:
            return self.config.rise_threshold_overrides[station.city_group]
        return self.config.rise_threshold

    # ===============================
    # 観測値の取り込み
    # ===============================
    def ingest(self, station_id: str, shindo: float, now: float) -> None:
        station = self.stations.get(station_id)
        if station is None or station.blacklisted:
            return

        if not station.history:
            # 初回データ点: 判定材料が無いため、上昇なし・ブラックリスト判定もスキップする。
            # ここで diff=0 と決め打ちすると、Bot起動直後に本物の急上昇が来た場合でも
            # 「変化なし（フラット）」と誤認し、ブラックリスト判定にまで
            # 誤って合致してしまう（起動直後の地震を機器異常と誤判定するバグの原因）。
            diff = 0.0
            has_baseline = False
        else:
            # 【2026-07-22 変更】基準値の計算を、単一の「N秒前ちょうどの値」
            # から「baseline_window_start_sec〜baseline_window_end_sec秒前
            # の範囲内サンプルの平均」に変更した（参考HTML実装のbaselineAvg
            # 方式）。基準値がノイズ1点に左右されにくくなる。
            prev = station.baseline_average(
                now,
                self.config.baseline_window_start_sec,
                self.config.baseline_window_end_sec,
            )
            if prev is None:
                prev = station.history[0][1]
            diff = shindo - prev
            has_baseline = True

        station.push(now, shindo, self.config.history_window_sec)

        if has_baseline:
            # ── ブラックリスト判定 ──
            # 「周囲無反応 + 過去10秒でほぼ変化なし + 現在値が異常に高い」観測点は機器異常とみなす。
            # 周囲の反応有無は tick() 側で分かるため、ここでは「フラット + 高震度」のみ仮チェックし、
            # 実際の除外は tick() 内で近隣の非反応を確認してから行う。
            threshold = self.config.blacklist_shindo_threshold_island if station.is_island \
                else self.config.blacklist_shindo_threshold
            station._flat_and_high = (abs(diff) < self.config.blacklist_flat_diff and shindo >= threshold)

            # 【2026-07-22 追加】上昇幅による判定に加え、現在値そのものが
            # high_value_bypass_shindo 以上であれば無条件で候補とする
            # 救済ロジック（参考HTML実装の `|| latest.value >= 8` に相当）。
            # ポーリング間隔の谷間で「上昇の瞬間」を捉えきれず、基準値との
            # 差分がたまたま閾値未満になってしまうケースを救済する。
            rose_by_diff = diff >= self._threshold_for(station)
            rose_by_bypass = shindo >= self.config.high_value_bypass_shindo
            station._rose_this_tick = rose_by_diff or rose_by_bypass

            # 【2026-07-22 バグ修正】last_rise_at（動的タイマー延長の基準時刻）
            # は rose_by_diff（＝実際に基準値からの上昇があったこと）が
            # 成立した場合のみ更新する。rose_by_bypass（現在値が高いという
            # 理由だけの救済判定）まで含めて last_rise_at を更新すると、
            # 震度が変化せず高止まりし続けているだけの状態でも
            # 「常に上昇中」と誤認され続け、stale_after_sec による
            # タイムアウト打ち切りが機能しなくなってしまう
            # （＝一度検知すると通知が止まらないバグの直接原因だった）。
            if rose_by_diff:
                station.last_rise_at = now
        else:
            station._flat_and_high = False
            station._rose_this_tick = False

        station._seen_this_tick = True

    def ingest_confirmed(self, station_id: str, shindo: float, now: float) -> None:
        """
        既に上位層（core.kyoshin_cluster_tracker.ClusterTracker 等）で
        クラスタリング・複数フレーム持続確認済みの観測点を直接取り込む。

        通常の ingest() が行う「rise_threshold による上昇トリガー判定」
        「ブラックリスト判定」はスキップし、tick() で直接イベント割当対象
        （confirmed_ids）として扱われるようにする。

        これは、独自の敏感な閾値判定（EventManager内蔵のrise_threshold等）
        が誤検知の温床になっていたため、より頑健なHSVマスク＋クラスタリング＋
        複数フレーム検証を行う上位層に検知の主導権を委ねるためのインターフェース。
        """
        station = self.stations.get(station_id)
        if station is None or station.blacklisted:
            return
        station.push(now, shindo, self.config.history_window_sec)
        station._seen_this_tick = True
        station._pre_confirmed_this_tick = True
        station._rose_this_tick = False  # 独自の上昇トリガー判定は使わない
        station._flat_and_high = False   # ブラックリスト判定もスキップする
        station.last_rise_at = now       # 事前確定扱いのたびに「上昇中」とみなす

    # ===============================
    # tick: イベントの生成・更新・マージ・終了判定
    # ===============================
    def tick(self, now: float) -> list["_Change"]:
        changes: list[_Change] = []

        risen_ids = [sid for sid, st in self.stations.items() if st._rose_this_tick]
        pre_confirmed_ids = [sid for sid, st in self.stations.items() if st._pre_confirmed_this_tick]

        # ── ブラックリスト化（周囲が無反応なのに単独でフラット&高震度） ──
        # 既にイベントに参加中の観測点は、進行中の地震で震度が高止まりしているだけの
        # 可能性があるため、ブラックリスト判定の対象から除外する
        # （「新規の上昇トリガー」の有無だけを見ると、継続中の地震で全観測点が
        # 同時に高止まりした場合に誤って機器異常と判定してしまうため）。
        for sid, st in list(self.stations.items()):
            if st.event_id is not None:
                continue
            if getattr(st, "_flat_and_high", False):
                neighbor_active = any(
                    self.stations[n]._rose_this_tick or self.stations[n].event_id is not None
                    for n in st.neighbors if n in self.stations
                )
                if not neighbor_active:
                    st.blacklisted = True
                    logger.warning(f"KyoshinDetector: 観測点 {sid} をブラックリスト化しました（機器異常疑い）")

        # ── 空間クロスバリデーション: 上昇トリガー成立の判定 ──
        #
        # 【2026-07-22 追加のバグ修正・その2】stale_after_sec による動的
        # タイマー延長の停止だけでは不十分だった。震度が高止まりしたまま
        # _rose_this_tick が true であり続けるセルは、イベント終了直後の
        # tick でも再び confirmed_ids に入ってしまい、「イベント終了 →
        # 同じtickで即座に新規イベント再生成」が無限に繰り返されてしまう
        # バグがあった（「一度検知すると通知が止まらない」の真因その1）。
        #
        # 【2026-07-23 追加のバグ修正・その3】その2の対策
        # （st.event_id is None のセルのみ stale チェックする）だけでは
        # まだ不十分だった。「既にイベントに所属中のセル」は stale
        # チェックが一切スキップされる設計だったため、そのセルは
        # 震度が高いままの限り _rose_this_tick=True であり続け、
        # neighbor_rise_count の計算に使われる際に「別の、まだ
        # イベント未所属の近隣セル」の新規イベント化トリガーとして
        # カウントされ続けてしまう。結果として、1440セル規模の実運用
        # では、既存イベントの近くでノイズが時々発生するたびに新しい
        # セルが次々と合流し、イベント全体が数時間単位で終わらなく
        # なる現象が実際のログで確認された。
        # stale判定は event_id の有無に関わらず一律で適用し、staleな
        # セルは risen_ids・confirmed_ids・neighbor_rise_count のいずれ
        # からも除外する（＝「新しい情報を提供していないセル」として
        # 扱う）ことで、この問題を解消する。イベントからの実際の離脱は
        # 引き続き expire_at ベースの自然減衰に任せる。
        def _is_stale(st: "Station") -> bool:
            return (
                st.last_rise_at is not None
                and (now - st.last_rise_at) >= self.config.stale_after_sec
            )

        # stale なセルは risen_ids から除外した「有効な上昇中セル」集合を
        # 別途用意し、neighbor_rise_count の計算にはこちらを使う。
        active_risen_ids = {sid for sid in risen_ids if not _is_stale(self.stations[sid])}

        confirmed_ids = []
        for sid in active_risen_ids:
            st = self.stations[sid]
            if st.blacklisted:
                continue
            neighbor_rise_count = sum(
                1 for n in st.neighbors
                if n in self.stations and n in active_risen_ids
            )
            if neighbor_rise_count >= self.config.neighbor_trigger_count:
                confirmed_ids.append(sid)

        # ── 事前確定済み（ClusterTracker等）は近隣検証をスキップして直接確定扱いにする ──
        for sid in pre_confirmed_ids:
            st = self.stations[sid]
            if st.blacklisted:
                continue
            if sid not in confirmed_ids:
                confirmed_ids.append(sid)

        # ── イベント割当 / 新規作成 / マージ ──
        for sid in confirmed_ids:
            st = self.stations[sid]
            neighbor_event_ids = {
                self.stations[n].event_id
                for n in st.neighbors
                if n in self.stations and self.stations[n].event_id is not None
            }
            if st.event_id is not None:
                neighbor_event_ids.add(st.event_id)

            if not neighbor_event_ids:
                # 新規イベント生成
                ev = SeismicEvent(
                    event_id=str(uuid.uuid4()),
                    created_at=now,
                    member_station_ids={sid},
                )
                ev.update_max(st.current_shindo, self.config, now)
                self.events[ev.event_id] = ev
                st.event_id = ev.event_id
                changes.append(_Change("created", ev.event_id, ev))
            else:
                # 既存イベントに割当（複数隣接していれば最古のイベントへマージ）
                target_event_id = min(
                    neighbor_event_ids, key=lambda eid: self.events[eid].created_at
                )
                for eid in neighbor_event_ids:
                    if eid != target_event_id:
                        self._merge_events(target_event_id, eid, now, changes)
                target_event = self.events[target_event_id]
                target_event.member_station_ids.add(sid)
                st.event_id = target_event_id
                target_event.update_max(st.current_shindo, self.config, now)
                changes.append(_Change("updated", target_event_id, target_event))

        # ── 動的タイマー更新（イベント所属中の観測点のうち、このtickで新規観測値を受け取ったもののみ延長） ──
        # ingest() されなかった（フィード停止・欠測等）観測点は延長せず、
        # 既存の expire_at のまま時間経過に任せる（＝いずれ自然に失効する）。
        #
        # 重要: 震度が active_floor_shindo 未満（平常域）まで下がった観測点は、
        # 新規観測値を受け取っていても延長しない。実運用では ingest() が
        # 毎tick必ず呼ばれ続けるため、ここで無条件に延長するとイベントが
        # 実質的に永遠に終了しなくなってしまう（揺れが収まったのに
        # 検知が終わらないバグの原因）。
        #
        # 【2026-07-22 追加のバグ修正】上記の対策だけでは、震度が
        # active_floor_shindo 以上のまま「高止まり」し続け、かつ新規の
        # 上昇（_rose_this_tick）が全く起きないケース（画像取得元が
        # 同一画像を繰り返し返してしまう等）で、動的延長が無限に
        # 繰り返されてしまう別のバグがあった。
        # 最後に上昇トリガーが立った時刻(last_rise_at)から
        # stale_after_sec 秒以上が経過している場合は、延長そのものを
        # 行わない（base_timeout_sec分の再延長すら行わない）。
        # ここで「base_timeout_secのみ延長」としてしまうと、ingest()が
        # 毎tick呼ばれ続ける実運用では expire_at が毎回 now+15秒に
        # 再設定され続け、結局 now > expire_at が永遠に成立しない
        # （＝一度検知すると通知が止まらないバグが再現する）。
        # 延長を完全に止めることで、既存の expire_at がそのまま固定され、
        # 時間経過とともに必ず期限切れになるようにする。
        for sid, st in self.stations.items():
            if st.event_id is None or not st._seen_this_tick:
                continue
            shindo = st.current_shindo or 0.0
            if shindo < self.config.active_floor_shindo:
                continue  # 平常域まで収まった → タイマーを延長せず自然減衰に任せる

            is_stale = (
                st.last_rise_at is not None
                and (now - st.last_rise_at) >= self.config.stale_after_sec
            )
            if is_stale:
                continue  # 新規の上昇が長時間無い → 延長せず自然減衰に任せる

            extension = self.config.base_timeout_sec + shindo * self.config.timeout_per_shindo
            st.expire_at = now + extension

        # ── 期限切れ観測点の離脱 ──
        for sid, st in list(self.stations.items()):
            if st.event_id is not None and st.expire_at is not None and now > st.expire_at:
                self._remove_from_event(st, now, changes)

        # ── 所属0件になったイベントの終了 ──
        for eid, ev in list(self.events.items()):
            if not ev.member_station_ids:
                del self.events[eid]
                changes.append(_Change("ended", eid, ev))

        # tickフラグをリセット
        for st in self.stations.values():
            st._rose_this_tick = False
            st._seen_this_tick = False
            st._pre_confirmed_this_tick = False

        return changes

    def _remove_from_event(self, station: Station, now: float, changes: list["_Change"]) -> None:
        eid = station.event_id
        if eid is None:
            return
        ev = self.events.get(eid)
        station.event_id = None
        station.expire_at = None
        if ev is not None:
            ev.member_station_ids.discard(station.station_id)

    def _merge_events(self, keep_id: str, drop_id: str, now: float, changes: list["_Change"]) -> None:
        """drop_id のイベントを keep_id へマージする（keep_id が古い方である前提）。"""
        if keep_id == drop_id or drop_id not in self.events:
            return
        keep_ev = self.events[keep_id]
        drop_ev = self.events.pop(drop_id)
        for sid in drop_ev.member_station_ids:
            self.stations[sid].event_id = keep_id
            keep_ev.member_station_ids.add(sid)
        if drop_ev.max_shindo > keep_ev.max_shindo:
            keep_ev.update_max(drop_ev.max_shindo, self.config, now)
        logger.info(f"KyoshinDetector: イベント {drop_id} を {keep_id} にマージしました")
        changes.append(_Change("merged", keep_id, keep_ev, merged_from=drop_id))


@dataclass
class _Change:
    """tick() が返すイベントライフサイクルの変化通知。"""
    kind: str            # "created" | "updated" | "merged" | "ended"
    event_id: str
    event: SeismicEvent | None = None
    merged_from: str | None = None
