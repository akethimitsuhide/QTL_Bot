// ダッシュボード共通の部品（/dashboard・/quake_map 共通）。
//
// 【XSS 対策の方針】Bot が外部から受け取った文字列（震源地名・通知タイトル・気象庁のコメント等）を、
// HTML 文字列に連結して innerHTML に入れることはしない。要素は h() で DOM API（createElement /
// textContent）として作る。文字列は常にテキストノードになるため、< や " を含んでも HTML として
// 解釈されない。さらに CSP（script-src 'self'）により、万一インラインのスクリプトが注入されても
// 実行されない（二重の防御）。
(function () {
  'use strict';

  // h('tr', {class: 'x', dataset: {lat: 1}}, [h('td', {text: '...'})]) のように要素を作る。
  // 使える属性は下の許可リストのみ（href・src・on* などは設定できない）。
  var ALLOWED = { class: 'className', text: 'textContent', title: 'title', value: 'value', type: 'type', colSpan: 'colSpan' };
  function h(tag, props, children) {
    var el = document.createElement(tag);
    var keys = props ? Object.keys(props) : [];
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i], v = props[k];
      if (k === 'style' && v && typeof v === 'object') Object.assign(el.style, v);   // CSSOM（CSP の対象外）
      else if (k === 'dataset' && v && typeof v === 'object') Object.assign(el.dataset, v);
      else if (Object.prototype.hasOwnProperty.call(ALLOWED, k)) el[ALLOWED[k]] = v;
    }
    var list = [].concat(children == null ? [] : children);
    for (var j = 0; j < list.length; j++) {
      var c = list[j];
      if (c == null || c === false) continue;
      el.append(c);      // 文字列はテキストノードとして追加される（HTML として解釈されない）
    }
    return el;
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function setContent(el, node) { clear(el); el.append(node); }

  function messageBox(cls, text) { return h('div', { class: cls, text: text }); }

  async function fetchJson(url) {
    var res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname); }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  // テーマ切り替えボタン（#themeBtn）の配線
  function wireThemeButton() {
    var btn = document.getElementById('themeBtn');
    if (!btn || !window.QTLTheme) return;
    var render = function () { btn.textContent = 'テーマ: ' + window.QTLTheme.label(); };
    btn.addEventListener('click', function () { window.QTLTheme.cycle(); });
    window.QTLTheme.onChange(render);
    render();
  }
  document.addEventListener('DOMContentLoaded', wireThemeButton);

  window.QTL = { h: h, clear: clear, setContent: setContent, messageBox: messageBox, fetchJson: fetchJson };
})();
