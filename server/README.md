# Dedicated server deployment

Production keeps its existing GitHub Pages URLs. A separate Linux `stockpicker`
account runs pinned copies of V1 and V4 in `/opt/stock-picker/current/{v1,v4}`.
It does not depend on the interactive SSH session or the owner's desktop.
Existing Caddy/Hermes configuration is not replaced.

## Calculation and timing

- `stock-picker.timer` starts at 16:00 Asia/Shanghai on weekdays. A trading
  calendar rejects holidays; the raw reference must cover today's settlement.
- Retry opportunities: 16:10, 16:20, 16:30, 16:40, 16:50, 17:30, 21:00.
  systemd never overlaps instances of this oneshot service. A fully verified
  publication makes subsequent same-day invocations exit without recalculating.
- `fast_qfq.py` requests the **entire five-year-plus-60-day** price history for
  every eligible stock on each new trading date. It uses Tencent's same qfq
  endpoint as AKShare 1.18.35, native JSON decoding, per-thread HTTPS sessions,
  and overlapping windows of no more than two calendar years / 640 bars.
- All overlapping OHLCV values must match. Daily current close must match the
  independent Tushare reference. No historical prices are rebased locally;
  no cross-day cache splicing or approximate adjustment factors are used.
- Both unchanged strategy functions share the snapshot; evaluation counters
  are separate. Full five-year history is retained even though V1 needs less.
- Both outputs must pass `verify_output.py` before either repository is pushed.
  A failed fetch or calculation blocks publication. Git races fail closed;
  pushes are never forced and newer trading dates cannot be overwritten.
- The server pushes only generated `docs/data.json` and `docs/index.html`.
  `server-pages.yml` validates and deploys them without re-fetching the market.
  Exact live JSON and HTML are checked after deployment, not merely git success.
- Target: afternoon publication, aiming for 16:30. Fixed trigger time does not
  guarantee upstream settlement readiness or GitHub Pages queue latency.

## Credentials and persistence

`/opt/stock-picker/private` is mode 0700. `tushare.token` is mode 0600. It was
transferred encrypted to a server-generated RSA key, never committed or logged.
Each repository has its own write deploy key, rather than a broad account token.
GitHub host keys come from GitHub's authenticated metadata API.

`state/.cache/qfq-v2/YYYY-MM-DD` holds validated same-day downloads. The newest
seven date folders are retained; these are reproducible market caches, not
credentials or user data. Failed runs keep completed downloads for retry.
`state/status.json` records running/validated/awaiting_pages/published/failed.
An initial full transport audit and offline signal audit live under
`state/transport-audit/`; they do not publish.

Service limits: 2600 MB RAM, three CPU-equivalents, 3-hour timeout, private temp,
no privilege escalation, read-only filesystem except state and publication
repositories. Dependencies are pinned in `requirements.lock`.

## Operations

```bash
sudo systemctl status stock-picker.timer stock-picker.service
sudo systemctl list-timers stock-picker.timer
sudo journalctl -u stock-picker.service -n 80 --no-pager
sudo cat /opt/stock-picker/state/status.json
sudo systemctl start stock-picker.service
```

To pause scheduling (without stopping Hermes or changing the website):
`sudo systemctl disable --now stock-picker.timer`.
Keep the old GitHub calculation workflow available as a manual fallback; remove
its cron only after the server's first complete live publication is confirmed.

To upgrade: create a new release containing reviewed copies of both repositories,
rerun both test suites and the server tests with that release, then change the
`current` symlink when no screening service is active. Updating the git clones
used for publication does **not** automatically change the strategy code.
