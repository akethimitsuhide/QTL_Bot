"""
core/delivery_stats.py
========================
Discordへの通知送信（channel.send）が実際に成功したか失敗したかを
記録する、全Cog共有の軽量な統計モジュール。

【設計方針: notification_log.py との違い】
core/notification_log.py は「何を通知したか」（内容）を記録するのに対し、
このモジュールは「送信が成功したか」（結果）だけを記録する、より
軽量なカウンタである。送信の都度、成功/失敗のブール値と大まかな
時刻だけを記録するリングバッファを保持し、Web Dashboardの
「配信成功率」表示（直近N時間の成功率）に使う。

【なぜ必要か】
Discord APIのレート制限や権限エラー等で通知が実際には送信できて
いなかった場合、従来はログファイルを漁らないと気づけなかった
（「大地震の直後に通知が来なかった気がする」を後から検証できない）。
このモジュールは、各Cogの通知送信箇所に1行 record_delivery() を
追加するだけで、Web Dashboardから配信成功率を確認できるようにする。

【設計方針: 失敗時も例外は握りつぶさない】
record_delivery() 自体は記録のみを行い、呼び出し元の例外処理・
再送ロジックには一切関与しない。呼び出し元（各Cogのnotify_*）は
これまで通り例外を捕捉してログに残す責務を持ち続ける。
"""
from collections import deque
from datetime import datetime, timedelta

# 保持件数の上限（メモリ上のリングバッファ、Bot再起動でリセット）。
# 全Cog合計で「直近の送信試行」を追跡できれば十分なため、
# notification_logより少し大きめの件数を確保しておく
# （通知系だけでなく音声・その他の送信試行も将来含める余地を持たせる）。
DELIVERY_LOG_MAXLEN = 500

_delivery_log: deque = deque(maxlen=DELIVERY_LOG_MAXLEN)


def record_delivery(success: bool, kind: str = "", detail: str = "") -> None:
    """
    通知送信の成功/失敗を1件記録する。

    Parameters
    ----------
    success : 送信が成功したか（例外が発生しなかったか）
    kind : 通知種別（core.notification_log.record_notification と
        同じラベル体系を推奨。例: "EEW", "地震情報"）
    detail : 失敗時のエラー内容等、補足情報（任意）
    """
    try:
        _delivery_log.append({
            "timestamp": datetime.now().isoformat(),
            "success": bool(success),
            "kind": kind,
            "detail": detail,
        })
    except Exception:
        # 記録の失敗で本来の送信フローを妨げないよう、ここで握りつぶす
        pass


def get_delivery_stats(window_hours: float | None = 24.0) -> dict:
    """
    直近 window_hours 時間（省略時は全保持件数）の配信成功率を集計して返す。

    戻り値: {"total": int, "success": int, "failure": int,
             "success_rate": float | None, "window_hours": float | None}
    success_rate は total=0 の場合 None を返す（0除算回避、かつ
    「まだ何も送信していない」ことを「成功率0%」と誤認させないため）。
    """
    items = list(_delivery_log)
    if window_hours is not None:
        cutoff = datetime.now() - timedelta(hours=window_hours)
        items = [
            it for it in items
            if datetime.fromisoformat(it["timestamp"]) >= cutoff
        ]

    total = len(items)
    success = sum(1 for it in items if it["success"])
    failure = total - success
    rate = (success / total * 100.0) if total > 0 else None

    return {
        "total": total,
        "success": success,
        "failure": failure,
        "success_rate": round(rate, 2) if rate is not None else None,
        "window_hours": window_hours,
    }


def get_recent_failures(limit: int = 20) -> list[dict]:
    """直近の失敗記録のみを新しい順で返す（障害調査用）。"""
    failures = [it for it in _delivery_log if not it["success"]]
    return list(reversed(failures))[:limit]
