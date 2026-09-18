"""網頁介面的版面與元件。

設計原則（以一般投資人為對象，不是量化研究員）：

  1. 搜尋優先 —— 頂部永遠有搜尋框，隨時可以換股票。
  2. 白話為主 —— 主畫面不出現 IC、單調性、Newey-West 這些詞。
     專業數據收在「詳細數據」裡，想看的人再展開。
  3. 一頁一個重點 —— 不把所有統計倒在同一屏。
  4. 事實核對取代買賣建議 —— 不給「可以買 / 不要買」。
     這個工具已經被驗證無法預測，給結論等於包裝一個無效的東西。
     改成把進場前該確認的事實攤開，判斷交還給使用者。
  5. 誠實優先於好看 —— 訊號沒有預測力這件事要講在顯眼的地方，
     而不是藏在免責聲明裡。
"""
import html

from webui_css import CSS

THEME_JS = """
/* 在 body 渲染前就套用主題，避免切換時閃一下白底 */
(function(){
  try{
    var t=localStorage.getItem('theme');
    if(t==='dark'||t==='light') document.documentElement.setAttribute('data-theme',t);
  }catch(e){}
})();
"""

JS = """
/* ---------- 明暗模式切換 ---------- */
(function(){
  var btn=document.getElementById('themebtn');
  if(!btn) return;
  function sysDark(){ return window.matchMedia &&
    window.matchMedia('(prefers-color-scheme: dark)').matches; }
  function cur(){
    var a=document.documentElement.getAttribute('data-theme');
    return a || (sysDark()?'dark':'light');
  }
  function paint(){ btn.textContent = cur()==='dark' ? '☀' : '☾';
    btn.title = cur()==='dark' ? '切換成淺色模式' : '切換成深夜模式';
    btn.setAttribute('aria-label', btn.title); }
  btn.addEventListener('click', function(){
    var next = cur()==='dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try{ localStorage.setItem('theme', next); }catch(e){}
    paint();
  });
  paint();
})();

/* ---------- 可展開的狀態列 ---------- */
(function(){
  document.addEventListener('click', function(e){
    var m = e.target.closest ? e.target.closest('.statusbar .more') : null;
    if(!m) return;
    var box = document.getElementById(m.getAttribute('data-for'));
    if(!box) return;
    var open = box.classList.toggle('open');
    m.textContent = open ? '收合' : '為什麼？';
  });
})();

/* ---------- 名詞解釋浮層 ---------- */
(function(){
  var box=null, cur=null;
  function ensure(){
    if(!box){ box=document.createElement('div'); box.id='tipbox'; document.body.appendChild(box); }
    return box;
  }
  function show(el){
    var t=el.getAttribute('data-tip'); if(!t) return;
    var b=ensure(); b.innerHTML=t; b.classList.add('show');
    var r=el.getBoundingClientRect(), bw=b.offsetWidth, bh=b.offsetHeight;
    var left=r.left+window.scrollX+r.width/2-bw/2;
    left=Math.max(8, Math.min(left, window.innerWidth-bw-8));
    var top=r.top+window.scrollY-bh-9;
    if(top < window.scrollY+6){ top=r.bottom+window.scrollY+9; }  // 上方放不下就翻到下方
    b.style.left=left+'px'; b.style.top=top+'px';
    cur=el;
  }
  function hide(){ if(box){ box.classList.remove('show'); } cur=null; }
  document.addEventListener('mouseover', function(e){
    var el=e.target.closest ? e.target.closest('.tip') : null;
    if(el && el!==cur) show(el);
  });
  document.addEventListener('mouseout', function(e){
    var el=e.target.closest ? e.target.closest('.tip') : null;
    if(el===cur) hide();
  });
  /* 手機沒有 hover：點一下顯示，再點別處關閉 */
  document.addEventListener('click', function(e){
    var el=e.target.closest ? e.target.closest('.tip') : null;
    if(el){ e.preventDefault(); if(el===cur){ hide(); } else { show(el); } }
    else hide();
  });
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') hide(); });
  window.addEventListener('scroll', hide, {passive:true});
  window.addEventListener('resize', hide);
  /* 鍵盤可及性 */
  document.addEventListener('focusin', function(e){
    var el=e.target.closest ? e.target.closest('.tip') : null; if(el) show(el);
  });
  document.addEventListener('focusout', hide);
})();

/* 頁面上可能有兩個搜尋框（首頁的大框 + 頂部列的小框），
   所以整段包成 bind()，對每個 .searchbox 各跑一次自己的狀態。 */
function bindSearch(bx){
  var box=bx.querySelector('.qin'), sug=bx.querySelector('.sug'), sel=-1, items=[];
  if(!box) return;
  function hide(){ if(sug){sug.innerHTML=''; sug.style.display='none';} sel=-1; items=[]; }
  function render(rows){
    if(!rows.length){ hide(); return; }
    sug.innerHTML = rows.map(function(r){
      return '<a href="/s/'+r.code+'"><span class="c">'+r.code+'</span>'+
             '<span class="n">'+r.name+'</span>'+
             '<span class="t">'+r.kind+'</span></a>';
    }).join('');
    sug.style.display='block';
    items = sug.querySelectorAll('a'); sel=-1;
  }
  var timer=null, lastQ=null, composing=false;
  function query(){
    clearTimeout(timer);
    var v=box.value.trim();
    if(!v){ lastQ=null; hide(); return; }
    if(v===lastQ) return;
    timer=setTimeout(function(){
      lastQ=v;
      fetch('/api/search?q='+encodeURIComponent(v))
        .then(function(r){return r.json();}).then(render).catch(hide);
    }, 90);
  }
  // input 一個事件不夠：用注音／倉頡等輸入法打中文時，組字期間 input 的
  // 行為在各瀏覽器不一致，必須等 compositionend；keyup 則是最後的保險。
  box.addEventListener('input', function(){ if(!composing) query(); });
  box.addEventListener('compositionstart', function(){ composing=true; });
  box.addEventListener('compositionend', function(){ composing=false; query(); });
  box.addEventListener('keyup', function(e){
    if(e.key==='ArrowDown'||e.key==='ArrowUp'||e.key==='Enter'||e.key==='Escape') return;
    if(!composing) query();
  });
  box.addEventListener('focus', function(){ if(box.value.trim()) query(); });
  box.addEventListener('keydown', function(e){
    if(!items.length) return;
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){
      e.preventDefault();
      if(sel>=0) items[sel].classList.remove('sel');
      sel += (e.key==='ArrowDown'?1:-1);
      if(sel<0) sel=items.length-1; if(sel>=items.length) sel=0;
      items[sel].classList.add('sel'); items[sel].scrollIntoView({block:'nearest'});
    } else if(e.key==='Enter'){
      e.preventDefault();
      if(sel>=0) location.href=items[sel].getAttribute('href');
      else if(items.length) location.href=items[0].getAttribute('href');
    } else if(e.key==='Escape'){ hide(); box.blur(); }
  });
  document.addEventListener('click', function(e){
    if(!box.contains(e.target) && sug && !sug.contains(e.target)) hide();
  });
}
(function(){
  var boxes = document.querySelectorAll('.searchbox');
  Array.prototype.forEach.call(boxes, bindSearch);
  // 按 / 聚焦搜尋框：頁面上有大框就用大框，否則用頂部列那個
  var target = document.querySelector('.searchbox.big .qin')
            || document.querySelector('.searchbox .qin');
  if(!target) return;
  document.addEventListener('keydown', function(e){
    var a = document.activeElement;
    if(a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')) return;
    if(e.key==='/'){ e.preventDefault(); target.focus(); }
  });
})();

/* ---------- 資料更新 ----------
   按下按鈕後不等待、不阻塞：後端開背景執行緒去抓，前端每 2 秒問一次進度。
   完成後自動重新載入，使用者不必自己想「好了沒、要不要按 F5」。 */
(function(){
  var bar = document.getElementById('updbar');
  if(!bar) return;
  var inner = bar.querySelector('.in'), timer = null, wasRunning = false;

  function paint(st){
    if(st.state === 'running'){
      wasRunning = true;
      bar.removeAttribute('data-empty');
      bar.className = 'stale busy';
      inner.innerHTML = '<span class="spin"></span><b>更新中</b><span>' +
        (st.msg || '') + (st.detail ? ' — ' + st.detail : '') + '</span>';
      return true;
    }
    if(st.state === 'error'){
      bar.removeAttribute('data-empty');
      bar.className = 'stale bad';
      inner.innerHTML = '<b>更新失敗</b><span>' + (st.detail || '') +
        '</span><button class="updbtn" type="button">重試</button>';
      return false;
    }
    if(wasRunning){ location.reload(); return false; }
    return false;
  }

  function poll(){
    fetch('/api/update-status').then(function(r){ return r.json(); })
      .then(function(st){
        if(!paint(st) && timer){ clearInterval(timer); timer = null; }
      }).catch(function(){});
  }
  function watch(){ if(!timer){ timer = setInterval(poll, 2000); poll(); } }

  bar.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('.updbtn') : null;
    if(!b) return;
    b.disabled = true;
    fetch('/api/update', {method:'POST'}).then(function(r){ return r.json(); })
      .then(function(st){ paint(st); watch(); })
      .catch(function(){ b.disabled = false; });
  });

  /* 開頁時若背景已經在跑（例如 server 剛啟動就自動補），要接上進度 */
  poll();
  fetch('/api/update-status').then(function(r){ return r.json(); })
    .then(function(st){ if(st.state === 'running') watch(); }).catch(function(){});
})();

/* ---------- 表格分頁 ----------
   為什麼做在前端而不是後端加 ?page=2：
     1. 資料量小（最多幾百列），一次送完比多跑一趟 Flask 快得多，
        而且那幾趟都要重算整個面板。
     2. 網址不變 —— 使用者按上一頁是要離開這個榜單，不是退回第 1 頁。
   每張超過 PER 列的表格自動長出分頁列，不需要在產生 HTML 的地方改任何東西；
   想針對單一表格調整每頁列數，在 .scroll 上加 data-per="20" 即可。 */
(function(){
  var PER = 15;
  var tables = document.querySelectorAll('.scroll > table');
  Array.prototype.forEach.call(tables, function(tb){
    var tbody = tb.tBodies[0];
    if(!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    var per = parseInt(tb.parentNode.getAttribute('data-per'), 10) || PER;
    if(rows.length <= per) return;
    var pages = Math.ceil(rows.length / per), cur = 0;

    var box = document.createElement('div');
    box.className = 'pager';
    box.innerHTML =
      '<button class="pgb" type="button" data-d="-1" aria-label="上一頁">\\u2190</button>' +
      '<span class="pgn" aria-live="polite"></span>' +
      '<button class="pgb" type="button" data-d="1" aria-label="下一頁">\\u2192</button>';
    var prev = box.children[0], num = box.children[1], next = box.children[2];
    /* 掛在 .scroll 外面 —— 掛在裡面的話，表格橫向捲動時分頁鈕會跟著捲走 */
    tb.parentNode.parentNode.insertBefore(box, tb.parentNode.nextSibling);

    function paint(){
      var lo = cur * per, hi = Math.min(rows.length, lo + per);
      for(var i=0;i<rows.length;i++){
        rows[i].style.display = (i >= lo && i < hi) ? '' : 'none';
      }
      num.textContent = (lo + 1) + '\\u2013' + hi + ' / ' + rows.length;
      prev.disabled = cur === 0;
      next.disabled = cur >= pages - 1;
    }
    box.addEventListener('click', function(e){
      var b = e.target && e.target.closest ? e.target.closest('.pgb') : null;
      if(!b || b.disabled) return;
      cur = Math.max(0, Math.min(pages - 1, cur + parseInt(b.getAttribute('data-d'), 10)));
      paint();
      /* 換頁後若表頭已經捲出畫面，把它拉回來 ——
         否則按下一頁會停在新一頁的中段，看起來像沒反應 */
      var top = tb.getBoundingClientRect().top + window.scrollY - 86;
      if(window.scrollY > top) window.scrollTo({top: top, behavior: 'smooth'});
    });
    paint();
  });
})();
"""


