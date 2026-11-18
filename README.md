# Ingenuity

MLB run-totals Monte Carlo research app. Single page: `index.html`.

## Automatic stats

Stats no longer need to be pasted by hand.

- **`tools/fetch_stats.py`** pulls season batting, 14-day form, pitchers, player
  platoon splits, team xwOBA, team OAA and catcher framing from Baseball Savant
  and the MLB Stats API. Every player is routed to his **current** club by MLB
  player id, read from each club's 40-man roster. Standard library only.
- **`.github/workflows/stats.yml`** runs it every morning and commits the result
  to `data/stats/` — only when the numbers actually changed.
- In the app: **Database → Import → Check for new stats.** Each file goes through
  the same preview a paste does. Nothing changes until you tap **Apply**.

Safeguards:

- If any source is down or returns thin data, the run fails and publishes
  nothing, so a half-refreshed set never reaches the app.
- Before opening day the run exits quietly and keeps last season's files.
- Every refresh is a commit, so the history of what the model was fed is kept
  and any refresh can be reverted.

Run it manually: **Actions → Refresh stats → Run workflow** (optionally with a
season), or locally with `python3 tools/fetch_stats.py`.

No API keys are used or stored. This repository is public; never commit keys.

## 2026 offseason backtest (frozen Nov 9, 2026)
`data/season-2026/graded-games.json` is the read-only 2026 graded-games snapshot (701 graded, 489 on v2.7). `python3 tools/backtest_calibration.py data/season-2026/graded-games.json` compares baseline v2.7 with a CALIBRATION_OFFSET correction. Result (`backtest-calibration.json`): **rejected**. The offset removes the +0.23 bias, but totals error and O/U hit rate get slightly worse, and the bias is within noise. 0 closing lines were captured, so closing-line value can't be measured yet.

`data/season-2026/graded-intervals.json` holds each game's model total, 80% band (p10, p90) and final score from the same backup (573 games, 489 on v2.7). `python3 tools/backtest_intervals.py data/season-2026/graded-intervals.json` tests whether a narrower variant ("v2.7-narrow", the band rescaled around the model total, v2.7 itself untouched) would sit closer to the 80% target. Result (`backtest-intervals.json`): **not needed**. v2.7's band covers **80.2%** of its 489 games (target 80%, mean width 9.9 runs), so it is already calibrated; the 82% that prompted the test did not reproduce on the games that carry a recorded band. Fitting the rescale on games before 2026-09-15 pushed it wider, not narrower (1.2x), and on the later games v2.7 covered 85.8%, so the gap is sample noise, not a systematic bias. v2.7 stays the published model.
