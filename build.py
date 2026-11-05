"""NBA Daily Digest builder.

Pulls today's slate from ESPN public endpoints (no API key) and generates
docs/index.html with:

- Betting lines & totals (when ESPN provides them)
- Schedule fatigue (B2B, 3-in-4, 4th road in 6 nights)
- Travel context (west-to-east night games, altitude in DEN/UTA)
- Playoff revenge matchups
- Offense/defense rankings + mismatch / lockdown tags
- Recent form (last 10, streak, home/road, close games) once enough games
- Roster age gaps
- Rivalry / incident notes filtered to current players
- Simple letdown heuristics
- Injury report (OUT players + notable day-to-day)
- Referee crew (when ESPN has posted it)

Designed to run free via GitHub Actions every morning.
"""
import json, math, os, re, sys, time, traceback, unicodedata, zipfile, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
_cache = {}


def norm(s):
    """Lower-case a name and strip accents so Doncic matches Doncic with a hacek."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    for ch in ".'\u2019":
        s = s.replace(ch, "")
    return " ".join(s.split())


def get(url, tries=3):
    """GET json, retrying brief network hiccups (timeouts, 5xx) before giving up."""
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:
                raise
        except Exception as e:
            last = e
        if k < tries - 1:
            time.sleep(2 * (k + 1))
    raise last


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Team meta: timezone + altitude (hardcoded, free, reliable)
# ---------------------------------------------------------------------------
TEAM_META = {
    "ATL": ("America/New_York", False), "BOS": ("America/New_York", False),
    "BKN": ("America/New_York", False), "CHA": ("America/New_York", False),
    "CHI": ("America/Chicago", False),  "CLE": ("America/New_York", False),
    "DAL": ("America/Chicago", False),  "DEN": ("America/Denver", True),
    "DET": ("America/New_York", False), "GS":  ("America/Los_Angeles", False),
    "HOU": ("America/Chicago", False),  "IND": ("America/New_York", False),
    "LAC": ("America/Los_Angeles", False), "LAL": ("America/Los_Angeles", False),
    "MEM": ("America/Chicago", False),  "MIA": ("America/New_York", False),
    "MIL": ("America/Chicago", False),  "MIN": ("America/Chicago", False),
    "NO":  ("America/Chicago", False),  "NY":  ("America/New_York", False),
    "OKC": ("America/Chicago", False),  "ORL": ("America/New_York", False),
    "PHI": ("America/New_York", False), "PHX": ("America/Phoenix", False),
    "POR": ("America/Los_Angeles", False), "SA":  ("America/Chicago", False),
    "SAC": ("America/Los_Angeles", False), "TOR": ("America/Toronto", False),
    "UTA": ("America/Denver", True),    "WSH": ("America/New_York", False),
}

WEST_COAST = {"GS", "LAC", "LAL", "POR", "SAC", "PHX"}  # for west-to-east travel


# ---------------------------------------------------------------------------
# Rich team schedule / form cache
# ---------------------------------------------------------------------------
_hist_cache = {}   # tid -> list of past game dicts (newest last)


def team_history(tid, season):
    """Full regular-season history for a team: list of dicts ordered by date.
    Each: {date, home, win, margin, opp_id, opp_abbr, our_score, opp_score}
    """
    if tid in _hist_cache:
        return _hist_cache[tid]
    try:
        data = get(f"{API}/teams/{tid}/schedule?season={season}&seasontype=2")
    except Exception as e:
        print(f"WARNING: schedule fetch failed for {tid}: {e}")
        _hist_cache[tid] = []
        return []
    out = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        status = comp.get("status", {}).get("type", {})
        if not status.get("completed"):
            continue
        me = next(c for c in comp["competitors"] if str(c["team"]["id"]) == str(tid))
        opp = next(c for c in comp["competitors"] if str(c["team"]["id"]) != str(tid))
        try:
            our = float(me.get("score", {}).get("value") or me.get("score") or 0)
            theirs = float(opp.get("score", {}).get("value") or opp.get("score") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "date": parse_dt(ev["date"]).astimezone(ET).date(),
            "home": me["homeAway"] == "home",
            "win": bool(me.get("winner")),
            "margin": our - theirs,
            "opp_id": opp["team"]["id"],
            "opp_abbr": opp["team"]["abbreviation"],
            "our_score": our,
            "opp_score": theirs,
        })
    out.sort(key=lambda g: g["date"])
    _hist_cache[tid] = out
    return out


def rest_and_travel(tid, abbr, today, season):
    """Return rest/travel info dict used by team_line and tags."""
    hist = team_history(tid, season)
    prior = [g for g in hist if g["date"] < today]
    day = lambda k: today - timedelta(days=k)

    played_dates = {g["date"] for g in prior}
    b2b = day(1) in played_dates
    third_in_four = sum(day(k) in played_dates for k in (1, 2, 3)) >= 2
    days_off = (today - max(g["date"] for g in prior)).days - 1 if prior else None

    road_streak = 0
    for g in reversed(prior):
        if not g["home"]:
            road_streak += 1
        else:
            break

    recent_road = sum(1 for g in prior if not g["home"] and (today - g["date"]).days <= 5)
    prev = prior[-1] if prior else None

    return {
        "b2b": b2b,
        "third_in_four": third_in_four,
        "days_off": days_off,
        "road_streak": road_streak,
        "recent_road_in_6": recent_road,
        "prev": prev,
        "hist": prior,
    }


def form_summary(info):
    """Compute last-10, streak, home/road, close-game from history."""
    hist = info.get("hist") or []
    if not hist:
        return None

    last10 = hist[-10:]
    wins10 = sum(1 for g in last10 if g["win"])
    losses10 = len(last10) - wins10

    streak_w = streak_l = 0
    for g in reversed(hist):
        if g["win"]:
            if streak_l:
                break
            streak_w += 1
        else:
            if streak_w:
                break
            streak_l += 1
    if streak_w:
        streak = f"W{streak_w}"
    elif streak_l:
        streak = f"L{streak_l}"
    else:
        streak = "—"

    home = [g for g in hist if g["home"]]
    road = [g for g in hist if not g["home"]]
    home_rec = f"{sum(g['win'] for g in home)}-{len(home)-sum(g['win'] for g in home)}" if home else "—"
    road_rec = f"{sum(g['win'] for g in road)}-{len(road)-sum(g['win'] for g in road)}" if road else "—"

    close = [g for g in hist if abs(g["margin"]) <= 5]
    close_rec = (f"{sum(g['win'] for g in close)}-{len(close)-sum(g['win'] for g in close)}"
                 if len(close) >= 3 else None)

    return {
        "last10": f"{wins10}-{losses10}",
        "streak": streak,
        "home": home_rec,
        "road": road_rec,
        "close": close_rec,
        "n_games": len(hist),
    }


def load_revenge():
    path = os.path.join(os.path.dirname(__file__) or ".", "data", "revenge.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rivalries():
    path = os.path.join(os.path.dirname(__file__) or ".", "data", "rivalries.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_injuries_by_team = None


def load_injuries():
    global _injuries_by_team
    if _injuries_by_team is not None:
        return _injuries_by_team
    _injuries_by_team = {}
    try:
        data = get(f"{API}/injuries")
    except Exception as e:
        print(f"WARNING: injuries fetch failed: {e}")
        return _injuries_by_team
    name_to_abbr = {}
    try:
        teams = get(f"{API}/teams")
        for t in teams["sports"][0]["leagues"][0]["teams"]:
            team = t["team"]
            abbr = team["abbreviation"]
            if abbr == "UTAH":
                abbr = "UTA"
            name_to_abbr[norm(team["displayName"])] = abbr
            name_to_abbr[norm(team["location"] + " " + team["name"])] = abbr
            name_to_abbr[norm(team["name"])] = abbr
    except Exception as e:
        print(f"WARNING: teams map for injuries failed: {e}")
    for entry in data.get("injuries", []):
        tname = entry.get("displayName") or ""
        abbr = name_to_abbr.get(norm(tname))
        if not abbr:
            continue
        players = []
        for inj in entry.get("injuries") or []:
            ath = inj.get("athlete") or {}
            players.append({
                "name": ath.get("displayName") or ath.get("shortName") or "?",
                "status": inj.get("status") or "?",
                "comment": (inj.get("shortComment") or inj.get("longComment") or "")[:160],
            })
        if players:
            _injuries_by_team[abbr] = players
    print(f"Injuries loaded for {len(_injuries_by_team)} teams")
    return _injuries_by_team


def injury_block(a_abbr, h_abbr):
    inj = load_injuries()
    tags = []
    parts = []
    for abbr in (a_abbr, h_abbr):
        players = inj.get(abbr) or []
        outs = [p for p in players if p["status"].lower() in ("out", "injured reserve", "out for season")]
        dtd = [p for p in players if "day" in p["status"].lower()]
        if outs:
            names = ", ".join(p["name"] for p in outs[:5])
            more = f" +{len(outs)-5}" if len(outs) > 5 else ""
            tags.append(("injury", f"{abbr} OUT: {names}{more}"))
            parts.append(f'<div class="prof m-injury"><b>{escape(abbr)} OUT</b>{escape(names)}{escape(more)}</div>')
        if dtd and len(dtd) >= 3:
            tags.append(("injury", f"{abbr} {len(dtd)} day-to-day"))
            if not outs:
                dnames = ", ".join(p["name"] for p in dtd[:4])
                parts.append(f'<div class="prof m-injury"><b>{escape(abbr)} DTD</b>{escape(dnames)}</div>')
    return tags, "".join(parts)


def fetch_officials(event_id):
    try:
        summary = get(f"{API}/summary?event={event_id}")
        officials = (summary.get("gameInfo") or {}).get("officials") or []
        return [o.get("displayName") or o.get("fullName") for o in officials if o.get("displayName") or o.get("fullName")]
    except Exception as e:
        print(f"WARNING: officials for {event_id}: {e}")
        return []


_rosters = {}
_ages = {}
AGE_GAP_TAG = 2.0


def _age_of(p):
    dob = p.get("dateOfBirth")
    if dob:
        try:
            born = datetime.strptime(dob[:10], "%Y-%m-%d").date()
            return (datetime.now(ET).date() - born).days / 365.25
        except ValueError:
            pass
    return p.get("age")


def roster_names(tid):
    if tid in _rosters:
        return _rosters[tid]
    try:
        data = get(f"{API}/teams/{tid}/roster")
    except Exception as e:
        print(f"WARNING: no roster for team {tid}: {e}")
        _rosters[tid] = None
        return None
    names, coaches, ages = set(), [], []
    for a in data.get("athletes", []):
        for p in a.get("items", [a]):
            if p.get("displayName"):
                names.add(norm(p["displayName"]))
                ages.append(_age_of(p))
    n_players = len(names)
    _ages[tid] = [x for x in ages if x]
    for c in data.get("coach", []):
        full = norm(f'{c.get("firstName", "")} {c.get("lastName", "")}')
        if full:
            names.add(full)
            coaches.append(full.title())
    print(f"Roster check team {tid}: {n_players} players, coach: {', '.join(coaches) or 'NOT FOUND'}")
    _rosters[tid] = names
    return names


def rivalry_notes(a_team, h_team, rivalries):
    key = "-".join(sorted([a_team["abbreviation"], h_team["abbreviation"]]))
    incidents = rivalries.get(key, [])
    print(f"Rivalry check {key}: {len(incidents)} incident(s) on file")
    if not incidents:
        return ""
    ra, rh = roster_names(a_team["id"]), roster_names(h_team["id"])
    if ra is None or rh is None:
        return ""
    current = ra | rh
    shown = [i for i in incidents
             if any(p["role"] != "referee" and norm(p["name"]) in current for p in i["people"])]
    print(f"Rivalry check {key}: {len(shown)} shown after roster filter")
    if not shown:
        return ""
    items = "".join(
        f'<li><b>{escape(i["date"])}</b> <span class="kind">{escape(i.get("type", ""))}</span> {escape(i["summary"])} '
        f'<a href="{escape(i["source_url"], quote=True)}" rel="noopener">{escape(i["source_name"])}</a></li>'
        for i in sorted(shown, key=lambda i: i["date"], reverse=True))
    return f'<details><summary>Rivalry notes ({len(shown)})</summary><ul>{items}</ul></details>'


NBA_STATS = "https://stats.nba.com/stats/leaguedashteamstats"
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com", "Accept": "application/json",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
}
GOOD, BAD = 10, 10
BLEND_GAMES = 20


def nba_stats_ratings(season_end):
    params = {"Conference": "", "DateFrom": "", "DateTo": "", "Division": "", "GameScope": "", "GameSegment": "",
              "LastNGames": 0, "LeagueID": "00", "Location": "", "MeasureType": "Advanced", "Month": 0,
              "OpponentTeamID": 0, "Outcome": "", "PORound": 0, "PaceAdjust": "N", "PerMode": "PerGame",
              "Period": 0, "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N", "Rank": "N",
              "Season": f"{season_end - 1}-{str(season_end)[2:]}", "SeasonSegment": "",
              "SeasonType": "Regular Season", "ShotClockRange": "", "StarterBench": "", "TeamID": 0,
              "TwoWay": 0, "VsConference": "", "VsDivision": ""}
    req = urllib.request.Request(NBA_STATS + "?" + urllib.parse.urlencode(params), headers=NBA_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        rs = json.load(r)["resultSets"][0]
    h = rs["headers"]
    i = {k: h.index(k) for k in ("TEAM_NAME", "GP", "OFF_RATING", "DEF_RATING")}
    return {norm(row[i["TEAM_NAME"]]): (row[i["OFF_RATING"]], row[i["DEF_RATING"]], row[i["GP"]])
            for row in rs["rowSet"] if row[i["GP"]]}


def espn_ratings(season_end):
    data = get(f"https://site.api.espn.com/apis/v2/sports/basketball/nba/standings?season={season_end}")
    out = {}
    for conf in data.get("children", []):
        for e in conf.get("standings", {}).get("entries", []):
            st = {s["name"]: s.get("value") for s in e.get("stats", [])}
            gp = st.get("gamesPlayed") or ((st.get("wins") or 0) + (st.get("losses") or 0))
            pf, pa = st.get("avgPointsFor"), st.get("avgPointsAgainst")
            if gp and pf and pa:
                out[norm(e["team"]["displayName"])] = (pf, pa, gp)
    return out


def load_ratings(today):
    season_end = today.year + 1 if today.month >= 8 else today.year
    try:
        cur, prior = nba_stats_ratings(season_end), nba_stats_ratings(season_end - 1)
        label = "offensive/defensive rating (NBA stats)"
    except Exception as e:
        print(f"NBA stats unavailable ({e}); using ESPN points scored/allowed per game")
        try:
            cur, prior = espn_ratings(season_end), espn_ratings(season_end - 1)
            label = "points scored/allowed per game (ESPN, not pace-adjusted)"
        except Exception as e2:
            print(f"WARNING: no team ratings: {e2}")
            return None
    blended = {}
    for k in set(cur) | set(prior):
        c, p = cur.get(k), prior.get(k)
        if c and p:
            w = min(c[2] / BLEND_GAMES, 1)
            blended[k] = (w * c[0] + (1 - w) * p[0], w * c[1] + (1 - w) * p[1])
        elif c or p:
            blended[k] = (c or p)[:2]
    n = len(blended)
    if n < 20:
        print(f"WARNING: only {n} teams have ratings; skipping mismatch flags")
        return None
    by_off = sorted(blended, key=lambda k: -blended[k][0])
    by_def = sorted(blended, key=lambda k: blended[k][1])
    ranks = {k: (by_off.index(k) + 1, by_def.index(k) + 1) for k in blended}
    games = [c[2] for c in cur.values()]
    if not games or max(games) == 0:
        note = f"Ranks use last season's {label} until the season starts."
    elif min(games) < BLEND_GAMES:
        note = f"Ranks use {label}, blended with last season until a team reaches {BLEND_GAMES} games."
    else:
        note = f"Ranks use this season's {label}."
    print(f"Ratings loaded for {n} teams: {label}")
    return {"ranks": ranks, "n": n, "note": note}


def chip(label, rank, n):
    cls = "elite" if rank <= GOOD else "weak" if rank > n - BAD else ""
    return f'<span class="chip {cls}">{label} #{rank}</span>'


def mismatch_tags(away, home, ratings):
    if not ratings:
        return [], ""
    n, ranks = ratings["n"], ratings["ranks"]
    ra = ranks.get(norm(away["team"]["displayName"]))
    rh = ranks.get(norm(home["team"]["displayName"]))
    if not ra or not rh:
        print(f"WARNING: no ranks for {away['team']['displayName']} or {home['team']['displayName']}")
        return [], ""
    tags = []
    for (x, rx), (y, ry) in (((away, ra), (home, rh)), ((home, rh), (away, ra))):
        xa, ya = x["team"]["abbreviation"], y["team"]["abbreviation"]
        if rx[0] <= GOOD and ry[1] > n - BAD:
            tags.append(("mismatch", f"{xa} elite offense (#{rx[0]}) vs. {ya} bottom-{BAD} defense (#{ry[1]})"))
        if rx[1] <= GOOD and ry[0] > n - BAD:
            tags.append(("lockdown", f"{xa} elite defense (#{rx[1]}) vs. {ya} bottom-{BAD} offense (#{ry[0]})"))
    row = lambda t, r: (f'<div class="prof m-match"><b>{escape(t["team"]["abbreviation"])}</b>'
                        f'{chip("Offense", r[0], n)}{chip("Defense", r[1], n)}</div>')
    return tags, row(away, ra) + row(home, rh)


def age_block(away, home):
    a, h = _ages.get(away["team"]["id"]), _ages.get(home["team"]["id"])
    if not a or not h:
        return [], ""
    ma, mh = sum(a) / len(a), sum(h) / len(h)
    aa, ha = away["team"]["abbreviation"], home["team"]["abbreviation"]
    gap = ma - mh
    older = aa if gap > 0 else ha
    tags = []
    if abs(gap) >= AGE_GAP_TAG:
        tags.append(("age", f"Age gap: {older} older by {abs(gap):.1f} yrs"))
    line = (f'<div class="prof m-age"><b>Avg age</b>{escape(aa)} {ma:.1f} &middot; {escape(ha)} {mh:.1f} '
            f'&middot; gap {abs(gap):.1f} yrs ({escape(older)} older)</div>')
    return tags, line


def odds_line(comp):
    odds = comp.get("odds")
    if not odds:
        return None
    if isinstance(odds, list) and odds:
        o = odds[0]
    elif isinstance(odds, dict):
        o = odds
    else:
        return None
    details = o.get("details") or o.get("spread")
    total = o.get("overUnder")
    parts = []
    if details:
        parts.append(str(details))
    if total is not None:
        parts.append(f"O/U {total}")
    return " · ".join(parts) if parts else None



# ---------------------------------------------------------------------------
# Crowd win % (Polymarket public API: free, no key). Price ~= implied win prob.
# ---------------------------------------------------------------------------
_crowd = None


def load_crowd():
    global _crowd
    if _crowd is not None:
        return _crowd
    _crowd = []
    try:
        url = ("https://gamma-api.polymarket.com/events?tag_slug=nba&active=true"
               "&closed=false&limit=200&order=startDate&ascending=false")
        for ev in get(url):
            st = ev.get("startTime") or ev.get("endDate")
            for m in ev.get("markets") or []:
                if m.get("sportsMarketType") != "moneyline":
                    continue
                outs, prices = m.get("outcomes"), m.get("outcomePrices")
                if isinstance(outs, str):
                    outs = json.loads(outs)
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if not st or not outs or not prices or len(outs) != 2:
                    continue
                p = [float(x) for x in prices]
                if sum(p) <= 0:
                    continue
                _crowd.append({"start": parse_dt(st),
                               "p": {norm(o): x / sum(p) for o, x in zip(outs, p)},
                               "liq": float(m.get("liquidity") or 0),
                               "cid": m.get("conditionId")})
    except Exception as e:
        print(f"WARNING: crowd odds unavailable: {e}")
    print(f"Crowd markets loaded: {len(_crowd)}")
    return _crowd


def crowd_flow(cid, sides):
    """Real-money flow on a Polymarket market: {outcome: (dollars, trade_count)}.
    A BUY of X counts for X. A SELL of X counts for the opponent, at the price of the
    equivalent opposite share (1 - price). Empty dict if unavailable."""
    if not cid:
        return {}
    flow = {}
    try:
        for page in range(6):  # up to 3,000 trades
            rows = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={page * 500}")
            for t in rows:
                o, size, price = norm(t.get("outcome") or ""), float(t["size"]), float(t["price"])
                if t.get("side") == "BUY":
                    target, dollars = o, size * price
                else:
                    others = [x for x in sides if x != o]
                    if len(others) != 1:
                        continue
                    target, dollars = others[0], size * (1 - price)
                d, n = flow.get(target, (0.0, 0))
                flow[target] = (d + dollars, n + 1)
            if len(rows) < 500:
                break
    except Exception as e:
        print(f"WARNING: crowd trades unavailable: {e}")
    return flow


def crowd_data(ev, away, home):
    """Polymarket crowd view for one game, or None when no market exists.
    -> {pa, ph, thin, money: ((dollars, trades), (dollars, trades)) or None}"""
    tip = parse_dt(ev["date"])
    gm = f'{away["team"]["abbreviation"]} at {home["team"]["abbreviation"]}'
    key = lambda t: norm(t["team"].get("name") or t["team"]["displayName"].split()[-1])
    ka, kh = key(away), key(home)
    pick = lambda d, k: next((v for n, v in d.items() if n == k or n.endswith(k) or k.endswith(n)), None)
    for c in load_crowd():
        if abs((c["start"] - tip).total_seconds()) > 12 * 3600:
            continue
        pa, ph = pick(c["p"], ka), pick(c["p"], kh)
        if pa is None or ph is None:
            continue
        flow = crowd_flow(c.get("cid"), list(c["p"]))
        fa, fh = pick(flow, ka), pick(flow, kh)
        print(f"Crowd: {gm} -> {pa*100:.0f}/{ph*100:.0f} (liquidity ${c['liq']:,.0f})")
        return {"pa": pa, "ph": ph, "thin": c["liq"] < 1000, "money": (fa, fh) if fa and fh else None}
    print(f"Crowd: no market for {gm}")
    return None


# ---------------------------------------------------------------------------
# Head-to-head: last 10 meetings (regular season + playoffs, newest first)
# ---------------------------------------------------------------------------
_sg_cache = {}
H2H_MAX, H2H_BACK = 10, 6
H2H_DOMINANT = 0.8   # 8-2 or better counts as dominating


def season_games(tid, season, stype):
    key = (tid, season, stype)
    if key in _sg_cache:
        return _sg_cache[key]
    out = []
    try:
        data = get(f"{API}/teams/{tid}/schedule?season={season}&seasontype={stype}")
        for ev in data.get("events", []):
            comp = ev["competitions"][0]
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            me = next(c for c in comp["competitors"] if str(c["team"]["id"]) == str(tid))
            opp = next(c for c in comp["competitors"] if str(c["team"]["id"]) != str(tid))
            sc = lambda c: float((c.get("score") or {}).get("value", 0) if isinstance(c.get("score"), dict) else c.get("score") or 0)
            out.append({"date": parse_dt(ev["date"]).astimezone(ET).date(), "opp_id": str(opp["team"]["id"]),
                        "win": bool(me.get("winner")), "our": sc(me), "their": sc(opp), "playoff": stype == 3})
    except Exception as e:
        print(f"WARNING: h2h schedule {tid} {season}/{stype}: {e}")
    _sg_cache[key] = out
    return out


def head_to_head(a_id, b_id, season, today):
    """Last meetings between two teams from team a's point of view, or None."""
    games = []
    for s in range(season, season - H2H_BACK - 1, -1):
        for st in (3, 2):
            games += [g for g in season_games(a_id, s, st) if g["opp_id"] == str(b_id) and g["date"] < today]
        if len(games) >= H2H_MAX:
            break
    games = sorted(games, key=lambda g: g["date"], reverse=True)[:H2H_MAX]
    if not games:
        return None
    aw = sum(g["win"] for g in games)
    streak_team_a, streak = games[0]["win"], 0
    for g in games:
        if g["win"] == streak_team_a:
            streak += 1
        else:
            break
    return {"n": len(games), "a": aw, "b": len(games) - aw,
            "margin": sum(g["our"] - g["their"] for g in games) / len(games),
            "last": games[0], "streak_a": streak_team_a, "streak": streak,
            "oldest": games[-1]["date"], "games": games}