# 導覽列。以「你想回答哪個問題」分，不是以「用了什麼方法」分。
#   個股   我想查某一檔 —— 首頁（品牌連結）
#   選股   我想找標的 —— 層層剔除後的候選名單
#   盤後   今天市場發生了什麼 —— 價量／位置／籌碼榜單
#   除權息 接下來要發生什麼 —— 事件行事曆
#
# 「層層剔除」原本掛在導覽列上，但那是<方法>不是<目的地>；
# 使用者看到它不知道點進去會拿到什麼。方法名稱移到頁面自己的標題列。
# 「排行榜」也不準確 —— 那頁裡面有排行、有清單、有行事曆、有篩選結果，
# 其中至少三個不是排行。
#
# 除權息原本是第一層，後來拿掉：它只是 14 個榜單裡的一個，
# 而且沒有任何驗證支撐，佔一個第一層的位置不合理。
# 換上「訊號」—— 指向三個通過 t>3.0 的榜單（優於預期 / 動能 / 月營收）。
# 那是整個系統唯一測得出東西的地方，本來被埋在 14 個分頁裡。
NAV = [
    ("/short", "短線", "short"),
    ("/funnel", "選股", "funnel"),
    ("/lists/sue", "訊號", "signal"),
    ("/lists/amount", "盤後", "lists"),
]


