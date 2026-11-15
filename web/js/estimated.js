// /estimated の描画（HTML 文字列は組み立てず、DOM API で作る。js/common.js の方針を参照）。
(function () {
  'use strict';
  var h = QTL.h;
  function $(id) { return document.getElementById(id); }

  var entries = [];          // /status/estimated_list の entries（新しい順）
  var mapEnabled = true;
  var selectedId = null;

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
  }

  // 地図は <img> で読み込む（同一オリジン。CSP の img-src 'self' で許可）。失敗時は API の JSON エラーを表示する。
  function showMap(e) {
    var box = $('mapBox'), info = $('mapInfo');
    QTL.clear(box);
    if (!mapEnabled) { info.textContent = '地図の表示は無効です（GIS_MAP_ENABLE=false）。'; return; }
    if (!e.has_map) { info.textContent = 'この地震には地図画像がありません。'; return; }
    info.textContent = '地図を作成中...（気象庁からメッシュ画像を取得するため、数秒かかることがあります）';
    var url = '/status/estimated_map?id=' + encodeURIComponent(e.id);
    var img = document.createElement('img');
    img.alt = e.occurred_at + ' ' + e.epicenter + ' の推計震度分布図';
    img.addEventListener('load', function () {
      if (selectedId === e.id) info.textContent = '';
    });
    img.addEventListener('error', async function () {
      if (selectedId !== e.id) return;
      var msg = '地図を表示できませんでした。';
      try {
        var res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
        if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname); return; }
        var data = await res.json();
        if (data && data.error) msg += '（' + data.error + '）';
      } catch (err) { /* JSON ではない応答は理由を出さない */ }
      info.textContent = msg;
    });
    img.src = url;
    box.append(img);
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
      mapEnabled = data.map_enabled !== false;
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
    load();
  });
})();
