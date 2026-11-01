"""
core/eew_wave.py
=================
EEW地図に重ねるP波・S波の円（予報円）の材料を作る純関数モジュール。

  build_wave_overlay(data, ...) : EEWデータ（Wolfx形式のdict）→ WaveOverlay（描画する円と注記）
  circle_ring(lon, lat, r_km)   : 震源から r_km の円周上の頂点（緯度経度）
  parse_eew_time(text)          : OriginTime / AnnouncedTime の文字列 → datetime（JST）

【円の半径】 core/travel_time.py（JMA2001走時表）で、震源の深さと経過秒数から求める。
  経過秒数 = 基準時刻 − OriginTime（発生時刻）
  基準時刻（EEW_WAVE_CIRCLE_TIME_BASE）:
    "render"   : 地図を描く時刻（投稿時刻にいちばん近い。既定）
    "announce" : 気象庁の発表時刻（AnnouncedTime）。得られない場合は "render" で代用する

【描かない場合】 仮定震源要素（PLUM法 = isAssumption）／震源不明／深さ不明／OriginTime が
  解釈できない／経過秒数が負（時計のずれの許容 CLOCK_SKEW_TOLERANCE_SEC を超える）または
  上限（max_elapsed）超え／地震発生から現在までが上限（max_elapsed）超え。
  最後の条件は基準時刻が "announce" でも判定する（過去のEEWの再送・再生で、発表時点の小さな
  円がもっともらしく描かれるのを防ぐ。通常の運用では発生から現在まで数十秒以内なので影響しない）。
  どちらの波も描けなければ None。
"""
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.travel_time import WAVE_P, WAVE_S, TravelTimeTable

logger = logging.getLogger("QTLBot")

JST = timezone(timedelta(hours=9))
EARTH_RADIUS_KM = 6371.0
CLOCK_SKEW_TOLERANCE_SEC = 5.0      # OriginTime が「未来」でもこの秒数までは時計のずれとして 0 秒扱い
TEST_DEFAULT_ELAPSED_SEC = 20.0     # テスト通知で発表時刻が分からないときの経過秒数
_MAX_LAT = 85.0


@dataclass(frozen=True)
class WaveOverlay:
    """地図に描くP波・S波の円。circles は [(WAVE_P|WAVE_S, 半径km), ...]。caption は画像に入れる注記（ASCII）。"""
    circles: tuple
    caption: str = ""


def parse_eew_time(text) -> Optional[datetime]:
    """
    "2026-10-16T12:35:00+09:00" / "2026/10/16 12:35:00" / "2026-10-16 12:35:00" 等を
    タイムゾーン付き datetime にする。タイムゾーンが無い値は JST とみなす。解釈できなければ None。
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s or s == "不明":
        return None
    s = s.replace("/", "-")
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


def parse_depth_km(value) -> Optional[float]:
    """Depth（10 / "10km" / "10" など）を km の数値にする。不明（-1・数値でない）は None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        depth = float(value)
    else:
        text = str(value).replace("km", "").strip()
        try:
            depth = float(text)
        except ValueError:
            return None
    if not math.isfinite(depth) or depth < 0:
        return None
    return depth


def circle_ring(lon: float, lat: float, radius_km: float, steps: int = 72) -> list:
    """
    (lon, lat) から地表を radius_km 進んだ点の集合（球面上の円。等距円筒図に投影しても
    高緯度で歪まない）。経度は震源の経度を基準に連続した値（日付変更線をまたいでも飛ばない）。
    """
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    ang = radius_km / EARTH_RADIUS_KM
    sin_lat1, cos_lat1 = math.sin(lat1), math.cos(lat1)
    sin_ang, cos_ang = math.sin(ang), math.cos(ang)
    ring = []
    for k in range(steps):
        brg = 2 * math.pi * k / steps
        sin_lat2 = sin_lat1 * cos_ang + cos_lat1 * sin_ang * math.cos(brg)
        lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
        lon2 = lon1 + math.atan2(
            math.sin(brg) * sin_ang * cos_lat1,
            cos_ang - sin_lat1 * sin_lat2,
        )
        dlon = (math.degrees(lon2) - lon + 180.0) % 360.0 - 180.0   # 震源の経度からの連続した差
        ring.append((lon + dlon, max(-_MAX_LAT, min(_MAX_LAT, math.degrees(lat2)))))
    return ring