# ---------------------------------------------------------------------------
# Season forecast (your forecast.json / forecast.zip in the data folder)
# ---------------------------------------------------------------------------
_fc = None


NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def name_key(name):
    """Normalized name without Jr./II style suffixes, for matching ESPN to the forecast file."""
    return " ".join(w for w in norm(name).split() if w not in NAME_SUFFIXES)


def slim_forecast(d):
    """Pull just the fields we show out of the big forecast file."""
    meta = d.get("meta", {})
    blend = d.get("blend", {})
    chg = {c["team"]: c for c in (d.get("coaching_changes") or {}).get("teams", [])}
    by_abbr = {}
    players = {}
    for pl in d.get("players", []):
        players.setdefault(pl.get("team"), []).append(pl)
    teams = {}
    for t in d["teams"]:
        ab = t["team"]
        dev, comp, post, cont = t.get("development") or {}, (t.get("profile") or {}).get("components") or {}, t.get("postseason") or {}, t.get("continuity") or {}
        pls = sorted(players.get(ab, []), key=lambda p: -(p.get("minutes") or 0))
        rank = lambda k: (comp.get(k) or {}).get("league_rank")
        coach = chg.get(ab)
        rot = [{"name": p["name"], "key": name_key(p["name"]), "pos": p.get("position"), "age": p.get("age"),
                "mpg": p.get("projected_mpg") or 0, "minutes": p.get("minutes") or 0,
                "impact": p.get("team_impact_contribution") or 0, "rookie": bool(p.get("is_rookie")),
                "pick": p.get("draft_pick")} for p in pls[:12]]
        teams[norm(t["name"])] = {
            "sd": (t.get("uncertainty_display") or {}).get("sd"), "team_minutes": t.get("minutes") or 1,
            "players": rot,
            "abbr": ab, "name": t["name"],
            "wins": t.get("display_wins"), "losses": t.get("display_losses"),
            "win_low": t.get("win_low"), "win_high": t.get("win_high"),
            "rank": t.get("rank"), "conf": t.get("conference"), "conf_rank": t.get("conference_rank"),
            "strength": t.get("neutral_strength"), "pd": t.get("display_point_differential"),
            "playoffs": post.get("playoffs_probability"), "no1": post.get("no1_seed_probability"),
            "sched_rank": (t.get("schedule") or {}).get("difficulty_rank"),
            "rotation_age": dev.get("weighted_age"),
            "young_share": dev.get("young_minutes_share"), "age32_share": dev.get("age32_minutes_share"),
            "returning_pct": cont.get("returning_minutes_pct"),
            "spacing": rank("spacing"), "rebounding": rank("rebounding"), "rim": rank("rim_protection"),
            "coach": (coach or {}).get("name"), "coach_first_time": (coach or {}).get("first_time_nba_head_coach"),
            "top": [{"name": p["name"], "age": p.get("age"), "mpg": p.get("projected_mpg")} for p in pls[:5]],
            "watch": [{"name": p["name"], "note": p.get("injury")} for p in pls if p.get("injury")][:3],
        }
    sds = sorted((v["sd"] for v in teams.values() if v.get("sd")), reverse=True)
    for v in teams.values():
        v["sd_rank"] = sds.index(v["sd"]) + 1 if v.get("sd") in sds else None
    return {"as_of": meta.get("as_of"), "updated": meta.get("model_updated"),
            "home_adv": blend.get("home_advantage", 2.0), "sd": blend.get("game_residual_sd", 13.6), "teams": teams}


def load_forecast():
    global _fc
    if _fc is not None:
        return _fc
    _fc = {}
    base = os.path.join(os.path.dirname(__file__) or ".", "data")
    try:
        raw = None
        for name in ("forecast.zip", "forecast.json"):
            path = os.path.join(base, name)
            if not os.path.exists(path):
                continue
            if name.endswith(".zip"):
                with zipfile.ZipFile(path) as z:
                    raw = json.loads(z.read(next(n for n in z.namelist() if n.endswith(".json"))))
            else:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            break
        if raw:
            _fc = slim_forecast(raw)
            print(f"Forecast loaded: {len(_fc['teams'])} teams (as of {_fc['as_of']})")
        else:
            print("Forecast file not found in data/ (forecast.zip or forecast.json); skipping")
    except Exception as e:
        print(f"WARNING: forecast unavailable: {e}")
    return _fc


def fc_team(team):
    f = load_forecast()
    if not f:
        return None
    ts = f["teams"]
    full = norm(team["displayName"])
    if full in ts:
        return ts[full]
    nick = norm(team.get("name") or "")
    return next((v for k, v in ts.items() if nick and k.endswith(nick)), None)


def model_win_prob(fa, fh):
    """P(home wins) = NormalCDF((home strength - away strength + home advantage) / game SD)."""
    f = load_forecast()
    edge = fh["strength"] - fa["strength"] + f["home_adv"]
    return 0.5 * (1 + math.erf(edge / f["sd"] / math.sqrt(2))), edge


# ---------------------------------------------------------------------------
# First half vs. second half, from the quarter-by-quarter scores ESPN lists on its scoreboard.
# Regular season only; overtime is left out of both halves. Finished games are kept in
# data/halves.json so each run only reads the days that are not finished yet.
# ---------------------------------------------------------------------------
HALF_MIN_GAMES = 3      # show the numbers from this many games
HALF_TAG_GAMES = 8      # only call a team a fast starter / strong finisher from this many games
HALF_TAG_SHIFT = 3.0    # points per game, second-half margin minus first-half margin
MAX_HALF_DAYS = 250
_halves = None


def _linescores(c):
    out = []
    for x in c.get("linescores") or []:
        v = x.get("value") if isinstance(x, dict) else x
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            return None
    return out


def load_halves(today, season):
    global _halves
    if _halves is not None:
        return _halves
    _halves = {"teams": {}, "n": 0, "games": 0, "bad": 0, "lg": {}, "note": ""}
    path = os.path.join(os.path.dirname(__file__) or ".", "data", "halves.json")
    cache = {"games": {}, "dates_done": []}
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
            cache.setdefault("games", {})
            cache.setdefault("dates_done", [])
    except Exception as e:
        print(f"WARNING: could not read halves cache: {e}")
    start = SEASON_STARTS.get(season)
    if not start:
        _halves["note"] = "no season start date on file"
        return _halves
    d, calls, new, bad = date(*start), 0, 0, 0
    try:
        while d <= today and calls < MAX_HALF_DAYS:
            ds = d.strftime("%Y%m%d")
            if ds in cache["dates_done"] and d < today - timedelta(days=1):
                d += timedelta(days=1)
                continue
            calls += 1
            sb = get(f"{API}/scoreboard?dates={ds}")
            all_final = True
            for ev in sb.get("events", []):
                comp = ev["competitions"][0]
                if not comp.get("status", {}).get("type", {}).get("completed"):
                    all_final = False
                    continue
                stype = (ev.get("season") or {}).get("type") or (sb.get("season") or {}).get("type")
                if stype is not None and stype != 2:
                    continue
                if ev["id"] in cache["games"]:
                    continue
                teams = {}
                for c in comp["competitors"]:
                    ls = _linescores(c)
                    sc = float((c.get("score") or {}).get("value", 0) if isinstance(c.get("score"), dict) else c.get("score") or 0)
                    if not ls or len(ls) < 4 or abs(sum(ls) - sc) > 1.5:
                        teams = None
                        break
                    teams[str(c["team"]["id"])] = [ls[0] + ls[1], ls[2] + ls[3], sum(ls[4:])]
                if not teams or len(teams) != 2:
                    bad += 1
                    all_final = False
                    continue
                cache["games"][ev["id"]] = {"d": ds, "t": teams}
                new += 1
            if all_final and ds not in cache["dates_done"]:
                cache["dates_done"].append(ds)
            d += timedelta(days=1)
    except Exception as e:
        print(f"WARNING: halves update stopped early: {e}")
    if new:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
        except Exception as e:
            print(f"WARNING: could not save halves cache: {e}")
    per = {}
    for g in sorted(cache["games"].values(), key=lambda g: g["d"]):
        (ida, a), (idb, b) = list(g["t"].items())
        for tid, mine, theirs in ((ida, a, b), (idb, b, a)):
            per.setdefault(tid, []).append({"d": g["d"], "f1": mine[0], "a1": theirs[0], "f2": mine[1], "a2": theirs[1]})
    res = {}
    for tid, gl in per.items():
        n = len(gl)
        avg = lambda k: sum(x[k] for x in gl) / n
        r = {"g": n, "f1": avg("f1"), "a1": avg("a1"), "f2": avg("f2"), "a2": avg("a2"), "games": gl[-10:]}
        r["m1"], r["m2"] = r["f1"] - r["a1"], r["f2"] - r["a2"]
        r["shift_f"], r["shift_a"], r["shift"] = r["f2"] - r["f1"], r["a2"] - r["a1"], r["m2"] - r["m1"]
        res[tid] = r
    pool = [k for k in res if res[k]["g"] >= HALF_MIN_GAMES] or list(res)
    for key, name in (("m1", "r1"), ("m2", "r2"), ("shift", "rs")):
        for i, k in enumerate(sorted(pool, key=lambda k: res[k][key], reverse=True), 1):
            res[k][name] = i
    _halves = {"teams": res, "n": len(pool), "games": len(cache["games"]), "bad": bad, "lg": {}, "note": ""}
    print(f"Halves: {len(cache['games'])} games cached ({new} new, {bad} without quarter scores); {len(res)} teams")
    return _halves


# ---------------------------------------------------------------------------
# Points per possession, from real box scores (regular season only)
# Possessions = FGA + 0.44*FTA - OREB + TOV, averaged over both teams in the game.
# Finished games are cached in data/possessions.json so each run only fetches new ones.
# ---------------------------------------------------------------------------
SEASON_STARTS = {2027: (2026, 10, 20)}
PPP_MIN_GAMES = 5
MAX_NEW_GAMES = 80
_ppp = None


def _stat(stats, names, part=None):
    for st in stats or []:
        n, lab = (st.get("name") or "").lower(), (st.get("label") or "").lower()
        if n in names or lab in names:
            v = str(st.get("displayValue") if st.get("displayValue") not in (None, "") else st.get("value", ""))
            if part is not None:
                if "-" not in v:
                    continue
                v = v.split("-")[part]
            try:
                return float(v)
            except ValueError:
                continue
    return None


def _secs(v):
    """'11:34' -> 694.0, '45.2' -> 45.2"""
    v = str(v or "").strip()
    if not v:
        return None
    try:
        if ":" in v:
            m, sec = v.split(":")
            return int(m) * 60 + float(sec)
        return float(v)
    except ValueError:
        return None


EARLY_SEC, LATE_SEC, OREB_RESET = 7, 18, 10   # early = first 7s used; late = more than 18s used (last ~6s)


def _clock_stats(plays, team_ids):
    """Estimate how many seconds of the 24-second clock each team uses before shooting.

    From play-by-play game-clock times: possession starts when a team gets the ball (defensive rebound,
    turnover/steal, opponent's made shot or last free throw); an offensive rebound counts as already
    10 seconds into a fresh clock (it resets to 14). Elapsed = start - shot time, kept only if 0..24.
    -> {team_id: [total_seconds, shots, early_shots, late_shots]} or None when plays are unusable."""
    if not plays or len(team_ids) != 2:
        return None
    a, b = team_ids
    other = {a: b, b: a}
    out = {a: [0.0, 0, 0, 0], b: [0.0, 0, 0, 0]}
    poss, start, offset, period = None, None, 0.0, None
    try:
        for p in plays:
            per = (p.get("period") or {}).get("number")
            clock = _secs((p.get("clock") or {}).get("displayValue"))
            if per is None or clock is None:
                continue
            if per != period:
                period, poss, start, offset = per, None, None, 0.0
            tid = str((p.get("team") or {}).get("id") or "")
            typ = ((p.get("type") or {}).get("text") or "").lower()
            if tid not in other:
                tid = ""
            shooting = bool(p.get("shootingPlay"))
            scoring = bool(p.get("scoringPlay"))
            text = (p.get("text") or "").lower()
            if "free throw" in typ or "free throw" in text:
                if tid and scoring and any(k in text for k in ("1 of 1", "2 of 2", "3 of 3")):
                    poss, start, offset = other[tid], clock, 0.0
                continue
            if shooting and tid:
                if poss == tid and start is not None:
                    used = start - clock + offset
                    if 0 <= used <= 24.5:
                        o = out[tid]
                        o[0] += used; o[1] += 1
                        o[2] += used <= EARLY_SEC
                        o[3] += used > LATE_SEC
                if scoring:
                    poss, start, offset = other[tid], clock, 0.0
                else:
                    poss = None
            elif "rebound" in typ and tid:
                if "offensive" in typ:
                    poss, start, offset = tid, clock, OREB_RESET
                else:
                    poss, start, offset = tid, clock, 0.0
            elif tid and ("turnover" in typ or "steal" in typ or "bad pass" in typ or "lost ball" in typ
                          or "traveling" in typ or "offensive foul" in typ):
                if "steal" in typ:
                    poss, start, offset = tid, clock, 0.0
                else:
                    poss, start, offset = other[tid], clock, 0.0
    except Exception as e:
        print(f"WARNING: clock parse: {e}")
        return None
    return out if (out[a][1] + out[b][1]) >= 20 else None


