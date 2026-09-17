"""HTML 報告產生器 —— 單一檔案、內嵌 CSS、雙擊即可開啟。

呈現原則：成績單必須跟名單在同一頁，而且要比名單更顯眼。
理由：每天掃 1000 檔，永遠會有東西排在最前面 —— 排序本身不保證有訊息量。
把驗證數字藏進說明文件，等於讓使用者只看到「很篤定的名單」。
"""
import datetime as dt
import html

import numpy as np
import pandas as pd

import layout
from config import REPORTS, COST_ROUND_TRIP



def _fmt(v, nd=2, pct=False, sign=False):
    if v is None:
        return "—"
    if isinstance(v, float) and np.isnan(v):
        return "—"
    if not isinstance(v, (int, float, np.floating, np.integer)):
        return html.escape(str(v))
    s = format(float(v), "+.{}f".format(nd)) if sign else format(float(v), ".{}f".format(nd))
    return s + ("%" if pct else "")


def _cls(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _zbar(v, w=30):
    """因子 z 值小條圖：以中線為基準向兩側發散，方便跨列比較。

    刻意用中性色（實心=正貢獻、淡色=負貢獻）而非紅綠 ——
    同一頁上紅綠已經用來表示報酬漲跌（台股慣例紅漲綠跌），
    因子貢獻度再用紅綠會讓兩種完全不同的意義撞在一起。
    """
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    v = max(-3.0, min(3.0, float(v)))
    px = abs(v) / 3.0 * w
    col = "var(--accent)" if v > 0 else "var(--mut)"
    left = ('<span class="bar" style="width:{:.0f}px;background:{}"></span>'
            .format(px, col) if v < 0 else "")
    right = ('<span class="bar" style="width:{:.0f}px;background:{}"></span>'
             .format(px, col) if v > 0 else "")
    return ('<span style="display:inline-block;width:{w}px;text-align:right">{l}</span>'
            '<span style="display:inline-block;width:1px;height:11px;vertical-align:middle;'
            'background:var(--line)"></span>'
            '<span style="display:inline-block;width:{w}px;text-align:left">{r}</span>'
            ).format(w=w, l=left, r=right)


def _rows(df, factor_cols):
    out = []
    for _, r in df.iterrows():
        amt = float(r["amt20"])
        liq = "佳" if amt >= 100e6 else ("普通" if amt >= 20e6 else "偏低")
        tag = ""
        if liq != "佳":
            cls = "tag w" if liq == "偏低" else "tag"
            tag = '<span class="{}">{}</span>'.format(cls, liq)
        nlim = r.get("limit_20d")
        if nlim is not None and not pd.isna(nlim) and nlim > 0:
            tag += '<span class="tag w">近20日觸及漲跌停 {:.0f} 次</span>'.format(float(nlim))
        cells = [
            '<td><span class="code">{}</span></td>'.format(layout.stock_link(r["code"])),
            '<td><span class="nm">{}</span></td>'.format(html.escape(str(r["name"])[:9])),
            "<td>{}</td>".format(int(r["rank"])),
            '<td class="{}">{}</td>'.format(_cls(r["score"]), _fmt(r["score"], 2, sign=True)),
            "<td>{}</td>".format(_fmt(r["pct"], 1)),
            "<td>{:,.2f}</td>".format(float(r["close"])),
            "<td>{:,.2f}億{}</td>".format(amt / 1e8, tag),
        ]
        for f in factor_cols:
            cells.append("<td>{}</td>".format(_zbar(r.get(f))))
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(out)


def _table(df, factor_cols):
    head = ["代號", "名稱", "名次", "分數", "百分位", "收盤", "20日均額"]
    head += [f.replace("z_", "") for f in factor_cols]
    th = "".join("<th>{}</th>".format(html.escape(c)) for c in head)
    return ('<div class="scroll"><table><thead><tr>' + th
            + "</tr></thead><tbody>" + _rows(df, factor_cols) + "</tbody></table></div>")


def _df_table(df):
    if df is None or not len(df):
        return '<div class="note">尚無足夠資料。</div>'
    cols = list(df.columns)
    th = "".join("<th>{}</th>".format(html.escape(str(c))) for c in cols)
    body = []
    for _, r in df.iterrows():
        tds = []
        for c in cols:
            v = r[c]
            if isinstance(v, (int, float, np.floating, np.integer)) and not pd.isna(v):
                nd = 4 if "IC" in str(c) and "IR" not in str(c) else 2
                tds.append("<td>{}</td>".format(_fmt(float(v), nd)))
            else:
                tds.append("<td>{}</td>".format(html.escape(str(v))))
        body.append("<tr>" + "".join(tds) + "</tr>")
    return ('<div class="scroll"><table><thead><tr>' + th
            + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")


def _profile_block(bt):
    """完整十分位剖面。

    只給 D9−D0 一個數字會誤導：關係非單調時，兩端的差值可能跟整體趨勢相反。
    把十個分位全部畫出來，才看得出來訊號是單調的、還是只有某一端有效、
    或者根本是反向的。
    """
    if not bt or not bt.get("十分位剖面"):
        return ""
    prof = bt["十分位剖面"]
    vals = [prof[k] for k in sorted(prof)]
    lo, hi = min(vals), max(vals)
    span = max(abs(lo), abs(hi)) or 1.0
    bars = []
    for k in sorted(prof):
        v = prof[k]
        w = abs(v) / span * 46
        # 這是報酬，依台股慣例紅漲綠跌，與同一列的數字顏色一致
        col = "var(--up)" if v > 0 else "var(--dn)"
        left = ('<span class="bar" style="width:{:.0f}px;background:{}"></span>'.format(w, col)
                if v < 0 else "")
        right = ('<span class="bar" style="width:{:.0f}px;background:{}"></span>'.format(w, col)
                 if v > 0 else "")
        bars.append(
            "<tr><td>D{}{}</td>"
            '<td><span style="display:inline-block;width:48px;text-align:right">{}</span>'
            '<span style="display:inline-block;width:1px;height:11px;vertical-align:middle;'
            'background:var(--line)"></span>'
            '<span style="display:inline-block;width:48px;text-align:left">{}</span></td>'
            '<td class="{}">{}</td></tr>'.format(
                k, "（分數最低）" if k == 0 else ("（分數最高）" if k == max(prof) else ""),
                left, right, _cls(v), _fmt(v, 3, sign=True)))
    mono = bt.get("單調性")
    warn = ""
    if mono is not None and not pd.isna(mono):
        if mono < -0.3:
            warn = ("<strong>單調性為負（{:+.2f}）：分數越高，後續表現越差。</strong>"
                    "這代表因子方向與這段期間的市場相反，不是「沒有預測力」。"
                    .format(mono))
        elif abs(mono) < 0.3:
            warn = ("單調性接近 0（{:+.2f}）：分數與報酬沒有穩定的單調關係，"
                    "兩端價差這個數字在此情況下不可靠。".format(mono))
        else:
            warn = "單調性為正（{:+.2f}）：分數越高後續表現越好，符合預期方向。".format(mono)
    return ("<h2>十分位剖面</h2>"
            '<div class="scroll"><table><thead><tr><th>分位</th><th>相對表現</th>'
            "<th>平均報酬</th></tr></thead><tbody>" + "".join(bars) + "</tbody></table></div>"
            + ('<div class="note" style="margin-top:8px">{}</div>'.format(warn) if warn else ""))


def _verdict(bt, meta):
    n_ind = int(meta.get("獨立期數", 0) or 0)
    if n_ind < 100:
        return ("", "樣本不足 — 這份名單目前沒有統計依據",
                "綜合分數只累積了 {} 個獨立觀察期，低於可下結論的門檻（100）。"
                "名單可以看，但<strong>不應據以下單</strong>。需要更長的歷史回補。".format(n_ind))
    t = bt.get("t值_NW") if bt else None
    ok = t is not None and not pd.isna(t) and abs(t) >= 2 and (bt.get("年化淨報酬") or 0) > 0
    if ok:
        return ("ok", "已通過驗證 — 但幅度請對照下方數字",
                "綜合分數在非重疊樣本上顯著，且扣除交易成本後仍為正。"
                "請特別注意最大回撤與勝率：這是一個微弱但可測的優勢，不是高勝率工具。")
    mono = bt.get("單調性") if bt else None
    if (mono is not None and not pd.isna(mono) and mono < -0.3
            and t is not None and not pd.isna(t) and abs(t) >= 2):
        return ("bad", "方向相反 — 這組因子在此期間是反指標",
                "十分位剖面呈現<strong>顯著的負單調關係</strong>（單調性 {:+.2f}，t = {:.2f}）："
                "分數越高，後續表現越差。這不是「沒有預測力」，而是"
                "<strong>因子方向與這段期間的市場相反</strong>。"
                "注意：<strong>不能因此直接把符號翻轉</strong> —— 看到結果才決定方向，"
                "同樣是對樣本過擬合。正確做法是用獨立的理論依據重選因子，再做樣本外驗證。"
                .format(mono, t))
    return ("bad", "未通過驗證 — 不建議據以下單",
            "綜合分數在扣除交易成本後未達統計顯著。這代表目前這組因子"
            "<strong>沒有可驗證的預測力</strong>，名單僅供觀察。")


def build(asof, bull, bear, factor_cols, ic_df, bt, meta, out=None, kind="common"):
    vcls, vtitle, vmsg = _verdict(bt, meta)

    stats = []
    if bt:
        stats = [
            ("年化淨報酬", _fmt(bt.get("年化淨報酬"), 2, pct=True)),
            ("Sharpe", _fmt(bt.get("Sharpe"), 2)),
            ("最大回撤", _fmt(bt.get("最大回撤"), 1, pct=True)),
            ("t值 (NW)", _fmt(bt.get("t值_NW"), 2)),
            ("勝率", _fmt(bt.get("勝率"), 1, pct=True)),
            ("換倉期數", str(bt.get("期數", "—"))),
            ("實測換手率", _fmt(bt.get("換手率"), 1, pct=True)),
            ("單調性", _fmt(bt.get("單調性"), 2)),
        ]
    sg = "".join(
        '<div class="stat"><div class="k">{}</div><div class="v">{}</div></div>'.format(k, v)
        for k, v in stats)

    horizon = meta.get("horizon", 10)
    is_etf = kind == "etf"
    page = "ranking_etf.html" if is_etf else "ranking.html"
    title = "ETF 橫斷面排序" if is_etf else "上市普通股橫斷面排序"
    other = ('<a href="ranking.html">看普通股排序 →</a>' if is_etf
             else '<a href="ranking_etf.html">看 ETF 排序 →</a>')
    parts = [
        layout.head("{} {}".format(title, asof)),
        layout.nav(page, asof),
        '<div class="wrap">',
        "<h1>{}</h1>".format(title),
        '<div class="sub">資料截止 {} ｜ 可交易宇宙 {:,} 檔 ｜ 納入因子 {} 個 ｜ {}</div>'.format(
            asof, meta.get("宇宙", 0), len(factor_cols), other),
        '<div class="card verdict {}"><h3>{}</h3><div class="note">{}</div>'.format(vcls, vtitle, vmsg),
        '<div class="grid">{}</div>'.format(sg),
        '<div class="note" style="margin-top:12px">十分位多空、每 {} 交易日換倉（不重疊）、'
        '報酬已橫斷面去均值（市場中性）、已扣除來回交易成本 {:.1f}%'
        '（證交稅 0.3% + 手續費 2×0.1425%）。</div></div>'.format(horizon, COST_ROUND_TRIP * 100),
        _profile_block(bt),
        "<h2>看漲側 — 綜合分數最高</h2>",
        _table(bull, factor_cols),
        "<h2>看跌側 — 綜合分數最低</h2>",
        _table(bear, factor_cols),
        '<div class="note" style="margin-top:10px">台股放空受限（平盤下不得放空之標的、借券成本、'
        "回補風險），看跌側的實務用途主要是<strong>避開</strong>，而非做空獲利。</div>",
        "<h2>各因子驗證明細</h2>",
        _df_table(ic_df),
        "<h2>怎麼讀這份報告</h2>",
        '<ul class="note">',
        "<li><strong>先看最上面的成績單，再看名單。</strong> 分數高不代表會漲 —— 每天掃上千檔，"
        "永遠會有東西排在最前面。名單有沒有用，完全取決於成績單那幾個數字。</li>",
        "<li><strong>分數是相對的。</strong> 百分位才是有意義的單位；分數本身會隨當日橫斷面分布浮動。</li>",
        "<li><strong>因子條圖</strong>顯示該檔在每個因子上的標準化位置"
        "（藍色向右為正貢獻、灰色向左為負）。刻意不用紅綠，因為紅綠在本頁代表報酬漲跌。"
        "可看出上榜原因集中在單一因子還是分散。</li>",
        "<li><strong>十分位剖面</strong>的紅綠依台股慣例：紅為正報酬、綠為負報酬。</li>",
        "<li><strong>合理期待</strong>：台股橫斷面因子 IC 約 0.02–0.05，意思是把勝率從 50% 推到 53%，"
        "不是「找出會漲的股票」。會有連續 6–12 個月失效的期間。</li>",
        "<li><strong>報酬已做除權息調整</strong>（TWSE TWT49U 權值+息值）。未調整會讓高殖利率股"
        "被系統性低估，進而製造出「高殖利率 = 低報酬」的假訊號。</li>",
        "</ul>",
        "<h2>已知限制</h2>",
        '<ul class="note">',
        "<li>universe 由每日快照建構，避免存活者偏誤；但股號回收、公司合併改名尚未完全處理。</li>",
        "<li>尚未納入處置股／注意股名單 —— 分盤交易會破壞成交量語意。</li>",
        "<li>漲跌停鎖死時無法成交，但報酬統計假設一定成交，會高估可得報酬。</li>",
        ("<li>本頁僅含台股一般型與高股息型 ETF。槓桿／反向（期望報酬結構性為負）、"
         "債券／海外（驅動因子不在 TWSE 資料裡）、主動式（歷史過短）皆已排除。"
         "缺折溢價因子，需另接投信投顧公會的淨值資料。</li>" if is_etf else
         "<li>ETF 未納入本排序，另見 ETF 排序頁：其價格被套利機制釘住淨值、"
         "法人買賣超反映造市商申贖而非方向性看法。</li>"),
        '<li>點代號可開啟該檔的<strong>個股 K 線報告</strong>（需先產生，見總覽頁）。</li>',
        "</ul>",
        "</div>",
        layout.foot(),
    ]
    doc = "".join(parts)
    out = out or (REPORTS / page)
    out.write_text(doc, encoding="utf-8")
    return out
