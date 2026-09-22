/* 今日清單的評分規則（首頁與「今日清單」共用）。
 *
 * 這一份必須和 shortterm.py 的 score / pick / ref_prices / reasons <b>完全一致</b>：
 * 頁面載入時會用預設參數與五個方案各跑一次，跟 Python 算出的清單比對，對不上就在畫面上警告。
 * 抽成共用檔案的理由：首頁也要顯示同一份清單，規則寫兩份遲早只改到其中一份。
 */
(function () {
function isNum(v) { return v !== null && v !== undefined && !isNaN(v); }
function f2(v) { return Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function r2(v) { return Math.round(v * 100) / 100; }
function tick(p) {
  return p < 10 ? 0.01 : p < 50 ? 0.05 : p < 100 ? 0.1 : p < 500 ? 0.5 : p < 1000 ? 1 : 5;
}

/* 停損距離 = ATR 的倍數（不是固定百分比）。理由見 shortterm.ref_prices 的說明：
   實測固定百分比的停損越緊越虧，而且每個策略、每個期間都一樣。
   目標價預設不設 —— 設上限會砍掉少數大漲的那幾筆，實測讓平均報酬變差。 */
var STOP_MIN = 0.02, STOP_MAX = 0.10;
var COST = 0.585;     // 來回手續費 0.1425% × 2 ＋ 證交稅 0.3%
function refPrices(close, atrp, p) {
  var dist = isNum(atrp) ? Math.min(Math.max(p.atr_mult * atrp, STOP_MIN), STOP_MAX) : STOP_MAX;
  var stop = close * (1 - dist);
  var t = tick(stop);
  stop = r2(Math.ceil(stop / t - 1e-9) * t);
  if (!p.use_target) return [stop, null];
  /* 目標價 = 進場價 × (1 + 目標淨賺% + 成本%)，賣掉之後才真的淨賺那個數。
     往上取整到檔位，取整後仍確保拿得到設定的淨賺。 */
  var target = close * (1 + (p.target_net + COST) / 100);
  t = tick(target);
  target = r2(Math.ceil(target / t - 1e-9) * t);
  return [stop, target];
}

function scoreRow(r, p) {
  /* 流動性與波動都是門檻不是分數。波動門檻的理由：20 天內要漲 5.6% 才能淨賺 5%，
     平常一天只動 1% 的股票很難做到，但手續費和稅一樣要付。 */
  var liq = isNum(r.vol20_lots) && r.vol20_lots >= p.liq_lots &&
    (!p.min_atrp || (isNum(r.atrp14) && r.atrp14 * 100 >= p.min_atrp));
  /* 第 9 輪的方案（跟 shortterm.score 同一套）：只挑大型股、停損太寬不買 */
  if (p.big_n) liq = liq && isNum(r.adtv_rank) && r.adtv_rank <= p.big_n;
  if (p.skip_wide) liq = liq && isNum(r.atrp14) && r.atrp14 * p.atr_mult <= STOP_MAX;
  var c1 = isNum(r.yoy) && r.yoy >= p.rev_yoy;
  var hi = c1 && r.rev_high12 === 1;
  var c2 = (isNum(r.streak_f) && r.streak_f >= p.inst_days) ||
           (isNum(r.streak_t) && r.streak_t >= p.inst_days);
  var c3 = isNum(r.close) && isNum(r.hi20_prev) && isNum(r.vol5_prev) &&
           r.close > r.hi20_prev && r.volume >= r.vol5_prev * p.brk_vol;
  var c4 = isNum(r.ma5) && isNum(r.ma10) && isNum(r.ma20) &&
           r.close > r.ma5 && r.ma5 > r.ma10 && r.ma10 > r.ma20;
  var bonus = p.rev_bonus / 100;
  var s1 = (c1 ? p.w_rev * (1 - bonus) : 0) + (hi ? p.w_rev * bonus : 0);
  var rest = (c2 ? p.w_inst : 0) + (c3 ? p.w_brk : 0) + (c4 ? p.w_ma : 0);
  var sc;
  if (r.kind === 'ETF') {
    var ef = p.w_inst + p.w_brk + p.w_ma;
    sc = ef ? rest * 100 / ef : 0;
  } else {
    var full = p.w_rev + p.w_inst + p.w_brk + p.w_ma;
    sc = full ? (s1 + rest) * 100 / full : 0;
  }
  /* 模型選股：分數 = 模型估的機率 × 100（ETF 與沒有模型的 0 分） */
  if (p.use_model) sc = isNum(r.mprob) ? r.mprob * 100 : 0;
  return { liq: liq, c1: c1, hi: hi, c2: c2, c3: c3, c4: c4,
           score: Math.round(sc * 10) / 10 };
}

function reasons(r, s, p) {
  var out = [];
  if (p.use_model && isNum(r.mprob))
    out.push('模型估 20 天後贏過 0050 的機率 ' + Math.round(r.mprob * 100) + '%');
  if (s.c1 && p.w_rev > 0)
    out.push('營收年增 ' + Math.round(r.yoy) + '%' + (s.hi ? '（創 12 個月新高）' : ''));
  if (s.c2 && p.w_inst > 0) {
    if (r.streak_t >= p.inst_days) out.push('投信連買 ' + r.streak_t + ' 天');
    if (r.streak_f >= p.inst_days) out.push('外資連買 ' + r.streak_f + ' 天');
  }
  if (s.c3 && p.w_brk > 0)
    out.push('突破前 20 日最高價 ' + f2(r.hi20_prev) + '，成交量是前 5 日平均的 ' +
      (r.volume / r.vol5_prev).toFixed(1) + ' 倍');
  if (s.c4 && p.w_ma > 0)
    out.push('均線多頭排列（5 日均價 ' + f2(r.ma5) + ' > 10 日 ' + f2(r.ma10) +
      ' > 20 日 ' + f2(r.ma20) + '）');
  return out;
}

function rank(T, p) {
  var out = [];
  T.objs.forEach(function (r) {
    var s = scoreRow(r, p);
    if (!s.liq || s.score < p.min_score || s.score <= 0) return;
    out.push({ r: r, s: s });
  });
  out.sort(function (a, b) {
    if (b.s.score !== a.s.score) return b.s.score - a.s.score;
    var x = isNum(a.r.amt20) ? a.r.amt20 : -1, y = isNum(b.r.amt20) ? b.r.amt20 : -1;
    return y - x;
  });
  out = out.slice(0, p.top_n);
  var R = T.risk || {}, seen = {};
  out.forEach(function (o, i) {
    var pr = refPrices(o.r.close, o.r.atrp14, p);
    o.rank = i + 1; o.stop = pr[0]; o.target = pr[1]; o.why = reasons(o.r, o.s, p);
    /* 規範 v1 第 4 節：處置股、注意股不買（跟 forward.py 的 flag_map 同一套）。
       同產業只是提醒：組合裡同產業已有 2 檔時才不買，清單上先標出第幾檔。 */
    o.flags = [];
    if ((R.disposal || []).indexOf(o.r.code) >= 0) o.flags.push('處置股');
    if ((R.attention || []).indexOf(o.r.code) >= 0) o.flags.push('注意股');
    if ((R.full_delivery || []).indexOf(o.r.code) >= 0) o.flags.push('全額交割');
    if (o.r.ind) { seen[o.r.ind] = (seen[o.r.ind] || 0) + 1; o.indn = seen[o.r.ind]; }
  });
  return out;
}

  window.Pick = {
    isNum: isNum, f2: f2, r2: r2, tick: tick, refPrices: refPrices,
    scoreRow: scoreRow, reasons: reasons, rank: rank,
    STOP_MIN: STOP_MIN, STOP_MAX: STOP_MAX, COST: COST
  };
})();
