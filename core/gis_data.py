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
5ファイルすべてを強制的に再ダウンロードできるようにする
（core/gis_data_refresh.py 参照）。
"""
import asyncio
import math
import os
import json
import logging
import threading
from typing import Optional

import aiohttp

from core.config import GIS_MAP_DATA_DIR, GIS_MAP_ENABLE

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
COUNTRIES_GEOJSON_URL = (
    "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"
)
STATIONS_JSON_URL = "https://www.jma.go.jp/jma/kishou/know/jishin/intens-st/stations.json"

# キー → (取得元URL, キャッシュ先ファイル名)
_FILES: dict[str, tuple[str, str]] = {
    "eew_areas":     (EEW_AREA_GEOJSON_URL,     "eew_areas.geojson"),
    "local_areas":   (LOCAL_AREA_GEOJSON_URL,   "local_areas.geojson"),
    "tsunami_areas": (TSUNAMI_AREA_GEOJSON_URL, "tsunami_areas.geojson"),
    "countries":     (COUNTRIES_GEOJSON_URL,    "countries.geojson"),
    "stations":      (STATIONS_JSON_URL,        "stations.json"),
}


def _path_for(key: str) -> str:
    _, filename = _FILES[key]
    return os.path.join(GIS_MAP_DATA_DIR, filename)


async def ensure_gis_data(session: aiohttp.ClientSession, force: bool = False) -> bool:
    """
    GIS地図描画に必要な5種の外部データがキャッシュ済みであることを
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
    bool : 5ファイルすべての準備に成功した場合 True。
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
    if force:
        # 簡略化国境データ（下記 build_simplified_countries_geojson 参照）は
        # countries.geojson から派生生成するキャッシュのため、
        # --refresh_gis_data で元データを再取得した場合は古い派生
        # キャッシュを無効化し、次回アクセス時に作り直させる
        # （2026-09-26追加。地理院タイル廃止に伴う変更）。
        _invalidate_simplified_countries_cache()
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
    """5種のキャッシュファイルが全て揃っているか（サイズ0のものは未整備扱い）を返す。"""
    return all(
        os.path.exists(_path_for(key)) and os.path.getsize(_path_for(key)) > 0
        for key in _FILES
    )


async def ensure_gis_data_ready(session: aiohttp.ClientSession, cog_label: str) -> None:
    """
    GIS_MAP_ENABLE=true のCogの cog_load() から呼び出す想定の薄いラッパー。
    ensure_gis_data() を呼び、結果をログに残すだけの処理（成否をここで
    呼び出し元に伝播させる必要はない。GIS_MAP_ENABLE=trueでも外部データが
    揃わなければ core.gis_render の各render関数がNoneを返し、GIS地図の
    添付が省略されるだけで、通知本体には影響しない設計のため）。

    EewCog / QuakeInfoCog / TsunamiCog の3つがそれぞれ自分の
    cog_load() から呼ぶ（他Cogの起動順に依存しない設計方針
    ＝各Cogが独立して自分のaiohttpセッションを持つのと同じ考え方）。
    ensure_gis_data() 自体はファイルが既に存在すればスキップするだけ
    なので、複数Cogから呼んでも二重ダウンロードにはならない。

    【2026-09-15 追加】GIS_MAP_ENABLE=true にしてもどのCogからも
    ensure_gis_data() を呼び出す配線が無く、GISデータが一度も
    自動ダウンロードされない（data/gis/ ディレクトリ自体が作成
    されない）という不具合があったため追加した。
    """
    if not GIS_MAP_ENABLE:
        return
    try:
        ok = await ensure_gis_data(session)
        if ok:
            logger.info(f"{cog_label}: GISデータ（地図描画用）の準備が完了しました")
            # 簡略化国境データ（core.gis_render.render_overseas_map・
            # Webダッシュボードの /status/gis_countries が共用する軽量版）
            # を、初回のみバックグラウンドスレッドで生成しておく
            # （2026-09-26追加。地理院タイル廃止に伴う変更）。CPUバウンドな
            # 処理（Raspberry Pi 5実機で数秒程度）のためrun_in_executorで
            # オフロードし、イベントループ（Discord通信等）をブロックしない。
            # ディスクキャッシュ済みなら即座に返るため、EewCog/QuakeInfoCog/
            # TsunamiCogの3つから呼ばれても実際に生成処理が走るのは最初の
            # 1回だけ。ここで生成しておくことで、海外地震の通知時（同期的に
            # 呼ばれるrender_overseas_map内）で初回生成の重い処理が走って
            # イベントループが止まる事態を避ける。
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, build_simplified_countries_geojson)
            except Exception:
                logger.warning(f"{cog_label}: 簡略化国境データの事前生成に失敗しました（初回アクセス時に再試行されます）", exc_info=True)
        else:
            logger.warning(
                f"{cog_label}: GISデータの取得に一部失敗しました。"
                f"GIS地図の添付が一部/全て省略される可能性があります。"
                f"次回のCogロード時（Bot再起動時）に再試行されます。"
            )
    except Exception:
        logger.error(f"{cog_label}: GISデータ準備中にエラーが発生しました", exc_info=True)


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


