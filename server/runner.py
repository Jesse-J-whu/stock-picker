"""Run both pinned strategies from one validated qfq snapshot; publish fail-closed."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import shutil
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = Path(os.environ.get('STOCK_PICKER_HOME', '/opt/stock-picker'))
RELEASE = Path(__file__).resolve().parents[1]
STATE = BASE / 'state'
REPOS = {'v1': 'stock-picker', 'v4': 'stock-picker-v4'}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def prune_cache(cache, keep=7):
    """Only remove reproducible date caches inside the exact owned cache directory."""
    root = cache.resolve()
    eligible = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.resolve().parent != root:
            continue
        try:
            parsed = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if parsed.isoformat() == folder.name:
            eligible.append(folder)
    for folder in sorted(eligible, key=lambda p:p.name, reverse=True)[keep:]:
        shutil.rmtree(folder)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind_snapshot(strategy, snapshot):
    # Histories are shared read-only; each strategy has independent accounting.
    strategy.MARKET_DATA = copy.copy(snapshot)
    strategy.MARKET_DATA.stats = {'evaluated': 0, 'insufficient_history': 0, 'errors': 0}
    strategy.prepare_market_data = lambda: None


def git(repo, *args):
    env = dict(os.environ)
    env['GIT_SSH_COMMAND'] = (
        f'ssh -i {BASE}/private/{repo}.key -o IdentitiesOnly=yes '
        f'-o UserKnownHostsFile={BASE}/private/github_known_hosts '
        '-o StrictHostKeyChecking=yes -o ConnectTimeout=20'
    )
    result = subprocess.run(['git', '-C', str(BASE / 'repos' / repo), *args],
                            env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f'Git {args[0]} failed for {repo}: {result.stderr[-600:]}')
    return result.stdout.strip()


def publish(repo, output, trade_date):
    folder = BASE / 'repos' / repo
    # Never discard local changes or force-push. Previous failed pushes may leave a commit.
    git(repo, 'fetch', 'origin', 'main')
    remote = json.loads(git(repo, 'show', 'origin/main:docs/data.json'))
    if remote.get('trade_date', '') > trade_date:
        raise RuntimeError(f'Refusing to overwrite newer {repo} data')
    git(repo, 'merge', '--ff-only', 'origin/main')
    for name in ('data.json', 'index.html'):
        (folder / 'docs' / name).write_bytes((output / name).read_bytes())
    git(repo, 'add', 'docs/data.json', 'docs/index.html')
    if git(repo, 'diff', '--cached', '--name-only'):
        git(repo, 'commit', '-m', f'Publish server-validated qfq results {trade_date}')
    # Normal push rejects races instead of overwriting another writer's update.
    git(repo, 'push', 'origin', 'HEAD:main')
    return git(repo, 'rev-parse', 'HEAD')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Calculate without publishing')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    import akshare as ak
    sys.path.insert(0, str(RELEASE / 'v1'))
    import qfq_data
    from verify_output import verify
    day = qfq_data.reference_day(ak.tool_trade_date_hist_sina(), now.date())
    if day != now.strftime('%Y%m%d') or now.hour < 16:
        print('Not an eligible settled trading-day window; no publication.', flush=True)
        return
    trade_date = now.strftime('%Y-%m-%d')
    status_path = STATE / 'status.json'
    previous = json.loads(status_path.read_text()) if status_path.exists() else {}
    if previous.get('trade_date') == trade_date and previous.get('state') == 'published' and not args.dry_run:
        print('Both Pages outputs already verified for today.', flush=True)
        return
    started = time.monotonic()
    status = {'trade_date': trade_date, 'state': 'running', 'started': now.isoformat(),
              'release': RELEASE.name, 'dry_run': args.dry_run}
    atomic_json(status_path, status)
    try:
        os.environ['TUSHARE_TOKEN'] = (BASE / 'private/tushare.token').read_text().strip()
        qfq_data.ROOT = STATE
        from fast_qfq import FastMarketData
        snapshot = FastMarketData().load()
        prune_cache(snapshot.cache)
        if snapshot.trade_date != trade_date:
            raise ValueError('Snapshot is not the requested trading date')
        status['coverage'] = snapshot.audit['coverage']
        status['universe'] = len(snapshot.frames)
        outputs, expected = {}, {}
        for version, repo in REPOS.items():
            strategy = load_module(f'strategy_{version}', RELEASE / version / 'strategy.py')
            bind_snapshot(strategy, snapshot)
            selected = strategy.run_strategy()
            output = STATE / 'output' / trade_date / version
            output.mkdir(parents=True, exist_ok=True)
            strategy.generate_html(selected, str(output / 'index.html'))
            strategy.save_data_json(selected, str(output / 'data.json'))
            data = json.loads((output / 'data.json').read_text(encoding='utf-8'))
            print(verify(data, (output / 'index.html').read_text(encoding='utf-8')), flush=True)
            outputs[repo] = output
            expected[repo] = data
        status.update(state='validated', counts={repo: data['count'] for repo, data in expected.items()},
                      calculation_seconds=round(time.monotonic()-started))
        atomic_json(status_path, status)
        if args.dry_run:
            return
        status['commits'] = {}
        for repo, output in outputs.items():
            status['commits'][repo] = publish(repo, output, trade_date)
        status['state'] = 'awaiting_pages'
        atomic_json(status_path, status)
        import requests
        pending = set(REPOS.values())
        for attempt in range(20):
            for repo in list(pending):
                try:
                    url = f'https://jesse-j-whu.github.io/{repo}'
                    response = requests.get(f'{url}/data.json?verify={time.time_ns()}', timeout=20)
                    response.raise_for_status()
                    html = requests.get(f'{url}/?verify={time.time_ns()}', timeout=20)
                    html.raise_for_status()
                    data = response.json()
                    verify(data, html.text)
                    if data == expected[repo] and html.text == (outputs[repo] / 'index.html').read_text(encoding='utf-8'):
                        pending.remove(repo)
                except (requests.RequestException, ValueError, KeyError):
                    pass
            if not pending:
                break
            time.sleep(20)
        if pending:
            raise RuntimeError(f'Pages verification pending: {sorted(pending)}')
        status.update(state='published', finished=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                      elapsed_seconds=round(time.monotonic()-started))
        atomic_json(status_path, status)
    except Exception as error:
        # No request bodies, secret environment or exception messages in persistent status.
        status.update(state='failed', failure_type=type(error).__name__,
                      elapsed_seconds=round(time.monotonic()-started))
        atomic_json(status_path, status)
        print(f'Run failed: {type(error).__name__}. Previous published data retained where not updated.', flush=True)
        raise


if __name__ == '__main__':
    main()