_ppp_debug = []
_ppp_notes = []


def _pnote(msg):
    print(msg)
    if len(_ppp_notes) < 6:
        _ppp_notes.append(msg)


def _either(stats, separate, combined, part):
    """ESPN sometimes lists made and attempted as separate stats and sometimes as one 'made-attempted' stat."""
    v = _stat(stats, separate)
    return v if v is not None else _stat(stats, combined, part)


def _parse_box(summary, scores, periods):
    """-> {team_id: {abbr, pts, fga, fta, oreb, tov, clk}} or None if the box score is missing or odd."""
    out = {}
    for t in (summary.get("boxscore") or {}).get("teams") or []:
        tid = str(t["team"]["id"])
        st = t.get("statistics") or []
        row = {"abbr": t["team"].get("abbreviation"), "pts": scores.get(tid),
               "fga": _either(st, {"fieldgoalsattempted", "fga"}, {"fieldgoalsmade-fieldgoalsattempted", "fg"}, 1),
               "fta": _either(st, {"freethrowsattempted", "fta"}, {"freethrowsmade-freethrowsattempted", "ft"}, 1),
               "oreb": _stat(st, {"offensiverebounds", "oreb"}),
               "tov": _stat(st, {"totalturnovers", "turnovers", "to"})}
        missing = [k for k, v in row.items() if v is None]
        if missing:
            if not _ppp_debug:
                _ppp_debug.append(f"missing {missing}; stat names seen: {[x.get('name') for x in st][:40]}")
            return None
        out[tid] = row
    if len(out) != 2:
        if not _ppp_debug:
            _ppp_debug.append(f"expected 2 teams in boxscore, found {len(out)}; summary keys: {list(summary)[:15]}")
        return None
    cs = _clock_stats(summary.get("plays"), list(out))
    for tid in out:
        out[tid]["clk"] = cs[tid] if cs else None
    return out


def load_ppp(today, season):
    """Update the cache with newly finished regular-season games and return per-team efficiency."""
    global _ppp
    if _ppp is not None:
        return _ppp
    _ppp = {}
    path = os.path.join(os.path.dirname(__file__) or ".", "data", "possessions.json")
    cache = {"games": {}, "dates_done": []}
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
    except Exception as e:
        _pnote(f"WARNING: could not read possessions cache: {e}")
    start = SEASON_STARTS.get(season)
    if not start:
        _pnote("PPP: no season start date on file for this season")
        _ppp = {"teams": {}, "n": 0, "games": 0, "bad": 0, "days": [], "notes": list(_ppp_notes)}
        return _ppp
    d, new, bad = date(*start), 0, 0
    days_seen = []
    try:
        while d <= today:
            ds = d.strftime("%Y%m%d")
            if ds in cache["dates_done"] and d < today - timedelta(days=1):
                d += timedelta(days=1)
                continue
            sb = get(f"{API}/scoreboard?dates={ds}")
            all_final = True
            seen_final = 0
            for ev in sb.get("events", []):
                comp = ev["competitions"][0]
                if not comp.get("status", {}).get("type", {}).get("completed"):
                    all_final = False
                    continue
                seen_final += 1
                stype = (ev.get("season") or {}).get("type") or (sb.get("season") or {}).get("type")
                if (stype is not None and stype != 2) or (cache["games"].get(ev["id"]) or {}).get("v") == 2:
                    continue
                if new >= MAX_NEW_GAMES:
                    all_final = False
                    continue
                scores = {str(c["team"]["id"]): float((c.get("score") or {}).get("value", 0) if isinstance(c.get("score"), dict) else c.get("score") or 0)
                          for c in comp["competitors"]}
                try:
                    box = _parse_box(get(f"{API}/summary?event={ev['id']}"), scores, comp["status"].get("period", 4))
                except Exception as e:
                    box = None
                    _pnote(f"WARNING: box score {ev['id']}: {e}")
                new += 1
                if not box:
                    bad += 1
                    all_final = False
                    continue
                cache["games"][ev["id"]] = {"v": 2, "date": ds, "periods": comp["status"].get("period", 4), "teams": box}
            print(f"PPP: {ds}: {len(sb.get('events', []))} games on the scoreboard, {seen_final} finished")
            days_seen.append(f"{ds[4:6]}/{ds[6:]}: {len(sb.get('events', []))} games, {seen_final} finished")
            if all_final and ds not in cache["dates_done"]:
                cache["dates_done"].append(ds)
            d += timedelta(days=1)
    except Exception as e:
        _pnote(f"WARNING: PPP update stopped early: {e}")
    if new:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
        except Exception as e:
            print(f"WARNING: could not save possessions cache: {e}")
    if _ppp_debug:
        _pnote("PPP debug: " + _ppp_debug[0])
    tot = {}
    for g in cache["games"].values():
        (ida, a), (idb, b) = list(g["teams"].items())
        mins = 48 + 5 * max(0, g.get("periods", 4) - 4)
        poss = lambda x: x["fga"] + 0.44 * x["fta"] - x["oreb"] + x["tov"]
        pg = (poss(a) + poss(b)) / 2
        if pg <= 0:
            continue
        for tid, mine, theirs in ((ida, a, b), (idb, b, a)):
            t = tot.setdefault(tid, {"abbr": mine["abbr"], "g": 0, "pts": 0.0, "opp": 0.0, "poss": 0.0, "pace": 0.0, "recent": [], "clk": [0.0, 0, 0, 0], "clk_opp": [0.0, 0, 0, 0]})
            t["g"] += 1; t["pts"] += mine["pts"]; t["opp"] += theirs["pts"]; t["poss"] += pg; t["pace"] += pg * 48 / mins
            t["recent"].append((g["date"], mine["pts"], theirs["pts"], pg, pg * 48 / mins))
            if mine.get("clk") and theirs.get("clk"):
                for i_ in range(4):
                    t["clk"][i_] += mine["clk"][i_]
                    t["clk_opp"][i_] += theirs["clk"][i_]
    res = {}
    for tid, t in tot.items():
        rec = sorted(t["recent"])[-5:]
        rp = sum(x[3] for x in rec)
        res[tid] = {"abbr": t["abbr"], "g": t["g"], "off": t["pts"] / t["poss"], "def": t["opp"] / t["poss"],
                    "pace": t["pace"] / t["g"], "off5": sum(x[1] for x in rec) / rp, "def5": sum(x[2] for x in rec) / rp,
                    "pace5": sum(x[4] for x in rec) / len(rec), "p5": [x[4] for x in rec]}
        res[tid]["net"] = res[tid]["off"] - res[tid]["def"]
        c, co = t["clk"], t["clk_opp"]
        if c[1] >= 150 and co[1] >= 150 and 8 <= c[0] / c[1] <= 20 and 8 <= co[0] / co[1] <= 20:
            res[tid].update({"sc": c[0] / c[1], "sc_opp": co[0] / co[1], "sc_n": c[1],
                             "early": 100 * c[2] / c[1], "late": 100 * c[3] / c[1]})
    ranked = [k for k, v in res.items() if v["g"] >= 1]
    sc_teams = [k for k in ranked if "sc" in res[k]]
    for i, k in enumerate(sorted(sc_teams, key=lambda k: res[k]["sc"]), 1):
        res[k]["sc_rank"] = i
    if sc_teams:
        print(f"Shot clock estimate: {len(sc_teams)} teams measured, league average "
              f"{sum(res[k]['sc'] for k in sc_teams) / len(sc_teams):.1f} s used before shooting")
    else:
        print("Shot clock estimate: not enough usable play-by-play yet (needs 150+ shots per team, 8-20 s average)")
    for key, rev, name in (("off", True, "off_rank"), ("def", False, "def_rank"), ("net", True, "net_rank"), ("pace", True, "pace_rank")):
        order = sorted(ranked, key=lambda k: res[k][key], reverse=rev)
        for i, k in enumerate(order, 1):
            res[k][name] = i
    lg = {}
    pool = [k for k in ranked if res[k]["g"] >= PPP_MIN_GAMES] or ranked
    for key in ("off", "def", "net", "pace", "sc"):
        vals = [res[k][key] for k in pool if key in res[k]]
        if len(vals) >= 2:
            lg[key] = (min(vals), max(vals), sum(vals) / len(vals))
    _ppp = {"teams": res, "n": len(ranked), "games": len(cache["games"]), "bad": bad, "days": days_seen[-3:], "notes": list(_ppp_notes), "lg": lg}
    print(f"PPP: {len(cache['games'])} games cached ({new} new, {bad} unreadable); {len(ranked)} teams")
    return _ppp


GLOW = "#ffb86b"   # one glow color for every matchup page


def rest_text(info):
    if info is None:
        return "No data"
    bits = []
    if info["b2b"]:
        bits.append("Back-to-back")
    if info["third_in_four"]:
        bits.append("3rd game in 4 nights")
    if info.get("recent_road_in_6", 0) >= 3:
        bits.append(f"{info['recent_road_in_6']} road games in last 5 nights")
    d = info["days_off"]
    if not bits:
        bits.append("Season opener" if d is None else f"{d} day{'s' if d != 1 else ''} rest")
    return "<br>".join(bits)


def rank_chip(rank, n):
    cls = "elite" if rank <= GOOD else "weak" if rank > n - BAD else ""
    note = "elite" if cls == "elite" else "bottom 10" if cls == "weak" else ""
    return f'<span class="chip {cls}">#{rank}</span>' + (f"<small>{note}</small>" if note else "")


def row(label, a, h, k, hi=""):
    return (f'<div class="row" style="--k:var(--c-{k})"><div class="v a{" hi" if hi == "a" else ""}">{a}</div>'
            f'<div class="lab">{escape(label)}</div><div class="v h{" hi" if hi == "h" else ""}">{h}</div></div>')


SHOW_LOGOS = True   # set to False to go back to the plain color badges


def badge(team):
    """Original monogram badge in the team's color (not the team's logo)."""
    col = (team.get("color") or "666666").lstrip("#")
    if len(col) != 6:
        col = "666666"
    r, g, b = (int(col[i:i + 2], 16) for i in (0, 2, 4))
    fg = "#111" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#fff"
    logo = (team.get("logo") or "").strip()
    img = ""
    if SHOW_LOGOS and logo.startswith("https://"):
        img = (f'<img src="{escape(logo, quote=True)}" alt="" loading="lazy" referrerpolicy="no-referrer" '
               f'onload="this.parentNode.classList.add(\'ok\')" onerror="this.remove()">')
    return (f'<span class="badge" style="--bc:#{col};--bf:{fg}" aria-hidden="true">{img}'
            f'<span class="ab">{escape(team["abbreviation"])}</span></span>')


def record_text(comp_team, info):
    """Overall record from ESPN's scoreboard, else counted from finished games."""
    recs = comp_team.get("records") or []
    tot = next((r for r in recs if (r.get("type") == "total" or (r.get("name") or "").lower() == "overall") and r.get("summary")), None)
    if tot:
        return tot["summary"]
    hist = (info or {}).get("hist") or []
    w = sum(1 for g in hist if g["win"])
    return f"{w}-{len(hist) - w}"


def name_block(comp_team, info):
    t = comp_team["team"]
    rec = escape(record_text(comp_team, info))
    ab = escape(t["abbreviation"], quote=True)
    return (f'<span class="tn">{escape(t["displayName"])}<small class="rec">{rec}</small>'
            f'<button class="fol" type="button" data-t="{ab}" aria-pressed="false">&#9734; Interested</button></span>')


def section(title, k, rows):
    rows = "".join(r for r in rows if r)
    if not rows:
        return ""
    return f'<section class="grp" style="--k:var(--c-{k})"><h3>{escape(title)}</h3>{rows}</section>'


# ---------------------------------------------------------------------------
# Small charts (plain HTML/SVG, no libraries). Each one is wrapped in _safe so a
# bad number can never take down a game page.
# ---------------------------------------------------------------------------
def _safe(fn, *args):
    try:
        return fn(*args) or ""
    except Exception as e:
        print(f"WARNING: chart {getattr(fn, '__name__', '?')} skipped: {e}")
        return ""


def _frac(v, rng, flip=False):
    """Position of v inside the league (low, high, avg) range, 0..1. flip=True when a lower value is better."""
    lo, hi = rng[0], rng[1]
    x = (v - lo) / (hi - lo) if hi > lo else 0.5
    x = min(1.0, max(0.0, x))
    return 1 - x if flip else x


def mbar(frac, strong):
    return f'<div class="mbar{"" if strong else " lo"}"><i style="width:{max(6.0, frac * 100):.0f}%"></i></div>'


def mirror_row(label, a_txt, h_txt, fa, fh, k):
    """A normal row plus a bar under each number; the longer bar is the better (or faster) team."""
    if fa is None or fh is None:
        return row(label, a_txt, h_txt, k)
    return row(label, a_txt + mbar(fa, fa >= fh), h_txt + mbar(fh, fh >= fa), k)


def form_strip(info):
    gs = ((info or {}).get("hist") or [])[-10:]
    if not gs:
        return ""
    cells = "".join(
        f'<i class="{"w" if g["win"] else "l"}" title="{escape(("W" if g["win"] else "L") + f" {g['our_score']:.0f}-{g['opp_score']:.0f} " + ("vs " if g["home"] else "at ") + g["opp_abbr"] + f", {g['date']:%b %-d}")}"></i>'
        for g in gs)
    return f'<div class="strip">{cells}</div>'


def rest_strip(info, today):
    played = {g["date"]: g for g in ((info or {}).get("hist") or [])}
    cells = []
    for k in range(6, -1, -1):
        d = today - timedelta(days=k)
        if k == 0:
            cells.append('<i class="t" title="Tonight"></i>')
            continue
        g = played.get(d)
        if g:
            t = f"{d:%a %b %-d}: {'W' if g['win'] else 'L'} {g['our_score']:.0f}-{g['opp_score']:.0f} " + ("vs " if g["home"] else "at ") + g["opp_abbr"]
            cells.append(f'<i class="p" title="{escape(t)}"></i>')
        else:
            cells.append(f'<i class="o" title="{d:%a %b %-d}: no game"></i>')
    return f'<div class="days">{"".join(cells)}</div>'


def stack(early, late):
    mid = max(0.0, 100 - early - late)
    seg = lambda w, c: f'<i class="{c}" style="width:{w:.1f}%">{f"{w:.0f}%" if w >= 11 else ""}</i>'
    return f'<div class="stack">{seg(early, "s1")}{seg(mid, "s2")}{seg(late, "s3")}</div>'


def spark(vals, avg, w=116, h=34):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals + [avg]) - 1, max(vals + [avg]) + 1
    X = lambda i: 4 + i * (w - 8) / (len(vals) - 1)
    Y = lambda v: h - 4 - (v - lo) / (hi - lo) * (h - 8)
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="pace over the last {len(vals)} games">'
            f'<line x1="4" x2="{w - 4}" y1="{Y(avg):.1f}" y2="{Y(avg):.1f}" stroke-dasharray="3 3" style="stroke:var(--mute)" stroke-width="1"/>'
            f'<polyline points="{pts}" fill="none" style="stroke:var(--k)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{X(len(vals) - 1):.1f}" cy="{Y(vals[-1]):.1f}" r="3.2" style="fill:var(--k)"/></svg>')


def league_scale(pa, ph, a_abbr, h_abbr, lg):
    tracks = [("Offense, pts per possession", "off", "worse", "better", False),
              ("Defense, pts allowed per possession", "def", "worse", "better", True),
              ("Pace, possessions per 48", "pace", "slower", "faster", False)]
    if "sc" in pa and "sc" in ph:
        tracks.append(("Seconds used before shooting", "sc", "slower", "faster", True))
    out = []
    for title, key, left, right, flip in tracks:
        if key not in lg:
            continue
        xa, xh, xv = (_frac(v, lg[key], flip) * 100 for v in (pa[key], ph[key], lg[key][2]))
        out.append(f'<div class="track"><div class="tl"><span>{left}</span><b>{escape(title)}</b><span>{right}</span></div>'
                   f'<div class="rail"><u style="left:{xv:.1f}%" title="league average"></u>'
                   f'<s class="d a" style="left:{xa:.1f}%"><em>{escape(a_abbr)}</em></s>'
                   f'<s class="d h" style="left:{xh:.1f}%"><em>{escape(h_abbr)}</em></s></div></div>')
    if not out:
        return ""
    return (f'<div class="chart"><div class="ch">Where each team sits in the league '
            f'<span class="ka">&#9679; {escape(a_abbr)}</span> <span class="kh">&#9679; {escape(h_abbr)}</span> &middot; tick = league average</div>'
            + "".join(out) + '</div>')


def h2h_chart(games, a_abbr, h_abbr):
    gs = list(reversed(games))
    n = len(gs)
    if n < 2:
        return ""
    W, mid = 300, 44
    bw = W / n
    mx = max(10.0, max(abs(g["our"] - g["their"]) for g in gs))
    parts = [f'<line x1="0" x2="{W}" y1="{mid}" y2="{mid}" style="stroke:var(--line)" stroke-width="1"/>']
    for i, g in enumerate(gs):
        m = g["our"] - g["their"]
        hgt = max(2.0, abs(m) / mx * 30)
        x, w = i * bw + bw * 0.18, bw * 0.64
        if m > 0:
            y, fill, ty = mid - hgt, "var(--k)", mid - hgt - 3
        else:
            y, fill, ty = mid, "var(--ink)", mid + hgt + 9
        tip = f"{g['date']:%b %-d, %Y}: {a_abbr if m > 0 else h_abbr} won by {abs(m):.0f}" + (" (playoffs)" if g["playoff"] else "")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{hgt:.1f}" rx="2" style="fill:{fill};opacity:{1 if m > 0 else .8}"><title>{escape(tip)}</title></rect>')
        parts.append(f'<text x="{x + w / 2:.1f}" y="{ty:.1f}" text-anchor="middle" font-size="8" style="fill:var(--mute)">{abs(m):.0f}</text>')
    return (f'<div class="chart"><div class="ch">Margin of each meeting</div>'
            f'<svg viewBox="0 0 {W} 88" width="100%" role="img" aria-label="margin of victory in recent meetings">{"".join(parts)}</svg>'
            f'<div class="cap2"><span style="color:var(--k)">&#9650; {escape(a_abbr)} won</span><span>oldest &rarr; newest</span><span>&#9660; {escape(h_abbr)} won</span></div></div>')


