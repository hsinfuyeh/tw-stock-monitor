"""L3 儲存層 — 原始檔 -> DuckDB。

重建策略：從 raw/ 全量重建。原始檔不可變，所以重建永遠可重現；
解析邏輯改了直接重跑即可，不必重抓。2.6M 列在 DuckDB 是秒級。
"""
import sys, time
import pandas as pd
import duckdb
import ingest, parse
from config import DB, DATASETS

TABLES = dict(mi_index="quotes", t86="inst", bwibbu="valuation", margin="margin")


def build(datasets=None, verbose=True):
    """從 raw/ 全量重建。

    寫到暫存檔再原子換掉，不是就地 DROP/CREATE。兩個理由：

    1. <b>DuckDB 不會就地回收空間</b>。反覆 DROP TABLE 再 CREATE TABLE，
       檔案只會一直長 —— 實測同一份資料，就地重建過幾十次之後是 873 MB，
       寫成新檔只有 143 MB，膨脹了 6 倍。
    2. 重建到一半掛掉時，舊的資料庫還完好無損；換檔是一瞬間的事。

    只重建部分資料集時不能換檔（會丟掉沒重建的表），那種情況退回就地更新。
    """
    datasets = datasets or list(DATASETS)
    days = ingest.trading_calendar()
    full = set(datasets) == set(DATASETS)
    target = DB.with_suffix(".building.duckdb") if full else DB
    if full and target.exists():
        target.unlink()
    con = duckdb.connect(str(target))
    for ds in datasets:
        rows, miss = [], 0
        t0 = time.time()
        for i, d in enumerate(days):
            j = ingest.load_raw(ds, d)
            if j is None:
                miss += 1; continue
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            rows.extend(parse.PARSERS[ds](j, iso))
            if verbose and (i + 1) % 400 == 0:
                print(f"  {ds}: {i+1}/{len(days)} 天, {len(rows):,} 列", flush=True)
        if not rows:
            print(f"  {ds}: 無資料，跳過"); continue
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        tbl = TABLES[ds]
        con.execute(f"DROP TABLE IF EXISTS {tbl}")
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE {tbl} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
        if verbose:
            print(f"  {ds} -> {tbl}: {len(df):,} 列, {df.date.nunique()} 天, "
                  f"缺 {miss} 天, {time.time()-t0:.0f}s", flush=True)
    # 刻意不建索引。實測這四個 (date, code) 索引佔掉 407 MB —— 整個檔案的 74% ——
    # 而且沒有任何好處：全表掃描 1.53s（有索引時反而是 1.98s），單檔查詢
    # 0.093s vs 0.083s 在誤差內。
    # 合理：DuckDB 是欄式儲存且有 zone map，這裡的查詢又幾乎都是整表掃進
    # pandas，ART 索引只是白佔空間。
    # 換檔前，把「不是 build 產生、但已經存在」的表搬過去。
    # exrights 就是這種：它由除權息流程另外建立，不在 DATASETS 裡。
    # 舊版是就地 DROP/CREATE，那些表自然會留著；改成換新檔之後如果不搬，
    # 就會安靜地被丟掉 —— 個股頁的除權息檢查項會整個消失，而且只有在
    # 有人打開個股頁時才會發現。
    if full and DB.exists():
        keep = set(TABLES.values())
        old = duckdb.connect(str(DB), read_only=True)
        try:
            others = [r[0] for r in old.execute("SHOW TABLES").fetchall()
                      if r[0] not in keep]
        finally:
            old.close()
        if others:
            src = str(DB).replace("\\", "/")
            con.execute(f"ATTACH '{src}' AS _old (READ_ONLY)")
            for t in others:
                con.execute(f"CREATE TABLE {t} AS SELECT * FROM _old.{t}")
            con.execute("DETACH _old")
            if verbose:
                print(f"  保留既有的表: {', '.join(others)}", flush=True)
    con.close()
    if full:
        import os
        # 換檔前的保險：新倉儲的交易日數不能比舊的少太多。
        #
        # 全量重建是「raw/ 裡有什麼就建什麼」。raw/ 不完整時（例如 CI 環境只帶了
        # 倉儲種子、沒帶原始檔封存），重建出來的倉儲只剩幾天，會把多年歷史整個蓋掉。
        # 2026-09-18 CI 就真的發生了：用兩天的原始檔重建，所有滾動視窗算不出來，
        # 候選名單變成空的 —— 那次是 publish 的自我檢查擋下來的，
        # 但倉儲不該等到下游才發現自己被掏空。
        if DB.exists():
            def _days(path):
                c = duckdb.connect(str(path), read_only=True)
                try:
                    return c.execute("SELECT COUNT(DISTINCT date) FROM quotes").fetchone()[0]
                except Exception:
                    return 0
                finally:
                    c.close()
            old_n, new_n = _days(DB), _days(target)
            if old_n and new_n < old_n * 0.9:
                target.unlink()
                raise RuntimeError(
                    "重建後的倉儲只有 {} 個交易日，舊的有 {} 個 —— 原始檔封存不完整，"
                    "拒絕覆蓋。舊倉儲保持不動。若是在 CI，應該用 store.append()。"
                    .format(new_n, old_n))
        os.replace(str(target), str(DB))
        if verbose:
            print(f"  倉儲 {DB.stat().st_size/1e6:.0f} MB", flush=True)