def load_countries() -> list[dict]:
    """
    世界の国境データ（datasets/geo-countries、258カ国）の元データ
    （約14MB、間引き前）のfeatures配列を返す。読み込み失敗時は空リスト。

    このデータをそのまま描画・配信に使うことはなく、
    build_simplified_countries_geojson() が間引き・軽量化した派生
    データ（load_countries_simplified()）を経由して、遠地地震（震源が
    日本国外）向けの地図描画（core.gis_render.render_overseas_map）・
    Webダッシュボード（/status/gis_countries）から使われる
    （2026-09-26変更。地理院タイル廃止に伴い、国土地理院タイルの
    補完用だった旧core/gis_tile_render.pyの役割を置き換えた）。
    """
    return _load_geojson_features(_path_for("countries"))


# ===============================
# 簡略化国境データ（2026-09-26追加。地理院タイル廃止に伴う変更）
# ===============================
# 従来、遠地地震（震源が日本国外）向けの地図は国土地理院タイルとの
# 重ね合わせ（旧core/gis_tile_render.py）で描画していたが、地理院タイル
# サーバー側の負荷・大規模地震時のアクセス集中への配慮から、地理院タイル
# の使用を廃止した。代わりに world countries.geojson（258カ国、約14MB・
# 頂点数約55万）を陸地色で塗りつぶす独自ベクター地図に統一したが、
# 元データをそのまま使うと ①Webダッシュボード（/status/gis_countries）
# が毎回14MBをブラウザへ配信することになる ②海外地震の通知のたびに
# core.gis_render.render_overseas_map が55万頂点をPillowで描画すること
# になり、いずれもRaspberry Pi上での運用に適さない。
#
# そこで、Ramer-Douglas-Peuckerアルゴリズム（折れ線の間引き。追加の
# 依存ライブラリなしで実装できる単純なアルゴリズムのため、本プロジェクト
# の「軽量・低依存」方針に沿って自前実装した）で頂点数を大幅に削減した
# 軽量版を1度だけ生成し、core.gis_render.render_overseas_map・
# Webダッシュボードの/status/gis_countriesの両方で共用する
# （生成後はディスクキャッシュを読むだけなので高速）。
_SIMPLIFIED_COUNTRIES_FILENAME = "countries_simplified.geojson"
# 間引きの許容誤差（度単位）。大きいほど頂点数が減り軽量になるが、
# 海岸線の形状が粗くなる。背景地図として国土の概形が分かれば十分な
# 用途のため、詳細さより軽量さを優先した値にしている
# （0.08度 ≈ 赤道上で約9km。258カ国・約55万頂点→約240件・約4万頂点、
# JSONサイズは約14MB→約0.6〜0.7MBに削減できることを確認済み）。
_COUNTRY_SIMPLIFY_EPSILON_DEG = 0.08
_COUNTRY_COORD_DECIMALS = 3  # 簡略化後の座標の丸め桁数（さらなる軽量化のため）

_simplified_countries_cache: Optional[list] = None
_simplified_countries_lock = threading.Lock()


