"""One-time calculation for a newly added strategy on the latest trading date."""
import argparse
import json
import os
from pathlib import Path
import sys

from runner import BASE, RELEASE, STATE, REPOS, bind_snapshot, load_module, publish


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('version', choices=REPOS)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    repo = REPOS[args.version]
    sys.path.insert(0, str(RELEASE / 'v1'))
    import qfq_data
    from fast_qfq import FastMarketData
    from verify_output import verify

    os.environ['TUSHARE_TOKEN'] = (BASE / 'private/tushare.token').read_text().strip()
    qfq_data.ROOT = STATE
    qfq_data.SOURCE = '腾讯前复权及流通市值（全量抓取）；Tushare当日日线校验'
    snapshot = FastMarketData().load()
    snapshot.audit.update(transport='tencent-native-json-full-window-v1',
                          full_refetch_each_trade_date=True, shared_snapshot=True)
    strategy = load_module(f'strategy_{args.version}', RELEASE / args.version / 'strategy.py')
    bind_snapshot(strategy, snapshot)
    selected = strategy.run_strategy()
    output = STATE / 'output' / snapshot.trade_date / args.version
    output.mkdir(parents=True, exist_ok=True)
    strategy.generate_html(selected, str(output / 'index.html'))
    strategy.save_data_json(selected, str(output / 'data.json'))
    data = json.loads((output / 'data.json').read_text(encoding='utf-8'))
    print(verify(data, (output / 'index.html').read_text(encoding='utf-8')), flush=True)
    if args.publish:
        print(f'Published commit {publish(repo, output, snapshot.trade_date)}', flush=True)


if __name__ == '__main__':
    main()
