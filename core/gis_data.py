"""
core/gis_data.py
=================
GIS地図描画機能（試験導入、core/gis_render.py参照）で使用する外部データの
ダウンロード・キャッシュ管理を担当するモジュール。

【データソース】
- AreaForecastLocalEEW_GIS（府県予報区、56件。緊急地震速報（警報）の
  発表地域の描画用。properties.name は REGION_MAP 変換後の名称
  「北海道道央」等と一致）
    https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/
    refs/heads/release/AreaForecastLocalEEW_GIS_20190125_01.geojson
- AreaForecastLocalE_GIS（細分区域、194件。緊急地震速報（予報）・
  全地震情報の震度分布描画用。properties.name は REGION_MAP 変換前の
  生の区域名「石狩地方南部」等と一致。実データ確認済み：2件は
  geometry が null＝境界データ未整備で、該当区域の塗りつぶしは
  スキップする）
    https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/
    refs/heads/release/AreaForecastLocalE_GIS_20190125_01.geojson
- AreaTsunami_GIS（津波予報区、70件。津波情報の沿岸線色分け描画用。
  2026-09-14追加。geometryはPolygonではなくLineString/MultiLineString
  ＝海岸線に沿った線データ。properties.name は津波情報APIの
  areas[].name と一致。4件はgeometryがnull＝「帰属未定」のプレース
  ホルダーのため実データで参照されることはない）
    https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/
    refs/heads/release/AreaTsunami_GIS_20240520_01.geojson
- stations.json（気象庁 震度観測点一覧。観測点ごとの震度表示用の
  緯度経度取得に使用）
    https://www.jma.go.jp/jma/kishou/know/jishin/intens-st/stations.json

  実データ構造（2026-09-13確認）: 配列で、各要素は以下の形式
  （lat/lonは文字列。prefは都道府県コード、affiは観測点の所属種別と
  思われるが本モジュールでは使用しない）:
    {"lat": "43.17", "lon": "141.32", "name": "石狩市花川",
     "pref": "1", "affi": "0"}
  "name" が地震情報APIの points[].addr（観測点名）と一致する前提で
  検索キーに使う。

【ライセンス上の注意】
GeoJSON 2種は CC-BY-4.0 で配布されているサードパーティ変換データの
ため、リポジトリへの誤コミットを避ける目的で、キャッシュ先ディレクトリ
（core.config.GIS_MAP_DATA_DIR）は .gitignore に登録している。

【取得方針】
GIS_MAP_ENABLE=true で GIS地図描画機能を使うCogがロードされた際、
キャッシュファイルが存在しなければダウンロードする（起動のたびに
毎回取得はしない＝JMA/GitHub側への不要な負荷を避ける）。
stations.json は気象庁側で観測点構成が変わることがあるため、他の
GeoJSON同様の「キャッシュがなければ取得」方式に加えて、明示的に
再取得したい場合のために `python3 bot.py --refresh_gis_data` で
4ファイルすべてを強制的に再ダウンロードできるようにする
（core/gis_data_refresh.py 参照）。
"""
import os
import json
import logging

import aiohttp

from core.config import GIS_MAP_DATA_DIR

logger = logging.getLogger("QTLBot")

EEW_AREA_GEOJSON_URL = (
    "https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/"
    "refs/heads/release/AreaForecastLocalEEW_GIS_20190125_01.geojson"
)
LOCAL_AREA_GEOJSON_URL = (
    "https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/"
    "refs/heads/release/AreaForecastLocalE_GIS_20190125_01.geojson"
)
TSUNAMI_AREA_GEOJSON_URL = (
    "https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/"
    "refs/heads/release/AreaTsunami_GIS_20240520_01.geojson"
)
STATIONS_JSON_URL = "https://www.jma.go.jp/jma/kishou/know/jishin/intens-st/stations.json"

# キー → (取得元URL, キャッシュ先ファイル名)
_FILES: dict[str, tuple[str, str]] = {
    "eew_areas":     (EEW_AREA_GEOJSON_URL,     "eew_areas.geojson"),
    "local_areas":   (LOCAL_AREA_GEOJSON_URL,   "local_areas.geojson"),
    "tsunami_areas": (TSUNAMI_AREA_GEOJSON_URL, "tsunami_areas.geojson"),
    "stations":      (STATIONS_JSON_URL,        "stations.json"),
}


