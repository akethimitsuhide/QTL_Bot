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
from collections import defaultdict

from core.helpers import safe_int, safe_float

logger = logging.getLogger("QTLBot")

# 地域ごとの予想震度の上限が「以上」（上限なし。気象庁電文 To="over"、
# P2P地震情報 scaleTo=99）であることを表す Shindo2 の値。
# 【2026-09-22 追加】Wolfx側は電文由来の "over" 相当の値を Shindo2 に
# 入れてくる可能性があるため、両方の表記を「上限なし」として扱う。
OPEN_UPPER_LABEL = "以上"
OPEN_UPPER_LABELS = (OPEN_UPPER_LABEL, "over")


def _is_open_upper(shindo2: str) -> bool:
    return shindo2 in OPEN_UPPER_LABELS


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
                                        scaleTo のみ 99 あり。99 は「震度7」ではなく
                                        気象庁電文の To="over"＝「以上」（上限なし）を
                                        表す。例: scaleFrom=45, scaleTo=99 は
                                        「震度5弱以上」（2026-09-22 訂正。従来は
                                        99を震度7として扱っていた）
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

        # 【2026-08-31 修正】PLUM法判定
        #
        # 以前は「eq.condition == 仮定震源要素」（震源そのものが未確定＝
        # 全体がPLUM法によるもの）に加えて「いずれかのareaのkindCode
        # が19（そのエリアの震度推定にPLUM法を使用）」もOR条件に
        # 含めていたが、これは誤りだった。kindCode=19は「そのエリア
        # 個別の震度推定手法」を表すフラグであり、周辺の一部エリアで
        # 局所的にPLUM法が使われていても、震源・マグニチュード自体は
        # 既に確定している（実測に基づく）ケースが実際に存在する
        # （特に規模の大きい地震で、辺縁部のエリアだけがPLUM法による
        # 簡易推定になりやすい）。
        #
        # この誤ったOR条件により、実際には震源・マグニチュードが
        # 確定しているにもかかわらず、eq.conditionでは検出されない
        # ケースで、notify_eew側の isAssumption 分岐が発動し、
        # 「マグニチュード： M推定なし」「深さ： 推定なし」という
        # 誤表示になっていた（実機ログで確認: KUMAMOTO_7_2.json の
        # テストで発生）。
        #
        # 震源全体がPLUM法によるものかどうかは、地震オブジェクト
        # 直下の condition フィールドのみで判定する。個別エリアの
        # kindCode=19 は、そのエリアの震度表示・警報種別
        # （kind_type_map、下記）にのみ反映させれば十分であり、
        # 全体のマグニチュード/震源表示を隠す理由にはならない。
        is_plum = eq.get("condition") == "仮定震源要素"

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

        max_scale_val  = -1
        max_scale_open = False  # 最大震度が「以上」（上限なし）由来か
        warn_areas     = []
        has_warn      = False  # kindCode=10 or 19 が1つでもあれば isWarn=True

        for area in areas:
            kind_code = str(area.get("kindCode", ""))
            sf = safe_int(area.get("scaleFrom", -1))
            st = safe_int(area.get("scaleTo",   -1))

            # 最大震度追跡。
            #
            # 【2026-09-22 修正】scaleTo=99 は「震度7」ではなく、気象庁電文の
            # To="over"＝「以上」（上限なし）を表す（例: scaleFrom=45,
            # scaleTo=99 は「震度5弱以上」）。従来は99を震度7（70）として
            # 扱っていたため、PLUM法（各地を「5弱以上」等と推定する）の
            # 発表で、全体の予想最大震度が実態と無関係に「7以上」と
            # 表示され、地域内訳（5弱・5強等）とも食い違っていた。
            #
            # 【2026-09-24 訂正】上記修正は「以上」表記自体をPLUM法にも
            # 一律適用してしまっており、これが新たな不具合だった
            # （タローさんからの指摘: Wolfx側はPLUM法で「以上」を出さず
            # 「震度X程度」のみを表示しており、P2P側も同様に処理すべき）。
            # scaleTo=99を「以上」として扱う（is_open）のは非PLUM法の
            # エリアに限定する。PLUM法（is_plum）のエリアは、scaleTo=99を
            # st=-1（不明）と同様に無視し、常にscaleFrom（下限＝PLUM法の
            # 推定値そのもの）を採用する。これにより、後段の
            # build_forecast_groups等は「震度X程度」ラベルを持つ
            # is_assumption分岐（Wolfx側と共通のロジック）に流れる。
            if st == 99 and not is_plum:
                effective, is_open = sf, sf != -1
            elif st != -1 and st != 99:
                effective, is_open = st, False
            else:
                effective, is_open = sf, False
            if effective > max_scale_val:
                max_scale_val, max_scale_open = effective, is_open
            elif effective == max_scale_val and is_open:
                max_scale_open = True

            # kindCode=10 or 19 のエリアが1つでもあれば警報フラグ
            # kindCode=11（到達済）だけの場合は isWarn=False のまま
            if kind_code in ("10", "19"):
                has_warn = True

            shindo1 = scale_map.get(sf, "不明")
            if st == 99 and not is_plum:
                # 上限なし（「以上」）。build_forecast_groups 等が
                # 「震度5弱以上」と表示できるよう、専用の表記で渡す。
                # PLUM法（is_plum）の場合はこの分岐に入らず、下の
                # scale_map.get(st, shindo1) が st=99 に対応するキーを
                # 持たないため自然に shindo1 へフォールバックする
                # （＝Wolfx側のPLUM法表示と同じ「程度」表記になる）。
                shindo2 = OPEN_UPPER_LABEL
            else:
                shindo2 = scale_map.get(st, shindo1)

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

        # MaxIntensity（全エリアの予想震度の最大。「以上」由来なら「5弱以上」等）
        max_intensity = scale_map.get(max_scale_val, "不明")
        if max_scale_open and max_scale_val not in (-1, 70):
            max_intensity += OPEN_UPPER_LABEL

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


