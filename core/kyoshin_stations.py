"""
core/kyoshin_stations.py
==========================
強震モニタの「実観測点データ」を扱うモジュール。

【背景】
従来（〜2026-08）は、観測点ごとの座標・ピクセル位置対応表を持たない
制約から、jma_s画像をN×Nピクセルのグリッドセルに分割し、セルを
「疑似観測点」として扱っていた（core/kyoshin_image_analyzer.py の
build_station_grid / analyze_all）。

ingen084氏が公開している観測点データ（kyoshin-monitor-observation-points
リポジトリの intensity-points.json）には、実際の観測点コード・名称・
緯度経度・画像上のピクセル座標・休止フラグが含まれているため、
このモジュールではそれを取得・キャッシュし、疑似グリッドの代わりに
実観測点を core.kyoshin_detector.EventManager に登録できるようにする。

参考実装（akethimitsuhide/kyoshin-monitor-python、フォーク元 t0729/
kyoshin-monitor-python）から、画像の色→実震度への変換式
（color2position、通称「フランソワさんの式」）を移植した。

【観測点データの「やや古さ」への対処】
intensity-points.json はコミュニティ管理のデータであり、NIED側の
観測点新設・廃止に追従しきれていない場合がある。これに対しては
2段構えで対処する:
  1. 静的な対処: is_suspended=true の観測点、pointデータの無い観測点は
     登録時点で除外する
  2. 動的な対処: 登録済みだが実際には壊れている/移設された観測点は、
     core.kyoshin_detector.EventManager 側の「10秒間フラット張り付き」
     ブラックリスト機構が実行時に自動検出・除外する（既存機構をそのまま
     活用でき、このモジュール側で追加対応は不要）
  さらに KYOSHIN_STATIONS_REFRESH_SEC ごとにソースを再取得することで、
  ingen084氏側のデータ更新にも追従する。

【色→震度変換について】
color2position() が None を返すケース（低彩度・低明度＝背景色）や、
変換後の震度が有効範囲外（-3.0〜6.9）のケースは、フォーク版では
「7.0」という無効値センチネルを返す仕様になっている。しかし
このセンチネルをそのまま core.kyoshin_detector.EventManager.ingest() に
渡すと、「観測点が突然震度7を検知した」という値として扱われてしまい、
複数観測点で同時に起きた場合に空間クロスバリデーションを偽陽性で
通過してしまう危険がある（今回の再設計全体の目的である誤検知対策に
真っ向から反する）。そのため本モジュールでは、無効値センチネルは
「静穏（背景相当）」を意味する固定の低い代表値
（core.kyoshin_image_analyzer.py の inactive_value と同じ考え方）に
読み替える。これにより、EventManager側の時系列追跡は途切れず、
かつ異常な高震度が誤って注入されることもない。
"""
from __future__ import annotations

import colorsys
import json
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger("QTLBot")


# ===============================
# データ構造
# ===============================
@dataclass
class KyoshinStationMeta:
    """intensity-points.json 1件分の観測点メタデータ。"""
    code: str
    name: str
    region: str
    sub_region: str
    latitude: float
    longitude: float
    pixel_x: int
    pixel_y: int

    @property
    def is_island(self) -> bool:
        """
        離島判定（簡易ヒューリスティック）。
        core.kyoshin_detector.Station.is_island（ブラックリスト閾値の
        切り替えに使用）に渡すための判定で、region/sub_regionに
        「島」「諸島」を含むかで判定する。完全な精度は求めず、
        離島特有の観測点疎密によるノイズ傾向への簡易対応として使う。
        """
        text = f"{self.region}{self.sub_region}"
        return any(kw in text for kw in ("諸島", "島", "沖縄"))


# ===============================
# 色→実震度変換（フォーク版から移植）
# ===============================
def color2position(r: int, g: int, b: int) -> Optional[float]:
    """
    強震モニタ画像のRGB値を、カラースケール上の位置(0.0〜)に変換する
    多項式補間（通称「フランソワさんの式」）。

    出典: akethimitsuhide/kyoshin-monitor-python（フォーク元:
    t0729/kyoshin-monitor-python）の color2position() を移植。

    低彩度・低明度のピクセル（背景の地図・海域等、カラースケール外の色）
    は None を返す。
    """
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if not (v > 0.05 and s > 0.75):
        return None
    if h > 0.1476:
        p = (280.31 * (h ** 6) - 916.05 * (h ** 5) + 1142.6 * (h ** 4)
             - 709.95 * (h ** 3) + 234.65 * (h ** 2) - 40.27 * h + 3.2217)
    elif 0.001 < h <= 0.1476:
        p = 151.4 * (h ** 4) - 49.32 * (h ** 3) + 6.753 * (h ** 2) - 2.481 * h + 0.9033
    else:
        p = -0.005171 * (v ** 2) - 0.3282 * v + 1.2236
    return max(p, 0)