def half_chart(games, mx):
    """Per game: 1st-half margin (light bar) and 2nd-half margin (solid bar); up = outscored the opponent."""
    n = len(games)
    if n < 1:
        return ""
    W, H, mid = 130, 52, 26
    slot = W / 10
    parts = [f'<line x1="0" x2="{W}" y1="{mid}" y2="{mid}" style="stroke:var(--line)" stroke-width="1"/>']
    for i, g in enumerate(games):
        x0 = i * slot
        for j, (m, solid) in enumerate((((g["f1"] - g["a1"]), False), ((g["f2"] - g["a2"]), True))):
            h = max(1.5, abs(m) / mx * (mid - 3))
            y = mid - h if m >= 0 else mid
            label = f"{g['d'][4:6]}/{g['d'][6:]} {'2nd' if solid else '1st'} half: {m:+.0f}"
            parts.append(f'<rect x="{x0 + 1 + j * 5.6:.1f}" y="{y:.1f}" width="5.2" height="{h:.1f}" rx="1.2" '
                         f'style="fill:var(--k);opacity:{1 if solid else .42}"><title>{escape(label)}</title></rect>')
    return f'<svg class="hchart" viewBox="0 0 {W} {H}" role="img" aria-label="first and second half margin for each recent game">{"".join(parts)}</svg>'


def half_summary(ha, hh, a_abbr, h_abbr):
    """Season average point differential in each half, one pair of bars per team."""
    vals = [ha["m1"], ha["m2"], hh["m1"], hh["m2"]]
    mx = max(3.0, max(abs(v) for v in vals))
    W, H, mid, room = 300, 142, 70, 36
    bw, gap = 46, 18
    xs = [38, 38 + bw + 6, 38 + 2 * bw + 6 + 60, 38 + 3 * bw + 12 + 60]
    parts = [f'<line x1="14" x2="{W - 14}" y1="{mid}" y2="{mid}" style="stroke:var(--mute)" stroke-width="1"/>']
    meta = [(xs[0], vals[0], "1st", False, "var(--k)"), (xs[1], vals[1], "2nd", True, "var(--k)"),
            (xs[2], vals[2], "1st", False, "var(--ink)"), (xs[3], vals[3], "2nd", True, "var(--ink)")]
    for x, v, lab, solid, col in meta:
        h = max(1.5, abs(v) / mx * room)
        y = mid - h if v >= 0 else mid
        ty = y - 4 if v >= 0 else y + h + 11
        txt = f"{v:+.1f}".replace("-", "−")
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bw}" height="{h:.1f}" rx="3" style="fill:{col};opacity:{1 if solid else .45}"/>')
        parts.append(f'<text x="{x + bw / 2}" y="{ty:.1f}" text-anchor="middle" font-size="12" font-weight="700" style="fill:{col}">{txt}</text>')
        parts.append(f'<text x="{x + bw / 2}" y="{H - 4}" text-anchor="middle" font-size="10" style="fill:var(--mute)">{lab} half</text>')
    parts.append(f'<text x="{(xs[0] + xs[1] + bw) / 2}" y="12" text-anchor="middle" font-size="12" font-weight="700" style="fill:var(--k)">{escape(a_abbr)}</text>')
    parts.append(f'<text x="{(xs[2] + xs[3] + bw) / 2}" y="12" text-anchor="middle" font-size="12" font-weight="700" style="fill:var(--ink)">{escape(h_abbr)}</text>')
    return (f'<div class="chart"><div class="ch">Average point differential in each half (+ outscores opponents, − gets outscored)</div>'
            f'<svg class="hsum" viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="average point differential in the first and second half">{"".join(parts)}</svg></div>')


def half_rows(ha, hh, a_abbr="", h_abbr=""):
    def pm(x):
        return f"{x:+.1f}".replace("-", "&minus;")
    def tilt(r):
        if r["g"] < HALF_TAG_GAMES:
            return ""
        if r["shift"] >= HALF_TAG_SHIFT:
            return "strong finisher"
        if r["shift"] <= -HALF_TAG_SHIFT:
            return "fast starter"
        return "even"
    h1 = lambda r: f"{r['f1']:.1f} / {r['a1']:.1f}<small>{pm(r['m1'])} net &middot; #{r['r1']}</small>"
    h2 = lambda r: f"{r['f2']:.1f} / {r['a2']:.1f}<small>{pm(r['m2'])} net &middot; #{r['r2']}</small>"
    sh = lambda r, k: pm(r[k])
    shn = lambda r: f"{pm(r['shift'])}" + (f"<small>{tilt(r)} &middot; #{r['rs']}</small>" if tilt(r) else "")
    dom = ""
    if abs(ha["shift"] - hh["shift"]) >= 1.0:
        dom = "a" if ha["shift"] > hh["shift"] else "h"
    mx = max([10.0] + [abs(g[k1] - g[k2]) for r in (ha, hh) for g in r["games"] for k1, k2 in (("f1", "a1"), ("f2", "a2"))])
    rows = [
        _safe(half_summary, ha, hh, a_abbr, h_abbr),
        row("1st half: scored / allowed", h1(ha), h1(hh), "half"),
        row("2nd half: scored / allowed", h2(ha), h2(hh), "half"),
        row("Scoring, 2nd half minus 1st", sh(ha, "shift_f"), sh(hh, "shift_f"), "half"),
        row("Allowed, 2nd half minus 1st", sh(ha, "shift_a"), sh(hh, "shift_a"), "half"),
        row("Margin, 2nd half minus 1st", shn(ha), shn(hh), "half", dom),
        row("Each of the last 10 games", _safe(half_chart, ha["games"], mx), _safe(half_chart, hh["games"], mx), "half"),
        '<p class="cap">Per game averages, regular season, overtime left out. Chart: light bar = 1st half, solid bar = 2nd half, up = outscored the opponent.</p>',
    ]
    return rows


def winprob_section(a_abbr, h_abbr, crowd, model_pw):
    def bar(label, pa, k, thin):
        pa = pa * 100
        ph = 100 - pa
        return (f'<div class="wp" style="--k:{k}"><div class="wph"><span>{escape(a_abbr)} {pa:.0f}%</span><b>{escape(label)}</b><span>{ph:.0f}% {escape(h_abbr)}</span></div>'
                f'<div class="split{" thin" if thin else ""}"><i class="a" style="width:{pa:.1f}%"></i><i class="h" style="width:{ph:.1f}%"></i></div></div>')
    rows = []
    if crowd:
        rows.append(bar("Crowd (Polymarket)", crowd["pa"], "var(--c-line)", False))
    if model_pw is not None:
        rows.append(bar("Model (my estimate)", 1 - model_pw, "var(--c-outlook)", bool(crowd)))
    return section("Win probability", "line", rows)



