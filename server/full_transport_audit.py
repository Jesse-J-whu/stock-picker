"""Full-universe transport validation against independently saved AKShare data.

Only changed histories are re-fetched with AKShare, since qfq basis can move
after the old snapshot date. No output here is eligible for publication.
"""
import json
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'v1'))
import pandas as pd
import akshare as ak
from fast_qfq import full_history
from qfq_data import validate_history

BASE = Path('/opt/stock-picker/state/transport-audit')
DAY = '2026-09-09'


def check(path):
    symbol = path.name[7:9].lower()+path.name[:6]
    for attempt in range(3):
        try:
            fresh = full_history(symbol, DAY)
            fresh = validate_history(fresh, DAY, fresh['close'].iloc[-1])
            old = pd.read_csv(path, parse_dates=['date'])
            result = {'code':path.name[:9], 'rows':len(fresh)}
            try:
                pd.testing.assert_frame_equal(fresh, old, check_dtype=False, check_exact=True)
                result['basis'] = 'matches_saved_akshare'
            except AssertionError:
                end = datetime.strptime(DAY, '%Y-%m-%d')
                baseline = ak.stock_zh_a_hist_tx(symbol=symbol,
                    start_date=(end-timedelta(days=5*365+60)).strftime('%Y%m%d'),
                    end_date=end.strftime('%Y%m%d'),adjust='qfq',timeout=20)
                baseline = baseline.rename(columns={'amount':'vol'})
                baseline['date'] = pd.to_datetime(baseline['date'])
                pd.testing.assert_frame_equal(fresh,baseline,check_dtype=False,check_exact=True)
                result['basis'] = 'changed_since_saved_but_matches_fresh_akshare'
            return result
        except Exception as error:
            if attempt == 2:
                return {'code':path.name[:9], 'error':type(error).__name__}
            time.sleep(3*(attempt+1))


if __name__ == '__main__':
    paths = sorted((BASE / 'input').glob('*.csv.gz'))
    if len(paths) != 5009:
        raise RuntimeError(f'Expected 5009 independent baseline files, got {len(paths)}')
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool, (BASE / 'results.jsonl').open('w') as out:
        futures = [pool.submit(check,path) for path in paths]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            out.write(json.dumps(result)+'\n');out.flush()
            if len(results)%100 == 0:
                print(f'Checked {len(results)}/5009; {time.monotonic()-started:.0f}s',flush=True)
    summary = {'completed':len(results), 'passed':all('error' not in r for r in results),
               'errors':[r for r in results if 'error' in r],
               'unchanged':sum(r.get('basis')=='matches_saved_akshare' for r in results),
               'refetched_changed':sum(r.get('basis')=='changed_since_saved_but_matches_fresh_akshare' for r in results),
               'seconds':round(time.monotonic()-started)}
    (BASE/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary),flush=True)
    sys.exit(0 if summary['passed'] else 1)
