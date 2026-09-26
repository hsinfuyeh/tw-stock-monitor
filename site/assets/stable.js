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

function card(r) {
  return '<div class="hcard">' +
    '<div class="top"><div class="nm"><span class="rk">' + r.rank + '</span>' +
    '<a href="stock.html?c=' + r.code + '">' + r.code + ' ' + App.esc(r.name) + '</a>' + badge(r) + '</div>' +
    '<div class="sc">' + App.esc(r.ind || '') + '　' + App.num(r.score, 0) + ' 分</div></div>' +
    '<div class="px"><div><span>今天收盤</span>' + f2(r.close) + '</div>' +
    '<div><span>近 20 日</span>' + App.pct(r.ret20, 1) + '</div>' +
    '<div><span>' + App.tip('每天震盪', 'ATR') + '</span>' + App.num(r.atrp, 1) + '%</div></div>' +
    '<div class="wy">' + App.esc((r.why || []).join('；') || '各項分數平均偏高') + '</div></div>';
}

/* 起點：穩定池隨便挑，10 天內收盤漲到 5% 的比例（回測 2008–2025） */
function baseline(T) {
  var b = T.baseline;
  if (!b) return '';
  return '過去 18 年，這份名單平均每 10 檔約有 <b>' + App.num(b.hit / 10, 1) + ' 檔</b>在 10 個交易日內收盤漲到 5%' +
    '（' + App.num(b.hit, 1) + '%），先跌到 −5% 的約 ' + App.num(b.fail, 1) + '%；' +
    '同一個穩定池隨便挑是 ' + App.num(b.pool_hit, 1) + '% 對 ' + App.num(b.pool_fail, 1) + '%。';
}

window.Stable = { f2: f2, card: card, badge: badge, riskNote: riskNote, marginNote: marginNote, baseline: baseline };
})();
