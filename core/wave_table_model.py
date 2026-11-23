"""
core/wave_table_model.py
=========================
EEW地図の予報円（core/eew_wave.py）に使う走時表の「モデル選択」だけを担うモジュール。
走時表の読み込み・逆引き自体は core/travel_time.py（TravelTimeTable）をそのまま使う。

【対応モデル（EEW_WAVE_TABLE_MODEL）】
  jma2001a : JMA2001A走時表（陸域用）。既定。従来どおり EEW_WAVE_TABLE_FILE のファイルを
             core.travel_time.get_table で読む（この経路は JMA2020 対応前から変更していない）。
  jma2020a : JMA2020A走時表（東北太平洋沖用）
  jma2020b : JMA2020B走時表（東北アウターライズ用）
  jma2020c : JMA2020C走時表（南海トラフ用）

【JMA2020 の扱い】
  気象庁の公開ページ（走時表フォーマット tttfmt）によると、JMA2020A/B/C は JMA2001A と同じ
  ファイル形式・同じメッシュ間隔（震央距離0〜2000km、深さ0〜700km）。そのため同じ
  TravelTimeTable で読める。配布元（出典: 気象庁ホームページ）:
  https://ds.data.jma.go.jp/eqev/data/bulletin/catalog/appendix/trtime/trt_j.html
  （tjma2020a.zip / tjma2020b.zip / tjma2020c.zip。標高100m毎のファイル群で、標高0mのファイルを使う）

  配置（既定）: <EEW_WAVE_TABLE_DIR>/tjma2020a/tjma2020a.-00000（b・c も同様）。
  JMA2020 系の標高別ファイルは tjma2020a.-00000 〜 tjma2020a.-08000（100m刻み）という名前で、
  観測点が海底（負の標高）の表のため「-」が付く（JMA2001A は tjma2001h.+00000）。このうち
  標高0mの表として「-00000」を使う（地図上の円は海面の震央距離で描くため）。
  その名前が無い場合は「+00000」、さらに無ければフォルダ内の標高0m相当（*.-00000 / *.+00000）が
  1つだけ見つかればそれを使う。

【注意】JMA2020A/B/C は気象庁の「観測点ごとの使用走時表」（観測点コード→走時表名）で観測点に
  割り当てられる走時表であり、震央の位置から自動で選ぶ公式の規則は無い。このモジュールは
  自動切り替えをせず、設定で選んだ1モデルだけを使う（根拠のない領域分けを作らないため）。

JMA2001A の表（travel_time.py のグローバルキャッシュ）と JMA2020 系の表（このモジュールの
キャッシュ）は別々に保持し、互いに上書きしない。標準ライブラリのみ。
"""
import glob
import logging
import os
import threading
from typing import Optional

from core import travel_time
from core.travel_time import TravelTimeTable

logger = logging.getLogger("QTLBot")

MODEL_JMA2001A = "jma2001a"
MODEL_JMA2020A = "jma2020a"
MODEL_JMA2020B = "jma2020b"
MODEL_JMA2020C = "jma2020c"
DEFAULT_MODEL = MODEL_JMA2001A

# モデル名 → 画像の注記に入れる表示名（JMA2001A は従来の注記を変えないため空）
_MODEL_LABELS = {
    MODEL_JMA2001A: "",
    MODEL_JMA2020A: "JMA2020A",
    MODEL_JMA2020B: "JMA2020B",
    MODEL_JMA2020C: "JMA2020C",
}
MODELS = tuple(_MODEL_LABELS)

# JMA2020 系のモデル → 既定のフォルダ名（= ファイル名の接頭辞）
_JMA2020_DIRS = {
    MODEL_JMA2020A: "tjma2020a",
    MODEL_JMA2020B: "tjma2020b",
    MODEL_JMA2020C: "tjma2020c",
}
# 観測点標高0mのファイルの接尾辞。JMA2020系の実ファイルは「-00000」（優先）。「+00000」は JMA2001A 形式の命名への備え
_ELEV0_SUFFIXES = (".-00000", ".+00000")


def normalize_model(value: Optional[str]) -> str:
    """設定値を対応モデル名にする。未対応・空は既定（jma2001a）。"""
    name = (value or "").strip().lower()
    return name if name in _MODEL_LABELS else DEFAULT_MODEL


def model_label(model: str) -> str:
    """画像の注記に添える表示名。JMA2001A は空文字（従来の注記のまま）。"""
    return _MODEL_LABELS.get(model, "")


def resolve_jma2020_path(model: str, base_dir: str) -> Optional[str]:
    """
    JMA2020 系モデルの走時表ファイル（標高0m）のパス。見つからなければ None。
    既定名 <base_dir>/<tjma2020x>/<tjma2020x>.-00000（無ければ .+00000）を優先し、無ければ
    フォルダ内の「*.-00000」「*.+00000」が合わせて1つだけの場合にそれを使う
    （複数ある場合は取り違え防止のため None）。
    """
    folder = _JMA2020_DIRS.get(model)
    if folder is None:
        return None
    for suffix in _ELEV0_SUFFIXES:
        primary = os.path.join(base_dir, folder, folder + suffix)
        if os.path.isfile(primary):
            return primary
    found = sorted({
        path for suffix in _ELEV0_SUFFIXES
        for path in glob.glob(os.path.join(base_dir, folder, "*" + suffix))
    })
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------
# JMA2020 系の表の遅延読み込み（モデルごとにプロセス内で1回）
# ---------------------------------------------------------------
_tables: dict = {}          # {モデル名: TravelTimeTable}
_failed: set = set()        # 読み込みに失敗したモデル（警告は1回だけ）
_lock = threading.Lock()


def _get_jma2020_table(model: str, base_dir: str) -> Optional[TravelTimeTable]:
    table = _tables.get(model)
    if table is not None:
        return table
    with _lock:
        table = _tables.get(model)
        if table is not None:
            return table
        if model in _failed:
            return None
        path = resolve_jma2020_path(model, base_dir)
        try:
            if path is None:
                raise OSError(
                    f"{os.path.join(base_dir, _JMA2020_DIRS[model], _JMA2020_DIRS[model] + _ELEV0_SUFFIXES[0])} が見つかりません"
                )
            table = TravelTimeTable.from_file(path)
        except (OSError, ValueError) as e:
            _failed.add(model)
            logger.warning(
                f"{model_label(model)}走時表を読み込めないため、EEW地図のP波・S波の円は描画しません: {e}"
            )
            return None
        _tables[model] = table
        lo, hi = table.depth_range
        logger.info(f"{model_label(model)}走時表を読み込みました (path={path}, 深さ={lo:g}〜{hi:g}km, {table.depth_count}段)")
        return table


def get_wave_table(model: str, jma2001_file: str, base_dir: str) -> Optional[TravelTimeTable]:
    """
    予報円に使う走時表を返す。読み込めなければ None（警告は1回だけ。置いた後は再起動が必要）。
      model        : normalize_model 済みのモデル名
      jma2001_file : JMA2001A の表ファイル（EEW_WAVE_TABLE_FILE）。model=jma2001a のときだけ使う
      base_dir     : JMA2020 系フォルダの親（EEW_WAVE_TABLE_DIR）。JMA2020 系のときだけ使う
    """
    if model in _JMA2020_DIRS:
        return _get_jma2020_table(model, base_dir)
    return travel_time.get_table(jma2001_file)


def reset_for_tests() -> None:
    with _lock:
        _tables.clear()
        _failed.clear()
