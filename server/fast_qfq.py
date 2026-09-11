"""Tencent qfq transport: full history, native JSON, pooled HTTPS, overlapping windows.

Same endpoint and OHLCV fields as AKShare 1.18.35 stock_zh_a_hist_tx.
Fetch at most two calendar years per window (well below the 640-bar cap).
No old/new-date cache splicing and no locally guessed adjustment factors.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import threading
import pandas as pd
import requests
from qfq_data import AkshareMarketData, MarketDataError, validate_history

_local = threading.local()
URL = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
COLUMNS = ['date', 'open', 'close', 'high', 'low', 'vol']


def today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def session():
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
    return _local.session


def full_history(symbol, end_date):
    end = datetime.strptime(end_date, '%Y-%m-%d')
    start = end - timedelta(days=5*365+60)
    chunks = []
    for year in range(start.year, end.year+1, 2):
        until = min(datetime(year+1, 12, 31), end).strftime('%Y-%m-%d')
        # Tencent's dated endpoint omits today's bar even after the close.
        # Its open-ended qfq response adds today's bar to the historical 640.
        live_window = until == end_date and end.date() == today()
        request_end = '' if live_window else until
        response = session().get(URL, params={
            'param': f'{symbol},day,{year}-01-01,{request_end},640,qfq'}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get('code', 0) != 0:
            raise MarketDataError('Tencent history response unsuccessful')
        values = payload['data'][symbol]
        bars = values.get('qfqday', values.get('day', []))
        if bars:
            frame = pd.DataFrame([row[:6] for row in bars], columns=COLUMNS)
            frame['date'] = pd.to_datetime(frame['date'], errors='raise')
            for column in COLUMNS[1:]:
                frame[column] = pd.to_numeric(frame[column], errors='raise')
            if len(frame) > (641 if live_window else 640) or frame['date'].duplicated().any():
                raise MarketDataError('Malformed Tencent window')
            if (frame['date'] > pd.Timestamp(until)).any():
                raise MarketDataError('Tencent window exceeds requested end')
            # 640 trading bars must cover two calendar years unless a stock listed later.
            if len(frame) >= 640 and frame['date'].min() > pd.Timestamp(year, 1, 1):
                raise MarketDataError('Tencent window truncated')
            chunks.append(frame)
    if not chunks:
        raise MarketDataError('No Tencent history')
    result = pd.concat(chunks, ignore_index=True)
    overlap = result[result['date'].duplicated(keep=False)]
    if not overlap.empty and (overlap.groupby('date')[COLUMNS[1:]].nunique() > 1).any().any():
        raise MarketDataError('Qfq basis differs across overlapping windows')
    result = result.drop_duplicates('date').sort_values('date')
    return result.loc[result['date'].between(start, end)].reset_index(drop=True)


class FastMarketData(AkshareMarketData):
    def fetch_one(self, ts_code):
        import time
        path = self.cache / self.trade_date / (ts_code + '.csv.gz')
        ref_close = self.raw.loc[ts_code, 'close']
        if path.exists():
            try:
                return validate_history(pd.read_csv(path), self.trade_date, ref_close)
            except Exception:
                pass
        for attempt in range(3):
            try:
                frame = validate_history(full_history(ts_code[-2:].lower()+ts_code[:6], self.trade_date),
                                         self.trade_date, ref_close)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                frame.to_csv(temporary, index=False, compression='gzip')
                temporary.replace(path)
                return frame
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5*(attempt+1))