def searchbox(value="", big=False):
    """搜尋框。首頁用大的，頂部列用小的 —— 同一套行為，只差尺寸。"""
    ph = ("輸入股票代號或名稱，例如 2330 或 台積電" if big
          else "輸入股票代號或名稱，例如 2330 或 台積電")
    return ('<div class="searchbox{b}">'
            '<input class="qin" autocomplete="off" placeholder="{p}" value="{v}">'
            '<div class="sug" style="display:none"></div></div>').format(
        b=" big" if big else "", p=ph, v=html.escape(value))


_STALE = {}


def stale_banner(remote=False):
    """資料狀態列。落後就顯示，並附「立即更新」按鈕。

    這個工具是拿來決定要不要下單的，顯示過期的價格而不講，比顯示錯誤更糟 ——
    使用者沒有任何理由會去對日期。之前資料卡在同一天五天，是使用者自己發現的，
    程式完全沒有出聲。

    判斷刻意用「營業日」而不是查交易日曆：查日曆要打 TWSE 的 API，
    每次開頁都打一次太貴，而且日曆本身也可能是舊的（那正是當初的 bug）。
    國定假日會讓這裡誤報一天 —— 誤報一次「可能過期」的成本，
    遠低於安靜地給過期數字。

    永遠輸出容器（沒事時是空的），因為手動按更新時要用它顯示進度。

    remote=True（從 Cloudflare Tunnel 進來的訪客）不顯示更新按鈕 ——
    按了也會被後端擋，給一個按不動的按鈕只會讓人困惑。
    """
    import datetime as dt
    body = ""
    try:
        import store
        from config import DB
        stamp = DB.stat().st_mtime_ns if DB.exists() else 0
        if _STALE.get("stamp") != stamp:
            last = store.q("SELECT MAX(date) m FROM quotes").m[0]
            _STALE.clear()
            _STALE["stamp"] = stamp
            _STALE["last"] = dt.date(last.year, last.month, last.day)
        last = _STALE["last"]
        now = dt.datetime.now()
        # 台股 13:30 收盤，當天資料約 14:30 後上架；抓 15:00 保守一點
        edge = now.date() if now.hour >= 15 else now.date() - dt.timedelta(days=1)
        n, d = 0, last + dt.timedelta(days=1)
        while d <= edge:
            if d.weekday() < 5:
                n += 1
            d += dt.timedelta(days=1)
        if n >= 1:
            btn = "" if remote else (
                '<button class="updbtn" type="button">立即更新</button>')
            body = ('<b>資料落後 {n} 個營業日</b>'
                    '<span>最新是 {last:%Y-%m-%d}。中間若有國定假日屬正常。</span>'
                    '{btn}').format(n=n, last=last, btn=btn)
    except Exception:
        pass
    return '<div class="stale" id="updbar"{hide}><div class="in">{b}</div></div>'.format(
        hide="" if body else ' data-empty="1"', b=body)


