"""
core/gateway_history.py
========================
Discord ゲートウェイ（Bot ⇔ Discord の常時接続）の切断・復旧の履歴を持つ（純粋なクラスのみ）。

【背景】
回線断や DNS 障害などで Discord と繋がらなくなった時間帯は、これまでログを遡らないと
分からなかった。Web ダッシュボードで「いま繋がっているか」「いつ・何分切れていたか」を
一目で確認できるよう、切断と復旧の時刻を記録する。

【設計】
- Discord・core.config に依存しない（SystemCog の on_disconnect / on_ready / on_resumed から
  呼ぶだけ。環境変数なしで単体テストできる）。
- 履歴は件数上限つきのメモリ内リングバッファ（Bot の再起動でリセット。ダッシュボードの
  他の履歴と同じ扱い）。
- 切断中に on_disconnect が繰り返し来ても、1回の切断として扱う。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Optional

# 保持する切断履歴の最大件数
GATEWAY_HISTORY_MAX = 50


class GatewayHistory:
    """切断（開始・復旧・継続時間）の履歴と、現在の接続状態を管理する。"""

    def __init__(self, started_at: datetime, max_events: int = GATEWAY_HISTORY_MAX):
        self._state_since = started_at                    # 現在の状態（接続中/切断中）になった時刻
        self._down_since: Optional[datetime] = None       # 切断中ならその開始時刻
        self._events: deque = deque(maxlen=max_events)    # 完了した切断（新しいものが右）
        self.total_disconnects = 0
        self.total_downtime_sec = 0.0
        self.longest_sec = 0.0

    @property
    def is_down(self) -> bool:
        return self._down_since is not None

    def on_disconnect(self, now: datetime) -> None:
        """切断を検知した。すでに切断中なら何もしない（同じ切断の重複通知）。"""
        if self._down_since is not None:
            return
        self._down_since = now
        self._state_since = now
        self.total_disconnects += 1

    def on_connected(self, now: datetime) -> None:
        """接続（再開・再接続）を確認した。切断中でなければ何もしない。"""
        if self._down_since is None:
            return
        duration = max(0.0, (now - self._down_since).total_seconds())
        self._events.append({
            "start": self._down_since.isoformat(),
            "end": now.isoformat(),
            "duration_sec": round(duration, 1),
        })
        self.total_downtime_sec += duration
        self.longest_sec = max(self.longest_sec, duration)
        self._down_since = None
        self._state_since = now

    def snapshot(self, now: datetime, limit: int = 20) -> dict:
        """ダッシュボード用の辞書。events は新しい順で、切断中ならその分を先頭に含む（end=None）。"""
        events = list(self._events)[::-1]
        if self._down_since is not None:
            ongoing = max(0.0, (now - self._down_since).total_seconds())
            events.insert(0, {"start": self._down_since.isoformat(), "end": None,
                              "duration_sec": round(ongoing, 1)})
        downtime = self.total_downtime_sec
        longest = self.longest_sec
        if self._down_since is not None:
            ongoing = max(0.0, (now - self._down_since).total_seconds())
            downtime += ongoing
            longest = max(longest, ongoing)
        return {
            "connected": self._down_since is None,
            "state_since": self._state_since.isoformat(),
            "state_duration_sec": round(max(0.0, (now - self._state_since).total_seconds()), 1),
            "total_disconnects": self.total_disconnects,
            "total_downtime_sec": round(downtime, 1),
            "longest_sec": round(longest, 1),
            "events": events[:limit],
        }
