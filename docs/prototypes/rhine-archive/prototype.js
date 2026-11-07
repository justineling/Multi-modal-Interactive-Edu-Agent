/* ==========================================================================
   rhine-archive / prototype.js
   单页原型的本地交互层。所有状态只落在当前页面，不发起任何网络请求。
   rhine-lab.js 的 data-* 钩子负责：主题、页签、模态框、刻度点击、检索、行选中。
   本文件只补四件事：默认亮色基准、目录树选中、刻度轨联动页序、Composer 本地回执。
   ========================================================================== */

(function () {
  'use strict';

  var THEME_KEY = 'rl-theme';

  function readTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (err) { return null; }
  }

  function writeTheme(mode) {
    try { localStorage.setItem(THEME_KEY, mode); } catch (err) { return; }
  }

  /* Q-5：亮色暖灰纸是基准。仅在用户从未做过显式选择时钉住亮色，
     避免 skill 在首次访问时跟随操作系统的深色偏好；暗色仍由按钮主动切换。 */
  function pinLightDefault() {
    var saved = readTheme();
    if (saved === 'dark' || saved === 'light') return;
    writeTheme('light');
    if (window.RhineLab && typeof window.RhineLab.setTheme === 'function') {
      window.RhineLab.setTheme('light', false);
    } else {
      document.documentElement.removeAttribute('data-rl-theme');
    }
  }

  function pad2(value) {
    return (value < 10 ? '0' : '') + String(value);
  }

  /* 目录树节点：单选，选中态用 aria-selected（treeitem 角色下合法），
     不借用 aria-current，以免额外占用暖杏金预算。 */
  function initTree() {
    var tree = document.getElementById('toc-tree');
    var current = document.getElementById('toc-current');
    if (!tree) return;
    var nodes = tree.querySelectorAll('[data-toc-node]');
    Array.prototype.forEach.call(nodes, function (node) {
      node.addEventListener('click', function () {
        Array.prototype.forEach.call(nodes, function (other) {
          other.setAttribute('aria-selected', other === node ? 'true' : 'false');
        });
        if (current) current.textContent = node.getAttribute('data-toc-node');
      });
    });
  }

  /* 两条刻度轨（卷宗页 + 页脚）联动，并同步所有页序读数；点击切页。 */
  function initPageRail() {
    var rails = document.querySelectorAll('.rl-ticks');
    var readouts = document.querySelectorAll('[data-page-readout]');
    document.addEventListener('rl:tick', function (event) {
      if (!event || !event.detail || typeof event.detail.index !== 'number') return;
      var label = pad2(event.detail.index + 1) + '/05';
      Array.prototype.forEach.call(readouts, function (el) { el.textContent = label; });
      Array.prototype.forEach.call(rails, function (rail) {
        var buttons = rail.querySelectorAll('button');
        Array.prototype.forEach.call(buttons, function (button, index) {
          if (index === event.detail.index) button.setAttribute('aria-current', 'true');
          else button.removeAttribute('aria-current');
        });
      });
    });
  }

  /* Composer：可读输入、可点击，但只写本地回执文字。
     页面里没有 form、没有 submit 类型按钮，也没有任何请求调用。 */
  function initComposer() {
    var input = document.getElementById('composer-input');
    var submit = document.getElementById('composer-submit');
    var status = document.getElementById('composer-status');
    if (!input || !submit || !status) return;
    submit.addEventListener('click', function () {
      var text = input.value.replace(/^\s+|\s+$/g, '');
      if (!text) {
        status.textContent = '尚未输入内容；本地草稿未记录，未发送任何请求。';
        return;
      }
      status.textContent = '本地草稿已记录（' + text.length + ' 字）；原型不发送任何网络请求。';
    });
  }

  function init() {
    pinLightDefault();
    initTree();
    initPageRail();
    initComposer();
  }

  init();
})();
