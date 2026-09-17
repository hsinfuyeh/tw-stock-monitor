"""TWSE 量化倉儲 — 全域設定"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW  = ROOT / "raw"
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
DB = DATA / "twse.duckdb"
for p in (RAW, DATA, REPORTS): p.mkdir(exist_ok=True)

BASE = "https://www.twse.com.tw"
UA = "Mozilla/5.0 (compatible; personal-research/1.0)"

# 回補起點。涵蓋 2020 COVID 崩跌、2022 空頭、2023-26 多頭 -> 多種波動環境
BACKFILL_START = "20190101"

DATASETS = {
    # 全市場日行情（OHLCV + 本益比）。核心資料集。
    "mi_index": dict(path="/rwd/zh/afterTrading/MI_INDEX",
                     params={"type": "ALLBUT0999"}),
    # 三大法人買賣超
    "t86":      dict(path="/rwd/zh/fund/T86",
                     params={"selectType": "ALL"}),
    # 個股本益比 / 殖利率 / 股價淨值比
    "bwibbu":   dict(path="/rwd/zh/afterTrading/BWIBBU_d",
                     params={"selectType": "ALL"}),
    # 融資融券餘額
    "margin":   dict(path="/rwd/zh/marginTrading/MI_MARGN",
                     params={"selectType": "ALL"}),
}

REQUEST_DELAY = 2.5      # 秒。TWSE 沒有公開速率限制，保守值。
MAX_RETRY = 4

# ---- 研究參數 ----
LIQ_MIN_AMT = 20_000_000   # 20 日均額門檻（元）。低於此視為不可交易。
FWD_HORIZONS = (1, 3, 5, 10, 20)   # 1/3/5/10 為原規格要求，20 供長天期驗證
COST_ROUND_TRIP = 0.006    # 台股來回成本：證交稅 0.3% + 手續費 2×0.1425%