def card(ev, today, season, revenge, rivalries, ratings, idx=0, total=1):
    comp = ev["competitions"][0]
    side = {c["homeAway"]: c for c in comp["competitors"]}
    away, home = side["away"], side["home"]
    a_abbr, h_abbr = away["team"]["abbreviation"], home["team"]["abbreviation"]
    a_id, h_id = away["team"]["id"], home["team"]["id"]

    ai = rest_and_travel(a_id, a_abbr, today, season)
    hi = rest_and_travel(h_id, h_abbr, today, season)
    af, hf = form_summary(ai), form_summary(hi)

    # ---- flags (same rules as before) ----
    tags = []
    if (ai["b2b"] and ai["third_in_four"] and hi["days_off"] is not None and hi["days_off"] >= 1):
        tags.append(("fatigue", "Road back-to-back and 3rd game in 4 nights vs. a rested opponent"))
    elif ai["b2b"] and (hi["days_off"] or 0) >= 2:
        tags.append(("fatigue", f"{a_abbr} on a back-to-back vs. {h_abbr} with {hi['days_off']} days rest"))
    road_in_window = ai.get("recent_road_in_6", 0) + 1
    if road_in_window >= 4:
        tags.append(("fatigue", f"{a_abbr}: 4th road game in 6 nights"))
    if h_abbr in ("DEN", "UTA") and a_abbr not in ("DEN", "UTA"):
        tags.append(("altitude", f"Altitude: {a_abbr} visits {h_abbr}"))
    tip_dt = parse_dt(ev["date"]).astimezone(PT)
    if a_abbr in WEST_COAST and h_abbr not in WEST_COAST and tip_dt.hour >= 16:
        tags.append(("travel", f"West-to-east: {a_abbr} plays a night game in the East"))
    for r in revenge:
        if {r["winner"], r["loser"]} == {a_abbr, h_abbr}:
            tags.append(("revenge", f"Playoff revenge: {r['winner']} beat {r['loser']} {r['result']} ({r['round']})"))
    for abbr, form in ((a_abbr, af), (h_abbr, hf)):
        if form and form["n_games"] >= 5:
            st = form["streak"]
            if (st.startswith("L") and int(st[1:]) >= 3) or (st.startswith("W") and int(st[1:]) >= 5):
                tags.append(("form", f"{abbr} on a {st} streak"))
    for info, abbr in ((ai, a_abbr), (hi, h_abbr)):
        prev = info.get("prev")
        if prev and prev["win"] and prev["margin"] >= 15 and info.get("days_off") == 1:
            tags.append(("schedule", f"Possible letdown: {abbr} is coming off a +{int(prev['margin'])} win"))
    rtags, _ = mismatch_tags(away, home, ratings)
    pp = load_ppp(today, season)
    pa_, ph_ = pp["teams"].get(str(a_id)), pp["teams"].get(str(h_id))
    use_ppp = bool(pa_ and ph_ and pa_["g"] >= PPP_MIN_GAMES and ph_["g"] >= PPP_MIN_GAMES)
    if use_ppp:
        n_t = pp["n"]
        rtags = []
        for (x, xa), (y, ya) in (((pa_, a_abbr), (ph_, h_abbr)), ((ph_, h_abbr), (pa_, a_abbr))):
            if x["off_rank"] <= GOOD and y["def_rank"] > n_t - BAD:
                rtags.append(("mismatch", f"{xa} elite offense (#{x['off_rank']}, {x['off']:.2f} pts/poss) vs. {ya} bottom-{BAD} defense (#{y['def_rank']}, {y['def']:.2f} allowed)"))
            if x["def_rank"] <= GOOD and y["off_rank"] > n_t - BAD:
                rtags.append(("lockdown", f"{xa} elite defense (#{x['def_rank']}, {x['def']:.2f} allowed) vs. {ya} bottom-{BAD} offense (#{y['off_rank']}, {y['off']:.2f} pts/poss)"))
    if pa_ and ph_ and pa_["g"] >= PPP_MIN_GAMES and ph_["g"] >= PPP_MIN_GAMES:
        n_t = pp["n"]
        cut = max(1, round(n_t * 8 / 30))
        ra, rh = pa_["pace_rank"], ph_["pace_rank"]
        exp_poss = (pa_["pace"] + ph_["pace"]) / 2
        if ra <= cut and rh <= cut:
            rtags.append(("outlook", f"Track meet: {a_abbr} (#{ra}) and {h_abbr} (#{rh}) are both top-{cut} in pace, about {exp_poss:.0f} possessions expected"))
        elif ra > n_t - cut and rh > n_t - cut:
            rtags.append(("outlook", f"Slow grind: {a_abbr} (#{ra}) and {h_abbr} (#{rh}) are both bottom-{cut} in pace, about {exp_poss:.0f} possessions expected"))
        elif (ra <= cut and rh > n_t - cut) or (rh <= cut and ra > n_t - cut):
            fast, slow = (a_abbr, h_abbr) if ra < rh else (h_abbr, a_abbr)
            rtags.append(("outlook", f"Pace clash: {fast} wants to run, {slow} wants to slow it down"))
    tags += rtags
    hv = load_halves(today, season)
    hv_a, hv_h = hv["teams"].get(str(a_id)), hv["teams"].get(str(h_id))
    use_halves = bool(hv_a and hv_h and hv_a["g"] >= HALF_MIN_GAMES and hv_h["g"] >= HALF_MIN_GAMES)
    if use_halves:
        for r_, ab in ((hv_a, a_abbr), (hv_h, h_abbr)):
            if r_["g"] >= HALF_TAG_GAMES and abs(r_["shift"]) >= HALF_TAG_SHIFT:
                if r_["shift"] > 0:
                    tags.append(("half", f"{ab} is a strong finisher: {r_['m1']:+.1f} in first halves, {r_['m2']:+.1f} in second halves"))
                else:
                    tags.append(("half", f"{ab} is a fast starter: {r_['m1']:+.1f} in first halves, {r_['m2']:+.1f} in second halves"))
    fa, fh = fc_team(away["team"]), fc_team(home["team"])
    ag, hg = _ages.get(a_id), _ages.get(h_id)
    roster_a = sum(ag) / len(ag) if ag else None
    roster_h = sum(hg) / len(hg) if hg else None
    rot_a, rot_h = (fa or {}).get("rotation_age"), (fh or {}).get("rotation_age")
    if rot_a and rot_h:
        gap, basis = rot_a - rot_h, "rotation"
    elif roster_a and roster_h:
        gap, basis = roster_a - roster_h, "roster"
    else:
        gap, basis = 0, ""
    if abs(gap) >= AGE_GAP_TAG:
        older = a_abbr if gap > 0 else h_abbr
        tags.append(("age", f"Age gap: {older} {basis} is older by {abs(gap):.1f} yrs"))
    if fa and fh:
        for f_, ab in ((fa, a_abbr), (fh, h_abbr)):
            if f_.get("returning_pct") is not None and f_["returning_pct"] < 50:
                tags.append(("age", f"Low continuity: {ab} returns only {f_['returning_pct']:.0f}% of last season's minutes"))
            if f_.get("coach"):
                tags.append(("age", f"New head coach: {ab} under {f_['coach']}" + (" (first NBA head job)" if f_.get("coach_first_time") else "")))
        pw, edge = model_win_prob(fa, fh)
        if abs(edge) >= 8:
            tags.append(("outlook", f"Model sees a {abs(edge):.0f}-point gap: {h_abbr if edge > 0 else a_abbr} is much stronger"))
        for (x, xa), (y, ya) in (((fa, a_abbr), (fh, h_abbr)), ((fh, h_abbr), (fa, a_abbr))):
            if x.get("rebounding") and y.get("rebounding") and x["rebounding"] <= 8 and y["rebounding"] >= 23:
                tags.append(("mismatch", f"Glass mismatch: {xa} rebounding #{x['rebounding']} vs. {ya} #{y['rebounding']}"))
            if x.get("spacing") and y.get("rim") and x["spacing"] <= 8 and y["rim"] >= 23:
                tags.append(("mismatch", f"Spacing vs. rim: {xa} spacing #{x['spacing']} vs. {ya} rim protection #{y['rim']}"))
    h2h = head_to_head(a_id, h_id, season, today)
    h2h_rows = []
    if h2h:
        n, aw, hw = h2h["n"], h2h["a"], h2h["b"]
        lead = a_abbr if aw > hw else h_abbr if hw > aw else None
        top = max(aw, hw) / n
        dom = ("a" if aw > hw else "h") if top >= 0.7 else ""
        if top >= H2H_DOMINANT and n >= 6:
            tags.append(("revenge", f"{lead} dominates this series: {max(aw, hw)}-{min(aw, hw)} in the last {n} meetings"))
        m = h2h["margin"]
        last = h2h["last"]
        winner = a_abbr if last["win"] else h_abbr
        sw = a_abbr if h2h["streak_a"] else h_abbr
        h2h_rows = [
            row("Record", f"{aw}-{hw}", f"{hw}-{aw}", "rivalry", dom),
            row("Avg margin", f"{m:+.1f}", f"{-m:+.1f}", "rivalry"),
            row("Streak", f"W{h2h['streak']}" if h2h["streak_a"] else "-", f"W{h2h['streak']}" if not h2h["streak_a"] else "-", "rivalry") if h2h["streak"] >= 2 else "",
            _safe(h2h_chart, h2h["games"], a_abbr, h_abbr),
        ]
        h2h_note = (f'<p class="note" style="--k:var(--c-rivalry)"><b>Last meeting</b> {last["date"]:%b %-d, %Y}: {escape(winner)} won '
                    f'{int(max(last["our"], last["their"]))}-{int(min(last["our"], last["their"]))}'
                    f'{" (playoffs)" if last["playoff"] else ""}</p>')
    else:
        h2h_note = ""
    inj_all = load_injuries()
    def availability(f_, abbr):
        """Match ESPN injuries to the team's projected rotation. -> dict or None"""
        if not f_:
            return None
        listed = {}
        for pl in inj_all.get(abbr) or []:
            st = pl["status"].lower()
            kind = "out" if st in ("out", "injured reserve", "out for season") else "dtd" if "day" in st else None
            if kind:
                listed[name_key(pl["name"])] = kind
        out_min = sum(x["minutes"] for x in f_["players"] if listed.get(x["key"]) == "out")
        dtd_min = sum(x["minutes"] for x in f_["players"] if listed.get(x["key"]) == "dtd")
        hits = []
        for rank, x in enumerate(f_["players"][:10], 1):
            if x["key"] in listed:
                hits.append((x, rank, listed[x["key"]]))
        return {"out_pct": 100 * out_min / f_["team_minutes"], "dtd_pct": 100 * dtd_min / f_["team_minutes"], "hits": hits}
    av_a, av_h = availability(fa, a_abbr), availability(fh, h_abbr)
    for av, ab in ((av_a, a_abbr), (av_h, h_abbr)):
        if not av:
            continue
        for x, rank, kind in av["hits"]:
            if kind == "out" and rank <= 3:
                tags.append(("injury", f"Key absence: {ab} is without {x['name']} (#{rank} in projected minutes)"))
        if av["out_pct"] >= 15:
            tags.append(("injury", f"{ab} is missing {av['out_pct']:.0f}% of its projected minutes"))
    for f_, ab in ((fa, a_abbr), (fh, h_abbr)):
        if f_ and f_.get("sd_rank") and f_["sd_rank"] <= 5:
            tags.append(("outlook", f"Wide range of outcomes: {ab}'s season forecast is among the 5 most uncertain"))
    if fa and fh and fa["players"] and fh["players"]:
        best = lambda f_: max(f_["players"], key=lambda x: x["impact"])
        ba, bh = best(fa), best(fh)
        if abs(ba["impact"] - bh["impact"]) >= 1.5:
            top_, ab_ = (ba, a_abbr) if ba["impact"] > bh["impact"] else (bh, h_abbr)
            tags.append(("age", f"Star power: {ab_} has the best player on the floor, {top_['name']}"))
    flags = "".join(f'<li class="{c}"><i></i>{escape(t)}</li>' for c, t in tags)
    flags_html = f'<ul class="flags">{flags}</ul>' if flags else '<p class="calm">No flags for this game.</p>'

    # ---- rows ----
    rest_rows = [row("Rest", rest_text(ai), rest_text(hi), "rest"),
                 row("Last 6 days", _safe(rest_strip, ai, today), _safe(rest_strip, hi, today), "rest"),
                 '<p class="cap">filled dot = played &middot; empty dot = no game &middot; ring = tonight</p>']
    def f(form, k, none="-"):
        return escape(str(form[k])) if form and form.get(k) else none
    def l10(form, info=None):
        if not form:
            return "-"
        n = min(form["n_games"], 10)
        return escape(form["last10"]) + (f"<small>{n} games played</small>" if n < 10 else "") + _safe(form_strip, info)
    form_rows = [
        row("Last 10 games", l10(af, ai), l10(hf, hi), "form"),
        row("Streak", f(af, "streak"), f(hf, "streak"), "form"),
        row("Home record", f(af, "home"), f(hf, "home"), "form"),
        row("Road record", f(af, "road"), f(hf, "road"), "form"),
        row("Close games", f(af, "close"), f(hf, "close"), "form") if (af and af.get("close")) or (hf and hf.get("close")) else "",
    ] if (af or hf) else []

    match_rows = []
    if ratings:
        n, ranks = ratings["n"], ratings["ranks"]
        ra, rh = ranks.get(norm(away["team"]["displayName"])), ranks.get(norm(home["team"]["displayName"]))
        if ra and rh:
            match_rows = [row("Offense", rank_chip(ra[0], n), rank_chip(rh[0], n), "match"),
                          row("Defense", rank_chip(ra[1], n), rank_chip(rh[1], n), "match")]

    ppp_note = ""
    if not (pa_ and ph_):
        why = "; ".join(pp.get("notes") or []) or "no finished games were read yet"
        ppp_note = (f'<p class="note" style="--k:var(--c-match)"><b>Points per possession and pace</b> '
                    f'not available yet. Games read: {pp.get("games", 0)} ({pp.get("bad", 0)} unreadable). '
                    f'Scoreboard: {escape(" | ".join(pp.get("days") or ["no dates checked"]))}. {escape(why)}</p>')
    if pa_ and ph_:
        n_t = pp["n"]
        lg = pp.get("lg") or {}
        cell = lambda v, rk, g: f"{v:.3f}" + (f"<small>#{rk} of {n_t} &middot; {g} games</small>")
        small = " (small sample)" if not use_ppp else ""
        fr = lambda key, a_v, h_v, flip=False: (_frac(a_v, lg[key], flip), _frac(h_v, lg[key], flip)) if key in lg else (None, None)
        fo, fo_h = fr("off", pa_["off"], ph_["off"])
        fd, fd_h = fr("def", pa_["def"], ph_["def"], True)
        fn, fn_h = fr("net", pa_["net"], ph_["net"])
        fp, fp_h = fr("pace", pa_["pace"], ph_["pace"])
        match_rows += [
            mirror_row("Offense, pts per possession" + small, cell(pa_["off"], pa_["off_rank"], pa_["g"]), cell(ph_["off"], ph_["off_rank"], ph_["g"]), fo, fo_h, "match"),
            mirror_row("Defense, pts allowed per possession", cell(pa_["def"], pa_["def_rank"], pa_["g"]), cell(ph_["def"], ph_["def_rank"], ph_["g"]), fd, fd_h, "match"),
            mirror_row("Net per possession", f"{pa_['net']:+.3f}<small>#{pa_['net_rank']}</small>", f"{ph_['net']:+.3f}<small>#{ph_['net_rank']}</small>", fn, fn_h, "match"),
            row("Last 5 games (off / def)", f"{pa_['off5']:.2f} / {pa_['def5']:.2f}", f"{ph_['off5']:.2f} / {ph_['def5']:.2f}", "match"),
            mirror_row("Pace, possessions per 48", f"{pa_['pace']:.1f}<small>#{pa_['pace_rank']} of {n_t}</small>", f"{ph_['pace']:.1f}<small>#{ph_['pace_rank']} of {n_t}</small>", fp, fp_h, "match"),
            *([row("Seconds used before shooting", f"{pa_['sc']:.1f} s<small>#{pa_['sc_rank']} fastest &middot; estimate</small>", f"{ph_['sc']:.1f} s<small>#{ph_['sc_rank']} fastest &middot; estimate</small>", "match"),
               row("Opponents' seconds used vs. them", f"{pa_['sc_opp']:.1f} s", f"{ph_['sc_opp']:.1f} s", "match"),
               row("Shot timing", stack(pa_["early"], pa_["late"]), stack(ph_["early"], ph_["late"]), "match"),
               '<p class="cap"><b class="k1">&#9632;</b> first 7 s &middot; <b class="k2">&#9632;</b> 7 to 18 s &middot; <b class="k3">&#9632;</b> after 18 s</p>']
              if "sc" in pa_ and "sc" in ph_ else []),
            row("Pace, last 5 games", _safe(spark, pa_.get("p5") or [], pa_["pace"]) + f"<small>{pa_['pace5']:.1f} &middot; {pa_['pace5'] - pa_['pace']:+.1f} vs. season</small>",
                _safe(spark, ph_.get("p5") or [], ph_["pace"]) + f"<small>{ph_['pace5']:.1f} &middot; {ph_['pace5'] - ph_['pace']:+.1f} vs. season</small>", "match"),
        ]
    if fa and fh:
        for lab, key in (("Spacing", "spacing"), ("Rebounding", "rebounding"), ("Rim protection", "rim")):
            if fa.get(key) and fh.get(key):
                match_rows.append(row(lab, rank_chip(fa[key], 30), rank_chip(fh[key], 30), "match"))
    if pa_ and ph_:
        match_rows.append(_safe(league_scale, pa_, ph_, a_abbr, h_abbr, pp.get("lg") or {}))

    half_sec = section("First half vs. second half", "half", half_rows(hv_a, hv_h, a_abbr, h_abbr)) if use_halves else ""
    age_rows = []
    if rot_a and rot_h:
        older_rot = "a" if rot_a - rot_h >= AGE_GAP_TAG else "h" if rot_h - rot_a >= AGE_GAP_TAG else ""
        age_rows.append(row("Rotation age", f"{rot_a:.1f}", f"{rot_h:.1f}", "age", older_rot))
    if roster_a and roster_h:
        age_rows.append(row("Roster average age", f"{roster_a:.1f}", f"{roster_h:.1f}", "age"))
    if fa and fh:
        coach = lambda f_: (escape(f_["coach"]) + "<small>new this season</small>") if f_.get("coach") else "Returning"
        age_rows.append(row("Head coach", coach(fa), coach(fh), "age") if (fa.get("coach") or fh.get("coach")) else "")
        if fa.get("returning_pct") is not None and fh.get("returning_pct") is not None:
            age_rows.append(row("Returning minutes", f"{fa['returning_pct']:.0f}%", f"{fh['returning_pct']:.0f}%", "age"))
        best3 = lambda f_: "<br>".join(
            f"{escape(x['name'])}<small>{escape(x['pos'] or '')} &middot; {x['mpg']:.0f} min &middot; impact {x['impact']:+.1f}</small>"
            for x in sorted(f_["players"], key=lambda x: -x["impact"])[:3])
        age_rows.append(row("Best players", best3(fa), best3(fh), "age"))
        rook = lambda f_: "<br>".join(
            f"{escape(x['name'])}<small>{('pick ' + str(int(x['pick'])) + ' &middot; ') if x['pick'] else ''}{x['mpg']:.0f} min</small>"
            for x in f_["players"] if x["rookie"] and x["mpg"] >= 12) or "None in rotation"
        age_rows.append(row("Rookies in rotation", rook(fa), rook(fh), "age"))

    inj = load_injuries()
    def inj_text(abbr):
        pl = inj.get(abbr) or []
        outs = [p["name"] for p in pl if p["status"].lower() in ("out", "injured reserve", "out for season")]
        dtd = [p["name"] for p in pl if "day" in p["status"].lower()]
        lines = [f"Out: {escape(x)}" for x in outs[:5]]
        if len(outs) > 5:
            lines.append(f"+{len(outs) - 5} more out")
        lines += [f"Day-to-day: {escape(x)}" for x in dtd[:4]]
        return "<br>".join(lines) or "None reported"
    inj_rows = [row("Report", inj_text(a_abbr), inj_text(h_abbr), "injury")]
    if av_a and av_h:
        def hit(av):
            lines = [f"{escape(x['name'])}<small>{'out' if kind == 'out' else 'day-to-day'} &middot; #{rank} in minutes &middot; {x['mpg']:.0f} min</small>"
                     for x, rank, kind in av["hits"]]
            return "<br>".join(lines) or "None of the top 10"
        inj_rows.append(row("Rotation players listed", hit(av_a), hit(av_h), "injury"))
        share = lambda av: f"{av['out_pct']:.0f}% out<small>{av['dtd_pct']:.0f}% day-to-day</small>"
        inj_rows.append(row("Projected minutes affected", share(av_a), share(av_h), "injury"))
    if fa and fh and (fa["watch"] or fh["watch"]):
        w = lambda f_: "<br>".join(f"{escape(x['name'])}<small>{escape((x['note'] or '')[:70])}</small>" for x in f_["watch"]) or "-"
        inj_rows.append(row(f"Preseason notes ({load_forecast()['as_of']})", w(fa), w(fh), "injury"))


    outlook_rows, out_note = [], ""
    model_pw = None
    if fa and fh:
        rec = lambda f_: f"{f_['wins']:.0f}-{f_['losses']:.0f}<small>{f_['win_low']}-{f_['win_high']} wins likely</small>"
        pct = lambda v: f"{v * 100:.0f}%" if v >= 0.01 else "&lt;1%"
        sched = lambda f_: f"#{f_['sched_rank']}" + ("<small>hardest</small>" if f_["sched_rank"] <= 5 else "<small>easiest</small>" if f_["sched_rank"] >= 26 else "")
        outlook_rows = [
            row("Projected record", rec(fa), rec(fh), "outlook"),
            row("League rank", f"#{fa['rank']}<small>{fa['conf']} #{fa['conf_rank']}</small>", f"#{fh['rank']}<small>{fh['conf']} #{fh['conf_rank']}</small>", "outlook"),
            row("Make playoffs", pct(fa["playoffs"]), pct(fh["playoffs"]), "outlook"),
            row("Schedule difficulty", sched(fa), sched(fh), "outlook"),
        ]
        spread = lambda f_: (f"&plusmn;{f_['sd']:.1f} wins" + ("<small>wide range</small>" if f_["sd_rank"] <= 5 else "<small>narrow range</small>" if f_["sd_rank"] >= 26 else "")) if f_.get("sd") else "-"
        outlook_rows.append(row("Outlook spread", spread(fa), spread(fh), "outlook"))
        pw, edge = model_win_prob(fa, fh)
        model_pw = pw
        fav, favp = (h_abbr, pw) if pw >= 0.5 else (a_abbr, 1 - pw)
        out_note = (f'<p class="note" style="--k:var(--c-outlook)"><b>Model view</b> {escape(fav)} wins {favp * 100:.0f}% of the time, '
                    f'by about {abs(edge):.1f} points. This is my estimate from the forecast file team strengths plus home court, '
                    f'not a number in the file.</p>')

    crowd = crowd_data(ev, away, home)
    market_rows, bar = [], ""
    if crowd:
        pa, ph = crowd["pa"] * 100, crowd["ph"] * 100
        market_rows.append(row("Crowd win %", f"{pa:.0f}%", f"{ph:.0f}%", "line"))
        bar = (f'<div class="bar" style="--k:var(--c-line)"><span style="width:{pa:.1f}%"></span></div>')
        if crowd["money"]:
            (da, na), (dh, nh) = crowd["money"]
            market_rows.append(row("Crowd money", f"${da:,.0f}<small>{na} trades</small>", f"${dh:,.0f}<small>{nh} trades</small>", "line"))
    market = section("Market", "line", market_rows)
    wp_html = _safe(winprob_section, a_abbr, h_abbr, crowd, model_pw)
    line = odds_line(comp)
    line_html = (f'<p class="note" style="--k:var(--c-line)"><b>Line</b> {escape(line)}</p>' if line else "")

    crew = fetch_officials(ev["id"])
    crew_html = f'<p class="note" style="--k:var(--c-ref)"><b>Officials</b> {escape(", ".join(crew))}</p>' if crew else ""
    notes = rivalry_notes(away["team"], home["team"], rivalries)
    src = ('<p class="src">Crowd data: Polymarket' + (" (thin market, treat as rough)" if crowd and crowd["thin"] else "") +
           f' &middot; updated {datetime.now(PT).strftime("%-I:%M %p PT")}</p>') if crowd else ""

    tip = tip_dt.strftime("%-I:%M %p PT")
    return f"""<article class="page" data-i="{idx}" data-set="t" data-label="{idx + 1} of {total}">
  <header class="match">
    <div class="teams"><h2 class="t a">{name_block(away, ai)}{badge(away["team"])}</h2><div class="mid"><span class="at">at</span><span class="tip">{tip}</span>{net_html(comp)}</div><h2 class="t h">{badge(home["team"])}{name_block(home, hi)}</h2></div>
  </header>
  <section class="grp flagsec" style="--k:var(--c-flags)"><h3>What stands out</h3>{flags_html}</section>
  {wp_html}
  {section("Rest and schedule", "rest", rest_rows)}
  {section("Form", "form", form_rows)}
  {section(f"Head to head, last {h2h['n']} meetings" if h2h else "Head to head", "rivalry", h2h_rows)}{h2h_note}
  {section(f"Season outlook (forecast as of {load_forecast()['as_of']})" if fa and fh else "Season outlook", "outlook", outlook_rows)}{out_note}
  {section("Matchup", "match", match_rows)}{ppp_note}
  {half_sec}
  {section("Roster", "age", age_rows)}
  {section("Injuries", "injury", inj_rows)}
  {market}
  {line_html}{crew_html}{src}{notes}
</article>"""