def _path_for(key: str) -> str:
    _, filename = _FILES[key]
    return os.path.join(GIS_MAP_DATA_DIR, filename)


async def ensure_gis_data(session: aiohttp.ClientSession, force: bool = False) -> bool:
    """
    GIS地図描画に必要な4種の外部データがキャッシュ済みであることを
    保証する。キャッシュディレクトリが無ければ作成する。

    Parameters
    ----------
    session : 呼び出し元（Cog）が保持する共有 aiohttp.ClientSession を
        渡す（このモジュール単体では新規セッションを作らない。
        cog_load/cog_unload によるセッションのライフサイクル管理に
        乗る設計方針は core/kyoshin_stations.py 等と同じ）。
    force : True の場合、既存キャッシュがあっても強制的に再ダウンロード
        する（`python3 bot.py --refresh_gis_data` 用）。

    戻り値
    ------
    bool : 4ファイルすべての準備に成功した場合 True。
        1つでも失敗した場合 False（呼び出し元は GIS地図描画機能を
        今回の起動では無効化する等、フォールバック動作を行うこと。
        次回のダウンロード再試行は次回のCogロード時に行われる）。
    """
    os.makedirs(GIS_MAP_DATA_DIR, exist_ok=True)
    all_ok = True
    for key, (url, _filename) in _FILES.items():
        path = _path_for(key)
        if not force and os.path.exists(path) and os.path.getsize(path) > 0:
            logger.debug(f"GISデータ: キャッシュ済みのためスキップ ({key})")
            continue
        ok = await _download(session, url, path)
        all_ok = all_ok and ok
    return all_ok


