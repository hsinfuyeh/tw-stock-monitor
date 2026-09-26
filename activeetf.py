"""主動式 ETF 每日持股：只顯示、不計分、不進研究（RESEARCH_LOG 2026-09-26 調查）。

為什麼只顯示 ——

    資料從 2025-05 才開始（第一檔主動式 ETF 上市），以持有 20 日計不重疊的
    獨立期數不到 20 個，低於本系統「< 30 不報顯著性」的門檻。所以現在只存、只看，
    等樣本夠了再事前登記研究。個股頁上一律標「未驗證」。

最重要的一件事：申購贖回 ≠ 經理人加碼 ——

    有人申購 ETF 時，經理人必須把新資金照比例買成股票，每一檔持股的張數都會變多。
    00981A 的發行單位數 2025-06 約 2.7 億、2026-09 約 95 億（35 倍），這段期間
    「張數增加」幾乎全是資金流入。直接拿張數變化當加碼訊號，量到的是資金流，不是判斷。

    所以這裡一律看「每單位持股數」= 持股股數 ÷ 發行單位數。單位數變多、
    每單位持股不變 = 被動照比例買；每單位持股變多 = 經理人真的加碼。
    拿不到單位數的投信，只存資料、不判定加減碼。

資料來源：各投信官網的 PCF（申購買回清單），沒有集中下載處。端點與欄位語意參考了
公開專案 nctuwanglin/active-etf 的整理（該 repo 無授權條款，只參考事實，程式自己寫）。
目前接了 6 家（統一、中信、復華、群益、安聯、國泰）；其餘投信（富邦、凱基、野村、
第一金、聯博、摩根、兆豐、永豐、台新）格式不一或擋自動請求，尚未接。

跑法：
    python activeetf.py            抓每檔最新一份，並補最近 60 個交易日的歷史（支援的投信）
    python activeetf.py 250        補最近 250 個交易日
"""
import datetime as dt
import gzip
import json
import re
import sys
import time

import duckdb
import pandas as pd
import requests

import ingest
from config import DB, RAW, REQUEST_DELAY, UA

DIR = RAW / "active_etf"
HOLD_TABLE, META_TABLE = "active_holdings", "active_meta"
DEFAULT_DAYS = 60
# 每單位持股變動超過這個比例才算加碼／減碼。持股以「股」為單位、國泰還要從
# 每基數股數還原，小部位的取整誤差可達 ±2%，門檻要高過它。
MOVE_TH = 0.03

_s = requests.Session()
_s.headers.update({"User-Agent": UA})


class Skip(Exception):
    """這一檔、這一天拿不到（查無資料、假日、端點擋）。不中斷整批。"""


def _num(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _json(r, what):
    try:
        d = r.json()
    except ValueError:
        raise Skip("{} 回非 JSON（被擋或改版）".format(what))
    return json.loads(d) if isinstance(d, str) else d


def _roc(d):
    return "{}/{:02d}/{:02d}".format(d.year - 1911, d.month, d.day)


# ======================================================================
# 各投信。每個 fetch(code, day) 回傳 dict(data_date, units, nav, holdings=[...])，
# day=None 代表最新一份。data_date 一律是「持股基準日」，不是公告日（T+1）——
# 取錯會讓這一家比別家多一天，跨 ETF 的同日比較就錯開了。
# ======================================================================
class President:
    """統一投信（ezmoney）。date 為民國日期、specificDate=True 可查歷史。"""
    name, history = "統一", True
    query_is_pub = True        # 查詢日是公告日（T+1），回來的是前一個營業日的持股
    PAGE = "https://www.ezmoney.com.tw/ETF/Transaction/PCF"
    API = "https://www.ezmoney.com.tw/ETF/Transaction/GetPCF"
    _map = None

    def fund(self, code):
        if self._map is None:
            html = _s.get(self.PAGE, timeout=30).text
            m = re.search(r"id=['\"]DataFundList['\"][^>]*data-content=['\"](.*?)['\"]",
                          html, re.S)
            if not m:
                raise Skip("統一：PCF 頁找不到基金清單")
            import html as _h
            self._map = {(f.get("sStockNo") or "").strip(): f["sFundCode"]
                         for f in json.loads(_h.unescape(m.group(1))) if f.get("sStockNo")}
        return self._map.get(code)

    def fetch(self, code, day):
        fc = self.fund(code)
        if not fc:
            raise Skip("統一：{} 不在清單".format(code))
        q = day or (dt.date.today() + dt.timedelta(days=3))
        d = _json(_s.post(self.API, json={"fundCode": fc, "date": _roc(q),
                                          "specificDate": day is not None},
                          headers={"Referer": self.PAGE}, timeout=30), "統一")
        st = [a for a in d.get("asset") or [] if a.get("AssetCode") == "ST"]
        pcf = d.get("pcf") or []
        if not st or not st[0].get("Details") or not pcf:
            raise Skip("統一：{} {} 查無持股".format(code, day))
        # 兩種格式都會出現：'/Date(1790092800000)/'（.NET 毫秒，台北午夜 = UTC 前一天 16:00）
        # 或 '2026-09-23T00:00:00'
        raw = str(pcf[0].get("TranDate"))
        m = re.search(r"/Date\((\d+)", raw)
        if m:
            ddate = (dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.timezone.utc)
                     + dt.timedelta(hours=8)).date()
        elif re.match(r"\d{4}-\d{2}-\d{2}", raw):
            ddate = dt.date(*map(int, raw[:10].split("-")))
        else:
            raise Skip("統一：資料日格式不明 {}".format(raw[:30]))
        amt = {r.get("PCFCode"): r.get("Amount") for r in pcf}
        return dict(data_date=ddate, units=_num(amt.get("OUT_UNIT")), nav=_num(amt.get("NAV")),
                    holdings=[dict(code=str(r["DetailCode"]).strip(),
                                   name=str(r["DetailName"]).strip(),
                                   shares=_num(r["Share"]), weight=_num(r["NavRate"]))
                              for r in st[0]["Details"]])