def page(title, body, q="", nav=None, remote=False):
    links = "".join(
        '<a class="navlink{on}" href="{h}">{t}</a>'.format(
            on=" on" if k == nav else "", h=h, t=t) for h, t, k in NAV)
    return (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        # Inter 與 Playfair Display 只涵蓋拉丁字母與數字（幾十 KB）。
        # 中文字型刻意不從網路載入 —— Noto Sans TC 全字重有好幾 MB，
        # 本機儀表板每次開頁都等它不划算，中文交給系統字型（Windows 上是
        # 微軟正黑體）。襯線那個情緒瞬間顯示的是「數字」，Playfair 完全蓋得到。
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@300;400;500;600&'
        'family=Playfair+Display:wght@400;500&display=swap">'
        "<title>{}</title><script>{}</script><style>{}</style></head><body>".format(
            html.escape(title), THEME_JS, CSS)
        + '<div class="top"><div class="in">'
          '<a class="brand" href="/">台股觀測</a>'
        + links
        + searchbox(q)
        + '<button id="themebtn" class="themebtn" type="button">☾</button>'
          "</div></div>"
        + stale_banner(remote)
        + body
        + "<script>{}</script></body></html>".format(JS))


_SB = [0]


def statusbar(level, text, detail=None):
    """一行狀態列，細節收在可展開區。

    取代原本整塊的警語卡片 —— 重要的話要看得到，但不該佔掉半個螢幕。
    等級：ok / warn / bad / 空字串。
    """
    _SB[0] += 1
    sid = "sb{}".format(_SB[0])
    more = ('<span class="more" data-for="{}">為什麼？</span>'.format(sid)
            if detail else "")
    bar = ('<div class="statusbar {}"><span class="txt">{}</span>{}</div>'
           .format(level, text, more))
    exp = ('<div class="expand" id="{}">{}</div>'.format(sid, detail)
           if detail else "")
    return bar + exp


