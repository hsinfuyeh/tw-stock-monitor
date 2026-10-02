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
     回測報告是查資料用的；其他排行（含中長期候選池）放最後。
     首頁只有一個查個股的大搜尋框，品牌名稱也回首頁。
     第 4 欄：除了自己以外，還有哪些頁面算在這一格底下（中長期候選池跟其他排行共用左側清單）。 */
  var NAV = [
    ['./', '首頁', 'home'],
    ['short', '今日名單', 'short'],
    ['history', '名單成績', 'history'],
    ['backtest', '回測報告', 'backtest'],
    ['lists', '其他排行', 'lists', ['funnel']]
  ];
  // 本機（python server.py）才有後端：可以按鈕更新、用自訂參數回測
  var LOCAL = !/github\.io$/.test(location.hostname);

  function shell(active, q) {
    var links = NAV.map(function (n) {
      var on = n[2] === active || (n[3] || []).indexOf(active) >= 0;
      // 首頁在手機上不佔一格（點左上角的品牌就是回首頁），否則一行放不下
      return '<a class="navlink' + (on ? ' on' : '') +
        (n[2] === 'home' ? ' navhome' : '') + '" href="' + n[0] + '">' + n[1] + '</a>';
    }).join('');
    return '<div class="top"><div class="in">' +
      '<a class="brand" href="./">台股觀測</a><nav class="navs">' + links + '</nav>' +
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
    // 休市日（中秋、教師節、春節…）來自 TWSE 公布的開休市日期，由 publish.py 放進 meta.json。
    // 只跳週末的話，連假會被當成「自動更新失敗」、一直叫人手動更新 —— 那幾天根本沒有資料。
    var off = {};
    (meta.holidays || []).forEach(function (h) { off[h] = 1; });
    function ymd(x) {
      return x.getFullYear() + '-' + ('0' + (x.getMonth() + 1)).slice(-2) + '-' + ('0' + x.getDate()).slice(-2);
    }
    var n = 0, d = new Date(last);
    d.setDate(d.getDate() + 1);
    while (d <= edge) {
      if (d.getDay() !== 0 && d.getDay() !== 6 && !off[ymd(d)]) n++;
      d.setDate(d.getDate() + 1);
    }
    if (n < 1) return;
    bar.removeAttribute('data-empty');
    /* 走到這裡代表晚上 8 點過後當天資料還沒上來 —— 排程五次都沒成功，
       或排程被停用（GitHub 對 60 天沒有 commit 的公開 repo 會自動停用排程）。
       按「立即更新」直接更新（公開網站交給 Cloudflare Worker，見下方 runUpdate）。
       排程被停用的話，要到 GitHub 的 Actions 頁重新啟用。 */
    bar.querySelector('.in').innerHTML =
      '<b>資料落後 ' + n + ' 個交易日</b><span>最新是 ' + esc(meta.data_date) +
      '（已扣掉週末與證交所公布的休市日）。每個交易日收盤後會自動更新，' +
      '晚上 8 點還沒更新代表自動更新沒有成功（颱風臨時停市除外），可以手動更新。</span>' + '<button class="updbtn" id="updgo" type="button">立即更新</button>';
    var go = document.getElementById('updgo');
    if (go) go.addEventListener('click', function () {
      go.disabled = true;
      runUpdate();             // 本機打自己的後端，公開網站交給 Cloudflare Worker
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
   *   .lead（卡片開頭的說明）      收進標題旁的 ⓘ
   *   .statusbar / .note / 檢查項   第一句（重點或數字）留著，其餘收進 ⓘ
   *   .tiphint（「虛線底線可以查」） 拿掉 —— 虛線本身就是提示
   * <b>但書一律留在畫面上</b>：含「但」「不代表有效」「不是買賣建議」「運氣」「未驗證」「風險」這類字的句子
   * 不收。舊版只留第一句，結果「通過淘汰關卡」看得到、後面那句「不代表有效」被收起來；
   * 個股頁「十字線 +14.39%，略高於成本」看得到、「只出現 7 次，很可能是運氣」看不到 —— 只看畫面會被誤導。
   * 收進去的只有「怎麼算的」這類方法說明。
   * 不動的：含按鈕／連結／輸入框的區塊（浮層滑鼠一離開就關，裡面的東西點不到）、
   * 有 id 的區塊（程式會回頭寫入）、標了 .keep 的，以及本來就很短的。
   * 用 DOM 切句而不是改各頁的文字 —— 各頁字串有一大半是 Python 端產的，
   * 兩邊各改一份遲早會不一致。 */
  var CUT_MIN = 10;     // 收起來的字少於這個就不收，收起來反而多一步
  var CAVEAT = /但|不過|然而|不代表(有效|會漲|有用)|不是(買|建議|預測|保證)|買賣建議|買進建議|運氣|不大|還沒|判不出|未驗證|僅供參考|風險|賺不回/;
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
  /* 把一個元素的內容切成句子：回傳 [[節點…], …]。只在最上層的文字切（句號在 <b>…。</b>
     裡面時，整個 <b> 算同一句）；<br> 也算斷句（本身丟掉）。會把元素清空，呼叫端負責放回去。 */
  function sentences(el) {
    var out = [], cur = [];
    function end() { if (cur.length) out.push(cur); cur = []; }
    [].slice.call(el.childNodes).forEach(function (n) {
      el.removeChild(n);
      if (n.nodeType === 1 && n.tagName === 'BR') return end();
      if (n.nodeType !== 3) {
        cur.push(n);
        if (/[。！？]\s*$/.test(n.textContent)) end();
        return;
      }
      var t = n.nodeValue, k;
      while ((k = t.search(/[。！？]/)) >= 0) {
        cur.push(document.createTextNode(t.slice(0, k + 1)));
        end();
        t = t.slice(k + 1);
      }
      if (t) cur.push(document.createTextNode(t));
    });
    end();
    return out;
  }
  function textOf(part) {
    return part.map(function (n) { return n.textContent; }).join('');
  }
  function put(el, parts) {
    parts.forEach(function (p) { p.forEach(function (n) { el.appendChild(n); }); });
  }
  /* 分成「留在畫面上」與「收進 ⓘ」：但書一定留；first=true 時第一句也留 */
  function sortOut(parts, first) {
    var keep = [], hide = [];
    parts.forEach(function (p, i) {
      ((first && i === 0) || CAVEAT.test(textOf(p)) ? keep : hide).push(p);
    });
    var span = document.createElement('span');
    put(span, hide);
    var short = span.textContent.replace(/^[\s　]+|[\s　]+$/g, '').length < CUT_MIN;
    return { keep: keep, hide: hide, span: span, short: short };
  }
  function trimTo(el) {
    if (el.getAttribute('data-compact') || el.id || el.classList.contains('keep') ||
        interactive(el)) return;
    el.setAttribute('data-compact', '1');
    var parts = sentences(el);
    var r = sortOut(parts, true);
    if (r.short) return put(el, parts);       // 收起來的太少：照原樣放回去
    put(el, r.keep);
    el.appendChild(infoIcon(tipHTML(r.span)));
  }
  function compact(root) {
    root = root || document;
    root.querySelectorAll('.tiphint').forEach(function (e) { e.remove(); });
    root.querySelectorAll('.lead:not([data-compact])').forEach(function (p) {
      p.setAttribute('data-compact', '1');
      if (p.id || interactive(p)) return;
      var card = p.closest('.card');
      var h = card && card.querySelector('h2');
      if (!h) { p.removeAttribute('data-compact'); return trimTo(p); }
      var parts = sentences(p);
      var r = sortOut(parts, false);
      if (r.short) return put(p, parts);
      h.appendChild(infoIcon(tipHTML(r.span)));
      if (r.keep.length) put(p, r.keep);        // 但書（例如「名單不是買賣建議」）留在標題下
      else p.remove();
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
        return '<a href="stock?c=' + encodeURIComponent(r.code) + '">' +
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
      else location.href = './';
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
  /* 網址不帶 .html（GitHub Pages 會自己對到 short.html 這類檔案）。
     舊書籤還是 xxx.html 或 index.html 的話，網址列直接換成乾淨的版本，不重新載入。 */
  function cleanUrl() {
    var p = location.pathname, c = p.replace(/index\.html$/, '').replace(/\.html$/, '');
    if (c !== p && history.replaceState) {
      try { history.replaceState(null, '', c + location.search + location.hash); } catch (e) {}
    }
  }

  function boot(active, render) {
    cleanUrl();
    document.body.insertAdjacentHTML('afterbegin', shell(active, qs('c') || ''));
    theme();
    tooltips();
    initSearch();
    getMeta().then(function (meta) {
      GLOSSARY = meta.glossary || {};
      staleBanner(meta);
      bindUpdateButton();
      autoRefresh(meta);
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
   * 本機版（python server.py）直接打自己的 /api/update。
   *
   * 公開網站是靜態的、沒有後端，所以交給自己的 Cloudflare Worker（cloudflare/worker.js）：
   *   1. 先在這個瀏覽器裡透過 Worker 讀 PTT 股票板，算出社群聲量（assets/social.js）。
   *      PTT 擋雲端主機，只有從使用者的瀏覽器發起才讀得到，所以這一步只能在這裡做。
   *      讀不到也沒關係，照樣更新其他資料。
   *   2. POST /update：Worker 用它保管的 GitHub 金鑰觸發更新流程（跟每天自動跑的是同一個），
   *      社群聲量跟著一起送過去。
   *   3. GET /status：每 10 秒問一次進度，跑完提示重新載入。
   * 瀏覽器裡沒有任何金鑰，也不需要輸入任何東西（以前要貼一把 GitHub token）。
   */
  var WORKER = 'https://tw-stock-monitor-cron.sam19920516-923.workers.dev';
  // 本機預覽要測公開網站這條路時，網址加 ?cloud=1
  var CLOUD = !LOCAL || /[?&]cloud=1\b/.test(location.search);
  try { localStorage.removeItem('gh_token'); } catch (e) {}     // 以前存在瀏覽器裡的 GitHub 金鑰，用不到了

  function msgbar(html, cls) {
    var bar = document.getElementById('updmsg');
    if (!bar) return;
    bar.removeAttribute('data-empty');
    bar.className = 'stale' + (cls ? ' ' + cls : '');
    bar.querySelector('.in').innerHTML = html;
  }

  function loadScript(src) {
    return new Promise(function (ok, fail) {
      if (window.Social) return ok();
      var s = document.createElement('script');
      s.src = src + (STAMP ? '?v=' + encodeURIComponent(STAMP) : '');
      s.onload = ok;
      s.onerror = function () { fail(new Error('載入 ' + src + ' 失敗')); };
      document.head.appendChild(s);
    });
  }

  /* ---------- Cloudflare Turnstile（真人驗證）----------
   * 更新入口是公開的、不用密碼（使用者的決定），任何人都能用程式一直打、或送假的社群聲量。
   * Turnstile 是 Cloudflare 的免費真人驗證：觸發更新前在背景拿一張一次性的通行證（token），
   * Worker 先拿去 Cloudflare 驗過才觸發。一般瀏覽器不用做任何事；只有被判定可疑時，
   * 右下角才會跳出一個勾選框。
   * TURNSTILE_SITEKEY 是公開的（本來就會出現在網頁裡），密鑰只在 Worker 的 Secret。
   * 空字串 = 還沒啟用：照舊直接送，Worker 沒設密鑰也不檢查。 */
  var TURNSTILE_SITEKEY = '';
  var _ts = null;
  function turnstileReady() {
    if (_ts) return _ts;
    _ts = new Promise(function (ok, fail) {
      if (window.turnstile) return ok(window.turnstile);
      window.__tsLoaded = function () { ok(window.turnstile); };
      var s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__tsLoaded';
      s.async = true;
      s.onerror = function () { _ts = null; fail(new Error('載入 Cloudflare 驗證失敗')); };
      document.head.appendChild(s);
    });
    return _ts;
  }
  /* 拿一張通行證（每次觸發都要新的一張，用過就失效、5 分鐘過期） */
  function humanToken() {
    if (!TURNSTILE_SITEKEY) return Promise.resolve('');
    return turnstileReady().then(function (ts) {
      return new Promise(function (ok, fail) {
        var box = document.createElement('div');
        box.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:9999';
        document.body.appendChild(box);
        var done = false, id = null;
        var timer = setTimeout(function () { finish(fail, new Error('Cloudflare 驗證逾時')); }, 30000);
        function finish(f, v) {
          if (done) return;
          done = true;
          clearTimeout(timer);
          try { if (id !== null) ts.remove(id); } catch (e) {}
          box.remove();
          f(v);
        }
        id = ts.render(box, {
          sitekey: TURNSTILE_SITEKEY, action: 'update', appearance: 'interaction-only',
          callback: function (tok) { finish(ok, tok); },
          'error-callback': function () { finish(fail, new Error('沒有通過 Cloudflare 的真人驗證')); return true; }
        });
      });
    });
  }

  function runLink(run) {
    return run && run.url ? '<a class="updbtn" href="' + esc(run.url) + '" target="_blank" rel="noopener">看進度</a>' : '';
  }

  /* 問 Worker 目前的進度。since：這次觸發的時間，比它舊的執行不是我們這一次。 */
  function watchCloud(since, note) {
    fetch(WORKER + '/status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      var run = j && j.run;
      if (!run || new Date(run.created_at) < since) {
        return setTimeout(function () { watchCloud(since, note); }, 5000);
      }
      if (run.status !== 'completed') {
        msgbar('<span class="spin"></span><b>正在更新資料…</b>' +
          '<span>大約 5–8 分鐘。可以關掉頁面，更新完再回來。' + (note || '') + '</span>' + runLink(run), 'busy');
        return setTimeout(function () { watchCloud(since, note); }, 10000);
      }
      if (run.conclusion === 'success') {
        msgbar('<b>更新完成</b><span>重新載入頁面看最新資料。' + (note || '') + '</span>' +
          '<button class="updbtn" id="reloadbtn" type="button">重新載入</button>', 'ok');
        var rb = document.getElementById('reloadbtn');
        if (rb) rb.addEventListener('click', hardReload);
      } else {
        msgbar('<b>更新沒有成功（' + esc(run.conclusion || '') + '）</b>' +
          '<span>多半是證交所當天的資料還沒發完，晚點再按一次即可。</span>' +
          (run.url ? '<a class="updbtn" href="' + esc(run.url) + '" target="_blank" rel="noopener">看紀錄</a>' : ''),
          'bad');
      }
    }).catch(function () {
      setTimeout(function () { watchCloud(since, note); }, 10000);
    });
  }

  var updating = false;
  function runUpdate() {
    if (!CLOUD) {                      // 本機：叫自己的後端做
      msgbar('<span class="spin"></span><b>開始更新…</b>', 'busy');
      fetch('api/update', { method: 'POST' }).then(function () { watchUpdate(); });
      return;
    }
    if (updating) return;
    updating = true;
    var note = '';
    msgbar('<span class="spin"></span><b>讀取社群聲量…</b><span>準備中</span>', 'busy');
    loadScript('assets/social.js').then(function () {
      return window.Social.collect(WORKER, DATA + 'social_rules.json', function (txt) {
        msgbar('<span class="spin"></span><b>讀取社群聲量…</b><span>' + esc(txt) +
          '（約 1 分鐘，請不要關掉頁面）</span>', 'busy');
      });
    }).then(function (social) {
      note = '（含 PTT ' + social.fetched + ' 篇文章的社群聲量）';
      return social;
    }, function (e) {
      // PTT 讀不到不擋更新：其他資料照常更新，社群聲量沿用上一次的
      note = '（這次沒有更新社群聲量：' + esc(e.message || String(e)) + '）';
      return null;
    }).then(function (social) {
      msgbar('<span class="spin"></span><b>送出更新要求…</b>', 'busy');
      return humanToken().then(function (tok) {
        var body = social ? { social: { days: social.days, articles: social.articles } } : {};
        if (tok) body.turnstile = tok;
        return fetch(WORKER + '/update', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
      }).then(function (r) { return r.json(); });
    }).then(function (j) {
      updating = false;
      if (j.state === 'started') return watchCloud(new Date(Date.parse(j.since) - 60000), note);
      if (j.state === 'busy') {
        // 已經有一次更新在跑（排程或別的裝置按的）：接著看它的進度
        return watchCloud(new Date(Date.parse(j.run.created_at) - 1000),
          '（已經有一次更新在進行，這次沒有另外觸發）');
      }
      if (j.state === 'cooldown') {
        return msgbar('<b>剛剛才更新過</b><span>兩次更新至少隔 5 分鐘，請在 ' +
          Math.ceil(j.wait / 60) + ' 分鐘後再按。</span>', 'ok');
      }
      msgbar('<b>更新沒有送出</b><span>' + esc(j.message || '') + '</span>', 'bad');
    }).catch(function (e) {
      updating = false;
      msgbar('<b>更新沒有送出</b><span>' + esc(e.message) + '</span>', 'bad');
    });
  }

  /* ---------- 打開網頁時自動更新 ----------
   * 網站沒有任何定時排程（2026-10-01 起）。取而代之的是：有人打開網頁時，照「現在幾點、今天開不開市」
   * 算出網站應該要有哪一天的資料；比那個舊，就自動請 Worker 更新（trigger=cron，比照舊的排程：
   * 證交所還沒發完就安靜略過、今天已發佈過就不重做）。
   *   交易日 14:40 之後：應該有今天的（行情約 14:30 發布，估值與三大法人更晚，還沒齊的話這次會略過）
   *   交易日 21:40 之後：今天的融資券也應該進來了（meta.margin_same_day），沒有就再更新一次
   *   其他時間、週末、休市日：應該有上一個交易日的
   * 同一個瀏覽器 20 分鐘內只自動觸發一次；Worker 那邊另有「5 分鐘內觸發過、或有更新在跑就不觸發」，
   * 所以很多人同時打開也只會跑一次。社群聲量要讀 100 多篇 PTT，不在這裡做，按 ⟳ 才做。
   */
  var AUTO_KEY = 'auto_update_at', AUTO_GAP = 20 * 60e3;

  function twNow() {
    var d = new Date(Date.now() + 8 * 3600e3);          // 用 UTC 的欄位讀，就是台北時間
    return { ymd: d.toISOString().slice(0, 10), min: d.getUTCHours() * 60 + d.getUTCMinutes() };
  }
  function isTradingDay(ymd, off) {
    var w = new Date(ymd + 'T00:00:00Z').getUTCDay();
    return w !== 0 && w !== 6 && !off[ymd];
  }
  function prevTradingDay(ymd, off) {
    var d = new Date(ymd + 'T00:00:00Z');
    for (var i = 0; i < 30; i++) {
      d.setUTCDate(d.getUTCDate() - 1);
      var s = d.toISOString().slice(0, 10);
      if (isTradingDay(s, off)) return s;
    }
    return ymd;
  }
  /* 照現在的時間，網站應該要有的資料日期；margin：那一天的融資券是不是也應該進來了 */
  function expectedData(meta) {
    var off = {}, t = twNow();
    (meta.holidays || []).forEach(function (h) { off[h] = 1; });
    if (isTradingDay(t.ymd, off) && t.min >= 14 * 60 + 40)
      return { date: t.ymd, margin: t.min >= 21 * 60 + 40 };
    return { date: prevTradingDay(t.ymd, off), margin: true };
  }

  function autoRefresh(meta) {
    if (!CLOUD || !meta || !meta.data_date) return;
    var exp = expectedData(meta);
    var behind = meta.data_date < exp.date;
    var noMargin = meta.data_date === exp.date && exp.margin && meta.margin_same_day === false;
    if (!behind && !noMargin) return;
    try {
      var last = +localStorage.getItem(AUTO_KEY) || 0;
      if (Date.now() - last < AUTO_GAP) return;
      localStorage.setItem(AUTO_KEY, String(Date.now()));
    } catch (e) { /* 存不了就照樣觸發，Worker 那邊有冷卻 */ }
    var since = new Date(Date.now() - 60000), was = meta.built_at;
    msgbar('<span class="spin"></span><b>' + (behind ? '網站的資料是 ' + esc(meta.data_date) + '，正在自動更新到最新'
      : '今天的融資券應該公布了，正在自動更新名單') + '…</b><span>大約 5 分鐘，可以繼續瀏覽。</span>', 'busy');
    humanToken().then(function (tok) {
      return fetch(WORKER + '/update', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(tok ? { auto: true, turnstile: tok } : { auto: true })
      });
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (j.state === 'started') return watchAuto(new Date(Date.parse(j.since) - 60000), was);
      if (j.state === 'busy') return watchAuto(new Date(Date.parse(j.run.created_at) - 1000), was);
      hideMsg();                        // 冷卻中或出錯：這是自動的，不打擾使用者
    }).catch(hideMsg);
  }

  function hideMsg() {
    var bar = document.getElementById('updmsg');
    if (bar) bar.setAttribute('data-empty', '1');
  }

  /* 等這次自動更新跑完，再看網站真的變了沒有：證交所還沒發完的話，這次會略過、什麼都沒變 */
  function watchAuto(since, was) {
    fetch(WORKER + '/status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      var run = j && j.run;
      if (!run || new Date(run.created_at) < since || run.status !== 'completed') {
        return setTimeout(function () { watchAuto(since, was); }, 10000);
      }
      return fetch(DATA + 'meta.json', { cache: 'no-cache' }).then(function (r) { return r.json(); }).then(function (m) {
        if (m.built_at !== was) {
          msgbar('<b>已更新到 ' + esc(m.data_date) + ' 的資料</b><span>重新載入頁面就會看到。</span>' +
            '<button class="updbtn" id="reloadbtn" type="button">重新載入</button>', 'ok');
          var rb = document.getElementById('reloadbtn');
          if (rb) rb.addEventListener('click', hardReload);
        } else {
          msgbar('<b>證交所的資料還沒發布完</b><span>晚一點再打開網頁會自動再試，或按右上角 ⟳。</span>', '');
        }
      });
    }).catch(function () { setTimeout(function () { watchAuto(since, was); }, 15000); });
  }

  function bindUpdateButton() {
    var b = document.getElementById('updbtn2');
    if (b) b.addEventListener('click', runUpdate);
  }

  /* 「其他排行」的左側清單（lists 與 funnel 共用）。
     桌機是左側清單、手機是下拉選單：原本一排排的分組標籤會隨寬度亂換行，看起來參差不齊。
     每組名單的性質用同一種標籤標出來 —— 「過去有效」跟「只是今天的事實」是兩回事。 */
  var GROUP_BADGE = {
    '中長期': ['long', '中長期：持有幾個月的候選'],
    '過去表現較好': ['ok', '過去有效：之後平均贏過大盤'],
    '今日行情': ['fact', '今天的事實，不是預測'],
    '法人動向': ['fact', '今天的事實，不是預測'],
    '其他': ['fact', '今天的事實，不是預測'],
    '社群聲量': ['fact', '今天的事實，不是預測'],
    '過去表現較差': ['bad', '過去反向：之後平均輸大盤，別照著買']
  };
  function listGroups(meta) {
    var info = {};
    (meta.screens || []).forEach(function (s) { info[s.key] = s; });
    return [['中長期', [['funnel', 'funnel', '中長期候選池']]]].concat(
      (meta.tab_groups || []).map(function (g) {
        return [g.label, g.keys.filter(function (k) { return info[k]; }).map(function (k) {
          return [k, 'lists?s=' + k, info[k].tab || info[k].title];
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
