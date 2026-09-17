"""CLI 入口。

  python run.py ingest            # 續跑回補（冪等，可隨時中斷）
  python run.py build             # raw -> DuckDB
  python run.py validate          # 框架自檢 + 因子驗證表
  python run.py rank              # 產生每日排序 HTML（上市普通股）
  python run.py rank-etf          # ETF 排序（獨立宇宙與因子集）
  python run.py stock 2330 2317   # 個股 K 線觀察報告
  python run.py index             # 產生總覽頁（把各報告串起來）
  python run.py daily             # 增量更新 + 重建 + 產全部報告 + 總覽（每日跑這個）
"""
import sys

import pandas as pd

import factors
import ingest
import rank as rankmod
import report
import store
import validate

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)

HORIZON = 10

# 納入綜合分數的因子。刻意只放有實證基礎的橫斷面因子；
# K_* 開頭的 K 線規則算出來當對照組，但不計入分數。
FINDINGS = [
    "<strong>六條 K 線規則的超額報酬全部趨近於零</strong>（+0.09% ~ +1.00%），"
    "13 個檢定經多重比較校正後無一通過。",
    "<strong>10 日換倉在台股數學上不可能獲利</strong>：來回成本年化拖累 30.2%，"
    "而實測最佳毛價差年化僅約 22.5%。40–60 日才是可行區間，但觀察期仍不足以定論。",
    "<strong>天真回測會把雜訊報成鐵證</strong>：同一訊號未修正重疊視窗時 t = −6.96，"
    "修正後 t = −0.86。框架自檢曾抓到 25% 的純隨機訊號被誤判為顯著。",
    "<strong>除權息不調整會有系統性偏誤</strong>：8,667 筆事件平均調整 3.73%，"
    "其中 24.8% 超過 5%。不調整會製造「高殖利率 = 低報酬」的假訊號。",
    "<strong>因子的滾動 IC 沒有持續性</strong>：自相關介於 −0.13 ~ +0.17、"
    "符號持續率 44–56%（等同丟銅板）。所以「用最近有效的因子」這種動態加權"
    "是在對雜訊擬合 —— 樣本外 walk-forward 已證實無效。",
    "<strong>IC 與十分位價差會給出相反結論</strong>：低波動股的 IC 為正"
    "（+0.061，贏的次數多），十分位價差卻是負的（單調性 −0.94，輸在平均），"
    "因為高波動股有樂透式肥尾。只看其中一個指標必然誤判。",
]

CORE = ["反轉_5日", "反轉_20日", "低波動", "量縮", "低周轉", "距20日高"]


def _panel(horizons=(1, 3, 5, 10, 20)):
    """建面板並回傳 (面板, 實際可用的因子清單)。

    不再就地修改模組層級的 CORE —— 那會讓因子集隨呼叫順序改變，
    同一份程式跑兩次可能得到不同結果，而且完全不會報錯。
    """
    d = factors.build(horizons=horizons)
    use = list(CORE)
    if "動能_12_1" in d.columns and d["動能_12_1"].notna().mean() > 0.5:
        use.append("動能_12_1")
    return d, [f for f in use if f in d.columns]


def cmd_ingest():
    days = ingest.trading_calendar()
    print("交易日 {}: {} ~ {}".format(len(days), days[0], days[-1]))
    ingest.backfill(list(ingest.DATASETS), days)


def cmd_build():
    store.build()
    print(store.summary())


def cmd_validate():
    d, _use = _panel()
    n_ind = d["date"].nunique() // HORIZON
    print("面板 {:,} 列 / {} 天 / 平均每日 {:.0f} 檔 / 約 {} 個獨立觀察期".format(
        len(d), d["date"].nunique(), len(d) / d["date"].nunique(), n_ind))

    print("\n" + "=" * 78)
    print("步驟 1／框架自檢：餵 20 組純隨機訊號")
    print("=" * 78)
    st = validate.framework_selftest(d, horizon=HORIZON, n_trials=20)
    for k, v in st.items():
        print("  {:<14}{}".format(k, v))
    if "失敗" in str(st.get("判定", "")):
        print("\n框架自檢未通過，後續數字不可信。停止。")
        return None, None, None
    if "無法執行" in str(st.get("判定", "")):
        print("\n資料量尚不足以執行自檢；下面的數字僅供管線驗證，不得作為研究結論。")

    print("\n" + "=" * 78)
    print("步驟 2／因子驗證（IC，{} 日前瞻，市場中性，非重疊抽樣）".format(HORIZON))
    print("=" * 78)
    allf = _use + [c for c in d.columns if c.startswith("K_")]
    ic = validate.ic_report(d, allf, horizon=HORIZON)
    print(ic.to_string(index=False))

    print("\n" + "=" * 78)
    print("步驟 3／十分位多空回測（扣交易成本）")
    print("=" * 78)
    rows = []
    for f in allf:
        r = validate.decile_backtest(d, f, horizon=HORIZON)
        if r:
            rows.append(r)
    bt = pd.DataFrame(rows)
    if len(bt):
        print(bt.to_string(index=False))
    return d, ic, bt