CSS = """
:root{color-scheme:dark;--bg:#262624;--card:#30302e;--ink:#f0eee6;--mute:#b0aea5;--line:#4a4945;--glow:#ffb86b;--c-flags:#f0eee6;
--c-rest:#ff9a62;--c-travel:#4dc9e6;--c-injury:#ff6b6b;--c-rivalry:#b79cff;--c-match:#4fd8b4;
--c-outlook:#6aa9ff;--c-form:#ffd24d;--c-age:#ff8fc0;--c-line:#7fd96b;--c-ref:#9db2c2;--c-half:#e879f9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Barlow",system-ui,sans-serif;
padding:0 16px 48px}
main{max-width:680px;margin:0 auto}
.bar-top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);
padding:max(12px,env(safe-area-inset-top)) 0 12px;margin-bottom:14px;border-bottom:1px solid var(--line)}
h1{font:700 1.7rem/1.1 "Barlow Condensed",sans-serif;margin:0}
.sub{color:var(--mute);margin:2px 0 10px;font-size:.95rem}
.pager{display:none;align-items:center;gap:10px}.js .pager{display:flex}
.pager button{font:600 1rem "Barlow",sans-serif;color:var(--ink);background:var(--card);border:1.5px solid var(--line);
border-radius:99px;padding:7px 18px;cursor:pointer;min-width:96px}
.pager button:hover:not(:disabled){border-color:var(--ink)}
.pager button:disabled{opacity:.35;cursor:default}
.pager button:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.count{flex:1;text-align:center;color:var(--mute);font-weight:600}
.page{background:var(--card);border:1px solid color-mix(in srgb,var(--glow) 70%,transparent);border-top:4px solid var(--glow);
border-radius:16px;padding:22px 20px 20px;
box-shadow:0 0 0 1px color-mix(in srgb,var(--glow) 20%,transparent),0 0 34px -6px color-mix(in srgb,var(--glow) 55%,transparent)}
.js .page{display:none}.js .page.on{display:block}
.page+.page{margin-top:22px}.js .page+.page{margin-top:0}
.tip{display:inline-block;background:var(--ink);color:var(--bg);font-weight:600;font-size:.85rem;padding:2px 11px;border-radius:99px}
.teams{display:grid;grid-template-columns:minmax(0,1fr) minmax(5.5rem,auto) minmax(0,1fr);gap:12px;align-items:center;margin:4px 0 4px}
.mid{display:flex;flex-direction:column;align-items:center;gap:6px}
.net{color:var(--mute);font:600 .78rem/1.2 "Barlow",sans-serif;text-align:center;letter-spacing:.02em}
.t{font:700 1.9rem/1.05 "Barlow Condensed",sans-serif;margin:0;display:flex;align-items:center;gap:10px}
.t.a{justify-content:flex-end;text-align:right}.t.h{justify-content:flex-start;text-align:left}
.tn{display:block;min-width:0}.rec{display:block;font:600 .95rem/1.2 "Barlow",system-ui,sans-serif;color:var(--mute);margin-top:3px}
.badge{position:relative;flex:none;width:3.1rem;height:3.1rem;border-radius:50%;overflow:hidden;background:var(--bc);color:var(--bf);display:grid;place-items:center;
font:700 .9rem/1 "Barlow Condensed",sans-serif;letter-spacing:.02em;box-shadow:0 0 0 2px color-mix(in srgb,var(--ink) 35%,transparent)}
.badge img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;padding:2px}
.badge.ok{background:transparent;box-shadow:none;overflow:visible}.badge.ok .ab{display:none}
.v.hi{font-weight:700;color:var(--k)}.v.hi::after{content:"";display:block;height:2px;border-radius:2px;background:var(--k);margin-top:2px;width:100%}
.at{color:var(--mute);font-size:.95rem;text-align:center;line-height:1}
.grp{margin-top:22px}
.grp h3{font:600 1.05rem "Barlow",sans-serif;color:var(--k);margin:0 0 6px;padding-left:10px;border-left:3px solid var(--k)}
.row{display:grid;grid-template-columns:1fr minmax(5.5rem,auto) 1fr;gap:12px;align-items:baseline;padding:7px 0;
border-bottom:1px solid color-mix(in srgb,var(--line) 60%,transparent)}
.row:last-child{border-bottom:0}
.lab{color:var(--k);font-size:.88rem;text-align:center;font-weight:600}
.v{font-size:1.02rem}.v.a{text-align:right}.v.h{text-align:left}
.v small{display:block;color:var(--mute);font-size:.8rem}
.chip{display:inline-block;padding:1px 10px;border-radius:99px;border:1.5px solid var(--line);color:var(--mute);font-size:.92rem}
.chip.elite{color:var(--c-match);background:color-mix(in srgb,var(--c-match) 16%,transparent);border-color:currentColor;font-weight:600}
.chip.weak{color:var(--c-match);border:1.5px dashed currentColor;font-weight:600;opacity:.8}
.flags{list-style:none;margin:0;padding:0}
.flags li{display:flex;gap:10px;align-items:baseline;padding:5px 0;font-weight:600}
.flags li i{flex:none;width:9px;height:9px;border-radius:50%;background:currentColor;transform:translateY(-1px)}
.fatigue,.schedule{color:var(--c-rest)}.altitude,.travel{color:var(--c-travel)}.injury{color:var(--c-injury)}
.outlook{color:var(--c-outlook)}.revenge{color:var(--c-rivalry)}.mismatch,.lockdown{color:var(--c-match)}.form{color:var(--c-form)}.age{color:var(--c-age)}
.half{color:var(--c-half)}
.calm{margin:0;color:var(--mute)}
.hsum{display:block;max-width:440px;margin:0 auto}
.hchart{display:block;width:100%;max-width:170px;height:auto}.v.a .hchart{margin-left:auto}
.bar{height:6px;border-radius:99px;background:color-mix(in srgb,var(--k) 22%,var(--bg));margin:4px 0 6px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--k);border-radius:99px 0 0 99px}
.note{margin:16px 0 0;padding-left:10px;border-left:3px solid var(--k)}.note b{color:var(--k);margin-right:8px}
.src{color:var(--mute);font-size:.82rem;margin:10px 0 0}
details{margin-top:18px;border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;font-weight:600;color:var(--c-rivalry)}
details ul{margin:8px 0 0;padding-left:18px}details li{margin:6px 0;color:var(--mute)}details li b{color:var(--ink)}
details a{color:var(--c-rivalry)}details .kind{font-weight:600;color:var(--ink)}
.mbar{height:7px;border-radius:99px;background:color-mix(in srgb,var(--k) 16%,var(--bg));margin-top:5px;overflow:hidden}
.mbar i{display:block;height:100%;background:var(--k);border-radius:99px}.mbar.lo i{opacity:.5}
.v.a .mbar i{margin-left:auto}
.strip,.days{display:flex;gap:3px;margin-top:6px}.v.a .strip,.v.a .days{justify-content:flex-end}
.strip i{flex:0 1 14px;min-width:6px;height:12px;border-radius:3px;border:2px solid var(--k)}.strip i.w{background:var(--k)}
.days{gap:5px;margin-top:0}.days i{flex:none;width:13px;height:13px;border-radius:50%;border:2px solid color-mix(in srgb,var(--k) 45%,transparent)}
.days i.p{background:var(--k);border-color:var(--k)}.days i.t{border-color:var(--ink);box-shadow:0 0 0 2px color-mix(in srgb,var(--ink) 25%,transparent)}
.cap{margin:2px 0 0;color:var(--mute);font-size:.8rem;text-align:center}.cap b{font-weight:400}
.k1{color:var(--c-match)}.k2{color:color-mix(in srgb,var(--c-match) 38%,var(--bg))}.k3{color:color-mix(in srgb,var(--c-match) 65%,#fff)}
.stack{display:flex;height:18px;border-radius:6px;overflow:hidden;margin-top:3px}
.stack i{display:block;font:600 .72rem/18px "Barlow",sans-serif;text-align:center;font-style:normal;overflow:hidden;white-space:nowrap}
.stack .s1{background:var(--k);color:#111}.stack .s2{background:color-mix(in srgb,var(--k) 38%,var(--bg));color:var(--ink)}.stack .s3{background:color-mix(in srgb,var(--k) 65%,#fff);color:#111}
.spark{display:block;max-width:100%;height:auto}.v.a .spark{margin-left:auto}
.chart{margin:12px 0 0;padding:10px 12px 8px;border:1px solid color-mix(in srgb,var(--k) 35%,var(--line));border-radius:12px;background:color-mix(in srgb,var(--k) 6%,transparent)}
.ch{color:var(--k);font-size:.85rem;font-weight:600;margin-bottom:6px}.ch .ka{color:var(--k)}.ch .kh{color:var(--ink);margin-left:6px}
.cap2{display:flex;justify-content:space-between;gap:8px;color:var(--mute);font-size:.78rem}
.track{margin:0 0 4px}.tl{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:.78rem;color:var(--mute)}.tl b{color:var(--ink);font-weight:600;font-size:.82rem;text-align:center}
.rail{position:relative;height:6px;margin:19px 14px 21px;border-radius:99px;background:linear-gradient(90deg,color-mix(in srgb,var(--k) 8%,var(--bg)),color-mix(in srgb,var(--k) 45%,var(--bg)))}
.rail u{position:absolute;top:-5px;width:2px;height:16px;background:var(--mute);transform:translateX(-50%);border-radius:2px;text-decoration:none}
.rail .d{position:absolute;top:50%;width:14px;height:14px;border-radius:50%;transform:translate(-50%,-50%);border:2px solid var(--bg);text-decoration:none}
.rail .d.a{background:var(--k)}.rail .d.h{background:var(--ink)}
.rail .d em{position:absolute;left:50%;transform:translateX(-50%);font:600 .72rem/1 "Barlow",sans-serif;font-style:normal;white-space:nowrap}
.rail .d.a em{bottom:15px;color:var(--k)}.rail .d.h em{top:15px;color:var(--ink)}
.wp{margin:8px 0 12px}.wph{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:baseline;font-weight:600}
.wph span:first-child{text-align:right}.wph b{color:var(--k);font-size:.85rem}
.split{display:flex;height:12px;border-radius:99px;overflow:hidden;margin-top:5px;gap:2px}.split.thin{height:7px}
.split i{display:block}.split .a{background:var(--k)}.split .h{background:color-mix(in srgb,var(--k) 35%,var(--bg))}
.fol{display:inline-block;margin-top:7px;font:600 .78rem/1 "Barlow",sans-serif;color:var(--mute);background:transparent;border:1.5px solid var(--line);border-radius:99px;padding:5px 11px;cursor:pointer;white-space:nowrap}
.fol:hover{border-color:var(--ink);color:var(--ink)}.fol.on{color:#111;background:var(--glow);border-color:var(--glow)}
.fol:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.minehead{font:700 1.9rem/1.05 "Barlow Condensed",sans-serif;margin:0;text-align:center}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}
.chip2{display:inline-flex;align-items:center;gap:6px;border:1.5px solid var(--glow);color:var(--glow);border-radius:99px;padding:3px 6px 3px 12px;font-weight:600}
.chip2 button{background:none;border:0;color:inherit;font-size:1.15rem;line-height:1;cursor:pointer;padding:0 5px}
.mg{margin-top:18px;padding:12px 14px 8px;border:1px solid color-mix(in srgb,var(--c-line) 40%,var(--line));border-radius:12px;background:color-mix(in srgb,var(--c-line) 5%,transparent)}
.mgh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px;color:var(--c-line)}.mgh b{font:700 1.3rem "Barlow Condensed",sans-serif;color:var(--ink)}
.mnum{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}.mnum small{display:block;color:var(--mute);font-size:.78rem}.mnum b{font-weight:600}
.sg{display:block;width:100%;max-width:540px;margin:8px auto 0;height:auto}
.sg .gl{stroke:var(--line);stroke-width:1}.sg .op{stroke:var(--mute);stroke-width:1.2;stroke-dasharray:4 3}.sg .ln{fill:none;stroke:var(--c-line);stroke-width:2.2;stroke-linejoin:round}
.sg .pt{fill:var(--c-line)}.sg text{font:600 10px "Barlow",sans-serif}.sg .ax{fill:var(--mute);font-weight:400}.sg .oplab{fill:var(--mute)}.sg .cur{fill:var(--c-line);font-size:11px}
.topline{display:flex;justify-content:space-between;align-items:center;gap:10px}
#menu{font:600 .85rem "Barlow",sans-serif;color:var(--mute);background:transparent;border:1.5px solid var(--line);border-radius:99px;padding:5px 12px;cursor:pointer;white-space:nowrap}
#menu:hover{color:var(--ink);border-color:var(--ink)}
html[data-mode="t"] .book[data-book="y"],html[data-mode="y"] .book[data-book="t"],html[data-mode="t"] .sub-y,html[data-mode="y"] .sub-t{display:none}
.scorerow{margin:2px 0 6px}.sc{font:800 2.6rem/1 "Oxanium","Barlow Condensed",sans-serif;color:var(--mute)}.sc.a{text-align:right}.sc.h{text-align:left}.sc.win{color:var(--ink)}
.dash{color:var(--mute);text-align:center}
.qt{width:100%;border-collapse:collapse;margin:4px 0 10px;font-size:.92rem}.qt th,.qt td{padding:4px 6px;text-align:center;color:var(--mute);font-weight:400}.qt th:first-child{text-align:left;color:var(--ink);font-weight:600}.qt tr+tr{border-top:1px solid var(--line)}
.hl{display:block;margin:8px 0;padding:11px 14px;border:1.5px solid var(--k);border-radius:12px;color:var(--ink);text-decoration:none;font-weight:600}
.hl:hover{background:color-mix(in srgb,var(--k) 14%,transparent)}.hl.sm{display:inline-block;margin:0;padding:6px 11px;font-size:.85rem;white-space:nowrap}
.yrow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}.yrow:first-of-type{border-top:0}
.ys b{font-weight:600;color:var(--mute)}.ys b.win{color:var(--ink)}.ys i{color:var(--mute);font-style:normal;font-size:.85rem}.ys small{display:block;color:var(--mute)}
.sub.ctr{text-align:center}
.why{list-style:none;margin:10px 0 4px;padding:0}.why li{display:flex;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.why li>b{flex:none;width:22px;height:22px;border-radius:50%;background:var(--c-line);color:#111;text-align:center;font-size:.8rem;line-height:22px}
.why small{display:block;color:var(--mute);margin-top:2px}
.sg .mk{fill:var(--c-line);stroke:var(--bg);stroke-width:2}.sg .mkn{fill:#111;font-size:10px;font-weight:700}
.more{border:1.5px dashed var(--line);border-radius:12px;padding:6px 14px 10px;margin-top:18px}.more h3{color:var(--mute)}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:26px;font-size:.85rem;color:var(--mute)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;background:var(--k)}
.empty,.foot{color:var(--mute)}.foot{font-size:.88rem;margin-top:14px}
#gate{display:none}
html.gated,html.gated body{background:#000;overflow:hidden;height:100%}
html.gated #app{display:none}
html.gated #gate{display:flex;position:fixed;inset:0;z-index:50;background:#000;flex-direction:column;align-items:center;
justify-content:center;gap:clamp(40px,8vh,72px);padding:24px;text-align:center}
.gate-title{margin:0;font:800 clamp(2.1rem,7.2vw,5.4rem)/1.04 "Oxanium","Orbitron",system-ui,sans-serif;letter-spacing:.07em;text-transform:uppercase;
background:linear-gradient(180deg,#fff 0%,#d3d7dc 46%,#6f757c 52%,#cfd3d8 100%);-webkit-background-clip:text;background-clip:text;color:transparent}
.gate-title span{display:block}
.gbtns{display:flex;flex-direction:column;align-items:center;gap:22px}
.gbtn{font:700 1.1rem/1 "Oxanium",system-ui,sans-serif;letter-spacing:.2em;text-indent:.2em;white-space:nowrap;text-transform:uppercase;color:#f4ff1a;background:#000;
border:2px solid #f4ff1a;border-radius:0;padding:18px 10px;cursor:pointer;width:min(86vw,360px);
text-shadow:0 0 10px rgba(244,255,26,.7);
box-shadow:0 0 10px rgba(244,255,26,.6),0 0 34px rgba(244,255,26,.28),inset 0 0 16px rgba(244,255,26,.16);
transition:background .18s,color .18s,text-shadow .18s}
.gbtn:hover{background:#f4ff1a;color:#000;text-shadow:none}
.gbtn:focus-visible{outline:2px solid #fff;outline-offset:5px}
@media (prefers-reduced-motion:no-preference){.gbtn{animation:breathe 3.4s ease-in-out infinite}.gate-title,.gbtn{animation-fill-mode:both}
@keyframes breathe{0%,100%{box-shadow:0 0 8px rgba(244,255,26,.5),0 0 26px rgba(244,255,26,.2),inset 0 0 12px rgba(244,255,26,.12)}
50%{box-shadow:0 0 14px rgba(244,255,26,.8),0 0 48px rgba(244,255,26,.38),inset 0 0 20px rgba(244,255,26,.22)}}}
@media (max-width:480px){.teams{grid-template-columns:minmax(0,1fr) 4.4rem minmax(0,1fr);gap:6px}.tip{font-size:.72rem;padding:2px 7px;white-space:nowrap}.net{font-size:.68rem}.t span{min-width:0}.t{font-size:1.18rem;gap:6px}.fol{font-size:.7rem;padding:4px 8px;margin-top:5px}.badge{font-size:.7rem}.row{gap:8px}.page{padding:18px 14px}.badge{width:2.4rem;height:2.4rem}.rec{font-size:.85rem}}
@media (prefers-reduced-motion:no-preference){.page.on{animation:flip .22s ease-out}}
@keyframes flip{from{opacity:0;transform:translateX(var(--dx,12px))}to{opacity:1;transform:none}}
"""

JS = """
(function(){
var root=document.documentElement,mode='t',i=0,pages=[];
root.classList.add('js');
var all=[].slice.call(document.querySelectorAll('.page')),
back=document.getElementById('back'),fwd=document.getElementById('fwd'),cnt=document.getElementById('count'),pager=document.querySelector('.pager');
function setOf(m){return all.filter(function(p){return p.getAttribute('data-set')===m})}
function show(n,dir){
  if(!pages.length){pager.style.display='none';cnt.textContent='';return}
  pager.style.display='';
  i=Math.max(0,Math.min(pages.length-1,n));
  all.forEach(function(p){p.classList.remove('on')});
  pages[i].classList.add('on');
  pages[i].style.setProperty('--dx',(dir<0?'-12px':'12px'));
  back.disabled=i===0;fwd.disabled=i===pages.length-1;
  cnt.textContent=pages[i].getAttribute('data-label')||((i+1)+' of '+pages.length);
  try{history.replaceState(null,'','#'+(mode==='t'?'g':'y')+(i+1))}catch(e){}
  window.scrollTo(0,0);
}
function openBook(m,n){mode=m;root.setAttribute('data-mode',m);pages=setOf(m);root.classList.remove('gated');show(n||0,1)}
function menu(){root.classList.add('gated');try{history.replaceState(null,'',location.pathname+location.search)}catch(e){}window.scrollTo(0,0)}
var hm=/^#([gy])(\\d+)$/.exec(location.hash);
if(hm)openBook(hm[1]==='g'?'t':'y',parseInt(hm[2],10)-1);else{root.setAttribute('data-mode','t');root.classList.add('gated')}
[].forEach.call(document.querySelectorAll('[data-go]'),function(b){b.onclick=function(){openBook(b.getAttribute('data-go'),0)}});
document.getElementById('menu').onclick=menu;
document.addEventListener('keydown',function(e){
  if(root.classList.contains('gated')){if(e.key==='Enter')openBook('t',0);return}
  if(e.key==='ArrowLeft')show(i-1,-1);else if(e.key==='ArrowRight')show(i+1,1)});
back.onclick=function(){show(i-1,-1)};fwd.onclick=function(){show(i+1,1)};
var x0=null;
document.addEventListener('touchstart',function(e){x0=e.touches[0].clientX},{passive:true});
document.addEventListener('touchend',function(e){
  if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;x0=null;
  if(!root.classList.contains('gated')&&Math.abs(dx)>70)show(i+(dx<0?1:-1),dx<0?1:-1)},{passive:true});
})();
"""


# ---------------------------------------------------------------------------
# Spreads: today's point spread for every game, with the history of how it moved.
# `python build.py --spreads` refreshes only this part (it is light enough to run every 10 minutes).
# History is kept in data/spread_history.json; the page reads docs/spreads.json.
# ---------------------------------------------------------------------------
SPREADS_JSON = os.path.join("docs", "spreads.json")
SPREAD_HIST = os.path.join("data", "spread_history.json")
SPREAD_KEEP_S = 25 * 60       # add a point at least this often even if the line did not move
SPREAD_STALE_S = 35 * 60      # the hourly build refreshes spreads itself only if the file is older than this


def parse_spread(comp, a_abbr, h_abbr):
    """-> {"line": home-team spread (negative = home favored), "total": over/under} or None."""
    odds = comp.get("odds")
    o = odds[0] if isinstance(odds, list) and odds else odds if isinstance(odds, dict) else None
    if not o:
        return None
    det = str(o.get("details") or "").strip()
    line = None
    if det.upper().startswith(("EVEN", "PK", "PICK")):
        line = 0.0
    else:
        m = re.match(r"([A-Za-z]{2,4})\s*([+-]?\d+(?:\.\d+)?)", det)
        if m:
            ab, v = m.group(1).upper(), abs(float(m.group(2)))
            if ab == h_abbr.upper():
                line = -v
            elif ab == a_abbr.upper():
                line = v
    if line is None and isinstance(o.get("spread"), (int, float)) and isinstance(o.get("homeTeamOdds"), dict) and "favorite" in o["homeTeamOdds"]:
        line = -abs(o["spread"]) if o["homeTeamOdds"]["favorite"] else abs(o["spread"])
    if line is None:
        return None
    return {"line": float(line), "total": o.get("overUnder")}


def _num(v):
    try:
        return float(str(v).replace("−", "-"))
    except (TypeError, ValueError):
        return 0.0 if str(v).strip().upper() in ("EVEN", "PK") else None


def _core_open(eid):
    """The opening home spread from ESPN's odds feed, if that feed lists one. -> float or None"""
    try:
        d = get(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds", tries=2)
    except Exception as e:
        print(f"Spread: no opening line for {eid} ({e})")
        return None
    for it in d.get("items", []):
        ps = (((it.get("homeTeamOdds") or {}).get("open") or {}).get("pointSpread") or {})
        for k in ("alternateDisplayValue", "displayValue", "value"):
            v = _num(ps.get(k))
            if v is not None:
                return v
    return None


INJ_LOG = os.path.join("data", "injury_log.json")


def log_injuries(now_ts):
    """Record every change on the injury report with the time we saw it, so line moves can be matched to news later."""
    inj = load_injuries()
    if not inj:
        return
    state = {}
    for abbr, players in inj.items():
        for p in players:
            state[f"{abbr}|{p['name']}"] = p["status"]
    log = {"since": now_ts, "state": {}, "changes": []}
    try:
        if os.path.exists(INJ_LOG):
            with open(INJ_LOG, encoding="utf-8") as f:
                log = json.load(f)
    except Exception as e:
        print(f"WARNING: could not read injury log: {e}")
    if log.get("state"):
        for k, new in state.items():
            old = log["state"].get(k)
            if old != new:
                ab, nm = k.split("|", 1)
                log["changes"].append([now_ts, ab, nm, old or "Not on report", new])
        for k, old in log["state"].items():
            if k not in state:
                ab, nm = k.split("|", 1)
                log["changes"].append([now_ts, ab, nm, old, "Cleared from report"])
    log["state"] = state
    log["changes"] = [c for c in log["changes"] if c[0] > now_ts - 3 * 86400]
    os.makedirs("data", exist_ok=True)
    with open(INJ_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))


