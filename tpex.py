"""上櫃（TPEx）資料：只供查詢，不進任何選股、研究或前瞻實測。

為什麼要有、又為什麼要隔開 ——

    主動式 ETF 買的股票有一截是上櫃的（2026-09-24 買超前 30 名有 5 檔：
    力旺、雙鴻、旺矽、台燿、寶雅），倉儲只有上市時這些完全看不到。

    但選股宇宙、研究（RESEARCH_LOG 每一輪）與前瞻實測（2026-09-22 起的 9 個臂）
    全部建立在「上市普通股」上。把上櫃直接混進 quotes 表，每個 cat='common'
    的查詢都會悄悄多出 800 檔：前瞻實測的範圍被改掉（規範：參數一改該臂重新起算）、
    研究數字重現不出來，而且沒有任何錯誤訊息。

    所以上櫃放在獨立的表（tpex_quotes / tpex_inst / tpex_valuation /
    tpex_margin / tpex_exrights），欄位跟上市的表一模一樣，
    只有明確說要上櫃的地方（server.panel("otc")）才會讀它。
    改成要參加選股，是另一個需要事前登記的決定，不是改一行 SQL。

資料來源：櫃買中心 www.tpex.org.tw 的每日盤後資料（可帶日期查歷史）。
交易日曆沿用 TWSE（兩個市場同一套休市日）。

跑法：
    python tpex.py          抓最近 300 個交易日（缺的才抓）並重建上櫃的表
    python tpex.py 60       只看最近 60 個交易日
"""
import datetime as dt
import gzip
import json
import sys
import time

import duckdb
import pandas as pd
import requests

import ingest
import parse
from config import DB, RAW, REQUEST_DELAY, MAX_RETRY, UA

BASE = "https://www.tpex.org.tw"
DIR = RAW / "tpex"
DEFAULT_DAYS = 300      # 個股頁最深的回看是 252 日；也蓋住主動 ETF（2025-05 起）的整段期間

# 每日資料集：(路徑, 固定參數)。日期參數一律 date=YYYY/MM/DD。
DATASETS = {
    "quotes": ("/www/zh-tw/afterTrading/otc", {"type": "EW"}),
    "inst":   ("/www/zh-tw/insti/dailyTrade", {"type": "Daily", "sect": "EW"}),
    "pe":     ("/www/zh-tw/afterTrading/peQryDate", {}),
    "margin": ("/www/zh-tw/margin/balance", {}),
}
TABLES = dict(quotes="tpex_quotes", inst="tpex_inst", pe="tpex_valuation",
              margin="tpex_margin")
EXR_TABLE = "tpex_exrights"

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


