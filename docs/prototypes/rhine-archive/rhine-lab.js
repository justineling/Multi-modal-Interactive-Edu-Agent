/* ==========================================================================
   Rhine Lab UI — interaction layer
   Zero dependencies. Auto-initialises from data attributes.
   Every behaviour degrades gracefully and honours reduced-motion.
   ========================================================================== */

(function () {
  'use strict';

  var reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)');
  /* True when the OS asks for less motion, or when the page opts in via
     .rl-reduced (an in-app settings toggle). Both are checked at call time so
     toggling mid-session takes effect on the next interaction. */
  var reduced = function () {
    return reduceMQ.matches ||
      document.documentElement.classList.contains('rl-reduced') ||
      (document.body && document.body.classList.contains('rl-reduced'));
  };
  var EASE = 'cubic-bezier(0.22, 0.61, 0.36, 1)';

  /* ---------- theme ------------------------------------------------------ */

  var THEME_KEY = 'rl-theme';

  function savedTheme() {
    try {
      var v = localStorage.getItem(THEME_KEY);
      return v === 'dark' || v === 'light' ? v : null;
    } catch (e) { return null; }
  }

  /* persist=false is used when mirroring the OS preference: it must not look
     like an explicit user choice, or the page would stop following the system. */
  function setTheme(mode, persist) {
    var root = document.documentElement;
    if (mode === 'dark') root.setAttribute('data-rl-theme', 'dark');
    else root.removeAttribute('data-rl-theme');
    document.querySelectorAll('[data-theme-btn]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.themeBtn === mode));
    });
    if (persist !== false) {
      try { localStorage.setItem(THEME_KEY, mode); } catch (e) {}
    }
    document.dispatchEvent(new CustomEvent('rl:theme', { detail: { mode: mode } }));
  }

  function initTheme() {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');

    // Explicit choice wins; otherwise follow the OS on first visit.
    var saved = savedTheme();
    if (saved) setTheme(saved, false);
    else if (mq.matches) setTheme('dark', false);

    document.querySelectorAll('[data-theme-btn]').forEach(function (b) {
      b.setAttribute('aria-pressed',
        String((document.documentElement.getAttribute('data-rl-theme') === 'dark') === (b.dataset.themeBtn === 'dark')));
      b.addEventListener('click', function () { setTheme(b.dataset.themeBtn); });
    });

    // Keep following the OS until the user makes an explicit choice.
    var onChange = function (e) {
      if (!savedTheme()) setTheme(e.matches ? 'dark' : 'light', false);
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  /* ---------- tabs: one sliding indicator -------------------------------- */

  function initTabs(bar) {
    var tabs = Array.prototype.slice.call(bar.querySelectorAll('[role="tab"]'));
    var indicator = bar.querySelector('.rl-tab-indicator');
    if (!tabs.length) return;

    function move(btn) {
      if (!indicator) return;
      indicator.style.width = btn.offsetWidth + 'px';
      indicator.style.transform = 'translateX(' + (btn.offsetLeft - bar.offsetLeft) + 'px)';
    }

    function select(btn) {
      tabs.forEach(function (t) {
        var on = t === btn;
        t.setAttribute('aria-selected', String(on));
        // Roving tabindex (ARIA APG): only the selected tab is in the Tab order,
        // arrows move between tabs. Without this every tab becomes a Tab stop.
        t.setAttribute('tabindex', on ? '0' : '-1');
      });
      var id = btn.dataset.tab;
      var scope = bar.parentElement || document;
      scope.querySelectorAll('.rl-tab-panel').forEach(function (p) {
        var on = p.dataset.panel === id;
        p.hidden = !on;
        if (on) { p.style.animation = 'none'; void p.offsetWidth; p.style.animation = ''; }
      });
      move(btn);
      bar.dispatchEvent(new CustomEvent('rl:tab', { detail: { index: id }, bubbles: true }));
    }

    tabs.forEach(function (t, i) {
      if (!t.hasAttribute('tabindex')) {
        t.setAttribute('tabindex', t.getAttribute('aria-selected') === 'true' ? '0' : '-1');
      }
      t.addEventListener('click', function () { select(t); });
      t.addEventListener('keydown', function (e) {
        var next = null;
        if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
        else if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === 'Home') next = tabs[0];
        else if (e.key === 'End') next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); next.focus(); select(next); }
      });
    });

    var active = tabs.find(function (t) { return t.getAttribute('aria-selected') === 'true'; }) || tabs[0];
    // wait a frame so layout is measurable
    requestAnimationFrame(function () { move(active); });
    window.addEventListener('resize', function () {
      var cur = tabs.find(function (t) { return t.getAttribute('aria-selected') === 'true'; });
      if (cur) move(cur);
    });
  }

  /* ---------- rolling numbers -------------------------------------------- */

  /* Vertical roll: the old value slides up and out, the new one slides in.
     The logical value lives in dataset.rollValue, never in textContent — during
     a roll the element holds two stacked spans, so reading textContent would
     return "X-001 / 040X-002 / 040" and poison the next roll. An in-flight roll
     is cancelled before a new one starts, so interrupted rolls cannot stack. */
  function rollTo(el, value) {
    var next = String(value);
    var prev = el.dataset.rollValue;
    if (prev === undefined) prev = (el.textContent || '').trim();
    if (prev === next) return;
    el.dataset.rollValue = next;

    if (el._rollTimer) { clearTimeout(el._rollTimer); el._rollTimer = null; }

    var restore = function () {
      el.textContent = next;
      el.style.overflow = '';
      if (el._rollWasStatic) { el.style.position = ''; el._rollWasStatic = false; }
      el._rollTimer = null;
    };

    if (reduced()) { restore(); return; }

    if (getComputedStyle(el).position === 'static') {
      el.style.position = 'relative';
      el._rollWasStatic = true;
    }
    el.style.overflow = 'hidden';
    el.textContent = '';

    var from = document.createElement('span');
    var to = document.createElement('span');
    from.textContent = prev;
    to.textContent = next;
    // The outgoing value stays in the DOM during the roll; hide it from AT so a
    // screen reader announces only the new value instead of both concatenated.
    from.setAttribute('aria-hidden', 'true');
    [from, to].forEach(function (s) {
      s.style.display = 'block';
      s.style.transition = 'transform 460ms ' + EASE + ', opacity 460ms ' + EASE;
    });
    to.style.position = 'absolute';
    to.style.inset = '0';
    to.style.transform = 'translateY(60%)';
    to.style.opacity = '0';

    el.appendChild(from);
    el.appendChild(to);

    requestAnimationFrame(function () {
      from.style.transform = 'translateY(-60%)';
      from.style.opacity = '0';
      to.style.transform = 'translateY(0)';
      to.style.opacity = '1';
    });

    el._rollTimer = setTimeout(restore, 480);
  }

  function initRolls() {
    document.querySelectorAll('[data-roll]').forEach(function (el) {
      el.dataset.rollValue = el.textContent.trim();
    });
  }

  /* ---------- modal ------------------------------------------------------ */

  var openModals = [];

  function openModal(modal, trigger) {
    var scrim = document.querySelector('.rl-scrim[data-for="' + modal.id + '"]')
             || document.querySelector('.rl-scrim');
    var state = { modal: modal, scrim: scrim, lastFocus: trigger || document.activeElement };
    openModals.push(state);

    if (scrim) { scrim.hidden = false; }
    modal.hidden = false;

    requestAnimationFrame(function () {
      if (scrim) scrim.dataset.open = 'true';
      modal.dataset.open = 'true';
    });

    // isolate the rest of the page
    Array.prototype.forEach.call(document.body.children, function (el) {
      if (el !== modal && el !== scrim && !el.contains(modal) && !el.contains(scrim)) {
        el.setAttribute('inert', '');
      }
    });

    var focusable = modal.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    (focusable || modal).focus();

    modal.dispatchEvent(new CustomEvent('rl:modal-open', { bubbles: true }));
  }

  function closeModal(modal) {
    var i = openModals.findIndex(function (s) { return s.modal === modal; });
    if (i < 0) return;
    var state = openModals.splice(i, 1)[0];

    modal.dataset.open = 'false';
    if (state.scrim) state.scrim.dataset.open = 'false';

    var wait = reduced() ? 0 : 200;
    setTimeout(function () {
      modal.hidden = true;
      if (state.scrim) state.scrim.hidden = true;
      Array.prototype.forEach.call(document.body.children, function (el) { el.removeAttribute('inert'); });
      if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
      modal.dispatchEvent(new CustomEvent('rl:modal-close', { bubbles: true }));
    }, wait);
  }

  function initModals() {
    document.querySelectorAll('[data-modal-open]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var target = document.getElementById(btn.dataset.modalOpen);
        if (target) openModal(target, btn);
      });
    });
    document.querySelectorAll('[data-modal-close]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var m = btn.closest('.rl-modal');
        if (m) closeModal(m);
      });
    });
    document.querySelectorAll('.rl-scrim').forEach(function (s) {
      s.addEventListener('click', function () {
        var m = openModals[openModals.length - 1];
        if (m) closeModal(m.modal);
      });
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var top = openModals[openModals.length - 1];
      if (!top) return;
      e.stopPropagation();
      closeModal(top.modal);
    });
    // trap focus inside the topmost modal
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var top = openModals[openModals.length - 1];
      if (!top) return;
      var items = top.modal.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      if (!items.length) return;
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }

  /* ---------- ticks ------------------------------------------------------ */

  function initTicks() {
    document.querySelectorAll('.rl-ticks').forEach(function (rail) {
      var btns = Array.prototype.slice.call(rail.querySelectorAll('button'));
      btns.forEach(function (b, i) {
        b.addEventListener('click', function () {
          btns.forEach(function (x) { x.removeAttribute('aria-current'); });
          b.setAttribute('aria-current', 'true');
          rail.dispatchEvent(new CustomEvent('rl:tick', { detail: { index: i }, bubbles: true }));
        });
      });
    });
  }

  /* ---------- filter (search) -------------------------------------------- */

  function initFilters() {
    document.querySelectorAll('[data-filter]').forEach(function (input) {
      var listSel = input.dataset.filter;
      var statusSel = input.dataset.filterStatus;
      var list = document.querySelector(listSel);
      var status = statusSel ? document.querySelector(statusSel) : null;
      if (!list) return;

      var items = Array.prototype.slice.call(list.children);
      items.forEach(function (li) { li.dataset.raw = li.textContent.toLowerCase(); });

      input.addEventListener('input', function () {
        var q = input.value.trim().toLowerCase();
        var hits = 0;
        items.forEach(function (li) {
          var on = !q || li.dataset.raw.indexOf(q) > -1;
          li.hidden = !on;
          if (on) hits++;
        });
        if (status) {
          status.textContent = !q
            ? (status.dataset.idle || '输入以检索已归档条目。')
            : (hits ? hits + ' MATCHING RECORD' : 'NO MATCHING RECORD');
        }
      });
    });
  }

  /* ---------- selectable rows -------------------------------------------- */

  /* Single- or multi-select over a list/tree/table.
     Markup contract:
       <ul class="rl-list rl-list--selectable" role="listbox" data-selectable>
         <li role="option">…</li>                     ← single select
       <ul … data-selectable="multi">…                ← multi select
     A row may contain a .rl-check input; clicking the checkbox is left to the
     browser and synced back via change, so the state never toggles twice. */
  function initSelectable() {
    document.querySelectorAll('[data-selectable]').forEach(function (list) {
      var multi = list.dataset.selectable === 'multi';
      var rows = Array.prototype.slice.call(
        list.querySelectorAll(':scope > [role="option"], :scope > [role="row"], :scope > li, :scope > tr')
      );
      if (!rows.length) return;

      var emit = function (row, on) {
        list.dispatchEvent(new CustomEvent('rl:select', {
          detail: { row: row, selected: on, index: rows.indexOf(row) },
          bubbles: true
        }));
      };

      var apply = function (row, on) {
        row.setAttribute('aria-selected', String(on));
        var box = row.querySelector('.rl-check input');
        if (box && box.checked !== on) box.checked = on;
      };

      var selectOnly = function (row, on) {
        if (!multi) rows.forEach(function (r) { if (r !== row) apply(r, false); });
        apply(row, on);
        emit(row, on);
      };

      rows.forEach(function (row) {
        row.addEventListener('click', function (e) {
          // A click on the checkbox (or its label) is handled by change below.
          if (e.target.closest && e.target.closest('.rl-check')) return;
          selectOnly(row, row.getAttribute('aria-selected') !== 'true');
        });

        var box = row.querySelector('.rl-check input');
        if (box) {
          box.addEventListener('change', function () { selectOnly(row, box.checked); });
          row.setAttribute('aria-selected', String(box.checked));
        }
      });

      // Roving focus: only one row is a Tab stop; arrows move within the list.
      rows.forEach(function (row, i) {
        if (!row.hasAttribute('tabindex')) row.setAttribute('tabindex', i === 0 ? '0' : '-1');
        row.addEventListener('keydown', function (e) {
          var next = null;
          if (e.key === 'ArrowDown') next = rows[Math.min(i + 1, rows.length - 1)];
          else if (e.key === 'ArrowUp') next = rows[Math.max(i - 1, 0)];
          else if (e.key === 'Home') next = rows[0];
          else if (e.key === 'End') next = rows[rows.length - 1];
          else if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault();
            selectOnly(row, row.getAttribute('aria-selected') !== 'true');
            return;
          }
          if (!next) return;
          e.preventDefault();
          rows.forEach(function (r) { r.setAttribute('tabindex', r === next ? '0' : '-1'); });
          next.focus();
          // Single-select listboxes follow focus (automatic activation);
          // multi-select only moves focus so arrows never change the set.
          if (!multi) selectOnly(next, true);
        });
      });
    });
  }

  /* ---------- forms ------------------------------------------------------ */

  /* The HTML attribute `indeterminate` does not exist — it is a DOM property
     only, so <input indeterminate> silently does nothing. Support data-* instead. */
  function initForms() {
    document.querySelectorAll('input[data-indeterminate]').forEach(function (el) {
      el.indeterminate = el.dataset.indeterminate !== 'false';
    });
  }

  /* ---------- progress --------------------------------------------------- */

  function setProgress(bar, ratio) {
    var r = Math.max(0, Math.min(1, ratio));
    bar.style.width = (r * 100) + '%';
  }

  function initProgress() {
    document.querySelectorAll('[data-progress-auto]').forEach(function (bar) {
      var target = parseFloat(bar.dataset.progressAuto) || 1;
      var label = bar.dataset.progressLabel ? document.querySelector(bar.dataset.progressLabel) : null;
      var step = 0, steps = 40;
      var t = setInterval(function () {
        step = Math.min(steps, step + 1);
        setProgress(bar, step / steps * target);
        if (label) label.textContent = String(step).padStart(3, '0') + ' / 040';
        if (step === steps) clearInterval(t);
      }, reduced() ? 0 : 60);
    });
  }

  /* ---------- toast ------------------------------------------------------ */

  function toast(text, ms) {
    var el = document.querySelector('.rl-toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'rl-toast';
      el.style.cssText = 'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);opacity:0;pointer-events:none;z-index:80';
      el.innerHTML = '<span class="rl-status"><i></i></span><span></span>';
      document.body.appendChild(el);
    }
    el.lastElementChild.textContent = text;
    el.style.transition = 'opacity ' + (reduced() ? 0 : 220) + 'ms ' + EASE;
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.style.opacity = '0'; }, ms || 2200);
  }

  /* ---------- boot ------------------------------------------------------- */

  function init() {
    initTheme();
    document.querySelectorAll('.rl-tabs').forEach(initTabs);
    initRolls();
    initModals();
    initTicks();
    initFilters();
    initSelectable();
    initForms();
    initProgress();

    // data-toast="Message" on any button
    document.querySelectorAll('[data-toast]').forEach(function (btn) {
      btn.addEventListener('click', function () { toast(btn.dataset.toast); });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.RhineLab = {
    setTheme: setTheme,
    openModal: function (id, trigger) {
      var m = document.getElementById(id);
      if (m) openModal(m, trigger);
    },
    closeModal: closeModal,
    toast: toast,
    rollTo: function (selector, value) {
      var el = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (el) rollTo(el, value);
    },
    setProgress: setProgress
  };
})();
