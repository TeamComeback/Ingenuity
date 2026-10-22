#!/usr/bin/env python3
"""
Ingenuity stat fetcher.

Pulls the same numbers that used to be pasted by hand into the Database tab
(Baseball Savant + the MLB Stats API), resolves every player to his CURRENT
club by MLB player id, and writes CSVs in the exact shapes the app's existing
importers already parse. The app then previews them through the same
planImport -> preview -> Apply path a paste goes through. Nothing here writes
to the model; it only produces files.

Fails loudly: every dataset is checked against a minimum row count, and if any
source is unreachable or comes back thin, the run exits non-zero BEFORE
touching data/stats/. A half-refreshed stat set never gets committed.

Standard library only, so the GitHub Actions runner needs no installs.

Usage:
    python3 tools/fetch_stats.py                 # current season
    python3 tools/fetch_stats.py --season 2026   # explicit season
    python3 tools/fetch_stats.py --out data/stats
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Ingenuity stat fetcher; +https://github.com/TeamComeback/ingenuity)"
SAVANT = "https://baseballsavant.mlb.com"
MLB = "https://statsapi.mlb.com/api/v1"

# The app's own linear weights (index.html, WOBA_W) so a wOBA built here is
# the same number the app would build from the same components.
WOBA_W = {"bb": 0.69, "hbp": 0.72, "s": 0.89, "d": 1.27, "t": 1.62, "hr": 2.10}

# Minimum rows per dataset. Well below an in-season count, well above what a
# broken or empty response produces. Below these, the run aborts.
MIN_ROWS = {
    "batters_season": 300,
    "pitchers_season": 300,
    "batters_14d": 150,
    "splits_players": 300,
    "team_defense": 30,
}


class FetchError(RuntimeError):
    pass


# player_id -> MLB primary position abbreviation ("P", "TWP" for two-way, "SS"...)
POSITIONS = {}
PITCHER_POSITIONS = {"P", "TWP"}


def is_pure_pitcher(pid):
    """A listed pitcher who is not two-way. Their token plate appearances
    (universal DH) would otherwise seed them as hitters, and the next pitcher
    import then writes batters-faced onto that hitter record as PA."""
    return POSITIONS.get(str(pid)) == "P"


class NotStarted(FetchError):
    """The season has no completed games yet. Not a failure: the previous
    season's files stay published and the run exits cleanly."""