def icon(level):
    """ok / warn / bad -> 圓形圖示。用符號而非只靠顏色，色盲也能辨識。"""
    ch = {"ok": "✓", "warn": "!", "bad": "✕"}.get(level, "?")
    return '<span class="ico {}">{}</span>'.format(level, ch)


def checklist(items):
    """items: [(level, 標題, 說明, 名詞表key)]。key 可省略。"""
    out = []
    for it in items:
        lv, t, desc = it[0], it[1], it[2]
        key = it[3] if len(it) > 3 else None
        label = tip(t, key) if key else html.escape(t)
        out.append('<li>{}<div class="body"><div class="t">{}</div>'
                   '<div class="s">{}</div></div></li>'.format(icon(lv), label, desc))
    return '<ul class="chk">{}</ul>'.format("".join(out))


def signal_bars(rows, cost_pct=0.6):
    """rows: [(名稱, 數值%)]。以 0 為中線左右發散，並標出交易成本參考線。"""
    if not rows:
        return ""
    span = max(max(abs(v) for _, v in rows), cost_pct * 1.4)
    out = []
    for name, v in rows:
        half = abs(v) / span * 50
        col = "var(--up)" if v > 0 else "var(--dn)"
        style = ("left:50%;width:{:.1f}%".format(half) if v > 0
                 else "right:50%;width:{:.1f}%".format(half))
        cost_l = 50 - cost_pct / span * 50
        cost_r = 50 + cost_pct / span * 50
        out.append(
            '<div class="row"><div class="nm">{}</div>'
            '<div class="track"><div class="zero"></div>'
            '<div class="costline" style="left:{:.1f}%"></div>'
            '<div class="costline" style="left:{:.1f}%"></div>'
            '<div class="fill" style="{};background:{}"></div></div>'
            '<div class="val {}">{:+.2f}%</div></div>'.format(
                html.escape(name), cost_l, cost_r, style, col,
                "up" if v > 0 else "dn", v))
    return '<div class="sig">{}</div>'.format("".join(out))


