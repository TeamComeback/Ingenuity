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
