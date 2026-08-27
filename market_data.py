"""Tushare 全市场日线缓存，并在本地聚合为策略所需的 K 线。"""

import os
import time
from datetime import date, timedelta

import pandas as pd
import requests


API_URL = "https://api.tushare.pro"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(ROOT_DIR, ".cache", "tushare_daily.csv.gz")
STOCKS_CACHE_PATH = os.path.join(ROOT_DIR, ".cache", "tushare_stocks.csv")
HISTORY_DAYS = 5 * 365 + 45
MIN_ROWS_PER_TRADE_DATE = 4_000
REQUEST_INTERVAL_SECONDS = 2.6  # 两个仓库同时首跑时，合计仍低于 50 次/分钟


class MarketDataError(RuntimeError):
    """行情数据无法满足完整性要求。"""


class TushareMarketData:
    def __init__(self, token=None):
        self.token = token or os.environ.get("TUSHARE_TOKEN")
        if not self.token:
            raise MarketDataError("未配置 TUSHARE_TOKEN GitHub Secret")
        self.session = requests.Session()
        self.daily = pd.DataFrame()
        self.stocks = pd.DataFrame()

    def _request(self, api_name, params, fields):
        response = self.session.post(
            API_URL,
            json={"api_name": api_name, "token": self.token, "params": params, "fields": fields},
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise MarketDataError(f"Tushare {api_name} 调用失败：{payload.get('msg', '未知错误')}")
        data = payload.get("data") or {}
        return pd.DataFrame(data.get("items", []), columns=data.get("fields", []))

    def _daily_for_date(self, trade_date):
        for attempt in range(5):
            try:
                return self._request("daily", {"trade_date": trade_date}, "ts_code,trade_date,open,high,low,close,pre_close,vol,amount")
            except MarketDataError as error:
                if "频率超限" not in str(error) or attempt == 4:
                    raise
                time.sleep(15 * (attempt + 1))

    def _load_cache(self):
        if not os.path.exists(CACHE_PATH):
            return pd.DataFrame()
        data = pd.read_csv(CACHE_PATH, dtype={"ts_code": str, "trade_date": str})
        required = {"ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"}
        if not required.issubset(data.columns):
            return pd.DataFrame()
        return data

    def _save_cache(self, data):
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        data.to_csv(CACHE_PATH, index=False, compression="gzip")

    def _load_stocks_cache(self):
        if not os.path.exists(STOCKS_CACHE_PATH):
            return pd.DataFrame()
        data = pd.read_csv(STOCKS_CACHE_PATH, dtype=str)
        return data if {"ts_code", "symbol", "name"}.issubset(data.columns) else pd.DataFrame()

    def load(self):
        """载入五年历史；缓存存在时只补齐缺少的交易日。"""
        today = date.today()
        start = (today - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        cached = self._load_cache()
        cached_dates = set(cached.get("trade_date", pd.Series(dtype=str)).astype(str))
        # trade_cal 对免费 Token 同样有极低频次限制。工作日空数据代表休市，
        # 可安全跳过；非空日线则必须达到全市场行数下限。
        required_dates = pd.bdate_range(start=start, end=end).strftime("%Y%m%d").tolist()
        missing_dates = [day for day in required_dates if day not in cached_dates]

        if missing_dates:
            print(f"  Tushare：补齐 {len(missing_dates)} 个交易日的全市场日线...")
        batches = [cached]
        for index, trade_date in enumerate(missing_dates, start=1):
            frame = self._daily_for_date(trade_date)
            if frame.empty:
                continue
            if len(frame) < MIN_ROWS_PER_TRADE_DATE:
                raise MarketDataError(
                    f"{trade_date} 日线数据不完整：仅 {len(frame)} 行，停止发布结果"
                )
            batches.append(frame)
            if index % 25 == 0 or index == len(missing_dates):
                print(f"    已同步 {index}/{len(missing_dates)} 个交易日")
            if index < len(missing_dates):
                time.sleep(REQUEST_INTERVAL_SECONDS)

        self.daily = pd.concat(batches, ignore_index=True)
        self.daily["trade_date"] = self.daily["trade_date"].astype(str)
        self.daily = self.daily.drop_duplicates(["ts_code", "trade_date"], keep="last")
        self.daily = self.daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        self._save_cache(self.daily)

        self.stocks = self._load_stocks_cache()
        if self.stocks.empty:
            # stock_basic 有每小时频次限制；它只用于展示名称，不能阻断行情筛选。
            self.stocks = self.daily[["ts_code"]].drop_duplicates().copy()
            self.stocks["symbol"] = self.stocks["ts_code"].str.split(".").str[0]
            self.stocks["name"] = self.stocks["symbol"]
            try:
                names = self._request("stock_basic", {"exchange": "", "list_status": "L"}, "ts_code,symbol,name")
                self.stocks = names[["ts_code", "symbol", "name"]]
                os.makedirs(os.path.dirname(STOCKS_CACHE_PATH), exist_ok=True)
                self.stocks.to_csv(STOCKS_CACHE_PATH, index=False)
            except MarketDataError:
                print("  股票名称接口暂时限流，使用代码作为展示名称，不影响选股计算")
        self.stocks = self.stocks[~self.stocks["name"].str.contains("ST|退|PT", regex=True, na=False)].reset_index(drop=True)
        self.daily = self.daily.set_index("ts_code", drop=False).sort_index()
        return self

    def active_stocks(self):
        return self.stocks.rename(columns={"symbol": "代码", "name": "名称"})[["代码", "名称"]]

    def daily_bars(self, stock_code):
        ts_code = self.stocks.loc[self.stocks["symbol"] == stock_code, "ts_code"]
        if ts_code.empty:
            return pd.DataFrame()
        try:
            bars = self.daily.loc[ts_code.iloc[0]].copy()
        except KeyError:
            return pd.DataFrame()
        if isinstance(bars, pd.Series):
            bars = bars.to_frame().T
        if bars.empty:
            return bars
        bars["date"] = pd.to_datetime(bars["trade_date"])
        bars = bars.sort_values("date")
        return bars[["date", "open", "close", "high", "low", "vol", "amount", "pre_close"]].reset_index(drop=True)

    def bars(self, stock_code, period, count):
        bars = self.daily_bars(stock_code)
        if bars.empty:
            return bars
        if period == "day":
            result = bars
        else:
            frequency = "W-FRI" if period == "week" else "ME"
            result = (
                bars.set_index("date")
                .resample(frequency)
                .agg({"open": "first", "close": "last", "high": "max", "low": "min", "vol": "sum", "amount": "sum", "pre_close": "first"})
                .dropna(subset=["close"])
                .reset_index()
            )
        result["date"] = pd.to_datetime(result["date"]).dt.strftime("%Y-%m-%d")
        return result.tail(count).reset_index(drop=True)

    def display_quote(self, stock_code):
        bars = self.daily_bars(stock_code)
        if bars.empty:
            return {}
        latest = bars.iloc[-1]
        previous = float(latest["pre_close"])
        close = float(latest["close"])
        return {
            "price": close,
            "change_pct": round((close - previous) / previous * 100, 2) if previous else 0,
            "volume": float(latest["vol"]),
            "turnover": float(latest["amount"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "open": float(latest["open"]),
        }
