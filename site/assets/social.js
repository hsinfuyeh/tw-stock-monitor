/* 社群聲量：按「更新」時，在使用者的瀏覽器裡讀 PTT 股票板、算出每檔股票被提到幾次。
 *
 * 為什麼在瀏覽器做：PTT 整站在 Cloudflare 後面，GitHub 的雲端主機怎麼連都是 403，
 * 但從一般使用者的瀏覽器透過自己的 Cloudflare Worker 代讀是通的（cloudflare/worker.js 的 /ptt/…）。
 * 不登入、不用任何帳號。算好的次數跟著「更新」一起送給 Worker，轉交 GitHub，
 * 由 social.apply_payload 檢查後併進 snapshots/social/ptt.json。
 *
 * 比對是照 social.py 的 Matcher 重寫的一份。規則（股票名稱、只認代號的日常用語、補回的全名）
 * 從 data/social_rules.json 讀，不在這裡抄第二份；那個檔還附了幾句話的正確答案，
 * 開始抓之前先對一次（selfCheck），對不上就不送 —— 兩邊算法悄悄漂移比不更新更糟。
 */
(function () {
  'use strict';

  var DAYS = 3;            // 讀最近幾天發的文章（推文還在長的範圍）
  var PARALLEL = 4;        // 同時幾個請求。PTT 是別人的站，不要一次全打出去
  var MAX_ARTICLES = 400;  // 保險上限；股票板一天約 35–50 篇

  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  /* ---------- 比對（對應 social.Matcher） ---------- */
  function Matcher(rules) {
    var names = rules.names, amb = {}, byName = {};
    (rules.ambiguous || []).forEach(function (n) { amb[n] = 1; });
    Object.keys(names).forEach(function (c) {
      var n = String(names[c]).trim();
      if (n.length >= 2 && !amb[n] && !byName[n]) byName[n] = c;
    });
    Object.keys(rules.aliases || {}).forEach(function (n) {
      if (names[rules.aliases[n]]) byName[n] = rules.aliases[n];
    });
    var list = Object.keys(byName).sort(function (a, b) { return b.length - a.length; });   // 長的先比
    this.names = names;
    this.byName = byName;
    this.nameRe = new RegExp(list.map(esc).join('|'), 'g');
    // 代號都是 4 位數字。前後不能是數字、「/」「-」「.」「:」「$」，後面不能接年、點、元、張這類單位
    this.codeRe = /(?<![\d\/\-.:$])(\d{4})(?![\d\/\-.:年月日點元塊張萬億%])/g;
    this.strip = rules.strip || [];
  }
  Matcher.prototype.find = function (text) {
    var self = this, out = {}, named = {}, m;
    this.strip.forEach(function (w) { text = text.split(w).join(' '); });
    (text.match(this.nameRe) || []).forEach(function (n) { named[self.byName[n]] = 1; out[self.byName[n]] = 1; });
    this.codeRe.lastIndex = 0;
    while ((m = this.codeRe.exec(text))) {
      var c = m[1];
      if (!this.names[c]) continue;
      // 長得像年份的代號（2022、2027…）要同一段文字也出現名稱才算
      var y = +c;
      if (y >= 1990 && y <= 2035 && !named[c] && text.indexOf(this.names[c]) < 0) continue;
      out[c] = 1;
    }
    return Object.keys(out);
  };

  function selfCheck(matcher, rules) {
    var bad = [];
    (rules.cases || []).forEach(function (k) {
      var got = matcher.find(k.text).sort().join(','), want = (k.codes || []).slice().sort().join(',');
      if (got !== want) bad.push(k.text + '：網頁算出 [' + got + ']，應該是 [' + want + ']');
    });
    return bad;
  }

  /* ---------- 讀 PTT ---------- */
  function getDoc(base, path) {
    return fetch(base + '/ptt' + path, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) { var e = new Error('PTT ' + r.status); e.status = r.status; throw e; }
      return r.text();
    }).then(function (t) { return new DOMParser().parseFromString(t, 'text/html'); });
  }

  // 台北日期（不管使用者的電腦在哪個時區）
  function twDate(ms) { return new Date(ms + 8 * 3600e3).toISOString().slice(0, 10); }

  /* 列表頁：[{id, epoch}]、上一頁的路徑。分隔線以下是置底公告，不算。 */
  function parseIndex(doc) {
    var items = [], nodes = doc.querySelectorAll('.r-list-container > *');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.classList.contains('r-list-sep')) break;
      var a = n.querySelector && n.querySelector('.title a');
      var m = a && /\/bbs\/Stock\/(M\.(\d+)\.A\.[0-9A-F]+)\.html/.exec(a.getAttribute('href') || '');
      if (m) items.push({ id: m[1], epoch: +m[2] });
    }
    var prev = null;
    doc.querySelectorAll('a.btn.wide').forEach(function (a) {
      if (a.textContent.indexOf('上頁') >= 0) prev = a.getAttribute('href');
    });
    return { items: items, prev: prev };
  }

  /* 一篇文章：標題、內文（不含推文）、推文 [[日期, 內容]]。對應 social._article。 */
  function parseArticle(doc, epoch) {
    var title = '';
    doc.querySelectorAll('.article-metaline').forEach(function (l) {
      var t = l.querySelector('.article-meta-tag');
      if (t && t.textContent.trim() === '標題') title = l.querySelector('.article-meta-value').textContent.trim();
    });
    var posted = new Date(epoch * 1000 + 8 * 3600e3);      // 用 UTC 的欄位讀，就是台北時間
    var py = posted.getUTCFullYear(), pm = posted.getUTCMonth() + 1;
    var pushes = [];
    doc.querySelectorAll('#main-content .push').forEach(function (p) {
      var c = p.querySelector('.push-content'), t = p.querySelector('.push-ipdatetime');
      var m = t && /(\d{2})\/(\d{2})/.exec(t.textContent);
      if (!c || !m) return;
      var mo = +m[1], d = +m[2], y = py + (mo < pm - 6 ? 1 : 0);      // 12 月的文、1 月的推文
      pushes.push([y + '-' + m[1] + '-' + m[2], c.textContent.replace(/^[:\s]+/, '').trim().slice(0, 200)]);
    });
    var main = doc.querySelector('#main-content');
    var body = '';
    if (main) {
      main = main.cloneNode(true);
      main.querySelectorAll('.push, .article-metaline, .article-metaline-right').forEach(function (e) { e.remove(); });
      body = main.textContent.split('※ 發信站')[0].slice(0, 20000);
    }
    return { date: twDate(epoch * 1000), title: title, body: body, pushes: pushes };
  }

  /* 同時最多 PARALLEL 個，一個失敗不影響其他（回 null）。 */
  function pool(tasks, onTick) {
    var out = new Array(tasks.length), next = 0, done = 0;
    function worker() {
      if (next >= tasks.length) return Promise.resolve();
      var i = next++;
      return tasks[i]().then(function (v) { out[i] = v; }, function () { out[i] = null; })
        .then(function () { done++; if (onTick) onTick(done, tasks.length); return worker(); });
    }
    var ws = [];
    for (var k = 0; k < Math.min(PARALLEL, tasks.length); k++) ws.push(worker());
    return Promise.all(ws).then(function () { return out; });
  }

  /* 主流程。回傳 {days: {日期: {代號: [文章數, 推文數]}}, articles: {日期: 篇數}}。
     任何一步失敗就丟例外，由呼叫端決定（只更新資料、不帶社群聲量）。 */
  function collect(base, rulesUrl, onProgress) {
    var say = onProgress || function () {};
    return fetch(rulesUrl).then(function (r) {
      if (!r.ok) throw new Error('讀不到比對規則（' + r.status + '）');
      return r.json();
    }).then(function (rules) {
      var matcher = new Matcher(rules);
      var bad = selfCheck(matcher, rules);
      if (bad.length) throw new Error('比對規則跟伺服器不一致：' + bad[0]);
      var cutoff = Date.now() / 1000 - DAYS * 86400, todo = [], pages = 0;

      function walk(path) {
        say('讀取 PTT 文章列表（第 ' + (pages + 1) + ' 頁）');
        return getDoc(base, path).then(function (doc) {
          var ix = parseIndex(doc);
          pages++;
          if (!ix.items.length) return;
          var oldest = Infinity;
          ix.items.forEach(function (it) {
            oldest = Math.min(oldest, it.epoch);
            if (it.epoch >= cutoff) todo.push(it);
          });
          if (oldest < cutoff || !ix.prev || pages >= DAYS * 15 + 10 || todo.length >= MAX_ARTICLES) return;
          return walk(ix.prev);
        });
      }

      return walk('/bbs/Stock/index.html').then(function () {
        if (!todo.length) throw new Error('PTT 列表裡沒有最近的文章');
        todo = todo.slice(0, MAX_ARTICLES);
        return pool(todo.map(function (it) {
          return function () {
            return getDoc(base, '/bbs/Stock/' + it.id + '.html').then(function (d) { return parseArticle(d, it.epoch); });
          };
        }), function (done, total) { say('讀取 PTT 文章 ' + done + ' / ' + total); });
      }).then(function (arts) {
        var days = {}, count = {}, got = 0;
        function add(day, code, slot) {
          var d = days[day] || (days[day] = {});
          var v = d[code] || (d[code] = [0, 0]);
          v[slot]++;
        }
        arts.forEach(function (a) {
          if (!a) return;
          got++;
          count[a.date] = (count[a.date] || 0) + 1;
          matcher.find(a.title + '\n' + a.body).forEach(function (c) { add(a.date, c, 0); });
          a.pushes.forEach(function (p) { matcher.find(p[1]).forEach(function (c) { add(p[0], c, 1); }); });
        });
        // 抓不到的文章太多（PTT 開始擋、網路不穩）就整批不送：半套的數字會讓「暴增」失真
        if (got < arts.length * 0.8) throw new Error('只讀到 ' + got + ' / ' + arts.length + ' 篇，這次不送');
        return { days: days, articles: count, fetched: got };
      });
    });
  }

  window.Social = { collect: collect, Matcher: Matcher, selfCheck: selfCheck,
                    parseIndex: parseIndex, parseArticle: parseArticle };
})();
