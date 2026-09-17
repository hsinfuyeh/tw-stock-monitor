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


if __name__ == "__main__":
    print("=" * 66)
    print("TWSE 量化系統 — 正確性測試")
    print("=" * 66)
    test_parse()
    test_aux_datasets()
    test_ticks()
    nd = test_data()
    test_exdiv()
    test_no_lookahead()
    test_framework(nd)
    print("\n" + "=" * 66)
    print("通過 {} 項，失敗 {} 項".format(len(PASS), len(FAIL)))
    if FAIL:
        print("失敗項目: " + ", ".join(FAIL))
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
