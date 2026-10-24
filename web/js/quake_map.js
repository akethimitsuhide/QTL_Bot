// /quake_map の描画（HTML 文字列は組み立てず、DOM API で作る。js/common.js の方針を参照）。
(function () {
  'use strict';
  var h = QTL.h, T = window.QTLTheme;
  function $(id) { return document.getElementById(id); }

  // core/constants.py の SHINDO_COLORS / INT_MAP と同じ対応関係をブラウザ側で描画するためのミラー
  // （Bot 側の値を都度 API で渡す設計にはしていない。値を変更した場合は両方を合わせて更新すること）。
  var SHINDO_COLORS = {
    '-1': '#62626B', '0': '#62626B', '10': '#3098BD', '20': '#4CD0A7',
    '30': '#F6CB51', '40': '#FF9939', '45': '#E52A18', '46': '#E52A18',
    '50': '#C31B1B', '55': '#A30A6B', '60': '#86046E', '70': '#54068E',
  };
  var INT_MAP = {
    '-1': '不明', '10': '1', '20': '2', '30': '3', '40': '4',
    '45': '5弱', '46': '推定5弱以上', '50': '5強', '55': '6弱', '60': '6強', '70': '7',
  };
  var TYPE_CONFIG = {
    quake: {
      label: '地震情報', endpoint: '/status/quake_history',
      subtitle: 'P2P地震情報（地震情報）の通知履歴。qtlbot.log（ローテーション含む）から復元しています。この記録形式を導入する前に発生した地震は含まれません。',
      emptyMessage: '該当する地震情報がありません',
    },
    eew: {
      label: 'EEW（緊急地震速報）', endpoint: '/status/eew_history',
      subtitle: '緊急地震速報（Wolfx/P2P由来）の通知履歴。同一の地震は最終報（またはその時点での最新の報）に統合して表示しています。qtlbot.log（ローテーション含む）から復元しています。この記録形式を導入する前のEEWは含まれません。',
      emptyMessage: '該当するEEWがありません',
    },
  };
  var currentType = 'quake';
  var allRecords = [], lastFiltered = [], markers = [];

  // ── 日付変更線（経度±180度）をまたぐ地震の表示 ──
  // 背景地図の GeoJSON は経度 -180〜180度の1枚だけで、Leaflet のタイルと違い自動では東西にコピーされない。
  // そのため経度を「日本を中心に太平洋側へ連続させた座標系」（WRAP_WEST〜WRAP_WEST+360度）に揃え、
  // 陸地は +360度シフトしたコピーも重ねて描く（core/gis_render.py の _pick_lon_shift と同じ考え方）。
  var WRAP_WEST = -30;   // 座標系の西端（大西洋上。欧州・アフリカ西岸を分断しない）
  function normLon(lon) {
    lon = Number(lon);
    while (lon < WRAP_WEST) lon += 360;
    while (lon >= WRAP_WEST + 360) lon -= 360;
    return lon;
  }
  function shiftCoords(c, dx) {
    return typeof c[0] === 'number' ? [c[0] + dx, c[1]] : c.map(function (x) { return shiftCoords(x, dx); });
  }
  function shiftFeatures(features, dx) {
    return features.map(function (f) {
      return { type: 'Feature', properties: {}, geometry: { type: f.geometry.type, coordinates: shiftCoords(f.geometry.coordinates, dx) } };
    });
  }

  // 描画負荷対策として、ベクターは Canvas レンダラーで描画する（SVG より DOM が軽い）。
  var map = L.map('map', {
    preferCanvas: true,
    minZoom: 2,
    maxBounds: [[-85, WRAP_WEST], [85, WRAP_WEST + 360]],
    maxBoundsViscosity: 1.0,
  }).setView([36.5, 138.0], 5);   // 日本全体が収まる初期表示

  // 背景地図は、Bot が配信する世界の国境データ（/status/gis_countries）・日本の細分区域データ
  // （/status/gis_local_areas）を陸地色で塗る簡易ベースマップ（地図タイルサーバーへの外部アクセスなし）。
  // 震源マーカーより必ず背面に描くため、専用ペイン（overlayPane の z-index:400 より低い 350）に置く。
  map.createPane('landFillPane');
  map.getPane('landFillPane').style.zIndex = 350;

  var baseLayers = [];   // [{layers: [L.GeoJSON...], border: '--map-border' | '--map-border-local'}]
  function baseStyle(borderVar) {
    return { color: T.cssVar(borderVar), weight: 1, fillColor: T.cssVar('--map-land'), fillOpacity: 1, interactive: false };
  }
  function loadBaseMapLayer(url, borderVar, wrapCopy) {
    QTL.fetchJson(url).then(function (features) {
      var entry = { layers: [], border: borderVar };
      entry.layers.push(L.geoJSON(features, { pane: 'landFillPane', style: baseStyle(borderVar) }).addTo(map));
      if (wrapCopy) {   // 日付変更線をまたいだ先（東経180度〜）にも陸地を描く +360度コピー
        entry.layers.push(L.geoJSON(shiftFeatures(features, 360), { pane: 'landFillPane', style: baseStyle(borderVar) }).addTo(map));
      }
      baseLayers.push(entry);
    }).catch(function () { /* データ未取得時は #map の背景（海色）のみの地図になる（致命的ではない） */ });
  }
  // 世界の国境（日本を除く簡略版）→ 日本の細分区域の順に重ねる（日本は自前データの方が精密なため上書き）
  loadBaseMapLayer('/status/gis_countries', '--map-border', true);
  loadBaseMapLayer('/status/gis_local_areas', '--map-border-local', false);

  function restyleBaseMap() {
    baseLayers.forEach(function (e) { e.layers.forEach(function (l) { l.setStyle(baseStyle(e.border)); }); });
  }

  function scaleLabel(code) { return INT_MAP[String(code)] || '不明'; }
  function scaleColor(code) { return SHINDO_COLORS[String(code)] || '#62626B'; }

  // マグニチュードに応じた円マーカーの半径(px)。M3.0〜M8.0 を 5px〜22px に線形マッピング（範囲外はクランプ）。
  // 不明（0以下・null 等）は固定の中間値にして、規模を誤解させないようにする。
  function magnitudeRadius(mag) {
    var m = Number(mag);
    if (!m || m <= 0) return 9;
    var clamped = Math.min(Math.max(m, 3.0), 8.0);
    return 5 + (clamped - 3.0) / 5.0 * 17;
  }

  function clearMarkers() { markers.forEach(function (m) { map.removeLayer(m); }); markers = []; }

  function popupNode(r) {
    var lines = [h('b', { text: String(r.hypocenter_name == null ? '' : r.hypocenter_name) }), h('br'), String(r.time_display == null ? '' : r.time_display), h('br'),
      '最大震度: ' + scaleLabel(r.max_scale) +
      (r.magnitude && r.magnitude > 0 ? ' / M' + r.magnitude : '') +
      (r.depth != null && r.depth >= 0 ? ' / 深さ約' + r.depth + 'km' : '')];
    if (currentType === 'eew') {
      lines.push(h('br'), (r.is_final ? '最終報' : '予報中（第' + r.serial + '報）') + (r.is_warn ? ' / 警報' : ' / 予報'));
    }
    return h('div', null, lines);     // 文字列はテキストノードになる（HTML として解釈されない）
  }

  function renderMarkers(records) {
    clearMarkers();
    var pts = [];
    // 最大震度が大きい（同じならマグニチュードが大きい）地震ほど後から描き、重なったとき前面に出す
    var sorted = records.slice().sort(function (a, b) {
      var sa = a.max_scale != null ? a.max_scale : -1, sb = b.max_scale != null ? b.max_scale : -1;
      if (sa !== sb) return sa - sb;
      return (a.magnitude || 0) - (b.magnitude || 0);
    });
    sorted.forEach(function (r) {
      if (r.latitude == null || r.longitude == null) return;
      var c = scaleColor(r.max_scale), lon = normLon(r.longitude);
      var marker = L.circleMarker([r.latitude, lon], {
        radius: magnitudeRadius(r.magnitude), color: c, fillColor: c, fillOpacity: 0.75, weight: 1,
      }).addTo(map);
      marker.bindPopup(popupNode(r));
      markers.push(marker);
      pts.push([r.latitude, lon]);
    });
    if (pts.length > 0) map.fitBounds(pts, { maxZoom: 9, padding: [20, 20] });
  }

  function renderTable(records) {
    var el = $('tableContent');
    $('tableTitle').textContent = '履歴一覧（' + records.length + '件）';
    if (records.length === 0) { QTL.setContent(el, QTL.messageBox('notif-empty', TYPE_CONFIG[currentType].emptyMessage)); return; }
    var eew = currentType === 'eew';
    var head = h('tr', null, [h('th', { text: '発生時刻' }), h('th', { text: '震源地' }), h('th', { text: '最大震度' }),
      h('th', { text: 'M' }), h('th', { class: 'hide-sm', text: '深さ' }), eew ? h('th', { text: '状態' }) : null]);
    var rows = records.map(function (r) {
      var depthText = (r.depth != null && r.depth >= 0) ? (r.depth === 0 ? 'ごく浅い' : r.depth + 'km') : '-';
      var magText = (r.magnitude != null && r.magnitude > 0) ? String(r.magnitude) : '-';
      var badge = h('span', { class: 'scale-badge', text: scaleLabel(r.max_scale) });
      badge.style.background = scaleColor(r.max_scale);
      var row = h('tr', { class: 'clickable' }, [
        h('td', { class: 'nowrap', text: String(r.time_display == null ? '' : r.time_display) }),
        h('td', { text: String(r.hypocenter_name == null ? '' : r.hypocenter_name) }),
        h('td', null, badge),
        h('td', { class: 'nowrap', text: magText }),
        h('td', { class: 'hide-sm nowrap', text: depthText }),
        eew ? h('td', { text: (r.is_final ? '最終報' : '第' + r.serial + '報') + (r.is_warn ? '・警報' : '・予報') }) : null,
      ]);
      if (r.latitude != null && r.longitude != null) {       // 行クリックで地図を該当地点へフォーカスする
        row.addEventListener('click', function () { map.setView([r.latitude, normLon(r.longitude)], 9); });
      }
      return row;
    });
    QTL.setContent(el, h('table', { class: 'notif-table' }, [h('thead', null, head), h('tbody', null, rows)]));
  }

  function populateScaleFilterOptions(records) {
    var select = $('scaleFilter'), prev = select.value;
    var scales = Array.from(new Set(records.map(function (r) { return r.max_scale; }))).sort(function (a, b) { return b - a; });
    QTL.clear(select);
    select.append(h('option', { value: '0', text: '最大震度: すべて' }));
    scales.forEach(function (s) { select.append(h('option', { value: String(s), text: '震度' + scaleLabel(s) + 'のみ' })); });
    if (Array.from(select.options).some(function (o) { return o.value === prev; })) select.value = prev;
  }

  function applyFilter() {
    var selected = $('scaleFilter').value, dateFrom = $('dateFrom').value, dateTo = $('dateTo').value;   // "YYYY-MM-DD" or ""
    var filtered = allRecords;
    if (selected !== '0') filtered = filtered.filter(function (r) { return String(r.max_scale) === selected; });
    if (dateFrom || dateTo) {
      filtered = filtered.filter(function (r) {
        // time_raw: "YYYY/MM/DD HH:MM"。日付部分だけ "YYYY-MM-DD" に揃えて比較する。
        var raw = (r.time_raw || '').slice(0, 10).split('/').join('-');
        if (!raw) return false;
        if (dateFrom && raw < dateFrom) return false;
        if (dateTo && raw > dateTo) return false;
        return true;
      });
    }
    lastFiltered = filtered;
    renderMarkers(filtered);
    renderTable(filtered);
    renderMonthlyChart(filtered);
  }

  var monthlyChart = null;
  function renderMonthlyChart(records) {
    var counts = {};
    records.forEach(function (r) {   // time_raw ("YYYY/MM/DD HH:MM") から "YYYY-MM" を抽出して月次集計
      var ym = (r.time_raw || '').slice(0, 7).replace('/', '-');
      if (ym.length === 7) counts[ym] = (counts[ym] || 0) + 1;
    });
    var months = Object.keys(counts).sort();
    $('chartTitle').textContent = '月次件数（' + TYPE_CONFIG[currentType].label + '、表示中のフィルタ適用後）';
    var sub = T.cssVar('--text-sub'), grid = T.cssVar('--chart-grid');
    if (monthlyChart) monthlyChart.destroy();
    monthlyChart = new Chart($('monthlyChart'), {
      type: 'bar',
      data: { labels: months, datasets: [{ label: '件数', data: months.map(function (m) { return counts[m]; }), backgroundColor: T.cssVar('--accent') }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: sub }, grid: { color: grid } },
          y: { beginAtZero: true, ticks: { color: sub, precision: 0 }, grid: { color: grid } },
        },
      },
    });
  }

  async function loadAndRender() {
    var cfg = TYPE_CONFIG[currentType];
    $('pageSubtitle').textContent = cfg.subtitle;
    try {
      var data = await QTL.fetchJson(cfg.endpoint);
      allRecords = data.records || [];
      populateScaleFilterOptions(allRecords);
      applyFilter();
    } catch (e) {
      QTL.setContent($('tableContent'), QTL.messageBox('error', cfg.label + '履歴の取得に失敗しました: ' + e.message));
    }
  }

  $('typeSelect').addEventListener('change', function (ev) { currentType = ev.target.value; loadAndRender(); });
  $('scaleFilter').addEventListener('change', applyFilter);
  $('dateFrom').addEventListener('change', applyFilter);
  $('dateTo').addEventListener('change', applyFilter);
  $('clearDateBtn').addEventListener('click', function () { $('dateFrom').value = ''; $('dateTo').value = ''; applyFilter(); });
  $('refreshBtn').addEventListener('click', loadAndRender);
  T.onChange(function () { restyleBaseMap(); renderMonthlyChart(lastFiltered); });

  loadAndRender();
})();
