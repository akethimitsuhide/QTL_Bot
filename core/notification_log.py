"""
core/notification_log.py
=========================
Web Dashboard の「直近の通知履歴」表示用に、実際にDiscordへ送信した
通知（種別・タイトル・時刻）を軽量なリングバッファへ記録する共有モジュール。

【設計方針: なぜモジュールレベルの状態なのか】
各Cog（EewCog, QuakeInfoCog, TsunamiCog, VolcanoCog, UsgsCog,
OtherInfoCog）はそれぞれ独立したCogインスタンスであり、共通の基底
クラスを持たない（音声再生は core.audio.AudioMixin 経由で共有して
いるが、これは別の関心事）。全Cog横断で「直近の通知」を1つの
リストとして集約するには、AudioMixin と同様にモジュールレベルの
共有状態を使うのが最もシンプルで、Bot全体の構成を変更せずに済む。

status_history（core/system.py内、システムリソースの推移）とは
別物で、こちらは「実際に何が通知されたか」という内容そのものを
保持する。

【永続化しない設計】
メモリ上のリングバッファ（collections.deque）のみで保持し、
外部DB等は使用しない。Bot再起動でリセットされる（プロジェクトの
軽量・低リソース消費という方針に合わせるため）。
"""
from collections import deque
from datetime import datetime

# 保持件数の上限。Web Dashboardでの一覧表示に十分な件数として50件とした
# （status_historyのSTATUS_HISTORY_MAXLENとは独立した値）。
NOTIFICATION_LOG_MAXLEN = 50

_notification_log: deque = deque(maxlen=NOTIFICATION_LOG_MAXLEN)


def record_notification(kind: str, title: str, detail: str = "") -> None:
    """
    通知1件をリングバッファへ記録する。

    Parameters
    ----------
    kind : 通知種別を表す短いラベル（例: "EEW", "地震情報", "津波情報",
        "火山情報", "噴火速報", "噴火警報", "USGS", "長周期地震動",
        "気象庁その他"）。Web Dashboard側でのフィルタ・色分けに使う。
    title : 通知のタイトル（Discord Embedのtitleと同じ文言を渡す想定）。
    detail : 補足情報（震源地・マグニチュード等、任意）。省略可。

    呼び出し元（各Cogのnotify_*メソッド）でのテスト通知
    （is_test=True）は記録対象に含めない設計とする
    （呼び出し元側でis_testの場合はそもそも呼ばないこと）。

    例外を送出しない設計とする。この関数の失敗が本来の通知処理
    （Discordへの送信）に影響を与えてはならないため。
    """
    try:
        _notification_log.append({
            "timestamp": datetime.now().isoformat(),
            "kind": kind,
            "title": title,
            "detail": detail,
        })
    except Exception:
        # 通知記録の失敗で本来の通知フローを妨げないよう、ここで握りつぶす。
        # （ロガーの呼び出し自体も失敗しうる状況を想定し、あえて何もしない）
        pass


def get_recent_notifications(limit: int | None = None) -> list[dict]:
    """
    記録済みの通知履歴を新しい順（降順）で返す。

    limit を指定した場合はその件数までに絞る（省略時は全件、
    最大でも NOTIFICATION_LOG_MAXLEN 件）。
    """
    items = list(reversed(_notification_log))
    if limit is not None:
        return items[:limit]
    return items
