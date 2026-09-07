"""前复权：原始 OHLC × 当日因子 / 样本末日因子；不调整成交量。"""
import os
import time
import numpy as np
import pandas as pd


def load_factors(provider):
    from market_data import ROOT_DIR, MarketDataError
    path = os.path.join(ROOT_DIR, ".cache", "adj_factors_v1.csv.gz")
    fields = ["ts_code", "trade_date", "adj_factor"]
    cached = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str}) if os.path.exists(path) else pd.DataFrame(columns=fields)
    dates = sorted(provider.daily["trade_date"].unique())
    known = set(cached["trade_date"])
    batches = [cached]
    missing = [d for d in dates if d not in known]
    print(f"前复权因子：需要补齐 {len(missing)} 个交易日", flush=True)
    for i, day in enumerate(missing):
        frame = provider._request("adj_factor", {"trade_date": day}, ",".join(fields))
        if frame.empty:
            raise MarketDataError(f"{day} 复权因子为空，停止计算")
        frame["trade_date"] = frame["trade_date"].astype(str)
        expected = set(provider.daily.loc[provider.daily["trade_date"] == day, "ts_code"])
        if not expected.issubset(set(frame["ts_code"])):
            raise MarketDataError(f"{day} 复权因子未覆盖当日行情，停止计算")
        batches.append(frame)
        if (i + 1) % 100 == 0:
            print(f"复权因子进度 {i + 1}/{len(missing)}", flush=True)
        time.sleep(2.6)
    factors = pd.concat(batches, ignore_index=True).drop_duplicates(["ts_code", "trade_date"], keep="last")
    factors.to_csv(path, index=False, compression="gzip")
    merged = provider.daily.merge(factors, on=["ts_code", "trade_date"], how="left", validate="one_to_one")
    if merged["adj_factor"].isna().any() or not np.isfinite(merged["adj_factor"]).all() or (merged["adj_factor"] <= 0).any():
        raise MarketDataError("复权因子缺失或非法，禁止退回未复权计算")
    return merged


def adjust_bars(bars):
    bars = bars.copy()
    factors = pd.to_numeric(bars["adj_factor"], errors="raise")
    if factors.isna().any() or (factors <= 0).any():
        raise ValueError("Invalid adjustment factor")
    ratio = factors / factors.iloc[-1]
    for column in ["open", "high", "low", "close"]:
        bars[column] = bars[column] * ratio
    return bars