def _valid_lonlat(lon, lat) -> bool:
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return False
    return (math.isfinite(lon) and math.isfinite(lat)
            and lon > -180 and lat > -90 and abs(lat) <= 90)


def elapsed_seconds(data: dict, *, basis: str = "render", now: Optional[datetime] = None) -> Optional[float]:
    """
    OriginTime から基準時刻までの経過秒数。OriginTime が解釈できなければ None。
    basis="announce" で AnnouncedTime が解釈できない場合は "render"（now）で代用する。
    """
    origin = parse_eew_time(data.get("OriginTime"))
    if origin is None:
        return None
    now = now or datetime.now(JST)
    ref = now
    if basis == "announce":
        announced = parse_eew_time(data.get("AnnouncedTime"))
        if announced is not None:
            ref = announced
        else:
            logger.debug("EEW波面円: AnnouncedTime が無いため描画時刻を基準にします")
    return (ref - origin).total_seconds()


def build_wave_overlay(
    data: dict,
    table: Optional[TravelTimeTable],
    *,
    basis: str = "render",
    max_elapsed: float = 180.0,
    now: Optional[datetime] = None,
    test_mode: bool = False,
) -> Optional[WaveOverlay]:
    """
    EEWデータからP波・S波の円を作る。描けない場合は None（理由は上の「描かない場合」）。

    test_mode=True（テスト通知）: サンプルJSONのOriginTimeは過去のため、経過秒数は
    「AnnouncedTime − OriginTime」（得られなければ TEST_DEFAULT_ELAPSED_SEC）を使う。
    """
    if table is None or not isinstance(data, dict):
        return None
    if data.get("isAssumption"):
        return None   # 仮定震源要素（PLUM法）は震源・深さが仮定のため描かない
    if not _valid_lonlat(data.get("Longitude"), data.get("Latitude")):
        return None
    depth = parse_depth_km(data.get("Depth"))
    if depth is None:
        return None

    if test_mode:
        origin = parse_eew_time(data.get("OriginTime"))
        announced = parse_eew_time(data.get("AnnouncedTime"))
        if origin is not None and announced is not None and announced >= origin:
            elapsed = (announced - origin).total_seconds()
        else:
            elapsed = TEST_DEFAULT_ELAPSED_SEC
        ref = datetime.now(JST)
    else:
        origin = parse_eew_time(data.get("OriginTime"))
        if origin is None:
            return None
        now_dt = now or datetime.now(JST)
        # 基準時刻が announce でも、地震発生から「今」までが上限を超えた古い情報は描かない
        if (now_dt - origin).total_seconds() > max_elapsed:
            return None
        elapsed = elapsed_seconds(data, basis=basis, now=now_dt)
        ref = now_dt
        if basis == "announce":
            announced = parse_eew_time(data.get("AnnouncedTime"))
            if announced is not None:
                ref = announced

    if elapsed < -CLOCK_SKEW_TOLERANCE_SEC:
        return None
    elapsed = max(elapsed, 0.0)
    if elapsed > max_elapsed:
        return None

    circles = []
    for wave in (WAVE_P, WAVE_S):
        radius = table.radius_km(depth, elapsed, wave)
        if radius is not None:
            circles.append((wave, radius))
    if not circles:
        return None

    ref_jst = ref.astimezone(JST)
    caption = f"{ref_jst:%H:%M:%S} JST  +{elapsed:.0f}s"
    return WaveOverlay(circles=tuple(circles), caption=caption)