class Ctbc:
    """中信投信。先取 token；StartDate 必填，假日回 ResultCode≠0。"""
    name, history = "中信", True
    B = "https://www.ctbcinvestments.com.tw/API/"
    _tok = _map = None

    def fund(self, code):
        if self._map is None:
            self._tok = _json(_s.post(self.B + "home/AuthToken",
                                      params={"token": "www.ctbcinvestments.com"}, json={},
                                      timeout=30), "中信 token")["Data"]["token"]
            d = _json(_s.post(self.B + "etf/ETFList", params={"token": self._tok}, json={},
                              timeout=30), "中信清單")
            rows = d["Data"]["Data"] if isinstance(d.get("Data"), dict) else d.get("Data") or []
            self._map = {r["ETF_ID"]: r["FID"] for r in rows if r.get("ETF_ID")}
        return self._map.get(code)

    def fetch(self, code, day):
        fid = self.fund(code)
        if not fid:
            raise Skip("中信：{} 不在清單".format(code))
        q = day or dt.date.today()
        d = _json(_s.post(self.B + "etf/ETFHoldingWeight", params={"token": self._tok},
                          json={"FID": fid, "StartDate": q.strftime("%Y/%m/%d")}, timeout=30),
                  "中信")
        data = d.get("Data") or {}
        a = (data.get("FundAssets") or [None])[0]
        g = [x for x in data.get("FundAssetsDetail") or [] if x.get("Code") == "STOCK"]
        if d.get("ResultCode") != 0 or not a or not g or not g[0].get("Data"):
            raise Skip("中信：{} {} 查無持股".format(code, q))
        rows = g[0]["Data"]
        k = dict(zip(("代號", "名稱", "股數", "權重"), ("code_", "name_", "qty_", "weights_")))
        if not all(c in rows[0] for c in k.values()):
            raise Skip("中信：持股欄名不明 {}".format(list(rows[0])))
        return dict(data_date=dt.date(*map(int, a["資料日期"].split("/"))),
                    units=_num(a.get("基金在外流通單位數")), nav=_num(a.get("基金淨資產價值")),
                    holdings=[dict(code=str(r[k["代號"]]).strip(), name=str(r[k["名稱"]]).strip(),
                                   shares=_num(r[k["股數"]]), weight=_num(r[k["權重"]]))
                              for r in rows])


