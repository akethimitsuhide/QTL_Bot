"""
cogs/jishin_kanchi.py
=======================
P2P地震情報「地震感知情報」（code=9611）を受信し、Discordへ通知するCog。

【地震感知情報とは】
気象庁の公式発表ではなく、P2P地震情報が独自に集計する速報値
（詳細な算出方法は非公開）。信頼度（レベル1〜4、地域ごとのA〜F）が
付与されており、値が高いほど誤報の可能性が高い。JMA発表の地震情報
より速く傾向を把握できる利点がある一方、誤報リスクもあるため、
本Cogはデフォルトで無効（JISHIN_KANCHI_ENABLE=false）。

【受信経路】
core.p2p_ws_hub.P2PWebSocketHub の code=9611 ディスパッチャ
（キー "jishin_kanchi"）経由でメッセージを受け取る。WebSocket接続
そのものは他のP2P系Cog（EewCog/QuakeInfoCog/TsunamiCog）と共有する
1本のみで、新たな接続は増やさない。

【通知フォーマット】
ユーザー指定の仕様に基づく。地域は信頼度A〜Fでグルーピングし、
信頼度が高い順（A→F）に表示する。地域名は core.epsp_area の
地域コード変換マップ（epsp_area.csv）を用いる。地図画像は
地震情報通知（cogs/quake.py）と同じ生成方法
（cdn.p2pquake.net/app/images/{id}_trim_big.png）を用い、
embed最下部（set_image）に配置する。

【閾値によるフィルタリング】
- JISHIN_KANCHI_MAX_LEVEL: 通知する最大レベル（数値が大きいほど
  信頼度が低い）。既定4=全レベル通知。
- JISHIN_KANCHI_MIN_COUNT: 通知する最小件数（count）。

【音声読み上げ・効果音】
AudioClientMixin経由で共有AudioCogに処理を委譲する。効果音ファイルは
JISHIN_KANCHI_SOUND_FILE で設定可能（AudioCogのaudio_filesへ動的登録）。
"""
import logging
import time
from datetime import datetime

import discord
from discord.ext import commands

from core.config import (
    CHANNEL_ID, JISHIN_KANCHI_CHANNEL_ID, JISHIN_KANCHI_ENABLE,
    JISHIN_KANCHI_MAX_LEVEL, JISHIN_KANCHI_MIN_COUNT,
    JISHIN_KANCHI_SPEECH_ENABLE, JISHIN_KANCHI_SOUND_ENABLE,
    JISHIN_KANCHI_SOUND_FILE,
    JISHIN_KANCHI_EVENT_STATE_TTL_SEC,
)
from core.audio import AudioClientMixin
from core.p2p_image import P2PImageMixin
from core.notification_log import record_notification
from core.delivery_stats import record_delivery
from core.epsp_area import get_area_name
from core.jishin_kanchi_convert import (
    confidence_to_level, confidence_to_level_label,
    area_confidence_display, area_confidence_sort_key, format_p2p_time,
)

logger = logging.getLogger("QTLBot")

# 効果音として AudioCog.audio_files に動的登録するキー名
_SOUND_KEY = "jishin_kanchi"