def update_spreads(events, write=True, now_ts=None):
    now_ts = int(now_ts or time.time())
    hist = {"events": {}}
    try:
        if os.path.exists(SPREAD_HIST):
            with open(SPREAD_HIST, encoding="utf-8") as f:
                hist = json.load(f)
            hist.setdefault("events", {})
    except Exception as e:
        print(f"WARNING: could not read spread history: {e}")
    games = []
    for ev in events:
        try:
            comp = ev["competitions"][0]
            side = {c["homeAway"]: c for c in comp["competitors"]}
            a, h = side["away"]["team"], side["home"]["team"]
            eid = str(ev["id"])
            rec = hist["events"].setdefault(eid, {"pts": [], "open": None, "tries": 0})
            state = ((comp.get("status") or {}).get("type") or {}).get("state", "pre")
            sp = parse_spread(comp, a["abbreviation"], h["abbreviation"]) if state == "pre" else None
            if sp:
                pts = rec["pts"]
                if not pts or pts[-1][1] != sp["line"] or now_ts - pts[-1][0] >= SPREAD_KEEP_S:
                    pts.append([now_ts, sp["line"]])
                    if len(pts) > 400:
                        del pts[1:len(pts) - 399]
                rec["total"] = sp["total"]
                rec["checked"] = now_ts
                if rec.get("open") is None and rec.get("tries", 0) < 3:
                    rec["tries"] = rec.get("tries", 0) + 1
                    o = _core_open(eid)
                    if o is not None:
                        rec["open"] = o
            rec["tip"] = int(parse_dt(ev["date"]).timestamp())
            rec["state"] = state
            rec["away"] = {"abbr": a["abbreviation"], "name": a["displayName"]}
            rec["home"] = {"abbr": h["abbreviation"], "name": h["displayName"]}
            pts = rec["pts"]
            opened = rec["open"] if rec.get("open") is not None else (pts[0][1] if pts else None)
            games.append({"id": eid, "away": rec["away"], "home": rec["home"], "tip": rec["tip"], "state": state,
                          "open": opened, "open_src": "espn" if rec.get("open") is not None else "first",
                          "cur": pts[-1][1] if pts else None, "total": rec.get("total"), "pts": pts,
                          "checked": rec.get("checked")})
        except Exception as e:
            print(f"WARNING: spread for event {ev.get('id')} skipped: {e}")
    for k in [k for k, v in hist["events"].items() if v.get("tip", now_ts) < now_ts - 3 * 86400]:
        del hist["events"][k]
    payload = {"generated": now_ts, "games": games}
    if write:
        _safe(log_injuries, now_ts)
        try:
            os.makedirs("docs", exist_ok=True)
            os.makedirs("data", exist_ok=True)
            with open(SPREAD_HIST, "w", encoding="utf-8") as f:
                json.dump(hist, f, separators=(",", ":"))
            with open(SPREADS_JSON, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
        except Exception as e:
            print(f"WARNING: could not save spreads: {e}")
    print(f"Spreads: {sum(1 for g in games if g['cur'] is not None)} of {len(games)} games have a line")
    return payload


def spreads_for_page(events):
    """Reuse docs/spreads.json when the 10-minute job keeps it fresh, otherwise refresh it here."""
    try:
        if os.path.exists(SPREADS_JSON):
            with open(SPREADS_JSON, encoding="utf-8") as f:
                d = json.load(f)
            ids = {str(e["id"]) for e in events}
            if time.time() - d.get("generated", 0) < SPREAD_STALE_S and ids == {g["id"] for g in d.get("games", [])}:
                return d
    except Exception as e:
        print(f"WARNING: could not read {SPREADS_JSON}: {e}")
    return update_spreads(events)


def spreads_only():
    today = datetime.now(ET).date()
    try:
        data = get(f"{API}/scoreboard?dates={today:%Y%m%d}")
    except Exception as e:
        print(f"WARNING: ESPN scoreboard unavailable ({e}); spreads left as they were")
        return
    update_spreads(sorted(data.get("events", []), key=lambda e: e["date"]))


JS_MINE = r"""
(function(){
var KEY='jmag_interest',followed=[],data=null,TZ='America/Los_Angeles';
var el=document.getElementById('spreads');
try{data=JSON.parse(el.textContent)}catch(e){data=null}
var app=document.getElementById('app'),DAY=(app&&app.getAttribute('data-day'))||'';
function load(){followed=[];try{var v=JSON.parse(localStorage.getItem(KEY)||'null');
  if(v&&!Array.isArray(v)&&v.d===DAY&&Array.isArray(v.t))followed=v.t;else if(v)localStorage.removeItem(KEY)}catch(e){}}
function save(){try{localStorage.setItem(KEY,JSON.stringify({d:DAY,t:followed}))}catch(e){}}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function tm(t){try{return new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',timeZone:TZ}).format(new Date(t*1000))}catch(e){return ''}}
function fmt(n){if(n===null||n===undefined)return '-';if(n===0)return 'PK';return (n>0?'+':'−')+Math.abs(n)}
function sp(g,team,v){return v===null||v===undefined?null:(team===g.home.abbr?v:-v)}
function chart(g,team){
  var pts=(g.pts||[]).map(function(p){return [p[0],sp(g,team,p[1])]});
  if(!pts.length)return '';
  var W=320,H=150,L=40,R=14,T=22,B=24,op=sp(g,team,g.open),vals=pts.map(function(p){return p[1]});
  if(op!==null)vals.push(op);
  var lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals);
  if(hi-lo<2){lo-=1;hi+=1}else{lo-=.5;hi+=.5}
  var t0=pts[0][0],t1=pts[pts.length-1][0];
  var X=function(t){return t1>t0?L+(t-t0)/(t1-t0)*(W-L-R):(L+W-R)/2};
  var Y=function(v){return T+(v-lo)/(hi-lo)*(H-T-B)};
  var o='<svg class="sg" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+esc(team)+' spread, opening vs. now">';
  o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(lo+.5*(hi-lo)).toFixed(1)+'" y2="'+Y(lo+.5*(hi-lo)).toFixed(1)+'" class="gl"/>';
  o+='<text x="'+(L-5)+'" y="'+(T+4)+'" class="ax" text-anchor="end">'+fmt(Math.round(lo*2)/2)+'</text>';
  o+='<text x="'+(L-5)+'" y="'+(H-B+4)+'" class="ax" text-anchor="end">'+fmt(Math.round(hi*2)/2)+'</text>';
  if(op!==null){o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(op).toFixed(1)+'" y2="'+Y(op).toFixed(1)+'" class="op"/>'
    +'<text x="'+(L+4)+'" y="'+(Y(op)-4).toFixed(1)+'" class="oplab" text-anchor="start">Open '+fmt(op)+'</text>'}
  if(pts.length>1)o+='<polyline class="ln" points="'+pts.map(function(p){return X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)}).join(' ')+'"/>';
  pts.forEach(function(p,k){o+='<circle cx="'+X(p[0]).toFixed(1)+'" cy="'+Y(p[1]).toFixed(1)+'" r="'+(k===pts.length-1?4.5:2.2)+'" class="pt"><title>'+esc(tm(p[0])+': '+team+' '+fmt(p[1]))+'</title></circle>'});
  var last=pts[pts.length-1];
  o+='<text x="'+Math.min(W-R-14,X(last[0])).toFixed(1)+'" y="'+(Y(last[1])+(Y(last[1])>H/2?-9:16)).toFixed(1)+'" class="cur" text-anchor="middle">Now '+fmt(last[1])+'</text>';
  o+='<text x="'+L+'" y="'+(H-6)+'" class="ax" text-anchor="start">'+tm(t0)+'</text>';
  if(t1>t0)o+='<text x="'+(W-R)+'" y="'+(H-6)+'" class="ax" text-anchor="end">'+tm(t1)+'</text>';
  return o+'</svg><p class="cap">Higher on the chart = '+esc(team)+' more favored. Times are PT.</p>';
}
function gameCard(g){
  var team=followed.indexOf(g.away.abbr)>=0?g.away.abbr:g.home.abbr;
  var cur=sp(g,team,g.cur),op=sp(g,team,g.open),h='<div class="mg"><div class="mgh"><b>'+esc(g.away.abbr)+' at '+esc(g.home.abbr)+'</b><span>'+esc(tm(g.tip))+' PT</span></div>';
  if(cur===null)return h+'<p class="calm">No line posted yet. It will appear here once the books post it.</p></div>';
  var mv=cur-op,mt=Math.abs(mv)<.01?'no move':(mv<0?'moved '+Math.abs(mv)+' toward '+esc(team):'moved '+Math.abs(mv)+' away from '+esc(team));
  h+='<div class="mnum"><div><small>Now</small><b>'+esc(team)+' '+fmt(cur)+'</b></div><div><small>'+(g.open_src==='espn'?'Opened':'First seen')+'</small><b>'+esc(team)+' '+fmt(op)+'</b></div>'
    +'<div><small>Move</small><b>'+mt+'</b></div>'+(g.total!==null&&g.total!==undefined?'<div><small>Total</small><b>'+esc(g.total)+'</b></div>':'')+'</div>';
  h+=chart(g,team);
  if(g.state!=='pre')h+='<p class="cap">Game has started; the line is frozen at the last pre-game reading.</p>';
  else if(g.open_src!=='espn')h+='<p class="cap">ESPN did not list an opening line, so "first seen" is the first reading this page recorded.</p>';
  return h+'</div>';
}
function renderMine(){
  var box=document.getElementById('mine-body');if(!box)return;
  var games=(data&&data.games)||[],h='';
  if(!followed.length){box.innerHTML='<p class="calm">Tap <b>&#9734; Interested</b> next to a team on any game page. Teams you pick are listed here with their spread for today, and the list clears itself tomorrow.</p>';return}
  h+='<div class="chips">'+followed.map(function(t){return '<span class="chip2">'+esc(t)+'<button type="button" data-rm="'+esc(t)+'" aria-label="Remove '+esc(t)+'">&times;</button></span>'}).join('')+'</div>';
  var mine=games.filter(function(g){return followed.indexOf(g.away.abbr)>=0||followed.indexOf(g.home.abbr)>=0});
  var playing={};mine.forEach(function(g){playing[g.away.abbr]=1;playing[g.home.abbr]=1});
  var off=followed.filter(function(t){return !playing[t]});
  if(!data)h+='<p class="calm">Spread data is not available yet.</p>';
  else if(!mine.length)h+='<p class="calm">None of your picked teams play today.</p>';
  h+=mine.map(gameCard).join('');
  if(off.length&&mine.length)h+='<p class="cap">Not playing today: '+off.map(esc).join(', ')+'.</p>';
  if(data&&data.generated)h+='<p class="src">Spreads checked '+tm(data.generated)+' PT &middot; this page re-reads them every 10 minutes while it is open.</p>';
  box.innerHTML=h;
}
function sync(){[].forEach.call(document.querySelectorAll('.fol'),function(b){var on=followed.indexOf(b.getAttribute('data-t'))>=0;b.classList.toggle('on',on);b.setAttribute('aria-pressed',on?'true':'false');b.innerHTML=(on?'&#9733;':'&#9734;')+' Interested'})}
document.addEventListener('click',function(e){
  var b=e.target.closest&&e.target.closest('.fol,[data-rm]');if(!b)return;
  var t=b.getAttribute('data-t')||b.getAttribute('data-rm'),k=followed.indexOf(t);
  if(b.hasAttribute('data-rm')||k>=0){if(k>=0)followed.splice(k,1)}else followed.push(t);
  save();sync();renderMine();
});
function refresh(){
  if(!window.fetch)return;
  fetch('spreads.json?_='+Date.now(),{cache:'no-store'}).then(function(r){return r.ok?r.json():Promise.reject()})
   .then(function(d){if(d&&d.games){data=d;renderMine()}}).catch(function(){});
}
load();sync();renderMine();refresh();
setInterval(refresh,600000);
document.addEventListener('visibilitychange',function(){if(!document.hidden)refresh()});
})();
"""



NATIONAL_NETS = {"ESPN", "ESPN2", "ABC", "NBC", "PEACOCK", "PRIME VIDEO", "AMAZON PRIME VIDEO", "AMAZON", "NBA TV", "CBS",
                 "PARAMOUNT+", "TNT", "TBS", "TRUTV", "MAX", "HBO MAX", "FOX", "FS1", "NETFLIX", "APPLE TV", "APPLE TV+", "YOUTUBE", "NBCSN", "USA"}
NET_RENAME = {"AMAZON PRIME VIDEO": "Prime Video", "AMAZON": "Prime Video", "PRIME VIDEO": "Prime Video", "PEACOCK": "Peacock",
              "NBA TV": "NBA TV", "HBO MAX": "HBO Max", "MAX": "Max", "PARAMOUNT+": "Paramount+", "TRUTV": "truTV", "APPLE TV": "Apple TV", "APPLE TV+": "Apple TV+"}


def nat_networks(comp):
    """National TV/streaming networks for a game (local and regional channels are left out). -> list of names"""
    found = []
    def add(name):
        name = " ".join(str(name or "").split())
        if not name:
            return
        key = name.upper()
        label = NET_RENAME.get(key) or (name if key not in NATIONAL_NETS or len(name) > 4 else key)
        if label not in found:
            found.append(label)
    try:
        for b in comp.get("broadcasts") or []:
            market = str(b.get("market") or "").lower()
            if isinstance(b.get("market"), dict):
                market = str(b["market"].get("type") or "").lower()
            names = b.get("names") or ([b["name"]] if b.get("name") else [])
            if market == "national" or (not market and any(str(n).upper() in NATIONAL_NETS for n in names)):
                for n in names:
                    add(n)
        for g in comp.get("geoBroadcasts") or []:
            market = g.get("market")
            market = str(market.get("type") if isinstance(market, dict) else market or "").lower()
            media = g.get("media") or {}
            name = media.get("shortName") or media.get("name")
            if market == "national" or (not market and str(name).upper() in NATIONAL_NETS):
                add(name)
    except Exception as e:
        print(f"WARNING: could not read broadcasters: {e}")
    return found


def net_html(comp):
    nets = nat_networks(comp)
    return f'<span class="net">{escape(" · ".join(nets))}</span>' if nets else ""


# ---------------------------------------------------------------------------
# Yesterday's summary: finals, box score leaders, how the line moved (and the likely reason), highlight links.
# ---------------------------------------------------------------------------
BIG_MOVE = 1.0     # points; a move at least this big gets a numbered marker and a reason
SEVERITY = ("out", "doubtful", "questionable", "day-to-day", "probable", "available", "active")


def _ptime(t):
    return datetime.fromtimestamp(t, PT).strftime("%-I:%M %p")


def fmt_line(team, v):
    if v is None:
        return "-"
    return "PK" if v == 0 else f"{team} {'+' if v > 0 else chr(0x2212)}{abs(v):g}"


def _read_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"WARNING: could not read {path}: {e}")
    return default


def yt_search(q):
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q)


def yt_game(away, home, day):
    return yt_search(f"{away} vs {home} full game highlights {day:%B %-d %Y}")


def yt_top10(day):
    return "https://www.youtube.com/@NBA/search?query=" + urllib.parse.quote_plus(f"top 10 plays {day:%B %-d}")


def _score(c):
    v = c.get("score")
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def comp_leaders(c):
    out = {}
    for cat in c.get("leaders") or []:
        nm, ls = cat.get("name"), cat.get("leaders") or []
        if ls and nm in ("points", "rebounds", "assists"):
            a = ls[0].get("athlete") or {}
            out[nm] = (a.get("shortName") or a.get("displayName") or "?", ls[0].get("displayValue") or ls[0].get("value") or "")
    return out


def box_leaders(eid):
    """Fallback: leaders from the box score. -> {team id: {"points": (name, n), ...}}"""
    res = {}
    try:
        d = get(f"{API}/summary?event={eid}")
        for team in (d.get("boxscore") or {}).get("players", []):
            tid = str(team["team"]["id"])
            blocks = team.get("statistics") or []
            if not blocks:
                continue
            labels = blocks[0].get("labels") or []
            best = {}
            for key, lab in (("points", "PTS"), ("rebounds", "REB"), ("assists", "AST")):
                if lab not in labels:
                    continue
                ix = labels.index(lab)
                for a in blocks[0].get("athletes") or []:
                    try:
                        v = float((a.get("stats") or [])[ix])
                    except (IndexError, ValueError, TypeError):
                        continue
                    if key not in best or v > best[key][2]:
                        nm = (a.get("athlete") or {}).get("shortName") or (a.get("athlete") or {}).get("displayName") or "?"
                        best[key] = (nm, f"{v:.0f}", v)
            res[tid] = {k: (v[0], v[1]) for k, v in best.items()}
    except Exception as e:
        print(f"WARNING: box score leaders for {eid} failed: {e}")
    return res


def explain_moves(pts, changes, teams, since):
    """Big moves between consecutive readings, each with injury-report changes recorded around the same time."""
    out = []
    for i in range(1, len(pts)):
        d = pts[i][1] - pts[i - 1][1]
        if abs(d) < BIG_MOVE:
            continue
        t0, t1 = pts[i - 1][0], pts[i][0]
        cand = [c for c in changes if t0 - 900 <= c[0] <= t1 + 300 and c[1] in teams]
        cand.sort(key=lambda c: (SEVERITY.index(c[4].lower()) if c[4].lower() in SEVERITY else 9))
        out.append({"i": i, "t0": t0, "t1": t1, "frm": pts[i - 1][1], "to": pts[i][1], "d": d,
                    "causes": cand[:3], "tracked": since is not None and t0 >= since - 600})
    out.sort(key=lambda m: -abs(m["d"]))
    keep = sorted(out[:6], key=lambda m: m["i"])
    for n, m in enumerate(keep, 1):
        m["n"] = n
    return keep