class Fuhhwa:
    """復華投信。qDate 可查歷史；此端點<b>不含發行單位數</b>，所以不判定加減碼。"""
    name, history = "復華", True
    _map = None

    def fund(self, code):
        if self._map is None:
            d = _json(_s.get("https://www.fhtrust.com.tw/api/fundList?ec001=3", timeout=30),
                      "復華清單")
            self._map = {(f.get("etf002") or "").strip(): f["fundID"]
                         for f in d.get("result") or [] if f.get("etf002")}
        return self._map.get(code)

    def fetch(self, code, day):
        fid = self.fund(code)
        if not fid:
            raise Skip("復華：{} 不在清單".format(code))
        rows, r = [], {}
        for q in ([day] if day else [dt.date.today() - dt.timedelta(days=b) for b in range(8)]):
            d = _json(_s.get("https://www.fhtrust.com.tw/api/assets",
                             params={"fundID": fid, "qDate": q.strftime("%Y/%m/%d")}, timeout=30),
                      "復華")
            r = (d.get("result") or [{}])[0]
            rows = [x for x in r.get("detail") or []
                    if x.get("ftype") == "股票" and (x.get("stockid") or "").strip()]
            if rows and r.get("dDate"):
                break
            if not day:
                time.sleep(REQUEST_DELAY)
        if not rows or not r.get("dDate"):
            raise Skip("復華：{} {} 查無持股".format(code, day))
        wkey = next((k for k in rows[0] if "rate" in k.lower()), None)
        skey = next((k for k in ("qshare", "share", "shares", "fqty")
                     if k in rows[0]), None)
        if not skey:
            skey = next((k for k in rows[0] if "share" in k.lower() or "qty" in k.lower()), None)
        if not skey:
            raise Skip("復華：股數欄名不明 {}".format(list(rows[0])))
        return dict(data_date=dt.date(*map(int, r["dDate"].split("/"))), units=None, nav=None,
                    holdings=[dict(code=x["stockid"].strip(), name=str(x.get("stockname")).strip(),
                                   shares=_num(x[skey]), weight=_num(x.get(wkey)) if wkey else None)
                              for x in rows])


class Capital:
    """群益投信。只有最新一份（無日期參數）。"""
    name, history = "群益", False
    _map = None

    def fund(self, code):
        if self._map is None:
            d = _json(_s.post("https://www.capitalfund.com.tw/CFWeb/api/etf/items", json={},
                              timeout=30), "群益清單")
            self._map = {(f.get("stockNo") or "").strip(): f["fundNo"]
                         for f in d.get("data") or [] if f.get("stockNo")}
        return self._map.get(code)

    def fetch(self, code, day):
        if day is not None:
            raise Skip("群益：不支援歷史")
        fid = self.fund(code)
        if not fid:
            raise Skip("群益：{} 不在清單".format(code))
        d = _json(_s.post("https://www.capitalfund.com.tw/CFWeb/api/etf/buyback",
                          json={"fundId": fid}, timeout=30), "群益")
        data = d.get("data") or {}
        pcf, st = data.get("pcf") or {}, data.get("stocks") or []
        if not st or not pcf.get("date2"):
            raise Skip("群益：{} 查無持股".format(code))
        return dict(data_date=dt.date(*map(int, re.split(r"[/-]", pcf["date2"])[:3])),
                    units=_num(pcf.get("totUnit")), nav=_num(pcf.get("nav")),
                    holdings=[dict(code=str(x["stocNo"]).strip(), name=str(x["stocName"]).strip(),
                                   shares=_num(x["share"]), weight=_num(x.get("weight")))
                              for x in st])