async def _download(session: aiohttp.ClientSession, url: str, path: str) -> bool:
    """
    1ファイルをダウンロードし、一時ファイル経由でアトミックに配置する
    （ダウンロード中にプロセスが落ちても、壊れかけの不完全なファイルが
    正式なキャッシュファイル名で残ってしまうことを防ぐため）。
    """
    tmp_path = path + ".tmp"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                logger.error(f"GISデータ取得失敗: {url} → HTTP {resp.status}")
                return False
            body = await resp.read()

        if not body:
            logger.error(f"GISデータ取得失敗: {url} → 空レスポンス")
            return False

        with open(tmp_path, "wb") as f:
            f.write(body)
        os.replace(tmp_path, path)
        logger.info(f"GISデータ取得完了: {os.path.basename(path)} ({len(body):,} bytes)")
        return True

    except Exception as e:
        logger.error(f"GISデータ取得エラー: {url}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def gis_data_ready() -> bool:
    """4種のキャッシュファイルが全て揃っているか（サイズ0のものは未整備扱い）を返す。"""
    return all(
        os.path.exists(_path_for(key)) and os.path.getsize(_path_for(key)) > 0
        for key in _FILES
    )


def load_eew_areas() -> list[dict]:
    """AreaForecastLocalEEW_GIS（府県予報区、56件）のfeatures配列を返す。読み込み失敗時は空リスト。"""
    return _load_geojson_features(_path_for("eew_areas"))


def load_local_areas() -> list[dict]:
    """AreaForecastLocalE_GIS（細分区域、194件）のfeatures配列を返す。読み込み失敗時は空リスト。"""
    return _load_geojson_features(_path_for("local_areas"))


def load_tsunami_areas() -> list[dict]:
    """
    AreaTsunami_GIS（津波予報区、70件）のfeatures配列を返す。読み込み
    失敗時は空リスト。geometryはPolygonではなくLineString/MultiLineString
    （海岸線に沿った線）である点に注意（気象庁の実際の津波予報区表示に
    合わせ、塗りつぶしではなく色付き沿岸線として描画する想定）。
    """
    return _load_geojson_features(_path_for("tsunami_areas"))


def _load_geojson_features(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("features", [])
    except Exception as e:
        logger.error(f"GIS GeoJSON読み込みエラー ({path}): {e}")
        return []


def load_stations() -> dict[str, tuple[float, float]]:
    """
    stations.json を読み込み、観測点名 → (lat, lon) の辞書を返す。
    読み込み失敗時・パース失敗時は空辞書を返す（呼び出し元は観測点
    マーカーの描画をスキップすればよく、致命的エラーにはしない）。

    同名の観測点が複数存在するケースは実データ上未確認だが、万一
    存在した場合は後方のエントリで上書きされる（後勝ち）。
    """
    path = _path_for("stations")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error(f"stations.json読み込みエラー ({path}): {e}")
        return {}

    if not isinstance(raw, list):
        logger.error(f"stations.json形式エラー: 配列を期待していましたが {type(raw).__name__} でした")
        return {}

    result: dict[str, tuple[float, float]] = {}
    skipped = 0
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        if not name:
            skipped += 1
            continue
        try:
            lat = float(item.get("lat"))
            lon = float(item.get("lon"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        result[name] = (lat, lon)

    if skipped:
        logger.warning(f"stations.json: {skipped}件のエントリを解析できずスキップしました")
    return result"""
core/gis_data.py
=================
GIS地図描画機能（試験導入、core/gis_render.py参照）で使用する外部データの
ダウンロード・キャッシュ管理を担当するモジュール。

【データソース】
- AreaForecastLocalEEW_GIS（府県予報区、56件。緊急地震速報（警報）の
  発表地域の描画用。properties.name は REGION_MAP 変換後の名称
  「北海道道央」等と一致）
    https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/
    refs/heads/release/AreaForecastLocalEEW_GIS_20190125_01.geojson
- AreaForecastLocalE_GIS（細分区域、194件。緊急地震速報（予報）・
  全地震情報の震度分布描画用。properties.name は REGION_MAP 変換前の
  生の区域名「石狩地方南部」等と一致。実データ確認済み：2件は
  geometry が null＝境界データ未整備で、該当区域の塗りつぶしは
  スキップする）
    https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/
    refs/heads/release/AreaForecastLocalE_GIS_20190125_01.geojson
- stations.json（気象庁 震度観測点一覧。観測点ごとの震度表示用の
  緯度経度取得に使用）
    https://www.jma.go.jp/jma/kishou/know/jishin/intens-st/stations.json

  実データ構造（2026-09-13確認）: 配列で、各要素は以下の形式
  （lat/lonは文字列。prefは都道府県コード、affiは観測点の所属種別と
  思われるが本モジュールでは使用しない）:
    {"lat": "43.17", "lon": "141.32", "name": "石狩市花川",
     "pref": "1", "affi": "0"}
  "name" が地震情報APIの points[].addr（観測点名）と一致する前提で
  検索キーに使う。

【ライセンス上の注意】
GeoJSON 2種は CC-BY-4.0 で配布されているサードパーティ変換データの
ため、リポジトリへの誤コミットを避ける目的で、キャッシュ先ディレクトリ
（core.config.GIS_MAP_DATA_DIR）は .gitignore に登録している。

【取得方針】
GIS_MAP_ENABLE=true で GIS地図描画機能を使うCogがロードされた際、
キャッシュファイルが存在しなければダウンロードする（起動のたびに
毎回取得はしない＝JMA/GitHub側への不要な負荷を避ける）。
stations.json は気象庁側で観測点構成が変わることがあるため、他の
GeoJSON同様の「キャッシュがなければ取得」方式に加えて、明示的に
再取得したい場合のために `python3 bot.py --refresh_gis_data` で
3ファイルすべてを強制的に再ダウンロードできるようにする
（core/gis_data_refresh.py 参照）。
"""
import os
import json
import logging

import aiohttp

from core.config import GIS_MAP_DATA_DIR

logger = logging.getLogger("QTLBot")

EEW_AREA_GEOJSON_URL = (
    "https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/"
    "refs/heads/release/AreaForecastLocalEEW_GIS_20190125_01.geojson"
)
LOCAL_AREA_GEOJSON_URL = (
    "https://raw.githubusercontent.com/Ichihai1415/JMA-GIS-GeoJSON/"
    "refs/heads/release/AreaForecastLocalE_GIS_20190125_01.geojson"
)
STATIONS_JSON_URL = "https://www.jma.go.jp/jma/kishou/know/jishin/intens-st/stations.json"

# キー → (取得元URL, キャッシュ先ファイル名)
_FILES: dict[str, tuple[str, str]] = {
    "eew_areas":   (EEW_AREA_GEOJSON_URL,   "eew_areas.geojson"),
    "local_areas": (LOCAL_AREA_GEOJSON_URL, "local_areas.geojson"),
    "stations":    (STATIONS_JSON_URL,      "stations.json"),
}


def _path_for(key: str) -> str:
    _, filename = _FILES[key]
    return os.path.join(GIS_MAP_DATA_DIR, filename)


async def ensure_gis_data(session: aiohttp.ClientSession, force: bool = False) -> bool:
    """
    GIS地図描画に必要な3種の外部データがキャッシュ済みであることを
    保証する。キャッシュディレクトリが無ければ作成する。

    Parameters
    ----------
    session : 呼び出し元（Cog）が保持する共有 aiohttp.ClientSession を
        渡す（このモジュール単体では新規セッションを作らない。
        cog_load/cog_unload によるセッションのライフサイクル管理に
        乗る設計方針は core/kyoshin_stations.py 等と同じ）。
    force : True の場合、既存キャッシュがあっても強制的に再ダウンロード
        する（`python3 bot.py --refresh_gis_data` 用）。

    戻り値
    ------
    bool : 3ファイルすべての準備に成功した場合 True。
        1つでも失敗した場合 False（呼び出し元は GIS地図描画機能を
        今回の起動では無効化する等、フォールバック動作を行うこと。
        次回のダウンロード再試行は次回のCogロード時に行われる）。
    """
    os.makedirs(GIS_MAP_DATA_DIR, exist_ok=True)
    all_ok = True
    for key, (url, _filename) in _FILES.items():
        path = _path_for(key)
        if not force and os.path.exists(path) and os.path.getsize(path) > 0:
            logger.debug(f"GISデータ: キャッシュ済みのためスキップ ({key})")
            continue
        ok = await _download(session, url, path)
        all_ok = all_ok and ok
    return all_ok


async def _download(session: aiohttp.ClientSession, url: str, path: str) -> bool:
    """
    1ファイルをダウンロードし、一時ファイル経由でアトミックに配置する
    （ダウンロード中にプロセスが落ちても、壊れかけの不完全なファイルが
    正式なキャッシュファイル名で残ってしまうことを防ぐため）。
    """
    tmp_path = path + ".tmp"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                logger.error(f"GISデータ取得失敗: {url} → HTTP {resp.status}")
                return False
            body = await resp.read()

        if not body:
            logger.error(f"GISデータ取得失敗: {url} → 空レスポンス")
            return False

        with open(tmp_path, "wb") as f:
            f.write(body)
        os.replace(tmp_path, path)
        logger.info(f"GISデータ取得完了: {os.path.basename(path)} ({len(body):,} bytes)")
        return True

    except Exception as e:
        logger.error(f"GISデータ取得エラー: {url}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def gis_data_ready() -> bool:
    """3種のキャッシュファイルが全て揃っているか（サイズ0のものは未整備扱い）を返す。"""
    return all(
        os.path.exists(_path_for(key)) and os.path.getsize(_path_for(key)) > 0
        for key in _FILES
    )


def load_eew_areas() -> list[dict]:
    """AreaForecastLocalEEW_GIS（府県予報区、56件）のfeatures配列を返す。読み込み失敗時は空リスト。"""
    return _load_geojson_features(_path_for("eew_areas"))


def load_local_areas() -> list[dict]:
    """AreaForecastLocalE_GIS（細分区域、194件）のfeatures配列を返す。読み込み失敗時は空リスト。"""
    return _load_geojson_features(_path_for("local_areas"))


def _load_geojson_features(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("features", [])
    except Exception as e:
        logger.error(f"GIS GeoJSON読み込みエラー ({path}): {e}")
        return []


def load_stations() -> dict[str, tuple[float, float]]:
    """
    stations.json を読み込み、観測点名 → (lat, lon) の辞書を返す。
    読み込み失敗時・パース失敗時は空辞書を返す（呼び出し元は観測点
    マーカーの描画をスキップすればよく、致命的エラーにはしない）。

    同名の観測点が複数存在するケースは実データ上未確認だが、万一
    存在した場合は後方のエントリで上書きされる（後勝ち）。
    """
    path = _path_for("stations")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error(f"stations.json読み込みエラー ({path}): {e}")
        return {}

    if not isinstance(raw, list):
        logger.error(f"stations.json形式エラー: 配列を期待していましたが {type(raw).__name__} でした")
        return {}

    result: dict[str, tuple[float, float]] = {}
    skipped = 0
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        if not name:
            skipped += 1
            continue
        try:
            lat = float(item.get("lat"))
            lon = float(item.get("lon"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        result[name] = (lat, lon)

    if skipped:
        logger.warning(f"stations.json: {skipped}件のエントリを解析できずスキップしました")
    return result
