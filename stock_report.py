"""個股觀察報告 —— 原 Skill 規格的產出，但修掉它的統計缺陷。

保留原規格要求的一切：
  30 日 K 線、成交量、六種規則、黃色圓點、命中後 1/3/5/10 日報酬、
  歷史案例統計、資料來源與截止日、規則判定說明、單一 HTML。

修掉的三個缺陷：
  1. 原規格只報「命中後平均報酬」，沒有基準線。在多頭樣本裡六條規則的
     絕對報酬必然全正 —— 量到的是大盤 beta，不是型態的資訊量。
     這裡同時呈現絕對報酬與「相對同日全市場中位數」的超額報酬。
  2. 原規格只給平均數。厚尾分布下 n=9 的平均會被單一離群值主導。
     這裡補上中位數、勝率、最差 10% 情境。
  3. 原規格沒有樣本數警告。這裡在 n < 30 時直接標示不具統計意義。
"""
import datetime as dt
import html

import numpy as np
import pandas as pd

import factors
import layout
import webui
from config import REPORTS

RULES = ["K_長紅", "K_長黑", "K_十字", "K_爆量", "K_突破20高", "K_跌破20低"]
LABEL = {"K_長紅": "長紅K", "K_長黑": "長黑K", "K_十字": "十字線",
         "K_爆量": "成交量放大", "K_突破20高": "突破前20日高", "K_跌破20低": "跌破前20日低"}
RULE_DEF = {
    "K_長紅": "(C − O) / O ≥ +1.5%",
    "K_長黑": "(C − O) / O ≤ −1.5%",
    "K_十字": "|C − O| / O ≤ 0.2%",
    "K_爆量": "V > 前20日均量 × 1.5（不含當日）",
    "K_突破20高": "C > 前20日最高價（不含當日）",
    "K_跌破20低": "C < 前20日最低價（不含當日）",
}
HORIZONS = (1, 3, 5, 10)


# --------------------------------------------------------------------- 資料
def prepare(code, cat="common", subcats=None, panel=None):
    """取得個股面板，並附上同日全市場中位數報酬當基準線。

    panel 可傳入已建好的面板重複使用 —— 一次 factors.build() 約 30 秒，
    批次產生 40 份報告時共用同一份面板可省下約 20 分鐘。
    """
    d = panel if panel is not None else factors.build(cat=cat, subcats=subcats)
    if code not in set(d["code"]):
        return None, None
    # 基準線：同日全市場橫斷面中位數（比大盤指數更貼近「其他股票」）
    base = d.groupby("date")[[f"fwd{h}" for h in HORIZONS]].median()
    base.columns = ["base{}".format(h) for h in HORIZONS]
    s = d[d["code"] == code].merge(base, on="date", how="left").sort_values("date")
    for h in HORIZONS:
        s["ex{}".format(h)] = s["fwd{}".format(h)] - s["base{}".format(h)]
    return s, d


def rule_stats(s):
    """每條規則的歷史案例統計。

    基準線要扣兩層，少扣一層就會得到完全錯誤的結論：

      第一層 扣大盤 —— 多頭期間任何規則的絕對報酬都是正的，
                      那是大盤漲跌不是型態的資訊量。
      第二層 扣個股自身漂移 —— 這一層最容易漏。以 2330 為例，它的無條件
                      10 日超額報酬就有 +2.13%（本來就大幅跑贏大盤），
                      所以六條規則命中後全都顯示約 +2%，看起來條條有效。
                      實際上扣掉自身漂移後，資訊量是 -1.29% ~ +0.33%。

    所以真正的訊號 = 命中日平均 − 未命中日平均（同一檔股票內部比較）。
    """
    out = []
    for r in RULES:
        hit = s[s[r] == 1.0]
        miss = s[s[r] != 1.0]
        row = {"規則": LABEL[r], "案例數": len(hit)}
        for h in HORIZONS:
            a = hit["fwd{}".format(h)].dropna()
            e = hit["ex{}".format(h)].dropna()
            m = miss["ex{}".format(h)].dropna()
            row["abs{}".format(h)] = a.mean() if len(a) else np.nan
            row["ex{}".format(h)] = e.mean() if len(e) else np.nan
            # 這一欄才是規則真正的資訊量
            row["net{}".format(h)] = ((e.mean() - m.mean())
                                      if len(e) and len(m) else np.nan)
            row["base{}".format(h)] = m.mean() if len(m) else np.nan
            row["med{}".format(h)] = e.median() if len(e) else np.nan
            row["win{}".format(h)] = (e > 0).mean() * 100 if len(e) else np.nan
            row["n{}".format(h)] = len(e)
            row["p10_{}".format(h)] = e.quantile(0.10) if len(e) else np.nan
        out.append(row)
    return pd.DataFrame(out)


