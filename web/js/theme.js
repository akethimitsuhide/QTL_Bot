// テーマ（ダーク／ライト／自動）の保存と適用。<head> で同期的に読み込み、描画前に data-theme を設定する
// （読み込みが遅れると、最初に一瞬だけ別のテーマが見える）。
// 保存先はこのブラウザの localStorage（サーバーには送らない）。使えない環境（プライベートモード等）でも
// 例外にせず、保存なしの「自動」で動く。
(function () {
  'use strict';
  var KEY = 'qtl-theme';
  var MODES = ['auto', 'dark', 'light'];
  var LABELS = { auto: '自動', dark: 'ダーク', light: 'ライト' };
  var listeners = [];

  function load() {
    try {
      var v = window.localStorage.getItem(KEY);
      return MODES.indexOf(v) >= 0 ? v : 'auto';
    } catch (e) { return 'auto'; }
  }
  function apply(mode) {
    if (mode === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', mode);
  }
  var current = load();
  apply(current);

  window.QTLTheme = {
    mode: function () { return current; },
    label: function () { return LABELS[current]; },
    set: function (mode) {
      if (MODES.indexOf(mode) < 0) return;
      current = mode;
      try { window.localStorage.setItem(KEY, mode); } catch (e) { /* 保存できなくても動作は続ける */ }
      apply(mode);
      listeners.forEach(function (fn) { fn(mode); });
    },
    cycle: function () { this.set(MODES[(MODES.indexOf(current) + 1) % MODES.length]); },
    // 現在有効な配色（CSS 変数）を取得する。Chart.js・Leaflet の色指定に使う。
    cssVar: function (name) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    },
    onChange: function (fn) { listeners.push(fn); },
  };

  // 「自動」のとき、OS 側の設定が変わったら再描画できるよう通知する
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: light)');
    var notify = function () { if (current === 'auto') listeners.forEach(function (fn) { fn('auto'); }); };
    if (mq.addEventListener) mq.addEventListener('change', notify);
    else if (mq.addListener) mq.addListener(notify);
  }
})();
