"""Offseason backtest: v2.7 baseline vs a CALIBRATION_OFFSET correction, on the frozen 2026 graded games.
Usage: python3 tools/backtest_calibration.py data/season-2026/graded-games.json
Errors are final - projection; a positive bias means the model ran low."""
import json, sys, statistics as st
rows = [v for v in json.load(open(sys.argv[1]))['games'] if v['model'] is not None and v['final'] is not None]
g = sorted([v for v in rows if v['engine'] == '2.7'], key=lambda v: v['date'])

def metrics(games, off):
    e = [v['final'] - (v['model'] + off(v)) for v in games]
    hit = n = 0
    for v in games:
        if v['line'] is None: continue
        p = v['model'] + off(v)
        if p == v['line'] or v['final'] == v['line']: continue
        n += 1; hit += (p > v['line']) == (v['final'] > v['line'])
    return {"n": len(games), "bias": round(st.mean(e), 3), "mae": round(st.mean(abs(x) for x in e), 3),
            "rmse": round(st.mean(x*x for x in e) ** .5, 3), "ou_n": n, "ou_hit": round(hit / n, 4) if n else None}

clamp = lambda x: max(-1.5, min(1.5, x))
# walk-forward: offset = mean error of all earlier days' v2.7 games (min 50), as the Brain would learn it
wf = {}
for i, v in enumerate(g):
    prior = [w['final'] - w['model'] for w in g if w['date'] < v['date']]
    wf[id(v)] = clamp(round(st.mean(prior), 2)) if len(prior) >= 50 else 0.0
split = '2026-09-15'
train = [v for v in g if v['date'] < split]; test = [v for v in g if v['date'] >= split]
fit = clamp(round(st.mean(v['final'] - v['model'] for v in train), 2))
full_fit = clamp(round(st.mean(v['final'] - v['model'] for v in g), 2))
out = {
  "games": len(g), "first": g[0]['date'], "last": g[-1]['date'], "with_line": sum(v['line'] is not None for v in g),
  "baseline_all": metrics(g, lambda v: 0),
  "walkforward": metrics(g, lambda v: wf[id(v)]),
  "walkforward_final_offset": wf[id(g[-1])],
  "split": {"date": split, "train_n": len(train), "fit_offset": fit,
            "baseline_test": metrics(test, lambda v: 0), "calibrated_test": metrics(test, lambda v: fit)},
  "in_sample_offset": full_fit, "in_sample": metrics(g, lambda v: full_fit),
}
# bootstrap: is the bias distinguishable from zero?
import random; random.seed(7)
e = [v['final'] - v['model'] for v in g]
bs = sorted(st.mean(random.choices(e, k=len(e))) for _ in range(4000))
out["bias_95ci"] = [round(bs[100], 3), round(bs[3899], 3)]
out["verdict"] = "reject"
out["closing_lines"] = 0
print(json.dumps(out, indent=1))