def cmd_stock(codes=None, quiet=False):
    """個股觀察報告。python run.py stock 2330 2317

    面板只建一次，所有代號共用。
    """
    import stock_report
    codes = codes or sys.argv[2:] or ["2330"]
    common = [c for c in codes if not c.startswith("00")]
    etfs = [c for c in codes if c.startswith("00")]
    made = []
    for group, cat, sub in ((common, "common", None),
                            (etfs, "etf", factors.ETF_SUBCATS)):
        if not group:
            continue
        panel = factors.build(cat=cat, subcats=sub)
        for c in group:
            out = stock_report.build(c, cat=cat, subcats=sub, panel=panel)
            if out:
                made.append(c)
            if not quiet:
                print("  {} -> {}".format(c, out or "找不到或資料不足"))
    return made


def cmd_index():
    import index_page
    out = index_page.build(findings=FINDINGS)
    print("總覽頁已產生: {}".format(out))
    return out


def cmd_rank_etf():
    """ETF 排序。獨立宇宙、獨立因子集。

    只納入台股一般型與高股息型（見 factors.ETF_SUBCATS）。
    只用價格類因子：ETF 的股價淨值比被套利釘在 1.00，法人買賣超是造市商申贖。
    """
    d = factors.build(cat="etf", subcats=factors.ETF_SUBCATS)
    use = [f for f in factors.ETF_FACTORS if f in d.columns]
    n_per_day = len(d) / max(d["date"].nunique(), 1)
    print("ETF 宇宙: {} 檔 / {} 天 / 平均每日可排序 {:.0f} 檔".format(
        d["code"].nunique(), d["date"].nunique(), n_per_day))
    if n_per_day < 30:
        print("  注意：橫斷面過窄（<30 檔），十分位回測不可靠，改以 top/bottom 5 呈現。")
    top_n = 10 if n_per_day >= 40 else 5
    ic, bt = rankmod.scorecard(d, use, horizon=HORIZON)
    day, bull, bear = rankmod.daily_ranking(d, use, top_n=top_n)
    asof = str(pd.Timestamp(day["date"].iloc[0]).date())
    ic_all = validate.ic_report(d, use + [c for c in d.columns if c.startswith("K_")],
                                horizon=HORIZON)
    meta = dict(宇宙=len(day), horizon=HORIZON, 獨立期數=d["date"].nunique() // HORIZON)
    out = report.build(asof, bull, bear, ["z_" + f for f in use], ic_all, bt, meta,
                       kind="etf")
    print("ETF 報告已產生: {}".format(out))
    return list(bull["code"]) + list(bear["code"])


def cmd_rank():
    d, use = _panel()
    ic, bt = rankmod.scorecard(d, use, horizon=HORIZON)
    day, bull, bear = rankmod.daily_ranking(d, use, top_n=20)
    asof = str(pd.Timestamp(day["date"].iloc[0]).date())
    fcols = ["z_" + f for f in use]
    allf = use + [c for c in d.columns if c.startswith("K_")]
    ic_all = validate.ic_report(d, allf, horizon=HORIZON)
    meta = dict(宇宙=len(day), horizon=HORIZON,
                獨立期數=d["date"].nunique() // HORIZON)
    out = report.build(asof, bull, bear, fcols, ic_all, bt, meta, kind="common")
    print("綜合分數成績單:")
    print(ic.to_string(index=False))
    if bt:
        print("\n十分位回測: " + "  ".join("{}={}".format(k, v) for k, v in bt.items()))
    print("\n報告已產生: {}".format(out))
    return out


def cmd_daily():
    days = ingest.trading_calendar()
    recent = days[-15:]
    print("增量更新最近 {} 個交易日…".format(len(recent)))
    ingest.backfill(list(ingest.DATASETS), recent)
    # 除權息是陸續公告的，每天要重抓當年度，否則行事曆會停在上次抓取的時間點
    print("更新除權息行事曆…")
    try:
        import exrights, duckdb
        from config import DB
        df = exrights.load()
        con = duckdb.connect(str(DB)); con.register("_t", df)
        con.execute("DROP TABLE IF EXISTS exrights")
        con.execute("CREATE TABLE exrights AS SELECT * FROM _t")
        con.close()
        print("  除權息事件 {:,} 筆".format(len(df)))
    except Exception as e:
        print("  除權息更新失敗（不影響其他部分）: {}".format(e))
    store.build()
    print(store.summary())
    # 順序很重要：先算出名單並產生個股報告，再產排序頁 ——
    # 排序頁的代號連結會檢查檔案是否存在，順序反了就會全部退化成純文字。
    print("預先產生名單上的個股報告…")
    for cat, sub, factor_set, top_n in (
            ("common", None, CORE, 20), ("etf", factors.ETF_SUBCATS, factors.ETF_FACTORS, 10)):
        d = factors.build(cat=cat, subcats=sub)
        use = [f for f in factor_set if f in d.columns]
        _, bull, bear = rankmod.daily_ranking(d, use, top_n=top_n)
        import stock_report
        for c in list(bull["code"]) + list(bear["code"]):
            stock_report.build(c, cat=cat, subcats=sub, panel=d)
        print("  {}: {} 份".format(cat, len(bull) + len(bear)))
    cmd_rank()
    cmd_rank_etf()
    cmd_index()
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rank"
    {"ingest": cmd_ingest, "build": cmd_build, "validate": cmd_validate,
     "rank": cmd_rank, "rank-etf": cmd_rank_etf, "stock": cmd_stock,
     "index": cmd_index, "daily": cmd_daily}[cmd]()
