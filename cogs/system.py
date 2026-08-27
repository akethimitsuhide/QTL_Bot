"""
cogs/system.py
===============
Bot全体の稼働状況を横断的に集約する Cog。

【この Cog が担当する機能】
- !status / /qtl_status コマンド（Bot稼働状態のEmbed表示）
- Web Dashboard（GET /status, /health, /health/full）
- エラー自動通知（管理者チャンネルへの通知・日次サマリー）
- リソース監視（CPU/メモリ/ディスク使用率の定期ログ）

【他モジュールとの依存関係】
- core.config      : 各種設定値（ADMIN_CHANNEL_ID, WEB_DASHBOARD_PORT 等）
- core.constants   : INT_MAP
- core.cog_utils   : get_cog_attr（他Cogの状態を安全に取得する）

【Step8 時点の設計: なぜ他Cogの内部状態を直接参照するのか】
分割前は単一クラスの self._last_recv 等を直接読むだけで済んでいたが、
Cog分割後は EewCog・QuakeInfoCog・AudioCog・TsunamiCog・VolcanoCog・
UsgsCog がそれぞれ自分自身の _last_recv / _recv_count / 各種タスク
ハンドルを保持している（EewCog=EEW、QuakeInfoCog=地震情報、
AudioCog=両者が共有する音声キューの実体）。

SystemCog はこれらを `core.cog_utils.get_cog_attr(self.bot, "EewCog", "_last_recv")`
のような形で「Cog名を指定して安全に読みにいく」。
これは理想的には「各Cogが自分の状態をイベントやプロパティとして公開する」
設計の方が疎結合だが、既存コードの構造をなるべく壊さず移行することを
優先し、直接属性アクセス方式を採用している。
Cog名は固定文字列 "EewCog" 等を使うため、Cogクラス名を変更する際は
この Cog 内の参照も合わせて更新すること。
"""
import os
import time
import socket
import logging
import asyncio
from datetime import datetime, timedelta
from collections import deque

import discord
from discord.ext import commands
import aiohttp

from core.config import (
    CHANNEL_ID, ADMIN_CHANNEL_ID,
    WOLFX_HEARTBEAT_TIMEOUT,
    USGS_ENABLED, USGS_MAGNITUDE_MIN, USGS_FETCH_INTERVAL,
    USGS_REGION_LAT_MIN, USGS_REGION_LAT_MAX,
    USGS_REGION_LON_MIN, USGS_REGION_LON_MAX,
    QUAKE_MIN_SCALE, QUAKE_MIN_MAG, QUAKE_MIN_DEPTH, QUAKE_MAX_DEPTH,
    STATUS_SHOW_CPU, STATUS_SHOW_MEM, STATUS_SHOW_DISK, STATUS_SHOW_UPTIME,
    RESOURCE_MONITORING_ENABLED, RESOURCE_CHECK_INTERVAL,
    STATUS_HISTORY_INTERVAL, STATUS_HISTORY_MAXLEN,
    DISK_WARNING_THRESHOLD, DISK_ERROR_THRESHOLD,
    HEALTH_CHECK_TIMEOUT, HEALTH_CHECK_CACHE_TTL, ERROR_NOTIFICATION_TTL,
    ENABLE_KYOSHIN,
    DIGEST_ENABLED, DIGEST_INTERVAL, DIGEST_WEEKDAY, DIGEST_HOUR, DIGEST_CHANNEL_ID,
)
from core.constants import INT_MAP
from core.cog_utils import get_cog_attr
from core.notification_log import get_recent_notifications
from core.delivery_stats import get_delivery_stats
from core import test_runner as _test_runner_module

logger = logging.getLogger("QTLBot")

# 【2026-08-27 修正】以前はこの変数を on_ready() 内で
# os.getenv("WEB_DASHBOARD_ENABLED", "true") として直接読み込んでおり、
# .env.example に明記されているデフォルト値「false」と実際のコードの
# デフォルト値「true」が食い違っていた。そのため .env に
# WEB_DASHBOARD_ENABLED を書き忘れると、ドキュメント上は無効なはずの
# Web Dashboard が意図せず起動しポートを待ち受けてしまう不具合があった。
# 他の WEB_DASHBOARD_* 設定と同様にモジュールレベル定数へ揃え、
# ドキュメント通り既定値を false に修正した。
WEB_DASHBOARD_ENABLED = os.getenv("WEB_DASHBOARD_ENABLED", "false").strip().lower() == "true"

WEB_DASHBOARD_PORT = int(os.getenv("WEB_DASHBOARD_PORT", "8080"))

# Web Dashboard のバインドアドレス。
# デフォルトは 127.0.0.1（ローカルホストのみ）にし、以前の "0.0.0.0"
# （誰でもアクセス可能）を既定挙動から外した。LAN内の別端末から見たい
# 場合などは .env で明示的に WEB_DASHBOARD_HOST=0.0.0.0 等を指定する。
WEB_DASHBOARD_HOST = os.getenv("WEB_DASHBOARD_HOST", "127.0.0.1").strip()

# アクセスを許可するクライアントIPのカンマ区切りリスト（例: "192.168.1.10,192.168.1.20"）。
# 空（未設定）の場合はIP制限を行わない（WEB_DASHBOARD_HOST側の制御のみに委ねる）。
# CIDR表記（例: "192.168.1.0/24"）にも対応する。
_raw_allowed_ips = os.getenv("WEB_DASHBOARD_ALLOWED_IPS", "").strip()
WEB_DASHBOARD_ALLOWED_IPS = [
    ip.strip() for ip in _raw_allowed_ips.split(",") if ip.strip()
] if _raw_allowed_ips else []

