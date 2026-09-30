"""正確性測試。

跑法： python tests.py

這些不是形式主義。系統會用來決定真實下單，而這類管線最危險的 bug
（正負號反了、除權息沒調整、look-ahead）都不會報錯，只會靜靜地產出
漂亮又完全錯誤的數字。每一條測試都對應一個實際踩過的坑。
"""
import sys

import numpy as np
import pandas as pd

import factors
import parse
import store

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print("  [{}] {}{}".format(mark, name, ("  -> " + detail) if detail else ""))


# ---------------------------------------------------------------- 解析層
def test_parse():
    print("\n解析層")
    check("HTML 正負號：綠色減號 -> -1",
          parse.parse_sign("<p style= color:green>-</p>") == (-1, False))
    check("HTML 正負號：紅色加號 -> +1",
          parse.parse_sign("<p style= color:red>+</p>") == (1, False))
    check("除權息標記 X 被辨識",
          parse.parse_sign("<p>X</p>") == (0, True))
    check("空白 -> 無漲跌",
          parse.parse_sign("<p> </p>") == (0, False))

    check("千分位逗號", parse.num("1,234,567") == 1234567.0)
    check("虧損公司本益比 '-' 視為缺值（不是 0）", parse.num("-") is None)
    check("空字串視為缺值", parse.num("") is None)

    check("四碼 -> 普通股", parse.classify("2330", "台積電") == ("common", "common"))
    check("槓桿 ETF 被分出來", parse.classify("00631L", "元大台灣50正2")[1] == "leveraged")
    check("反向 ETF 被分出來", parse.classify("00632R", "元大台灣50反1")[1] == "inverse")
    check("債券 ETF 被分出來", parse.classify("00710B", "復華彭博非投等債")[1] == "bond")
    check("海外標的被分出來", parse.classify("00636", "國泰中國A50")[1] == "foreign")
    check("特別股被分出來", parse.classify("2891B", "中信金乙特")[0] == "preferred")
    check("民國年轉換", parse.roc_to_iso("115/09/11") == "2026-09-11")
    # 權證單日 1.4 萬檔，誤判為 ETF 會讓倉儲膨脹數十倍並汙染 ETF 宇宙
    check("權證不被誤判為 ETF",
          all(parse.classify(c, "")[0] == "warrant"
              for c in ("058158", "061821", "074397", "063254", "030001")))
    check("ETF 仍正確辨識（00 開頭）",
          all(parse.classify(c, "")[0] == "etf"
              for c in ("0050", "0056", "00878", "006208", "00400A")))


def test_aux_datasets():
    """t86 / bwibbu / margin 的解析器。

    MI_MARGN 的欄位名稱有重複（買進/賣出/今日餘額 各出現兩次，融資一組融券一組），
    用 {名稱: 索引} 字典會讓融資餘額被靜默讀成融券餘額。這裡驗證兩者不同。
    """
    print("\n輔助資料集解析")
    import ingest
    for ds, minrows in (("t86", 500), ("bwibbu", 500), ("margin", 500)):
        j = ingest.load_raw(ds, "20260910")
        if j is None:
            check("{} 有原始檔可測".format(ds), False, "尚未抓取"); continue
        rows = parse.PARSERS[ds](j, "2026-09-10")
        check("{} 解析出資料".format(ds), len(rows) >= minrows, "{} 列".format(len(rows)))
        if ds == "margin" and rows:
            diff = sum(1 for r in rows if r["margin_bal"] != r["short_bal"])
            check("融資與融券餘額是不同欄位（防重複欄名覆蓋）",
                  diff > len(rows) * 0.5, "{}/{} 列兩者不同".format(diff, len(rows)))
        if ds == "t86" and rows:
            cats = {parse.classify(r["code"], "")[0] for r in rows}
            check("t86 已濾除權證", "warrant" not in cats,
                  "剩餘類別 {}".format(sorted(cats)))