class Allianz:
    """安聯投信。需要 X-XSRF-TOKEN；Date 必須恰好是公告日（營業日）。"""
    name, history = "安聯", True
    query_is_pub = True
    B = "https://etf.allianzgi.com.tw"
    _tok = _map = None

    def _h(self):
        if self._tok is None:
            _s.get(self.B + "/list-trade", timeout=30)
            self._tok = _json(_s.get(self.B + "/webapi/api/AntiForgery/GetAntiForgeryToken",
                                     timeout=30), "安聯 token")["token"]
        return {"Content-Type": "application/json", "X-XSRF-TOKEN": self._tok,
                "Referer": self.B + "/list-trade"}

    def fund(self, code):
        if self._map is None:
            t = _json(_s.post(self.B + "/webapi/api/Category/GetFundTypeDropdownOptions",
                              json={}, headers=self._h(), timeout=30), "安聯類別")
            tid = next((e["Id"] for e in t.get("Entries") or [] if "主動" in (e.get("Name") or "")),
                       None)
            if tid is None:
                raise Skip("安聯：找不到主動式類別")
            f = _json(_s.post(self.B + "/webapi/api/Category/GetFundDropdownOptions",
                              json={"TypeId": tid}, headers=self._h(), timeout=30), "安聯清單")
            self._map = {(e.get("SecuritiesCode") or "").strip(): e["FundNo"]
                         for e in f.get("Entries") or [] if (e.get("SecuritiesCode") or "").strip()}
        return self._map.get(code)

    def fetch(self, code, day):
        fno = self.fund(code)
        if not fno:
            raise Skip("安聯：{} 不在清單".format(code))
        # 最新一份：往回試最多 7 天找到公告日
        days = [day] if day else [dt.date.today() - dt.timedelta(days=b) for b in range(8)]
        for q in days:
            d = _json(_s.post(self.B + "/webapi/api/Fund/GetFundTradeInfo", headers=self._h(),
                              json={"Type": 1, "Keyword": "", "FundNo": fno,
                                    "Date": q.strftime("%Y-%m-%d")}, timeout=30), "安聯")
            e = d.get("Entries")
            tb = [t for t in (e or {}).get("DynamicTableData") or []
                  if (t.get("TableTitle") or "").startswith("股票")]
            if e and tb and e.get("CNavDt"):
                rows = [r for r in tb[0].get("Rows") or [] if len(r) >= 5]
                return dict(data_date=dt.date(*map(int, e["CNavDt"][:10].split("-"))),
                            units=_num(e.get("CAnceTotalIssues")), nav=_num(e.get("CAnceTotalAv")),
                            holdings=[dict(code=str(r[1]).strip(), name=str(r[2]).strip(),
                                           shares=_num(r[3]), weight=_num(r[4])) for r in rows])
            if not day:
                time.sleep(REQUEST_DELAY)
        raise Skip("安聯：{} {} 查無持股".format(code, day))


class Cathay:
    """國泰投信。PCF 只有最新一份；總股數 = 每基數股數 × 流通基數（官方公告值換算）。"""
    name, history = "國泰", False
    B = "https://cwapi.cathaysite.com.tw/api/"
    _map = None

    def _r(self, path, params, what):
        d = _json(_s.get(self.B + path, params=params, timeout=30), what)
        if not d.get("success"):
            raise Skip("國泰：{} 失敗".format(what))
        return d.get("result")

    def fund(self, code):
        if self._map is None:
            rows = self._r("ETF/GetETFList", {"FundType": "", "PerPageCount": 9999, "status": 1},
                           "清單")
            rows = rows if isinstance(rows, list) else (rows or {}).get("list") or []
            self._map = {(r.get("stockCode") or "").strip(): r["fundCode"]
                         for r in rows if r.get("stockCode") and r.get("fundCode")}
        return self._map.get(code)

    def fetch(self, code, day):
        if day is not None:
            raise Skip("國泰：不支援歷史")
        fc = self.fund(code)
        if not fc:
            raise Skip("國泰：{} 不在清單".format(code))
        bs = self._r("BuySale/GetBuySale", {"FundCode": fc, "IsTest": "false", "status": 1}, "PCF")
        tot, basket = _num(bs.get("totUnit")), _num(bs.get("basketUnit"))
        if not (tot and basket and bs.get("preDateC")):
            raise Skip("國泰：{} PCF 缺單位數或基準日".format(code))
        st = self._r("BuySale/GetStocksList", {"FundCode": fc, "SearchDate": bs["date"],
                                                "IsTest": "false", "status": 1}, "成分股") or []
        if not st:
            raise Skip("國泰：{} 成分股為空".format(code))
        k = tot / basket
        return dict(data_date=dt.date(*map(int, re.split(r"[/-]", bs["preDateC"])[:3])),
                    units=tot, nav=_num(bs.get("aum")),
                    holdings=[dict(code=str(r["prod"]).strip(), name=str(r["prodName"]).strip(),
                                   shares=round(_num(r["basketShares"]) * k), weight=None)
                              for r in st if _num(r.get("basketShares")) is not None])


# ETF 名稱一律是「主動<投信><系列名>」，用前綴對應投信
ISSUERS = [("統一", President()), ("中信", Ctbc()), ("復華", Fuhhwa()),
           ("群益", Capital()), ("安聯", Allianz()), ("國泰", Cathay())]
# 名稱看得出是海外型的不收（台股持股要過半才有意義）
FOREIGN = ("美國", "全球", "日本", "越南", "世界", "印度", "ARK")