# --------------------------------------------------------------------- 擷取
def _get(path, params):
    for attempt in range(MAX_RETRY):
        try:
            r = _s.get(BASE + path, params=dict(params, response="json"), timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                time.sleep(REQUEST_DELAY * (2 ** attempt) + 5)
                continue
            return None
        except Exception:
            time.sleep(REQUEST_DELAY * (2 ** attempt))
    return None


def path_for(ds, date):
    d = DIR / ds
    d.mkdir(parents=True, exist_ok=True)
    return d / "{}.json.gz".format(date)


def have(ds, date):
    p = path_for(ds, date)
    return p.exists() and p.stat().st_size > 200


def load_raw(ds, date):
    p = path_for(ds, date)
    if not p.exists():
        return None
    try:
        return json.loads(gzip.decompress(p.read_bytes()).decode())
    except Exception:
        return None


def fetch_day(ds, date):
    """抓一個資料集的一天（date = YYYYMMDD）。回傳 True 表示檔案已就緒。

    TPEx 查不到資料時回 stat 不是 ok 或 tables 空的。那種回應不存檔 ——
    跟 TWSE 不同，TPEx 常常是「還沒發布」而不是「那天沒有」，存起來就永遠不會再抓。"""
    if have(ds, date):
        return True
    p, fixed = DATASETS[ds]
    j = _get(p, dict(fixed, date="{}/{}/{}".format(date[:4], date[4:6], date[6:])))
    if not j or str(j.get("stat", "")).lower() != "ok":
        return False
    if not any(t.get("data") for t in j.get("tables") or []):
        return False
    path_for(ds, date).write_bytes(gzip.compress(json.dumps(j, ensure_ascii=False).encode()))
    return True


def fetch_exrights_month(ym, max_age_hours=12):
    """一個月份的除權息事件（ym = YYYYMM）。當月與未來月份會陸續公告，過期就重抓。"""
    p = DIR / "exrights" / "{}.json.gz".format(ym)
    p.parent.mkdir(parents=True, exist_ok=True)
    this_ym = dt.date.today().strftime("%Y%m")
    if p.exists() and p.stat().st_size > 50:
        fresh = ym < this_ym or (time.time() - p.stat().st_mtime) / 3600 < max_age_hours
        if fresh:
            return json.loads(gzip.decompress(p.read_bytes()).decode())
    y, m = int(ym[:4]), int(ym[4:])
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    j = _get("/www/zh-tw/bulletin/exDailyQ",
             {"startDate": "{}/{:02d}/01".format(y, m),
              "endDate": "{}/{:02d}/{:02d}".format(y, m, last)})
    if not j or str(j.get("stat", "")).lower() != "ok":
        return None
    p.write_bytes(gzip.compress(json.dumps(j, ensure_ascii=False).encode()))
    return j


# --------------------------------------------------------------------- 解析
def _table(j):
    t = (j or {}).get("tables") or []
    return t[0] if t and t[0].get("data") else None


def _ix(fields):
    # TPEx 的欄名常帶空白與 <br>（'收盤 '、'最後買量<br>(張數)'）
    return {parse._TAG.sub("", f).strip(): i for i, f in enumerate(fields)}


def parse_quotes(j, date):
    """上櫃日行情 -> 與 quotes 表同欄位的列。

    漲跌欄本身帶正負號（'+240.00'、'-0.03'），除權息日是文字（'除息'／'除權'／'除權息'），
    停牌是 '---'。跟 TWSE 那個「絕對值＋另一欄放符號」的陷阱不同，不要套用同一套解析。"""
    t = _table(j)
    if not t:
        return []
    ix = _ix(t["fields"])
    need = ("代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "成交股數", "成交金額(元)")
    if any(k not in ix for k in need):
        return []                          # 欄位對不上就不猜
    out = []
    for r in t["data"]:
        code, name = str(r[ix["代號"]]).strip(), str(r[ix["名稱"]]).strip()
        o, h, l, c = (parse.num(r[ix[k]]) for k in ("開盤", "最高", "最低", "收盤"))
        if None in (o, h, l, c) or c <= 0 or o <= 0:
            continue
        if not (h >= max(o, c) and l <= min(o, c) and h >= l):
            continue
        chg_raw = parse._TAG.sub("", str(r[ix["漲跌"]])).strip()
        exdiv = "除" in chg_raw
        cat, sub = parse.classify(code, name)
        if cat in ("other", "warrant"):
            continue
        out.append(dict(
            date=date, code=code, name=name,
            open=o, high=h, low=l, close=c,
            volume=parse.num(r[ix["成交股數"]]) or 0.0,
            trades=(parse.num(r[ix["成交筆數"]]) or 0.0) if "成交筆數" in ix else 0.0,
            amount=parse.num(r[ix["成交金額(元)"]]) or 0.0,
            change=None if exdiv else parse.num(chg_raw),
            is_exdiv=exdiv,
            pe_raw=None,
            cat=cat, subcat=sub,
        ))
    return out


def parse_inst(j, date):
    """三大法人。欄名重複（買進股數／賣出股數／買賣超股數 各出現 7 次），一律按位置取：

        2-4   外資及陸資（不含外資自營商）   <- foreign_net，對齊 TWSE T86 的同名欄
        5-7   外資自營商
        8-10  外資及陸資合計
        11-13 投信                          <- trust_net
        14-16 自營商（自行買賣）
        17-19 自營商（避險）
        20-22 自營商合計                    <- dealer_net
        23    三大法人買賣超合計            <- total_net

    欄位數不是 24 就不猜，整天放棄。"""
    t = _table(j)
    if not t or len(t["fields"]) != 24:
        return []
    f0 = parse._TAG.sub("", t["fields"][0]).strip()
    last = parse._TAG.sub("", t["fields"][23]).strip()
    if f0 != "代號" or not last.startswith("三大法人"):
        return []
    out = []
    for r in t["data"]:
        code = str(r[0]).strip()
        if parse.classify(code, "")[0] in ("other", "warrant"):
            continue
        out.append(dict(date=date, code=code,
                        foreign_net=parse.num(r[4]), trust_net=parse.num(r[13]),
                        dealer_net=parse.num(r[22]), total_net=parse.num(r[23])))
    return out


def parse_pe(j, date):
    t = _table(j)
    if not t:
        return []
    ix = _ix(t["fields"])
    if "股票代號" not in ix:
        return []
    out = []
    for r in t["data"]:
        pe = parse.num(r[ix["本益比"]]) if "本益比" in ix else None
        pb = parse.num(r[ix["股價淨值比"]]) if "股價淨值比" in ix else None
        dy = parse.num(r[ix["殖利率(%)"]]) if "殖利率(%)" in ix else None
        if pe is not None and pe <= 0: pe = None
        if pb is not None and pb <= 0: pb = None
        out.append(dict(date=date, code=str(r[ix["股票代號"]]).strip(),
                        pe=pe, pb=pb, div_yield=dy))
    return out


def parse_margin(j, date):
    """融資融券餘額（張）。欄名一樣有重複，按位置取：
        2 前資餘額  6 資餘額  10 前券餘額  14 券餘額
    用「前資餘額／資餘額／前券餘額／券餘額」四個欄名確認排列，不符就放棄。"""
    t = _table(j)
    if not t:
        return []
    f = [parse._TAG.sub("", x).strip() for x in t["fields"]]
    if len(f) < 15 or not (f[2].startswith("前資餘額") and f[6] == "資餘額"
                           and f[10].startswith("前券餘額") and f[14] == "券餘額"):
        return []
    out = []
    for r in t["data"]:
        code = str(r[0]).strip()
        if parse.classify(code, "")[0] in ("other", "warrant"):
            continue
        out.append(dict(date=date, code=code,
                        margin_bal=parse.num(r[6]), margin_prev=parse.num(r[2]),
                        short_bal=parse.num(r[14]), short_prev=parse.num(r[10])))
    return out


def parse_exrights(j):
    """除權息 -> 與 exrights 表同欄位（date, code, prev_close, ref_price, value, kind）。

    kind 對齊 TWSE 的「權／息／權息」；value 是「權值+息值」。"""
    t = _table(j)
    if not t:
        return []
    ix = _ix(t["fields"])
    need = ("除權息日期", "代號", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息")
    if any(k not in ix for k in need):
        return []
    kmap = {"除權": "權", "除息": "息", "除權息": "權息", "權": "權", "息": "息", "權息": "權息"}
    out = []
    for r in t["data"]:
        kind = kmap.get(str(r[ix["權/息"]]).strip())
        pc = parse.num(r[ix["除權息前收盤價"]])
        rp = parse.num(r[ix["除權息參考價"]])
        v = parse.num(r[ix["權值+息值"]])
        if not kind or not pc or not rp or pc <= 0 or rp <= 0:
            continue
        try:
            d = parse.roc_to_iso(str(r[ix["除權息日期"]]))
        except Exception:
            continue
        out.append(dict(date=d, code=str(r[ix["代號"]]).strip(),
                        prev_close=pc, ref_price=rp, value=v or 0.0, kind=kind))
    return out


PARSERS = dict(quotes=parse_quotes, inst=parse_inst, pe=parse_pe, margin=parse_margin)


# --------------------------------------------------------------------- 寫入
def _write_days(con, ds, days):
    """把指定交易日寫進該資料集的表：先刪這幾天、再插入（重跑冪等）。表不存在就建。"""
    tbl = TABLES[ds]
    rows = []
    for d in days:
        j = load_raw(ds, d)
        if j is not None:
            rows.extend(PARSERS[ds](j, "{}-{}-{}".format(d[:4], d[4:6], d[6:])))
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [tbl]
    ).fetchone()[0]
    con.register("_tmp", df)
    if not exists:
        con.execute("CREATE TABLE {} AS SELECT * FROM _tmp".format(tbl))
    else:
        isos = sorted(df["date"].dt.strftime("%Y-%m-%d").unique())
        con.execute("DELETE FROM {} WHERE date IN ({})".format(
            tbl, ", ".join("DATE '{}'".format(x) for x in isos)))
        cols = [r[0] for r in con.execute("DESCRIBE {}".format(tbl)).fetchall()]
        con.execute("INSERT INTO {} SELECT {} FROM _tmp".format(tbl, ", ".join(cols)))
    con.unregister("_tmp")
    return len(df)


def _write_exrights(con, months):
    rows = []
    for ym in months:
        p = DIR / "exrights" / "{}.json.gz".format(ym)
        if p.exists():
            rows.extend(parse_exrights(json.loads(gzip.decompress(p.read_bytes()).decode())))
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date", "code"], keep="last")
    con.register("_tmp", df)
    con.execute("DROP TABLE IF EXISTS {}".format(EXR_TABLE))
    con.execute("CREATE TABLE {} AS SELECT * FROM _tmp".format(EXR_TABLE))
    con.unregister("_tmp")
    return len(df)


def _months(days):
    ms = sorted({d[:6] for d in days})
    # 多抓下一個月：未來的除權息是提前公告的，個股頁的「即將除權息」需要它
    if ms:
        y, m = int(ms[-1][:4]), int(ms[-1][4:])
        ms.append("{:04d}{:02d}".format(y + (m == 12), m % 12 + 1))
    return ms


def update(n_days=DEFAULT_DAYS, budget=None, verbose=True):
    """抓最近 n_days 個交易日裡缺的資料，寫進上櫃的表。

    budget 限制這次最多「抓」幾個交易日（CI 用，避免一次回補拖垮排程）；
    已經在 raw/ 的日子不算。回傳寫入的交易日數。
    不會丟例外給呼叫端 —— 上櫃是附加資訊，失敗不該擋住上市那條主線。"""
    try:
        days = ingest.trading_calendar()[-n_days:]
        todo = [d for d in days if not all(have(ds, d) for ds in DATASETS)]
        if budget is not None:
            # 新的日子優先：個股頁最需要的是最近的資料
            todo = sorted(todo, reverse=True)[:budget]
        def flush(ds_days, exr_months=()):
            """把這幾天寫進資料庫（重跑冪等）。分批寫：下載中途斷掉時，
            已經抓到的日子也已經在表裡，不必等重跑完才看得到。"""
            con = duckdb.connect(str(DB))
            try:
                n = {ds: _write_days(con, ds, ds_days) for ds in DATASETS} if ds_days else {}
                nx = _write_exrights(con, list(exr_months)) if exr_months else 0
                con.execute("CHECKPOINT")
            finally:
                con.close()
            return n, nx

        def backlog():
            """raw 有、表裡還沒有的日子（例如上次下載中斷前抓到、還沒寫進去的）。"""
            present = {d for d in days if have("quotes", d)}
            try:
                import store
                in_tbl = set(store.q("SELECT DISTINCT strftime(date, '%Y%m%d') d FROM {}"
                                     .format(TABLES["quotes"]))["d"])
            except Exception:
                in_tbl = set()
            return sorted(present - in_tbl)

        early = backlog()
        if early:
            flush(early)          # 先把已經下載好的寫進去，馬上可用
        fetched, pending = [], []
        for i, d in enumerate(sorted(todo)):
            ok = True
            for ds in DATASETS:
                if not have(ds, d):
                    ok = fetch_day(ds, d) and ok
                    time.sleep(REQUEST_DELAY)
            if ok:
                fetched.append(d)
                pending.append(d)
            if len(pending) >= 20:
                flush(pending)
                pending = []
            if verbose and (i + 1) % 20 == 0:
                print("  上櫃 {}/{}".format(i + 1, len(todo)), flush=True)
        months = _months(days)
        for ym in months:
            fetch_exrights_month(ym)
            time.sleep(REQUEST_DELAY)
        write = sorted(set(pending) | set(backlog()))
        n, nx = flush(write, months)
        total = len(set(early) | set(fetched) | set(write))
        if verbose:
            print("上櫃：缺 {} 天、這次抓到 {} 天、寫入 {} 天 {}；除權息 {} 筆".format(
                len(todo), len(fetched), total,
                ", ".join("{} {:,} 列".format(TABLES[k], v) for k, v in n.items()), nx),
                flush=True)
        return total
    except Exception as e:
        print("::warning::上櫃資料更新失敗（不影響上市資料與網站主線）：{}".format(e),
              flush=True)
        return 0


def summary():
    con = duckdb.connect(str(DB), read_only=True)
    out = []
    try:
        for t in list(TABLES.values()) + [EXR_TABLE]:
            try:
                r = con.execute("SELECT COUNT(*), MIN(date), MAX(date), COUNT(DISTINCT date) "
                                "FROM {}".format(t)).fetchone()
                out.append("  {:<16} {:>9,} 列  {:>4} 天  {:%Y-%m-%d} ~ {:%Y-%m-%d}".format(
                    t, r[0], r[3], r[1], r[2]))
            except Exception:
                out.append("  {:<16} (尚未建立)".format(t))
    finally:
        con.close()
    return "\n".join(out)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    update(n_days=n)
    print(summary())
