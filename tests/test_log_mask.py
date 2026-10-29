"""
tests/test_log_mask.py
=======================
ログ表示時の秘密情報マスク（core/log_mask.py）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.log_mask import mask_secrets  # noqa: E402

# GitHub のプッシュ保護（シークレット検知）は、トークン・Webhook の「形」だけで反応する。
# ダミーでも誤検知されるため、ソースには連続した形で書かず、実行時に組み立てる。
_WEBHOOK = "https://discord.com/api/webhooks/" + "123456789012345678" + "/" + "SECRETwebhookTOKEN-abc_123"
_SLACK = "https://hooks.slack.com/" + "services/" + "T000/B000/" + "XXXXXXXX"
_BOT_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GabcDE", "abcdefghijklmnopqrstuvwxyz1"])


def test_webhook_urls_are_masked():
    t, n = mask_secrets(f"送信先 {_WEBHOOK} へ")
    assert "webhooks" not in t and n == 1
    t, n = mask_secrets(_SLACK)
    assert "hooks.slack.com" not in t and n == 1


def test_key_value_json_bearer_and_query():
    t, _ = mask_secrets("token=abc123 WEB_DASHBOARD_TOKEN: s3cr3t-value")
    assert "abc123" not in t and "s3cr3t" not in t
    t, _ = mask_secrets('{"api_key": "k-12345", "n": 1}')
    assert "k-12345" not in t and '"n": 1' in t
    t, _ = mask_secrets("Authorization: Bearer abcdefghijklmnop12345")
    assert "abcdefghijklmnop12345" not in t
    t, _ = mask_secrets("GET /x?access_token=zzz111&a=1")
    assert "zzz111" not in t and "a=1" in t


def test_discord_bot_token_shape_is_masked():
    t, n = mask_secrets(f"bot {_BOT_TOKEN} end")
    assert "MTIzNDU2" not in t and n == 1


def test_ordinary_log_lines_are_untouched():
    for line in (
        "P2P地震情報取得: id=6ae20d5ae88ee598246bf443",
        "EEW 検知: EventID=20261029000759 Serial=3 → 通知",
        "WEB_DASHBOARD_TOKEN が短すぎます（20文字以上を推奨）",
        "QUAKE_RECORD_V1 {\"id\": \"jma:20261029001222_20261029000759_VXSE5k_1.json_1\", \"magnitude\": 5.4}",
    ):
        assert mask_secrets(line) == (line, 0)


def test_masking_is_idempotent():
    once, _ = mask_secrets("token=abc123")
    twice, n = mask_secrets(once)
    assert once == twice and n == 0
