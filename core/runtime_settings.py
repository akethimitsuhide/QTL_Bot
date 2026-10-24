"""
core/runtime_settings.py
=========================
再起動せずに変更できる設定（ホットリロード対象）の定義・現在値・変更処理。

【背景】
core/config.py の設定値はモジュール定数で、各 Cog が `from core.config import X` で
取り込むため、起動後に値を変えても反映されない。通知フィルターのように運用中に
調整したい設定だけを、ここで「実行時の値」として持ち、呼び出し側は
`runtime_settings.get("QUAKE_MIN_SCALE")` のように通知のたびに読む。

【対象にしている設定（通知のたびに値を読む箇所があるものだけ）】
  地震情報フィルター（震度・M・深さの下限/上限、情報種別ごとの ON/OFF）、
  津波情報・長周期地震動・後発地震注意情報等の ON/OFF、GIS 地図の配色テーマ。
【対象外】
  起動時にタスクやチャンネルを組み立てる設定（ENABLE_KYOSHIN、チャンネルID、
  WebSocket 設定など）。反映に再起動が必要なため。

【変更の流れ（change）】
  入力の検証 → .env へ書き戻し（core/env_writer.py）→ 実行時の値を更新。
  .env への書き込みに失敗した場合は、実行時の値も変更しない
  （「画面上の値」と「再起動後の値」がずれないようにする）。

【注意】
  systemd の EnvironmentFile や環境変数で同じキーを直接渡している場合、再起動後は
  そちらが優先され、.env への書き戻しは反映されない（python-dotenv は既存の環境変数を
  上書きしない）。
"""
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from core.env_writer import update_env_file

logger = logging.getLogger("QTLBot")

ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


class SettingError(ValueError):
    """ユーザー入力の誤りなど、そのまま利用者に見せてよいエラー。"""


_TRUE_WORDS = {"1", "true", "yes", "on", "有効", "オン"}
_FALSE_WORDS = {"0", "false", "no", "off", "無効", "オフ"}


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    kind: str                                   # "bool" | "int" | "float" | "choice"
    description: str
    minimum: float | None = None
    maximum: float | None = None
    # choice 用: (利用者向けの表記, .env に書く文字列, 内部の値)
    options: tuple[tuple[str, str, Any], ...] = ()

    # ── 入力 → 値
    def parse(self, raw: str) -> Any:
        text = (raw or "").strip()
        if not text:
            raise SettingError("値が空です。")
        if self.kind == "bool":
            low = text.lower()
            if low in _TRUE_WORDS:
                return True
            if low in _FALSE_WORDS:
                return False
            raise SettingError("on / off（または true / false、有効 / 無効）のいずれかを指定してください。")
        if self.kind == "choice":
            norm = text.replace("震度", "").replace("以上", "").strip()
            for label, env_text, value in self.options:
                if norm.lower() in (label.lower(), env_text.lower()):
                    return value
            raise SettingError("次のいずれかを指定してください: " + " / ".join(o[0] for o in self.options))
        try:
            number = int(text) if self.kind == "int" else float(text)
        except ValueError:
            raise SettingError(f"{'整数' if self.kind == 'int' else '数値'}を指定してください。") from None
        if self.kind == "float" and number != number:   # NaN
            raise SettingError("数値を指定してください。")
        if (self.minimum is not None and number < self.minimum) or (
            self.maximum is not None and number > self.maximum
        ):
            raise SettingError(f"{self.minimum:g}〜{self.maximum:g} の範囲で指定してください。")
        return number

    # ── 値 → .env に書く文字列 / 表示
    def to_env(self, value: Any) -> str:
        if self.kind == "bool":
            return "true" if value else "false"
        if self.kind == "choice":
            for _, env_text, v in self.options:
                if v == value:
                    return env_text
            raise SettingError(f"{self.key}: 未対応の値です: {value!r}")
        return str(value)

    def display(self, value: Any) -> str:
        if self.kind == "bool":
            return "有効" if value else "無効"
        if self.kind == "choice":
            for label, _, v in self.options:
                if v == value:
                    return self._pretty_choice(label)
            return str(value)
        return str(value)

    def _pretty_choice(self, label: str) -> str:
        if self.key == "QUAKE_MIN_SCALE":
            return "全て" if label == "全て" else f"震度{label}以上"
        return label

    def input_hints(self) -> list[str]:
        """スラッシュコマンドのオートコンプリート用の入力候補。"""
        if self.kind == "bool":
            return ["on", "off"]
        if self.kind == "choice":
            return [o[0] for o in self.options]
        return []


_SCALE_OPTIONS = (
    ("全て", "0", 0), ("1", "10", 10), ("2", "20", 20), ("3", "30", 30), ("4", "40", 40),
    ("5弱", "45", 45), ("5強", "50", 50), ("6弱", "55", 55), ("6強", "60", 60), ("7", "70", 70),
)
_THEME_OPTIONS = tuple((t, t, t) for t in ("light", "dark", "highlight"))


def _bool(key: str, label: str, description: str) -> SettingSpec:
    return SettingSpec(key, label, "bool", description)


