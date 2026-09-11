"""Independent SDK vs pooled transport comparison. Does not publish."""
import sys
from pathlib import Path
import json
import time
from datetime import date,timedelta
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'v1'))
import akshare as ak
import pandas as pd
from fast_qfq import full_history

CODES = ['sz000001','sz000002','sz000333','sz000651','sz000858',
         'sz002001','sz002415','sz002594','sz300015','sz300750',
         'sh600000','sh600036','sh600519','sh600900','sh601318',
         'sh601398','sh603259','sh603976','sh688008','sh688981']


def compare(symbol):
    end = date.today()-timedelta(days=1)
    start = end-timedelta(days=5*365+60)
    t = time.monotonic()
    fresh = full_history(symbol, end.isoformat())
    elapsed = time.monotonic()-t
    baseline = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start.strftime('%Y%m%d'),
                                    end_date=end.strftime('%Y%m%d'), adjust='qfq', timeout=20)
    baseline = baseline.rename(columns={'amount':'vol'})
    baseline['date'] = pd.to_datetime(baseline['date'])
    pd.testing.assert_frame_equal(fresh, baseline, check_dtype=False, check_exact=True)
    return {'code':symbol, 'identical':True, 'rows':len(fresh), 'fast_seconds':round(elapsed,3)}


if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(compare,CODES))
    print(json.dumps(results),flush=True)