# ---------------------------------------------------------------- 檔位與漲跌停
def test_ticks():
    print("\n檔位與漲跌停")
    cases = [(8.5, 0.01), (25.0, 0.05), (75.0, 0.10),
             (130.0, 0.50), (800.0, 1.00), (2400.0, 5.00)]
    ok = all(abs(float(factors.tick_size([p])[0]) - t) < 1e-9 for p, t in cases)
    check("六段檔位級距正確", ok)

    # 漲跌停：含跨檔位邊界的刁鑽案例
    lim = [(100.0, 110.0, 90.0),
           (24.50, 26.95, 22.05),
           (1930.0, 2120.0, 1740.0),   # 1930*1.1=2123 -> 檔位5 -> 捨去 2120
           (9.99, 10.95, 9.00)]        # 跨越 10 元檔位邊界
    ok = True
    for pc, eu, ed in lim:
        u, d = factors.limit_prices([pc])
        if abs(float(u[0]) - eu) > 1e-6 or abs(float(d[0]) - ed) > 1e-6:
            ok = False
            print("        前收 {} 期望 {}/{} 實得 {}/{}".format(pc, eu, ed, u[0], d[0]))
    check("漲跌停價（含跨檔位邊界）", ok)


# ---------------------------------------------------------------- 資料層
def test_data():
    print("\n資料層")
    try:
        s = store.q("SELECT COUNT(*) n, COUNT(DISTINCT date) d FROM quotes")
    except Exception as e:
        check("quotes 表存在", False, str(e)[:60]); return None
    n, nd = int(s.n[0]), int(s.d[0])
    check("quotes 有資料", n > 0, "{:,} 列 / {} 天".format(n, nd))

    sg = store.q("""SELECT
        SUM(CASE WHEN change > 0 THEN 1 ELSE 0 END) pos,
        SUM(CASE WHEN change < 0 THEN 1 ELSE 0 END) neg
        FROM quotes WHERE change IS NOT NULL""")
    pos, neg = float(sg.pos[0] or 0), float(sg.neg[0] or 0)
    # 這是最重要的一條：MI_INDEX 的漲跌價差是絕對值，正負號在另一個 HTML 欄位。
    # 解析錯的話 neg 會是 0，而且不會有任何錯誤訊息。
    check("漲跌正負號雙向都有（防「全市場天天漲」bug）",
          neg > 0 and 0.5 < pos / max(neg, 1) < 2.0,
          "正 {:,.0f} / 負 {:,.0f}".format(pos, neg))

    bad = store.q("""SELECT COUNT(*) n FROM quotes
        WHERE high < open OR high < close OR low > open OR low > close OR high < low""")
    check("OHLC 邏輯一致（無 high<low 等）", int(bad.n[0]) == 0)

    neg_p = store.q("SELECT COUNT(*) n FROM quotes WHERE close <= 0 OR open <= 0")
    check("無非正數價格", int(neg_p.n[0]) == 0)

    dup = store.q("""SELECT COUNT(*) n FROM (
        SELECT date, code, COUNT(*) c FROM quotes GROUP BY 1,2 HAVING c > 1)""")
    check("無重複 (日期, 代號)", int(dup.n[0]) == 0)
    return nd


# ---------------------------------------------------------------- 除權息調整
def test_exdiv():
    print("\n除權息調整")
    try:
        ex = store.q("SELECT COUNT(*) n FROM exrights")
    except Exception as e:
        check("exrights 表存在", False, str(e)[:60]); return
    check("除權息事件已載入", int(ex.n[0]) > 0, "{:,} 筆".format(int(ex.n[0])))

    q = factors.load_panel()
    j = q[q["ref_price"].notna()].copy()
    if not len(j):
        check("面板內有除權息日可驗證", False, "目前載入的日期範圍內無事件"); return

    g = q.groupby("code", sort=False)
    q["_pc"] = g["close"].shift(1)
    j = q[q["ref_price"].notna() & q["_pc"].notna()].copy()
    # 未調整報酬 vs 已調整報酬。配息會讓未調整報酬偏低。
    j["raw_ret"] = j["close"] / j["_pc"] - 1
    diff = (j["ret"] - j["raw_ret"]).dropna()
    check("除權息日的調整後報酬高於未調整報酬",
          len(diff) > 0 and diff.mean() > 0,
          "平均拉高 {:.2f} 個百分點（= 被吃掉的配息）".format(diff.mean() * 100))

    # 調整後報酬應落在合理範圍（±10% 漲跌停內，容許少量特例）
    out = (j["ret"].abs() > 0.11).mean()
    check("調整後報酬落在漲跌停範圍內", out < 0.02,
          "超出比例 {:.2%}".format(out))


