"""Read-only live probe; never changes strategy results or uses secrets."""
import hashlib
import csv
import json
import os
import random
from pathlib import Path
import subprocess
import sys
import time

SYMBOLS = [
    'sz000001', 'sz000002', 'sz000333', 'sz000651', 'sz000858',
    'sz002001', 'sz002415', 'sz002594', 'sz300015', 'sz300750',
    'sh600000', 'sh600036', 'sh600519', 'sh600900', 'sh601318',
    'sh601398', 'sh603259', 'sh603976', 'sh688008', 'sh688981',
]


def choose_symbols(size, path=Path('.cache/tushare_stocks.csv')):
    if not 20 <= size <= 100:
        raise ValueError('sample_size must be between 20 and 100')
    if size == 20:
        return list(SYMBOLS)
    with path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    candidates = set()
    for row in rows:
        code = row.get('ts_code', '')
        if code.endswith(('.SH', '.SZ')) and len(code) == 9:
            candidates.add(code[-2:].lower() + code[:6])
    extra = sorted(candidates - set(SYMBOLS))
    if len(extra) < size - len(SYMBOLS):
        raise ValueError('Stock-list snapshot too small; refusing invented stock codes')
    return list(SYMBOLS) + random.Random(20260909).sample(extra, size - len(SYMBOLS))


def fetch(symbol, expected):
    import akshare as ak
    import pandas as pd
    frame = ak.stock_zh_a_hist_tx(
        symbol=symbol, start_date='20210901',
        end_date=expected.replace('-', ''), adjust='qfq', timeout=12,
    )
    if frame.empty:
        raise ValueError('empty response')
    prices = frame[['open', 'high', 'low', 'close']]
    issues = []
    if frame.isna().any().any():
        issues.append('null values')
    if frame.date.duplicated().any() or not frame.date.is_monotonic_increasing:
        issues.append('duplicate or unsorted dates')
    if not (prices.high >= prices[['open', 'close', 'low']].max(axis=1)).all():
        issues.append('invalid high')
    if not (prices.low <= prices[['open', 'close', 'high']].min(axis=1)).all():
        issues.append('invalid low')
    if str(frame.date.iloc[-1]) != expected:
        issues.append('latest date differs from expected; check suspension/calendar')
    if len(frame) < 1000:
        issues.append('less than 1000 daily bars')
    dates = pd.to_datetime(frame.date)
    return dict(rows=len(frame), first=str(frame.date.iloc[0]),
                last=str(frame.date.iloc[-1]), issues=issues,
                digest=hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest(),
                months=int(dates.dt.to_period('M').nunique()),
                last_close=float(frame.close.iloc[-1]))


def main():
    expected = os.environ['EXPECTED_DATE']
    from datetime import date
    date.fromisoformat(expected)
    symbols = choose_symbols(int(os.environ.get('SAMPLE_SIZE', '20')))
    Path('akshare-probe-manifest.json').write_text(json.dumps(dict(
        expected_date=expected, symbols=symbols, seed=20260909,
        selection='20 original samples plus random SH/SZ codes from cached stock list',
        caveat='Cached stock list may be stale. No BJ coverage. Short/stale data requires review.',
    ), indent=2), encoding='utf-8')
    results = []
    start = time.monotonic()
    for round_id in (1, 2):
        for symbol in symbols:
            tick = time.monotonic()
            result = dict(round=round_id, symbol=symbol)
            try:
                proc = subprocess.run(
                    [sys.executable, __file__, '--worker', symbol, expected],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode:
                    raise RuntimeError(proc.stderr[-1200:])
                result.update(json.loads(proc.stdout.strip().splitlines()[-1]))
                result['ok'] = not result['issues']
            except Exception as error:
                result.update(ok=False, error=str(error)[:1500])
            result['seconds'] = round(time.monotonic() - tick, 2)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            Path('akshare-probe-report.json').write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            time.sleep(1)
    mismatches = [s for s in symbols if
                  len({r.get('digest') for r in results if r['symbol'] == s}) != 1]
    ok = sum(r['ok'] for r in results)
    fetched = sum('digest' in r for r in results)
    summary = (f'AKShare Tencent qfq: {ok}/{len(results)} checks passed; '
               f'fetches returned data: {fetched}/{len(results)}; '
               f'repeated-data mismatches: {mismatches}; '
               f'elapsed: {time.monotonic() - start:.1f}s. '
               f'{len(symbols)} SH/SZ samples only; not full-market or long-term reliability proof.')
    print(summary, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
            out.write(summary + '\n')
    return 0 if ok == len(results) and not mismatches else 1


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        print(json.dumps(fetch(sys.argv[2], sys.argv[3])))
    else:
        sys.exit(main())