def append(days, verbose=True):
    """只把指定交易日寫進既有倉儲，不動其他日期。

    給 CI 用。CI 的環境只有倉儲種子、沒有完整的原始檔封存，所以不能全量重建
    （見 build() 換檔前那段保險）。這裡對每個資料集：先刪掉這幾天既有的列
    （重跑冪等），再把這幾天的原始檔解析後插入。

    只寫「四個資料集的原始檔都拿到」以外的也照寫 —— 哪些日子算數由呼叫端
    （ci_update 的齊備閘門）決定，這裡不重複判斷。
    """
    if not days:
        return 0
    con = duckdb.connect(str(DB))
    total = 0
    try:
        for ds, tbl in TABLES.items():
            rows = []
            for d in days:
                j = ingest.load_raw(ds, d)
                if j is None:
                    continue
                rows.extend(parse.PARSERS[ds](j, f"{d[:4]}-{d[4:6]}-{d[6:]}"))
            isos = ", ".join("DATE '{}-{}-{}'".format(d[:4], d[4:6], d[6:]) for d in days)
            con.execute(f"DELETE FROM {tbl} WHERE date IN ({isos})")
            if rows:
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"])
                # 欄位順序對齊既有表，避免 INSERT 依位置錯位
                cols = [r[0] for r in con.execute(f"DESCRIBE {tbl}").fetchall()]
                df = df.reindex(columns=cols)
                con.register("_tmp", df)
                con.execute(f"INSERT INTO {tbl} SELECT * FROM _tmp")
                con.unregister("_tmp")
            total += len(rows)
            if verbose:
                print(f"  {ds} -> {tbl}: 寫入 {len(rows):,} 列", flush=True)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return total


def q(sql, params=None):
    con = duckdb.connect(str(DB), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def summary():
    con = duckdb.connect(str(DB), read_only=True)
    out = []
    for t in ("quotes", "inst", "valuation", "margin"):
        try:
            r = con.execute(f"SELECT COUNT(*) n, MIN(date) a, MAX(date) b, "
                            f"COUNT(DISTINCT date) d FROM {t}").fetchone()
            out.append(f"  {t:<10} {r[0]:>9,} 列  {r[3]:>5} 天  {r[1]:%Y-%m-%d} ~ {r[2]:%Y-%m-%d}")
        except Exception:
            out.append(f"  {t:<10} (尚未建立)")
    con.close()
    return "\n".join(out)


if __name__ == "__main__":
    ds = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    build(ds)
    print(summary())