# ---------------------------------------------------------------- 無 look-ahead
def test_no_lookahead():
    print("\nLook-ahead 防呆")
    q = factors.load_panel()
    q = factors.add_features(q)
    one = q[q["code"] == q["code"].iloc[len(q) // 2]].sort_values("date")
    if len(one) < 60:
        check("樣本足夠做 look-ahead 檢查", False); return

    i = len(one) - 5
    row = one.iloc[i]
    prev20 = one.iloc[i - 20:i]
    # hi20 必須等於「當日之前」20 天的最高價，不含當日
    check("hi20 不含當日",
          abs(float(row["hi20"]) - float(prev20["high"].max())) < 1e-6)
    check("lo20 不含當日",
          abs(float(row["lo20"]) - float(prev20["low"].min())) < 1e-6)
    check("amt20 不含當日",
          abs(float(row["amt20"]) - float(prev20["amount"].mean())) < 1.0)

    # 前瞻報酬必須真的取未來，而且最後 h 天要是缺值
    q2 = factors.add_forward(q.copy(), horizons=(10,))
    tail = q2.groupby("code")["fwd10"].apply(lambda s: s.tail(10).isna().all())
    check("前瞻報酬在序列末端為缺值（未用不存在的未來）", bool(tail.all()))


# ---------------------------------------------------------------- 驗證框架
def test_framework(nd):
    print("\n驗證框架自檢")
    import validate
    d = factors.build()
    n_ind = d["date"].nunique() // 10
    if n_ind < validate.MIN_OBS:
        check("獨立期數足以執行自檢", False,
              "目前 {} 期，需要 {} 期（回補完成後再跑）".format(n_ind, validate.MIN_OBS))
        return
    st = validate.framework_selftest(d, horizon=10, n_trials=20)
    check("隨機訊號未被誤判為顯著", "通過" in str(st.get("判定", "")),
          str(st.get("誤報率_t檢定", "")))


def test_revenue():
    print("\n月營收（公開資訊觀測站月報表）")
    import revenue
    import ci_update
    head = ("<table><tr><th>公司<br>代號</th><th>公司名稱</th><th>當月營收</th>"
            "<th>上月營收</th><th>去年當月營收</th><th>上月比較<br>增減(%)</th></tr>")
    row = ("<tr align=right><td align=center>{}</td><td align=left>x</td>"
           "<td nowrap> {} </td><td nowrap> 90 </td><td nowrap> 50 </td><td> 1 </td></tr>")
    html = head + row.format("2330", "1,000") + row.format("2330", "1,000") + row.format("6505", "-")
    check("月報表：千分位、同公司重複只留一筆、當月沒數字的跳過",
          revenue.parse(html) == [("2330", 1000.0, 90.0, 50.0)])
    check("月報表：表頭對不上 -> 空的（不猜欄位）",
          revenue.parse(html.replace("去年當月營收", "某欄")) == [])

    check("過了 12 日上個月就該有", ci_update.revenue_stale("202607", pd.Timestamp("2026-09-15"))
          and not ci_update.revenue_stale("202608", pd.Timestamp("2026-09-15")))
    check("12 日之前有上上個月就算新", not ci_update.revenue_stale("202607", pd.Timestamp("2026-09-05")))

    rv = revenue.load()
    if not len(rv):
        check("月營收資料存在（raw/revenue_mops/）", False, "先跑 python revenue.py")
        return
    rv["rev_month"] = pd.to_datetime(rv["rev_month"])

    def one(code, ym):
        r = rv[(rv["code"] == code) & (rv["rev_month"] == ym)]
        return r.iloc[0] if len(r) else None

    a, b = one("2330", "2008-01-01"), one("2330", "2026-08-01")
    check("2330 2008-01 營收與年增跟月報表一致",
          a is not None and a["revenue"] == 30_286_454_000 and abs(a["yoy"] - 45.24) < 0.01)
    check("2330 2026-08 營收與年增跟月報表一致",
          b is not None and b["revenue"] == 514_805_337_000 and abs(b["yoy"] - 53.32) < 0.01)
    # 2018 年被日月光併購下市的矽品：舊版（只抓今天熱門的 570 檔）不可能有它
    check("已下市公司也有歷史營收（沒有存活者偏誤）",
          one("2325", "2010-01-01") is not None)
    # 可用日一定在營收所屬月份的次月 10 日之後
    deadline = rv["rev_month"] + pd.DateOffset(months=1) + pd.Timedelta(days=9)
    check("可用日都在次月 10 日之後（不偷看）", bool((rv["avail_date"] > deadline).all()))
    n_months = rv["rev_month"].nunique()
    check("2008 年起每個月都有", rv.loc[rv["rev_month"] >= "2008-01-01", "rev_month"].nunique()
          == len(pd.period_range("2008-01", rv["rev_month"].max(), freq="M")),
          "{} 個月、{:,} 家公司".format(n_months, rv["code"].nunique()))


# ---------------------------------------------------------------- 上櫃
def test_tpex():
    """上櫃只供查詢：解析要對，而且絕不能滲進上市的選股範圍。"""
    print("\n上櫃（只供查詢）")
    import tpex

    # 行情：漲跌欄自帶正負號；除權息日是文字；'---' 是停牌
    f = ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  ",
         " 成交金額(元)", " 成交筆數 "]
    j = {"tables": [{"fields": f, "data": [
        ["3529", "力旺", "2,640.00", "+240.00", "2,500", "2,650", "2,480", "1,000", "2,640,000", "10"],
        ["6488", "環球晶", "994.00", "-8.00", "1,000", "1,005", "990", "2,000", "1,988,000", "20"],
        ["1234", "某某", "50.00", "除息", "50", "51", "49", "3,000", "150,000", "5"],
        ["5555", "停牌", "---", "---", "---", "---", "---", "0", "0", "0"],
        ["030001", "某權證", "1.00", "+0.10", "1", "1", "1", "1", "1", "1"]]}]}
    rows = {r["code"]: r for r in tpex.parse_quotes(j, "2026-09-24")}
    check("上櫃漲跌欄正號", rows["3529"]["change"] == 240.0)
    check("上櫃漲跌欄負號（不是絕對值）", rows["6488"]["change"] == -8.0)
    check("上櫃除權息日標記", rows["1234"]["is_exdiv"] and rows["1234"]["change"] is None)
    check("上櫃停牌與權證不入表", "5555" not in rows and "030001" not in rows)

    # 法人：欄名重複，按位置取；總計要等於 外資合計 + 投信 + 自營商合計
    fi = ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"]
    r = ["3529", "力旺", "319,048", "904,347", "-585,299", "0", "0", "0",
         "319,048", "904,347", "-585,299", "420,000", "4,000", "416,000",
         "33,805", "18,000", "15,805", "20,428", "20,733", "-305",
         "54,233", "38,733", "15,500", "-153,799"]
    x = tpex.parse_inst({"tables": [{"fields": fi, "data": [r]}]}, "2026-09-24")[0]
    check("上櫃法人欄位位置", (x["foreign_net"], x["trust_net"], x["dealer_net"],
                              x["total_net"]) == (-585299, 416000, 15500, -153799),
          str(x))
    check("上櫃法人欄位數不對就放棄",
          tpex.parse_inst({"tables": [{"fields": fi[:-1], "data": [r[:-1]]}]}, "d") == [])

    # 融資券：按位置取，欄名對不上就放棄
    fm = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金",
          "資使用率(%)", "資限額", "前券餘額(張)", "券賣", "券買", "券償", "券餘額"]
    m = tpex.parse_margin({"tables": [{"fields": fm, "data": [
        ["3529", "力旺", "1,991", "1", "2", "0", "1,937", "0", "1", "9", "23", "0", "5", "0", "18"]]}]},
        "2026-09-24")[0]
    check("上櫃融資券欄位位置",
          (m["margin_prev"], m["margin_bal"], m["short_prev"], m["short_bal"]) == (1991, 1937, 23, 18))
    check("上櫃融資券欄名不符就放棄",
          tpex.parse_margin({"tables": [{"fields": fm[::-1], "data": [["x"] * 15]}]}, "d") == [])

    # 除權息：類型對齊 TWSE 的 權／息／權息
    fe = ["除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價", "權值", "息值",
          "權值+息值", "權/息"]
    e = tpex.parse_exrights({"tables": [{"fields": fe, "data": [
        ["115/09/01", "1234", "某某", "50.00", "48.00", "0", "2.0", "2.0", "除息"]]}]})[0]
    check("上櫃除權息解析", (e["date"], e["kind"], e["ref_price"]) == ("2026-09-01", "息", 48.0))

    # 隔離：上市的面板（選股、研究、前瞻實測都走這條）每一列都只能來自上市的表。
    # 不能用「代號沒出現在上櫃」來檢查：轉市場的股票（例如 6423 億而得 2026-01
    # 從上市轉上櫃）兩邊都有合法的歷史。
    try:
        n_otc = int(store.q("SELECT COUNT(*) n FROM tpex_quotes")["n"][0])
    except Exception:
        n_otc = 0
    if n_otc:
        p = factors.load_panel("common")
        n_tw = int(store.q("SELECT COUNT(*) n FROM quotes WHERE cat = 'common'")["n"][0])
        check("上市面板只來自上市的表（選股範圍沒被改）", len(p) == n_tw,
              "面板 {:,} 列 vs 上市表 {:,} 列".format(len(p), n_tw))
        o = factors.load_panel("common", market="tpex")
        n_o = int(store.q("SELECT COUNT(*) n FROM tpex_quotes WHERE cat = 'common'")["n"][0])
        check("上櫃面板只來自上櫃的表", len(o) == n_o)
    else:
        print("  [SKIP] 本機還沒有上櫃的表，跳過隔離檢查")