def eew_warn_advisory_note(max_shindo_code: int | None) -> str:
    """
    isWarn=True（緊急地震速報・警報）時の注意喚起文を、予想最大震度
    （int_mapのキー。例: 45=5弱, 55=6弱）に応じて選択する。

    - 6弱以上（55以上）        : 「直ちに身の安全を確保してください」
    - 5弱以上6弱未満（45〜54） : 「強い揺れに警戒してください」
      （46="推定5弱以上"を含む。PLUM法等で確定値ではないが、
       6弱以上と確定できない値のため、より安全側の文言にする）
    - それ以外（震度不明・5弱未満など）:
      予想最大震度から強さを判断できないケースであり、警報自体は
      発表されている（isWarn=True）ため、従来通り
      「強い揺れに警戒してください」を安全側のデフォルトとして返す。

    max_shindo_code が None（INT_MAPに対応する値が見つからない）場合も
    同様に安全側のデフォルトを返す。
    """
    if max_shindo_code is not None and max_shindo_code >= 55:
        return "**⚠ 直ちに身の安全を確保してください。**"
    return "**⚠ 強い揺れに警戒してください。**"


def shindo_rank(value: str, int_map: dict) -> int:
    """
    "1"〜"7"・"5弱"・"6強"等の震度文字列を、大小比較用の数値ランクに
    変換する（int_mapのキーがそのままランクとして使える）。

    int_map は core.constants.INT_MAP を呼び出し元から渡す
    （extract_alert_regions と同じ理由でモジュールを独立させるため）。
    見つからない場合は 0（最小扱い）を返す。
    """
    if value in int_map.values():
        for num, txt in int_map.items():
            if txt == value:
                return num
    return 0