def make_shindo_decoder(inactive_sentinel: float):
    """
    shindo_from_rgb(r, g, b) -> float を生成するファクトリ。

    inactive_sentinel: color2position() が None を返した場合、または
    変換後の震度が有効範囲外（-3.0〜6.9）だった場合に返す代表値。
    「7.0を絶対に返さない」という安全設計上の理由から、フォーク版の
    無効値センチネル（7.0）はそのまま使わない（モジュール docstring 参照）。
    """
    def shindo_from_rgb(r: int, g: int, b: int) -> float:
        v = shindo_from_rgb_or_none(r, g, b)
        return inactive_sentinel if v is None else v
    return shindo_from_rgb


def shindo_from_rgb_or_none(r: int, g: int, b: int) -> Optional[float]:
    """
    RGBを震度に変換する。色がカラースケール外（背景・地図等）の場合や
    変換後の値が有効範囲外（-3.0〜6.9）の場合は None を返す
    （make_shindo_decoder のようにセンチネル値へ丸め込まない生の変換）。

    make_shindo_decoder が返す shindo_from_rgb() は単一ピクセル方式
    向けにセンチネル値へ丸め込むが、パッチサンプリング
    （make_patch_shindo_sampler）では「このピクセルは背景相当だった」
    という情報自体を、パッチ内の集約（中央値/平均）から除外するために
    必要となるため、この生の変換を別途公開している。
    """
    p = color2position(r, g, b)
    if p is None:
        return None
    shindo = round(10.0 * float(p) - 3.0, 1)
    if shindo < -3.0 or shindo > 6.9:
        return None
    return shindo


def make_patch_shindo_sampler(inactive_sentinel: float, radius: int, aggregation: str = "median"):
    """
    【2026-09-09 追加】パッチサンプリング方式の shindo_from_patch(pixels,
    x, y, w, h) -> float を生成するファクトリ。

    【背景】単一ピクセルのみをサンプリングする方式（2026-08〜）は、
    旧・グリッド方式（N×N平均で平滑化済み）と比べてノイズに弱く、
    KYOSHIN_RISE_THRESHOLD / KYOSHIN_NEIGHBOR_TRIGGER_COUNT を本来より
    厳しめの値に引き上げて凌ぐ「一時的な緩和措置」が続いていた
    （core/config.py 参照）。本関数はその恒久対策として、観測点の
    ピクセル位置を中心とした (2*radius+1)^2 の正方形パッチ内の各
    ピクセルを個別に震度へ変換し、その中央値（または平均）を
    その観測点の震度として採用する。

    【なぜRGBを平均せず、震度に変換してから集約するのか】
    color2position() はHSVの非線形な多項式補間であり、色空間上で
    単純にRGBを平均すると、パッチ内に「カラースケール上の色」と
    「背景色（地図・海域）」が混在した場合に、どちらのカテゴリにも
    属さない無意味な色（≒誤った震度）に化けてしまう危険がある。
    そのため、各ピクセルをまず個別に「震度 or 背景（None）」へ変換した
    上で、震度として有効だった値だけを対象に集約する
    （shindo_from_rgb_or_none参照）。

    【中央値をデフォルトにしている理由】
    平均は少数の外れ値（単一ピクセルの色化け等のノイズ）に弱い。
    中央値はそうした外れ値の影響を受けにくく、旧グリッド方式ほどの
    過剰な平滑化（震度変化への追従の鈍化）も避けられるため、
    デフォルトの集約方式とした。

    Parameters
    ----------
    inactive_sentinel : パッチ内が全て背景相当（有効な震度が1つも
        得られなかった）場合に返す代表値。単一ピクセル方式と同じ考え方。
    radius : パッチの半径（ピクセル）。0を指定すると単一ピクセル方式と
        等価（呼び出し側は通常 radius=0 のときはこの関数自体を使わず
        make_shindo_decoder の方を使うこと。cogs/kyoshin_monitor.py参照）。
    aggregation : "median"（既定）または "mean"。
    """
    if aggregation not in ("median", "mean"):
        raise ValueError(f"aggregation は 'median' か 'mean' を指定してください: {aggregation!r}")

    def shindo_from_patch(pixels, x: int, y: int, w: int, h: int) -> float:
        values: list[float] = []
        for dy in range(-radius, radius + 1):
            py = y + dy
            if not (0 <= py < h):
                continue
            for dx in range(-radius, radius + 1):
                px = x + dx
                if not (0 <= px < w):
                    continue
                r, g, b = pixels[px, py]
                v = shindo_from_rgb_or_none(r, g, b)
                if v is not None:
                    values.append(v)

        if not values:
            return inactive_sentinel

        if aggregation == "median":
            values.sort()
            mid = len(values) // 2
            if len(values) % 2 == 1:
                result = values[mid]
            else:
                result = (values[mid - 1] + values[mid]) / 2.0
        else:
            result = sum(values) / len(values)

        return round(result, 1)

    return shindo_from_patch


