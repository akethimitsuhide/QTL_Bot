// /logs の描画（HTML 文字列は組み立てず、DOM API で作る。js/common.js の方針を参照）。
(function () {
  'use strict';
  var h = QTL.h;
  function $(id) { return document.getElementById(id); }

  var KIND_LABEL = { current: '現在のログ', archive: '圧縮済み (.gz)', pending: '圧縮待ち', legacy: '旧形式' };
  var KNOWN_LEVELS = { debug: 1, info: 1, warning: 1, error: 1, critical: 1 };
  var selected = null;       // 選択中のファイル（一覧の1件分のオブジェクト）
  var files = [];

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function renderList() {
    var box = $('fileList');
    if (!files.length) { QTL.setContent(box, h('div', { class: 'notif-empty', text: 'ログファイルがありません' })); return; }
    var head = h('tr', null, ['ファイル', '種別', 'サイズ', '最終更新', ''].map(function (t) { return h('th', { text: t }); }));
    var rows = files.map(function (f) {
      var btn = h('button', { class: 'btn-link', type: 'button', text: '表示' });
      btn.addEventListener('click', function () { select(f); });
      var tr = h('tr', { class: selected && selected.name === f.name ? 'file-row-selected' : '' }, [
        h('td', { text: f.name }),
        h('td', { class: 'nowrap', text: KIND_LABEL[f.kind] || f.kind }),
        h('td', { class: 'nowrap', text: fmtSize(f.size) }),
        h('td', { class: 'nowrap', text: f.mtime }),
        h('td', null, [btn]),
      ]);
      return tr;
    });
    QTL.setContent(box, h('table', { class: 'notif-table' }, [h('thead', null, [head]), h('tbody', null, rows)]));
  }

  async function loadList() {
    try {
      var data = await QTL.fetchJson('/status/logs');
      files = data.files || [];
      var sel = $('levelSel');
      if (!sel.options.length) {
        sel.append(h('option', { value: '', text: 'すべて' }));
        (data.levels || []).forEach(function (lv) { sel.append(h('option', { value: lv, text: lv + ' 以上' })); });
      }
      $('retentionLine').textContent = data.keep_days > 0
        ? '圧縮済みログの保持: ' + data.keep_days + '日分（LOG_KEEP_DAYS）'
        : '圧縮済みログの保持: ' + data.keep_count + '世代（LOG_BACKUP_COUNT）';
      renderList();
    } catch (e) {
      QTL.setContent($('fileList'), QTL.messageBox('error', '一覧の取得に失敗しました（' + e.message + '）'));
    }
  }

  function select(f) {
    selected = f;
    $('downloadBtn').disabled = !f.downloadable;
    renderList();
    loadView();
  }

  async function loadView() {
    if (!selected) return;
    $('viewTitle').textContent = '内容: ' + selected.name;
    $('viewInfo').textContent = '読み込み中...';
    var qs = new URLSearchParams({
      file: selected.name, tail: $('tailSel').value, level: $('levelSel').value, q: $('keywordInput').value,
    });
    try {
      var data = await QTL.fetchJson('/status/logs/view?' + qs.toString());
      var view = $('logView');
      QTL.clear(view);
      if (!data.entries.length) view.append(h('div', { class: 'log-empty', text: '該当するログがありません' }));
      data.entries.forEach(function (e) {
        var lv = String(e.level || '').toLowerCase();
        view.append(h('div', { class: 'log-entry' + (KNOWN_LEVELS[lv] ? ' level-' + lv : ''), text: e.text }));
      });
      view.scrollTop = view.scrollHeight;          // 新しい側（末尾）を見せる
      var note = '該当 ' + data.matched + ' 件のうち末尾 ' + data.entries.length + ' 件を表示';
      if (data.truncated) note += '（ファイルが大きいため、先頭から ' + data.scanned + ' 件までを対象にしています）';
      if (data.masked > 0) note += ' ／ 秘密情報らしき文字列 ' + data.masked + ' 箇所を伏せ字にしました';
      $('viewInfo').textContent = note;
    } catch (e) {
      $('viewInfo').textContent = '取得に失敗しました（' + e.message + '）';
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('reloadListBtn').addEventListener('click', loadList);
    $('applyBtn').addEventListener('click', loadView);
    $('keywordInput').addEventListener('keydown', function (ev) { if (ev.key === 'Enter') loadView(); });
    $('downloadBtn').addEventListener('click', function () {
      if (selected && selected.downloadable) {
        window.location.href = '/logs/download?file=' + encodeURIComponent(selected.name);
      }
    });
    loadList().then(function () { if (files.length) select(files[0]); });   // 先頭（現在のログ）を開く
  });
})();