# 表示順＝この並び。key は core/config.py の環境変数名と同じ（.env へそのまま書き戻す）
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("QUAKE_MIN_SCALE", "地震情報: 震度の下限", "choice",
                "この震度未満の地震情報は通知しない", options=_SCALE_OPTIONS),
    SettingSpec("QUAKE_MIN_MAG", "地震情報: Mの下限", "float",
                "このマグニチュード未満は通知しない", minimum=0.0, maximum=10.0),
    SettingSpec("QUAKE_MIN_DEPTH", "地震情報: 深さの下限(km)", "int",
                "この深さ未満（浅い）は通知しない", minimum=0, maximum=9999),
    SettingSpec("QUAKE_MAX_DEPTH", "地震情報: 深さの上限(km)", "int",
                "この深さを超える（深い）ものは通知しない", minimum=0, maximum=9999),
    _bool("QUAKE_ENABLE_SCALE_PROMPT", "地震情報: 震度速報", "震度速報の通知"),
    _bool("QUAKE_ENABLE_DESTINATION", "地震情報: 震源に関する情報", "震源に関する情報の通知"),
    _bool("QUAKE_ENABLE_SCALE_AND_DEST", "地震情報: 震度・震源に関する情報", "震度・震源に関する情報の通知"),
    _bool("QUAKE_ENABLE_DETAIL_SCALE", "地震情報: 各地の震度に関する情報", "各地の震度に関する情報の通知"),
    _bool("QUAKE_ENABLE_FOREIGN", "地震情報: 遠地地震に関する情報", "遠地地震に関する情報の通知"),
    _bool("QUAKE_ENABLE_OTHER", "地震情報: その他の情報", "その他の地震情報の通知"),
    _bool("EEW_ENABLE", "EEW（緊急地震速報）", "緊急地震速報の通知・読み上げ・効果音"),
    _bool("QUAKE_ENABLE", "地震情報（全体）", "地震情報（震度速報・震源・各地の震度など）の通知"),
    _bool("VOLCANO_ENABLE", "火山情報", "火山情報・噴火速報・噴火警報の通知"),
    _bool("TSUNAMI_ENABLE", "津波情報", "津波情報（P2P）の通知"),
    _bool("ENABLE_LONG_PERIOD", "長周期地震動", "長周期地震動に関する観測情報の通知"),
    _bool("ENABLE_ADVISORY", "後発地震注意情報等", "後発地震注意情報・南海トラフ地震臨時情報等の通知"),
    SettingSpec("GIS_MAP_THEME", "GIS地図: 配色テーマ", "choice",
                "次に描画する地図から反映", options=_THEME_OPTIONS),
)
SPEC_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SPECS}


class RuntimeSettings:
    """実行時の設定値の保持と変更。読み取りはロック不要（辞書の get はアトミック）。"""

    def __init__(self, initial: Mapping[str, Any]):
        missing = [k for k in SPEC_BY_KEY if k not in initial]
        if missing:
            raise KeyError(f"初期値がありません: {missing}")
        self._values: dict[str, Any] = {k: initial[k] for k in SPEC_BY_KEY}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        return self._values[key]

    def snapshot(self) -> dict[str, Any]:
        return dict(self._values)

    def _check_depth_order(self, key: str, new_value: Any) -> None:
        lo = new_value if key == "QUAKE_MIN_DEPTH" else self._values["QUAKE_MIN_DEPTH"]
        hi = new_value if key == "QUAKE_MAX_DEPTH" else self._values["QUAKE_MAX_DEPTH"]
        if key in ("QUAKE_MIN_DEPTH", "QUAKE_MAX_DEPTH") and lo > hi:
            raise SettingError(f"深さの下限（{lo}km）が上限（{hi}km）を超えています。")

    def change(self, key: str, raw: str, env_path: str | None = None, persist: bool = True) -> tuple[str, str]:
        """
        設定を変更して (変更前の表示, 変更後の表示) を返す。
        .env への書き込みに失敗した場合は SettingError を送出し、実行時の値は変更しない。
        """
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise SettingError(f"変更できない設定です: {key}")
        new_value = spec.parse(raw)
        with self._lock:
            old_value = self._values[key]
            self._check_depth_order(key, new_value)
            if persist:
                try:
                    update_env_file(env_path or ENV_FILE_PATH, {key: spec.to_env(new_value)})
                except OSError as e:
                    logger.error(f"設定の .env への保存に失敗しました（{key}）: {e}")
                    raise SettingError(
                        f".env に保存できませんでした（{type(e).__name__}）。設定は変更していません。"
                        f"ファイルの権限を確認してください。"
                    ) from e
            self._values[key] = new_value
        logger.info(f"設定を変更しました: {key}: {spec.display(old_value)} → {spec.display(new_value)}")
        return spec.display(old_value), spec.display(new_value)


def _initial_from_config() -> dict[str, Any]:
    from core import config
    return {key: getattr(config, key) for key in SPEC_BY_KEY}


settings = RuntimeSettings(_initial_from_config())


def get(key: str) -> Any:
    """実行時の設定値を返す。通知のたびに呼ぶ（値をモジュール定数に取り込まないこと）。"""
    return settings.get(key)
