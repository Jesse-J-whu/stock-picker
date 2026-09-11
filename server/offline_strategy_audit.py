"""Compare every signal on the saved 5009-stock input, without publishing."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'v1'))
import pandas as pd
from qfq_data import aggregate
from runner import load_module, RELEASE

BASE = Path('/opt/stock-picker/state/transport-audit')


if __name__ == '__main__':
    expected = {row['code']:row for row in
                map(json.loads,(BASE/'expected-signals.jsonl').read_text().splitlines())}
    paths = sorted((BASE/'input').glob('*.csv.gz'))
    assert len(expected)==len(paths)==5009
    v1 = load_module('audit_v1',RELEASE/'v1/strategy.py')
    v4 = load_module('audit_v4',RELEASE/'v4/strategy.py')
    started=time.monotonic()
    differences=[]
    picks={'v1':[],'v4':[]}
    short={'v1':0,'v4':0}
    for index,path in enumerate(paths,1):
        frame=pd.read_csv(path,parse_dates=['date'])
        week,month,day=(aggregate(frame,p,n) for p,n in [('week',130),('month',60),('day',100)])
        s1=len(week)<30
        s4=len(month)<v4.MIN_MONTH or len(week)<v4.MIN_WEEK or len(day)<v4.MIN_DAY
        actual={'v1':bool(v1.apply_strategy(week).iloc[-1]) if not s1 else False,
                'v4':bool(v4.apply_strategy(month,week,day)) if not s4 else False,
                'short_v1':s1,'short_v4':s4}
        code=path.name[:6]
        if any(actual[k]!=expected[code][k] for k in actual):
            differences.append({'code':code,'actual':actual,'expected':expected[code]})
        for version in picks:
            if actual[version]:picks[version].append(code)
            short[version]+=actual['short_'+version]
        if index%500==0:print('Offline signals',index,flush=True)
    result={'completed':len(paths),'passed':not differences,'differences':differences,
            'selected':picks,'insufficient_history':short,'seconds':round(time.monotonic()-started)}
    (BASE/'strategy-summary.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    sys.exit(0 if result['passed'] else 1)
