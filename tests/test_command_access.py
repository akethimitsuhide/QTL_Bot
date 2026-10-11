"""
!status / /qtl_status の実行権限（core/command_access.py と cogs/system.py の配線）のテスト
（2026-10-12追加）。

確認すること:
1. モード・ロールIDの環境変数パース（不正値は admin に倒れる）
2. admin / all / role それぞれの実行可否（role では管理者も常に許可）
3. role モードでロールID未設定なら admin に倒れる
4. SystemCog 側の判定ヘルパーが User/Member の差（DM を含む）を正しく扱う
5. Discord 側の既定権限は、両方 admin のときだけ付く
"""
import os
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

from core.command_access import (  # noqa: E402
    ACCESS_ADMIN, ACCESS_ALL, ACCESS_ROLE,
    effective_mode, is_allowed, parse_access_mode, parse_role_ids,
)


@pytest.mark.parametrize("raw,expected", [
    (None, ACCESS_ADMIN), ("", ACCESS_ADMIN), ("  ", ACCESS_ADMIN),
    ("admin", ACCESS_ADMIN), ("ALL", ACCESS_ALL), (" Role ", ACCESS_ROLE),
    ("everyone", ACCESS_ADMIN),  # 不正値 → 安全側
])
def test_parse_access_mode(raw, expected):
    assert parse_access_mode(raw, "X") == expected


def test_parse_role_ids_skips_invalid():
    assert parse_role_ids("111, 222,abc,,333", "X") == frozenset({111, 222, 333})
    assert parse_role_ids(None, "X") == frozenset()


def test_effective_mode_role_without_ids_falls_back_to_admin():
    assert effective_mode(ACCESS_ROLE, frozenset(), "X") == ACCESS_ADMIN
    assert effective_mode(ACCESS_ROLE, frozenset({1}), "X") == ACCESS_ROLE
    assert effective_mode(ACCESS_ALL, frozenset(), "X") == ACCESS_ALL


def test_is_allowed_matrix():
    ids = frozenset({10, 20})
    # all: 誰でも
    assert is_allowed(ACCESS_ALL, ids, is_admin=False, member_role_ids=[])
    # admin: 管理者のみ（ロールは無関係）
    assert is_allowed(ACCESS_ADMIN, ids, is_admin=True)
    assert not is_allowed(ACCESS_ADMIN, ids, is_admin=False, member_role_ids=[10])
    # role: 該当ロール、または管理者
    assert is_allowed(ACCESS_ROLE, ids, is_admin=False, member_role_ids=[1, 20])
    assert not is_allowed(ACCESS_ROLE, ids, is_admin=False, member_role_ids=[1, 2])
    assert is_allowed(ACCESS_ROLE, ids, is_admin=True, member_role_ids=[])


# ---- SystemCog 側の配線 ----------------------------------------------------

def _member(admin=False, roles=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=admin),
        roles=[SimpleNamespace(id=r) for r in roles],
    )


def test_member_access_info_dm_user_is_not_admin():
    from cogs import system
    dm_user = SimpleNamespace(id=1)  # Member ではない（guild_permissions / roles なし）
    assert system._member_access_info(dm_user) == (False, [])
    assert system._member_access_info(_member(True, [5, 6])) == (True, [5, 6])


def test_can_use_status_follows_config(monkeypatch):
    from cogs import system
    monkeypatch.setattr(system, "QTL_STATUS_ACCESS", ACCESS_ROLE)
    monkeypatch.setattr(system, "QTL_STATUS_ROLE_IDS", frozenset({7}))
    assert system._can_use_status(_member(False, [7]))
    assert not system._can_use_status(_member(False, [8]))
    assert system._can_use_status(_member(True, []))

    monkeypatch.setattr(system, "QTL_STATUS_ACCESS", ACCESS_ADMIN)
    assert not system._can_use_status(_member(False, [7]))


def test_status_and_history_are_independent(monkeypatch):
    from cogs import system
    monkeypatch.setattr(system, "QTL_STATUS_ACCESS", ACCESS_ADMIN)
    monkeypatch.setattr(system, "QTL_STATUS_HISTORY_ACCESS", ACCESS_ALL)
    user = _member(False, [])
    assert not system._can_use_status(user)
    assert system._can_use_status_history(user)


def test_default_permissions_only_when_both_admin(monkeypatch):
    from cogs import system

    def _perm_attr(func):
        return getattr(func, "__discord_app_commands_default_permissions__", None)

    monkeypatch.setattr(system, "QTL_STATUS_ACCESS", ACCESS_ADMIN)
    monkeypatch.setattr(system, "QTL_STATUS_HISTORY_ACCESS", ACCESS_ADMIN)
    def f1(): ...
    assert _perm_attr(system._status_default_permissions(f1)) is not None

    monkeypatch.setattr(system, "QTL_STATUS_HISTORY_ACCESS", ACCESS_ALL)
    def f2(): ...
    assert _perm_attr(system._status_default_permissions(f2)) is None
