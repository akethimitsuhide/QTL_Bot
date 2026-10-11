"""
core/command_access.py
=======================
!status / /qtl_status（および履歴オプション）の実行権限を、.env の設定で
切り替えるための判定ロジック。

【モード】
- admin : サーバー管理者のみ（既定）
- all   : 全ユーザー
- role  : 指定ロール（ROLE_IDS）を持つユーザーのみ。管理者は常に許可する

【設計上の注意】
- Discord 側の default_permissions はサーバー設定で上書きされうるため、
  実際の可否はコマンド内で必ずこの判定を通す（多重防御）。
- 不正な設定値は「より安全な側（admin）」に倒し、警告ログを残す。
- core.config に依存しない（config 側からこのモジュールを使うため、循環を避ける）。
- 権限情報（管理者かどうか・所持ロール）の取得は呼び出し側（Cog）の責務。
"""
import logging
from typing import Iterable

logger = logging.getLogger("QTLBot")

ACCESS_ADMIN = "admin"
ACCESS_ALL = "all"
ACCESS_ROLE = "role"
_VALID_MODES = (ACCESS_ADMIN, ACCESS_ALL, ACCESS_ROLE)


def parse_access_mode(raw: str | None, env_name: str) -> str:
    """
    環境変数の値を admin / all / role のいずれかに正規化する。
    未設定・空文字は admin。不正な値は警告を出して admin にする。
    """
    value = (raw or "").strip().lower()
    if not value:
        return ACCESS_ADMIN
    if value in _VALID_MODES:
        return value
    logger.warning(
        f"{env_name}: 不正な値 {raw!r} です（admin / all / role のいずれかを指定）。"
        f"admin（管理者のみ）として扱います"
    )
    return ACCESS_ADMIN


def parse_role_ids(raw: str | None, env_name: str) -> frozenset[int]:
    """
    カンマ区切りのロールID文字列を frozenset[int] にする。
    数値でない要素は警告を出して無視する。
    """
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning(f"{env_name}: 数値でないロールID {part!r} を無視しました")
    return frozenset(ids)


def effective_mode(mode: str, role_ids: frozenset[int], env_name: str = "") -> str:
    """
    role モードでロールIDが1件も無い場合は、誰も使えない/全員使えるといった
    意図しない状態を避けるため admin に倒す。
    """
    if mode == ACCESS_ROLE and not role_ids:
        if env_name:
            logger.warning(
                f"{env_name}: role モードですがロールIDが未設定です。admin（管理者のみ）として扱います"
            )
        return ACCESS_ADMIN
    return mode


def is_allowed(
    mode: str,
    role_ids: frozenset[int],
    *,
    is_admin: bool,
    member_role_ids: Iterable[int] = (),
) -> bool:
    """
    実行可否を返す。

    - all  : 常に許可
    - admin: 管理者のみ
    - role : 管理者、または role_ids のいずれかを持つユーザー
    """
    if mode == ACCESS_ALL:
        return True
    if is_admin:
        return True
    if mode == ACCESS_ROLE:
        return any(rid in role_ids for rid in member_role_ids)
    return False