def registry():
    """目前掛牌、有接投信的台股主動式 ETF：[(code, name, issuer)]。"""
    import store
    q = store.q("""SELECT code, name FROM quotes
                   WHERE date = (SELECT MAX(date) FROM quotes) AND code LIKE '00%A'""")
    try:
        q = pd.concat([q, store.q("""SELECT code, name FROM tpex_quotes
                   WHERE date = (SELECT MAX(date) FROM tpex_quotes) AND code LIKE '00%A'""")])
    except Exception:
        pass
    out = []
    for _, r in q.iterrows():
        n = str(r["name"])
        if any(w in n for w in FOREIGN):
            continue
        iss = next((a for p, a in ISSUERS if n.startswith("主動" + p)), None)
        if iss:
            out.append((str(r["code"]), n, iss))
    return sorted(out)


# ======================================================================
# 存檔與寫入
# ======================================================================
def path_for(etf, ymd):
    d = DIR / etf
    d.mkdir(parents=True, exist_ok=True)
    return d / "{}.json.gz".format(ymd)


def save(etf, rec):
    """存成正規化後的 JSON（各家原始格式差太多）。同一個基準日重抓就覆蓋 ——
    投信偶爾會事後更正，以最後一次為準。"""
    tw = [h for h in rec["holdings"] if re.fullmatch(r"\d{4,6}", h["code"] or "")
          and h["shares"] is not None]
    if not tw:
        raise Skip("{}：沒有台股持股".format(etf))
    # 名稱看不出來的海外型（例如 00411A、00998A 持股大半是外國股票）：台股權重不到一半就不收
    w_all = sum(h["weight"] or 0 for h in rec["holdings"])
    w_tw = sum(h["weight"] or 0 for h in tw)
    if w_all > 0 and w_tw < 50:
        raise Skip("{}：台股權重只有 {:.0f}%，視為海外型".format(etf, w_tw))
    ymd = rec["data_date"].strftime("%Y%m%d")
    body = dict(etf=etf, data_date=rec["data_date"].isoformat(), units=rec["units"],
                nav=rec["nav"], fetched_at=dt.datetime.now().isoformat(timespec="seconds"),
                holdings=tw)
    path_for(etf, ymd).write_bytes(gzip.compress(json.dumps(body, ensure_ascii=False).encode()))
    return ymd


def update(n_days=DEFAULT_DAYS, budget=None, verbose=True):
    """每檔抓最新一份；支援歷史的投信另補最近 n_days 個交易日裡缺的日子。

    budget 限制「補歷史」這次最多發幾個請求（CI 用）。回傳寫入的檔案數。
    不會丟例外給呼叫端 —— 這是附加資訊，失敗不該擋住網站主線。"""
    try:
        reg = registry()
        cal = [dt.date(int(d[:4]), int(d[4:6]), int(d[6:]))
               for d in ingest.trading_calendar()[-n_days:]]
        n_new, spent, fails = 0, 0, []
        for code, name, iss in reg:
            try:
                save(code, iss.fetch(code, None))
                n_new += 1
            except Exception as e:
                fails.append("{} {}".format(code, str(e)[:60]))
            time.sleep(REQUEST_DELAY)
            if not iss.history:
                continue
            have = {p.name[:8] for p in (DIR / code).glob("*.json.gz")}
            pub = getattr(iss, "query_is_pub", False)
            for i in range(len(cal) - 1, -1, -1):  # 新的日子優先
                day = cal[i]
                if day.strftime("%Y%m%d") in have:
                    continue
                # 查詢日是公告日的投信，要查「下一個營業日」才拿得到這一天的持股
                if pub:
                    if i + 1 >= len(cal):
                        continue                   # 最新那天由上面「最新一份」負責
                    q = cal[i + 1]
                else:
                    q = day
                if budget is not None and spent >= budget:
                    break
                spent += 1
                try:
                    ymd = save(code, iss.fetch(code, q))
                    have.add(ymd)
                    n_new += 1
                except Exception:
                    pass
                time.sleep(REQUEST_DELAY)
        n = build()
        if verbose:
            print("主動 ETF：{} 檔、寫入 {} 份、補歷史請求 {} 次、持股表 {:,} 列".format(
                len(reg), n_new, spent, n), flush=True)
            for f in fails:
                print("  抓不到最新：{}".format(f), flush=True)
        return n_new
    except Exception as e:
        print("::warning::主動 ETF 更新失敗（不影響網站主線）：{}".format(e), flush=True)
        return 0


