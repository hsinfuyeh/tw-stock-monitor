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
  var STAMP = '';        // 由 meta.json 的 built_at 決定，見下方說明

  /* ---------- 取資料 ----------
   * GitHub Pages 對所有檔案送 Cache-Control: max-age=600，也就是部署完之後
   * 瀏覽器與 CDN 最多還會供應 10 分鐘的舊版本 —— 而頁面上看不出來。
   * 對一個每天更新名單的工具來說，那代表你可能盯著昨天的資料做決定。
   *
   * 解法分兩層：
   *   meta.json  用 no-cache 強制向伺服器驗證（只有 13 KB，ETag 命中時不傳內容）
   *   其餘檔案   用 meta 的 built_at 當版本參數 —— 同一次部署內正常快取，
   *              部署一換就自動全部失效
   */
  function get(path) {
    if (!cache[path]) {
      var url = DATA + path + (STAMP ? '?v=' + encodeURIComponent(STAMP) : '');
      cache[path] = fetch(url).then(function (r) {
        if (!r.ok) throw new Error(path + ' ' + r.status);
        return r.json();
      });
    }
    return cache[path];
  }

  function getMeta() {
    return fetch(DATA + 'meta.json', { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) throw new Error('meta.json ' + r.status);
      return r.json();
    }).then(function (m) {
      STAMP = m.built_at || '';
      cache['meta.json'] = Promise.resolve(m);
      return m;
    });
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
  /* 導覽列依「每天用到的頻率」排：今日名單每天看；名單成績每週看；
     回測報告是查資料用的。其他排行、中長期候選池不常用，收進「更多」。
     查個股不佔一格：頂部的搜尋框隨時可用，品牌名稱回首頁。 */
  var NAV = [
    ['index.html', '首頁', 'home'],
    ['short.html', '今日名單', 'short'],
    ['history.html', '名單成績', 'history'],
    ['journal.html', '交易紀錄', 'journal'],
    ['backtest.html', '回測報告', 'backtest']
  ];
  var MORE = [
    ['lists.html', '其他排行', 'lists', '營收優於預期、成交金額、法人買賣…'],
    ['funnel.html', '中長期候選池', 'funnel', '一層層刷掉不適合長抱的股票']
  ];
  // 本機（python server.py）才有後端：可以按鈕更新、用自訂參數回測
  var LOCAL = !/github\.io$/.test(location.hostname);

  function shell(active, q) {
    var links = NAV.map(function (n) {
      // 首頁在手機上不佔一格（點左上角的品牌就是回首頁），否則一行放不下
      return '<a class="navlink' + (n[2] === active ? ' on' : '') +
        (n[2] === 'home' ? ' navhome' : '') + '" href="' + n[0] + '">' + n[1] + '</a>';
    }).join('');
    var inMore = MORE.some(function (n) { return n[2] === active; });
    var more = '<details class="navmore"><summary class="navlink' + (inMore ? ' on' : '') + '">更多 ▾</summary>' +
      '<div class="menu">' + MORE.map(function (n) {
        return '<a href="' + n[0] + '"' + (n[2] === active ? ' class="on"' : '') + '><b>' + n[1] +
          '</b><span>' + n[3] + '</span></a>';
      }).join('') + '</div></details>';
    return '<div class="top"><div class="in">' +
      '<a class="brand" href="index.html">台股觀測</a><nav class="navs">' + links + '</nav>' + more +
      '<button id="updbtn2" class="themebtn" type="button" title="立即更新資料">⟳</button>' +
      '<button id="themebtn" class="themebtn" type="button">☾</button>' +
      '</div></div><div class="stale" id="updbar" data-empty="1"><div class="in"></div></div>' +
      '<div class="stale" id="updmsg" data-empty="1"><div class="in"></div></div>';
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
    // 自動更新在台北 14:40 起每小時試一次，最後一次 19:10。
    // 過了 20:00 當天資料還沒上來，才代表真的有問題 —— 在那之前跳橫幅，
    // 只是在提醒一件排程馬上就會自己處理的事，會訓練人忽略這條橫幅。
    var edge = new Date(now);
    if (now.getHours() < 20) edge.setDate(edge.getDate() - 1);
    edge.setHours(0, 0, 0, 0);
    var n = 0, d = new Date(last);
    d.setDate(d.getDate() + 1);
    while (d <= edge) {
      if (d.getDay() !== 0 && d.getDay() !== 6) n++;
      d.setDate(d.getDate() + 1);
    }
    if (n < 1) return;
    bar.removeAttribute('data-empty');
    /* 走到這裡代表晚上 8 點過後當天資料還沒上來 —— 排程五次都沒成功，
       或排程被停用（GitHub 對 60 天沒有 commit 的公開 repo 會自動停用排程）。
       連結指向 Actions 的 workflow 頁：可以手動觸發，也可以在那裡重新啟用排程。
       只有 repo 擁有者按得動，一般訪客點進去只看得到執行紀錄。 */
    bar.querySelector('.in').innerHTML =
      '<b>資料落後 ' + n + ' 個營業日</b><span>最新是 ' + esc(meta.data_date) +
      '。每個交易日收盤後會自動更新；中間若有國定假日屬正常，' +
      '否則是自動更新沒有成功，可以手動更新。</span>' + (LOCAL
        ? '<button class="updbtn" id="updgo" type="button">立即更新</button>'
        : '<a class="updbtn" href="https://github.com/hsinfuyeh/tw-stock-monitor/actions/workflows/update.yml"' +
          ' target="_blank" rel="noopener">前往更新</a>');
    var go = document.getElementById('updgo');
    if (go) go.addEventListener('click', function () {
      go.disabled = true;
      fetch('api/update', { method: 'POST' }).then(function () { watchUpdate(); });
    });
  }

  /* 本機更新進度。更新要重建資料庫再重新產生網頁（合計約 5-8 分鐘），
     期間頂部顯示進度，完成後自動重新載入。公開網站沒有後端，不會走到這裡。 */
  function watchUpdate() {
    var bar = document.getElementById('updbar');
    function tick() {
      fetch('api/update-status', { cache: 'no-store' }).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (st) {
        if (!st) return;
        if (st.state === 'running') {
          bar.removeAttribute('data-empty');
          bar.className = 'stale busy';
          bar.querySelector('.in').innerHTML = '<span class="spin"></span><b>' +
            esc(st.msg || '更新中') + '</b><span>' + esc(st.detail || '') + '</span>';
          setTimeout(tick, 3000);
        } else if (st.state === 'error') {
          bar.removeAttribute('data-empty');
          bar.className = 'stale bad';
          bar.querySelector('.in').innerHTML = '<b>' + esc(st.msg || '更新失敗') +
            '</b><span>' + esc(st.detail || '') + '</span>';
        } else if (bar.classList.contains('busy')) {
          location.reload();
        }
      }).catch(function () {});
    }
    tick();
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
    function hide() { clearTimeout(later); if (box) box.classList.remove('show'); cur = null; }
    /* 浮層裡可能有連結（說明文字收進 ⓘ 之後，「見回測報告」這種連結也跟著進去），
       所以滑鼠離開觸發點時稍等一下：移進浮層就不關，浮層本身可以點。 */
    var later = null;
    function hideSoon() { clearTimeout(later); later = setTimeout(hide, 180); }
    document.addEventListener('mouseover', function (e) {
      if (box && box.contains(e.target)) { clearTimeout(later); return; }
      var el = e.target.closest ? e.target.closest('.tip') : null;
      if (el) { clearTimeout(later); if (el !== cur) show(el); }
    });
    document.addEventListener('mouseout', function (e) {
      if (box && box.contains(e.target)) { hideSoon(); return; }
      var el = e.target.closest ? e.target.closest('.tip') : null;
      if (el === cur) hideSoon();
    });
    /* 手機沒有 hover：點一下顯示，再點別處關閉 */
    document.addEventListener('click', function (e) {
      if (box && box.contains(e.target)) return;      // 點浮層裡的連結：照常前往
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

  /* ---------- 精簡版面：說明文字收進 ⓘ ----------
   * 各頁的說明、警示、備註原本整段攤在畫面上，太佔篇幅。這裡在渲染後統一處理：
   *   .lead（卡片開頭的說明）      整段收進標題旁的 ⓘ
   *   .statusbar / .note / 檢查項   只留第一句（重點或數字），其餘收進 ⓘ
   *   .tiphint（「虛線底線可以查」） 拿掉 —— 虛線本身就是提示
   * 不動的：含按鈕／連結／輸入框的區塊（浮層滑鼠一離開就關，裡面的東西點不到）、
   * 有 id 的區塊（程式會回頭寫入）、標了 .keep 的，以及本來就很短的。
   * 警示的第一句一定留在畫面上：「未驗證」「觸發緊急停止」這種字不能只藏在浮層裡。
   * 用 DOM 切句而不是改各頁的文字 —— 各頁字串有一大半是 Python 端產的，
   * 兩邊各改一份遲早會不一致。 */
  var CUT_MIN = 10;     // 剩下的字少於這個就不收，收起來反而多一步
  function infoIcon(html) {
    var s = document.createElement('span');
    s.className = 'tip infoi';
    s.setAttribute('tabindex', '0');
    s.setAttribute('role', 'button');
    s.setAttribute('aria-label', '說明');
    s.setAttribute('data-tip', html);
    s.textContent = 'i';
    return s;
  }
  // 連結可以進浮層（浮層可點）；按鈕、輸入框、表格、圖不行 —— 那些要留在頁面上操作
  function interactive(el) {
    return !!el.querySelector('button,input,select,textarea,details,table,svg');
  }
  /* 浮層裡不能再套浮層（滑鼠一移進去外層就關了）：名詞解釋的虛線字改回純文字 */
  function tipHTML(el) {
    var c = el.cloneNode(true);
    c.querySelectorAll('.tip').forEach(function (t) {
      t.replaceWith(document.createTextNode(t.textContent));
    });
    return c.innerHTML.replace(/^[\s　]+/, '');
  }
  /* 在第一個句號後切開：回傳 [前半的節點們, 後半的 HTML]；切不開回傳 null。
     只在最上層切：句號在 <b>…。</b> 裡面時，整個 <b> 算前半。 */
  function splitFirst(el) {
    var nodes = [].slice.call(el.childNodes);
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.nodeType === 3) {
        var k = n.nodeValue.search(/[。！？]/);
        if (k >= 0 && k < n.nodeValue.length - 1) {
          var rest = n.nodeValue.slice(k + 1);
          n.nodeValue = n.nodeValue.slice(0, k + 1);
          var tail = document.createElement('span');
          tail.appendChild(document.createTextNode(rest));
          for (var j = i + 1; j < nodes.length; j++) tail.appendChild(nodes[j]);
          return tail;
        }
        if (k === n.nodeValue.length - 1 && i < nodes.length - 1) {
          var t2 = document.createElement('span');
          for (var j2 = i + 1; j2 < nodes.length; j2++) t2.appendChild(nodes[j2]);
          return t2;
        }
      } else if (n.nodeType === 1 && n.tagName !== 'BR' &&
                 /[。！？]\s*$/.test(n.textContent) && i < nodes.length - 1) {
        var t3 = document.createElement('span');
        for (var j3 = i + 1; j3 < nodes.length; j3++) t3.appendChild(nodes[j3]);
        return t3;
      } else if (n.nodeType === 1 && n.tagName === 'BR' && i > 0) {
        var t4 = document.createElement('span');
        for (var j4 = i + 1; j4 < nodes.length; j4++) t4.appendChild(nodes[j4]);
        el.removeChild(n);
        return t4;
      }
    }
    return null;
  }
  function trimTo(el) {
    if (el.getAttribute('data-compact') || el.id || el.classList.contains('keep') ||
        interactive(el)) return;
    el.setAttribute('data-compact', '1');
    var tail = splitFirst(el);
    if (!tail) return;
    var rest = tail.textContent.replace(/^[\s　]+|[\s　]+$/g, '');
    if (rest.length < CUT_MIN) {           // 太短：放回去
      while (tail.firstChild) el.appendChild(tail.firstChild);
      return;
    }
    el.appendChild(infoIcon(tipHTML(tail)));
  }
  function compact(root) {
    root = root || document;
    root.querySelectorAll('.tiphint').forEach(function (e) { e.remove(); });
    root.querySelectorAll('.lead:not([data-compact])').forEach(function (p) {
      p.setAttribute('data-compact', '1');
      if (p.id || interactive(p)) return;
      var card = p.closest('.card');
      var h = card && card.querySelector('h2');
      if (!h) return trimTo(p);
      h.appendChild(infoIcon(tipHTML(p)));
      p.remove();
    });
    root.querySelectorAll('.statusbar .txt, .note, .chk .s, .stale .in > span, ' +
                          '.card > p:not([class])').forEach(trimTo);
    // 入選理由（今日清單、首頁卡片）：只留一行，完整內容移到浮層
    root.querySelectorAll('td.why, .mcard .w, .hcard .wy').forEach(function (w) {
      if (w.getAttribute('data-compact')) return;
      w.setAttribute('data-compact', '1');
      w.classList.add('tip', 'clamp1');
      w.setAttribute('tabindex', '0');
      w.setAttribute('data-tip', w.innerHTML);
    });
  }
  var _cq = null;
  function watchCompact() {
    compact();
    // 頁面之後還會局部重畫（改參數、切分頁、展開明細），新長出來的也要處理
    new MutationObserver(function () {
      clearTimeout(_cq);
      _cq = setTimeout(function () { compact(); }, 30);
    }).observe(document.body, { childList: true, subtree: true });
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
  var keyBound = false;
  function initSearch() {
    /* 可能被呼叫兩次：外框插入時一次、頁面渲染完再一次（首頁的大搜尋框是
       render 之後才存在的）。用 WeakSet 記住綁過的，避免重複綁事件。 */
    document.querySelectorAll('.searchbox').forEach(function (bx) {
      if (bound.has(bx)) return;
      bound.add(bx);
      bindSearch(bx);
    });
    /* 頂部列沒有搜尋框了（首頁與個股頁各有一個）。按 / 時：
       這一頁有搜尋框就聚焦，沒有就回首頁的大搜尋框。 */
    var t = document.querySelector('.searchbox.big .qin') ||
      document.querySelector('.searchbox .qin');
    if (keyBound) return;
    keyBound = true;
    document.addEventListener('keydown', function (e) {
      var a = document.activeElement;
      if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')) return;
      if (e.key !== '/') return;
      e.preventDefault();
      var box = document.querySelector('.searchbox.big .qin') || document.querySelector('.searchbox .qin');
      if (box) box.focus();
      else location.href = 'index.html';
    });
  }

  /* 頁尾標示這份內容是什麼時候產出的。
     GitHub Pages 有 10 分鐘快取，沒有這個就無法分辨「今天還沒更新」
     和「更新了但我看到的是快取」。 */
  /* 真的重新載入：GitHub Pages 對 html/js/css 有 10 分鐘快取，單純 reload 還是拿到舊的。
     先用 cache:'reload' 把這一頁用到的檔案重抓一次，再載入頁面。
     （這正是「改了樣式或連結，但畫面沒變」的原因。） */
  function hardReload() {
    var files = ['', 'assets/app.js', 'assets/style.css', 'assets/stable.js']
      .map(function (f) { return f ? f : location.pathname; });
    Promise.all(files.map(function (f) {
      return fetch(f, { cache: 'reload' }).catch(function () {});
    })).then(function () { location.reload(); });
  }

  function buildStamp(meta) {
    if (document.getElementById('bstamp')) return;
    var d = document.createElement('div');
    d.id = 'bstamp';
    d.className = 'bstamp';
    d.innerHTML = '資料 ' + esc(meta.data_date) + '　·　產出 ' +
      esc(String(meta.built_at || '').replace('T', ' ')) +
      '　·　<span class="reload">重新載入</span>';
    (document.querySelector('.wrap') || document.body).appendChild(d);
    d.querySelector('.reload').addEventListener('click', function () {
      hardReload();
    });
  }

  /* ---------- 啟動 ---------- */
  function boot(active, render) {
    document.body.insertAdjacentHTML('afterbegin', shell(active, qs('c') || ''));
    document.addEventListener('click', function (e) {
      var m = document.querySelector('.navmore');
      if (m && m.open && !m.contains(e.target)) m.open = false;
    });
    theme();
    tooltips();
    initSearch();
    getMeta().then(function (meta) {
      GLOSSARY = meta.glossary || {};
      staleBanner(meta);
      bindUpdateButton();
      if (LOCAL) watchUpdate();
      var r = render(meta);
      return Promise.resolve(r).then(function () { buildStamp(meta); });
    }).then(function () {
      initSearch();          // 頁面渲染後才存在的搜尋框（首頁大框）要補綁
      watchCompact();
    }).catch(function (e) {
      var w = document.querySelector('.wrap') || document.body;
      w.innerHTML = '<div class="empty"><h2>載入失敗</h2>' +
        '<div class="note">' + esc(e.message) + '<br>' +
        '資料檔可能還沒產生，或這個頁面不是從網站根目錄開啟的。</div></div>';
    });
  }

  /* ---------- 立即更新（右上角的 ⟳）----------
   * 本機版直接打自己的 /api/update。
   * 公開網站是靜態的、沒有後端，所以改成請 GitHub 跑那個自動更新流程
   * （跟每天排程跑的是同一個）。這需要一把只能「觸發這個 repo 的 workflow」
   * 的鑰匙，存在<b>你自己的瀏覽器</b>（localStorage），不會進 repo、
   * 也不會傳給別人。第一次按會請你貼上，之後就記住。
   */
  var GH = { owner: 'hsinfuyeh', repo: 'tw-stock-monitor', wf: 'update.yml' };
  var TOKEN_KEY = 'gh_token';

  function ghToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }

  function msgbar(html, cls) {
    var bar = document.getElementById('updmsg');
    if (!bar) return;
    bar.removeAttribute('data-empty');
    bar.className = 'stale' + (cls ? ' ' + cls : '');
    bar.querySelector('.in').innerHTML = html;
  }

  function askToken() {
    msgbar('<b>需要一把 GitHub 鑰匙才能從網頁按更新</b>' +
      '<span>到 <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" ' +
      'rel="noopener" style="color:#fff;text-decoration:underline">GitHub 建立 Fine-grained token</a>：' +
      'Repository access 選 ' + GH.repo + '，Permissions 只開 <b>Actions: Read and write</b>。' +
      '建好後貼進來，它只存在這台瀏覽器。</span>' +
      '<input id="ghtok" type="password" placeholder="github_pat_…" ' +
      'style="flex:1 0 220px;padding:6px 10px;border-radius:6px;border:none;font:inherit">' +
      '<button class="updbtn" id="ghsave" type="button">儲存並更新</button>');
    document.getElementById('ghsave').addEventListener('click', function () {
      var v = (document.getElementById('ghtok').value || '').trim();
      if (!v) return;
      try { localStorage.setItem(TOKEN_KEY, v); } catch (e) {}
      runUpdate();
    });
  }

  function gh(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({
      'Accept': 'application/vnd.github+json',
      'Authorization': 'Bearer ' + ghToken()
    }, opts.headers || {});
    return fetch('https://api.github.com/repos/' + GH.owner + '/' + GH.repo + path, opts);
  }

  function watchGh(since) {
    gh('/actions/workflows/' + GH.wf + '/runs?per_page=1').then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      var run = j && j.workflow_runs && j.workflow_runs[0];
      if (!run || new Date(run.created_at) < since) {
        return setTimeout(function () { watchGh(since); }, 5000);
      }
      if (run.status !== 'completed') {
        msgbar('<span class="spin"></span><b>GitHub 正在更新資料…</b>' +
          '<span>大約 5–8 分鐘。可以關掉頁面，更新完再回來。</span>' +
          '<a class="updbtn" href="' + run.html_url + '" target="_blank" rel="noopener">看進度</a>',
          'busy');
        return setTimeout(function () { watchGh(since); }, 10000);
      }
      if (run.conclusion === 'success') {
        msgbar('<b>更新完成</b><span>重新載入頁面看最新資料。</span>' +
          '<button class="updbtn" id="reloadbtn" type="button">重新載入</button>', 'ok');
        var rb = document.getElementById('reloadbtn');
        if (rb) rb.addEventListener('click', hardReload);
      } else {
        msgbar('<b>更新沒有成功（' + esc(run.conclusion || '') + '）</b>' +
          '<span>多半是 TWSE 當天的資料還沒發完，晚點再按一次即可。</span>' +
          '<a class="updbtn" href="' + run.html_url + '" target="_blank" rel="noopener">看紀錄</a>',
          'bad');
      }
    }).catch(function () {
      setTimeout(function () { watchGh(since); }, 10000);
    });
  }

  function runUpdate() {
    if (LOCAL) {                       // 本機：叫自己的後端做
      msgbar('<span class="spin"></span><b>開始更新…</b>', 'busy');
      fetch('api/update', { method: 'POST' }).then(function () { watchUpdate(); });
      return;
    }
    if (!ghToken()) return askToken();
    var since = new Date(Date.now() - 60000);
    msgbar('<span class="spin"></span><b>送出更新要求…</b>', 'busy');
    gh('/actions/workflows/' + GH.wf + '/dispatches', {
      method: 'POST', body: JSON.stringify({ ref: 'main' })
    }).then(function (r) {
      if (r.status === 204) return watchGh(since);
      if (r.status === 401 || r.status === 403 || r.status === 404) {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
        return msgbar('<b>這把鑰匙不能用（' + r.status + '）</b>' +
          '<span>可能是權限不足或已過期。重新建一把，Permissions 要開 Actions: Read and write。</span>',
          'bad');
      }
      return r.text().then(function (t) {
        msgbar('<b>觸發失敗（' + r.status + '）</b><span>' + esc(t.slice(0, 160)) + '</span>', 'bad');
      });
    }).catch(function (e) {
      msgbar('<b>連不上 GitHub</b><span>' + esc(e.message) + '</span>', 'bad');
    });
  }

  function bindUpdateButton() {
    var b = document.getElementById('updbtn2');
    if (b) b.addEventListener('click', runUpdate);
  }

  /* 「更多」裡的名單（lists.html 與 funnel.html 共用）。
     桌機是左側清單、手機是下拉選單：原本一排排的分組標籤會隨寬度亂換行，看起來參差不齊。
     每組名單的性質用同一種標籤標出來 —— 「過去有效」跟「只是今天的事實」是兩回事。 */
  var GROUP_BADGE = {
    '中長期': ['long', '中長期：持有幾個月的候選'],
    '過去表現較好': ['ok', '過去有效：之後平均贏過大盤'],
    '今日行情': ['fact', '今天的事實，不是預測'],
    '法人動向': ['fact', '今天的事實，不是預測'],
    '其他': ['fact', '今天的事實，不是預測'],
    '過去表現較差': ['bad', '過去反向：之後平均輸大盤，別照著買']
  };
  function listGroups(meta) {
    var info = {};
    (meta.screens || []).forEach(function (s) { info[s.key] = s; });
    return [['中長期', [['funnel', 'funnel.html', '中長期候選池']]]].concat(
      (meta.tab_groups || []).map(function (g) {
        return [g.label, g.keys.filter(function (k) { return info[k]; }).map(function (k) {
          return [k, 'lists.html?s=' + k, info[k].tab || info[k].title];
        })];
      }));
  }
  function listBadge(meta, current) {
    var gs = listGroups(meta);
    for (var i = 0; i < gs.length; i++)
      for (var j = 0; j < gs[i][1].length; j++)
        if (gs[i][1][j][0] === current) {
          var b = GROUP_BADGE[gs[i][0]] || ['fact', gs[i][0]];
          return '<span class="lbadge ' + b[0] + '">' + esc(b[1]) + '</span>';
        }
    return '';
  }
  function otherTabs(meta, current) {
    var gs = listGroups(meta);
    var side = gs.map(function (g) {
      return '<div class="lgrp"><span class="glabel">' + esc(g[0]) + '</span>' +
        g[1].map(function (t) {
          return '<a href="' + t[1] + '" class="litem' + (t[0] === current ? ' on' : '') +
            (g[0] === '過去表現較差' ? ' bad' : '') + '">' + esc(t[2]) + '</a>';
        }).join('') + '</div>';
    }).join('');
    var sel = '<select class="lsel" aria-label="選擇名單" onchange="location.href=this.value">' +
      gs.map(function (g) {
        return '<optgroup label="' + esc(g[0]) + '">' + g[1].map(function (t) {
          return '<option value="' + t[1] + '"' + (t[0] === current ? ' selected' : '') + '>' + esc(t[2]) + '</option>';
        }).join('') + '</optgroup>';
      }).join('') + '</select>';
    return '<aside class="lside">' + side + '</aside>' + sel;
  }

  window.App = {
    get: get, esc: esc, num: num, pct: pct, cls: cls, yi: yi, qs: qs,
    tip: tip, pager: pager, boot: boot, searchbox: searchbox,
    otherTabs: otherTabs, listBadge: listBadge, local: LOCAL, initSearch: initSearch,
    glossary: function () { return GLOSSARY; }
  };
})();