def get(url, want="text", tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read().decode("utf-8-sig", errors="replace")
            if want == "json":
                return json.loads(body)
            # Savant answers a moved or broken CSV URL with an HTML page, not an
            # error status. Treat HTML as a failure rather than parse it as data.
            if body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                raise FetchError(f"got an HTML page instead of CSV (the URL has probably moved): {url}")
            return body
        except FetchError:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last = e
            time.sleep(3 * (i + 1))
    raise FetchError(f"unreachable after {tries} tries: {url} ({last})")


def read_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


def fnum(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except ValueError:
        return None


def ip_to_decimal(ip):
    """MLB writes innings as 142.1 meaning 142 and one third."""
    if ip in (None, ""):
        return None
    whole, _, frac = str(ip).partition(".")
    try:
        return int(whole) + (int(frac or 0) / 3.0)
    except ValueError:
        return None


def woba_from(stat):
    ab = stat.get("atBats") or 0
    h = stat.get("hits") or 0
    d = stat.get("doubles") or 0
    t = stat.get("triples") or 0
    hr = stat.get("homeRuns") or 0
    bb = stat.get("baseOnBalls") or 0
    ibb = stat.get("intentionalWalks") or 0
    hbp = stat.get("hitByPitch") or 0
    sf = stat.get("sacFlies") or 0
    singles = h - d - t - hr
    denom = ab + (bb - ibb) + sf + hbp
    if denom <= 0 or singles < 0:
        return None
    num = (WOBA_W["bb"] * (bb - ibb) + WOBA_W["hbp"] * hbp + WOBA_W["s"] * singles
           + WOBA_W["d"] * d + WOBA_W["t"] * t + WOBA_W["hr"] * hr)
    return num / denom


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def fmt(x, nd=3):
    return "" if x is None else f"{x:.{nd}f}"


def write_csv(path, header, rows, quote_all=False):
    with open(path, "w", newline="", encoding="utf-8") as f:
        # quote_all: the app's team-stats parser preserves empty cells only for
        # quoted CSV; unquoted, it drops blanks and shifts later columns left.
        w = csv.writer(f, quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL)
        w.writerow(header)
        # Fixed order. Savant's row order varies between identical requests,
        # which made unchanged data look changed: a commit and a site redeploy
        # every morning with nothing new in them.
        w.writerows(sorted(rows, key=lambda r: [str(c) for c in r]))


def et_today():
    # Eastern time is what the app keys its days on.
    return (dt.datetime.utcnow() - dt.timedelta(hours=4)).date()


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def roster_map(season, tnames):
    """player_id -> (fullName, current club).

    The club comes from each team's 40-man roster, which is the current club
    year-round. The sports/1/players list (what the app's 'Sync MLB rosters'
    reads) drops currentTeam for most players once their club is out of the
    postseason — in October 2026 it was missing for 512 of 662 hitters — so it
    is only a fallback here. A player on no 40-man roster and with no
    currentTeam is on no club, and stays unrouted on purpose."""
    j = get(f"{MLB}/sports/1/players?season={season}", "json")
    names, fallback = {}, {}
    for p in j.get("people", []):
        if p.get("id") is None or not p.get("fullName"):
            continue
        names[str(p["id"])] = p["fullName"]
        POSITIONS[str(p["id"])] = (p.get("primaryPosition") or {}).get("abbreviation")
        team = (p.get("currentTeam") or {}).get("name")
        if team:
            fallback[str(p["id"])] = team
    if len(names) < 1000:
        raise FetchError(f"MLB player list came back with only {len(names)} players")
    forty = {}
    for tid, tname in tnames.items():
        r = get(f"{MLB}/teams/{tid}/roster?rosterType=40Man&season={season}", "json")
        for e in r.get("roster", []):
            forty[str(e["person"]["id"])] = tname
            names.setdefault(str(e["person"]["id"]), e["person"].get("fullName"))
    if len(forty) < 1000:
        raise FetchError(f"40-man rosters came back with only {len(forty)} players")
    return {pid: (names.get(pid), forty.get(pid) or fallback.get(pid))
            for pid in set(names) | set(forty)}


TEAM_ALIASES = {}  # Savant short name / abbreviation (lowercased) -> MLB full name


def team_names():
    j = get(f"{MLB}/teams?sportId=1", "json")
    for t in j.get("teams", []):
        for k in ("teamName", "abbreviation", "clubName", "name"):
            if t.get(k):
                TEAM_ALIASES[t[k].lower()] = t["name"]
    return {str(t["id"]): t["name"] for t in j.get("teams", [])}


def last_regular_season_day(season, today):
    end = min(today, dt.date(season, 11, 30))
    if end < dt.date(season, 3, 1):
        raise NotStarted(f"the {season} season hasn't started yet — keeping the published files")
    j = get(f"{MLB}/schedule?sportId=1&gameType=R&startDate={season}-03-01&endDate={end.isoformat()}", "json")
    days = [d["date"] for d in j.get("dates", [])
            if any((g.get("status") or {}).get("abstractGameState") == "Final" for g in d.get("games", []))]
    if not days:
        raise NotStarted(f"no completed regular-season games in {season} yet — keeping the published files")
    return dt.date.fromisoformat(max(days))


def mlb_stats(params):
    j = get(f"{MLB}/stats?{params}", "json")
    stats = j.get("stats") or []
    return stats[0].get("splits", []) if stats else []


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------
def build(season, outdir):
    today = et_today()
    # Checked first: before opening day the leaderboards are empty, and that
    # should read as "nothing yet", not as a broken source.
    end = last_regular_season_day(season, today)
    start = end - dt.timedelta(days=13)
    tnames = team_names()
    ros = roster_map(season, tnames)
    unrouted = {}

    def club(pid, _stat_line_team=None):
        # Current club only. A stat line's team is where he PLAYED, which for a
        # released or traded player is not where he is now; using it would
        # credit a club with a player it no longer has.
        return ros.get(str(pid), (None, None))[1]

    def name_of(pid, fallback):
        return ros.get(str(pid), (fallback, None))[0] or fallback

    datasets = []

    # ---- batters, season: Savant custom leaderboard -----------------------
    txt = get(f"{SAVANT}/leaderboard/custom?year={season}&type=batter&filter=&min=1"
              "&selections=pa,k_percent,bb_percent,woba,xwoba&chart=false&x=pa&y=pa&r=no"
              "&chartType=beeswarm&sort=pa&sortDir=desc&csv=true")
    rows, skipped = [], 0
    for r in read_csv(txt):
        pid = r.get("player_id")
        if is_pure_pitcher(pid):
            continue
        team = club(pid)
        if not team:
            skipped += 1
            continue
        rows.append([name_of(pid, r.get("last_name, first_name", "")), pid, team, r.get("pa"),
                     r.get("woba"), r.get("xwoba"), r.get("bb_percent"), r.get("k_percent")])
    write_csv(os.path.join(outdir, "batters_season.csv"),
              ["name", "player_id", "team", "pa", "woba", "est_woba", "bb_percent", "k_percent"], rows)
    unrouted["batters_season"] = skipped
    datasets.append({"file": "batters_season.csv", "kind": "batters", "rowsAre": "hitters", "rows": len(rows),
                     "label": "Batters · season",
                     "source": "Baseball Savant custom leaderboard (wOBA, xwOBA, BB%, K%)"})

    # ---- pitchers, season: Savant expected stats + statcast + MLB IP/GS ----
    xs = {r["player_id"]: r for r in read_csv(get(
        f"{SAVANT}/leaderboard/expected_statistics?type=pitcher&year={season}&position=&team=&min=1&csv=true"))}
    sc = {r["player_id"]: r for r in read_csv(get(
        f"{SAVANT}/leaderboard/statcast?type=pitcher&year={season}&position=&team=&min=1"
        "&sort=barrels_per_bbe&sortDir=desc&csv=true"))}
    mlbp = {}
    for s in mlb_stats(f"stats=season&group=pitching&gameType=R&season={season}&sportId=1&playerPool=ALL&limit=4000"):
        pid = str((s.get("player") or {}).get("id"))
        mlbp[pid] = s
    rows, skipped, position_players = [], 0, 0
    for pid, r in xs.items():
        # Position players who pitched a mop-up inning are in Savant's pitcher
        # list. If they reach the staff, the app's pitcher test then treats
        # their BATTING rows as pitcher rows on the next import, and a regular
        # (e.g. a 490-PA infielder) silently drops out of the lineup. Only
        # MLB-listed pitchers and two-way players belong in this file.
        if POSITIONS.get(pid) not in PITCHER_POSITIONS:
            position_players += 1
            continue
        m = mlbp.get(pid, {})
        st = m.get("stat") or {}
        team = club(pid, (m.get("team") or {}).get("name"))
        if not team:
            skipped += 1
            continue
        ip = ip_to_decimal(st.get("inningsPitched"))
        s2 = sc.get(pid, {})
        rows.append([name_of(pid, r.get("last_name, first_name", "")), pid, team,
                     r.get("pa"), fmt(ip, 1), st.get("gamesStarted", ""),
                     r.get("era"), r.get("xera"), r.get("est_woba"),
                     s2.get("brl_percent", ""), s2.get("ev95percent", "")])
    write_csv(os.path.join(outdir, "pitchers_season.csv"),
              ["name", "player_id", "team", "pa", "ip", "gs", "era", "xera", "est_woba",
               "barrel_batted_rate", "hard_hit_percent"], rows)
    unrouted["pitchers_season"] = skipped
    unrouted["position_players_excluded_from_pitchers"] = position_players
    datasets.append({"file": "pitchers_season.csv", "kind": "pitchers", "rowsAre": "pitchers", "rows": len(rows),
                     "label": "Pitchers · season",
                     "source": "Savant expected stats (xwOBA, ERA, xERA) + Savant statcast (barrel%, hard-hit%) + MLB (IP, GS)"})

    # ---- hitters, last 14 days of the season so far: MLB date range -------
    rows, skipped = [], 0
    for s in mlb_stats(f"stats=byDateRange&group=hitting&gameType=R&season={season}&sportId=1"
                       f"&playerPool=ALL&limit=4000&startDate={start}&endDate={end}"):
        st = s.get("stat") or {}
        p = s.get("player") or {}
        pa = st.get("plateAppearances") or 0
        if pa <= 0 or is_pure_pitcher(p.get("id")):
            continue
        team = club(p.get("id"), (s.get("team") or {}).get("name"))
        if not team:
            skipped += 1
            continue
        rows.append([name_of(p.get("id"), p.get("fullName", "")), p.get("id"), team, pa,
                     fmt(woba_from(st)), pct(st.get("baseOnBalls") or 0, pa), pct(st.get("strikeOuts") or 0, pa)])
    write_csv(os.path.join(outdir, "batters_14d.csv"),
              ["name", "player_id", "team", "pa", "woba", "bb_percent", "k_percent"], rows)
    unrouted["batters_14d"] = skipped
    datasets.append({"file": "batters_14d.csv", "kind": "batters14", "rowsAre": "hitters", "rows": len(rows),
                     "label": f"14-day hitters · {start:%b %-d}–{end:%b %-d}",
                     "window": {"start": start.isoformat(), "end": end.isoformat()},
                     "source": "MLB Stats API date range; wOBA built with the app's own linear weights"})

    # ---- player platoon splits: MLB statSplits vs L / vs R ----------------
    rows, skipped = [], 0
    for s in mlb_stats(f"stats=statSplits&sitCodes=vl,vr&group=hitting&gameType=R&season={season}"
                       "&sportId=1&playerPool=ALL&limit=6000"):
        st = s.get("stat") or {}
        p = s.get("player") or {}
        code = (s.get("split") or {}).get("code")
        pa = st.get("plateAppearances") or 0
        if code not in ("vl", "vr") or pa <= 0 or is_pure_pitcher(p.get("id")):
            continue
        team = club(p.get("id"), (s.get("team") or {}).get("name"))
        if not team:
            skipped += 1
            continue
        rows.append([name_of(p.get("id"), p.get("fullName", "")), team,
                     "vs L" if code == "vl" else "vs R", pa, fmt(woba_from(st))])
    write_csv(os.path.join(outdir, "splits_players.csv"), ["name", "team", "split", "pa", "woba"], rows)
    unrouted["splits_players"] = skipped
    datasets.append({"file": "splits_players.csv", "kind": "psplits", "rows": len(rows),
                     "label": "Splits · by player",
                     "source": "MLB Stats API statSplits (vs LHP / vs RHP); wOBA built with the app's weights"})

    # ---- team defense: Savant team OAA + catcher framing summed by club ---
    oaa = {}
    for r in read_csv(get(f"{SAVANT}/leaderboard/outs_above_average?type=Fielding_Team&startYear={season}"
                          f"&endYear={season}&split=no&team=&range=year&min=10&pos=&roles=&viz=hide&csv=true")):
        name = tnames.get(str(r.get("team_id")), r.get("team_name"))
        oaa[name] = fnum(r.get("outs_above_average"))
    xteam = {}
    for r in read_csv(get(f"{SAVANT}/leaderboard/expected_statistics?type=batter-team&year={season}"
                          "&position=&team=&min=q&csv=true")):
        name = TEAM_ALIASES.get((r.get("team") or "").lower()) or TEAM_ALIASES.get((r.get("team_id") or "").lower())
        if name:
            xteam[name] = fnum(r.get("est_woba"))
    framing, catchers, fr_skipped = {}, {}, 0
    for r in read_csv(get(f"{SAVANT}/leaderboard/catcher-framing?type=catcher&seasonStart={season}"
                          f"&seasonEnd={season}&team=&min=q&sortColumn=rv_tot&sortDirection=desc&csv=true")):
        team = club(r.get("id"))
        rv = fnum(r.get("rv_tot"))
        if not team or rv is None:
            fr_skipped += 1
            continue
        framing[team] = framing.get(team, 0.0) + rv
        catchers[team] = catchers.get(team, 0) + 1
    rows = []
    for team in sorted(set(oaa) | set(framing) | set(xteam)):
        rows.append([team, "" if oaa.get(team) is None else f"{oaa[team]:.0f}",
                     "" if team not in framing else f"{framing[team]:.1f}",
                     "" if xteam.get(team) is None else f"{xteam[team]:.3f}"])
    if len(xteam) < 30:
        raise FetchError(f"team xwOBA matched only {len(xteam)} of 30 clubs")
    write_csv(os.path.join(outdir, "team_defense.csv"), ["Team", "OAA", "Framing", "xwOBA"], rows, quote_all=True)
    unrouted["framing_catchers"] = fr_skipped
    datasets.append({"file": "team_defense.csv", "kind": "teamstats", "rows": len(rows),
                     "label": "Team · OAA / framing / xwOBA",
                     "source": "Savant team OAA + team batting xwOBA + catcher framing summed by each catcher's CURRENT club (routed by player id)"})

    # ---- contract check: abort before anything is published ---------------
    thin = [f"{d['file']} has {d['rows']} rows (need {MIN_ROWS[d['file'][:-4]]})"
            for d in datasets if d["rows"] < MIN_ROWS[d["file"][:-4]]]
    if thin:
        raise FetchError("thin data, nothing published: " + "; ".join(thin))

    return {
        "app": "ingenuity",
        "schema": 1,
        "season": season,
        "generatedAt": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "dataThrough": end.isoformat(),
        "datasets": datasets,
        "unrouted": unrouted,
        "notes": [
            "Every player is credited to his CURRENT club (MLB player id), the same rule the app's roster sync uses.",
            "Framing is a season total, so a catcher traded mid-year is fully credited to his new club.",
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data", "stats"))
    a = ap.parse_args()
    season = a.season or et_today().year
    out = os.path.abspath(a.out)
    tmp = tempfile.mkdtemp(prefix="ingenuity-stats-")
    try:
        manifest = build(season, tmp)
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        # publish only after every dataset passed
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.isdir(out):
            shutil.rmtree(out)
        shutil.copytree(tmp, out)
    except NotStarted as e:
        print(f"Nothing to do — {e}")
        sys.exit(0)
    except FetchError as e:
        print(f"ABORTED — {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"season {season}, data through {manifest['dataThrough']}")
    for d in manifest["datasets"]:
        print(f"  {d['file']:22s} {d['rows']:5d} rows  ({d['kind']})")
    print(f"  unrouted: {manifest['unrouted']}")


if __name__ == "__main__":
    main()
