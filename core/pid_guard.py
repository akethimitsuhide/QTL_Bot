"""
core/pid_guard.py
==================
プロセスの二重起動を検知するための PID ファイル管理モジュール。

【なぜこれが必要か】
2026-08-01〜02、CLIテスト実行後に前のプロセス（旧バージョンのbot.py
プロセス）が完全に終了しないまま新しいプロセスを起動してしまい、
2つのQTL_Botプロセスが同時にWolfx/P2P WebSocketへそれぞれ独立して
接続し、同一の緊急地震速報・地震情報を2重に受信・通知する事故が
実際に発生した（詳細はコミット履歴・実機ログ参照）。

このモジュールは、通常運用時（systemdサービスとしての起動、または
`python3 bot.py` の直接起動）に、同じ役割のプロセスが既に動いている
場合はすぐに気づけるよう、起動時に PID ファイルを作成・チェックする。

【CLIテストモードとの関係】
CLIテスト（`python3 bot.py --test_xxx ...`）は、1回のテストが数秒で
完了し、意図的に短時間で連続実行することも多い（デバッグ時等）。
本番プロセスと同じ PID ファイルを共有すると「テスト実行のたびに
本番プロセスと衝突している」という誤検知が起きるため、CLIテスト
モードでは別のPIDファイル（拡張子に `.test` を付与）を使う。

【運用上の注意】
このガードは「気づけるようにする」ことが目的であり、二重起動を
強制的に禁止するものではない（ロックファイルによる排他制御ではなく、
警告ログの出力に留める）。理由:
  - Bot が異常終了（kill -9、電源断など）した場合、PIDファイルの
    削除処理（finally節）が実行されずファイルが残り続けることがある。
    その状態で正規の再起動を「二重起動」と誤検知してブロックして
    しまうと、正常な再起動ができなくなり本番影響の方が大きい。
  - 実際に二重起動していた場合でも、記録されているPIDのプロセスが
    既に存在しなければ「古いPIDファイルの残骸」と判断し、警告なしで
    上書きして起動を続行する。
"""
import logging
import os

logger = logging.getLogger("QTLBot")

_PID_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.pid")
_TEST_PID_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.test.pid")


def _pid_is_alive(pid: int) -> bool:
    """
    指定PIDのプロセスが現在も存在するかを確認する。
    Unix系専用（os.kill(pid, 0) はシグナルを送らずプロセスの存在確認のみ行う）。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 別ユーザーが所有するプロセスの場合、シグナル送信権限がなくても
        # プロセス自体は存在する（=生きている）と判断する
        return True
    except Exception:
        # 判定不能な場合は「安全側」として生きているとみなす
        return True
    return True


def check_and_write_pid(is_test_mode: bool = False) -> None:
    """
    起動時に呼び出す。既存のPIDファイルが有効な（=生きている）プロセスを
    指している場合は二重起動の可能性が高いため警告ログを出す。
    いずれの場合も、最終的に自分自身のPIDで（上書き）記録する。

    is_test_mode=True の場合は本番用と別のPIDファイル
    （bot.test.pid）を使うため、CLIテストの連続実行同士でしか
    衝突検知しない（本番プロセスとテスト実行を混同しない）。
    """
    path = _TEST_PID_FILE_PATH if is_test_mode else _PID_FILE_PATH
    my_pid = os.getpid()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing_pid_str = f.read().strip()
            existing_pid = int(existing_pid_str) if existing_pid_str else None
        except (ValueError, OSError) as e:
            existing_pid = None
            logger.warning(f"pid_guard: 既存PIDファイルの読み取りに失敗しました（無視して続行）: {e}")

        if existing_pid is not None and existing_pid != my_pid and _pid_is_alive(existing_pid):
            logger.warning(
                f"★★★ 二重起動の可能性があります ★★★ "
                f"PIDファイル ({path}) には既に稼働中と思われるプロセス "
                f"(PID={existing_pid}) が記録されています。"
                f"意図しない場合は 'kill {existing_pid}' 等で停止してから、"
                f"このプロセス (PID={my_pid}) を再起動することを推奨します。"
                f"同一の緊急地震速報・地震情報が二重に通知される可能性があります。"
            )
        elif existing_pid is not None and existing_pid != my_pid:
            logger.info(
                f"pid_guard: 古いPIDファイル (PID={existing_pid}) は既に終了しているプロセスの"
                f"ものと判断し、上書きします（異常終了時の残骸の可能性）"
            )

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(my_pid))
    except OSError as e:
        logger.warning(f"pid_guard: PIDファイルの書き込みに失敗しました（続行します）: {e}")


def remove_pid_file(is_test_mode: bool = False) -> None:
    """
    プロセス終了時に呼び出す。自分自身が書き込んだPIDファイルのみを
    削除する（他プロセスが既に上書きしている場合は削除しない）。
    """
    path = _TEST_PID_FILE_PATH if is_test_mode else _PID_FILE_PATH
    my_pid = os.getpid()

    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            recorded_pid_str = f.read().strip()
        if recorded_pid_str and int(recorded_pid_str) == my_pid:
            os.remove(path)
    except (ValueError, OSError) as e:
        logger.debug(f"pid_guard: PIDファイル削除中にエラー（無視します）: {e}")
