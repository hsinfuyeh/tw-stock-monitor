"""L2 正規化層 — 把 TWSE 各種不一致的回傳格式轉成乾淨紀錄。

這一層存在的唯一理由是 TWSE 的格式陷阱。已處理：
  1. MI_INDEX 回傳是 tables[] 陣列，個股表位置不固定 -> 用欄位簽章尋找，不寫死索引。
  2. MI_INDEX 的「漲跌價差」是絕對值，正負號在另一個 HTML 欄位
     (<p style= color:green>-</p>)。直接讀會得到全市場天天漲。
  3. 除權息標記藏在同一個 HTML 欄位 (<p>X</p>)；STOCK_DAY 則是 "X0.00" 前綴。
     跨越除權息的報酬必須標記，否則高殖利率股的報酬會被系統性低估。
  4. 本益比對虧損公司是 "-"、對 ETF 是 "0.00"，都不是數值 0。
  5. 民國年 -> 西元年。
  6. 證券分類：普通股 / 各類 ETF / 特別股 / TDR，報酬驅動來源完全不同，不可混排。
"""
import re

_TAG = re.compile(r"<[^>]+>")


def roc_to_iso(s):
    """115/09/11 -> 2026-09-11"""
    yy, mm, dd = s.strip().split("/")
    return f"{int(yy)+1911:04d}-{mm.zfill(2)}-{dd.zfill(2)}"


def num(s):
    """TWSE 數值欄位。'-'、''、'--' 代表無資料（不是 0）。"""
    if s is None: return None
    s = _TAG.sub("", str(s)).replace(",", "").strip()
    if s in ("", "-", "--", "---", "N/A", "不適用"): return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v


def parse_sign(html):
    """MI_INDEX 漲跌欄位 -> (sign, is_exdiv)。
    sign: +1 / -1 / 0 ；is_exdiv: 該日除權息（參考價已調整）。"""
    t = _TAG.sub("", str(html)).strip()
    if t == "X":  return 0, True      # 除權息，當日不計漲跌
    if t == "+":  return 1, False
    if t == "-":  return -1, False
    return 0, False


def classify(code, name):
    """證券分類。回傳 (大類, 次類)。"""
    c, n = code.strip(), name.strip()
    # 順序很重要：0050 / 0056 / 0051 也是 4 碼，若先套用 [0-9]{4} 會被誤判成普通股
    # （台灣最大的兩檔 ETF 就會混進普通股宇宙）。00 開頭一律先判為 ETF。
    if c.startswith("00"):
        if c.endswith("L") or "正2" in n or "正二" in n: return "etf", "leveraged"
        if c.endswith("R") or "反1" in n or "反一" in n: return "etf", "inverse"
        if c.endswith("B") or "債" in n:                 return "etf", "bond"
        if any(w in n for w in ("美國","標普","那斯","費城","日本","歐洲","中國","越南",
                                "印度","全球","世界","上証","滬深","韓國","亞太","新興")):
            return "etf", "foreign"
        if c.endswith("A") or "主動" in n:               return "etf", "active"
        if "高股息" in n or "優息" in n or "高息" in n:   return "etf", "dividend"
        return "etf", "domestic"
    if re.fullmatch(r"[0-9]{4}", c):
        return "common", "common"
    if re.fullmatch(r"[0-9]{4}[A-Z]", c):
        return "preferred", "preferred"          # 特別股，流動性極差
    if re.fullmatch(r"9[0-9]{4}", c):
        return "tdr", "tdr"                      # TDR / F股，受海外母股驅動
    # 權證：6 碼、0 開頭但不是 00（03xxxx~08xxxx 為主），部分帶字母尾碼。
    # 單日就有 1.4 萬檔，若誤判為 ETF 會讓倉儲膨脹數十倍且汙染 ETF 宇宙。
    if re.fullmatch(r"0[1-9][0-9]{4}[A-Z]?", c):
        return "warrant", "warrant"
    if re.fullmatch(r"00[0-9]{2,4}[A-Z]?", c):
        # ETF / ETN（台股 ETF 一律以 00 開頭）
        if c.endswith("L") or "正2" in n or "正二" in n: return "etf", "leveraged"
        if c.endswith("R") or "反1" in n or "反一" in n: return "etf", "inverse"
        if c.endswith("B") or "債" in n:                 return "etf", "bond"
        if any(w in n for w in ("美國","標普","那斯","費城","日本","歐洲","中國","越南",
                                "印度","全球","世界","上証","滬深","韓國","亞太","新興")):
            return "etf", "foreign"
        if c.endswith("A") or "主動" in n:               return "etf", "active"
        if "高股息" in n or "優息" in n or "高息" in n:   return "etf", "dividend"
        return "etf", "domestic"
    return "other", "other"


def _find_table(j, first_field):
    """MI_INDEX / MI_MARGN 回傳 tables[]；用欄位簽章找表，不依賴索引順序。"""
    for t in j.get("tables", []):
        f = t.get("fields")
        if f and f[0].strip() == first_field:
            return t
    return None


def _by_name(fields):
    return {f.strip(): i for i, f in enumerate(fields)}


