// /estimated の描画（HTML 文字列は組み立てず、DOM API で作る。js/common.js の方針を参照）。
// 背景地図は /quake_map と同じく Bot が配信する GeoJSON（/status/gis_countries・/status/gis_local_areas）を
// Leaflet で描く（外部の地図サーバーへはアクセスしない）。推計震度分布図は、気象庁のメッシュ画像（透過PNG）を
// Bot が中継する /status/estimated_tile から読み込み、各メッシュの緯度経度範囲に重ねる。
(function () {
  'use strict';
  var h = QTL.h, T = window.QTLTheme;
  function $(id) { return document.getElementById(id); }

  var entries = [];          // /status/estimated_list の entries（新しい順）
  var selectedId = null;
  var overlayOpacity = 0.6;  // 気象庁HPと同じ重ね合わせの濃さ

  // ── 地図 ──
  var map = L.map('estMap', { preferCanvas: true, minZoom: 3, maxZoom: 12 }).setView([36.5, 138.0], 5);
  map.createPane('landFillPane');   map.getPane('landFillPane').style.zIndex = 350;   // 陸地の塗り
  map.createPane('estTilePane');    map.getPane('estTilePane').style.zIndex = 380;    // 推計震度分布図（メッシュ画像）
  map.createPane('estBorderPane');  map.getPane('estBorderPane').style.zIndex = 390;  // 区域の境界線（画像の上に出す）

  var baseLayers = [];   // [{layer, kind: 'fill'|'border', borderVar}]
  function baseStyle(b) {
    if (b.kind === 'border') return { color: T.cssVar(b.borderVar), weight: 1, fill: false, interactive: false };
    return { stroke: b.borderVar === '--map-border', color: T.cssVar('--map-border'), weight: 1,
             fillColor: T.cssVar('--map-land'), fillOpacity: 1, interactive: false };
  }
  function addBase(features, kind, borderVar) {
    var b = { kind: kind, borderVar: borderVar };
    b.layer = L.geoJSON(features, { pane: kind === 'border' ? 'estBorderPane' : 'landFillPane', style: baseStyle(b) }).addTo(map);
    baseLayers.push(b);
  }
  // 世界の国境（日本を除く簡略版）→ 日本の細分区域の順に重ねる。日本は塗りと境界線を別レイヤーにして、
  // 境界線だけを推計震度分布図の上に描く。読み込めなくても致命的ではない（海色の背景＋画像だけになる）。
  QTL.fetchJson('/status/gis_countries').then(function (f) { addBase(f, 'fill', '--map-border'); }).catch(function () {});
  QTL.fetchJson('/status/gis_local_areas').then(function (f) {
    addBase(f, 'fill', '--map-border-local');
    addBase(f, 'border', '--map-border-local');
  }).catch(function () {});

  var tileGroup = L.layerGroup().addTo(map);   // 選択中の地震のメッシュ画像
  var epicenter = null;
  var legendControl = null;

  function buildLegend(items) {
    if (legendControl) return;
    legendControl = L.control({ position: 'bottomleft' });
    legendControl.onAdd = function () {
      var rows = items.map(function (it) {
        var sw = h('span', { class: 'est-legend-swatch' });
        sw.style.backgroundColor = it.color;      // CSSOM 経由の設定（CSP のインラインスタイル禁止に抵触しない）
        return h('div', { class: 'est-legend-row' }, [sw, h('span', { text: '震度' + it.label })]);
      });
      return h('div', { class: 'est-legend' }, [h('div', { class: 'est-legend-title', text: '推計震度' })].concat(rows));
    };
    legendControl.addTo(map);
  }

  // ── 一覧 ──
  function title(e) {
    var parts = [e.occurred_at || '日時不明', e.epicenter || '震央不明'];
    if (e.magnitude) parts.push('M' + e.magnitude);
    return parts.join('　');
  }
  function maxRank(e) { return e.ranks && e.ranks.length ? '最大震度' + e.ranks[0].label : ''; }
  function matches(e, kw) {
    if (!kw) return true;
    return (e.occurred_at + ' ' + e.epicenter + ' M' + e.magnitude + ' ' + maxRank(e)).toLowerCase().indexOf(kw) !== -1;
  }

  function renderList() {
    var kw = $('keywordInput').value.trim().toLowerCase();
    var shown = entries.filter(function (e) { return matches(e, kw); });
    var box = $('eventList');
    if (!shown.length) {
      QTL.setContent(box, h('div', { class: 'notif-empty', text: entries.length ? '該当する地震がありません' : '推計震度分布図がまだありません' }));
    } else {
      QTL.clear(box);
      shown.forEach(function (e) {
        var btn = h('button', { class: 'est-item' + (e.id === selectedId ? ' est-selected' : ''), type: 'button' }, [
          h('div', { class: 'est-item-title', text: (e.occurred_at || '日時不明') + '　' + (e.epicenter || '震央不明') }),
          h('div', { class: 'est-item-sub', text: ['M' + (e.magnitude || '?'), maxRank(e)].filter(Boolean).join('　') }),
        ]);
        btn.addEventListener('click', function () { select(e.id, true); });
        box.append(btn);
      });
    }
    $('listInfo').textContent = entries.length + ' 件中 ' + shown.length + ' 件を表示';
  }

  function renderDetail(e) {
    $('detailTitle').textContent = title(e);
    var meta = h('dl', { class: 'est-meta' }, [
      h('dt', { text: '震源地' }), h('dd', { text: e.epicenter || '不明' }),
      h('dt', { text: '規模' }), h('dd', { text: e.magnitude ? 'M' + e.magnitude : '不明' }),
      h('dt', { text: '深さ' }), h('dd', { text: e.depth == null ? '不明' : (e.depth < 5 ? 'ごく浅い' : e.depth + 'km') }),
      h('dt', { text: '推計震度ごとのメッシュ数' }),
      h('dd', { text: e.ranks && e.ranks.length ? e.ranks.map(function (r) { return '震度' + r.label + ': ' + r.count.toLocaleString(); }).join('　') : '—' }),
    ]);
    var nodes = [meta];
    if (e.comment) nodes.push(h('div', { class: 'est-comment', text: e.comment }));
    QTL.setContent($('detailMeta'), h('div', null, nodes));
    var link = $('imageLink');
    link.href = '/status/estimated_map?id=' + encodeURIComponent(e.id);
    link.hidden = false;
  }

  // ── 地図への表示 ──
  function clearMap() {
    tileGroup.clearLayers();
    if (epicenter) { map.removeLayer(epicenter); epicenter = null; }
  }

  function showMap(e) {
    var info = $('mapInfo');
    clearMap();
    info.textContent = '地図の部品を取得中...';
    QTL.fetchJson('/status/estimated_detail?id=' + encodeURIComponent(e.id)).then(function (d) {
      if (selectedId !== e.id) return;           // 取得中に別の地震が選ばれた
      buildLegend(d.legend || []);
      var all = [];
      var total = (d.tiles || []).length, done = 0, failed = 0;
      function progress() {
        if (selectedId !== e.id) return;
        if (done + failed < total) { info.textContent = 'メッシュ画像を読み込み中... ' + (done + failed) + ' / ' + total; return; }
        info.textContent = failed ? ('メッシュ画像 ' + failed + ' 枚を取得できませんでした。図に欠けがあります。') : '';
      }
      (d.tiles || []).forEach(function (t) {
        var b = L.latLngBounds(t.bounds[0], t.bounds[1]);
        all.push(b.getSouthWest(), b.getNorthEast());
        var url = '/status/estimated_tile?id=' + encodeURIComponent(e.id) + '&mesh=' + encodeURIComponent(t.mesh);
        var ov = L.imageOverlay(url, b, { pane: 'estTilePane', opacity: overlayOpacity, interactive: false, alt: '' });
        ov.on('load', function () { done++; progress(); });
        ov.on('error', function () { failed++; progress(); });
        tileGroup.addLayer(ov);
      });
      if (d.lat != null && d.lon != null) {
        var mark = h('span', { class: 'est-epi-mark', text: '✕' });
        epicenter = L.marker([d.lat, d.lon], {
          icon: L.divIcon({ className: 'est-epi', html: mark, iconSize: [28, 28], iconAnchor: [14, 14] }),
          keyboard: false, zIndexOffset: 1000,
        }).addTo(map);
        epicenter.bindPopup(h('div', null, [h('b', { text: '震源' }), h('br'), String(e.epicenter || '')]));
        all.push(L.latLng(d.lat, d.lon));
      }
      var bounds = d.bounds ? L.latLngBounds(d.bounds[0], d.bounds[1]) : null;
      if (bounds && d.lat != null && d.lon != null) bounds.extend([d.lat, d.lon]);
      if (!bounds && all.length) bounds = L.latLngBounds(all);
      if (bounds) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 10 });
      if (!total) info.textContent = 'この地震にはメッシュ画像がありません。';
      else progress();
    }).catch(function (err) {
      if (selectedId === e.id) info.textContent = '地図を表示できませんでした（' + err.message + '）';
    });
  }

  function select(id, updateHash) {
    var e = entries.find(function (x) { return x.id === id; });
    if (!e) return;
    selectedId = id;
    if (updateHash) history.replaceState(null, '', '#' + encodeURIComponent(id));
    renderList();
    renderDetail(e);
    showMap(e);
  }

  async function load() {
    try {
      var data = await QTL.fetchJson('/status/estimated_list');
      entries = data.entries || [];
    } catch (err) {
      QTL.setContent($('eventList'), QTL.messageBox('error', '一覧の取得に失敗しました（' + err.message + '）'));
      return;
    }
    var wanted = decodeURIComponent((window.location.hash || '').replace(/^#/, ''));
    var first = entries.some(function (x) { return x.id === wanted; }) ? wanted : (entries[0] && entries[0].id);
    renderList();
    if (first) select(first, false);
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('keywordInput').addEventListener('input', renderList);
    $('reloadBtn').addEventListener('click', load);
    $('opacityRange').addEventListener('input', function (ev) {
      overlayOpacity = Number(ev.target.value) / 100;
      tileGroup.eachLayer(function (l) { l.setOpacity(overlayOpacity); });
    });
    // テーマ切替（ダーク/ライト）に合わせて背景地図の色を更新する
    T.onChange(function () { baseLayers.forEach(function (b) { b.layer.setStyle(baseStyle(b)); }); });
    load();
  });
})();
