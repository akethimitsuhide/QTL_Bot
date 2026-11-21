"""
tests/test_kyoshin_maintenance.py
=================================
強震モニタ・長周期地震動モニタのメンテナンスモード（cogs/kyoshin_monitor.py の /qtl_maintenance、
2026-11追加）のテスト。

不具合: 配信元（防災科研）のメンテナンス中、強震モニタの画像取得が毎秒失敗し、失敗時の探索を
含めて最大で毎秒5リクエスト（1日約43万回）を送り続けていた。メンテナンスモード中は、強震モニタ
（jma_s）の画像取得も、長周期地震動モニタ（abrspmx_s）の画像探索も、リクエストをいっさい送らない。
"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

import cogs.kyoshin_monitor as km  # noqa: E402

Cog = km.KyoshinMonitorCog


# ===============================
# 部品
# ===============================
class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, **kw):
        self.sent.append(embed)


class FakeUser:
    def __init__(self, admin=True, uid=1234):
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.id = uid

    def __str__(self):
        return "admin#0001"


class FakeInteraction:
    def __init__(self, admin=True):
        self.user = FakeUser(admin)
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send)

    async def _send(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    @property
    def text(self) -> str:
        """送信された内容（本文・Embed のタイトルと説明）を1つの文字列にしたもの。"""
        parts = []
        for m in self.sent:
            parts.append(m["content"] or "")
            if m["embed"] is not None:
                parts += [m["embed"].title or "", m["embed"].description or ""]
        return "\n".join(parts)


class ExplodingSession:
    """メンテナンス中に呼ばれてはいけない。呼ばれたら記録して失敗させる。"""

    def __init__(self):
        self.calls = []

    def _boom(self, url, **kw):
        self.calls.append(url)
        raise AssertionError(f"メンテナンス中にリクエストが送られました: {url}")

    get = head = _boom


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = str(tmp_path / "maint.json")
    monkeypatch.setattr(km, "KYOSHIN_MAINTENANCE_STATE_PATH", path)
    return path


def make_cog(monkeypatch, state_path, **cfg):
    monkeypatch.setattr(km, "ENABLE_KYOSHIN", True)
    monkeypatch.setattr(km, "KYOSHIN_RECV_JMA_S_ENABLE", True)
    for k, v in cfg.items():
        monkeypatch.setattr(km, k, v)
    bot = SimpleNamespace(get_cog=lambda n: None, is_closed=lambda: False)
    cog = Cog(bot)
    cog.session = ExplodingSession()
    cog.kyoshin_channel = FakeChannel()
    cog.lmoni_calls = 0

    async def fake_lmoni(session):
        cog.lmoni_calls += 1
        return "https://example/lmoni.gif"

    async def fake_play(key):
        pass
    cog._dual_image_fetcher.fetch_lmoni_url = fake_lmoni
    cog.play_mp3 = fake_play
    cog._last_image_url = "https://example/jma_s.gif"
    cog._last_image_at = time.monotonic()
    cog._vib_level, cog._vib_at = 150, time.monotonic()
    return cog


def run(coro):
    return asyncio.run(coro)


def start(cog, duration=None, admin=True):
    it = FakeInteraction(admin)
    run(Cog.slash_maintenance_start.callback(cog, it, duration))
    return it


# ===============================
# リクエストが送られない
# ===============================
def test_no_request_is_sent_to_jma_s_during_maintenance(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    cog.maintenance.start(None)
    cog._latest_max_shindo = 3.0
    for _ in range(5):                                    # 毎秒の取得が何回呼ばれても
        assert run(cog._fetch_current_shindo_map()) == {}
    assert cog.session.calls == []                        # リクエストは1回も送られない
    assert cog._latest_max_shindo is None                 # 古い最大震度が通知色に使われないよう消す


def test_requests_resume_after_manual_end(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    downloads = []

    async def fake_download():
        downloads.append(1)
        return None, None
    cog._download_latest_image = fake_download

    cog.maintenance.start(None)
    run(cog._fetch_current_shindo_map())
    assert downloads == []                                # メンテナンス中は取得しない
    cog.maintenance.end()
    run(cog._fetch_current_shindo_map())
    assert downloads == [1]                               # 解除すれば取得を再開する


def test_requests_resume_automatically_when_deadline_passes(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    downloads = []

    async def fake_download():
        downloads.append(1)
        return None, None
    cog._download_latest_image = fake_download

    cog.maintenance.start(60, now=time.time())            # 60秒間
    run(cog._fetch_current_shindo_map())
    assert downloads == []
    cog.maintenance.start(60, now=time.time() - 120)      # 期限が2分前に過ぎている状態
    run(cog._fetch_current_shindo_map())
    assert downloads == [1] and not cog.maintenance.active


def test_lmoni_probe_is_skipped_during_maintenance_but_notification_still_goes(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    cog.maintenance.start(None)
    run(cog._send_notification(eew=True, event=None))
    assert cog.lmoni_calls == 0                           # 長周期地震動モニタの画像探索（HEAD）は送らない
    assert len(cog.kyoshin_channel.sent) == 1             # 通知自体は振動レベルで継続する
    assert cog.kyoshin_channel.sent[0].thumbnail.url is None


def test_lmoni_probe_runs_normally_when_not_in_maintenance(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    run(cog._send_notification(eew=True, event=None))
    assert cog.lmoni_calls == 1
    assert cog.kyoshin_channel.sent[0].thumbnail.url == "https://example/lmoni.gif"


def test_cog_built_without_init_is_not_in_maintenance():
    """既存テストのように __init__ を通らずに作った Cog でも、取得処理が動く（メンテナンス扱いにならない）。"""
    cog = Cog.__new__(Cog)
    assert cog._in_maintenance() is False


# ===============================
# コマンド
# ===============================
def test_command_group_is_admin_only_and_has_three_subcommands():
    group = Cog.qtl_maintenance
    assert group.name == "qtl_maintenance"
    assert group.default_permissions.administrator is True and group.guild_only is True
    assert {c.name for c in group.commands} == {"start", "end", "status"}


def test_start_with_duration(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    it = start(cog, "2h")
    assert cog.maintenance.is_active()
    assert abs(cog.maintenance.until - (time.time() + 7200)) < 5
    assert it.sent[0]["ephemeral"] is True
    assert "メンテナンスモードを開始しました" in it.text and "残り2時間0分" in it.text
    assert "admin#0001" in cog.maintenance.started_by


def test_start_without_duration_lasts_until_manual_end(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    it = start(cog, None)
    assert cog.maintenance.is_active() and cog.maintenance.until is None
    assert "手動で解除するまで" in it.text and "/qtl_maintenance end" in it.text
    start(make_cog(monkeypatch, state_path), "  ")        # 空白だけの期間も省略と同じ


def test_start_again_updates_the_period(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    start(cog, "1h")
    it = start(cog, None)
    assert "期間を更新しました" in it.text and cog.maintenance.until is None


def test_invalid_duration_is_rejected_and_state_is_unchanged(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    for bad in ("abc", "90", "999d"):
        it = start(cog, bad)
        assert "期間を指定できません" in it.text and it.sent[0]["ephemeral"] is True
        assert not cog.maintenance.is_active()


def test_non_admin_is_denied_for_every_subcommand(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    it = start(cog, "1h", admin=False)
    assert "権限がありません" in it.text and not cog.maintenance.is_active()
    cog.maintenance.start(None)
    for callback in (Cog.slash_maintenance_end.callback, Cog.slash_maintenance_status.callback):
        it = FakeInteraction(admin=False)
        run(callback(cog, it))
        assert "権限がありません" in it.text
    assert cog.maintenance.is_active()                    # 非管理者は解除もできない


def test_end_command(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    it = FakeInteraction()
    run(Cog.slash_maintenance_end.callback(cog, it))
    assert "メンテナンスモードではありません" in it.text
    start(cog, "1h")
    it = FakeInteraction()
    run(Cog.slash_maintenance_end.callback(cog, it))
    assert "解除しました" in it.text and "再開します" in it.text
    assert not cog.maintenance.is_active() and not os.path.exists(state_path)


def test_status_command(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    it = FakeInteraction()
    run(Cog.slash_maintenance_status.callback(cog, it))
    assert "通常稼働" in it.text
    start(cog, "90m")
    it = FakeInteraction()
    run(Cog.slash_maintenance_status.callback(cog, it))
    assert "メンテナンス中" in it.text and "残り1時間30分" in it.text and "admin#0001" in it.text


def test_commands_are_refused_when_kyoshin_is_disabled(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    monkeypatch.setattr(km, "ENABLE_KYOSHIN", False)
    it = start(cog, "1h")
    assert "ENABLE_KYOSHIN=false" in it.text and not cog.maintenance.is_active()


def test_save_failure_is_reported_to_the_user(monkeypatch, tmp_path):
    monkeypatch.setattr(km, "KYOSHIN_MAINTENANCE_STATE_PATH", str(tmp_path / "no_such_dir" / "maint.json"))
    cog = make_cog(monkeypatch, str(tmp_path))
    it = start(cog, "1h")
    assert cog.maintenance.is_active()                    # メモリ上は有効
    assert "保存できませんでした" in it.text               # 再起動で解除される旨を伝える


# ===============================
# 再起動後の引き継ぎ
# ===============================
def test_state_is_carried_over_after_restart(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    start(cog, "2h")
    restarted = make_cog(monkeypatch, state_path)          # Bot を再起動した想定（新しい Cog が状態ファイルを読む）
    assert restarted.maintenance.is_active()
    assert abs(restarted.maintenance.until - cog.maintenance.until) < 1
    assert run(restarted._fetch_current_shindo_map()) == {}
    assert restarted.session.calls == []                   # 再起動直後もリクエストを送らない


def test_manual_end_is_carried_over_after_restart(monkeypatch, state_path):
    cog = make_cog(monkeypatch, state_path)
    start(cog, None)
    run(Cog.slash_maintenance_end.callback(cog, FakeInteraction()))
    assert not make_cog(monkeypatch, state_path).maintenance.is_active()