def test_activeetf():
    """主動 ETF：申購帶來的張數增加不能被當成加碼。"""
    print("\n主動式 ETF（只顯示）")
    import activeetf as a
    k, c = a.classify_move(1000, 2000, 1e8, 2e8)
    check("單位數翻倍、張數翻倍 = 持平（被動照比例買）", k == "持平" and abs(c) < 1e-9)
    k, _ = a.classify_move(1000, 1300, 1e8, 1e8)
    check("單位數不變、張數 +30% = 加碼", k == "加碼")
    k, _ = a.classify_move(1000, 1500, 1e8, 2e8)
    check("張數 +50% 但單位數翻倍 = 減碼（每單位持股變少）", k == "減碼")
    check("新進／出清", a.classify_move(0, 500, 1e8, 1e8)[0] == "新進"
          and a.classify_move(500, 0, 1e8, 1e8)[0] == "出清")
    k, _ = a.classify_move(1000, 2000, None, None)
    check("拿不到單位數時只陳述張數、不判定加碼", k == "張數增加")
    check("變化小於門檻 = 持平", a.classify_move(1000, 1020, 1e8, 1e8)[0] == "持平")


def test_social():
    """社群聲量（social.py）：股票比對、交易日窗口、暴增倍數。全部用合成資料，不連網。"""
    print("\n社群聲量")
    import datetime as dt
    import json
    import tempfile
    from pathlib import Path as _P
    import social
    names = {"2330": "台積電", "5347": "世界", "1303": "南亞", "2408": "南亞科", "2027": "大成鋼",
             "1210": "大成",
             "2618": "長榮航", "2603": "長榮", "8299": "群聯", "3167": "大量"}
    m = social.Matcher(names)
    cases = [("2330 台積電 多", {"2330"}), ("東南亞市場", set()), ("南亞科漲停", {"2408"}),
             ("長榮航大漲", {"2618"}), ("世界很大", set()),
             ("5347 看好", {"5347"}), ("群聯2300點", {"8299"}), ("2026/09/28 盤後", set()),
             ("爆出大量", set()), ("2027 年產能", set()), ("2027大成鋼 漲停", {"2027"}),
             ("大成功", set())]
    bad = [(t, sorted(m.find(t)), sorted(w)) for t, w in cases if m.find(t) != w]
    check("代號、名稱、長名稱優先、日常用語與日期不誤判", not bad, str(bad)[:200])
    m2 = social.Matcher(dict(names, **{"5347": "世界"}))
    check("被擋掉的名稱用完整稱呼補回（世界先進）", m2.find("世界先進法說") == {"5347"})

    w = social.windows(["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-29"], 2)
    check("交易日窗口：連假（9/25–9/28）算進開市那天",
          w.get("2026-09-29") == ["2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"],
          str(w.get("2026-09-29")))

    # 合成 PTT 文章：2330 每天 3 篇（平穩）、8299 平常 1 篇、最後一天 20 篇（暴增）
    tmp = _P(tempfile.mkdtemp())
    saved = (social.PTT_DIR, social.TH_DIR, social._names, social.SNAP_FILE, social.LOCAL_FILE)
    social.PTT_DIR, social.TH_DIR = tmp / "ptt", tmp / "threads"
    social.SNAP_FILE, social.LOCAL_FILE = tmp / "snap.json", tmp / "local.json"
    social._names = lambda: names
    cal = ["2026-08-{:02d}".format(d) for d in range(3, 29) if dt.date(2026, 8, d).weekday() < 5]
    k = 0
    for day in cal:
        for code, n in (("2330", 3), ("8299", 20 if day == cal[-1] else 1)):
            for _ in range(n):
                k += 1
                social._write(social.PTT_DIR / "M.{}.A.X.json.gz".format(k),
                              dict(id=str(k), epoch=0, date=day, title=code, body="", pushes=[]))
    try:
        check("沒有匯總檔 -> 空榜、標成 missing，不丟例外",
              social.ranking(cal[-1], cal)[1]["sources"][0]["status"] == "missing")
        # 本機匯出（social_push.py 做的事），雲端只讀匯總檔
        social.export_ptt(names)
        df, info = social.ranking(cal[-1], cal)
        r = df.set_index("code")
        check("匯總檔記下每天的文章數", info["sources"][0]["articles"] == 23, str(info["sources"][0]))
        check("聲量最高：當天的提及次數", int(r.loc["8299", "total"]) == 20 and int(r.loc["2330", "total"]) == 3)
        check("暴增倍數 = (今天 + 1) ÷ (前 20 個交易日平均 + 1)",
              abs(r.loc["8299", "ratio"] - 21 / 2) < 1e-9 and abs(r.loc["2330", "ratio"] - 1) < 1e-9,
              "{:.2f} / {:.2f}".format(r.loc["8299", "ratio"], r.loc["2330", "ratio"]))
        check("基準只用有資料的交易日窗口", info["base_windows"] == 18, str(info["base_windows"]))
        # 版控裡的比本機的新（別的電腦推的）-> 用新的那份
        newer = dict(json.loads(social.LOCAL_FILE.read_text(encoding="utf-8")), updated_at="2099-01-01T00:00:00")
        newer["days"][cal[-1]]["8299"] = [0, 0]
        social.SNAP_FILE.write_text(json.dumps(newer), encoding="utf-8")
        # 雲端只有最近幾天的原文：更早的日子沿用上一份匯總，邊界那天逐格取大的，之後的以原文為準
        first = cal[0]
        prev = dict(updated_at="2026-01-01T00:00:00", articles={"2026-07-31": 7, first: 99, cal[-1]: 1},
                    days={"2026-07-31": {"2330": [5, 50]}, first: {"2330": [1, 40], "8299": [9, 0]},
                          cal[-1]: {"2330": [99, 99]}})
        real_now = social.dt.datetime
        class _Now(real_now):
            @classmethod
            def now(cls, tz=None):
                return real_now(2026, 8, 28, 12, 0, tzinfo=tz)
        social.dt.datetime = _Now
        try:
            mg = social.export_ptt(names, prev=prev)
        finally:
            social.dt.datetime = real_now
        check("合併：原文沒涵蓋的舊日子沿用上一份", mg["days"].get("2026-07-31") == {"2330": [5, 50]}
              and mg["articles"].get("2026-07-31") == 7, str(mg["days"].get("2026-07-31")))
        check("合併：邊界那天逐格取大的", mg["days"][first]["2330"] == [3, 40] and mg["days"][first]["8299"] == [9, 0]
              and mg["articles"][first] == 99, str(mg["days"][first]))
        check("合併：原文完整的日子以重算為準，不被舊匯總蓋掉", mg["days"][cal[-1]]["2330"] == [3, 0],
              str(mg["days"][cal[-1]]["2330"]))
        # 網頁按「更新」送來的聲量（公開入口、不可信）：只收最近幾天、真的代號、合理的整數
        today = social.dt.datetime.now(social.TW).date()
        d0, old_day = today.isoformat(), (today - social.dt.timedelta(days=30)).isoformat()
        before = social.load_ptt()          # 版控與本機取比較新的那份，apply_payload 就是併進這一份
        got = social.apply_payload(json.dumps(dict(
            days={d0: {"2330": [2, 30], "9999": [5, 5], "8299": [1, 999999], "1303": [-1, 3],
                       "2408": ["3", 1], "2618": [True, 1], "2603": [0, 0]},
                  old_day: {"2330": [50, 50]}, "not-a-date": {"2330": [1, 1]}},
            articles={d0: 40, old_day: 9, "x": 1})), names)
        after = json.loads(social.SNAP_FILE.read_text(encoding="utf-8"))
        check("網頁送來的聲量：只收下合格的那一格", got == 1 and after["days"][d0] == {"2330": [2, 30]},
              "{} {}".format(got, after["days"].get(d0)))
        check("網頁送來的聲量：太舊的日子、亂寫的日期不收",
              old_day not in after["days"] and "not-a-date" not in after["days"] and after["articles"].get(d0) == 40
              and old_day not in after["articles"])
        diff = [d for d, v in before["days"].items() if after["days"].get(d) != v]
        check("網頁送來的聲量：原有的歷史不動", not diff, str(diff))
        social.apply_payload(json.dumps(dict(days={d0: {"2330": [1, 45]}})), names)
        check("網頁送來的聲量：跟現有的逐格取大的",
              json.loads(social.SNAP_FILE.read_text(encoding="utf-8"))["days"][d0]["2330"] == [2, 45])
        check("網頁送來的聲量：格式不對回 0、不丟例外",
              social.apply_payload("not json", names) == 0 and social.apply_payload("[1,2]", names) == 0
              and social.apply_payload(json.dumps(dict(days="x")), names) == 0)
        r2 = social.ranking(cal[-1], cal)[0].set_index("code")
        check("版控與本機的匯總檔取比較新的", "8299" not in r2.index or int(r2.loc["8299", "total"]) == 0)
    finally:
        (social.PTT_DIR, social.TH_DIR, social._names, social.SNAP_FILE, social.LOCAL_FILE) = saved


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 主控台預設 cp950，印不出 −
    print("=" * 66)
    print("TWSE 量化系統 — 正確性測試")
    print("=" * 66)
    test_parse()
    test_aux_datasets()
    test_ticks()
    nd = test_data()
    test_exdiv()
    test_no_lookahead()
    test_revenue()
    test_tpex()
    test_activeetf()
    test_social()
    test_framework(nd)
    import tests_short
    tests_short.run(check)
    import tests_stable
    tests_stable.run(check)
    print("\n" + "=" * 66)
    print("通過 {} 項，失敗 {} 項".format(len(PASS), len(FAIL)))
    if FAIL:
        print("失敗項目: " + ", ".join(FAIL))
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
