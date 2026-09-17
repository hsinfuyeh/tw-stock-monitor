import ingest, time
days = ingest.trading_calendar()
print(f"交易日 {len(days)}: {days[0]} ~ {days[-1]}", flush=True)
for ds in ["mi_index", "t86", "bwibbu", "margin"]:
    missing = [d for d in days if not ingest.have(ds, d)]
    print(f"\n=== {ds}: 缺 {len(missing)} / {len(days)} 天 ===", flush=True)
    t0 = time.time()
    ingest.backfill([ds], days)
    print(f"=== {ds} 完成，耗時 {(time.time()-t0)/60:.1f} 分 ===", flush=True)
print("\nALL DONE", flush=True)
