"""
core/maintenance.py
===================
強震モニタ・長周期地震動モニタ（防災科研 lmoni.bosai.go.jp）の「メンテナンスモード」の状態管理。

【背景】
配信元がメンテナンス中は画像が取得できず、取得に失敗するたびに探索のリクエストを毎秒送り続けて
しまう（強震モニタの画像取得は既定1秒間隔で、失敗時のフォールバック探索を含めると最大で毎秒5
リクエスト、1日あたり約43万回）。Discord のコマンド（/qtl_maintenance）でメンテナンス中で
あることを Bot に伝えると、この間は配信元へのリクエストをいっさい送らない。

【仕様】
- 強震モニタ（jma_s）と長周期地震動モニタ（abrspmx_s）は、まとめて1つの状態で切り替える。
- 期間を指定すると、その時刻に自動で解除される。省略すると、手動で解除するまで続く。
- 状態はファイル（JSON）に保存し、Bot を再起動しても引き継ぐ（期限も保持する）。
- 状態ファイルが壊れている・読めない場合は、警告を出して通常稼働として扱う（Bot の起動を妨げない）。
- 保存に失敗しても、メモリ上の状態は有効（再起動すると失われる）。呼び出し側が last_save_ok で
  確認して利用者に伝える。

【設計上の注意】
- 時刻は壁時計（time.time()、epoch 秒）で保存する。monotonic は再起動をまたげないため。
- 外部ライブラリ・Discord に依存しない純粋なロジック。テストしやすいよう、現在時刻は引数で注入できる。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("QTLBot")

_JST = ZoneInfo("Asia/Tokyo")

# 期間の上限。「999d」のような入力ミスで、実質的に解除されないまま放置されるのを防ぐ。
MAX_DURATION_SEC = 30 * 86400
MIN_DURATION_SEC = 60

_UNIT_SEC = {"d": 86400, "日": 86400, "h": 3600, "時間": 3600, "m": 60, "分": 60}
_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(日|時間|分|d|h|m)")


def parse_duration(text: str) -> float:
    """
    期間の文字列を秒数にする。例: "2h" "90m" "1d" "1h30m" "2時間30分" "1日"。

    単位（d/h/m または 日/時間/分）は必須。単位のない数字（"90"）は、分か時間かが曖昧なため
    受け付けない。解釈できない・0以下・短すぎる（1分未満）・長すぎる（30日超）場合は
    ValueError（メッセージは利用者向け）。
    """
    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        raise ValueError("期間が空です。例: 2h, 90m, 1d, 1h30m, 2時間")

    pos, total = 0, 0.0
    while pos < len(normalized):
        m = _TOKEN_RE.match(normalized, pos)
        if m is None:
            if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
                raise ValueError("単位を付けてください（例: 90m, 2h, 1d）。数字だけでは分か時間か判断できません")
            raise ValueError(f"期間を解釈できません: {text!r}（例: 2h, 90m, 1d, 1h30m, 2時間）")
        total += float(m.group(1)) * _UNIT_SEC[m.group(2)]
        pos = m.end()

    if total < MIN_DURATION_SEC:
        raise ValueError("期間は1分以上にしてください")
    if total > MAX_DURATION_SEC:
        raise ValueError(f"期間は{MAX_DURATION_SEC // 86400}日以内にしてください（長期の場合は期間を省略し、手動で解除してください）")
    return total


def format_remaining(seconds: float) -> str:
    """残り時間を「1日2時間」「1時間20分」「5分」「30秒」のような日本語表記にする。"""
    # 四捨五入（開始直後の「2h」が、経過したわずかな時間のせいで「残り1時間59分」と出ないように）
    total = max(0, round(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}日{hours}時間"
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分"
    return f"{secs}秒"


def format_jst(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _JST).strftime("%Y/%m/%d %H:%M")


@dataclass
class MaintenanceState:
    """メンテナンスモードの状態。active のとき、until が None なら手動で解除するまで続く。"""

    path: str
    active: bool = False
    until: float | None = None        # 終了予定時刻（epoch 秒）。None は手動解除まで
    started_at: float | None = None   # 開始時刻（epoch 秒）
    started_by: str = ""              # 開始した利用者（表示・ログ用）
    last_save_ok: bool = True         # 直近の保存が成功したか

    # ---------- 読み込み ----------
    @classmethod
    def load(cls, path: str, now: float | None = None) -> "MaintenanceState":
        """状態ファイルを読み込む。無い・壊れている・期限切れの場合は通常稼働の状態を返す。"""
        state = cls(path=path)
        now = time.time() if now is None else now
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as e:
            logger.warning(f"メンテナンス状態ファイルを読み込めません（通常稼働として扱います）: {path}: {e}")
            return state

        try:
            if not isinstance(data, dict) or data.get("active") is not True:
                return state
            until = data.get("until")
            if until is not None:
                until = float(until)
            started_at = data.get("started_at")
            state.until = until
            state.started_at = float(started_at) if started_at is not None else None
            state.started_by = str(data.get("started_by", ""))[:100]
            state.active = True
        except (TypeError, ValueError) as e:
            logger.warning(f"メンテナンス状態ファイルの内容が不正です（通常稼働として扱います）: {path}: {e}")
            return cls(path=path)

        if state.until is not None and now >= state.until:
            logger.info("メンテナンスモードの期限が、Bot の停止中に過ぎていたため解除しました")
            state._clear()
        return state

    # ---------- 状態の参照 ----------
    def is_active(self, now: float | None = None) -> bool:
        """メンテナンス中か。期限を過ぎていれば、ここで自動的に解除して False を返す。"""
        if not self.active:
            return False
        now = time.time() if now is None else now
        if self.until is not None and now >= self.until:
            logger.info("メンテナンスモードの期限が来たため解除しました。強震モニタ・長周期地震動モニタのリクエストを再開します")
            self._clear()
            return False
        return True

    def remaining(self, now: float | None = None) -> float | None:
        """残り秒数。メンテナンス中でない、または手動解除までの場合は None。"""
        if not self.is_active(now) or self.until is None:
            return None
        now = time.time() if now is None else now
        return max(0.0, self.until - now)

    def describe(self, now: float | None = None) -> str:
        """状態の説明文（!status・コマンドの応答用）。"""
        now = time.time() if now is None else now
        if not self.is_active(now):
            return "通常稼働"
        if self.until is None:
            return "メンテナンス中（手動で解除するまで）"
        return f"メンテナンス中（{format_jst(self.until)} まで・残り{format_remaining(self.until - now)}）"

    # ---------- 状態の変更 ----------
    def start(self, duration_sec: float | None, started_by: str = "", now: float | None = None) -> None:
        """メンテナンスモードを開始する（すでに開始済みなら期間を置き換える）。duration_sec が None なら手動解除まで。"""
        now = time.time() if now is None else now
        self.active = True
        self.started_at = now
        self.started_by = started_by[:100]
        self.until = None if duration_sec is None else now + duration_sec
        self.last_save_ok = self._save()

    def end(self) -> bool:
        """メンテナンスモードを解除する。解除した（それまで有効だった）場合は True。"""
        was_active = self.is_active()
        self._clear()
        return was_active

    # ---------- 内部 ----------
    def _clear(self) -> None:
        self.active = False
        self.until = None
        self.started_at = None
        self.started_by = ""
        self.last_save_ok = self._remove_file()

    def _save(self) -> bool:
        """状態を一時ファイル経由で原子的に保存する（書き込み途中の電源断でも壊れたファイルを残さない）。"""
        payload = {
            "active": self.active,
            "until": self.until,
            "started_at": self.started_at,
            "started_by": self.started_by,
        }
        tmp_path = None
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            fd, tmp_path = tempfile.mkstemp(prefix=".maintenance_", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, self.path)
            return True
        except OSError as e:
            logger.warning(f"メンテナンス状態を保存できません（再起動すると失われます）: {self.path}: {e}")
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    def _remove_file(self) -> bool:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"メンテナンス状態ファイルを削除できません（再起動すると再び有効になる場合があります）: {self.path}: {e}")
            return False
        return True
