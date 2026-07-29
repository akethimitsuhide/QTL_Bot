"""
core/fetch_backoff.py
======================
HTTP ポーリング処理向けの Circuit Breaker（連続失敗時のバックオフ）ヘルパー。

各 Cog が個別に持っていた _fetch_failures / _fetch_backoff_until 辞書と
_fetch_backoff_is_active / _reset_fetch_backoff / _record_fetch_failure の
組を、Cog非依存の1クラスにまとめたもの。EewCog・QuakeInfoCog など複数の
Cog がそれぞれ自分専用のインスタンスを持って使う想定
（状態そのものは共有しない。ロジックだけを共通化する）。
"""
import time
import logging

logger = logging.getLogger("QTLBot")


class FetchBackoff:
    """key（例: "quake"）ごとに連続失敗回数とバックオフ期限を管理する。"""

    def __init__(self, failure_threshold: int, backoff_seconds: float):
        self.failure_threshold = failure_threshold
        self.backoff_seconds = backoff_seconds
        self._failures: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}

    def is_active(self, key: str) -> bool:
        now = time.monotonic()
        until = self._backoff_until.get(key, 0.0)
        if until > now:
            return True
        if self._failures.get(key, 0) > 0:
            self._failures[key] = 0
            self._backoff_until[key] = 0.0
        return False

    def reset(self, key: str) -> None:
        self._failures[key] = 0
        self._backoff_until[key] = 0.0

    def record_failure(self, key: str, reason: str) -> None:
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self.failure_threshold:
            self._backoff_until[key] = time.monotonic() + self.backoff_seconds
            logger.warning(
                f"{key} fetch failure threshold reached ({self._failures[key]}): "
                f"backoff for {self.backoff_seconds}s ({reason})"
            )
