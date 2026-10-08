"""
起動直後の「起動前から存在した情報（既存情報）」判定の補助（2026-10-09追加）。

JMA の list.json 等をポーリングする各処理は、起動直後の最初の取得結果を
「既存情報」として記録のみ行い、通知しない（起動のたびに過去の情報を再通知しない
ため）。ただし発表時刻を見ずに一律で既存扱いにすると、次の取りこぼしが起きる。

- 起動直後の取得が失敗し、初期化が遅れた間（またはその初回取得の直前）に
  発表された本物の新規情報が、「既存情報」として握りつぶされる。

そこで、発表時刻が Bot の起動時刻以降の情報は「起動後に発表された新規情報」として
扱い、初回取得でも通知する。起動時刻より前の情報は従来どおり既存情報として記録のみ。
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

# このモジュールを最初に import した時刻（＝実質的にプロセス起動時刻）
BOT_START_EPOCH: float = time.time()

_JST = timezone(timedelta(hours=9))


def parse_report_epoch(raw) -> Optional[float]:
    """
    JMA の発表時刻文字列（ISO 8601。例: "2026-09-30T14:08:00+09:00"）を
    UNIX 時刻（秒）に変換する。タイムゾーンが無い場合は JST とみなす。
    解析できない場合は None。
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_JST)
    return dt.timestamp()


def reported_since_start(raw) -> bool:
    """
    発表時刻 raw が Bot の起動時刻以降なら True。
    解析できない場合は False（従来どおり「既存情報」として扱う＝誤通知を避ける側に倒す）。
    """
    epoch = parse_report_epoch(raw)
    return epoch is not None and epoch >= BOT_START_EPOCH
