"""重大訊息分類（第 12 輪）：把觀測站的公告主旨分成幾類「公司事件」。

規則是看主旨文字訂的（2026-10-08，看了約 2.7 萬則的主旨、每類抽 12 則人工檢查，**沒有看任何之後的股價**）。
抽查後修過一次（同一天、仍然沒看股價）：會計異常拿掉「更換會計師」（多半是事務所輪調）、排除 iXBRL 申報欄位更正；
災害停工排除「非本公司」「無影響」「未受」。之後凍結，不再改。
每一類 = 包含其中一個字眼、而且不包含排除字眼。訂好就不改；要改得記進 RESEARCH_LOG 並重新登記。

分兩層：
  NEGATIVE（重大負面，第 12 輪的排雷條件用這五類）：
      司法調查、資安事件、財務危機、會計異常、災害停工
  INFO（只在網頁上顯示，不拿來排除 —— 方向不明或太常見）：
      澄清報導（傳聞可能是好消息也可能是壞消息）、主管機關裁罰（多半金額很小）、治理異動（董監經理人辭任或異動）

時間：公告有發言日期與時間。名單是收盤後產生的，所以「交易日 14:30 以前」的公告算當天，
14:30 以後（或非交易日）的算下一個交易日（effective_dates）。不會拿到收盤後才公布的東西。
"""
import re

import numpy as np
import pandas as pd

# (代號, 名稱, 包含, 排除)
NEGATIVE = [
    ("legal", "司法調查",
     r"檢調|調查局|搜索|約談|羈押|起訴書|偵查|涉嫌|刑事判決|檢察官.{0,12}起訴",
     r"不起訴|撤回起訴|無罪"),
    ("cyber", "資安事件",
     r"駭客|網路攻擊|資安事件|勒索軟體|勒索病毒|遭受.{0,6}攻擊",
     r"資安長|資安專責|資安主管|資安.{0,4}委員會|設置|任命"),
    ("distress", "財務危機",
     r"退票|跳票|存款不足|違約|重整|破產|紓困|無法償還|無法如期|債務協商|累積虧損達實收資本額|淨值為負|繼續經營.{0,4}疑慮|暫停交易",
     r"有重大訊息待公布|解除|撤回重整"),
    ("audit", "會計異常",
     r"重編|無法表示意見|否定意見|保留意見|會計師.{0,6}辭任|延後.{0,8}財務報告|財務報告.{0,8}延後",
     r"內部.{0,4}調整|輪調|獨立董事|iXBRL|申報資訊"),
    ("disaster", "災害停工",
     r"火災|爆炸|氣爆|停工|停產|工安|災害損失|淹水",
     r"並無|無重大|無影響|非本公司|未造成|未受|未有|沒有|復工|恢復生產|計畫停工及"),
]
INFO = [
    ("clarify", "澄清報導", r"澄清", r""),
    ("fine", "主管機關裁罰", r"裁罰|罰鍰|糾正|警告處分|處以罰", r""),
    ("gov", "治理異動",
     r"(董事長|總經理|財務主管|會計主管|稽核主管|獨立董事|發言人).{0,8}(辭任|解任|異動)|(辭任|解任).{0,6}(董事長|總經理|獨立董事)",
     r"代理發言人|逝世"),
]
CATS = NEGATIVE + INFO
NAME = {k: n for k, n, *_ in CATS}
NEG_KEYS = [k for k, *_ in NEGATIVE]
_RX = [(k, re.compile(inc), re.compile(exc) if exc else None) for k, _, inc, exc in CATS]


def classify(subject):
    """主旨 -> 這則公告屬於哪幾類（list，依 CATS 的順序）。"""
    s = str(subject)
    return [k for k, inc, exc in _RX if inc.search(s) and not (exc and exc.search(s))]


def effective_dates(ts, cal):
    """每則公告「算在哪個交易日」：交易日 14:30（含）以前發的算當天，之後或非交易日算下一個交易日。
    ts：公告時間（Series of Timestamp）；cal：交易日（排序好的 DatetimeIndex）。超過最後一個交易日的回傳 NaT。"""
    ts = pd.to_datetime(pd.Series(ts)).reset_index(drop=True)
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(cal).unique()))
    day = ts.dt.normalize()
    after = (ts - day) > pd.Timedelta(hours=14, minutes=30)
    pos = cal.searchsorted(day, side="left")              # 當天或之後第一個交易日
    pos = np.asarray(pos)
    on_trading = (pos < len(cal)) & (cal[np.minimum(pos, len(cal) - 1)] == day.to_numpy())
    pos = pos + (on_trading & after.to_numpy())             # 交易日收盤後才發的 -> 下一個交易日
    out = pd.Series(pd.NaT, index=ts.index, dtype="datetime64[ns]")
    ok = pos < len(cal)
    out[ok] = cal[pos[ok]]
    return out


def tag(news, cal):
    """mops_news 的表 -> 每則加上分類與生效交易日；只留有分類的。"""
    if news is None or not len(news):
        return pd.DataFrame(columns=["ts", "eff", "code", "name", "subject", "cats", "neg"])
    x = news.copy()
    x["cats"] = x["subject"].map(classify)
    x = x[x["cats"].map(len) > 0].copy()
    x["neg"] = x["cats"].map(lambda c: any(k in NEG_KEYS for k in c))
    x["eff"] = effective_dates(x["ts"], cal).to_numpy()
    return x.dropna(subset=["eff"]).reset_index(drop=True)


def recent_flag(d, tagged, window=10, only_negative=True):
    """特徵面板 d（date, code）的每一列：近 window 個交易日內（含當天）有沒有這家公司的負面事件。
    回傳布林 Series（與 d 同索引）。這就是第 12 輪影子名單的排除條件。"""
    ev = tagged[tagged["neg"]] if only_negative else tagged
    cal = pd.DatetimeIndex(sorted(d["date"].unique()))
    if not len(ev):
        return pd.Series(False, index=d.index)
    ei = cal.searchsorted(pd.DatetimeIndex(ev["eff"]))          # 事件的交易日位置
    ok = (ei < len(cal)) & (cal[np.minimum(ei, len(cal) - 1)] == pd.DatetimeIndex(ev["eff"]))
    by_code = {}
    for c, i in zip(ev["code"].astype(str).to_numpy()[ok], ei[ok]):
        by_code.setdefault(c, []).append(i)
    by_code = {c: np.sort(np.array(v)) for c, v in by_code.items()}
    di = cal.searchsorted(pd.DatetimeIndex(d["date"]))
    codes = d["code"].astype(str).to_numpy()
    flag = np.zeros(len(d), bool)
    for c, arr in by_code.items():
        m = codes == c
        if not m.any():
            continue
        rows = di[m]
        # 有沒有事件位置落在 [row - window + 1, row]
        lo = np.searchsorted(arr, rows - window + 1, side="left")
        hi = np.searchsorted(arr, rows, side="right")
        flag[np.where(m)[0]] = hi > lo
    return pd.Series(flag, index=d.index)
