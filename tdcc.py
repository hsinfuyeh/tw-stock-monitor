"""集保戶股權分散表（集保結算所開放資料）：每週一份，各股票依持股張數分級的人數與股數。

只顯示、不進選股與研究 —— 至少要累積一年以上才談得上檢驗。

為什麼現在就要開始存 ——

    官方開放資料只給「最新一週」（2026-10-07 實測：6.9 萬列全部是同一個資料日期），
    沒有歷史可以回補。今天不存，這一週就永遠沒有了。ToAlpha 那類網站的大戶週報，
    也是自己一週一週累積出來的。

    網站沒有定時排程，資料只在有人打開網頁或按 ⟳ 時更新（ci_update.extras 會呼叫這裡）。
    集保每週五後更新；之後一整週只要有一次更新跑到這裡就收得到。整整一週都沒人看的話，
    那一週會漏掉 —— update() 會在發現兩份之間隔超過 7 天時印警告。

資料：
    raw/tdcc/YYYYMMDD.csv.gz   原始 CSV（資料日期命名），倉儲可以從這裡完整重建
    holders 表                  date, code, level, people, shares, pct

持股分級（集保的定義，單位是股）：1 = 1–999、2 = 1,000–5,000、…、
15 = 1,000,001 以上（超過 1,000 張，一般說的「千張大戶」）、16 = 差異數調整、17 = 合計。
"""
import datetime as dt
import gzip
import io
import time

import duckdb
import pandas as pd
import requests

from config import DB, RAW, UA

URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
DIR = RAW / "tdcc"
TABLE = "holders"
FIELDS = ["資料日期", "證券代號", "持股分級", "人數", "股數", "占集保庫存數比例%"]


def parse(text):
    """開放資料的 CSV -> DataFrame（date, code, level, people, shares, pct）。
    表頭對不上就回傳空的，不猜欄位。只收股票與 ETF（代號 4–6 碼），權證等不收。"""
    df = pd.read_csv(io.StringIO(text.lstrip("﻿")), dtype=str)
    if [c.strip() for c in df.columns] != FIELDS:
        return pd.DataFrame(columns=["date", "code", "level", "people", "shares", "pct"])
    df.columns = ["date", "code", "level", "people", "shares", "pct"]
    df["code"] = df["code"].str.strip()
    df = df[df["code"].str.fullmatch(r"\d{4}|00\d{2,3}[A-Z]?")]
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"].str.strip(), format="%Y%m%d"),
        "code": df["code"],
        "level": pd.to_numeric(df["level"], errors="coerce").astype("Int64"),
        "people": pd.to_numeric(df["people"].str.replace(",", ""), errors="coerce"),
        "shares": pd.to_numeric(df["shares"].str.replace(",", ""), errors="coerce"),
        "pct": pd.to_numeric(df["pct"], errors="coerce"),
    })
    return out.dropna(subset=["level"]).reset_index(drop=True)


def stored_dates():
    return sorted(p.name[:8] for p in DIR.glob("*.csv.gz"))


def write(df):
    """寫進倉儲（同一個資料日期先刪再插，重跑冪等）。"""
    if df.empty:
        return 0
    con = duckdb.connect(str(DB))
    try:
        exists = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                             [TABLE]).fetchone()[0]
        con.register("_tmp", df)
        if exists:
            dates = ", ".join("DATE '{}'".format(d.strftime("%Y-%m-%d")) for d in df["date"].unique())
            con.execute(f"DELETE FROM {TABLE} WHERE date IN ({dates})")
            con.execute(f"INSERT INTO {TABLE} SELECT date, code, level, people, shares, pct FROM _tmp")
        else:
            con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return len(df)


def update(verbose=True):
    """抓最新一週；已經存過就只確認倉儲裡有。不會丟例外（附加資訊，不擋網站主線）。
    回傳這次新存的資料日期（YYYYMMDD），沒有新的回傳 None。"""
    try:
        r = None
        for attempt in range(3):
            try:
                r = requests.get(URL, headers={"User-Agent": UA}, timeout=60)
                if r.status_code == 200 and len(r.content) > 1000:
                    break
            except requests.RequestException:
                r = None
            time.sleep(5 * (attempt + 1))
        if r is None or r.status_code != 200:
            print("::warning::集保股權分散表抓不到（{}），這週之後再試".format(
                r.status_code if r is not None else "連線失敗"), flush=True)
            return None
        text = r.content.decode("utf-8-sig", "replace")
        df = parse(text)
        if df.empty:
            print("::warning::集保股權分散表格式變了（表頭對不上），這次不存", flush=True)
            return None
        dates = sorted(df["date"].dt.strftime("%Y%m%d").unique())
        if len(dates) != 1:
            print("::warning::集保股權分散表有 {} 個資料日期，預期只有最新一週".format(len(dates)), flush=True)
        day = dates[-1]
        DIR.mkdir(parents=True, exist_ok=True)
        before = stored_dates()
        new = day not in before
        if new:
            (DIR / (day + ".csv.gz")).write_bytes(gzip.compress(text.encode("utf-8")))
            if before:
                gap = (dt.datetime.strptime(day, "%Y%m%d") - dt.datetime.strptime(before[-1], "%Y%m%d")).days
                if gap > 7:
                    print("::warning::集保資料 {} 到 {} 隔了 {} 天，中間的週已經拿不到了".format(
                        before[-1], day, gap), flush=True)
        n = write(df)
        if verbose:
            print("集保股權分散表：資料日期 {}（{}），{:,} 列；已累積 {} 週".format(
                day, "新的一週" if new else "已經存過", n, len(stored_dates())), flush=True)
        return day if new else None
    except Exception as e:
        print("::warning::集保股權分散表更新失敗（不影響網站主線）：{}".format(e), flush=True)
        return None


def rebuild():
    """從 raw/tdcc/ 全部重建 holders 表（本機用）。"""
    frames = [parse(gzip.decompress(p.read_bytes()).decode("utf-8")) for p in sorted(DIR.glob("*.csv.gz"))]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return 0
    con = duckdb.connect(str(DB))
    try:
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
    finally:
        con.close()
    return write(pd.concat(frames, ignore_index=True))


if __name__ == "__main__":
    update()