def build_forecast_groups(warn_areas: list, int_map: dict, is_assumption: bool = False) -> dict:
    """
    WarnArea配列から「震度X程度」「震度X〜Y程度」ラベルごとに地域名を
    グルーピングした dict（ラベル → [地域名, ...]）を返す。

    【2026-08-27 追加】単一EEW通知（notify_eew）と複数EEWサマリー通知
    （「複数の緊急地震速報が発表されています」）の両方で全く同じ
    グルーピングロジックが必要になったため、重複を避けてここに共通化した。

    【2026-09-02 修正】以前は shindo1（scaleFrom＝下限値ベース）が
    「不明」のエリアを、shindo2（scaleTo＝上限値ベース）が判明していても
    丸ごと表示から除外していた。この結果、convert_p2p_eew_to_wolfx の
    MaxIntensity計算と、本関数が生成する地域ごとの震度内訳との間に
    食い違いが生じ、「全体の予想最大震度は7以上なのに、地域ごとの内訳
    には6弱・5強までしか出てこない」という実機での報告に一致する
    不具合があった。
    【2026-09-22 訂正】上記の食い違いの根本原因は、scaleTo=99 を
    「震度7」と誤解していたこと（実際は「以上」＝上限なし）だった。
    99は Shindo2="以上" として渡され、本関数は「震度5弱以上」の
    ように下限のみを表示する（convert_p2p_eew_to_wolfx 参照）。
    下限（scaleFrom）が不明なだけで、上限（scaleTo）は判明している
    エリアは実際に存在しうる（EEW初期の推定でscaleFromが未計算の
    ケース等）。shindo1・shindo2のうち少なくとも一方が判明していれば
    表示するよう修正し、下限が不明な場合は上限（shindo2）のみで
    ラベルを組み立てる。
    """
    forecast_groups: dict = defaultdict(list)
    for area in warn_areas:
        chiiki = area.get("Chiiki")
        if not chiiki:
            continue
        shindo1 = area.get("Shindo1", "不明")
        shindo2 = area.get("Shindo2", shindo1)

        if _is_open_upper(shindo2):
            # 【2026-09-22 追加、2026-09-24 訂正】上限なし（「震度5弱以上」）。
            # P2P地震情報の scaleTo=99（気象庁電文 To="over"）由来。
            # convert_p2p_eew_to_wolfx側でPLUM法（is_assumption）のエリアは
            # scaleTo=99でもこの表記（Shindo2="以上"）にならないよう
            # 処理しているため、この分岐に到達するのは非PLUM法のエリアのみ
            # （PLUM法は下の is_assumption 分岐で「震度X程度」表記になる。
            # Wolfx側のPLUM法表示と揃えるための仕様）。下限が不明なら
            # 表示できる情報が無いため除外する。
            if shindo1 != "不明":
                forecast_groups[f"震度{shindo1}以上"].append(chiiki)
            continue

        if is_assumption:
            # shindo1が不明でもshindo2（上限値）が判明していればそちらを使う
            display_val = shindo1 if shindo1 != "不明" else shindo2
            if display_val != "不明":
                forecast_groups[f"震度{display_val}程度"].append(chiiki)
            continue

        if shindo1 == "不明" and shindo2 == "不明":
            # 上限・下限とも不明な場合のみ、本当に表示できないため除外する
            continue

        if shindo1 == "不明":
            # 下限は不明だが上限（例: 7以上→"7"）は判明している
            label = f"震度{shindo2}程度"
        elif shindo2 == "不明":
            # 逆に上限のみ不明なケース（通常は起こりにくいが念のため対応）
            label = f"震度{shindo1}程度"
        elif shindo1 == shindo2:
            label = f"震度{shindo1}程度"
        else:
            r1, r2 = shindo_rank(shindo1, int_map), shindo_rank(shindo2, int_map)
            high, low = (shindo1, shindo2) if r1 >= r2 else (shindo2, shindo1)
            label = f"震度{high}〜{low}程度"
        forecast_groups[label].append(chiiki)
    return dict(forecast_groups)


def build_region_shindo_map(warn_areas: list, int_map: dict, is_assumption: bool = False) -> dict:
    """
    WarnArea配列から「地域名（Chiiki、REGION_MAP変換前の生の名称）→
    震度コード（int_mapのキー。10,20,...,70）」のフラットな辞書を作る。

    core/gis_render.py のGIS地図描画（震度分布の地域塗りつぶし）向けに、
    build_forecast_groups（ラベル→地域名リストのグルーピング）とは別に
    追加した（2026-09-13）。ロジック自体はbuild_forecast_groupsと同様、
    Shindo1（下限）・Shindo2（上限）のうち高い方を採用する
    （PLUM法＝is_assumption時はShindo1のみを使う点も同様）。
    Shindo1・Shindo2とも「不明」の地域は結果に含めない。
    """
    result: dict = {}
    for area in warn_areas:
        chiiki = area.get("Chiiki")
        if not chiiki:
            continue
        shindo1 = area.get("Shindo1", "不明")
        shindo2 = area.get("Shindo2", shindo1)

        if _is_open_upper(shindo2):
            # 上限なし（「以上」）: 下限を、その地域の代表震度として塗る
            # （build_forecast_groups と同じ考え方。2026-09-22追加）
            if shindo1 == "不明":
                continue
            code = shindo_rank(shindo1, int_map)
        elif is_assumption:
            display_val = shindo1 if shindo1 != "不明" else shindo2
            if display_val == "不明":
                continue
            code = shindo_rank(display_val, int_map)
        else:
            if shindo1 == "不明" and shindo2 == "不明":
                continue
            if shindo1 == "不明":
                code = shindo_rank(shindo2, int_map)
            elif shindo2 == "不明":
                code = shindo_rank(shindo1, int_map)
            else:
                code = max(shindo_rank(shindo1, int_map), shindo_rank(shindo2, int_map))

        if code:  # shindo_rank は未知の値に対して0を返すため、0は除外する
            result[chiiki] = code
    return result


