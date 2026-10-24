// /dashboard の描画（HTML 文字列は組み立てず、DOM API で作る。js/common.js の方針を参照）。
(function () {
  'use strict';
  var h = QTL.h, T = window.QTLTheme;

  // 通知種別ごとの表示色（notif-kind の背景色。core/notification_log.py の kind 文字列と対応）。
  // 未知の種別は common.css の既定色にフォールバックする。
  var KIND_COLORS = {
    'EEW': '#f38ba8', '地震情報': '#94e2d5', '津波情報': '#89dceb',
    '津波観測情報': '#74c7ec', '津波予報': '#89dceb', '震源要素更新': '#b4befe',
    '南海トラフ': '#eba0ac', '火山情報': '#fab387', '噴火速報': '#f9e2af',
    '噴火警報': '#f38ba8', 'USGS': '#cba6f7', '長周期地震動': '#a6e3a1',
    '気象庁その他': '#9399b2',
  };
  var RECV_KEYS = ['wolfx', 'p2p_eew', 'quake', 'tsunami', 'jishin_kanchi', 'usgs', 'volcano', 'kyoshin'];

  var charts = {};            // canvas id → Chart（再読み込みのたびに作り直さず、データだけ更新する）
  var notifItemsCache = [];   // フィルタ再描画用に直近取得分を保持

  function color(key) { return T.cssVar('--c-' + key) || '#888888'; }
  function $(id) { return document.getElementById(id); }
  function pad2(n) { return String(n).padStart(2, '0'); }

  // ── グラフ ──
  function chartOptions(yLabel) {
    var muted = T.cssVar('--text-muted'), grid = T.cssVar('--chart-grid');
    return {
      responsive: true,
      animation: false,
      scales: {
        x: { ticks: { color: muted, maxTicksLimit: 8 }, grid: { color: grid } },
        y: { ticks: { color: muted }, grid: { color: grid }, title: { display: true, text: yLabel, color: muted } },
      },
      plugins: { legend: { labels: { color: T.cssVar('--text') } } },
    };
  }
  function datasetFor(spec, labelCount) {
    return { label: spec.label, data: spec.data, borderColor: color(spec.colorKey), tension: 0.2, pointRadius: 0, _key: spec.colorKey };
  }
  function upsertChart(canvasId, labels, specs, yLabel) {
    var chart = charts[canvasId];
    if (!chart) {
      charts[canvasId] = new Chart($(canvasId), {
        type: 'line',
        data: { labels: labels, datasets: specs.map(datasetFor) },
        options: chartOptions(yLabel),
      });
      charts[canvasId]._yLabel = yLabel;
      return;
    }
    chart.data.labels = labels;
    specs.forEach(function (spec, i) { chart.data.datasets[i].data = spec.data; });
    chart.update('none');
  }
  // テーマが変わったとき、既存のグラフの配色だけを入れ替える
  function recolorCharts() {
    Object.keys(charts).forEach(function (id) {
      var chart = charts[id], opts = chartOptions(chart._yLabel);
      chart.options.scales = opts.scales;
      chart.options.plugins.legend.labels.color = T.cssVar('--text');
      chart.data.datasets.forEach(function (d) { d.borderColor = color(d._key); });
      chart.update('none');
    });
  }

  // ── サマリー ──
  function dotText(dotClass, text) {
    return [h('span', { class: 'dot ' + dotClass }), String(text)];
  }
  async function loadSummary() {
    if (document.hidden) return;
    try {
      var data = await QTL.fetchJson('/status');
      var ok = data.status === 'online';
      QTL.setContent($('sumStatus'), h('span', null, dotText(ok ? 'dot-green' : 'dot-red', data.bot_user || (ok ? 'オンライン' : 'オフライン'))));
      $('sumUptime').textContent = data.uptime || '-';
      $('sumPing').textContent = data.ping_ms != null ? data.ping_ms + ' ms' : '-';

      var wolfx = (data.eew && data.eew.wolfx) || {};
      var ws = wolfx.ws_status || 'unknown';
      var wsDot = ws === 'online' ? 'dot-green' : (ws === 'connecting' ? 'dot-yellow' : 'dot-red');
      QTL.setContent($('sumWolfx'), h('span', null, dotText(wsDot, ws)));

      var sys = data.system || {};
      if (sys.cpu_percent != null) $('sumResource').textContent = Number(sys.cpu_percent).toFixed(1) + '% / ' + sys.memory_mb + 'MB';
      if (sys.disk_percent != null) {
        var p = Number(sys.disk_percent);
        QTL.setContent($('sumDisk'), h('span', null, dotText(p >= 90 ? 'dot-red' : (p >= 80 ? 'dot-yellow' : 'dot-green'), p + '%')));
      }
    } catch (e) {
      QTL.setContent($('sumStatus'), h('span', null, dotText('dot-gray', '取得失敗')));
    }
  }

  // ── 履歴グラフ ──
  async function loadCharts() {
    if (document.hidden) return;
    try {
      var data = await QTL.fetchJson('/status/history');
      var history = data.history || [];
      if (history.length === 0) {
        $('statusLine').textContent = 'まだ履歴データがありません（記録間隔: ' + data.interval_sec + '秒。しばらく待ってから再読み込みしてください）';
        return;
      }
      var labels = history.map(function (r) {
        var d = new Date(r.timestamp);
        return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
      });
      upsertChart('cpuChart', labels, [{ label: 'CPU %', colorKey: 'cpu', data: history.map(function (r) { return r.cpu_percent; }) }], '%');
      upsertChart('memChart', labels, [{ label: 'メモリ MB', colorKey: 'mem', data: history.map(function (r) { return r.memory_mb; }) }], 'MB');
      upsertChart('diskChart', labels, [{ label: 'ディスク %', colorKey: 'disk', data: history.map(function (r) { return r.disk_percent; }) }], '%');
      upsertChart('recvChart', labels, RECV_KEYS.map(function (k) {
        return { label: k, colorKey: k, data: history.map(function (r) { return (r.recv_count || {})[k] || 0; }) };
      }), '件数（累積）');
      $('statusLine').textContent = data.count + '件のスナップショット（記録間隔: ' + data.interval_sec +
        '秒、最大保持: ' + data.max_points + '件） ／ 最終更新: ' + new Date().toLocaleTimeString('ja-JP');
    } catch (e) {
      $('statusLine').textContent = '履歴データの取得に失敗しました: ' + e.message;
    }
  }

  // ── 通知履歴 ──
  function renderNotifTable(items) {
    var el = $('notifContent');
    if (items.length === 0) { QTL.setContent(el, QTL.messageBox('notif-empty', '該当する通知がありません')); return; }
    var head = h('tr', null, [h('th', { text: '時刻' }), h('th', { text: '種別' }), h('th', { text: 'タイトル' }), h('th', { class: 'hide-sm', text: '詳細' })]);
    var rows = items.map(function (n) {
      var d = new Date(n.timestamp);
      var timeStr = (d.getMonth() + 1) + '/' + d.getDate() + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
      var kind = h('span', { class: 'notif-kind', text: String(n.kind) });
      if (KIND_COLORS[n.kind]) kind.style.background = KIND_COLORS[n.kind];
      return h('tr', null, [
        h('td', { class: 'nowrap', text: timeStr }),
        h('td', null, kind),
        h('td', { text: String(n.title == null ? '' : n.title) }),
        h('td', { class: 'hide-sm', text: String(n.detail || '') }),
      ]);
    });
    QTL.setContent(el, h('table', { class: 'notif-table' }, [h('thead', null, head), h('tbody', null, rows)]));
  }
  function populateFilter(items) {
    var select = $('notifFilter'), prev = select.value;
    var kinds = Array.from(new Set(items.map(function (n) { return n.kind; }))).sort();
    QTL.clear(select);
    select.append(h('option', { value: '', text: 'すべての種別' }));
    kinds.forEach(function (k) { select.append(h('option', { value: String(k), text: String(k) })); });
    if (kinds.indexOf(prev) >= 0) select.value = prev;     // 再描画後も選択中のフィルタを維持する
  }
  function applyFilter() {
    var selected = $('notifFilter').value;
    renderNotifTable(selected ? notifItemsCache.filter(function (n) { return n.kind === selected; }) : notifItemsCache);
  }
  async function loadNotifications() {
    if (document.hidden) return;
    try {
      var data = await QTL.fetchJson('/status/notifications?limit=50');
      notifItemsCache = data.notifications || [];
      populateFilter(notifItemsCache);
      applyFilter();
    } catch (e) {
      QTL.setContent($('notifContent'), QTL.messageBox('error', '通知履歴の取得に失敗しました: ' + e.message));
    }
  }

  function loadAll() { loadSummary(); loadCharts(); loadNotifications(); }

  $('notifFilter').addEventListener('change', applyFilter);
  $('refreshBtn').addEventListener('click', loadAll);
  T.onChange(recolorCharts);
  // 非表示のタブでは更新を止め（Raspberry Pi への無駄なアクセスを減らす）、表示に戻ったら即更新する
  document.addEventListener('visibilitychange', function () { if (!document.hidden) loadAll(); });

  loadAll();
  setInterval(loadSummary, 15000);
  setInterval(loadCharts, 60000);
  setInterval(loadNotifications, 30000);
})();