def build():
    """把 raw/active_etf/ 的每一份快照寫進兩張表：同一個 (ETF, 持股日) 以原始檔為準，
    表裡其他日子保留。

    不能每次整張重建：CI 冷啟動時（快取過期、從種子還原）raw/ 可能只有最近幾天，
    整張重建會把群益、國泰這種「只有最新一份、無法回補」的歷史永久丟掉。"""
    hold, meta = [], []
    for f in sorted(DIR.glob("*/*.json.gz")):
        try:
            b = json.loads(gzip.decompress(f.read_bytes()).decode())
        except Exception:
            continue
        d = b["data_date"]
        meta.append(dict(date=d, etf=b["etf"], units=b.get("units"), nav=b.get("nav")))
        for h in b["holdings"]:
            hold.append(dict(date=d, etf=b["etf"], code=h["code"], name=h.get("name"),
                             shares=h["shares"], weight=h.get("weight")))
    if not hold:
        return 0
    con = duckdb.connect(str(DB))
    try:
        for tbl, rows in ((HOLD_TABLE, hold), (META_TABLE, meta)):
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            con.register("_tmp", df)
            exists = con.execute("SELECT COUNT(*) FROM information_schema.tables "
                                 "WHERE table_name = ?", [tbl]).fetchone()[0]
            if not exists:
                con.execute("CREATE TABLE {} AS SELECT * FROM _tmp".format(tbl))
            else:
                con.execute("DELETE FROM {t} WHERE (etf, date) IN "
                            "(SELECT DISTINCT etf, date FROM _tmp)".format(t=tbl))
                cols = [r[0] for r in con.execute("DESCRIBE {}".format(tbl)).fetchall()]
                con.execute("INSERT INTO {} SELECT {} FROM _tmp".format(tbl, ", ".join(cols)))
            con.unregister("_tmp")
        n = con.execute("SELECT COUNT(*) FROM {}".format(HOLD_TABLE)).fetchone()[0]
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return n


# ======================================================================
# 給個股頁用：這檔股票最近被主動式 ETF 怎麼動
# ======================================================================
def classify_move(sa, sb, ua, ub):
    """一檔持股在相鄰兩個揭露日之間的動作 -> (kind, 變化比例)。

    sa/sb：前後兩天的股數；ua/ub：前後兩天的 ETF 發行單位數（拿不到給 None）。
    有單位數才判定加碼／減碼（看每單位持股數）；沒有就只陳述張數變化。"""
    if not sa and sb:
        return "新進", None
    if sa and not sb:
        return "出清", None
    if not sa and not sb:
        return "持平", None
    if ua and ub:
        chg = (sb / ub) / (sa / ua) - 1
        return ("加碼" if chg >= MOVE_TH else ("減碼" if chg <= -MOVE_TH else "持平")), chg
    chg = sb / sa - 1
    return ("張數增加" if chg >= MOVE_TH else ("張數減少" if chg <= -MOVE_TH else "持平")), chg


def moves(lookback=5):
    """每檔 ETF 最近 lookback 個揭露日、每檔持股的動作。

    回傳 DataFrame：etf, code, date, kind, spu_chg, units_known
      kind: 新進 / 出清 / 加碼 / 減碼 / 持平（每單位持股變動 < MOVE_TH）
            / 張數增加 / 張數減少（拿不到單位數時，只能陳述張數，不判定是不是經理人的決定）
    """
    import store
    h = store.q("SELECT date, etf, code, shares FROM {}".format(HOLD_TABLE))
    m = store.q("SELECT date, etf, units FROM {}".format(META_TABLE))
    h = h.merge(m, on=["date", "etf"], how="left")
    out = []
    # 相鄰兩個揭露日比較；中間缺日（沒抓到）就不比，否則會把好幾天的變化算成一天
    cal = pd.to_datetime(ingest.trading_calendar()[-400:])
    pos = {d: i for i, d in enumerate(cal)}
    for etf, g in h.groupby("etf"):
        dates = sorted(g["date"].unique())
        for a, b in zip(dates[-lookback - 1:-1], dates[-lookback:]):
            if a not in pos or b not in pos or pos[b] - pos[a] != 1:
                continue
            x = g[g["date"] == a].set_index("code")
            y = g[g["date"] == b].set_index("code")
            ua = x["units"].iloc[0] if len(x) else None
            ub = y["units"].iloc[0] if len(y) else None
            known = bool(ua and ub and ua == ua and ub == ub)
            for code in set(x.index) | set(y.index):
                kind, chg = classify_move(x["shares"].get(code, 0.0), y["shares"].get(code, 0.0),
                                         ua if known else None, ub if known else None)
                out.append(dict(etf=etf, code=code, date=b, kind=kind, spu_chg=chg,
                                units_known=known))
    return pd.DataFrame(out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    update(n_days=n)