# --------------------------------------------------------------------- 圖
def candles_svg(w30, width=980, ph=260, vh=84, pad=56):
    """30 日 K 線 + 成交量。台股慣例：紅漲綠跌。黃點 = 當日至少命中一條規則。"""
    n = len(w30)
    if n == 0:
        return ""
    hi, lo = w30["high"].max(), w30["low"].min()
    rng = (hi - lo) or 1.0
    hi, lo = hi + rng * 0.08, lo - rng * 0.08
    rng = hi - lo
    vmax = w30["volume"].max() or 1.0
    iw = width - pad * 2
    step = iw / n
    bw = max(2.6, step * 0.58)

    def y(p):
        return pad * 0.5 + (hi - p) / rng * ph

    parts = ['<svg viewBox="0 0 {} {}" width="100%" role="img" '
             'aria-label="最近30個交易日K線與成交量" '
             'xmlns="http://www.w3.org/2000/svg" style="display:block">'
             .format(width, ph + vh + pad * 1.8)]

    # 價格格線
    for k in range(5):
        p = lo + rng * k / 4
        yy = y(p)
        parts.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                     'stroke="var(--line)" stroke-width="1"/>'
                     .format(pad, yy, width - pad, yy))
        parts.append('<text x="{:.1f}" y="{:.1f}" fill="var(--mut)" font-size="12" '
                     'text-anchor="start">{:,.1f}</text>'
                     .format(width - pad + 5, yy + 3, p))

    vtop = pad * 0.5 + ph + 26
    for i, (_, r) in enumerate(w30.iterrows()):
        cx = pad + step * (i + 0.5)
        up = r["close"] >= r["open"]
        col = "var(--up)" if up else "var(--dn)"
        # 影線
        parts.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                     'stroke="{}" stroke-width="1.1"/>'
                     .format(cx, y(r["high"]), cx, y(r["low"]), col))
        # 實體（同價時畫成 1px 橫線）
        y1, y2 = y(max(r["open"], r["close"])), y(min(r["open"], r["close"]))
        bh = max(1.0, y2 - y1)
        fill = col if not up else "none"
        parts.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
                     'fill="{}" stroke="{}" stroke-width="1.1">'
                     '<title>{} O{:,.2f} H{:,.2f} L{:,.2f} C{:,.2f}</title></rect>'
                     .format(cx - bw / 2, y1, bw, bh, fill, col,
                             r["date"].strftime("%Y-%m-%d"),
                             r["open"], r["high"], r["low"], r["close"]))
        # 成交量
        vhh = max(1.0, r["volume"] / vmax * vh)
        parts.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
                     'fill="{}" opacity=".5"/>'
                     .format(cx - bw / 2, vtop + vh - vhh, bw, vhh, col))
        # 黃色圓點：當日至少命中一條規則
        hits = [LABEL[k] for k in RULES if r.get(k) == 1.0]
        if hits:
            parts.append('<circle cx="{:.1f}" cy="{:.1f}" r="3.6" fill="#e8b400" '
                         'stroke="var(--bg)" stroke-width="1"><title>{}：{}</title></circle>'
                         .format(cx, y(r["high"]) - 9,
                                 r["date"].strftime("%Y-%m-%d"), "、".join(hits)))
        # 日期標籤（每 5 根一個）
        if i % 5 == 0 or i == n - 1:
            parts.append('<text x="{:.1f}" y="{:.1f}" fill="var(--mut)" font-size="11.5" '
                         'text-anchor="middle">{}</text>'
                         .format(cx, vtop + vh + 15, r["date"].strftime("%m/%d")))

    parts.append('<text x="{:.1f}" y="{:.1f}" fill="var(--mut)" font-size="12">成交量</text>'
                 .format(pad, vtop - 5))
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------- HTML
def _n(v, nd=2, sign=False, pct=True):
    if v is None or (isinstance(v, float) and (np.isnan(v))):
        return "—"
    s = format(float(v), "+.{}f".format(nd)) if sign else format(float(v), ".{}f".format(nd))
    return s + ("%" if pct else "")


