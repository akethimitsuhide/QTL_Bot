"""
core/eew_convert.py
====================
cogs/eew.py（955行）の肥大化対策として、Bot/Cogの状態（self.xxx）に
依存しない「純粋な変換ロジック」を切り出したモジュール。

【切り出しの方針】
ここに置くのは「入力（dict等）だけから出力が決まる」関数のみ。
EventIDごとの累積状態管理（eew_state）や音声再生判定のような、
Cogのインスタンス状態に依存する処理は cogs/eew.py 側に残す
（core/constants.py のdocstringにある設計方針と同じ考え方）。

【元 cogs/eew.py からの移行元】
EewCog._convert_p2p_eew_to_wolfx() / EewCog._extract_alert_regions()
（2026-08 リファクタリングで分離。EewCog側には後方互換の薄い
ラッパーメソッドを残してあるため、外部からの呼び出し方法は
変わらない）。
"""
import logging
import traceback

from core.helpers import safe_int, safe_float

logger = logging.getLogger("QTLBot")


def convert_p2p_eew_to_wolfx(p2p_data: dict) -> dict | None:
    """
    P2P地震情報の緊急地震速報（code=556）を Wolfx 形式に変換する。

    実際のフィールド仕様（確認済み）:
      issue.eventId   : EventID（"20260515202209" 形式）
      issue.serial    : 情報番号（文字列）
      earthquake.hypocenter.depth     : 深さ km（int）
      earthquake.hypocenter.magnitude : マグニチュード（float）
      earthquake.hypocenter.name      : 震央地名
      earthquake.originTime           : 発生時刻（"2026/05/15 20:22:02" 形式）
      earthquake.condition            : "仮定震源要素" のとき PLUM 法
      areas[].scaleFrom / scaleTo     : int（-1/0/10/20/30/40/45/50/55/60/70）
                                        scaleTo のみ 99（以上）あり
      areas[].kindCode                : "10"=警報未到達 / "11"=到達済 / "19"=PLUM法
      areas[].arrivalTime             : 到達予測時刻（null の場合あり）
      areas[].name                    : 細分区域名
      areas[].pref                    : 府県予報区
    """
    try:
        issue    = p2p_data.get("issue", {})
        event_id = issue.get("eventId", "")
        serial   = safe_int(issue.get("serial", "1")) or 1

        if p2p_data.get("cancelled"):
            return {
                "type":      "jma_eew",
                "Title":     "緊急地震速報（警報）",
                "EventID":   event_id,
                "Serial":    serial,
                "isCancel":  True,
                "isFinal":   False,
                "Magnitude": -1.0,
                "Depth":     -1,
            }

        eq    = p2p_data.get("earthquake", {}) or {}
        hypo  = eq.get("hypocenter", {}) or {}
        areas = p2p_data.get("areas", []) or []

        # PLUM 法判定
        is_plum = (
            eq.get("condition") == "仮定震源要素"
            or any(str(a.get("kindCode", "")) == "19" for a in areas)
        )

        # scaleFrom の Enum に 99 は存在しない（scaleTo のみ）
        scale_map = {
            -1: "不明", 0: "0", 10: "1", 20: "2", 30: "3", 40: "4",
            45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7",
        }

        kind_type_map = {
            "10": "警報",    # 主要動未到達
            "11": "到達済",  # 主要動到達済み
            "19": "警報",    # PLUM法
        }

        max_scale_val = -1
        warn_areas    = []
        has_warn      = False  # kindCode=10 or 19 が1つでもあれば isWarn=True

        for area in areas:
            kind_code = str(area.get("kindCode", ""))
            sf = safe_int(area.get("scaleFrom", -1))
            st = safe_int(area.get("scaleTo",   -1))

            # 最大震度追跡。
            # scaleTo=99（震度7以上）は「70(震度7)以上」を意味するため、
            # 70として扱う必要がある。st in (-1, 99) のとき scaleFrom
            # （下限値）にフォールバックしてしまうと、99のケースで
            # 本来の上限値(70)ではなく下限値を採用してしまうバグになる
            # （例: scaleFrom=45"5弱", scaleTo=99"7以上" → 誤って5弱扱い）。
            if st == 99:
                effective = 70
            elif st != -1:
                effective = st
            else:
                effective = sf if sf != -1 else -1
            if effective > max_scale_val:
                max_scale_val = effective

            # kindCode=10 or 19 のエリアが1つでもあれば警報フラグ
            # kindCode=11（到達済）だけの場合は isWarn=False のまま
            if kind_code in ("10", "19"):
                has_warn = True

            shindo1 = scale_map.get(sf, "不明")
            shindo2_val = 70 if st == 99 else st
            shindo2 = scale_map.get(shindo2_val, shindo1)

            warn_areas.append({
                "Chiiki":      area.get("name", ""),
                "Pref":        area.get("pref", ""),
                "Shindo1":     shindo1,
                "Shindo2":     shindo2,
                "Type":        kind_type_map.get(kind_code, "警報"),
                "KindCode":    kind_code,
                # arrivalTime は null の場合があるので None → "" に正規化
                "ArrivalTime": area.get("arrivalTime") or "",
            })

        # MaxIntensity
        if any(safe_int(a.get("scaleTo", -1)) == 99 for a in areas):
            max_intensity = "7以上"
        else:
            max_intensity = scale_map.get(max_scale_val, "不明")

        raw_depth = hypo.get("depth", -1)
        depth = safe_int(raw_depth) if safe_int(raw_depth) != -1 else -1
        mag   = safe_float(hypo.get("magnitude", -1))

        return {
            "type":         "jma_eew",
            "Title":        "緊急地震速報（警報）",
            "EventID":      event_id,
            "Serial":       serial,
            "isCancel":     False,
            "isFinal":      False,
            "isTraining":   bool(p2p_data.get("test", False)),
            "isAssumption": is_plum,
            "isWarn":       has_warn,
            "OriginTime":   eq.get("originTime", ""),
            "ArrivalTime":  eq.get("arrivalTime", ""),
            "Hypocenter":   hypo.get("name", "不明") or "不明",
            "Latitude":     safe_float(hypo.get("latitude",  -200)),
            "Longitude":    safe_float(hypo.get("longitude", -200)),
            "Depth":        depth,
            "Magnitude":    mag if mag > 0 else -1,
            "Magunitude":   str(mag) if mag > 0 else "不明",
            "MaxIntensity": max_intensity,
            "WarnArea":     warn_areas,
            "_source":      "P2P地震情報",
        }

    except Exception:
        logger.error(f"P2P→Wolfx変換エラー:\n{traceback.format_exc()}")
        return None


def extract_alert_regions(data: dict, region_map: dict) -> set:
    """
    Wolfx形式のEEWデータから、警報(Type in ("警報","到達済"))対象の
    地域名（region_map変換後）の集合を抽出する。

    region_map は core.constants.REGION_MAP を呼び出し元から渡す
    （このモジュールをBotの状態から独立させるため、モジュール内で
    core.constants を直接インポートしない設計とする）。
    """
    alert_regions = set()
    for area in data.get("WarnArea", []):
        chiiki = area.get("Chiiki")
        if chiiki and area.get("Type", "").lower() in ("警報", "到達済"):
            alert_regions.add(region_map.get(chiiki, "その他"))
    return alert_regions