# GET /dashboard で返すグラフ表示用HTML。
# Chart.js は CDN から読み込み、グラフの計算・描画処理はすべて
# ブラウザ側（クライアント）で行う。Bot側（Raspberry Pi）は
# /status/history のスナップショット履歴を返すだけで、追加の
# 計算負荷は発生しない設計。
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QTL_Bot ダッシュボード</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #1e1e2e; color: #cdd6f4; margin: 0; padding: 20px; }
  h1 { font-size: 1.4em; margin-bottom: 4px; }
  .subtitle { color: #7f849c; font-size: 0.85em; margin-bottom: 16px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
          gap: 20px; }
  .card { background: #292c3c; border-radius: 8px; padding: 16px; }
  .card h2 { font-size: 1em; margin: 0 0 12px 0; color: #a6adc8; }
  canvas { max-height: 280px; }
  .status-line { color: #7f849c; font-size: 0.8em; margin-top: 8px; }
  .error { color: #f38ba8; padding: 20px; }
  .toolbar { margin-bottom: 16px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .btn-link { display: inline-block; color: #89b4fa; background: #292c3c;
              border: 1px solid #45475a; border-radius: 6px;
              padding: 6px 14px; font-size: 0.85em; text-decoration: none;
              cursor: pointer; font-family: inherit; }
  .btn-link:hover { background: #313244; }

  /* ── ステータスサマリーヘッダー ── */
  .summary-bar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }
  .summary-chip { background: #292c3c; border-radius: 8px; padding: 10px 16px;
                  min-width: 130px; flex: 1 1 130px; }
  .summary-chip .chip-label { color: #7f849c; font-size: 0.75em; margin-bottom: 4px; }
  .summary-chip .chip-value { font-size: 1.1em; font-weight: 600; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  .dot-green  { background: #a6e3a1; }
  .dot-yellow { background: #f9e2af; }
  .dot-red    { background: #f38ba8; }
  .dot-gray   { background: #6c7086; }

  /* ── 通知履歴 ── */
  .notif-table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  .notif-table th, .notif-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #313244; }
  .notif-table th { color: #7f849c; font-weight: normal; }
  .notif-kind { display: inline-block; padding: 2px 8px; border-radius: 10px;
                background: #45475a; color: #11111b; font-size: 0.85em; white-space: nowrap;
                font-weight: 600; }
  .notif-empty { color: #7f849c; padding: 12px 0; }
  .notif-card { grid-column: 1 / -1; }
  .notif-header-row { display: flex; justify-content: space-between; align-items: center;
                      flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .notif-header-row h2 { margin: 0; }
  .notif-filter { background: #1e1e2e; color: #cdd6f4; border: 1px solid #45475a;
                  border-radius: 6px; padding: 5px 10px; font-size: 0.85em; }

  @media (max-width: 480px) {
    body { padding: 10px; }
    .grid { grid-template-columns: 1fr; gap: 12px; }
    .summary-chip { min-width: 100px; }
  }
</style>
</head>
<body>
  <h1>QTL_Bot ダッシュボード</h1>
  <div class="subtitle">システムリソース・受信件数の推移（メモリ上のリングバッファ、Bot再起動でリセットされます）</div>

  <div class="summary-bar" id="summaryBar">
    <div class="summary-chip"><div class="chip-label">Bot状態</div><div class="chip-value" id="sumStatus">読み込み中...</div></div>
    <div class="summary-chip"><div class="chip-label">稼働時間</div><div class="chip-value" id="sumUptime">-</div></div>
    <div class="summary-chip"><div class="chip-label">Ping</div><div class="chip-value" id="sumPing">-</div></div>
    <div class="summary-chip"><div class="chip-label">Wolfx EEW</div><div class="chip-value" id="sumWolfx">-</div></div>
    <div class="summary-chip"><div class="chip-label">CPU / メモリ</div><div class="chip-value" id="sumResource">-</div></div>
    <div class="summary-chip"><div class="chip-label">ディスク</div><div class="chip-value" id="sumDisk">-</div></div>
  </div>

  <div class="toolbar">
    <a class="btn-link" href="/status/history?format=csv" download>CSVをダウンロード</a>
    <button class="btn-link" id="refreshBtn" type="button">今すぐ更新</button>
  </div>
  <div id="chartsContent" class="grid">
    <div class="card"><h2>CPU 使用率 (%)</h2><canvas id="cpuChart"></canvas></div>
    <div class="card"><h2>メモリ使用量 (MB)</h2><canvas id="memChart"></canvas></div>
    <div class="card"><h2>ディスク使用率 (%)</h2><canvas id="diskChart"></canvas></div>
    <div class="card"><h2>受信件数（累積）</h2><canvas id="recvChart"></canvas></div>
  </div>
  <div id="notifSection" class="grid" style="margin-top: 20px;">
    <div class="card notif-card">
      <div class="notif-header-row">
        <h2>直近の通知履歴</h2>
        <select class="notif-filter" id="notifFilter">
          <option value="">すべての種別</option>
        </select>
      </div>
      <div id="notifContent"><div class="notif-empty">読み込み中...</div></div>
    </div>
  </div>
  <div class="status-line" id="statusLine">読み込み中...</div>

<script>
const COLORS = {
  cpu: '#89b4fa', mem: '#a6e3a1', disk: '#f9e2af',
  wolfx: '#f38ba8', p2p_eew: '#fab387', quake: '#94e2d5',
  tsunami: '#89dceb', usgs: '#cba6f7', volcano: '#eba0ac',
};

// 通知種別ごとの表示色（notif-kindピルの背景色）。
// core/notification_log.py 経由で記録されるkind文字列と対応させる。
// 未知の種別が来た場合はCSS側のデフォルト（#45475a）にフォールバックする。
const KIND_COLORS = {
  'EEW': '#f38ba8', '地震情報': '#94e2d5', '津波情報': '#89dceb',
  '津波観測情報': '#74c7ec', '津波予報': '#89dceb', '震源要素更新': '#b4befe',
  '南海トラフ': '#eba0ac', '火山情報': '#fab387', '噴火速報': '#f9e2af',
  '噴火警報': '#f38ba8', 'USGS': '#cba6f7', '長周期地震動': '#a6e3a1',
  '気象庁その他': '#9399b2',
};

let notifItemsCache = [];  // フィルタ再描画用に直近取得分を保持

function makeLineChart(ctx, labels, datasets, yLabel) {
  return new Chart(ctx, {
    type: 'line',
    data: { labels: labels, datasets: datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { ticks: { color: '#7f849c', maxTicksLimit: 8 }, grid: { color: '#313244' } },
        y: { ticks: { color: '#7f849c' }, grid: { color: '#313244' }, title: { display: true, text: yLabel, color: '#7f849c' } },
      },
      plugins: { legend: { labels: { color: '#cdd6f4' } } },
    },
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ── ステータスサマリーヘッダー（/status を取得して表示） ──
async function loadAndRenderSummary() {
  try {
    const res = await fetch('/status');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    const statusOk = data.status === 'online';
    document.getElementById('sumStatus').innerHTML =
      '<span class="dot ' + (statusOk ? 'dot-green' : 'dot-red') + '"></span>' +
      escapeHtml(data.bot_user || (statusOk ? 'オンライン' : 'オフライン'));

    document.getElementById('sumUptime').textContent = data.uptime || '-';
    document.getElementById('sumPing').textContent =
      (data.ping_ms != null ? data.ping_ms + ' ms' : '-');

    const wolfx = (data.eew && data.eew.wolfx) || {};
    const wolfxStatus = wolfx.ws_status || 'unknown';
    const wolfxDot = wolfxStatus === 'online' ? 'dot-green'
      : (wolfxStatus === 'connecting' ? 'dot-yellow' : 'dot-red');
    document.getElementById('sumWolfx').innerHTML =
      '<span class="dot ' + wolfxDot + '"></span>' + escapeHtml(wolfxStatus);

    const sys = data.system || {};
    if (sys.cpu_percent != null) {
      document.getElementById('sumResource').textContent =
        sys.cpu_percent.toFixed(1) + '% / ' + sys.memory_mb + 'MB';
    }
    if (sys.disk_percent != null) {
      const diskDot = sys.disk_percent >= 90 ? 'dot-red' : (sys.disk_percent >= 80 ? 'dot-yellow' : 'dot-green');
      document.getElementById('sumDisk').innerHTML =
        '<span class="dot ' + diskDot + '"></span>' + sys.disk_percent + '%';
    }
  } catch (e) {
    document.getElementById('sumStatus').innerHTML =
      '<span class="dot dot-gray"></span>取得失敗';
  }
}

async function loadAndRender() {
  try {
    const res = await fetch('/status/history');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const history = data.history || [];

    if (history.length === 0) {
      document.getElementById('statusLine').textContent =
        'まだ履歴データがありません（記録間隔: ' + data.interval_sec + '秒。しばらく待ってから再読み込みしてください）';
      return;
    }

    const labels = history.map(h => {
      const d = new Date(h.timestamp);
      return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
    });

    makeLineChart(document.getElementById('cpuChart'), labels, [
      { label: 'CPU %', data: history.map(h => h.cpu_percent), borderColor: COLORS.cpu, tension: 0.2, pointRadius: 0 },
    ], '%');

    makeLineChart(document.getElementById('memChart'), labels, [
      { label: 'メモリ MB', data: history.map(h => h.memory_mb), borderColor: COLORS.mem, tension: 0.2, pointRadius: 0 },
    ], 'MB');

    makeLineChart(document.getElementById('diskChart'), labels, [
      { label: 'ディスク %', data: history.map(h => h.disk_percent), borderColor: COLORS.disk, tension: 0.2, pointRadius: 0 },
    ], '%');

    const recvKeys = ['wolfx', 'p2p_eew', 'quake', 'tsunami', 'jishin_kanchi', 'usgs', 'volcano', 'kyoshin'];
    const recvDatasets = recvKeys.map(k => ({
      label: k,
      data: history.map(h => (h.recv_count || {})[k] || 0),
      borderColor: COLORS[k],
      tension: 0.2,
      pointRadius: 0,
    }));
    makeLineChart(document.getElementById('recvChart'), labels, recvDatasets, '件数（累積）');

    const nowStr = new Date().toLocaleTimeString('ja-JP');
    document.getElementById('statusLine').textContent =
      data.count + '件のスナップショット（記録間隔: ' + data.interval_sec + '秒、最大保持: ' + data.max_points + '件） ／ 最終更新: ' + nowStr;
  } catch (e) {
    document.getElementById('chartsContent').innerHTML = '<div class="error">履歴データの取得に失敗しました: ' + e.message + '</div>';
  }
}

function renderNotifTable(items) {
  const el = document.getElementById('notifContent');
  if (items.length === 0) {
    el.innerHTML = '<div class="notif-empty">該当する通知がありません</div>';
    return;
  }
  let html = '<table class="notif-table"><thead><tr>' +
    '<th>時刻</th><th>種別</th><th>タイトル</th><th>詳細</th>' +
    '</tr></thead><tbody>';
  for (const n of items) {
    const d = new Date(n.timestamp);
    const timeStr = (d.getMonth() + 1) + '/' + d.getDate() + ' ' +
      d.getHours().toString().padStart(2, '0') + ':' +
      d.getMinutes().toString().padStart(2, '0') + ':' +
      d.getSeconds().toString().padStart(2, '0');
    const kindColor = KIND_COLORS[n.kind] || '#45475a';
    html += '<tr>' +
      '<td>' + timeStr + '</td>' +
      '<td><span class="notif-kind" style="background:' + kindColor + '">' + escapeHtml(n.kind) + '</span></td>' +
      '<td>' + escapeHtml(n.title) + '</td>' +
      '<td>' + escapeHtml(n.detail || '') + '</td>' +
      '</tr>';
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

function populateNotifFilterOptions(items) {
  const select = document.getElementById('notifFilter');
  const prevValue = select.value;
  const kinds = [...new Set(items.map(n => n.kind))].sort();

  select.innerHTML = '<option value="">すべての種別</option>' +
    kinds.map(k => '<option value="' + escapeHtml(k) + '">' + escapeHtml(k) + '</option>').join('');

  // 再描画後も選択中のフィルタを維持する（選択肢が存在する場合のみ）
  if (kinds.includes(prevValue)) {
    select.value = prevValue;
  }
}

function applyNotifFilter() {
  const selected = document.getElementById('notifFilter').value;
  const filtered = selected
    ? notifItemsCache.filter(n => n.kind === selected)
    : notifItemsCache;
  renderNotifTable(filtered);
}

async function loadAndRenderNotifications() {
  const el = document.getElementById('notifContent');
  try {
    const res = await fetch('/status/notifications?limit=50');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    notifItemsCache = data.notifications || [];

    populateNotifFilterOptions(notifItemsCache);
    applyNotifFilter();
  } catch (e) {
    el.innerHTML = '<div class="error">通知履歴の取得に失敗しました: ' + e.message + '</div>';
  }
}

function loadAll() {
  loadAndRenderSummary();
  loadAndRender();
  loadAndRenderNotifications();
}

document.getElementById('notifFilter').addEventListener('change', applyNotifFilter);
document.getElementById('refreshBtn').addEventListener('click', loadAll);

loadAll();
setInterval(loadAndRenderSummary, 15000);
setInterval(loadAndRender, 60000);
setInterval(loadAndRenderNotifications, 30000);
</script>
</body>
</html>
"""


class SystemCog(commands.Cog):
    """Bot全体の稼働状況集約・エラー監視・Web Dashboardを扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.channel = None
        self.admin_channel = None

        # -- HTTPセッション（ヘルスチェック用） --
        self.session: aiohttp.ClientSession | None = None

        # -- 起動時刻 --
        self._bot_start_time: datetime = datetime.now()
        self._start_time: float = time.time()

        # -- /status 用の永続 psutil.Process（リクエスト時のみCPU計測） --
        self._status_psutil_proc = None
        try:
            import psutil as _psutil_init
            self._status_psutil_proc = _psutil_init.Process(os.getpid())
            self._status_psutil_proc.cpu_percent(interval=None)  # 基準値をプライミング
        except Exception:
            pass

        # -- ヘルスチェックキャッシュ --
        self.health_check_cache = None
        self.last_health_check_time = None

        # -- エラー監視 --
        self.error_summary_task: asyncio.Task | None = None
        self.error_notification_cache: dict = {}
        self.error_count_today: int = 0
        self.daily_error_summary: dict = {}

        # -- リソース監視 --
        self.resource_monitor_task: asyncio.Task | None = None
        self.status_history_task: asyncio.Task | None = None

        # -- 週間/月間ダイジェスト（2026-08〜） --
        self.digest_task: asyncio.Task | None = None
        # 前回ダイジェスト実行時点の累積受信カウントのスナップショット。
        # 次回実行時にこれとの差分を取ることで「その期間内の件数」を
        # 求める（notification_logのような上限付きリングバッファでは
        # 活発な期間に取りこぼしが起きるため、無制限に増え続ける
        # 累積カウンタの差分方式を採用する）。
        self._digest_last_recv_count_snapshot: dict = {}
        self._digest_last_run_at: datetime | None = None

        # -- Web Dashboard グラフ・履歴表示用データ（2026-08-04 追加） --
        # resource_monitor が RESOURCE_CHECK_INTERVAL 秒ごとに1件ずつ
        # 追記する。deque(maxlen=...) により、上限を超えると自動的に
        # 古いものから破棄される（メモリを圧迫しない設計。Raspberry Pi
        # 上での軽量運用を維持するため、外部DB等は導入しない）。
        # STATUS_HISTORY_MAXLEN 分の履歴を保持する（デフォルト288件 ×
        # 300秒間隔 = 24時間分）。
        self.status_history: deque = deque(maxlen=STATUS_HISTORY_MAXLEN)

        # -- Web Dashboard --
        self._web_app = None
        self._web_runner = None

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
        )
        logger.info("SystemCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.error_summary_task and not self.error_summary_task.done():
            self.error_summary_task.cancel()
            logger.info("error_summary_worker タスクをキャンセルしました")

        if self.resource_monitor_task and not self.resource_monitor_task.done():
            self.resource_monitor_task.cancel()
            logger.info("resource_monitor タスクをキャンセルしました")

        if self.status_history_task and not self.status_history_task.done():
            self.status_history_task.cancel()
            logger.info("status_history_recorder タスクをキャンセルしました")

        if self.digest_task and not self.digest_task.done():
            self.digest_task.cancel()
            logger.info("digest_worker タスクをキャンセルしました")

        if self._web_runner:
            await self._web_runner.cleanup()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("SystemCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel = self.bot.get_channel(CHANNEL_ID)

        if ADMIN_CHANNEL_ID != 0:
            self.admin_channel = self.bot.get_channel(ADMIN_CHANNEL_ID)
            if self.admin_channel:
                logger.info(f"管理者チャンネルを設定しました（ID: {ADMIN_CHANNEL_ID}）")
            else:
                logger.warning(f"管理者チャンネルが見つかりません（ID: {ADMIN_CHANNEL_ID}）")

        if not self.error_summary_task:
            self.error_summary_task = self.bot.loop.create_task(self.error_summary_worker())
            logger.info("日次エラーサマリータスクを開始しました")

        if not self.resource_monitor_task:
            self.resource_monitor_task = self.bot.loop.create_task(self.resource_monitor())
            logger.info("リソース監視タスクを開始しました")

        if not self.status_history_task:
            self.status_history_task = self.bot.loop.create_task(self.status_history_recorder())
            logger.info("Web Dashboard 履歴記録タスクを開始しました")

        if not self.digest_task:
            self.digest_task = self.bot.loop.create_task(self.digest_worker())
            logger.info(
                f"週間/月間ダイジェストタスクを開始しました "
                f"(有効={DIGEST_ENABLED}, 間隔={DIGEST_INTERVAL})"
            )

        if _test_runner_module.CLI_TEST_MODE:
            # CLIテストモード（python3 bot.py --test_xxx ...）では、
            # systemd の本番プロセス（discord-bot.service）が既に同じ
            # WEB_DASHBOARD_PORT を使用中の可能性が高いため、Web Dashboard
            # の起動自体をスキップする（ポート衝突エラーの発生源を断つ）。
            logger.info(
                "CLIテストモードのため Web ダッシュボードの起動をスキップします"
                "（本番プロセスとのポート衝突を回避）"
            )
        elif os.getenv("WEB_DASHBOARD_ENABLED", "true").lower() == "true":
            self.bot.loop.create_task(self.start_web_dashboard())

        # スラッシュコマンドを同期
        # 複数Cogに分割された今も、この処理は1箇所（SystemCog）でのみ実行すれば良い
        # （bot.tree はグローバルなコマンドツリーであり、Cog横断で共有される）
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"スラッシュコマンドを同期しました（{len(synced)}件）")
        except Exception as e:
            logger.warning(f"スラッシュコマンド同期失敗: {e}")

        # Bot起動通知（管理者チャンネル宛）
        # 他Cogのon_readyが出揃うのを少し待ってから送る
        self.bot.loop.create_task(self._notify_startup())

        logger.info("SystemCog: on_ready 完了")

    async def _notify_startup(self) -> None:
        """Bot起動完了を管理者チャンネルに通知する。"""
        if not self.admin_channel:
            logger.debug("管理者チャンネル未設定のため起動通知はスキップします")
            return

        await asyncio.sleep(5)  # 他Cogのon_readyが出揃うのを待つ

        try:
            cog_names = list(self.bot.cogs.keys())
            embed = discord.Embed(
                title="QTL_Bot 起動完了",
                description="Bot が起動し、稼働を開始しました。",
                color=discord.Color.green(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="ログインユーザー", value=str(self.bot.user), inline=False)
            embed.add_field(name="登録Cog数", value=f"{len(cog_names)}件", inline=True)
            embed.add_field(name="Cog一覧", value=", ".join(cog_names) if cog_names else "なし", inline=False)
            embed.set_footer(text="QTL_Bot システム通知")

            await self.admin_channel.send(embed=embed)
            logger.info("Bot起動通知を管理者チャンネルに送信しました")
        except Exception as e:
            logger.error(f"Bot起動通知の送信に失敗: {e}", exc_info=True)

    # ===============================
    # 他Cogの状態を集約するヘルパー
    # ===============================

    def _eew_attr(self, name, default=None):
        return get_cog_attr(self.bot, "EewCog", name, default)

    def _quake_info_attr(self, name, default=None):
        return get_cog_attr(self.bot, "QuakeInfoCog", name, default)

    def _audio_attr(self, name, default=None):
        return get_cog_attr(self.bot, "AudioCog", name, default)

    def _tsunami_attr(self, name, default=None):
        return get_cog_attr(self.bot, "TsunamiCog", name, default)

    def _p2p_hub_stats(self) -> dict | None:
        """
        core.p2p_ws_hub.P2PWebSocketHub の統計情報を安全に取得する。
        bot.py で bot.p2p_hub にハブがぶら下げられている前提だが、
        CLIテストモード等でハブが未起動の場合も想定し None を許容する。
        """
        hub = getattr(self.bot, "p2p_hub", None)
        if hub is None:
            return None
        try:
            return hub.get_stats()
        except Exception:
            return None

    def _volcano_attr(self, name, default=None):
        return get_cog_attr(self.bot, "VolcanoCog", name, default)

    def _usgs_attr(self, name, default=None):
        return get_cog_attr(self.bot, "UsgsCog", name, default)

    def _other_attr(self, name, default=None):
        return get_cog_attr(self.bot, "OtherInfoCog", name, default)

    def _kyoshin_attr(self, name, default=None):
        return get_cog_attr(self.bot, "KyoshinMonitorCog", name, default)

    def _jishin_kanchi_attr(self, name, default=None):
        return get_cog_attr(self.bot, "JishinKanchiCog", name, default)

    def _kyoshin_active_events_suffix(self) -> str:
        """
        KyoshinMonitorCog.monitor.event_manager.events の件数
        （現在検知中の揺れイベント数）を " (検知中: N件)" の形式で返す。
        取得できない場合（Cog未登録・属性未初期化等）は空文字列を返す。
        """
        monitor = self._kyoshin_attr("monitor")
        if monitor is None:
            return ""
        try:
            event_manager = getattr(monitor, "event_manager", None)
            if event_manager is None:
                return ""
            count = len(getattr(event_manager, "events", {}))
            if count > 0:
                return f" (検知中: {count}件)"
            return ""
        except Exception:
            return ""

    def _kyoshin_status_dict(self) -> dict:
        """
        Web Dashboard JSON API (/status) 向けに、強震モニタ画像解析検知の
        稼働状態・現在検知中のイベント数を辞書で返す。
        """
        monitor = self._kyoshin_attr("monitor")
        active_event_ids: list[str] = []
        if monitor is not None:
            try:
                event_manager = getattr(monitor, "event_manager", None)
                if event_manager is not None:
                    active_event_ids = list(getattr(event_manager, "events", {}).keys())
            except Exception:
                pass
        return {
            "enabled": ENABLE_KYOSHIN,
            "active_event_count": len(active_event_ids),
            "active_event_ids": active_event_ids,
        }

    def _merged_last_recv(self) -> dict:
        """全Cogの _last_recv を1つの dict にマージして返す。"""
        merged: dict = {}
        for attr_getter in (self._eew_attr, self._quake_info_attr,
                             self._tsunami_attr,
                             self._volcano_attr, self._usgs_attr,
                             self._other_attr, self._jishin_kanchi_attr,
                             self._kyoshin_attr):
            d = attr_getter("_last_recv", {}) or {}
            merged.update(d)
        return merged

    def _merged_recv_count(self) -> dict:
        merged: dict = {}
        for attr_getter in (self._eew_attr, self._quake_info_attr,
                             self._tsunami_attr,
                             self._volcano_attr, self._usgs_attr,
                             self._other_attr, self._jishin_kanchi_attr,
                             self._kyoshin_attr):
            d = attr_getter("_recv_count", {}) or {}
            merged.update(d)
        return merged

    # ===============================
    # !status / /qtl_status コマンド
    # ===============================

    def _sample_resource_usage(self) -> dict | None:
        """
        プロセスのCPU使用率・メモリ使用量・ディスク使用率をまとめて
        計測する共通ヘルパー。

        【2026-08 統合の経緯】
        以前は3箇所がそれぞれ独自にpsutilで計測しており、計測方式が
        不統一だった:
          - _build_status_embed: 毎回 psutil.Process() を新規生成し、
            cpu_percent(interval=0.5) で0.5秒間 *同期的に* ブロックして
            計測していた（!status実行のたびにBot全体の応答が0.5秒
            停止する問題があった）
          - resource_monitor: 同様に毎回新規Process()を生成し、
            cpu_percent(interval=1) で1秒間ブロック
          - Web Dashboard /status・status_history_recorder: 起動時に
            プライミング済みの永続インスタンス self._status_psutil_proc
            を cpu_percent(interval=None) でノンブロッキング計測
            （こちらが正しい方式）

        本メソッドは全箇所を後者のノンブロッキング方式に統一する。
        self._status_psutil_proc（__init__ で起動時に一度だけ生成・
        プライミング済み）が利用できない場合（psutil未インストール等）
        は None を返す。
        """
        if self._status_psutil_proc is None:
            return None
        try:
            import psutil
            proc = self._status_psutil_proc
            cpu_percent = proc.cpu_percent(interval=None)
            mem_mb = proc.memory_info().rss / 1024 / 1024
            mem_total_mb = psutil.virtual_memory().total / 1024 / 1024
            disk = psutil.disk_usage("/")
            return {
                "cpu_percent": cpu_percent,
                "memory_mb": mem_mb,
                "memory_total_mb": mem_total_mb,
                "disk": disk,  # psutil.disk_usage結果（.percent/.free/.used/.total）
            }
        except Exception:
            return None

    def _history_csv_response(self, history_list: list[dict]):
        """
        status_history のスナップショット一覧を CSV（text/csv）の
        aiohttp.web.Response として組み立てて返す。

        history_handler（GET /status/history?format=csv）専用の
        ヘルパー。history_list の各スナップショットは
        {"timestamp", "cpu_percent", "memory_mb", "disk_percent",
         "recv_count": {...}} という構造（status_history_recorder参照）。
        recv_count は "recv_<key>" 列にフラット化する（CSVは
        ネスト構造を持てないため）。

        recv_count のキー集合はスナップショットごとに変わらない前提だが、
        念のため全スナップショットを走査してキー集合の和集合を取り、
        欠けているキーは空欄にする（将来的にrecv_countへキーが
        追加/削除されても壊れないようにするため）。
        """
        import csv
        import io
        from aiohttp import web

        recv_keys: list[str] = []
        seen_recv_keys: set[str] = set()
        for snap in history_list:
            for k in (snap.get("recv_count") or {}).keys():
                if k not in seen_recv_keys:
                    seen_recv_keys.add(k)
                    recv_keys.append(k)

        fieldnames = ["timestamp", "cpu_percent", "memory_mb", "disk_percent"]
        fieldnames += [f"recv_{k}" for k in recv_keys]

        buf = io.StringIO()
        # Excelでの文字化け防止のためUTF-8 BOM付きにする
        buf.write("\ufeff")
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for snap in history_list:
            row = {
                "timestamp": snap.get("timestamp", ""),
                "cpu_percent": snap.get("cpu_percent", ""),
                "memory_mb": snap.get("memory_mb", ""),
                "disk_percent": snap.get("disk_percent", ""),
            }
            recv_count = snap.get("recv_count") or {}
            for k in recv_keys:
                row[f"recv_{k}"] = recv_count.get(k, "")
            writer.writerow(row)

        filename = f"qtlbot_status_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return web.Response(
            body=buf.getvalue().encode("utf-8"),
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _build_status_embed(self) -> discord.Embed:
        """ステータス Embed を組み立てて返す（!status と /qtl_status 共通）"""
        metrics = self._sample_resource_usage()
        if metrics is not None:
            cpu = metrics["cpu_percent"]
            mem = metrics["memory_mb"]
            mem_total = metrics["memory_total_mb"]
            disk = metrics["disk"]
            _psutil_ok = True
        else:
            _psutil_ok = False
            cpu = mem = mem_total = disk = None

        now = datetime.now()
        uptime = now - self._bot_start_time
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{uptime.days}日 {h % 24}時間 {m}分 {s}秒"
        ping_ms = round(self.bot.latency * 1000)

        last_recv = self._merged_last_recv()
        recv_count = self._merged_recv_count()

        def api_status(key: str, warn_sec: int = 300, err_sec: int = 600,
                       connection_alive: bool | None = None) -> tuple[str, str]:
            """
            API/データソースごとの受信状況を (アイコン, 詳細文字列) で返す。

            【2026-08-04 修正: イベント駆動系の誤NG表示】
            当初はこの関数を「最後の受信からの経過時間」のみで判定していた。
            これはUSGS（10分間隔で必ずポーリングされる）のような定期
            ポーリング系には正しい判定方法だが、P2P地震情報・津波情報・
            長周期地震動等のイベント駆動系（地震や津波が実際に発生
            しない限り何も受信されない）には適用できない。実際に
            「2時間地震が発生していないだけで接続自体は正常」なのに
            [NG]（エラー）と誤表示される事象が実機で確認された。

            connection_alive を指定した場合はイベント駆動系向けの判定
            ロジックに切り替わる: 受信自体がまだ無くても接続/タスクが
            生存していれば正常（[OK] または [ - ]）とし、経過時間による
            [NG]化は行わない。接続/タスクが死んでいる場合のみ [NG] とする。
            connection_alive を省略した場合は従来通りの経過時間ベース
            判定を行う（USGS等の定期ポーリング系向け）。
            """
            t = last_recv.get(key)
            count = recv_count.get(key, 0)

            if connection_alive is not None:
                # イベント駆動系: 接続/タスクの生存状態を優先して判定する。
                # 受信済みなら受信時刻を表示し、未受信でも接続が生きて
                # いれば「[OK] 未受信（正常）」として扱う。
                if t is None:
                    if connection_alive:
                        return "[OK]", "未受信（接続は正常）"
                    else:
                        return "[NG]", "未受信（接続断）"
                diff = int((now - t).total_seconds())
                time_str = t.strftime("%H:%M:%S")
                count_str = f"(計{count}件)"
                if diff < 60:
                    ago = f"{diff}秒前"
                elif diff < 3600:
                    ago = f"{diff // 60}分{diff % 60}秒前"
                else:
                    ago = f"{diff // 3600}時間前"
                icon = "[OK]" if connection_alive else "[NG]"
                return icon, f"{time_str} ({ago}) {count_str}"

            # 従来ロジック（経過時間ベース、USGS等の定期ポーリング系向け）
            if t is None:
                return "[ - ]", "未受信"
            diff = int((now - t).total_seconds())
            time_str = t.strftime("%H:%M:%S")
            count_str = f"(計{count}件)"
            if diff < warn_sec:
                icon = "[OK]"
            elif diff < err_sec:
                icon = "[--]"
            else:
                icon = "[NG]"
            if diff < 60:
                ago = f"{diff}秒前"
            elif diff < 3600:
                ago = f"{diff // 60}分{diff % 60}秒前"
            else:
                ago = f"{diff // 3600}時間前"
            return icon, f"{time_str} ({ago}) {count_str}"

        def task_status(task_loop) -> str:
            if task_loop is None:
                return "[ - ] 未起動"
            if task_loop.is_running():
                return "[OK] 稼働中"
            if task_loop.failed():
                return "[NG] エラー停止"
            return "[--] 停止"

        def asyncio_task_status(task) -> str:
            if task is None:
                return "[ - ] 未起動"
            if not task.done():
                return "[OK] 稼働中"
            if task.cancelled():
                return "[--] キャンセル"
            if task.exception() is not None:
                return "[NG] エラー停止"
            return "[ - ] 完了"

        # Wolfx 状態（EewCog から取得）
        now_mono = time.monotonic()
        wolfx_last_heartbeat = self._eew_attr("_wolfx_last_heartbeat")
        wolfx_last_eew_recv = self._eew_attr("_wolfx_last_eew_recv")
        if wolfx_last_heartbeat is None:
            wolfx_icon, wolfx_detail = "[ - ]", "heartbeat 未受信（起動中）"
        else:
            hb_elapsed = now_mono - wolfx_last_heartbeat
            if hb_elapsed < WOLFX_HEARTBEAT_TIMEOUT:
                wolfx_icon = "[OK]"
                eew_detail = ""
                if wolfx_last_eew_recv is not None:
                    eew_diff = int((now - wolfx_last_eew_recv).total_seconds())
                    eew_detail = f", EEW {eew_diff}秒前"
                wolfx_detail = f"ONLINE ({hb_elapsed:.1f}s{eew_detail})"
            else:
                wolfx_icon = "[NG]"
                wolfx_detail = f"heartbeat TIMEOUT ({hb_elapsed:.1f}s > {WOLFX_HEARTBEAT_TIMEOUT}s)"

        color = 0x00FF00 if ping_ms < 100 else (0xFFFF00 if ping_ms < 300 else 0xFF0000)
        embed = discord.Embed(
            title="QTL_Bot ステータス",
            color=color,
            timestamp=now,
        )

        # -- システム --
        sys_lines = [f"稼働時間: {uptime_str}", f"Ping: {ping_ms}ms"]
        if _psutil_ok:
            if STATUS_SHOW_CPU:
                sys_lines.append(f"CPU: {cpu:.1f}%")
            if STATUS_SHOW_MEM:
                sys_lines.append(f"RAM: {mem:.0f} / {mem_total:.0f} MB ({mem / mem_total * 100:.1f}%)")
            if STATUS_SHOW_DISK:
                sys_lines.append(f"Disk: {disk.percent:.1f}% ({disk.used // 1024**3:.1f}/{disk.total // 1024**3:.1f} GB)")
        embed.add_field(name="システム", value="\n".join(sys_lines), inline=False)

        # -- EEW --
        p2p_eew_t = last_recv.get('p2p_eew')
        if p2p_eew_t is None:
            p2p_status = "[ - ] 未受信"
        else:
            p2p_diff = int((now - p2p_eew_t).total_seconds())
            p2p_status = f"[OK] {p2p_eew_t.strftime('%H:%M:%S')} ({p2p_diff}秒前) (計{recv_count.get('p2p_eew', 0)}件)"
        eew_lines = [
            f"{wolfx_icon} Wolfx: {wolfx_detail}",
            f"P2P EEW (警報専用・常時): {p2p_status}",
        ]
        embed.add_field(name="EEW", value="\n".join(eew_lines), inline=False)

        # -- API 受信状況 --
        # 【2026-08-04 修正】quake/tsunami/long_period/tsunami_obs/
        # quake_advisory/volcano はイベント駆動系（実際に地震・津波・
        # 噴火等が発生しない限り受信されない）のため、経過時間ではなく
        # 接続/タスクの生存状態で判定する。usgsのみ定期ポーリング系
        # （10分間隔で必ず実行される）のため従来通り経過時間で判定する。
        def _loop_is_running(task_obj) -> bool:
            """
            core.config.tasks.loop（is_running()を持つ）と
            asyncio.Task（done()を持つ）の両方に対応した生存判定。
            volcano_task 等は asyncio.Task、fetch_long_period 等は
            tasks.loop でラップされているため、両対応にしている。
            """
            if task_obj is None:
                return False
            try:
                if hasattr(task_obj, "is_running"):
                    return bool(task_obj.is_running())
                if hasattr(task_obj, "done"):
                    return not task_obj.done()
            except Exception:
                pass
            return False

        p2p_hub_alive = self._p2p_hub_stats() is not None

        api_rows = [
            # (label, key, warn_sec, err_sec, connection_alive)
            ("地震情報 (P2P)",     "quake",           120, 600, p2p_hub_alive),
            ("津波情報 (P2P)",     "tsunami",          60, 300, p2p_hub_alive),
            ("地震感知情報 (P2P)", "jishin_kanchi",   120, 600, p2p_hub_alive),
            ("長周期地震動",       "long_period",      120, 600, _loop_is_running(self._other_attr("fetch_long_period"))),
            ("津波観測情報",       "tsunami_obs",      120, 600, _loop_is_running(self._tsunami_attr("fetch_tsunami_observation"))),
            ("気象庁その他",       "quake_advisory",   120, 600, _loop_is_running(self._other_attr("fetch_quake_advisory"))),
            ("火山情報",           "volcano",         120, 600, _loop_is_running(self._volcano_attr("volcano_task"))),
            ("噴火速報",           "eruption",        120, 600, _loop_is_running(self._volcano_attr("eruption_task"))),
            ("噴火警報",           "warning",         120, 600, _loop_is_running(self._volcano_attr("warning_task"))),
            ("USGS 地震情報",      "usgs",            600, 1200, None),
        ]
        api_lines = []
        for label, key, warn, err, connection_alive in api_rows:
            icon, detail = api_status(key, warn, err, connection_alive=connection_alive)
            api_lines.append(f"{icon} **{label}**: {detail}")

        # 強震モニタ（jma_s系統の画像解析による、EEW発表を待たない常時
        # 検知。KyoshinMonitorCog）。ENABLE_KYOSHIN で機能自体が無効化
        # される場合があり、他のイベント駆動系と違って「無効」を明示
        # する必要があるため、api_rows の一律処理には含めず個別に
        # 組み立てる。
        if ENABLE_KYOSHIN:
            kyoshin_icon, kyoshin_detail = api_status(
                "kyoshin", 120, 600,
                connection_alive=_loop_is_running(self._kyoshin_attr("_monitor_task")),
            )
            api_lines.append(
                f"{kyoshin_icon} **強震モニタ（画像解析検知）**: "
                f"{kyoshin_detail}{self._kyoshin_active_events_suffix()}"
            )
        else:
            api_lines.append("[ - ] **強震モニタ（画像解析検知）**: 無効")

        # 長周期地震動モニタ（cogs/eew.py の vibration_monitor_loop。
        # EEW発表時のみ一時的に動作し、jma_s/abrspmx_s画像＋振動レベルを
        # 通知する別機能）。
        # 【設計メモ】この機能は「EEWが発表されていない」通常時は
        # 動いていないのが正常であり、他のAPI受信状況のような
        # 「長時間未受信=接続断」という判定は適用できない
        # （2026-08-04にquake/tsunami等で修正したのと同じ問題が
        # そのまま起こるため）。そのため api_status() の2つの判定モード
        # （経過時間ベース／接続生存ベース）のどちらにも寄せず、
        # 「EEW監視中か否か」で分岐する専用の表示にする。
        if ENABLE_KYOSHIN:
            lp_monitor_active = self._eew_attr("monitored_event_id") is not None
            lp_last = last_recv.get("long_period_monitor")
            lp_count = recv_count.get("long_period_monitor", 0)
            if lp_monitor_active:
                lp_line = "[OK] **長周期地震動モニタ**: EEW発表中・監視中"
            elif lp_last is not None:
                lp_diff = int((now - lp_last).total_seconds())
                if lp_diff < 60:
                    lp_ago = f"{lp_diff}秒前"
                elif lp_diff < 3600:
                    lp_ago = f"{lp_diff // 60}分{lp_diff % 60}秒前"
                else:
                    lp_ago = f"{lp_diff // 3600}時間前"
                lp_line = (
                    f"[ - ] **長周期地震動モニタ**: 待機中 "
                    f"（最終通知: {lp_last.strftime('%H:%M:%S')} {lp_ago}、計{lp_count}件）"
                )
            else:
                lp_line = "[ - ] **長周期地震動モニタ**: 待機中（EEW発表時のみ動作、通知実績なし）"
            api_lines.append(lp_line)
        else:
            api_lines.append("[ - ] **長周期地震動モニタ**: 無効（ENABLE_KYOSHIN=false）")

        embed.add_field(name="API 受信状況", value="\n".join(api_lines), inline=False)

        # -- タスク稼働状態 --
        p2p_hub_stats = self._p2p_hub_stats()
        if p2p_hub_stats is None:
            p2p_hub_line = "[ - ] 未起動 **P2PWebSocketHub (統合, 551/552/556/9611)**"
        else:
            recv = p2p_hub_stats.get("recv_count", {})
            p2p_hub_line = (
                f"[OK] 稼働中 **P2PWebSocketHub (統合, 551/552/556/9611)** "
                f"quake={recv.get('quake', 0)} tsunami={recv.get('tsunami', 0)} "
                f"eew={recv.get('eew', 0)} jishin_kanchi={recv.get('jishin_kanchi', 0)}"
            )
        task_lines = [
            p2p_hub_line,
            f"{task_status(self._tsunami_attr('fetch_tsunami_observation'))} **fetch_tsunami_observation**",
            f"{task_status(self._usgs_attr('fetch_usgs_quake')) if USGS_ENABLED else '[ - ] 無効'} **fetch_usgs_quake**",
            f"{asyncio_task_status(self._audio_attr('speech_task'))} **speech_worker (audio)**",
            f"{asyncio_task_status(self._audio_attr('mp3_task'))} **mp3_worker (audio)**",
            f"{asyncio_task_status(self._volcano_attr('volcano_task'))} **volcano_poller**",
            f"{asyncio_task_status(self._volcano_attr('eruption_task'))} **eruption_poller**",
            f"{asyncio_task_status(self._volcano_attr('warning_task'))} **warning_poller**",
            f"{task_status(self._other_attr('fetch_long_period'))} **fetch_long_period**",
            f"{task_status(self._other_attr('fetch_quake_advisory'))} **fetch_quake_advisory**",
            f"{asyncio_task_status(self._kyoshin_attr('_monitor_task')) if ENABLE_KYOSHIN else '[ - ] 無効'} "
            f"**kyoshin_monitor（画像解析検知）**{self._kyoshin_active_events_suffix()}",
            f"{asyncio_task_status(self._eew_attr('vibration_monitor_task')) if ENABLE_KYOSHIN else '[ - ] 無効'} "
            f"**vibration_monitor_loop（長周期地震動モニタ、EEW発表時のみ稼働）**",
        ]
        embed.add_field(name="タスク稼働状態", value="\n".join(task_lines), inline=False)


        # -- APM (Mackerel連携) --
        apm_cog = self.bot.get_cog("ApmCog")
        if apm_cog is not None:
            embed.add_field(name="APM (Mackerel)", value=apm_cog.apm_status_summary(), inline=False)

        # -- 配信成功率（直近24時間） --
        delivery = get_delivery_stats(window_hours=24.0)
        if delivery["total"] > 0:
            rate = delivery["success_rate"]
            icon = "🟢" if rate >= 99.0 else ("🟡" if rate >= 90.0 else "🔴")
            delivery_lines = [
                f"{icon} 成功率: {rate:.1f}% "
                f"({delivery['success']}/{delivery['total']}件、直近24時間)",
            ]
            if delivery["failure"] > 0:
                delivery_lines.append(f"⚠️ 失敗: {delivery['failure']}件")
            embed.add_field(name="配信成功率", value="\n".join(delivery_lines), inline=False)

        # -- フィルター設定 --
        if STATUS_SHOW_UPTIME:
            filter_lines = [
                f"震度下限: {INT_MAP.get(QUAKE_MIN_SCALE, str(QUAKE_MIN_SCALE))} / M下限: {QUAKE_MIN_MAG} / 深さ: {QUAKE_MIN_DEPTH}〜{QUAKE_MAX_DEPTH}km",
            ]
            embed.add_field(name="フィルター", value="\n".join(filter_lines), inline=False)

        return embed

    @commands.command(name="status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx):
        """Bot の稼働状態・各API受信状況・Ping を表示する"""
        embed = self._build_status_embed()
        await ctx.send(embed=embed)

    @cmd_status.error
    async def cmd_status_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("このコマンドはサーバー管理者のみ実行可能です。", delete_after=5)

    @discord.app_commands.command(name="qtl_status", description="QTL_Bot の稼働状態・各API受信状況を表示します（管理者専用）")
    @discord.app_commands.default_permissions(administrator=True)
    async def slash_qtl_status(self, interaction: discord.Interaction):
        """スラッシュコマンド版ステータス表示"""
        await interaction.response.defer(ephemeral=False)
        embed = self._build_status_embed()
        await interaction.followup.send(embed=embed)

    # ===============================
    # Web ダッシュボード
    # ===============================
    async def start_web_dashboard(self):
        """Web ダッシュボード（aiohttp）を起動"""
        from aiohttp import web
        import ipaddress
        # 【2026-08】CPU/メモリ/ディスク計測は _sample_resource_usage() に
        # 統一したため、ここでの psutil インポートは不要になった
        # （各ハンドラは _sample_resource_usage 経由で計測する）。

        port = WEB_DASHBOARD_PORT
        host = WEB_DASHBOARD_HOST
        allowed_ips = WEB_DASHBOARD_ALLOWED_IPS

        # 起動時に許可IP設定の妥当性を検証しておく（CIDR/単一IPどちらも可）。
        # 不正な値が混じっていた場合はログで警告しつつ、その値だけ無視する
        # （Web Dashboard自体の起動を止めない）。
        _validated_networks = []
        for ip_str in allowed_ips:
            try:
                _validated_networks.append(ipaddress.ip_network(ip_str, strict=False))
            except ValueError:
                logger.warning(
                    f"WEB_DASHBOARD_ALLOWED_IPS: 不正なIP/CIDR表記のためスキップします: {ip_str!r}"
                )

        @web.middleware
        async def ip_allowlist_middleware(request, handler):
            """
            WEB_DASHBOARD_ALLOWED_IPS が設定されている場合、リストにない
            送信元IPからのアクセスを 403 で拒否する。未設定（空リスト）の
            場合は何もチェックしない（WEB_DASHBOARD_HOST 側の制御のみに委ねる）。
            """
            if _validated_networks:
                peername = request.transport.get_extra_info("peername") if request.transport else None
                client_ip_str = peername[0] if peername else request.remote
                try:
                    client_ip = ipaddress.ip_address(client_ip_str)
                except (ValueError, TypeError):
                    logger.warning(f"Web Dashboard: 送信元IPの解析に失敗しました ({client_ip_str!r}) → 拒否します")
                    return web.json_response({"error": "forbidden"}, status=403)

                if not any(client_ip in net for net in _validated_networks):
                    logger.warning(f"Web Dashboard: 許可されていないIPからのアクセスを拒否しました: {client_ip_str}")
                    return web.json_response({"error": "forbidden"}, status=403)

            return await handler(request)

        async def status_handler(request):
            """GET /status - ステータス JSON を返す（拡充版）"""
            try:
                now = datetime.now()
                uptime_seconds = int(time.time() - self._start_time)
                uptime_str = self._format_uptime(uptime_seconds)

                last_recv = self._merged_last_recv()
                recv_count = self._merged_recv_count()

                # システムリソース（リクエスト時のみ計測、共通ヘルパー経由でノンブロッキング）
                system_info: dict = {}
                metrics = self._sample_resource_usage()
                if metrics is not None:
                    mem = metrics["memory_mb"]
                    mem_total = metrics["memory_total_mb"]
                    disk = metrics["disk"]
                    system_info = {
                        "cpu_percent": metrics["cpu_percent"],
                        "memory_mb": round(mem, 1),
                        "memory_total_mb": round(mem_total, 1),
                        "memory_percent": round(mem / mem_total * 100, 1),
                        "disk_percent": disk.percent,
                        "disk_free_gb": round(disk.free / 1024**3, 2),
                    }

                def _api_info(key: str) -> dict:
                    t = last_recv.get(key)
                    return {
                        "last_recv_time": t.isoformat() if t else None,
                        "recv_count": recv_count.get(key, 0),
                    }

                # EEW 状態（EewCog から取得）
                now_mono = time.monotonic()
                wolfx_hb = self._eew_attr("_wolfx_last_heartbeat")
                if wolfx_hb is None:
                    wolfx_ws_status = "connecting"
                    wolfx_hb_elapsed = None
                else:
                    wolfx_hb_elapsed = round(now_mono - wolfx_hb, 2)
                    wolfx_ws_status = "online" if wolfx_hb_elapsed < WOLFX_HEARTBEAT_TIMEOUT else "timeout"

                eew_info = {
                    "wolfx": {
                        "ws_status": wolfx_ws_status,
                        "heartbeat_elapsed_sec": wolfx_hb_elapsed,
                        "heartbeat_timeout_sec": WOLFX_HEARTBEAT_TIMEOUT,
                        "last_eew_id": self._eew_attr("last_eew_event_id"),
                        **_api_info("wolfx"),
                    },
                    "p2p_eew": {
                        **_api_info("p2p_eew"),
                    },
                }

                def _loop_status(t) -> str:
                    if t is None: return "not_started"
                    if t.is_running(): return "running"
                    if t.failed(): return "error"
                    return "stopped"

                def _task_status(t) -> str:
                    if t is None: return "not_started"
                    if not t.done(): return "running"
                    if t.cancelled(): return "cancelled"
                    try:
                        t.exception()
                    except Exception:
                        return "error"
                    return "done"

                p2p_hub_stats = self._p2p_hub_stats()
                tasks_info = {
                    "p2p_ws_hub": "running" if p2p_hub_stats is not None else "not_started",
                    "fetch_tsunami_observation": _loop_status(self._tsunami_attr("fetch_tsunami_observation")),
                    "fetch_usgs_quake": _loop_status(self._usgs_attr("fetch_usgs_quake")) if USGS_ENABLED else "disabled",
                    "speech_worker_audio": _task_status(self._audio_attr("speech_task")),
                    "mp3_worker_audio": _task_status(self._audio_attr("mp3_task")),
                    "volcano_poller": _task_status(self._volcano_attr("volcano_task")),
                    "eruption_poller": _task_status(self._volcano_attr("eruption_task")),
                    "warning_poller": _task_status(self._volcano_attr("warning_task")),
                    "fetch_long_period": _loop_status(self._other_attr("fetch_long_period")),
                    "fetch_quake_advisory": _loop_status(self._other_attr("fetch_quake_advisory")),
                    "kyoshin_monitor": _task_status(self._kyoshin_attr("_monitor_task")) if ENABLE_KYOSHIN else "disabled",
                    "vibration_monitor_loop": _task_status(self._eew_attr("vibration_monitor_task")) if ENABLE_KYOSHIN else "disabled",
                }
                if p2p_hub_stats is not None:
                    tasks_info["p2p_ws_hub_recv_count"] = p2p_hub_stats.get("recv_count", {})

                usgs_info: dict = {"enabled": USGS_ENABLED}
                if USGS_ENABLED:
                    last_usgs_ids_dict = self._usgs_attr("last_usgs_ids", {}) or {}
                    usgs_last_ids = list(last_usgs_ids_dict.keys())[-5:] if last_usgs_ids_dict else []
                    usgs_info.update({
                        "magnitude_min": USGS_MAGNITUDE_MIN,
                        "fetch_interval_sec": USGS_FETCH_INTERVAL,
                        "region": {
                            "lat": [USGS_REGION_LAT_MIN, USGS_REGION_LAT_MAX],
                            "lon": [USGS_REGION_LON_MIN, USGS_REGION_LON_MAX],
                        },
                        "last_event_ids": usgs_last_ids,
                        **_api_info("usgs"),
                    })

                last_volcano_event_id = self._volcano_attr("_last_volcano_event_id")
                last_volcano_recv_time = self._volcano_attr("_last_volcano_recv_time")
                volcano_recv_count = self._volcano_attr("_volcano_recv_count", 0)
                volcano_task = self._volcano_attr("volcano_task")

                status_data = {
                    "status": "online",
                    "timestamp": now.isoformat(),
                    "bot_user": str(self.bot.user),
                    "uptime": uptime_str,
                    "uptime_seconds": uptime_seconds,
                    "ping_ms": round(self.bot.latency * 1000),
                    "system": system_info,
                    "eew": eew_info,
                    "api_status": {
                        "wolfx": last_recv.get("wolfx").isoformat() if last_recv.get("wolfx") else None,
                        "p2p_eew": last_recv.get("p2p_eew").isoformat() if last_recv.get("p2p_eew") else None,
                        "quake": last_recv.get("quake").isoformat() if last_recv.get("quake") else None,
                        "tsunami": last_recv.get("tsunami").isoformat() if last_recv.get("tsunami") else None,
                        "jishin_kanchi": last_recv.get("jishin_kanchi").isoformat() if last_recv.get("jishin_kanchi") else None,
                        "volcano": last_recv.get("volcano").isoformat() if last_recv.get("volcano") else None,
                    },
                    "recv_count": {
                        "wolfx": recv_count.get("wolfx", 0),
                        "p2p_eew": recv_count.get("p2p_eew", 0),
                        "quake": recv_count.get("quake", 0),
                        "tsunami": recv_count.get("tsunami", 0),
                        "jishin_kanchi": recv_count.get("jishin_kanchi", 0),
                        "long_period": recv_count.get("long_period", 0),
                        "tsunami_obs": recv_count.get("tsunami_obs", 0),
                        "volcano": recv_count.get("volcano", 0),
                        "eruption": recv_count.get("eruption", 0),
                        "warning": recv_count.get("warning", 0),
                        "kyoshin": recv_count.get("kyoshin", 0),
                        "long_period_monitor": recv_count.get("long_period_monitor", 0),
                        "usgs": recv_count.get("usgs", 0),
                    },
                    "monitoring": {
                        "quake": _api_info("quake"),
                        "tsunami": _api_info("tsunami"),
                        "jishin_kanchi": _api_info("jishin_kanchi"),
                        "long_period": _api_info("long_period"),
                        "tsunami_obs": _api_info("tsunami_obs"),
                        "quake_advisory": _api_info("quake_advisory"),
                        "volcano": {
                            "last_event_id": last_volcano_event_id,
                            "polling_status": _task_status(volcano_task),
                            **_api_info("volcano"),
                            "total_recv_count": volcano_recv_count,
                        },
                        "eruption": {
                            "last_event_id": self._volcano_attr("_last_eruption_id"),
                            "polling_status": _task_status(self._volcano_attr("eruption_task")),
                            **_api_info("eruption"),
                        },
                        "warning": {
                            "last_event_id": self._volcano_attr("_last_warning_id"),
                            "polling_status": _task_status(self._volcano_attr("warning_task")),
                            **_api_info("warning"),
                        },
                        "usgs": usgs_info,
                        "kyoshin": {**self._kyoshin_status_dict(), **_api_info("kyoshin")},
                        "long_period_monitor": {
                            "active": self._eew_attr("monitored_event_id") is not None,
                            **_api_info("long_period_monitor"),
                        },
                    },
                    "tasks": tasks_info,
                    "delivery": get_delivery_stats(window_hours=24.0),
                    # 後方互換フィールド
                    "last_eew": {
                        "event_id": self._eew_attr("last_eew_event_id"),
                        "timestamp": last_recv.get("wolfx").isoformat() if last_recv.get("wolfx") else None,
                    },
                    "volcano_monitoring": {
                        "last_event_id": last_volcano_event_id,
                        "last_recv_time": last_volcano_recv_time.isoformat() if last_volcano_recv_time else None,
                        "polling_status": "active" if volcano_task and not volcano_task.done() else "inactive",
                        "total_recv_count": volcano_recv_count,
                    },
                    "memory_usage_mb": system_info.get("memory_mb", 0),
                }
                return web.json_response(status_data)
            except Exception as e:
                logger.error(f"Web ダッシュボード /status エラー: {e}")
                return web.json_response({"error": str(e)}, status=500)

        async def health_handler(request):
            """GET /health - ヘルスチェック"""
            return web.json_response({"status": "online"})

        async def health_full_handler(request):
            """GET /health/full - 詳細ヘルスチェック（各 API の疎通確認）"""
            try:
                result = await self.check_api_status()
                return web.json_response(result)
            except Exception as e:
                logger.error(f"/health/full エラー: {e}", exc_info=True)
                return web.json_response(
                    {"status": "error", "error": str(e)},
                    status=500
                )

        async def history_handler(request):
            """
            GET /status/history - グラフ・履歴表示用のスナップショット履歴。
            status_history_recorder が STATUS_HISTORY_INTERVAL 秒ごとに
            記録したデータ（メモリ上のリングバッファ、Bot再起動でリセット）
            をそのままJSON配列として返す。

            【CSVエクスポート】
            クエリパラメータ ?format=csv を付けると、同じデータを
            text/csv（Content-Disposition: attachment付き）で返す。
            長期障害調査時にExcel等の外部ツールへ取り込みやすくするため
            （2026-08 追加）。JSON配列に含まれる recv_count は各キーを
            "recv_<key>" 列に展開してフラット化する。
            """
            history_list = list(self.status_history)

            if request.query.get("format", "").lower() == "csv":
                return self._history_csv_response(history_list)

            return web.json_response({
                "interval_sec": STATUS_HISTORY_INTERVAL,
                "max_points": STATUS_HISTORY_MAXLEN,
                "count": len(history_list),
                "history": history_list,
            })

        async def notifications_handler(request):
            """
            GET /status/notifications - 直近の通知履歴。

            各Cogのnotify_*メソッドが core.notification_log.record_notification
            で記録した「実際にDiscordへ送信した通知」の一覧を新しい順に
            返す（メモリ上のリングバッファ、Bot再起動でリセット）。
            障害調査時に「何が通知されたか」を素早く確認できるようにする
            ためのエンドポイント（2026-08 追加）。

            クエリパラメータ ?limit=N で件数を絞れる（省略時は全件、
            最大でも core.notification_log.NOTIFICATION_LOG_MAXLEN 件）。
            """
            limit_raw = request.query.get("limit")
            limit = None
            if limit_raw is not None:
                try:
                    limit = max(1, int(limit_raw))
                except ValueError:
                    pass
            notifications = get_recent_notifications(limit)
            return web.json_response({
                "count": len(notifications),
                "notifications": notifications,
            })

        async def dashboard_handler(request):
            """
            GET /dashboard - システムリソース・受信件数の推移をグラフ表示
            するHTMLページ。外部ライブラリ（Chart.js等）はCDNから読み込み、
            Bot側は /status/history のデータをそのまま描画するのみで、
            グラフ計算処理自体はブラウザ側で行う（Raspberry Pi側の
            負荷を増やさない設計）。
            """
            html = _DASHBOARD_HTML
            return web.Response(text=html, content_type="text/html")

        try:
            self._web_app = web.Application(middlewares=[ip_allowlist_middleware])
            self._web_app.router.add_get("/status", status_handler)
            self._web_app.router.add_get("/status/history", history_handler)
            self._web_app.router.add_get("/status/notifications", notifications_handler)
            self._web_app.router.add_get("/dashboard", dashboard_handler)
            self._web_app.router.add_get("/health", health_handler)
            self._web_app.router.add_get("/health/full", health_full_handler)

            self._web_runner = web.AppRunner(self._web_app)
            await self._web_runner.setup()
            site = web.TCPSite(self._web_runner, host, port)
            await site.start()

            if allowed_ips:
                ip_note = f"許可IP: {', '.join(allowed_ips)}"
            else:
                ip_note = "IP制限なし（WEB_DASHBOARD_HOSTのバインド範囲のみで制御）"
            logger.info(
                f"Web ダッシュボード起動: http://{host}:{port}/status ({ip_note})"
            )
            if host == "0.0.0.0" and not allowed_ips:
                logger.warning(
                    "Web Dashboard が 0.0.0.0（全アドレス）にバインドされ、"
                    "かつ WEB_DASHBOARD_ALLOWED_IPS も未設定です。"
                    "ネットワーク環境によっては外部から誰でもアクセスできる状態です。"
                    "必要に応じて WEB_DASHBOARD_ALLOWED_IPS の設定を推奨します。"
                )
        except OSError as e:
            # [Errno 98] Address already in use 等。ポートが既に使用中の
            # ケースが大半で、多くは他プロセス（本番のsystemdサービスや
            # 前回終了しきれなかったプロセス）との衝突が原因。
            logger.error(
                f"Web ダッシュボード起動失敗（ポート {port} が使用中の可能性）: {e}"
            )
        except Exception as e:
            logger.error(f"Web ダッシュボード起動失敗: {e}")

    def _format_uptime(self, seconds: int) -> str:
        """秒数を 'Xd XXh XXm' 形式に変換"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        return f"{days}d {hours}h {minutes}m"

    # ===============================
    # リソース監視
    # ===============================
    async def resource_monitor(self) -> None:
        """1時間ごとにリソース使用率をログに記録"""
        if self._status_psutil_proc is None:
            logger.warning("psutil がインストールされていません。リソース監視は無効です。")
            return

        if not RESOURCE_MONITORING_ENABLED:
            logger.info("リソース監視は無効です。")
            return

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(RESOURCE_CHECK_INTERVAL)

                try:
                    # 【2026-08修正】以前は毎回 psutil.Process() を新規生成し
                    # cpu_percent(interval=1) で1秒間 *同期的に* ブロック
                    # していた（1時間に1回とはいえ、その1秒間はBot全体の
                    # 応答が完全に停止していた）。_sample_resource_usage()
                    # 経由のノンブロッキング計測に統一する。
                    metrics = self._sample_resource_usage()
                    if metrics is None:
                        logger.error("リソース情報取得エラー: psutil計測に失敗しました")
                        continue

                    cpu_percent = metrics["cpu_percent"]
                    mem_mb = metrics["memory_mb"]
                    disk_info = metrics["disk"]
                    disk_percent = disk_info.percent
                    disk_free_gb = disk_info.free / 1024 / 1024 / 1024

                    log_msg = (
                        f"リソース監視 - CPU: {cpu_percent:.1f}%, "
                        f"MEM: {mem_mb:.1f}MB, "
                        f"DISK: {disk_percent}% (空き容量: {disk_free_gb:.1f}GB)"
                    )

                    if disk_percent >= DISK_ERROR_THRESHOLD:
                        logger.error(f"[ERROR] {log_msg} - ディスク使用率が {DISK_ERROR_THRESHOLD}% を超えています")
                    elif disk_percent >= DISK_WARNING_THRESHOLD:
                        logger.warning(f"[WARN] {log_msg} - ディスク使用率が {DISK_WARNING_THRESHOLD}% を超えています")
                    else:
                        logger.info(f"{log_msg}")

                except Exception as e:
                    logger.error(f"リソース情報取得エラー: {e}")

            except asyncio.CancelledError:
                logger.info("resource_monitor が停止しました")
                break
            except Exception as e:
                logger.error(f"resource_monitor エラー: {e}")
                await asyncio.sleep(60)

    async def status_history_recorder(self) -> None:
        """
        Web Dashboard のグラフ・履歴表示（/status/history）用に、
        STATUS_HISTORY_INTERVAL 秒ごとにシステムリソース・各種受信件数の
        スナップショットを self.status_history（リングバッファ）へ記録する。

        resource_monitor（ログ出力専用、デフォルト1時間間隔）とは独立した
        別タスクとして動かす。記録内容はメモリ上にのみ保持し、Bot再起動で
        リセットされる（外部DB等は使用しない軽量設計）。
        """
        if self._status_psutil_proc is None:
            logger.warning("psutil がインストールされていません。履歴記録は無効です。")
            return

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(STATUS_HISTORY_INTERVAL)

                try:
                    # 【2026-08修正】計測ロジックを _sample_resource_usage()
                    # に統一（他2箇所と方式を揃え、psutil呼び出しを一元化）
                    metrics = self._sample_resource_usage()
                    if metrics is None:
                        logger.debug("status_history_recorder: psutil計測に失敗したためスキップします")
                        continue
                    cpu_percent = metrics["cpu_percent"]
                    mem_mb = metrics["memory_mb"]
                    disk_info = metrics["disk"]

                    recv_count = self._merged_recv_count()
                    p2p_hub_stats = self._p2p_hub_stats()
                    p2p_recv = p2p_hub_stats.get("recv_count", {}) if p2p_hub_stats else {}

                    snapshot = {
                        "timestamp": datetime.now().isoformat(),
                        "cpu_percent": round(cpu_percent, 1),
                        "memory_mb": round(mem_mb, 1),
                        "disk_percent": disk_info.percent,
                        "recv_count": {
                            "wolfx": recv_count.get("wolfx", 0),
                            "p2p_eew": p2p_recv.get("eew", recv_count.get("p2p_eew", 0)),
                            "quake": p2p_recv.get("quake", recv_count.get("quake", 0)),
                            "tsunami": p2p_recv.get("tsunami", recv_count.get("tsunami", 0)),
                            "usgs": recv_count.get("usgs", 0),
                            "volcano": recv_count.get("volcano", 0),
                        },
                    }
                    self.status_history.append(snapshot)
                except Exception as e:
                    logger.debug(f"status_history_recorder: スナップショット記録エラー: {e}")

            except asyncio.CancelledError:
                logger.info("status_history_recorder が停止しました")
                break
            except Exception as e:
                logger.error(f"status_history_recorder エラー: {e}")
                await asyncio.sleep(60)

    # ===============================
    # ヘルスチェック
    # ===============================
    async def check_api_status(self) -> dict:
        """各 API（Wolfx, JMA, P2P, USGS）の疎通確認"""
        if (self.health_check_cache and
            self.last_health_check_time and
            (datetime.now() - self.last_health_check_time).total_seconds() < HEALTH_CHECK_CACHE_TTL):
            return self.health_check_cache

        result = {
            "overall_status": "healthy",
            "last_check_time": datetime.now().isoformat(),
            "api_status": {
                "wolfx": {"ok": False, "latency_ms": None, "error": None},
                "jma": {"ok": False, "latency_ms": None, "error": None},
                "p2p": {"ok": False, "latency_ms": None, "error": None},
                "usgs": {"ok": False, "latency_ms": None, "error": None},
            }
        }

        try:
            # Wolfx WebSocket ping（TCP 接続確認）
            try:
                start = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(HEALTH_CHECK_TIMEOUT)
                await asyncio.wait_for(
                    asyncio.to_thread(sock.connect, ('api.wolfx.jp', 443)),
                    timeout=HEALTH_CHECK_TIMEOUT
                )
                sock.close()
                latency = (time.time() - start) * 1000
                result["api_status"]["wolfx"] = {
                    "ok": True, "latency_ms": round(latency, 1), "error": None,
                }
            except Exception as e:
                result["api_status"]["wolfx"]["error"] = str(type(e).__name__)

            # JMA API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://www.jma.go.jp/bosai/common/const/area.json',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["jma"] = {
                            "ok": True, "latency_ms": round(latency, 1), "error": None,
                        }
            except Exception as e:
                result["api_status"]["jma"]["error"] = str(type(e).__name__)

            # P2P 地震情報 API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://api.p2pquake.net/v2/status',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["p2p"] = {
                            "ok": True, "latency_ms": round(latency, 1), "error": None,
                        }
            except Exception as e:
                result["api_status"]["p2p"]["error"] = str(type(e).__name__)

            # USGS API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["usgs"] = {
                            "ok": True, "latency_ms": round(latency, 1), "error": None,
                        }
            except Exception as e:
                result["api_status"]["usgs"]["error"] = str(type(e).__name__)

            all_ok = all(api["ok"] for api in result["api_status"].values())
            result["overall_status"] = "healthy" if all_ok else "degraded"

        except Exception as e:
            logger.error(f"ヘルスチェック中にエラー: {e}", exc_info=True)
            result["overall_status"] = "unhealthy"

        self.health_check_cache = result
        self.last_health_check_time = datetime.now()

        return result

    # ===============================
    # エラー自動通知
    # ===============================
    async def notify_error(self, error_msg: str, error_type: str = "Unknown") -> None:
        """エラーを管理者チャンネルに通知（重複防止付き）。他Cogからも呼べる。"""
        if not self.admin_channel:
            return

        try:
            error_hash = hash(f"{error_type}:{error_msg[:100]}")

            current_time = datetime.now()
            if error_hash in self.error_notification_cache:
                last_notified = self.error_notification_cache[error_hash]
                if (current_time - last_notified).total_seconds() < ERROR_NOTIFICATION_TTL:
                    logger.debug(f"エラー通知をスキップ（重複防止）: {error_type}")
                    return

            self.error_notification_cache[error_hash] = current_time

            self.error_count_today += 1
            if error_type not in self.daily_error_summary:
                self.daily_error_summary[error_type] = 0
            self.daily_error_summary[error_type] += 1

            embed = discord.Embed(
                title="エラー発生",
                description=f"**タイプ**: {error_type}\n**メッセージ**: {error_msg[:500]}",
                color=discord.Color.red(),
                timestamp=current_time
            )
            embed.add_field(name="発生時刻", value=current_time.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
            embed.add_field(name="本日のエラー件数", value=str(self.error_count_today), inline=True)
            embed.add_field(name="エラータイプ別", value=str(self.daily_error_summary), inline=False)
            embed.set_footer(text="QTL_Bot エラー監視")

            await self.admin_channel.send(embed=embed)
            logger.info(f"エラー通知を送信しました: {error_type}")

        except Exception as e:
            logger.error(f"エラー通知の送信に失敗: {e}", exc_info=True)

    async def error_summary_worker(self) -> None:
        """毎日 00:00 に日次エラーサマリーを生成・送信"""
        while not self.bot.is_closed():
            try:
                now = datetime.now()
                tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                wait_seconds = (tomorrow - now).total_seconds()

                logger.debug(f"日次エラーサマリー: {wait_seconds:.0f}秒後に実行")
                await asyncio.sleep(wait_seconds)

                if not self.admin_channel or self.error_count_today == 0:
                    logger.debug("エラーサマリー: エラーがないため送信をスキップ")
                    self.error_count_today = 0
                    self.daily_error_summary = {}
                    continue

                summary_text = "\n".join([
                    f"  • {etype}: {count} 件"
                    for etype, count in sorted(self.daily_error_summary.items(), key=lambda x: x[1], reverse=True)
                ])

                embed = discord.Embed(
                    title="日次エラーサマリー",
                    description=f"**集計日**: {datetime.now().strftime('%Y-%m-%d')}\n**総エラー数**: {self.error_count_today} 件",
                    color=discord.Color.orange(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="エラータイプ別集計", value=summary_text or "なし", inline=False)
                embed.set_footer(text="QTL_Bot エラー監視")

                await self.admin_channel.send(embed=embed)
                logger.info(f"日次エラーサマリーを送信しました（{self.error_count_today}件）")

                self.error_count_today = 0
                self.daily_error_summary = {}

            except asyncio.CancelledError:
                logger.info("error_summary_worker が停止しました")
                break
            except Exception as e:
                logger.error(f"error_summary_worker エラー: {e}", exc_info=True)
                await asyncio.sleep(60)

    # ===============================
    # 週間/月間ダイジェスト（2026-08〜）
    # ===============================
    def _compute_next_digest_time(self, now: datetime) -> datetime:
        """
        次回ダイジェスト投稿時刻を計算する。

        DIGEST_INTERVAL="monthly" の場合は「翌月1日のDIGEST_HOUR時」、
        それ以外（"weekly"扱い）は「次のDIGEST_WEEKDAY曜日の
        DIGEST_HOUR時」を返す。
        """
        if DIGEST_INTERVAL == "monthly":
            if now.month == 12:
                return now.replace(
                    year=now.year + 1, month=1, day=1,
                    hour=DIGEST_HOUR, minute=0, second=0, microsecond=0,
                )
            return now.replace(
                month=now.month + 1, day=1,
                hour=DIGEST_HOUR, minute=0, second=0, microsecond=0,
            )

        # weekly（DIGEST_INTERVALが不明な値の場合もこちらにフォールバック）
        days_ahead = (DIGEST_WEEKDAY - now.weekday()) % 7
        candidate = now.replace(
            hour=DIGEST_HOUR, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    # recv_count のキー → 表示ラベル対応表。
    # SystemCog._merged_recv_count() が返すキー名（内部識別子）を、
    # ダイジェストEmbedでの表示用の日本語ラベルに変換する。
    _DIGEST_LABEL_MAP = {
        "wolfx": "EEW",
        "p2p_eew": "EEW（P2Pフォールバック）",
        "quake": "地震情報",
        "tsunami": "津波情報",
        "long_period": "長周期地震動",
        "tsunami_obs": "津波観測情報",
        "volcano": "火山情報",
        "eruption": "噴火速報",
        "warning": "噴火警報",
        "usgs": "USGS地震情報",
    }

    async def _send_digest(self) -> None:
        """
        ダイジェストEmbedを組み立てて送信する。前回実行時点の累積
        受信カウントとの差分を「この期間内の件数」として集計する。

        【なぜ累積カウンタの差分方式なのか】
        core.notification_log は最大50件のリングバッファのため、
        活発な期間（大きな地震が続いた週等）には集計対象の期間中に
        古い記録が上書きされて消えてしまい、正確な週間/月間集計には
        使えない。各Cogが保持する「起動からの累積受信カウント」
        （_merged_recv_count()）は上限がないため、スナップショットの
        差分を取ることで正確な期間内カウントが得られる。

        【Bot再起動を挟んだ場合の注意】
        累積カウンタはBot再起動でゼロにリセットされる。再起動後の
        カウントが前回スナップショットより小さくなった場合（＝
        再起動があったと推定できる場合）、負の差分をそのまま使うと
        不自然な値になるため、0でクランプする（実態よりは少なく
        出るが、マイナス表示になるよりは健全）。
        """
        channel = self.bot.get_channel(DIGEST_CHANNEL_ID) or self.bot.get_channel(CHANNEL_ID)
        if not channel:
            logger.warning("digest_worker: 送信先チャンネルが見つかりません")
            return

        now = datetime.now()
        now_snapshot = self._merged_recv_count()
        prev_snapshot = self._digest_last_recv_count_snapshot or {}

        deltas: dict[str, int] = {}
        for key, current in now_snapshot.items():
            prev = prev_snapshot.get(key, 0)
            deltas[key] = max(current - prev, 0)

        total = sum(deltas.values())
        period_label = "先月" if DIGEST_INTERVAL == "monthly" else "先週"

        lines = []
        for key, label in self._DIGEST_LABEL_MAP.items():
            count = deltas.get(key, 0)
            if count > 0:
                lines.append(f"・{label}: {count}件")

        # 配信成功率（このダイジェスト期間にできるだけ近いウィンドウで取得）。
        # core.delivery_stats はリングバッファ上限があるため、活発な期間は
        # 実際の件数より少なく出ることがある点に注意（正確な累積値では
        # なく「直近の傾向」として参考値扱いとする）。
        window_hours = 24 * 30 if DIGEST_INTERVAL == "monthly" else 24 * 7
        delivery = get_delivery_stats(window_hours=window_hours)

        if self._digest_last_run_at is not None:
            period_desc = (
                f"{self._digest_last_run_at.strftime('%Y-%m-%d')} 〜 "
                f"{now.strftime('%Y-%m-%d')}"
            )
        else:
            period_desc = f"〜 {now.strftime('%Y-%m-%d')}（初回集計）"

        embed = discord.Embed(
            title=f"📊 {period_label}の地震活動まとめ",
            description=(
                f"**通知件数合計: {total}件**" if total > 0
                else "この期間の通知はありませんでした。"
            ),
            color=discord.Color.blue(),
            timestamp=now,
        )
        if lines:
            embed.add_field(name="種別ごとの件数", value="\n".join(lines), inline=False)
        if delivery["total"] > 0:
            embed.add_field(
                name="配信成功率（参考値）",
                value=(
                    f"{delivery['success_rate']:.1f}% "
                    f"({delivery['success']}/{delivery['total']}件)"
                ),
                inline=False,
            )
        embed.set_footer(text=f"集計期間: {period_desc}")

        try:
            await channel.send(embed=embed)
            logger.info(f"ダイジェストを送信しました（期間={period_label}, 合計{total}件）")
        except Exception as e:
            logger.error(f"digest_worker: 送信に失敗しました: {e}", exc_info=True)
            # 送信に失敗した場合でもスナップショットは更新する
            # （次回また同じ差分を再送しようとして二重計上になるより、
            # 1回分の集計を諦める方が実害が小さいという判断）。

        self._digest_last_recv_count_snapshot = now_snapshot
        self._digest_last_run_at = now

    async def digest_worker(self) -> None:
        """
        DIGEST_INTERVAL（weekly/monthly）に応じて定期的にダイジェストを
        送信するバックグラウンドタスク。DIGEST_ENABLED=false の場合は
        何もせず即座に終了する。
        """
        if not DIGEST_ENABLED:
            logger.debug("digest_worker: DIGEST_ENABLED=false のため無効です")
            return

        while not self.bot.is_closed():
            try:
                now = datetime.now()
                next_run = self._compute_next_digest_time(now)
                wait_seconds = (next_run - now).total_seconds()

                logger.debug(
                    f"digest_worker: 次回実行予定 {next_run.isoformat()} "
                    f"（{wait_seconds:.0f}秒後）"
                )
                await asyncio.sleep(max(wait_seconds, 1.0))
                await self._send_digest()

            except asyncio.CancelledError:
                logger.info("digest_worker が停止しました")
                break
            except Exception as e:
                logger.error(f"digest_worker エラー: {e}", exc_info=True)
                await asyncio.sleep(60)