class JishinKanchiCog(commands.Cog, AudioClientMixin, P2PImageMixin):
    """P2P地震情報「地震感知情報」（code=9611）の受信・通知を行う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel = None
        self.jishin_kanchi_channel = None
        self.session = None  # P2PImageMixin._attach_p2p_image が要求する

        # 直近に処理したイベントのstarted_atを記憶し、同一イベントの
        # 更新（updated_atのみ変化）で毎回通知が飛ぶのを抑制する用途は
        # 現状設けていない（P2P WebSocket Hub側のid単位デデュープに
        # 委ねる。started_atは仕様上「イベントを一意に識別するキー」と
        # 明記されているため、将来的な追加抑制のフックとして
        # _last_started_at を保持だけしておく）。
        self._last_started_at: str | None = None

        # 【2026-08-23 追加、2026-08-27 仕様変更】音声読み上げ・効果音の
        # イベント単位管理。
        # キー: started_at（イベント識別子＝第一報の識別子）
        # 値: {"last_seen_at": 最終更新時刻（time.monotonic()基準、TTL掃除用）}
        #
        # 【2026-08-27】以前は効果音のみ初回限定・読み上げはcount+50件
        # ごとに再トリガーしていたが、実運用で読み上げが繰り返し鳴って
        # うるさいとの指摘を受け、音声読み上げ・効果音とも「第一報
        # （started_atを初めて見たとき）の1回のみ」に統一した
        # （詳細は _judge_audio_triggers 参照）。
        self._event_states: dict[str, dict] = {}

        self._last_recv: dict[str, datetime | None] = {"jishin_kanchi": None}
        self._recv_count: dict[str, int] = {"jishin_kanchi": 0}

    async def cog_load(self):
        import aiohttp
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
        )
        logger.info("JishinKanchiCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("JishinKanchiCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel = self.bot.get_channel(CHANNEL_ID)
        self.jishin_kanchi_channel = (
            self.bot.get_channel(JISHIN_KANCHI_CHANNEL_ID) or self.channel
        )

        if not JISHIN_KANCHI_ENABLE:
            logger.info("JishinKanchiCog: JISHIN_KANCHI_ENABLE=false のため無効です")
        else:
            logger.info(
                f"JishinKanchiCog: on_ready 完了 "
                f"(最大レベル={JISHIN_KANCHI_MAX_LEVEL}, 最小件数={JISHIN_KANCHI_MIN_COUNT})"
            )

        # 効果音ファイル名をAudioCogへ動的登録する（JISHIN_KANCHI_SOUND_FILE
        # で管理者が任意のmp3ファイル名に変更できるようにするため。
        # AudioCog.audio_filesは静的な辞書のため、ここで実行時に追記する）。
        audio_cog = self.bot.get_cog("AudioCog")
        if audio_cog is not None and JISHIN_KANCHI_SOUND_FILE:
            audio_cog.audio_files[_SOUND_KEY] = JISHIN_KANCHI_SOUND_FILE

    # ===============================
    # P2P WebSocket Hub からのディスパッチ受け口
    # ===============================
    async def handle_p2p_jishin_kanchi(self, data: dict) -> None:
        """
        core.p2p_ws_hub.P2PWebSocketHub の "jishin_kanchi" ディスパッチャ
        （code=9611）から呼ばれるハンドラ。
        """
        if not JISHIN_KANCHI_ENABLE:
            return
        try:
            self._last_recv["jishin_kanchi"] = datetime.now()
            self._recv_count["jishin_kanchi"] += 1
            await self.notify_jishin_kanchi(data)
        except Exception:
            import traceback
            logger.error(f"地震感知情報 処理エラー:\n{traceback.format_exc()}")

    # ===============================
    # イベント単位の音声読み上げ・効果音制御（2026-08-23追加）
    # ===============================
    def _prune_stale_event_states(self, now: float) -> None:
        """
        JISHIN_KANCHI_EVENT_STATE_TTL_SEC 秒以上更新の無いイベント状態を
        削除する（メモリの際限ない増加を防ぐための単純なTTL掃除）。
        """
        cutoff = now - JISHIN_KANCHI_EVENT_STATE_TTL_SEC
        stale_keys = [
            k for k, v in self._event_states.items()
            if v["last_seen_at"] < cutoff
        ]
        for k in stale_keys:
            del self._event_states[k]

    def _judge_audio_triggers(self, event_key: str) -> tuple[bool, bool]:
        """
        与えられたイベント（started_at＝第一報の識別子）について、
        このイベントを初めて検知したかどうかで
        (効果音を鳴らすべきか, 音声読み上げをすべきか) のタプルを返す。

        【2026-08-27 変更】以前は「効果音は初回のみ・読み上げは
        count が JISHIN_KANCHI_SPEECH_COUNT_STEP 件以上増えるたびに実行」
        という別ルールだったが、実運用で件数が伸びるたびに読み上げが
        繰り返し鳴ってうるさいとの指摘を受け、音声読み上げ・効果音とも
        「第一報（started_at を初めて見たとき）の1回のみ」に統一した。
        JISHIN_KANCHI_SPEECH_COUNT_STEP は本メソッドでは使用しなくなった
        （設定項目自体は後方互換のため core.config に残してある）。

        ルール:
          - 初めて見るイベント（_event_states に未登録）
            → 効果音・音声読み上げとも鳴らす（EEW第一報相当）
          - 既知のイベントの更新（countのみ増加）
            → 効果音・音声読み上げとも鳴らさない
              （テキスト通知＝Embed自体は従来通り毎回送信される）

        呼び出しのたびに self._event_states を更新する副作用を持つ
        （TTL掃除も合わせて行う）。
        """
        now = time.monotonic()
        self._prune_stale_event_states(now)

        if event_key in self._event_states:
            # 既知のイベント（更新）→ 音声・効果音とも鳴らさない
            self._event_states[event_key]["last_seen_at"] = now
            return False, False

        # 初めて見るイベント（第一報）→ 効果音・音声読み上げとも1回だけ鳴らす
        self._event_states[event_key] = {"last_seen_at": now}
        return True, True

    # ===============================
    # 通知本体
    # ===============================
    async def notify_jishin_kanchi(self, data: dict, is_test: bool = False) -> None:
        channel = self.jishin_kanchi_channel or self.channel
        if not channel:
            return

        try:
            confidence = data.get("confidence")
            level = confidence_to_level(confidence)

            # confidence=0（非表示指定）またはレベル判定不能の場合は通知しない
            if level is None:
                logger.debug(
                    f"地震感知情報: confidence={confidence} のため非表示（通知スキップ）"
                )
                return

            # 閾値フィルタ: レベルが指定値より低精度（数値が大きい）場合は抑制
            if level > JISHIN_KANCHI_MAX_LEVEL:
                logger.debug(
                    f"地震感知情報: レベル{level} > JISHIN_KANCHI_MAX_LEVEL"
                    f"({JISHIN_KANCHI_MAX_LEVEL}) のため通知をスキップします"
                )
                return

            count = data.get("count", 0) or 0
            if count < JISHIN_KANCHI_MIN_COUNT:
                logger.debug(
                    f"地震感知情報: count={count} < JISHIN_KANCHI_MIN_COUNT"
                    f"({JISHIN_KANCHI_MIN_COUNT}) のため通知をスキップします"
                )
                return

            started_at = format_p2p_time(data.get("started_at"))
            updated_at = format_p2p_time(data.get("updated_at"))
            level_label = confidence_to_level_label(confidence)

            title = "地震感知情報"
            if is_test:
                title = "【テスト】" + title

            description = (
                f"**{title}**\n\n"
                f"開始時刻：{started_at}\n"
                f"最終更新：{updated_at}\n"
                f"情報信頼度：{level_label}\n"
                f"件数：{count}件"
            )

            # ── 地域ごとの状況（信頼度A→F順、地域名変換） ──
            area_confidences = data.get("area_confidences") or {}
            # {display文字("A"〜"F"): [(area_name, count), ...]}
            grouped: dict[str, list[tuple[str, int]]] = {}
            for area_code, info in area_confidences.items():
                info = info or {}
                display = area_confidence_display(info.get("display"), info.get("confidence"))
                area_name = get_area_name(area_code)
                area_count = info.get("count", 0) or 0
                grouped.setdefault(display, []).append((area_name, area_count))

            if grouped:
                description += "\n\n【地域ごとの状況】"
                for display in sorted(grouped.keys(), key=area_confidence_sort_key):
                    # グループ内は件数の多い順、同数なら地域名順で安定ソート
                    areas = sorted(grouped[display], key=lambda x: (-x[1], x[0]))
                    description += f"\n■ 信頼度{display}"
                    for area_name, area_count in areas:
                        description += f"\n　{area_name}（{area_count}件）"

            color = {1: 0xC31B1B, 2: 0xFF9939, 3: 0xF6CB51, 4: 0x62626B}.get(level, 0x62626B)

            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.now(),
            )
            if is_test:
                embed.set_footer(text="※これはテスト通知です。")

            try:
                sent_msg = await channel.send(embed=embed)
            except Exception as e:
                record_delivery(False, "地震感知情報", str(e))
                raise
            if not is_test:
                record_delivery(True, "地震感知情報")
                record_notification("地震感知情報", title, f"信頼度{level_label}・{count}件")

            # ── 地図画像（地震情報通知と同じ生成方法） ──
            # 【2026-08-27 修正】地震感知情報（code=9611）では、地図画像の
            # 取得には "id" ではなく "_id" フィールドを使う必要があることが
            # 実機ログで判明した（quake/tsunami の code=551/552 とはキーの
            # 優先順位が逆）。まず "_id" を優先し、無ければ "id" にフォール
            # バックする防御的な実装にしておく。
            image_id = data.get("_id") or data.get("id")
            if image_id:
                self.bot.loop.create_task(self._attach_p2p_image(sent_msg, image_id))

            # ── イベント単位の音声トリガー判定 ──
            # started_atをイベント識別子として使い、EEWのEventIDに相当する
            # 単位で「効果音は初回のみ」「読み上げは+50件ごと」に間引く
            # （詳細ロジックは _judge_audio_triggers のdocstring参照）。
            # is_test時はテスト通知のたびにイベント状態が汚染されないよう
            # 判定自体を呼ばない（常にFalse扱いとする）。
            if not is_test:
                event_key = data.get("started_at") or started_at
                should_play_sound, should_speak = self._judge_audio_triggers(event_key)
            else:
                should_play_sound, should_speak = False, False

            # ── 音声読み上げ（第一報のときのみ） ──
            if JISHIN_KANCHI_SPEECH_ENABLE and should_speak:
                speak_text = f"地震感知情報。信頼度{level_label.split('（')[0]}。{count}件の感知報告があります。"
                await self.speak_local(speak_text, priority=2)

            # ── 効果音再生（イベント初検知時のみ、EEW第一報相当） ──
            if JISHIN_KANCHI_SOUND_ENABLE and should_play_sound:
                await self.play_mp3(_SOUND_KEY)

        except Exception:
            import traceback
            # 送信失敗（channel.send）は既に上のtry/exceptで個別に
            # record_delivery(False, ...)を呼んでいるため、ここで
            # 重複して記録しない（send失敗時はraiseで再送出され、
            # このexceptにも到達するため、二重カウントを避ける必要がある）。
            # 送信より前の処理（信頼度判定・地域集計等）で例外が
            # 起きた場合は、送信自体は試みていないため配信記録の
            # 対象外とする（ログにのみ残す）。
            logger.error(f"notify_jishin_kanchi エラー:\n{traceback.format_exc()}")