def _cls(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return "up" if v > 0 else ("dn" if v < 0 else "")


def _hits_table(s, last_n=40):
    hit = s[s[RULES].sum(axis=1) > 0].tail(last_n).iloc[::-1]
    rows = []
    for _, r in hit.iterrows():
        names = "、".join(LABEL[k] for k in RULES if r.get(k) == 1.0)
        tds = ['<td>{}</td>'.format(r["date"].strftime("%Y-%m-%d")),
               '<td>{}</td>'.format(html.escape(names)),
               '<td>{:,.2f}</td>'.format(r["close"])]
        for h in HORIZONS:
            a, e = r.get("fwd{}".format(h)), r.get("ex{}".format(h))
            if a is None or (isinstance(a, float) and np.isnan(a)):
                tds.append('<td class="mut">資料不足</td>')
            else:
                tds.append('<td class="{}">{}<span class="ex">{}</span></td>'
                           .format(_cls(a), _n(a, 2, sign=True), _n(e, 2, sign=True)))
        rows.append("<tr>" + "".join(tds) + "</tr>")
    head = "".join("<th>{}日後</th>".format(h) for h in HORIZONS)
    return ('<div class="scroll"><table><thead><tr><th>日期</th><th>規則命中</th>'
            + "<th>{}</th>".format(webui.tip("收盤")) + head + "</tr></thead><tbody>"
            + ("".join(rows) or '<tr><td colspan="7" class="mut">期間內無命中</td></tr>')
            + "</tbody></table></div>")


def _stats_table(st):
    rows = []
    for _, r in st.iterrows():
        n = int(r["案例數"])
        warn = ('<span class="tag w">n&lt;30，不具統計意義</span>' if n < 30 else "")
        tds = ['<td>{}{}</td>'.format(html.escape(r["規則"]), warn),
               "<td>{}</td>".format(n)]
        for h in HORIZONS:
            net = r["net{}".format(h)]
            ex, base = r["ex{}".format(h)], r["base{}".format(h)]
            win = r["win{}".format(h)]
            nn, p10 = int(r["n{}".format(h)]), r["p10_{}".format(h)]
            if nn == 0:
                tds.append('<td class="mut">資料不足</td>')
                continue
            tds.append(
                '<td class="{}">{}'
                '<span class="ex">{} {} ｜ {} {} ｜ {} {} ｜ {} {} ｜ '
                '{}={}</span></td>'
                .format(_cls(net), _n(net, 2, sign=True),
                        webui.tip("命中"), _n(ex, 2, sign=True),
                        webui.tip("未命中"), _n(base, 2, sign=True),
                        webui.tip("勝率"), _n(win, 0),
                        webui.tip("最差10%"), _n(p10, 1, sign=True),
                        webui.tip("n"), nn))
        rows.append("<tr>" + "".join(tds) + "</tr>")
    head = "".join('<th>{}日 {}</th>'.format(h, webui.tip("資訊量")) for h in HORIZONS)
    return ('<div class="scroll"><table><thead><tr><th>規則</th>'
            + "<th>{}</th>".format(webui.tip("案例數"))
            + head + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")




def build(code, cat="common", subcats=None, out=None, panel=None):
    s, _ = prepare(code, cat, subcats, panel)
    if s is None or len(s) < 30:
        return None
    name = str(s["name"].iloc[-1])
    w30 = s.tail(30)
    st = rule_stats(s)
    asof = s["date"].iloc[-1]
    first, last = w30["close"].iloc[0], w30["close"].iloc[-1]
    r30 = (last / first - 1) * 100
    hit_days = int((w30[RULES].sum(axis=1) > 0).sum())
    mature = int(s["fwd10"].notna().sum())

    stats = [("最新收盤", "{:,.2f}".format(last)),
             ("30日區間報酬", _n(r30, 2, sign=True)),
             ("30日規則命中日", "{} 天".format(hit_days)),
             ("統計樣本", "{:,} 個交易日".format(len(s))),
             ("成熟事件日", "{:,} 個".format(mature))]
    sg = "".join('<div class="stat"><div class="k">{}</div><div class="v">{}</div></div>'
                 .format(k, v) for k, v in stats)

    defs = "".join("<li><strong>{}</strong>：<code>{}</code></li>"
                   .format(LABEL[k], RULE_DEF[k]) for k in RULES)

    back = "ranking_etf.html" if cat == "etf" else "ranking.html"
    parts = [
        layout.head("{} {}｜K線歷史統計".format(code, name)),
        layout.nav("stock", "{:%Y-%m-%d}".format(asof),
                   extra="{} {}".format(code, name)),
        "<div class='wrap'>",
        "<h1>{} {}｜K 線歷史資料統計</h1>".format(html.escape(code), html.escape(name)),
        '<div class="sub">資料截止 {:%Y-%m-%d} ｜ 統計期間 {:%Y-%m-%d} 起 ｜ '
        '<a href="{}">← 回到排序名單</a></div>'.format(asof, s["date"].iloc[0], back),

        '<div class="card warn"><div class="note">'
        "<strong>這是歷史統計，不是預測。</strong> 判斷一條規則有沒有用，要扣掉兩層基準："
        "<br>① <strong>扣大盤</strong> —— 多頭期間任何規則的絕對報酬都是正的，那是大盤漲跌。"
        "<br>② <strong>扣這檔股票自身的漂移</strong> —— 這層最容易漏。"
        "強勢股沒命中規則的日子表現也很好，不扣掉的話六條規則會全都看起來有效。"
        "<br>下方「歷史案例統計」的大字已經扣完兩層（命中日減未命中日），"
        "<strong>那才是規則真正提供的資訊量</strong>。"
        "「規則命中日期」表格的大字是絕對報酬、小字是扣大盤後的超額報酬。"
        "</div></div>",

        '<div class="grid">{}</div>'.format(sg),

        "<h2>最近 30 個交易日</h2>",
        '<div class="card">{}</div>'.format(candles_svg(w30)),
        '<div class="note" style="margin-top:8px">紅漲綠跌（台股慣例）。'
        '<span class="dot"></span>黃點代表當日至少命中一條規則，滑過可看命中哪幾條。</div>',

        "<h2>規則命中日期與後續報酬</h2>",
        _hits_table(s),
        '<div class="note" style="margin-top:8px">大字為絕對報酬，小字為超額報酬。'
        "「資料不足」代表後續交易日尚未走完，<strong>不補 0、不用最後一日代替</strong>。</div>",

        "<h2>歷史案例統計（規則的真實資訊量）</h2>",
        _stats_table(st),
        '<div class="note" style="margin-top:10px">'
        "大字是<strong>命中日減未命中日</strong>——這才是規則真正提供的資訊量。"
        "小字裡的「命中」是相對大盤的超額報酬，「未命中」是這檔股票沒命中時的表現。"
        "<br><strong>為什麼要扣兩層：</strong>只扣大盤還不夠。以本檔為例，"
        "它沒命中任何規則的日子平均也有正的超額報酬（見小字的「未命中」欄）——"
        "那是這檔股票自身的漂移，不是規則的功勞。"
        "六條規則如果都顯示差不多的數字，幾乎可以確定量到的是股票本身，不是型態。</div>",

        "<h2>規則判定定義</h2>",
        '<ul class="note">{}</ul>'.format(defs),
        '<div class="note">O=開盤 H=最高 L=最低 C=收盤 V=成交量。'
        "所有 20 日視窗一律<strong>不含當日</strong>（<code>rows[i-20:i]</code>）——"
        "把當日放進基準會讓突破與量能判定失真。</div>",

        "<h2>怎麼看這份報告</h2>",
        '<ul class="note">',
        "<li>黃色圓點只代表「這一天值得看」，<strong>不是買賣訊號</strong>。</li>",
        "<li>1／3／5／10 日都是<strong>實際交易日</strong>，不是日曆日。</li>",
        "<li><strong>案例數 &lt; 30 的統計不具意義</strong>，已在表上標示。"
        "厚尾分布下少數樣本的平均會被單一離群值主導，所以同時附上中位數、勝率與最差 10% 情境。</li>",
        "<li>報酬已用 TWSE 官方權值+息值做除權息調整；未調整會讓高殖利率股被系統性低估。</li>",
        "<li>鎖漲跌停日已排除（那天實際上買不到／賣不掉）。</li>",
        "<li>本工具僅整理已發生的歷史資料，不預測未來，不構成投資建議。</li>",
        "</ul>",
        "</div>",
        layout.foot(),
    ]
    out = out or (REPORTS / "stock_{}.html".format(code))
    out.write_text("".join(parts), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "2330"
    cat = "etf" if code.startswith("00") else "common"
    sub = factors.ETF_SUBCATS if cat == "etf" else None
    p = build(code, cat=cat, subcats=sub)
    print("已產生: {}".format(p) if p else "找不到 {} 或資料不足".format(code))