def _label_rank(label: str, int_map: dict) -> float:
    """
    「震度5弱程度」「震度6弱〜5強程度」「震度5弱以上」等の予想震度ラベルを
    大小比較用の数値ランクに変換する（範囲の場合は高い方）。「以上」
    （上限なし）は同じ階級の「程度」より僅かに上（+0.5）として扱う。
    """
    is_open = label.endswith(OPEN_UPPER_LABEL)
    body = label[: -len(OPEN_UPPER_LABEL)] if is_open else label
    rank = max(shindo_rank(s.strip("震度程度〜"), int_map) for s in body.split("〜"))
    return rank + (0.5 if is_open else 0)


def sorted_forecast_labels(forecast_groups: dict, int_map: dict) -> list:
    """forecast_groups のラベルを、震度が高い順にソートして返す。"""
    return sorted(
        forecast_groups.keys(),
        key=lambda lbl: _label_rank(lbl, int_map),
        reverse=True,
    )


def format_forecast_section(forecast_groups: dict, int_map: dict) -> str:
    """
    forecast_groups を Embed description に連結できる
    「【地域ごとの予想震度】」セクションのテキストに整形する。
    forecast_groups が空の場合は空文字列を返す（呼び出し側で
    description に無条件で連結できるようにするため）。
    """
    if not forecast_groups:
        return ""
    lines = ["\n\n**【地域ごとの予想震度】**"]
    for label in sorted_forecast_labels(forecast_groups, int_map):
        areas = sorted(forecast_groups[label])
        area_text = "\n".join(f"　{a}" for a in areas)
        lines.append(f"\n■ {label}\n{area_text}")
    return "".join(lines)


def merge_forecast_groups(warn_areas_list: list, int_map: dict) -> dict:
    """
    複数のEEW（それぞれの WarnArea 配列 + isAssumption フラグ）を横断して、
    地域ごとの予想震度をマージする。

    【2026-08-27 追加】「複数の緊急地震速報が発表されています」サマリー
    通知で、既存の「強い揺れが予想される地域」（merged_warn_regions、
    地方単位の粗い集合）と同様に、より詳細な「地域ごとの予想震度」
    （市区町村・地域単位）も複数EEWを横断して1つにまとめて表示するために
    追加した。

    同じ地域が複数のEEWで異なる予想震度になっている場合は、より大きい
    方の震度を採用する（同一地域名が複数の震度見出しに重複して
    表示されるのを防ぐため）。

    warn_areas_list: [(warn_areas, is_assumption), ...] のリスト
        （呼び出し側で sorted_eews の各要素から組み立てて渡す）
    """
    best_by_chiiki: dict = {}  # chiiki -> (rank, label)
    for warn_areas, is_assumption in warn_areas_list:
        groups = build_forecast_groups(warn_areas, int_map, is_assumption=is_assumption)
        for label, chiikis in groups.items():
            rank = _label_rank(label, int_map)
            for chiiki in chiikis:
                prev = best_by_chiiki.get(chiiki)
                if prev is None or rank > prev[0]:
                    best_by_chiiki[chiiki] = (rank, label)

    merged: dict = defaultdict(list)
    for chiiki, (_, label) in best_by_chiiki.items():
        merged[label].append(chiiki)
    return dict(merged)