def tabs(groups, current):
    """分頁列。groups: [(組名, [(key, 標題, 是否警示), ...]), ...]

    原本 12 個分頁平鋪成一長列，使用者得一個個讀完才知道裡面有什麼。
    分組之後，「價量 / 位置 / 籌碼 / 事件」這幾個字本身就說明了整頁的結構，
    而且「排除法」與「反指標」被明確標出來 —— 那兩個的性質跟其他榜單完全不同，
    混在同一列裡會讓人以為它們是同一種東西。
    """
    out = []
    for gname, items in groups:
        inner = "".join(
            '<a href="/lists/{k}" class="{c}">{l}</a>'.format(
                k=k,
                c=(("on " if k == current else "")
                   + ("warnTab" if warn else "")).strip(),
                l=html.escape(label))
            for k, label, warn in items)
        out.append('<div class="tabgrp"><span class="glabel">{}</span>{}</div>'
                   .format(html.escape(gname), inner))
    return '<div class="tabs">{}</div>'.format("".join(out))


# ---------------------------------------------------------------------------
# 名詞解釋。每個出現在畫面上的指標都該在這裡有一條白話說明。
# 寫法原則：先講「這是什麼」，再講「看它要幹嘛」，必要時給對照基準。
# 不要只翻譯名詞（「年化波動＝波動的年化值」這種等於沒解釋）。
GLOSSARY = {
    # --- 交易面 ---
    "20日均額":
        "<b>過去 20 個交易日的平均成交金額。</b>"
        "代表這檔股票平常一天有多少錢在買賣。"
        "數字越大越容易買賣，你的單子也比較不會推動價格。"
        "一般建議單筆不超過日均額的 1%。",
    "成交金額":
        "<b>當天實際成交的總金額。</b>"
        "量大代表市場關注度高，也代表你進得去出得來。",
    "收盤":
        "<b>當天最後的成交價格。</b>台股在 13:30 收盤，"
        "最後 5 分鐘採集合競價，所以你看到收盤價時已經買不到那個價了。",
    "今日":
        "<b>相對前一個交易日收盤價的漲跌幅。</b>"
        "台股慣例紅色為漲、綠色為跌（跟歐美相反）。",
    "流動性":
        "<b>這檔股票好不好買賣。</b>"
        "流動性差的時候：想買買不到理想價、想賣可能要降價求售。"
        "全市場約有四分之一的證券日成交量低到實際上無法交易。",

    # --- 風險面 ---
    "年化波動":
        "<b>股價上下起伏的幅度，換算成一年的尺度。</b>"
        "例如 30% 代表這檔股票一年內上下擺動約三成是常態。"
        "數字越大，同樣金額的部位風險越高，停損也要抓得更寬。",
    "波動度":
        "<b>股價起伏的劇烈程度。</b>"
        "高波動不等於不好，但代表你需要放小部位、並且能承受更大的帳面起伏。",
    "百分位":
        "<b>在全市場中的相對位置。</b>"
        "例如「比市場上 29% 的股票更波動」＝ 有 71% 的股票比它更會震，"
        "所以它其實算穩的。看相對位置比看絕對數字有意義，"
        "因為台股整體波動本來就偏高。",
    "漲跌停":
        "<b>台股單日漲跌幅上限 ±10%。</b>"
        "碰到上限就停在那裡。<b>鎖漲停時你買不到，鎖跌停時你賣不掉</b>——"
        "這是最容易被忽略的風險，因為回測都假設一定能成交。",
    "價格位置":
        "<b>現在的股價落在近一年高低區間的哪個位置。</b>"
        "0% 是一年最低、100% 是一年最高。"
        "<br><br><b>實測：排除位置最低 30% 的股票，剩餘池子的平均報酬 "
        "+2.93%／年（t = 2.96）。</b>四段樣本全部同向，5/10/20 日持有都成立，"
        "對應金融學的「52 週高點動能異常」。<br><br>"
        "但高位置本身<b>不是買進理由</b>——這是排除規則，不是選股規則。",

    # --- 除權息 ---
    "除權息":
        "<b>公司把獲利發還給股東，股價同步扣掉發放的金額。</b>"
        "除息日當天股價往下調整那個幅度，<b>那不是下跌</b>，"
        "錢是進到你口袋了。想參加要在除權息日的<b>前一個交易日</b>收盤前持有。",
    "配發":
        "<b>每股發放的金額（現金股利＋股票股利換算）。</b>"
        "例如配發 4.5 元，你有 1,000 股就領 4,500 元（扣稅前）。",
    "換算幅度":
        "<b>配發金額佔股價的比例。</b>"
        "例如股價 100 元配 5 元就是 5%。"
        "幅度高不一定划算——配息要併入所得課稅，而且股價當天就扣掉了。",

    # --- 型態訊號 ---
    "資訊量":
        "<b>這個型態真正提供的額外資訊。</b>"
        "算法是「型態出現日的表現」減「沒出現日的表現」，"
        "已經扣掉大盤漲跌，也扣掉這檔股票本身的漲跌趨勢。"
        "<b>要跟 0.6% 的來回交易成本比</b>：小於成本就等於沒有可操作性。",
    "交易成本":
        "<b>台股買賣一趟約 0.6%。</b>"
        "證交稅 0.3%（賣出時收）＋ 手續費 0.1425% × 2（買和賣各一次）。"
        "任何訊號的強度如果小於這個數，就算方向猜對也是白忙。",
    "命中":
        "<b>這個型態出現的那些日子，後續的平均表現（已扣大盤）。</b>",
    "未命中":
        "<b>這個型態沒出現的日子，後續的平均表現（已扣大盤）。</b>"
        "這一欄是基準線——強勢股連沒出現型態的日子表現都不錯，"
        "不扣掉的話會誤以為每種型態都有效。",
    "勝率":
        "<b>這個型態出現後，表現優於大盤的比例。</b>"
        "50% 等同丟銅板。要注意勝率高不等於賺錢："
        "可能小贏很多次、大輸一次。",
    "中位":
        "<b>把所有案例排序後正中間的那個值。</b>"
        "跟平均數一起看很重要：兩者差很多代表少數極端值主導了平均，"
        "那個平均就不可靠。",
    "最差10%":
        "<b>表現最差的那 10% 案例大約是什麼程度。</b>"
        "平均數看不到的下檔風險，要看這個。",
    "案例數":
        "<b>歷史上這個型態出現過幾次。</b>"
        "少於 30 次的統計沒有意義——樣本太少時，"
        "任何數字都可能只是運氣。",
    "n":
        "<b>有效樣本數。</b>少於 30 的統計結果不具參考性。",

    # --- 榜單 ---
    "量能倍數":
        "<b>當日成交量相對前 20 個交易日平均量的倍數。</b>"
        "例如 3 倍代表今天的量是平常的三倍。"
        "爆量代表有事發生，但<b>不代表是好事</b>——"
        "本系統實測爆量後的表現反而略差。",
    "超出前高":
        "<b>收盤價比前 20 個交易日的最高價高出多少。</b>"
        "常被當成突破訊號，但本系統實測資訊量只有 +0.33%，小於交易成本。",
    "低於前低":
        "<b>收盤價比前 20 個交易日的最低價低多少。</b>",
    "綜合分數":
        "<b>多個因子合成的排序分數。</b>"
        "<b>警告：這份分數經 1,869 個交易日回測是反指標</b>——"
        "分數越高後續表現越差（t = −4.44）。不要照著買。",

    # --- K 線 ---
    "今日成交額":
        "<b>當天實際成交的總金額。</b>"
        "跟右邊的「20 日均額」比可以看出今天是不是異常放量。",
    "今日漲幅":
        "<b>今天的漲幅，相對前一交易日收盤。</b>"
        "<b>已經漲完是事實，不是明天會續漲的理由。</b>"
        "本系統實測「突破前 20 日高」這類型態的資訊量只有 +0.33%，小於交易成本。",
    "今日跌幅":
        "<b>今天的跌幅，相對前一交易日收盤。</b>"
        "下跌通常有原因，而那個原因往往還沒反映完——接刀前先查清楚發生什麼事。",
    "還有":
        "<b>距離除權息日還有幾天。</b>"
        "要參加配息必須在除權息日的<b>前一個交易日</b>收盤前持有。",
    "法人買超":
        "<b>三大法人（外資、投信、自營商）今日買賣超，除以該股 20 日均量。</b>"
        "正值代表法人買超、負值賣超；±20% 代表相當於平常一天成交量的兩成。"
        "<br><br><b>這是本系統唯一通過驗證的真訊號</b>："
        "十分位剖面單調遞增（+0.88），樣本外毛價差仍為正（+0.23%/5日）。"
        "<b>但效果小於交易成本</b>（5 日換倉來回約 0.97%），"
        "所以只能當參考資訊，不能當進出訊號。",
    "殖利率位階":
        "<b>現金股利除以股價，看的是全市場排名而非絕對值。</b>"
        "它是估值指標（便宜與否），不只是「能領多少配息」。"
        "<br><br><b>實測：排除殖利率最低 40% 的股票，剩餘池子的中位數報酬 "
        "+5.49%／年（t = 4.69）。</b>注意是<b>中位數</b>——低殖利率股贏的次數少、"
        "但偶爾大贏，平均數被肥尾拉平。散戶不會持有夠多檔去捕捉那條尾巴，"
        "所以中位數才是你會遇到的典型結果。",
    "歷史勝率等級":
        "<b>把全市場依「殖利率位階 + 價格位置」等權排序切成五級，"
        "統計歷史上每一級有多少比例在後續 10 個交易日跑贏市場中位數。</b>"
        "<br><br>五級歷史勝率：46.2% → 48.3% → 50.4% → 51.3% → 53.6%"
        "（梯度 +6.84pp，t = 4.40，五級全單調）。"
        "<br><br><b>這是查表不是預測。</b>最高級也只有 53.6%，"
        "跟丟銅板只差 3.6 個百分點。它的用途是幫你排優先順序，"
        "不是告訴你哪一檔會漲。",
    "月營收年增":
        "<b>當月營收與去年同月相比的成長率。</b>"
        "用來分辨「便宜的好公司」與「價值陷阱」——"
        "殖利率高但營收衰退，通常代表股價下跌才讓殖利率看起來高。"
        "<br><br><b>資料已做 point-in-time 對齊</b>："
        "月營收依法規在次月 10 日前公布，系統只在公布日之後才看得到它，"
        "不會在回測裡偷看未來。",
    "K線":
        "<b>每根代表一個交易日。</b>"
        "上下細線是當天的最高與最低價，中間方塊的上下緣是開盤與收盤價。"
        "台股慣例：紅色代表當天收高於開，綠色代表收低於開。",
    "黃點":
        "<b>當天至少出現一種常見型態。</b>"
        "它只代表「這天有東西可以看」，<b>不是買賣訊號</b>。"
        "滑鼠移到黃點上可以看是哪幾種型態。",
}


TIP_HINT = ('<div class="tiphint">💡 帶有'
            '<span class="mark">虛線底線</span>的名詞，'
            '滑鼠移上去（手機點一下）會顯示白話解釋。</div>')


def tip(text, key=None, cls=""):
    """把名詞包成可 hover / 點擊看解釋的元素。

    key 省略時用 text 本身查名詞表；查不到就原樣輸出，不強行加上虛線底線
    （避免出現「有底線但點了沒反應」這種更糟的體驗）。
    """
    k = key or text
    d = GLOSSARY.get(k)
    if not d:
        return html.escape(str(text))
    return ('<span class="tip {}" data-tip="{}" tabindex="0">{}</span>'
            .format(cls, html.escape(d, quote=True), html.escape(str(text))))
