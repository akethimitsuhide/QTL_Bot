"""
core/kyoshin_detector.py
========================
強震モニタ（リアルタイム震度画像）の色相解析結果から、
数値APIを使わずに「揺れの検知・広がり・終了」を管理するコアロジック。

【2026-07-23 根本的な設計見直し】
これまで3回にわたり「一度検知すると通知が止まらない」バグが
形を変えて再発した。すべての再発は、観測点ごとに「震度が高いという
理由だけで無条件に"上昇中"とみなす救済ロジック」と「観測点ごとの
複雑な動的延長タイマー」を組み合わせたことに起因していた。
震度が高止まりし続ける限り「上昇中」の判定が真であり続けてしまい、
それを止めるための特例処理（stale判定）をあちこちに追加するたびに、
別の経路で同じ問題が再発するというパッチワークになっていた。

今回、以下の2点を軸に設計をやり直した。

1. 「上昇トリガー」は基準値との差分のみで判定する（震度の絶対値による
   無条件の救済は行わない）。
   基準値は過去10〜25秒の平均であり、震度が一定値のまま推移すれば
   基準値もその値に収束するため、diff は自然にゼロへ近づく。
   つまり「上昇トリガー」は放っておいても自己終息する性質を持つ。
   これにより「ずっと真であり続ける」状態そのものが構造的に
   発生しなくなる。

2. イベントの生死は、観測点ごとの個別タイマーではなく、
   イベント1つにつきたった1つの値 `last_rise_at`
   （そのイベントに属するいずれかの観測点で、最後に本物の上昇が
   確認された時刻）で判定する。
   `now - event.last_rise_at > event_timeout_sec` を満たしたら
   問答無用でイベントを終了する。判定箇所は tick() 内の1箇所のみ。

この2つにより、以前あった以下の概念をすべて削除した:
  - 観測点ごとの動的延長タイマー（expire_at, base_timeout_sec,
    timeout_per_shindo, active_floor_shindo, stale_after_sec）
  - 震度の絶対値による無条件の上昇判定（high_value_bypass_shindo）
  - 大都市圏ごとの個別しきい値（city_group, rise_threshold_overrides。
    実際にはどこからも指定されていなかった死んだ設定だった）

【三段構えの誤検知対策（変更なし）】
A. 時間軸: 過去10〜25秒の震度の平均を基準値とし、
   「現在値 - 基準値」の上昇幅がしきい値を超えたら「上昇トリガー」
B. 空間軸: 上昇トリガーが立った観測点は、あらかじめ静的に持たせた
   近隣観測点リストのうち何点が同時に上昇しているかで real/noise を判定
C. 状態管理: 揺れを検知した観測点群を SeismicEvent としてまとめ、
   別イベントの観測点と隣接したら「より古い(=震源に近い)イベント」へマージ

【ライフサイクル（単純化後）】
- イベント終了条件はただ1つ: 「そのイベントで最後に上昇トリガーが
  立ってから event_timeout_sec 秒が経過した」
- ブラックリスト: 「周囲無反応 + 過去10〜25秒でほぼ変化なし + 現在値が
  異常に高い」観測点は機器異常とみなし、以後の検知対象から除外する

参考: https://qiita.com/ingen084/items/82985e8d3227c97c608d
      （強震モニタの画像から揺れていることを検知する／ingen084氏）

【このモジュールが discord.py / aiohttp に依存しない理由】
純粋な状態機械（ステートマシン）として実装し、単体テストしやすくする
ため。実際の画像デコード（ピクセル色→リアルタイム震度）は含まない。

呼び出し側は、一定間隔（例: 1〜2秒）ごとに
  1. 各観測点の現在のリアルタイム震度を得る（画像解析結果）
  2. EventManager.ingest(station_id, shindo, now) を呼ぶ
  3. EventManager.tick(now) を呼び、イベントの生成・更新・終了を検知する
という流れで使用する。
"""
from __future__ import annotations

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
    # ⚠️ 重要: 以下の震度関連のしきい値は全て「実震度値」（10倍していない値。
    # 例: 震度3なら 3.0）で統一している。
    history_window_sec: float = 25.0         # 過去何秒分の履歴を保持するか

    rise_threshold: float = 0.5              # 「上昇トリガー」とみなす実震度の上昇幅（基準値との差分）

    # 基準値(baseline)は「過去 baseline_window_start_sec 〜
    # baseline_window_end_sec 秒前」の範囲内サンプルの平均で計算する
    # （参考: https://qiita.com/ingen084/items/82985e8d3227c97c608d
    #  のHTML実装が採用するbaselineAvg方式）。単一の「N秒前ちょうどの値」
    # より、ノイズ1点に基準値が左右されにくく安定する。
    baseline_window_start_sec: float = 10.0
    baseline_window_end_sec: float = 25.0

    neighbor_trigger_count: int = 2          # 近隣で同時に何点上昇していれば「本物」とみなすか

    # イベントの生死判定はこれ1つだけ。イベントに属するいずれかの観測点で
    # 最後に「本物の上昇トリガー」が立ってから、この秒数が経過したら
    # イベントを終了する。
    event_timeout_sec: float = 60.0

    # ブラックリスト（機器異常疑いの観測点を検知対象から除外する）関連。
    blacklist_shindo_threshold: float = 3.0         # 震度3以上
    blacklist_shindo_threshold_island: float = 4.5  # 離島は震度5弱以上
    blacklist_flat_diff: float = 0.3                # 「ほぼ変化なし」とみなす上昇幅の上限

    # フェーズ境界（実震度値）。ingen084氏の記事の基準にそのまま準拠。
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
    is_island: bool = False        # 離島判定（ブラックリスト閾値切り替え用）

    history: deque = field(default_factory=deque)  # [(timestamp, shindo), ...]
    event_id: str | None = None
    blacklisted: bool = False
    _rose_this_tick: bool = False   # このtickで上昇トリガーが立ったか（基準値との差分のみで判定。
                                     # 震度の絶対値による無条件の救済は行わない＝自己終息する）
    _flat_and_high: bool = False    # ブラックリスト判定の仮フラグ

    def push(self, now: float, shindo: float, window_sec: float) -> None:
        self.history.append((now, shindo))
        cutoff = now - window_sec * 1.5  # 少し余裕を持って古いものを捨てる
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def baseline_average(self, now: float, window_start_sec: float, window_end_sec: float) -> float | None:
        """
        (now - window_end_sec) <= t <= (now - window_start_sec) の範囲に
        存在するサンプルの平均値を返す。範囲内にサンプルが1つも無い場合は
        履歴中の最古の値（存在すれば）にフォールバックする。全く履歴が
        無い場合は None を返す。
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

    # イベントの生死を決めるたった1つの値。イベントに属する
    # いずれかの観測点で本物の上昇トリガーが立つたびに now で更新する。
    # tick() はこれだけを見て「now - last_rise_at > event_timeout_sec」
    # なら問答無用でイベントを終了する。
    last_rise_at: float = 0.0

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
        mgr.register_station("g0_0", neighbors=["g1_0", "g0_1", "g1_1"])
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
        is_island: bool = False,
    ) -> None:
        self.stations[station_id] = Station(
            station_id=station_id,
            neighbors=neighbors or [],
            is_island=is_island,
        )

    # ===============================
    # 観測値の取り込み
    # ===============================
    def ingest(self, station_id: str, shindo: float, now: float) -> None:
        """
        観測点の最新の実震度を取り込む。「上昇トリガーが立ったか」は
        基準値（過去10〜25秒平均）との差分のみで判定する。震度の絶対値が
        高いというだけで無条件に上昇扱いにする特例は設けない
        （そうした特例は、震度が高止まりし続ける限り上昇判定を永久に
        真にし続けてしまい、「検知が終わらないバグ」の直接原因になる
        ため、意図的に排除している）。
        """
        station = self.stations.get(station_id)
        if station is None or station.blacklisted:
            return

        if not station.history:
            # 初回データ点: 判定材料が無いため、上昇なし・ブラックリスト判定もスキップする。
            diff = 0.0
            has_baseline = False
        else:
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
            # 「周囲無反応 + 過去10〜25秒でほぼ変化なし + 現在値が異常に高い」
            # 観測点は機器異常とみなす。周囲の反応有無は tick() 側で分かる
            # ため、ここでは「フラット + 高震度」のみ仮チェックし、実際の
            # 除外は tick() 内で近隣の非反応を確認してから行う。
            threshold = self.config.blacklist_shindo_threshold_island if station.is_island \
                else self.config.blacklist_shindo_threshold
            station._flat_and_high = (abs(diff) < self.config.blacklist_flat_diff and shindo >= threshold)

            # 「上昇トリガー」は基準値との差分のみで判定する。
            station._rose_this_tick = diff >= self.config.rise_threshold
        else:
            station._flat_and_high = False
            station._rose_this_tick = False

    # ===============================
    # tick: イベントの生成・更新・マージ・終了判定
    # ===============================
    def tick(self, now: float) -> list["_Change"]:
        changes: list[_Change] = []

        risen_ids = {sid for sid, st in self.stations.items() if st._rose_this_tick}

        # ── ブラックリスト化（周囲が無反応なのに単独でフラット&高震度） ──
        # 既にイベントに参加中の観測点は、進行中の地震で震度が高止まりしている
        # 可能性があるため、ブラックリスト判定の対象から除外する。
        for sid, st in list(self.stations.items()):
            if st.event_id is not None:
                continue
            if st._flat_and_high:
                neighbor_active = any(
                    self.stations[n]._rose_this_tick or self.stations[n].event_id is not None
                    for n in st.neighbors if n in self.stations
                )
                if not neighbor_active:
                    st.blacklisted = True
                    logger.warning(f"KyoshinDetector: 観測点 {sid} をブラックリスト化しました（機器異常疑い）")

        # ── 空間クロスバリデーション: 上昇トリガー成立の判定 ──
        # risen_ids は基準値との差分のみで決まる「edge」な集合であり、
        # 震度が変化しなくなれば diff は自然にゼロへ近づいて risen_ids から
        # 自然に外れる（＝この集合自体がstale化しない設計になっている）。
        confirmed_ids = []
        for sid in risen_ids:
            st = self.stations[sid]
            if st.blacklisted:
                continue
            neighbor_rise_count = sum(1 for n in st.neighbors if n in risen_ids)
            if neighbor_rise_count >= self.config.neighbor_trigger_count:
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
                    last_rise_at=now,
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
                target_event.last_rise_at = now  # このイベントで「本物の上昇」が今起きた
                st.event_id = target_event_id
                target_event.update_max(st.current_shindo, self.config, now)
                changes.append(_Change("updated", target_event_id, target_event))

        # ── イベント終了判定（唯一の条件: 最後の上昇からevent_timeout_sec経過） ──
        # 観測点ごとの個別タイマーは一切持たない。イベント1つにつき
        # last_rise_at という1つの値だけを見るため、判定ロジックは
        # ここ1箇所に集約される。
        for eid, ev in list(self.events.items()):
            if now - ev.last_rise_at > self.config.event_timeout_sec:
                for sid in ev.member_station_ids:
                    st = self.stations.get(sid)
                    if st is not None:
                        st.event_id = None
                del self.events[eid]
                changes.append(_Change("ended", eid, ev))

        return changes

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
        keep_ev.last_rise_at = max(keep_ev.last_rise_at, drop_ev.last_rise_at)
        logger.info(f"KyoshinDetector: イベント {drop_id} を {keep_id} にマージしました")
        changes.append(_Change("merged", keep_id, keep_ev, merged_from=drop_id))


@dataclass
class _Change:
    """tick() が返すイベントライフサイクルの変化通知。"""
    kind: str            # "created" | "updated" | "merged" | "ended"
    event_id: str
    event: SeismicEvent | None = None
    merged_from: str | None = None
