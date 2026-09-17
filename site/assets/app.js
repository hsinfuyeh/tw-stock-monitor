/* 共用層：外框、搜尋、名詞解釋、表格分頁、明暗模式。
 *
 * 明暗切換 / tooltip / 分頁 / 搜尋建議 這四塊在本機版就已經是前端 JS，
 * 所以幾乎是原封不動搬過來的。真正改寫的只有「資料從哪來」：
 * 本機版是 Flask 組好 HTML 送出，這裡改成抓 JSON 再組。
 */
(function () {
  'use strict';

  var DATA = 'data/';
  var cache = {};

  /* ---------- 取資料 ---------- */
  function get(path) {
    if (!cache[path]) {
      cache[path] = fetch(DATA + path).then(function (r) {
        if (!r.ok) throw new Error(path + ' ' + r.status);
        return r.json();
      });
    }
    return cache[path];
  }

  /* ---------- 小工具 ---------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function num(v, nd, sign) {
    if (v == null || isNaN(v)) return '—';
    var s = Number(v).toFixed(nd == null ? 2 : nd);
    if (sign && v > 0) s = '+' + s;
    return s;
  }
  function cls(v) { return v > 0 ? 'up' : (v < 0 ? 'dn' : 'mut'); }
  function pct(v, nd) {
    if (v == null || isNaN(v)) return '—';
    return '<span class="' + cls(v) + '">' + num(v, nd, true) + '%</span>';
  }
  function yi(v) { return v == null || isNaN(v) ? '—' : (v / 1e8).toFixed(2) + '億'; }
  function qs(k) { return new URLSearchParams(location.search).get(k); }

  /* ---------- 名詞解釋 ---------- */
  var GLOSSARY = {};
  function tip(label, key) {
    var t = GLOSSARY[key || label];
    if (!t) return esc(label);
    return '<span class="tip" tabindex="0" data-tip="' + esc(t) + '">' +
      esc(label) + '</span>';
  }

  /* ---------- 外框 ---------- */
  var NAV = [
    ['funnel.html', '選股', 'funnel'],
    ['lists.html', '盤後', 'lists'],
    ['lists.html?s=exdiv', '除權息', 'exdiv']
  ];

  function shell(active, q) {
    var links = NAV.map(function (n) {
      return '<a class="navlink' + (n[2] === active ? ' on' : '') +
        '" href="' + n[0] + '">' + n[1] + '</a>';
    }).join('');
    return '<div class="top"><div class="in">' +
      '<a class="brand" href="index.html">台股觀測</a>' + links +
      searchbox(q || '') +
      '<button id="themebtn" class="themebtn" type="button">☾</button>' +
      '</div></div><div class="stale" id="updbar" data-empty="1"><div class="in"></div></div>';
  }

  function searchbox(value, big) {
    return '<div class="searchbox' + (big ? ' big' : '') + '">' +
      '<input class="qin" autocomplete="off" value="' + esc(value) +
      '" placeholder="輸入股票代號或名稱，例如 2330 或 台積電">' +
      '<div class="sug" style="display:none"></div></div>';
  }

  /* ---------- 資料新鮮度 ----------
   * 靜態站沒有後端可以問，所以直接用 meta.json 裡的資料日期自己算。
   * 這個橫幅在本機版是載重結構不是裝飾：這個工具是拿來決定下單的，
   * 安靜地顯示過期價格比顯示錯誤更危險，所以一定要跟著搬過來。 */
  function staleBanner(meta) {
    var bar = document.getElementById('updbar');
    if (!bar || !meta || !meta.data_date) return;
    var last = new Date(meta.data_date + 'T00:00:00');
    var now = new Date();
    // 台股 13:30 收盤，資料約 14:30 上架；抓 15:00 保守一點
    var edge = new Date(now);
    if (now.getHours() < 15) edge.setDate(edge.getDate() - 1);
    edge.setHours(0, 0, 0, 0);
    var n = 0, d = new Date(last);
    d.setDate(d.getDate() + 1);
    while (d <= edge) {
      if (d.getDay() !== 0 && d.getDay() !== 6) n++;
      d.setDate(d.getDate() + 1);
    }
    if (n < 1) return;
    bar.removeAttribute('data-empty');
    bar.querySelector('.in').innerHTML =
      '<b>資料落後 ' + n + ' 個營業日</b><span>最新是 ' + esc(meta.data_date) +
      '。中間若有國定假日屬正常；否則是每日自動更新沒有跑成功。</span>';
  }

  /* ---------- 表格分頁 ----------
   * 純前端切頁：資料一次全部到瀏覽器，箭頭只換顯示哪一段。
   * 不發請求、不動網址、不塞歷史紀錄 —— 使用者按上一頁是要離開這個榜單，
   * 不是退回第 1 頁。 */
  var PER = 15;
  function pager(root) {
    root.querySelectorAll('.scroll > table').forEach(function (tb) {
      var tbody = tb.tBodies[0];
      if (!tbody) return;
      var rows = [].slice.call(tbody.rows);
      var per = parseInt(tb.parentNode.getAttribute('data-per'), 10) || PER;
      if (rows.length <= per) return;
      var pages = Math.ceil(rows.length / per), cur = 0;

      var box = document.createElement('div');
      box.className = 'pager';
      box.innerHTML =
        '<button class="pgb" type="button" data-d="-1" aria-label="上一頁">←</button>' +
        '<span class="pgn" aria-live="polite"></span>' +
        '<button class="pgb" type="button" data-d="1" aria-label="下一頁">→</button>';
      /* 掛在 .scroll 外面 —— 掛裡面的話表格橫向捲動時分頁鈕會跟著捲走 */
      tb.parentNode.parentNode.insertBefore(box, tb.parentNode.nextSibling);
      var prev = box.children[0], lbl = box.children[1], next = box.children[2];

      function paint() {
        var lo = cur * per, hi = Math.min(rows.length, lo + per);
        rows.forEach(function (r, i) {
          r.style.display = (i >= lo && i < hi) ? '' : 'none';
        });
        lbl.textContent = (lo + 1) + '–' + hi + ' / ' + rows.length;
        prev.disabled = cur === 0;
        next.disabled = cur >= pages - 1;
      }
      box.addEventListener('click', function (e) {
        var b = e.target.closest ? e.target.closest('.pgb') : null;
        if (!b || b.disabled) return;
        cur = Math.max(0, Math.min(pages - 1, cur + parseInt(b.getAttribute('data-d'), 10)));
        paint();
        /* 換頁後表頭若已捲出畫面就拉回來，否則看起來像沒反應 */
        var top = tb.getBoundingClientRect().top + window.scrollY - 86;
        if (window.scrollY > top) window.scrollTo({ top: top, behavior: 'smooth' });
      });
      paint();
    });
  }

  /* ---------- 明暗模式 ---------- */
  function theme() {
    var btn = document.getElementById('themebtn');
    if (!btn) return;
    function sysDark() {
      return window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches;
    }
    function cur() {
      return document.documentElement.getAttribute('data-theme') ||
        (sysDark() ? 'dark' : 'light');
    }
    function paint() {
      btn.textContent = cur() === 'dark' ? '☀' : '☾';
      btn.title = cur() === 'dark' ? '切換成淺色模式' : '切換成深夜模式';
      btn.setAttribute('aria-label', btn.title);
    }
    btn.addEventListener('click', function () {
      var next = cur() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) { }
      paint();
    });
    paint();
  }

  /* ---------- tooltip 浮層 ----------
   * 掛在 body 上而不是用 CSS ::after —— 表格有 overflow-x:auto，
   * CSS tooltip 會被裁掉。 */
  function tooltips() {
    var box = null, cur = null;
    function ensure() {
      if (!box) { box = document.createElement('div'); box.id = 'tipbox'; document.body.appendChild(box); }
      return box;
    }
    function show(el) {
      var t = el.getAttribute('data-tip');
      if (!t) return;
      var b = ensure();
      b.innerHTML = t;
      b.classList.add('show');
      var r = el.getBoundingClientRect(), bw = b.offsetWidth, bh = b.offsetHeight;
      var left = r.left + window.scrollX + r.width / 2 - bw / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
      var top = r.top + window.scrollY - bh - 9;
      if (top < window.scrollY + 6) top = r.bottom + window.scrollY + 9;
      b.style.left = left + 'px';
      b.style.top = top + 'px';
      cur = el;
    }
    function hide() { if (box) box.classList.remove('show'); cur = null; }
    document.addEventListener('mouseover', function (e) {
      var el = e.target.closest ? e.target.closest('.tip') : null;
      if (el && el !== cur) show(el);
    });
    document.addEventListener('mouseout', function (e) {
      var el = e.target.closest ? e.target.closest('.tip') : null;
      if (el === cur) hide();
    });
    /* 手機沒有 hover：點一下顯示，再點別處關閉 */
    document.addEventListener('click', function (e) {
      var el = e.target.closest ? e.target.closest('.tip') : null;
      if (el) { e.preventDefault(); if (el === cur) hide(); else show(el); }
      else hide();
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
    window.addEventListener('scroll', hide, { passive: true });
    window.addEventListener('resize', hide);
    document.addEventListener('focusin', function (e) {
      var el = e.target.closest ? e.target.closest('.tip') : null;
      if (el) show(el);
    });
    document.addEventListener('focusout', hide);
  }

  /* ---------- 搜尋 ----------
   * 靜態站沒有 /api/search，改成把 universe.json 抓下來在前端比對。
   * 1,318 筆 / 71 KB，全部塞進記憶體比每次打後端還快。 */
  var UNI = null;
  function bindSearch(bx) {
    var box = bx.querySelector('.qin'), sug = bx.querySelector('.sug');
    var sel = -1, items = [], composing = false, timer = null;
    if (!box) return;

    function hide() { sug.innerHTML = ''; sug.style.display = 'none'; sel = -1; items = []; }
    function render(rows) {
      if (!rows.length) { hide(); return; }
      sug.innerHTML = rows.map(function (r) {
        return '<a href="stock.html?c=' + encodeURIComponent(r.code) + '">' +
          '<span class="c">' + esc(r.code) + '</span>' +
          '<span class="n">' + esc(r.name) + '</span>' +
          '<span class="t">' + esc(r.kind) + '</span></a>';
      }).join('');
      sug.style.display = 'block';
      items = sug.querySelectorAll('a');
      sel = -1;
    }
    function query() {
      clearTimeout(timer);
      var v = box.value.trim();
      if (!v) { hide(); return; }
      timer = setTimeout(function () {
        get('universe.json').then(function (u) {
          UNI = u;
          var lv = v.toLowerCase();
          var hit = u.filter(function (r) {
            return r.code.toLowerCase().indexOf(lv) === 0 ||
              r.name.indexOf(v) >= 0;
          });
          // 代號開頭相符的排前面，其次才是名稱包含
          hit.sort(function (a, b) {
            var ap = a.code.toLowerCase().indexOf(lv) === 0 ? 0 : 1;
            var bp = b.code.toLowerCase().indexOf(lv) === 0 ? 0 : 1;
            return ap - bp || a.code.localeCompare(b.code);
          });
          render(hit.slice(0, 12));
        }).catch(hide);
      }, 90);
    }
    /* input 一個事件不夠：用注音／倉頡打中文時，組字期間 input 的行為
       在各瀏覽器不一致，必須等 compositionend；keyup 是最後的保險。 */
    box.addEventListener('input', function () { if (!composing) query(); });
    box.addEventListener('compositionstart', function () { composing = true; });
    box.addEventListener('compositionend', function () { composing = false; query(); });
    box.addEventListener('keyup', function (e) {
      if (['ArrowDown', 'ArrowUp', 'Enter', 'Escape'].indexOf(e.key) >= 0) return;
      if (!composing) query();
    });
    box.addEventListener('focus', function () { if (box.value.trim()) query(); });
    box.addEventListener('keydown', function (e) {
      if (!items.length) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (sel >= 0) items[sel].classList.remove('sel');
        sel += (e.key === 'ArrowDown' ? 1 : -1);
        if (sel < 0) sel = items.length - 1;
        if (sel >= items.length) sel = 0;
        items[sel].classList.add('sel');
        items[sel].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        e.preventDefault();
        var t = sel >= 0 ? items[sel] : items[0];
        if (t) location.href = t.getAttribute('href');
      } else if (e.key === 'Escape') { hide(); box.blur(); }
    });
    document.addEventListener('click', function (e) {
      if (!box.contains(e.target) && !sug.contains(e.target)) hide();
    });
  }

  var bound = new WeakSet();
  function initSearch() {
    /* 可能被呼叫兩次：外框插入時一次、頁面渲染完再一次（首頁的大搜尋框是
       render 之後才存在的）。用 WeakSet 記住綁過的，避免重複綁事件。 */
    document.querySelectorAll('.searchbox').forEach(function (bx) {
      if (bound.has(bx)) return;
      bound.add(bx);
      bindSearch(bx);
    });
    var t = document.querySelector('.searchbox.big .qin') ||
      document.querySelector('.searchbox .qin');
    if (!t) return;
    document.addEventListener('keydown', function (e) {
      var a = document.activeElement;
      if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')) return;
      if (e.key === '/') { e.preventDefault(); t.focus(); }
    });
  }

  /* ---------- 啟動 ---------- */
  function boot(active, render) {
    document.body.insertAdjacentHTML('afterbegin', shell(active, qs('c') || ''));
    theme();
    tooltips();
    initSearch();
    get('meta.json').then(function (meta) {
      GLOSSARY = meta.glossary || {};
      staleBanner(meta);
      return render(meta);
    }).then(function () {
      initSearch();          // 頁面渲染後才存在的搜尋框（首頁大框）要補綁
    }).catch(function (e) {
      var w = document.querySelector('.wrap') || document.body;
      w.innerHTML = '<div class="empty"><h2>載入失敗</h2>' +
        '<div class="note">' + esc(e.message) + '<br>' +
        '資料檔可能還沒產生，或這個頁面不是從網站根目錄開啟的。</div></div>';
    });
  }

  window.App = {
    get: get, esc: esc, num: num, pct: pct, cls: cls, yi: yi, qs: qs,
    tip: tip, pager: pager, boot: boot, searchbox: searchbox,
    glossary: function () { return GLOSSARY; }
  };
})();