def _simplify_ring_rdp(points: list, epsilon: float) -> list:
    """
    Ramer-Douglas-Peuckerアルゴリズムによる折れ線の間引き（純粋Python・
    再帰実装）。points は [(lon, lat), ...] の頂点列（リングの場合は
    閉路だが、始点・終点を固定端点として扱う単純な実装で、閉路である
    ことの特別扱いはしていない）。穴のあるポリゴン・自己交差の検証は
    行わない（本モジュール・core.gis_renderの他のジオメトリ処理と同様、
    外側リングの単純な折れ線として扱う割り切った実装）。
    """
    if len(points) < 3:
        return points

    def _perp_dist(pt, a, b):
        (x, y), (ax, ay), (bx, by) = pt, a, b
        if (ax, ay) == (bx, by):
            return math.hypot(x - ax, y - ay)
        dx, dy = bx - ax, by - ay
        t = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        return math.hypot(x - px, y - py)

    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _perp_dist(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i

    if dmax > epsilon:
        left = _simplify_ring_rdp(points[:idx + 1], epsilon)
        right = _simplify_ring_rdp(points[idx:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def _invalidate_simplified_countries_cache() -> None:
    """メモリ・ディスク双方の簡略化国境データキャッシュを破棄する。"""
    global _simplified_countries_cache
    _simplified_countries_cache = None
    path = os.path.join(GIS_MAP_DATA_DIR, _SIMPLIFIED_COUNTRIES_FILENAME)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"簡略化国境データキャッシュの削除に失敗しました: {e}")


def build_simplified_countries_geojson(force: bool = False) -> list[dict]:
    """
    countries.geojson を間引き・軽量化したfeatures配列を返す
    （2026-09-26追加。詳細は上の「簡略化国境データ」節参照）。

    【重要】CPUバウンドな処理（初回生成時、全頂点の間引きに
    Raspberry Pi 5実機で数秒程度かかる）のため、呼び出し元は必ず
    run_in_executor 等でワーカースレッドへオフロードすること
    （core.gis_data.ensure_gis_data_ready がCog起動時に1度だけ
    事前生成する。呼び出し元がオフロードを怠ってもBot自体が
    落ちることはないが、その間イベントループ〈Discord通信等〉が
    止まってしまう）。

    生成結果は GIS_MAP_DATA_DIR 配下に永続キャッシュし、2回目以降は
    ディスクキャッシュを読むだけで済む（プロセス内メモリキャッシュも
    別途保持するため、同一プロセス内では実質1回のみディスクI/Oが
    発生する）。force=True の場合はキャッシュを無視して再生成する。

    日本自体は結果から除外する（core.gis_render 側の自前GeoJSON
    〈AreaForecastLocalE_GIS〉の方が精密なため、そちらを別途重ね描き
    する想定。旧core/gis_tile_render.py の_get_countries()が行って
    いたのと同じ考え方）。
    """
    global _simplified_countries_cache
    if not force and _simplified_countries_cache is not None:
        return _simplified_countries_cache

    with _simplified_countries_lock:
        if not force and _simplified_countries_cache is not None:
            return _simplified_countries_cache

        cache_path = os.path.join(GIS_MAP_DATA_DIR, _SIMPLIFIED_COUNTRIES_FILENAME)
        if not force and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    features = json.load(f)
                _simplified_countries_cache = features
                return features
            except Exception as e:
                logger.warning(f"簡略化国境データのキャッシュ読み込みに失敗しました。再生成します: {e}")

        features: list[dict] = []
        skipped_features = 0
        for feature in load_countries():
            try:
                props = feature.get("properties", {}) or {}
                if props.get("name") == "Japan":
                    continue
                geometry = feature.get("geometry")
                if not geometry:
                    continue
                gtype = geometry.get("type")
                coords = geometry.get("coordinates")
                if gtype == "Polygon":
                    polygons = [coords]
                elif gtype == "MultiPolygon":
                    polygons = coords
                else:
                    continue

                new_polys = []
                for poly in polygons:
                    if not poly:
                        continue
                    # 座標は通常[lon, lat]の2要素だが、将来的にデータ元
                    # （datasets/geo-countries）の形式が変わり高度等の
                    # 3要素目が付与された場合でも壊れないよう、先頭2要素
                    # のみを使う（pt[1:]＝穴は他の処理と同様に無視）
                    ring = [(pt[0], pt[1]) for pt in poly[0]]
                    simplified = _simplify_ring_rdp(ring, _COUNTRY_SIMPLIFY_EPSILON_DEG)
                    if len(simplified) < 3:
                        continue
                    rounded_ring = [
                        [round(lon, _COUNTRY_COORD_DECIMALS), round(lat, _COUNTRY_COORD_DECIMALS)]
                        for lon, lat in simplified
                    ]
                    new_polys.append([rounded_ring])  # Polygon座標形式: [外側リング]（穴なし）

                if not new_polys:
                    continue
                new_geometry = (
                    {"type": "Polygon", "coordinates": new_polys[0]}
                    if len(new_polys) == 1
                    else {"type": "MultiPolygon", "coordinates": new_polys}
                )
                features.append({"type": "Feature", "properties": {}, "geometry": new_geometry})
            except Exception:
                # 1件のフィーチャの形式異常でデータ全体の生成を止めないよう、
                # そのフィーチャだけスキップする（他の安全側フォールバックと
                # 同じ考え方）。
                skipped_features += 1
                logger.debug("簡略化国境データ: 1件のフィーチャをスキップしました", exc_info=True)

        if skipped_features:
            logger.warning(f"簡略化国境データ: 形式異常のため{skipped_features}件のフィーチャをスキップしました")

        try:
            os.makedirs(GIS_MAP_DATA_DIR, exist_ok=True)
            tmp_path = cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(features, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, cache_path)
            logger.info(
                f"GISデータ: 簡略化国境データを生成・キャッシュしました "
                f"({len(features)}件, {os.path.getsize(cache_path):,} bytes)"
            )
        except Exception as e:
            logger.warning(f"簡略化国境データのキャッシュ書き込みに失敗しました: {e}")

        _simplified_countries_cache = features
        return features


def load_countries_simplified() -> list[dict]:
    """
    間引き済み（軽量版）の世界の国境データのfeatures配列を返す
    （2026-09-26追加）。build_simplified_countries_geojson() の薄い
    ラッパーで、生成済みならメモリ/ディスクキャッシュをそのまま返す
    だけの軽量な呼び出しになる（初回生成のみCPUバウンドで重いので
    注意。呼び出し元の注意事項は build_simplified_countries_geojson
    のdocstring参照）。core.gis_render.render_overseas_map・
    Webダッシュボードの /status/gis_countries の共通データソース。
    """
    return build_simplified_countries_geojson()


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