# ===============================
# 距離計算・K近傍
# ===============================
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の大圏距離をkmで返す。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def build_k_nearest_neighbors(
    stations: list[KyoshinStationMeta], k: int, cell_deg: float = 0.5
) -> dict[str, list[str]]:
    """
    観測点リストから、各観測点の地理的K近傍（haversine距離）を計算して返す。

    約1,500〜1,700件規模の全観測点で単純な総当たり(O(N^2))を行うと
    Raspberry Pi上で数秒かかる（実測: 約1600件で3秒強）ため、
    緯度経度をcell_deg度四方のバケットに分割し、自セル＋周囲の
    バケットのみを候補とするグリッドバケット法で高速化する
    （実測: 同条件で約0.1〜0.2秒。素朴な総当たり実装との出力一致も
    検証済み）。

    候補が k 件以下しかない疎な地域（離島等）では、候補が集まるまで
    探索リング（周囲何セル分見るか）を自動的に広げる。

    戻り値: {station_code: [近い順にk件のstation_code, ...], ...}
    """
    buckets: dict[tuple[int, int], list[KyoshinStationMeta]] = defaultdict(list)
    for st in stations:
        key = (int(st.latitude // cell_deg), int(st.longitude // cell_deg))
        buckets[key].append(st)

    result: dict[str, list[str]] = {}
    for st in stations:
        cy = int(st.latitude // cell_deg)
        cx = int(st.longitude // cell_deg)

        ring = 1
        candidates: list[KyoshinStationMeta] = []
        while True:
            candidates = []
            for dy in range(-ring, ring + 1):
                for dx in range(-ring, ring + 1):
                    candidates.extend(buckets.get((cy + dy, cx + dx), []))
            # 自分自身を除いた候補数がk件を超えていれば十分
            if len(candidates) - 1 >= k or ring >= 8:
                break
            ring += 1

        dists = []
        for other in candidates:
            if other.code == st.code:
                continue
            d = haversine_km(st.latitude, st.longitude, other.latitude, other.longitude)
            dists.append((d, other.code))
        dists.sort(key=lambda x: x[0])
        result[st.code] = [code for _, code in dists[:k]]

    return result


# ===============================
# データ取得・パース・キャッシュ
# ===============================
def parse_intensity_points(raw: list[dict]) -> list[KyoshinStationMeta]:
    """
    intensity-points.json（生JSON、リスト形式）を KyoshinStationMeta の
    リストへ変換する。is_suspended=true の観測点、pointデータの無い
    観測点は除外する。
    """
    result: list[KyoshinStationMeta] = []
    for item in raw:
        if item.get("is_suspended"):
            continue
        point = item.get("point")
        if not point:
            continue
        center = point.get("center_point") or {}
        offset = point.get("offset") or {}
        location = item.get("location") or {}
        lat = location.get("latitude")
        lon = location.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            px = int(center.get("x", 0)) + int(offset.get("x", 0))
            py = int(center.get("y", 0)) + int(offset.get("y", 0))
        except (TypeError, ValueError):
            continue

        result.append(KyoshinStationMeta(
            code=item.get("code", ""),
            name=item.get("name", "不明"),
            region=item.get("region", ""),
            sub_region=item.get("sub_region", ""),
            latitude=float(lat),
            longitude=float(lon),
            pixel_x=px,
            pixel_y=py,
        ))
    return result


class StationStore:
    """
    intensity-points.json の取得・キャッシュ・定期更新を管理するクラス。

    使い方:
        store = StationStore(source_url=..., cache_path=..., refresh_interval_sec=3600)
        stations = await store.load_or_fetch(session)   # 起動時
        ...
        stations = await store.refresh(session)         # 定期実行（Cog側でタスク化）
    """

    def __init__(self, source_url: str, cache_path: str, refresh_interval_sec: int = 3600):
        self.source_url = source_url
        self.cache_path = cache_path
        self.refresh_interval_sec = refresh_interval_sec
        self.stations: list[KyoshinStationMeta] = []
        self.last_fetched_at: float = 0.0  # time.time() 基準

    # ── キャッシュファイルI/O ──
    def _load_cache_file(self) -> Optional[tuple[float, list[dict]]]:
        """
        キャッシュファイルを読み込み、(fetched_at, raw_list) を返す。
        存在しない/壊れている場合は None を返す。
        """
        if not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return float(obj["fetched_at"]), obj["data"]
        except Exception as e:
            logger.warning(f"KyoshinStations: キャッシュファイル読み込みエラー: {e}")
            return None

    def _save_cache_file(self, fetched_at: float, raw_list: list[dict]) -> None:
        try:
            tmp_path = self.cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": fetched_at, "data": raw_list}, f, ensure_ascii=False)
            os.replace(tmp_path, self.cache_path)  # 書き込み中のクラッシュで壊れないようatomic置換
        except Exception as e:
            logger.warning(f"KyoshinStations: キャッシュファイル書き込みエラー: {e}")

    # ── 取得 ──
    async def _fetch_from_source(self, session: aiohttp.ClientSession) -> Optional[list[dict]]:
        try:
            async with session.get(
                self.source_url, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"KyoshinStations: 観測点データ取得失敗 (status={resp.status})")
                    return None
                raw_bytes = await resp.read()
        except Exception as e:
            logger.warning(f"KyoshinStations: 観測点データ取得エラー: {e}")
            return None

        try:
            # intensity-points.json はUTF-8 BOM付きで配信されているため utf-8-sig で除去する
            text = raw_bytes.decode("utf-8-sig")
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError("トップレベルがリストではありません")
            return data
        except Exception as e:
            logger.warning(f"KyoshinStations: 観測点データのパースエラー: {e}")
            return None

    async def load_or_fetch(self, session: aiohttp.ClientSession) -> list[KyoshinStationMeta]:
        """
        起動時に呼ぶ。キャッシュが新しければキャッシュを使い、
        古い/存在しない場合はソースから取得してキャッシュを更新する。
        取得に失敗した場合は、古くてもキャッシュがあればそれを使う
        （キャッシュも無ければ空リストを返し、この回の起動では
        強震モニタ機能は実質無効になる。次回の定期更新で回復する）。
        """
        cached = self._load_cache_file()
        now = time.time()

        if cached is not None:
            fetched_at, raw_list = cached
            age_sec = now - fetched_at
            if age_sec < self.refresh_interval_sec:
                self.last_fetched_at = fetched_at
                self.stations = parse_intensity_points(raw_list)
                logger.info(
                    f"KyoshinStations: キャッシュから観測点データを読み込みました "
                    f"({len(self.stations)}件、{age_sec:.0f}秒前に取得)"
                )
                return self.stations

        # キャッシュが無い/古い → 取得を試みる
        raw_list = await self._fetch_from_source(session)
        if raw_list is not None:
            self._save_cache_file(now, raw_list)
            self.last_fetched_at = now
            self.stations = parse_intensity_points(raw_list)
            logger.info(f"KyoshinStations: 観測点データを取得しました ({len(self.stations)}件)")
            return self.stations

        # 取得失敗 → 古くてもキャッシュがあればそれを使う
        if cached is not None:
            fetched_at, raw_list = cached
            self.last_fetched_at = fetched_at
            self.stations = parse_intensity_points(raw_list)
            logger.warning(
                f"KyoshinStations: 最新データ取得に失敗したため、"
                f"古いキャッシュ（{(now - fetched_at) / 3600:.1f}時間前）を使用します "
                f"({len(self.stations)}件)"
            )
            return self.stations

        logger.error("KyoshinStations: 観測点データを取得できず、キャッシュもありません。強震モニタ機能は無効化されます")
        self.stations = []
        return self.stations

    async def refresh(self, session: aiohttp.ClientSession) -> list[KyoshinStationMeta]:
        """
        定期更新タスクから呼ぶ。無条件にソースから再取得を試みる。
        失敗時は既存の self.stations をそのまま維持する（呼び出し元で
        EventManagerへの再登録は「新規追加分のみ」を行う設計のため、
        取得失敗時に何もしなくても安全）。
        """
        raw_list = await self._fetch_from_source(session)
        if raw_list is None:
            logger.warning("KyoshinStations: 定期更新の取得に失敗しました。既存データを維持します")
            return self.stations

        now = time.time()
        self._save_cache_file(now, raw_list)
        self.last_fetched_at = now
        new_stations = parse_intensity_points(raw_list)
        added = len(new_stations) - len(self.stations)
        logger.info(
            f"KyoshinStations: 観測点データを再取得しました "
            f"({len(new_stations)}件、前回比 {added:+d}件)"
        )
        self.stations = new_stations
        return self.stations