# ---------------------------------------------------------------- MI_INDEX
def parse_mi_index(j, date):
    """全市場日行情 -> list of dict"""
    if not j: return []
    t = _find_table(j, "證券代號")
    if not t:
        # 舊格式回退：直接是 fields/data
        if j.get("fields") and j["fields"][0].strip() == "證券代號":
            t = {"fields": j["fields"], "data": j["data"]}
        else:
            return []
    ix = _by_name(t["fields"])
    out = []
    for r in t["data"]:
        code = r[ix["證券代號"]].strip()
        name = r[ix["證券名稱"]].strip()
        o, h, l, c = (num(r[ix[k]]) for k in ("開盤價", "最高價", "最低價", "收盤價"))
        if None in (o, h, l, c) or c <= 0 or o <= 0:
            continue                                   # 無成交或異常價，整列丟掉
        if not (h >= max(o, c) and l <= min(o, c) and h >= l):
            continue                                   # OHLC 邏輯不一致
        sign, exdiv = parse_sign(r[ix.get("漲跌(+/-)", -1)]) if "漲跌(+/-)" in ix else (0, False)
        chg = num(r[ix["漲跌價差"]]) if "漲跌價差" in ix else None
        cat, sub = classify(code, name)
        out.append(dict(
            date=date, code=code, name=name,
            open=o, high=h, low=l, close=c,
            volume=num(r[ix["成交股數"]]) or 0.0,
            trades=num(r[ix["成交筆數"]]) or 0.0,
            amount=num(r[ix["成交金額"]]) or 0.0,
            change=(chg * sign) if chg is not None else None,
            is_exdiv=exdiv,
            pe_raw=num(r[ix["本益比"]]) if "本益比" in ix else None,
            cat=cat, subcat=sub,
        ))
    return out


# ---------------------------------------------------------------- T86 法人
def parse_t86(j, date):
    if not j or j.get("stat") != "OK": return []
    fields, data = j.get("fields"), j.get("data")
    if not fields:
        t = _find_table(j, "證券代號")
        if not t: return []
        fields, data = t["fields"], t["data"]
    ix = _by_name(fields)
    def g(r, *cands):
        for k in cands:
            if k in ix: return num(r[ix[k]])
        return None
    out = []
    for r in data:
        code = r[ix["證券代號"]].strip()
        # selectType=ALL 會把權證一起回傳（單日 16,000+ 列，其中約 15,000 是權證）。
        # 只留普通股 / ETF / 特別股 / TDR，其餘丟棄。
        if classify(code, "")[0] in ("other", "warrant"):
            continue
        out.append(dict(
            date=date, code=code,
            foreign_net=g(r, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數"),
            trust_net=g(r, "投信買賣超股數"),
            dealer_net=g(r, "自營商買賣超股數"),
            total_net=g(r, "三大法人買賣超股數"),
        ))
    return out


# ---------------------------------------------------------------- BWIBBU 估值
def parse_bwibbu(j, date):
    if not j or j.get("stat") != "OK": return []
    fields, data = j.get("fields"), j.get("data")
    if not fields:
        t = _find_table(j, "證券代號")
        if not t: return []
        fields, data = t["fields"], t["data"]
    ix = _by_name(fields)
    out = []
    for r in data:
        pe = num(r[ix["本益比"]]) if "本益比" in ix else None
        pb = num(r[ix["股價淨值比"]]) if "股價淨值比" in ix else None
        dy = num(r[ix["殖利率(%)"]]) if "殖利率(%)" in ix else None
        # 本益比 0 對 ETF 是佔位，不是真的 0；虧損公司是 '-'（num 已轉 None）
        if pe is not None and pe <= 0: pe = None
        if pb is not None and pb <= 0: pb = None
        out.append(dict(date=date, code=r[ix["證券代號"]].strip(),
                        pe=pe, pb=pb, div_yield=dy))
    return out


# ---------------------------------------------------------------- MI_MARGN 融資券
def parse_margin(j, date):
    """融資融券餘額。

    兩個陷阱：
      1. 首欄叫「代號」，不是「股票代號」或「證券代號」。
      2. 欄位名稱有重複 —— 買進/賣出/前日餘額/今日餘額/次一營業日限額
         各出現兩次（融資一組、融券一組）。用 {名稱: 索引} 字典的話，
         後者會靜默覆蓋前者，融資餘額就會被讀成融券餘額，而且不會報錯。
         所以這裡一律按位置取值。

    欄位順序：0 代號 1 名稱 | 2-7 融資(買進,賣出,現金償還,前日餘額,今日餘額,限額)
              | 8-13 融券(買進,賣出,現券償還,前日餘額,今日餘額,限額) | 14 資券互抵 15 註記
    """
    if not j: return []
    t = (_find_table(j, "代號") or _find_table(j, "股票代號")
         or _find_table(j, "證券代號"))
    if not t: return []
    f = [x.strip() for x in t["fields"]]
    if len(f) < 14:
        return []
    # 以「前日餘額」出現兩次來確認欄位排列符合預期；不符就不猜，直接放棄該日。
    if f.count("前日餘額") != 2:
        return []
    i_m_bal, i_s_bal = 6, 12          # 融資今日餘額 / 融券今日餘額
    i_m_prev, i_s_prev = 5, 11        # 融資前日餘額 / 融券前日餘額
    out = []
    for r in t["data"]:
        if len(r) <= i_s_bal: continue
        code = r[0].strip()
        if classify(code, "")[0] in ("other", "warrant"):
            continue
        out.append(dict(
            date=date, code=code,
            margin_bal=num(r[i_m_bal]), margin_prev=num(r[i_m_prev]),
            short_bal=num(r[i_s_bal]), short_prev=num(r[i_s_prev]),
        ))
    return out


PARSERS = dict(mi_index=parse_mi_index, t86=parse_t86,
               bwibbu=parse_bwibbu, margin=parse_margin)
