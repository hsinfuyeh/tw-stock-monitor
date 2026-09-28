/* 穩定強勢股名單的畫面元件（首頁與「今日名單」共用）。
 *
 * 名單本身由 stable.py 算好（stable/today.json），這裡只負責畫，不重算任何規則 ——
 * 規則只有 Python 一份，兩邊不會長歪。 */
(function () {
function f2(v) { return v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

/* 上線才有的排除（處置、近 30 日注意股、全額交割）抓不到時要讓人知道，不能當成「沒有」 */
function riskNote(T) {
  var r = T.risk || {}, miss = [];
  if (r.disposal_status && r.disposal_status !== 'ok' && r.disposal_status !== 'partial') miss.push('處置股');
  if (r.attention30_status && r.attention30_status !== 'ok') miss.push('近 30 日注意股');
  if (r.full_status && r.full_status !== 'ok' && r.full_status !== 'current') miss.push('全額交割股');
  return miss.length ? '這次抓不到' + miss.join('、') + '的名單，這幾類沒有排除，請自己確認。' : '';
}

function marginNote(T) {
  if (T.margin_same_day === false)
    return '融資券資料還沒公布（約 21:30），這一版用的是 ' + (T.margin_date || '前一天') +
      ' 的融資餘額；晚上會用完整資料再更新一次。';
  return '';
}

function badge(r) {
  if (r.streak <= 1) return '<span class="nbadge new">新進榜</span>';
  return '<span class="nbadge run">連續 ' + r.streak + ' 天</span>';
}

/* 近 60 日小走勢線（相對今天 = 100）。虛線是 60 天前的位置，看得出這段漲了多少、走得穩不穩。 */
function spark(v) {
  var p = (v || []).map(function (y, i) { return [i, y]; }).filter(function (a) { return a[1] != null; });
  if (p.length < 2) return '';
  var ys = p.map(function (a) { return a[1]; });
  var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys), n = v.length - 1;
  var H = 30, pad = 2, span = (hi - lo) || 1;
  function y(val) { return (pad + (hi - val) / span * (H - 2 * pad)).toFixed(1); }
  var pts = p.map(function (a) { return (a[0] / n * 100).toFixed(1) + ',' + y(a[1]); }).join(' ');
  return '<svg class="spark" viewBox="0 0 100 ' + H + '" preserveAspectRatio="none" aria-label="近 60 日走勢">' +
    '<line x1="0" x2="100" y1="' + y(p[0][1]) + '" y2="' + y(p[0][1]) + '"/>' +
    '<polyline points="' + pts + '"/></svg>';
}

function tags(r) {
  var w = r.why || [];
  if (!w.length) return '<div class="rtags"><span class="rtag">各項分數平均偏高</span></div>';
  return '<div class="rtags">' + w.map(function (t) { return '<span class="rtag">' + App.esc(t) + '</span>'; }).join('') + '</div>';
}

function groups(r) {
  return '<div class="gbars">' + (r.groups || []).map(function (g) {
    return '<div class="b"><span>' + App.esc(g.name) + '</span><span class="t"><i style="width:' +
      Math.max(0, Math.min(100, g.score || 0)) + '%"></i></span><span>' + (g.score == null ? '—' : g.score) + '</span></div>';
  }).join('') + '</div>';
}

/* 排除檢查燈號：綠 = 通過、黃 = 名單抓不到（沒辦法確認）、紅 = 被點名 */
function lamps(r) {
  return '<div class="lamps" title="名單上的股票都通過了這些檢查">' + (r.checks || []).map(function (c) {
    var cls = c.ok === true ? '' : c.ok === null ? ' class="q"' : ' class="x"';
    return '<span' + cls + '>' + App.esc(c.name) + ' ' + App.esc(c.val) + '</span>';
  }).join('') + '</div>';
}

function card(r) {
  return '<div class="hcard">' +
    '<div class="hhd"><div class="nm"><span class="rk">' + r.rank + '</span>' +
    '<a href="stock?c=' + r.code + '">' + r.code + ' ' + App.esc(r.name) + '</a>' + badge(r) + '</div>' +
    '<div class="sc">' + App.esc(r.ind || '') + '　' + App.num(r.score, 0) + ' 分</div></div>' +
    spark(r.spark) +
    '<div class="px"><div><span>今天收盤</span>' + f2(r.close) + '</div>' +
    '<div><span>近 20 日</span>' + App.pct(r.ret20, 1) + '</div>' +
    '<div><span>' + App.tip('每天震盪', 'ATR') + '</span>' + App.num(r.atrp, 1) + '%</div></div>' +
    tags(r) + groups(r) + lamps(r) + '</div>';
}

/* 名單 vs 穩定池隨便挑：中／平／倒三段堆疊橫條（回測 2008–2025） */
function stack(h, f) {
  var fl = Math.max(0, 100 - h - f);
  function seg(cls, v, t) { return '<i class="' + cls + '" style="width:' + v + '%">' + (v >= 9 ? t + ' ' + Math.round(v) + '%' : '') + '</i>'; }
  return '<div class="stk3">' + seg('h', h, '中') + seg('f', fl, '平') + seg('d', f, '倒') + '</div>';
}
function vsBars(T) {
  var b = T.baseline;
  if (!b) return '';
  return '<div class="vs">' +
    '<div class="row"><span><b>這份名單</b></span>' + stack(b.hit, b.fail) + '</div>' +
    '<div class="row"><span>隨便挑</span>' + stack(b.pool_hit, b.pool_fail) + '</div>' +
    '<div class="key"><span><i style="background:var(--up)"></i>' + App.tip('中') + '：10 天內收盤先漲到 +5%</span>' +
    '<span><i style="background:var(--dn)"></i>' + App.tip('倒') + '：先跌到 −5%</span></div></div>';
}
function baseline(T) {
  var b = T.baseline;
  if (!b) return '';
  return '過去 18 年，名單每 10 檔約 <b>' + App.num(b.hit / 10, 1) + ' 檔</b>中、' + App.num(b.fail / 10, 1) +
    ' 檔倒；從同一群穩定的股票隨便挑是 ' + App.num(b.pool_hit / 10, 1) + ' 檔中、' + App.num(b.pool_fail / 10, 1) + ' 檔倒。';
}

window.Stable = { f2: f2, card: card, badge: badge, riskNote: riskNote, marginNote: marginNote,
                  baseline: baseline, vsBars: vsBars, spark: spark };
})();
