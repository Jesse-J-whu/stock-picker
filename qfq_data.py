"""Validated AKShare/Tencent qfq history; Tushare only supplies current-day reference."""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from market_data import MarketDataError

SOURCE = "AKShare/腾讯前复权；Tushare当日日线校验"
ROOT = Path(__file__).resolve().parent


def beijing_now():
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def validate_history(frame, expected_date, reference_close):
    required = ["date", "open", "close", "high", "low", "vol"]
    if frame.empty or not set(required).issubset(frame.columns):
        raise MarketDataError("Empty or malformed history")
    frame = frame[required].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise MarketDataError("Duplicate or unordered dates")
    for column in required[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    if not np.isfinite(frame[required[1:]].to_numpy()).all():
        raise MarketDataError("Missing/nonfinite OHLCV")
    if (frame["vol"] < 0).any():
        raise MarketDataError("Negative volume")
    # Older qfq prices can be negative after large cumulative dividends.
    if (frame["high"] + 1e-8 < frame[["open", "close", "low"]].max(axis=1)).any():
        raise MarketDataError("Invalid high")
    if (frame["low"] - 1e-8 > frame[["open", "close", "high"]].min(axis=1)).any():
        raise MarketDataError("Invalid low")
    if frame["date"].iloc[-1].strftime("%Y-%m-%d") != expected_date:
        raise MarketDataError("History does not reach reference trading date")
    if abs(float(frame["close"].iloc[-1]) - float(reference_close)) > 0.011:
        raise MarketDataError("Latest qfq close disagrees with raw reference")
    return frame


def aggregate(frame, period, count):
    if period not in ("day", "week", "month"):
        raise ValueError("Unknown period")
    if period == "day":
        result = frame.copy()
    else:
        result = frame.set_index("date").resample(
            "W-FRI" if period == "week" else "ME"
        ).agg({"open": "first", "close": "last", "high": "max",
               "low": "min", "vol": "sum"}).dropna(subset=["close"]).reset_index()
    result["date"] = pd.to_datetime(result["date"]).dt.strftime("%Y-%m-%d")
    return result.tail(count).reset_index(drop=True)


class AkshareMarketData:
    def __init__(self):
        self.token = os.environ.get("TUSHARE_TOKEN")
        if not self.token:
            raise MarketDataError("Missing TUSHARE_TOKEN for reference data")
        self.cache = ROOT / ".cache" / "qfq-v2"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.frames = {}
        self.audit = {"price_adjustment": "qfq", "volume_basis": "unadjusted shares/hand",
                      "scope": "当日有成交的沪深A股，排除ST/PT/退市标记；不含北交所及当日停牌",
                      "source": SOURCE, "fetch_errors": [], "short_history": 0}
        self.stats = {"evaluated": 0, "insufficient_history": 0, "errors": 0}

    def request(self, api_name, params, fields):
        for attempt in range(3):
            try:
                response = requests.post("https://api.tushare.pro", json={
                    "api_name": api_name, "token": self.token,
                    "params": params, "fields": fields}, timeout=30)
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    # Do not print request bodies, headers or credentials.
                    raise MarketDataError(f"{api_name}: {payload.get('msg', 'API error')}")
                data = payload.get("data") or {}
                return pd.DataFrame(data.get("items", []), columns=data.get("fields", []))
            except (requests.RequestException, MarketDataError):
                if attempt == 2:
                    raise
                time.sleep(65 if api_name == "trade_cal" else 8)
        raise AssertionError("unreachable")

    def reference(self):
        now = beijing_now()
        # Before settlement use the preceding day, including manual morning runs.
        end = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
        cal = self.request("trade_cal", {
            "exchange": "SSE", "start_date": (end - timedelta(days=20)).strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"), "is_open": "1"}, "cal_date,is_open")
        if cal.empty:
            raise MarketDataError("No reference trading date")
        day = max(cal["cal_date"].astype(str))
        self.trade_date = datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d")
        raw = self.request("daily", {"trade_date": day},
                           "ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
        if len(raw) < 4000 or raw.ts_code.duplicated().any():
            raise MarketDataError(f"Reference snapshot incomplete: {len(raw)} rows")
        if set(raw.trade_date.astype(str)) != {day}:
            raise MarketDataError("Reference date mismatch")
        self.audit.update(trade_date=self.trade_date, reference_rows=len(raw))
        raw = raw[raw.ts_code.str.endswith((".SH", ".SZ"))].copy()
        self.audit["sh_sz_trading_rows"] = len(raw)
        self.raw = raw.set_index("ts_code")
        # Fresh names for the entire reference universe, never silently code-only.
        names = {}
        codes = list(self.raw.index)
        for offset in range(0, len(codes), 80):
            symbols = [c[-2:].lower() + c[:6] for c in codes[offset:offset + 80]]
            for attempt in range(3):
                try:
                    response = requests.get("https://qt.gtimg.cn/q=" + ",".join(symbols), timeout=20)
                    response.raise_for_status()
                    response.encoding = "gbk"
                    for symbol, content in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', response.text):
                        parts = content.split("~")
                        if len(parts) > 2 and parts[1].strip():
                            names[symbol[2:] + "." + symbol[:2].upper()] = parts[1].strip()
                    if all(c in names for c in codes[offset:offset + 80]):
                        break
                except requests.RequestException:
                    pass
                time.sleep(3)
        missing = set(codes) - set(names)
        if missing:
            raise MarketDataError(f"Missing current stock names: {len(missing)}")
        self.raw["name"] = [names[c] for c in self.raw.index]
        excluded = self.raw.name.str.contains("ST|PT|退", case=False, regex=True)
        self.audit["excluded_risk_names"] = int(excluded.sum())
        self.raw = self.raw.loc[~excluded].copy()
        self.audit["universe"] = len(self.raw)

    def fetch_one(self, ts_code):
        import akshare as ak
        path = self.cache / self.trade_date / (ts_code + ".csv.gz")
        ref_close = self.raw.loc[ts_code, "close"]
        if path.exists():
            try:
                return validate_history(pd.read_csv(path), self.trade_date, ref_close)
            except Exception:
                pass
        symbol = ts_code[-2:].lower() + ts_code[:6]
        # Every date gets a fresh full qfq series. Never splice differently adjusted histories.
        start = (datetime.strptime(self.trade_date, "%Y-%m-%d") - timedelta(days=5*365+60)).strftime("%Y%m%d")
        for attempt in range(3):
            try:
                frame = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start,
                    end_date=self.trade_date.replace("-", ""), adjust="qfq", timeout=15)
                # Tencent/AKShare calls volume "amount"; it is NOT monetary turnover.
                frame = validate_history(frame.rename(columns={"amount": "vol"}),
                                         self.trade_date, ref_close)
                path.parent.mkdir(parents=True, exist_ok=True)
                frame.to_csv(path, index=False, compression="gzip")
                return frame
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    def load(self):
        self.reference()
        start = time.monotonic()
        import akshare  # Import once before starting threads.
        print(f"Reference date {self.trade_date}; qfq universe {len(self.raw)}", flush=True)
        workers = min(6, max(1, int(os.environ.get("QFQ_WORKERS", "4"))))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.fetch_one, code): code for code in self.raw.index}
            for index, future in enumerate(as_completed(futures), 1):
                code = futures[future]
                try:
                    self.frames[code[:6]] = future.result()
                except Exception as error:
                    self.audit["fetch_errors"].append({"code": code, "error": str(error)[:500]})
                    print(f"Failed qfq {code}: {type(error).__name__}", flush=True)
                if index % 100 == 0 or index == len(futures):
                    print(f"QFQ {index}/{len(futures)}; valid {len(self.frames)}; elapsed {time.monotonic()-start:.0f}s", flush=True)
        self.audit.update(valid_histories=len(self.frames), elapsed_seconds=round(time.monotonic()-start),
                          coverage=len(self.frames)/len(self.raw))
        (ROOT / "data-quality.json").write_text(json.dumps(self.audit, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.audit["fetch_errors"]:
            raise MarketDataError("Incomplete qfq coverage; preserving previous published result")
        return self

    def active_stocks(self):
        return pd.DataFrame({"代码": [c[:6] for c in self.raw.index], "名称": self.raw.name.tolist()})

    def bars(self, stock_code, period, count):
        return aggregate(self.frames[stock_code], period, count)

    def display_quote(self, stock_code):
        row = self.raw.loc[[c for c in self.raw.index if c[:6] == stock_code][0]]
        return {"price": float(row.close), "change_pct": round((float(row.close)/float(row.pre_close)-1)*100, 2),
                "volume": float(row.vol), "turnover": float(row.amount), "name": row["name"],
                "open": float(row.open), "high": float(row.high), "low": float(row.low)}

    def metadata(self):
        return {**self.audit, "strategy_counts": self.stats,
                "note": "周/月包含当前未结束周期；成交量不复权；短历史股票单列，不算网络失败"}

    def page_status(self):
        return (f"行情日期 {self.trade_date} · 前复权 · 覆盖 {len(self.frames)}/{len(self.raw)} · "
                f"已计算 {self.stats['evaluated']} · 历史不足 {self.stats['insufficient_history']} · "
                "沪深当日交易股票（不含北交所/停牌/ST） · 周/月含未结束周期")

