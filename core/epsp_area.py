"""
core/epsp_area.py
===================
P2P地震情報（EPSP: Earthquake and tsunami Phenomena Sharing Platform）の
地域コード（epsp-area.csv）を扱うモジュール。

【用途】
地震感知情報（code=9611）の area_confidences のキーは地域コードの
文字列（例: "010"）で表現される。このモジュールは、その地域コードを
人間が読める地域名（例: "北海道 石狩"）に変換するためのマップを提供する。

【データソース】
epsp_area.csv は以下のリポジトリで配布されているCSVをそのまま
リポジトリ直下に同梱したもの（静的ファイル、起動時の外部フェッチは
行わない。region_map.json 等と同じ「同梱・手動更新」方式を踏襲）。
https://raw.githubusercontent.com/p2pquake/epsp-specifications/refs/heads/master/epsp-area.csv

CSVの列構成:
  地域コード(文字列型), 地域コード(数値型), 地方, 都道府県, 地域, 緯度, 経度
"""
import csv
import logging
import os

logger = logging.getLogger("QTLBot")


def load_epsp_area_map() -> dict[str, str]:
    """
    epsp_area.csv を読み込み、{地域コード(文字列): 地域名} の辞書を返す。

    地域コードは "010" のような3桁ゼロ埋め文字列として提供されるため、
    キーはCSVの「地域コード(文字列型)」列の値をそのまま使う
    （P2P WebSocketから受信するJSONのキーと同じ表記のはずだが、
    念のため数値変換後の3桁ゼロ埋め文字列でも引けるよう、
    get_area_name() 側でフォールバックを行う）。

    ファイルが存在しない場合は空の辞書を返す（呼び出し元は
    地域コードをそのまま表示するフォールバックを行うこと）。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # core/ の1階層上（プロジェクトルート）を見る
    path = os.path.join(base_dir, "..", "epsp_area.csv")

    if not os.path.exists(path):
        logger.warning("epsp_area.csv が存在しません（地震感知情報の地域名表示が地域コードのままになります）")
        return {}

    result: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code_str = (row.get("地域コード(文字列型)") or "").strip()
                area_name = (row.get("地域") or "").strip()
                if code_str and area_name:
                    result[code_str] = area_name
    except Exception as e:
        logger.error(f"epsp_area.csv 読み込みエラー: {e}")
        return {}

    return result


EPSP_AREA_MAP = load_epsp_area_map()

# 数値化した地域コード（先頭ゼロ除去）でも引けるようにした補助マップ。
# 例: "010" -> "10" でも引けるようにする（データソース側の表記揺れへの
# 保険。実際にはP2Pのメッセージは3桁ゼロ埋め文字列で来ることが
# ほとんどだが、念のため両対応にしておく）。
_EPSP_AREA_MAP_NUMERIC: dict[str, str] = {}
for _code, _name in EPSP_AREA_MAP.items():
    try:
        _EPSP_AREA_MAP_NUMERIC[str(int(_code))] = _name
    except ValueError:
        pass


def get_area_name(area_code: str) -> str:
    """
    地域コードから地域名を返す。マップに存在しない場合は、
    地域コードそのもの（"エリア{code}" の形式）を返す
    （データの欠落があっても通知自体は失敗させないため）。
    """
    if area_code in EPSP_AREA_MAP:
        return EPSP_AREA_MAP[area_code]
    try:
        numeric_key = str(int(area_code))
    except (ValueError, TypeError):
        numeric_key = None
    if numeric_key and numeric_key in _EPSP_AREA_MAP_NUMERIC:
        return _EPSP_AREA_MAP_NUMERIC[numeric_key]
    return f"エリア{area_code}"