def move_chart(pts, open_v, home, moves):
    """SVG: the home team's spread from the opener through every reading to tip-off. Higher = home more favored."""
    W, H, L, R, T, B = 320, 170, 40, 16, 24, 28
    vals = [p[1] for p in pts] + ([open_v] if open_v is not None else [])
    lo, hi = min(vals), max(vals)
    lo, hi = (lo - 1, hi + 1) if hi - lo < 2 else (lo - .5, hi + .5)
    t0, t1 = pts[0][0], pts[-1][0]
    X = lambda t: L + (t - t0) / (t1 - t0) * (W - L - R) if t1 > t0 else (L + W - R) / 2
    Y = lambda v: T + (v - lo) / (hi - lo) * (H - T - B)
    lab = lambda v: "PK" if v == 0 else f"{'+' if v > 0 else chr(0x2212)}{abs(v):g}"
    o = [f'<svg class="sg" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(home)} spread from open to tip-off">']
    o.append(f'<text x="{L - 5}" y="{T + 4}" class="ax" text-anchor="end">{lab(round(lo * 2) / 2)}</text>')
    o.append(f'<text x="{L - 5}" y="{H - B + 4}" class="ax" text-anchor="end">{lab(round(hi * 2) / 2)}</text>')
    if open_v is not None:
        o.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(open_v):.1f}" y2="{Y(open_v):.1f}" class="op"/>'
                 f'<text x="{L + 4}" y="{Y(open_v) - 4:.1f}" class="oplab">Open {lab(open_v)}</text>')
    if len(pts) > 1:
        o.append('<polyline class="ln" points="' + " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in pts) + '"/>')
    for p in pts:
        o.append(f'<circle cx="{X(p[0]):.1f}" cy="{Y(p[1]):.1f}" r="2.2" class="pt"><title>{escape(_ptime(p[0]) + " PT: " + home + " " + lab(p[1]))}</title></circle>')
    for m in moves:
        x, y = X(m["t1"]), Y(m["to"])
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" class="mk"/><text x="{x:.1f}" y="{y + 3.6:.1f}" class="mkn" text-anchor="middle">{m["n"]}</text>')
    lx, ly = X(t1), Y(pts[-1][1])
    o.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.8" class="pt"/>'
             f'<text x="{min(lx, W - R - 20):.1f}" y="{ly + (-11 if ly > H / 2 else 18):.1f}" class="cur" text-anchor="middle">Close {lab(pts[-1][1])}</text>')
    o.append(f'<text x="{L}" y="{H - 8}" class="ax">{_ptime(t0)}</text>')
    if t1 > t0:
        o.append(f'<text x="{W - R}" y="{H - 8}" class="ax" text-anchor="end">{_ptime(t1)} (tip-off)</text>')
    o.append('</svg>')
    return "".join(o) + f'<p class="cap">Higher on the chart = {escape(home)} more favored. Times are PT.</p>'


def line_section(rec, home_abbr, away_abbr, fallback_line):
    pts = (rec or {}).get("pts") or []
    if not pts and fallback_line is not None:
        pts = [[0, fallback_line]]
    if not pts:
        return '<section class="grp" style="--k:var(--c-line)"><h3>Line movement</h3><p class="calm">No line was recorded for this game.</p></section>'
    close = pts[-1][1]
    open_v = rec.get("open") if rec and rec.get("open") is not None else pts[0][1]
    src = "Opened" if rec and rec.get("open") is not None else "First seen"
    mv = close - open_v
    mt = "no move" if abs(mv) < .01 else (f"{abs(mv):g} toward {home_abbr}" if mv < 0 else f"{abs(mv):g} toward {away_abbr}")
    rows = [row(src + " / closed at tip-off", fmt_line(home_abbr, open_v), f"{escape(fmt_line(home_abbr, close))}<small>{'no net move' if mt == 'no move' else 'moved ' + escape(mt)}</small>", "line")]
    out = [f'<section class="grp" style="--k:var(--c-line)"><h3>Line movement</h3>' + "".join(rows)]
    if len(pts) < 2 or pts[0][0] == 0:
        out.append('<p class="calm">Only one reading was recorded, so there is no graph for this game. The 10-minute tracker fills this in from the next game day.</p>')
    else:
        log = _read_json(INJ_LOG, {"changes": [], "since": None})
        moves = explain_moves(pts, log.get("changes", []), {home_abbr, away_abbr}, log.get("since"))
        out.append(move_chart(pts, open_v, home_abbr, moves))
        if moves:
            items = []
            for m in moves:
                head = f'{_ptime(m["t0"])}&ndash;{_ptime(m["t1"])}: {escape(fmt_line(home_abbr, m["frm"]))} &rarr; {escape(fmt_line(home_abbr, m["to"]))}'
                if m["causes"]:
                    why = "Possible cause: " + "; ".join(
                        f'{escape(c[2])} ({escape(c[1])}) {escape(str(c[3]))} &rarr; {escape(str(c[4]))} at {_ptime(c[0])}' for c in m["causes"])
                elif not m["tracked"]:
                    why = "No clear cause. Injury tracking had not started yet at this time."
                else:
                    why = "No clear cause found. No injury-report change was recorded in that window, so this looks like general market movement."
                items.append(f'<li><b>{m["n"]}</b><div>{head}<small>{why}</small></div></li>')
            out.append('<ol class="why">' + "".join(items) + '</ol>')
        else:
            out.append('<p class="calm">No single move was a full point or more. The line only drifted.</p>')
    out.append('</section>')
    return "".join(out)


def y_game_page(ev, idx, total, day, hist):
    comp = ev["competitions"][0]
    side = {c["homeAway"]: c for c in comp["competitors"]}
    a, h = side["away"], side["home"]
    ab, hb = a["team"]["abbreviation"], h["team"]["abbreviation"]
    sa, sh = _score(a), _score(h)
    ls_a, ls_h = _linescores(a) or [], _linescores(h) or []
    ot = max(0, len(ls_a) - 4)
    final = "Final" + (f"/{ot}OT" if ot > 1 else "/OT" if ot == 1 else "")
    rec = hist["events"].get(str(ev["id"]))
    spr = parse_spread(comp, ab, hb)
    pts = (rec or {}).get("pts") or []
    line = pts[-1][1] if pts else (spr["line"] if spr else None)
    total_line = (rec or {}).get("total") if rec and rec.get("total") is not None else (spr["total"] if spr else None)
    # against the spread / over-under
    ats_a = ats_h = ""
    hi_ats = ""
    if line is not None:
        adj = (sh - sa) + line
        if adj > 0:
            ats_a, ats_h, hi_ats = "Did not cover", f"Covered<small>won by {sh - sa:+d} vs. {fmt_line(hb, line)}</small>", "h"
        elif adj < 0:
            ats_a, ats_h, hi_ats = f"Covered<small>{fmt_line(ab, -line)} line</small>", "Did not cover", "a"
        else:
            ats_a = ats_h = "Push"
    ou = ""
    if total_line not in (None, ""):
        try:
            tl = float(total_line)
            res = "Over" if sa + sh > tl else "Under" if sa + sh < tl else "Push"
            ou = row("Over / under", f"{sa + sh} points", f"{res}<small>line {tl:g}</small>", "flags")
        except (TypeError, ValueError):
            pass
    ats_row = row("Against the spread", ats_a, ats_h, "flags", hi_ats) if line is not None else ""
    # quarter table
    qt = ""
    if len(ls_a) >= 4 and len(ls_h) >= 4:
        heads = "".join(f"<th>{'Q' + str(i + 1) if i < 4 else ('OT' if ot == 1 else 'OT' + str(i - 3))}</th>" for i in range(len(ls_a)))
        r = lambda ab_, ls: f"<tr><th>{escape(ab_)}</th>" + "".join(f"<td>{x:.0f}</td>" for x in ls) + "</tr>"
        qt = f'<table class="qt"><tr><th></th>{heads}</tr>{r(ab, ls_a)}{r(hb, ls_h)}</table>'
    # leaders
    la, lh = comp_leaders(a), comp_leaders(h)
    if len(la) < 3 or len(lh) < 3:
        bl = box_leaders(ev["id"])
        la = {**bl.get(str(a["team"]["id"]), {}), **la}
        lh = {**bl.get(str(h["team"]["id"]), {}), **lh}
    cell = lambda d, k: f"{escape(str(d[k][0]))} <b>{escape(str(d[k][1]))}</b>" if k in d else "-"
    lead_rows = [row(lab, cell(la, k), cell(lh, k), "form") for lab, k in (("Points", "points"), ("Rebounds", "rebounds"), ("Assists", "assists")) if k in la or k in lh]
    win_a = sa > sh
    page = f"""<article class="page" data-set="y" data-label="Game {idx} of {total}">
  <header class="match">
    <div class="teams"><h2 class="t a"><span class="tn">{escape(a["team"]["displayName"])}<small class="rec">{escape(record_text(a, None))}</small></span></h2>
    <div class="mid"><span class="at">at</span><span class="tip">{final}</span></div>
    <h2 class="t h"><span class="tn">{escape(h["team"]["displayName"])}<small class="rec">{escape(record_text(h, None))}</small></span></h2></div>
    <div class="teams scorerow"><b class="sc a{' win' if win_a else ''}">{sa}</b><span class="dash">&ndash;</span><b class="sc h{'' if win_a else ' win'}">{sh}</b></div>
  </header>
  <section class="grp" style="--k:var(--c-flags)"><h3>Result</h3>{qt}{ats_row}{ou}</section>
  {section("Box score leaders", "form", lead_rows)}
  {line_section(rec, hb, ab, line)}
  <section class="grp" style="--k:var(--c-travel)"><h3>Highlights</h3><a class="hl" href="{escape(yt_game(a["team"]["displayName"], h["team"]["displayName"], day), quote=True)}" target="_blank" rel="noopener">&#9654; Watch {escape(ab)} at {escape(hb)} highlights on YouTube</a></section>
  <section class="grp more" style="--k:var(--mute)"><h3>More coming</h3><p class="calm">Reserved for additional data points.</p></section>
</article>"""
    return page, {"ab": ab, "hb": hb, "sa": sa, "sh": sh, "final": final, "link": yt_game(a["team"]["displayName"], h["team"]["displayName"], day),
                  "ats": (f"{hb} covered" if line is not None and (sh - sa) + line > 0 else f"{ab} covered" if line is not None and (sh - sa) + line < 0 else "push" if line is not None else "")}


def y_recap_page(day, items, n):
    games = "".join(
        f'<div class="yrow"><span class="ys"><b class="{"win" if i["sa"] > i["sh"] else ""}">{escape(i["ab"])} {i["sa"]}</b> <i>at</i> '
        f'<b class="{"win" if i["sh"] > i["sa"] else ""}">{escape(i["hb"])} {i["sh"]}</b><small>{escape(i["final"])}{(" &middot; " + escape(i["ats"])) if i["ats"] else ""}</small></span>'
        f'<a class="hl sm" href="{escape(i["link"], quote=True)}" target="_blank" rel="noopener">&#9654; Highlights</a></div>' for i in items)
    body = (f'<section class="grp" style="--k:var(--c-flags)"><h3>Scoreboard</h3>{games or "<p class=calm>No games were played yesterday.</p>"}</section>'
            f'<section class="grp" style="--k:var(--c-travel)"><h3>Everything you missed</h3>'
            f'<a class="hl" href="{escape(yt_top10(day), quote=True)}" target="_blank" rel="noopener">&#9654; NBA Top 10 plays, {day:%B %-d}</a>'
            f'<a class="hl" href="{escape(yt_search("NBA game recaps full game highlights " + day.strftime("%B %-d %Y")), quote=True)}" target="_blank" rel="noopener">&#9654; All of the night\'s game recaps</a>'
            f'<a class="hl" href="https://www.youtube.com/@NBA/videos" target="_blank" rel="noopener">&#9654; NBA YouTube channel, newest videos</a>'
            f'<p class="cap">These open YouTube search results, so each link lands on the right videos in one tap.</p></section>')
    return (f'<article class="page" data-set="y" data-label="Recap"><header class="match"><h2 class="minehead">Yesterday</h2>'
            f'<p class="sub ctr">{day:%A, %B %-d, %Y} &middot; {n} game{"s" if n != 1 else ""}</p></header>{body}</article>')


def build_yesterday(today):
    day = today - timedelta(days=1)
    try:
        data = get(f"{API}/scoreboard?dates={day:%Y%m%d}")
    except Exception as e:
        print(f"WARNING: yesterday's scoreboard unavailable: {e}")
        return ('<p class="empty">Yesterday\'s results could not be loaded this hour. They should return on the next update.</p>', 0)
    evs = [e for e in sorted(data.get("events", []), key=lambda e: e["date"])
           if (e["competitions"][0].get("status", {}).get("type", {}) or {}).get("completed")]
    hist = _read_json(SPREAD_HIST, {"events": {}})
    hist.setdefault("events", {})
    pages, items = [], []
    for k, e in enumerate(evs, 1):
        try:
            pg, it = y_game_page(e, k, len(evs), day, hist)
            pages.append(pg)
            items.append(it)
        except Exception:
            print(f"WARNING: yesterday's page for event {e.get('id')} failed")
            traceback.print_exc()
    html = y_recap_page(day, items, len(items)) + "".join(pages)
    print(f"Yesterday: {len(items)} of {len(evs)} games built")
    return html, len(items)


def fallback_card(ev, idx):
    """Bare-bones page for a game whose full card could not be built, so one bad game never breaks the site."""
    try:
        comp = ev["competitions"][0]["competitors"]
        away = next(c for c in comp if c["homeAway"] == "away")["team"]["displayName"]
        home = next(c for c in comp if c["homeAway"] == "home")["team"]["displayName"]
        tip = parse_dt(ev["date"]).astimezone(PT).strftime("%-I:%M %p PT")
        net = net_html(ev["competitions"][0])
    except Exception:
        away, home, tip, net = "Away team", "Home team", "", ""
    return (f'<article class="page" data-i="{idx}" data-set="t" data-label="{idx + 1}"><header class="match">'
            f'<div class="teams"><h2 class="t a"><span class="tn">{escape(away)}</span></h2>'
            f'<div class="mid"><span class="at">at</span><span class="tip">{escape(tip)}</span>{net}</div><h2 class="t h"><span class="tn">{escape(home)}</span></h2></div></header>'
            f'<p class="calm">Details for this game could not be loaded this hour. They should return on the next update.</p></article>')


def main():
    now = datetime.now(ET)
    today = now.date()
    season = today.year + 1 if today.month >= 8 else today.year
    try:
        data = get(f"{API}/scoreboard?dates={today:%Y%m%d}")
    except Exception as e:
        print(f"WARNING: ESPN scoreboard unavailable after retries ({e}); keeping the previous page")
        return
    events = sorted(data.get("events", []), key=lambda e: e["date"])
    revenge = load_revenge()
    rivalries = load_rivalries()
    for e in events:
        for c in e["competitions"][0]["competitors"]:
            roster_names(c["team"]["id"])
            team_history(c["team"]["id"], season)
    for loader, args in ((load_injuries, ()), (load_crowd, ()), (load_forecast, ()), (load_ppp, (today, season))):
        try:
            loader(*args)
        except Exception:
            print(f"WARNING: {loader.__name__} failed; continuing without it")
            traceback.print_exc()
    try:
        ratings = load_ratings(today)
    except Exception:
        print("WARNING: load_ratings failed; continuing without ranks")
        traceback.print_exc()
        ratings = None
    def safe_card(e, i):
        try:
            return card(e, today, season, revenge, rivalries, ratings, i, len(events))
        except Exception:
            print(f"WARNING: could not build the card for event {e.get('id')}")
            traceback.print_exc()
            return fallback_card(e, i)
    cards = "".join(safe_card(e, i) for i, e in enumerate(events)) or '<p class="empty">No NBA games today. Check back tomorrow morning.</p>'
    try:
        spread_payload = spreads_for_page(events)
    except Exception:
        print("WARNING: spreads failed; the My teams page will have no lines")
        traceback.print_exc()
        spread_payload = {"generated": 0, "games": []}
    spread_json = json.dumps(spread_payload, separators=(",", ":")).replace("</", "<\\/")
    mine_page = ('<article class="page" id="mine" data-set="t" data-label="Interested"><header class="match"><h2 class="minehead">Interested today</h2></header>'
                 '<div id="mine-body"></div></article>') if events else ""
    cards += mine_page
    try:
        y_html, y_n = build_yesterday(today)
    except Exception:
        print("WARNING: yesterday's summary failed")
        traceback.print_exc()
        y_html, y_n = '<p class="empty">Yesterday\'s summary could not be built this hour.</p>', 0
    foot = (ratings["note"] + " " if ratings else "") + "Rotation age weights each player by projected minutes; roster average counts every player equally. Points per possession come from box scores (possessions = FGA + 0.44 x FTA - offensive rebounds + turnovers) and count regular-season games only. The shot-clock rows are an estimate from play-by-play game-clock times, not official tracking data. Season outlook, impact and projected minutes come from your forecast file; impact is a player's contribution to team strength in that model. Form uses completed regular-season games only. Injuries and officials come from ESPN and may lag. Crowd % is the Polymarket price, a rough signal when trading is thin."
    stamp = now.astimezone(PT).strftime("%A, %B %-d, %Y") + " &middot; updated " + now.astimezone(PT).strftime("%-I:%M %p PT")
    n = len(events)
    pager = ('<div class="pager"><button id="back" type="button">&lsaquo; Back</button>'
             '<span class="count" id="count"></span><button id="fwd" type="button">Forward &rsaquo;</button></div>')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>JMAG'S NBA Daily Digest</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&family=Barlow+Condensed:wght@600;700&family=Oxanium:wght@700;800&display=swap">
<style>{CSS}</style></head><body>
<div id="gate"><h1 class="gate-title"><span>JMAG'S NBA</span><span>DAILY DIGEST</span></h1><div class="gbtns"><button class="gbtn" id="enter" type="button" data-go="t">Today's games</button><button class="gbtn" id="enter-y" type="button" data-go="y">Yesterday's summary</button></div></div>
<main id="app" data-day="{today:%Y-%m-%d}">
<div class="bar-top"><div class="topline"><h1>JMAG'S NBA Daily Digest</h1><button id="menu" type="button">&#8962; Menu</button></div>
<p class="sub sub-t">{stamp} &middot; {n} game{'s' if n != 1 else ''}</p><p class="sub sub-y">Yesterday's summary &middot; {y_n} game{'s' if y_n != 1 else ''}</p>{pager}</div>
<div class="book" data-book="t">{cards}</div>
<div class="book" data-book="y">{y_html}</div>
<div class="legend"><span><i style="--k:var(--c-flags)"></i>What stands out</span><span><i style="--k:var(--c-rest)"></i>Rest &amp; schedule</span><span><i style="--k:var(--c-half)"></i>First vs. second half</span><span><i style="--k:var(--c-travel)"></i>Travel</span>
<span><i style="--k:var(--c-injury)"></i>Injuries</span><span><i style="--k:var(--c-rivalry)"></i>Revenge, rivalry &amp; head to head</span>
<span><i style="--k:var(--c-match)"></i>Matchup (solid = elite, dashed = bottom 10)</span><span><i style="--k:var(--c-form)"></i>Form</span>
<span><i style="--k:var(--c-outlook)"></i>Season outlook</span><span><i style="--k:var(--c-age)"></i>Age, roster &amp; coach</span><span><i style="--k:var(--c-line)"></i>Line &amp; crowd</span><span><i style="--k:var(--c-ref)"></i>Officials</span></div>
<p class="foot">{foot}</p>
</main><script type="application/json" id="spreads">{spread_json}</script><script>{JS}</script><script>{JS_MINE}</script></body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote docs/index.html with {n} games")


if __name__ == "__main__":
    if "--spreads" in sys.argv:
        spreads_only()
    else:
        main()
