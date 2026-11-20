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
_coach_names = {}
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
    _coach_names[str(tid)] = coaches
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
                        "win": bool(me.get("winner")), "our": sc(me), "their": sc(opp), "playoff": stype == 3,
                        "home": me.get("homeAway") == "home"})
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


def ov_logo(team):
    """Plain team logo (no circle behind it) for the Overview page; ESPN's standard logo URL if the feed has none."""
    ab = (team.get("abbreviation") or "").strip()
    url = (team.get("logo") or "").strip()
    if not url.startswith("https://"):
        url = f"https://a.espncdn.com/i/teamlogos/nba/500/{ab.lower()}.png"
    return (f'<img class="ovlogo" src="{escape(url, quote=True)}" alt="{escape(ab, quote=True)}" referrerpolicy="no-referrer" '
            f'onerror="this.replaceWith(document.createTextNode(this.alt))">')


def record_text(comp_team, info):
    """Overall record from ESPN's scoreboard, else counted from finished games."""
    recs = comp_team.get("records") or []
    tot = next((r for r in recs if (r.get("type") == "total" or (r.get("name") or "").lower() == "overall") and r.get("summary")), None)
    if tot:
        return tot["summary"]
    hist = (info or {}).get("hist") or []
    w = sum(1 for g in hist if g["win"])
    return f"{w}-{len(hist) - w}"


# ---------------------------------------------------------------------------
# Head coaches. "team" = first season of the current stint with the team, "hc" = first season as an NBA head coach
# with any team, both as the year the season ends (2027 = 2026-27). A season counts if the coach started it or took
# over partway through it. Checked on 2026-11-13 against Basketball-Reference (seasons with franchise / overall) and
# the Wikipedia list of current NBA head coaches. To correct or add a coach without touching this file, put
# data/coaches.json next to build.py: {"ABBR": {"name": "...", "team": 2027, "hc": 2027}}.
# ---------------------------------------------------------------------------
COACHES = {
    "ATL": ("Quin Snyder", 2023, 2015), "BOS": ("Joe Mazzulla", 2023, 2023), "BKN": ("Jordi Fernandez", 2025, 2025),
    "CHA": ("Charles Lee", 2025, 2025), "CHI": ("Tiago Splitter", 2027, 2026), "CLE": ("Kenny Atkinson", 2025, 2017),
    "DAL": ("Dusty May", 2027, 2027), "DEN": ("David Adelman", 2025, 2025), "DET": ("J.B. Bickerstaff", 2025, 2016),
    "GS": ("Steve Kerr", 2015, 2015), "HOU": ("Ime Udoka", 2024, 2022), "IND": ("Rick Carlisle", 2022, 2002),
    "LAC": ("Tyronn Lue", 2021, 2016), "LAL": ("JJ Redick", 2025, 2025), "MEM": ("Tuomas Iisalo", 2025, 2025),
    "MIA": ("Erik Spoelstra", 2009, 2009), "MIL": ("Taylor Jenkins", 2027, 2020), "MIN": ("Chris Finch", 2021, 2021),
    "NO": ("Jamahl Mosley", 2027, 2022), "NY": ("Mike Brown", 2026, 2006), "OKC": ("Mark Daigneault", 2021, 2021),
    "ORL": ("Sean Sweeney", 2027, 2027), "PHI": ("Nick Nurse", 2024, 2019), "PHX": ("Jordan Ott", 2026, 2026),
    "POR": ("Micah Nori", 2027, 2027), "SAC": ("Doug Christie", 2025, 2025), "SA": ("Mitch Johnson", 2025, 2025),
    "TOR": ("Darko Rajakovic", 2024, 2024), "UTAH": ("Will Hardy", 2023, 2023), "WSH": ("Brian Keefe", 2024, 2024),
}
COACH_ALIASES = {"GSW": "GS", "NOP": "NO", "NYK": "NY", "SAS": "SA", "UTA": "UTAH", "WAS": "WSH", "PHO": "PHX", "BRK": "BKN", "CHO": "CHA"}
_coach_file = None


def _coach_entry(abbr):
    global _coach_file
    if _coach_file is None:
        _coach_file = {}
        path = os.path.join(os.path.dirname(__file__) or ".", "data", "coaches.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    _coach_file = {COACH_ALIASES.get(k.upper(), k.upper()): v for k, v in json.load(f).items()}
        except Exception as e:
            print(f"WARNING: could not read data/coaches.json: {e}")
    key = COACH_ALIASES.get(abbr.upper(), abbr.upper())
    if key in _coach_file:
        v = _coach_file[key]
        return v.get("name"), v.get("team"), v.get("hc")
    return COACHES.get(key)


def _seasons_text(n, what):
    return f"1st season {what}" if n == 1 else f"{n} seasons {what}"


def coach_html(comp_team):
    """Two small lines under a team's name: the head coach, and how long with the team / as an NBA head coach."""
    try:
        t = comp_team["team"]
        entry = _coach_entry(t["abbreviation"])
        roster_names(t["id"])
        live = _coach_names.get(str(t["id"])) or []
        season_now = datetime.now(ET).year + (1 if datetime.now(ET).month >= 8 else 0)
        if entry and entry[0]:
            last = norm(entry[0]).split()[-1]
            if live and not any(last in norm(c).split() for c in live):
                # ESPN lists a different head coach than our table: a change since it was checked
                print(f"WARNING: coach for {t['abbreviation']} is {live} on ESPN but {entry[0]} on file")
                return f'<small class="coach">Coach {escape(live[0])}</small><small class="coach">recently changed</small>'
            n_team, n_hc = season_now - entry[1] + 1, season_now - entry[2] + 1
            if n_team < 1 or n_hc < 1:
                return f'<small class="coach">Coach {escape(entry[0])}</small>'
            return (f'<small class="coach">Coach {escape(entry[0])}</small>'
                    f'<small class="coach tn2">{_seasons_text(n_team, "here")} &middot; {"1st" if n_hc == 1 else n_hc} as NBA HC</small>')
        if live:
            return f'<small class="coach">Coach {escape(live[0])}</small>'
    except Exception as e:
        print(f"WARNING: coach line skipped: {e}")
    return ""


def name_block(comp_team, info, side=""):
    t = comp_team["team"]
    rec = escape(record_text(comp_team, info))
    ab = escape(t["abbreviation"], quote=True)
    return (f'<span class="tn">{escape(t["displayName"])}<small class="rec">{rec}</small>{coach_html(comp_team)}'
            f'<span class="fs" data-s="{side}" hidden></span>'
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
    rows = [
        _safe(half_summary, ha, hh, a_abbr, h_abbr),
        row("1st half: scored / allowed", h1(ha), h1(hh), "half"),
        row("2nd half: scored / allowed", h2(ha), h2(hh), "half"),
        row("Scoring, 2nd half minus 1st", sh(ha, "shift_f"), sh(hh, "shift_f"), "half"),
        row("Allowed, 2nd half minus 1st", sh(ha, "shift_a"), sh(hh, "shift_a"), "half"),
        row("Margin, 2nd half minus 1st", shn(ha), shn(hh), "half", dom),
        '<p class="cap">Per game averages, regular season, overtime left out.</p>',
    ]
    return rows


def season_table(games, a_abbr, h_abbr):
    """Every regular-season meeting so far this season, oldest first. games are from the away team's point of view."""
    aw = sum(g["win"] for g in games)
    body = []
    for g in games:
        win_ab, lose_ab = (a_abbr, h_abbr) if g["win"] else (h_abbr, a_abbr)
        hi, lo = int(max(g["our"], g["their"])), int(min(g["our"], g["their"]))
        hm = g.get("home")
        place = f"{escape(a_abbr)} vs {escape(h_abbr)}" if hm is None else (f"{escape(a_abbr)} at {escape(h_abbr)}" if not hm else f"{escape(h_abbr)} at {escape(a_abbr)}")
        body.append(f'<tr><td>{g["date"]:%b %-d}</td><td>{place}</td><td><b>{escape(win_ab)}</b> won {hi}&ndash;{lo}</td><td>{hi - lo}</td></tr>')
    return (f'<table class="mt"><tr><th>Date</th><th>Game</th><th>Result</th><th>Margin</th></tr>{"".join(body)}</table>', aw)


def winprob_section(a_abbr, h_abbr, crowd, model_pw, book_pw=None, book_line=None, h_name=""):
    def bar(label, pa, k, thin):
        pa = pa * 100
        ph = 100 - pa
        return (f'<div class="wp" style="--k:{k}"><div class="wph"><span>{escape(a_abbr)} {pa:.0f}%</span><b>{escape(label)}</b><span>{ph:.0f}% {escape(h_abbr)}</span></div>'
                f'<div class="split{" thin" if thin else ""}"><i class="a" style="width:{pa:.1f}%"></i><i class="h" style="width:{ph:.1f}%"></i></div></div>')
    rows = []
    if book_pw is not None:
        rows.append(bar(f"Book ({fmt_line(h_name, book_line)})", 1 - book_pw, "var(--c-flags)", False))
    if crowd:
        rows.append(bar("Crowd (Polymarket)", crowd["pa"], "var(--c-line)", book_pw is not None))
    if model_pw is not None:
        rows.append(bar("Model (my estimate)", 1 - model_pw, "var(--c-outlook)", True))
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
    pw = None
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
        h2h_rows = []
        h2h_note = ""
    else:
        h2h_note = ""
    season_meet = sorted([g for g in season_games(a_id, season, 2) if g["opp_id"] == str(h_id) and g["date"] < today], key=lambda g: g["date"])
    if season_meet:
        tbl, aw_s = season_table(season_meet, a_abbr, h_abbr)
        n_s = len(season_meet)
        h2h_rows = [row("Series this season", f"{aw_s}-{n_s - aw_s}", f"{n_s - aw_s}-{aw_s}", "rivalry", "a" if aw_s > n_s - aw_s else "h" if n_s - aw_s > aw_s else ""), tbl]
    else:
        h2h_rows = ['<p class="calm">These teams have not met yet this regular season.</p>']
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
    # ---- prices: book spread vs. Polymarket vs. model, and projected total ----
    sp_g = _SPREADS.get(str(ev["id"]))
    book_line = sp_g.get("cur") if sp_g else None
    book_total = sp_g.get("total") if sp_g else None
    if book_line is None:
        _s = parse_spread(comp, a_abbr, h_abbr)
        if _s:
            book_line, book_total = _s["line"], _s["total"]
    book_pw = _ncdf(-book_line / BOOK_SD) if book_line is not None else None
    crowd = crowd_data(ev, away, home)
    prices = [(lab, v) for lab, v in (("book", book_pw), ("Polymarket", crowd["ph"] if crowd else None), ("model", pw)) if v is not None]
    if len(prices) >= 2 and max(v for _, v in prices) - min(v for _, v in prices) >= PRICE_GAP:
        tags.append(("market", f"Prices disagree on {h_abbr}: " + ", ".join(f"{lab} {v * 100:.0f}%" for lab, v in sorted(prices, key=lambda x: x[1]))))
    proj = None
    if use_ppp:
        proj_a, proj_h, proj = project_total(pa_, ph_)
        try:
            tl = float(book_total)
            if abs(proj - tl) >= TOTAL_GAP:
                tags.append(("market", f"Total: pace and efficiency project {proj:.0f} points, the line is {tl:g} ({'over' if proj > tl else 'under'} side)"))
        except (TypeError, ValueError):
            pass
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
            *([row("Projected score", f"{proj_a:.0f}", f"{proj_h:.0f}<small>est. from pace and efficiency</small>", "match"),
               row("Projected total vs. line", f"{proj:.1f}", (f"line {float(book_total):g}<small>{proj - float(book_total):+.1f}</small>" if book_total not in (None, "") else "no line yet"), "match")]
              if proj is not None else []),
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

    market_rows, bar = [], ""
    if crowd:
        pa, ph = crowd["pa"] * 100, crowd["ph"] * 100
        market_rows.append(row("Crowd win %", f"{pa:.0f}%", f"{ph:.0f}%", "line"))
        bar = (f'<div class="bar" style="--k:var(--c-line)"><span style="width:{pa:.1f}%"></span></div>')
        if crowd["money"]:
            (da, na), (dh, nh) = crowd["money"]
            market_rows.append(row("Crowd money", f"${da:,.0f}<small>{na} trades</small>", f"${dh:,.0f}<small>{nh} trades</small>", "line"))
    market = section("Market", "line", market_rows)
    wp_html = _safe(winprob_section, a_abbr, h_abbr, crowd, model_pw, book_pw, book_line, h_abbr)
    line = odds_line(comp)
    line_html = (f'<p class="note" style="--k:var(--c-line)"><b>Line</b> {escape(line)}</p>' if line else "")

    crew = fetch_officials(ev["id"])
    crew_html = f'<p class="note" style="--k:var(--c-ref)"><b>Officials</b> {escape(", ".join(crew))}</p>' if crew else ""
    notes = rivalry_notes(away["team"], home["team"], rivalries)
    src = ('<p class="src">Crowd data: Polymarket' + (" (thin market, treat as rough)" if crowd and crowd["thin"] else "") +
           f' &middot; updated {datetime.now(PT).strftime("%-I:%M %p PT")}</p>') if crowd else ""

    tip = tip_dt.strftime("%-I:%M %p PT")
    _OVERVIEW[idx] = {"a": a_abbr, "h": h_abbr, "tip": tip, "ts": int(tip_dt.timestamp()), "line": book_line, "total": book_total, "proj": proj,
                      "book_h": book_pw, "crowd_h": crowd["ph"] if crowd else None, "model_h": pw, "tags": list(tags),
                      "ab": ov_logo(away["team"]), "hb": ov_logo(home["team"]), "ac": away["team"].get("color"), "hc": home["team"].get("color")}
    return f"""<article class="page" data-i="{idx}" data-ev="{escape(str(ev["id"]), quote=True)}" data-set="t" data-label="Game {idx + 1} of {total}">
  <header class="match">
    <div class="teams"><h2 class="t a">{name_block(away, ai, "a")}{ov_logo(away["team"])}</h2><div class="mid"><span class="at">at</span><span class="tip">{tip}</span>{net_html(comp)}</div><h2 class="t h">{ov_logo(home["team"])}{name_block(home, hi, "h")}</h2></div>
  </header>
  <section class="grp flagsec" style="--k:var(--c-flags)"><h3>What stands out</h3>{flags_html}</section>
  {wp_html}
  {section("Rest and schedule", "rest", rest_rows)}
  {section("Form", "form", form_rows)}
  {section("Matchups This Season", "rivalry", h2h_rows)}
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
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Jost",system-ui,sans-serif;
padding:0 16px 48px}
main{max-width:680px;margin:0 auto}
.bar-top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);
padding:max(12px,env(safe-area-inset-top)) 0 12px;margin-bottom:14px;border-bottom:1px solid var(--line)}
h1{font:700 1.7rem/1.1 "Cormorant Garamond",sans-serif;margin:0}
.sub{color:var(--mute);margin:2px 0 10px;font-size:.95rem}
.pager{display:none;align-items:center;gap:10px}.js .pager{display:flex}
.pager button{font:600 1rem "Jost",sans-serif;color:var(--ink);background:var(--card);border:1.5px solid var(--line);
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
.net{color:var(--mute);font:600 .78rem/1.2 "Jost",sans-serif;text-align:center;letter-spacing:.02em}
.t{font:700 1.9rem/1.05 "Cormorant Garamond",sans-serif;margin:0;display:flex;align-items:center;gap:10px}
.t.a{justify-content:flex-end;text-align:right}.t.h{justify-content:flex-start;text-align:left}
.tn{display:block;min-width:0}.rec{display:block;font:600 .95rem/1.2 "Jost",system-ui,sans-serif;color:var(--mute);margin-top:3px}
.badge{position:relative;flex:none;width:3.1rem;height:3.1rem;border-radius:50%;overflow:hidden;background:var(--bc);color:var(--bf);display:grid;place-items:center;
font:700 .9rem/1 "Cormorant Garamond",sans-serif;letter-spacing:.02em;box-shadow:0 0 0 2px color-mix(in srgb,var(--ink) 35%,transparent)}
.badge img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;padding:2px}
.badge.ok{background:transparent;box-shadow:none;overflow:visible}.badge.ok .ab{display:none}
.v.hi{font-weight:700;color:var(--k)}.v.hi::after{content:"";display:block;height:2px;border-radius:2px;background:var(--k);margin-top:2px;width:100%}
.at{color:var(--mute);font-size:.95rem;text-align:center;line-height:1}
.grp{margin-top:22px}
.grp h3{font:600 1.05rem "Jost",sans-serif;color:var(--k);margin:0 0 6px;padding-left:10px;border-left:3px solid var(--k)}
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
.mt{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:.92rem}.mt th{color:var(--mute);font-weight:400;text-align:left;padding:4px 6px;font-size:.8rem}.mt td{padding:7px 6px;border-top:1px solid var(--line)}.mt td:last-child,.mt th:last-child{text-align:right}.mt b{font-weight:600}
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
.stack i{display:block;font:600 .72rem/18px "Jost",sans-serif;text-align:center;font-style:normal;overflow:hidden;white-space:nowrap}
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
.rail .d em{position:absolute;left:50%;transform:translateX(-50%);font:600 .72rem/1 "Jost",sans-serif;font-style:normal;white-space:nowrap}
.rail .d.a em{bottom:15px;color:var(--k)}.rail .d.h em{top:15px;color:var(--ink)}
.wp{margin:8px 0 12px}.wph{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:baseline;font-weight:600}
.wph span:first-child{text-align:right}.wph b{color:var(--k);font-size:.85rem}
.split{display:flex;height:12px;border-radius:99px;overflow:hidden;margin-top:5px;gap:2px}.split.thin{height:7px}
.split i{display:block}.split .a{background:var(--k)}.split .h{background:color-mix(in srgb,var(--k) 35%,var(--bg))}
.coach{display:block;color:var(--mute);font-size:.8rem;font-weight:400;line-height:1.25;margin-top:2px}.coach.tn2{font-size:.74rem;opacity:.85}
.fol{display:inline-block;margin-top:7px;font:600 .78rem/1 "Jost",sans-serif;color:var(--mute);background:transparent;border:1.5px solid var(--line);border-radius:99px;padding:5px 11px;cursor:pointer;white-space:nowrap}
.fol:hover{border-color:var(--ink);color:var(--ink)}.fol.on{color:#111;background:var(--glow);border-color:var(--glow)}
.fol:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.minehead{font:700 1.9rem/1.05 "Cormorant Garamond",sans-serif;margin:0;text-align:center}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}
.chip2{display:inline-flex;align-items:center;gap:6px;border:1.5px solid var(--glow);color:var(--glow);border-radius:99px;padding:3px 6px 3px 12px;font-weight:600}
.chip2 button{background:none;border:0;color:inherit;font-size:1.15rem;line-height:1;cursor:pointer;padding:0 5px}
.mg{margin-top:18px;padding:12px 14px 8px;border:1px solid color-mix(in srgb,var(--c-line) 40%,var(--line));border-radius:12px;background:color-mix(in srgb,var(--c-line) 5%,transparent)}
.mgh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px;color:var(--c-line)}.mgh b{font:700 1.3rem "Cormorant Garamond",sans-serif;color:var(--ink)}
.mnum{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}.mnum small{display:block;color:var(--mute);font-size:.78rem}.mnum b{font-weight:600}
.sg{display:block;width:100%;max-width:540px;margin:8px auto 0;height:auto}
.sg .gl{stroke:var(--line);stroke-width:1}.sg .op{stroke:var(--mute);stroke-width:1.2;stroke-dasharray:4 3}.sg .ln{fill:none;stroke:var(--c-line);stroke-width:2.2;stroke-linejoin:round}
.sg .pt{fill:var(--c-line)}.sg text{font:600 10px "Jost",sans-serif}.sg .ax{fill:var(--mute);font-weight:400}.sg .oplab{fill:var(--mute)}.sg .cur{fill:var(--c-line);font-size:11px}
.stale{margin:6px 0 0;padding:6px 10px;border:1px solid var(--c-injury);border-radius:8px;color:var(--c-injury);font-size:.85rem}
.topline{display:flex;justify-content:space-between;align-items:center;gap:10px}
#menu{font:600 .85rem "Jost",sans-serif;color:var(--mute);background:transparent;border:1.5px solid var(--line);border-radius:99px;padding:5px 12px;cursor:pointer;white-space:nowrap}
#menu:hover{color:var(--ink);border-color:var(--ink)}
html[data-mode="t"] .book[data-book="y"],html[data-mode="y"] .book[data-book="t"],html[data-mode="t"] .sub-y,html[data-mode="y"] .sub-t{display:none}
.scorerow{margin:2px 0 6px}.sc{font:800 2.6rem/1 "Jost",sans-serif;color:var(--mute)}.sc.a{text-align:right}.sc.h{text-align:left}.sc.win{color:var(--ink)}
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
.market{color:var(--c-line)}
.sortbar{display:flex;gap:8px;justify-content:center;margin:12px 0}
.srt{font:600 .85rem "Jost",sans-serif;color:var(--mute);background:transparent;border:1.5px solid var(--line);border-radius:99px;padding:6px 14px;cursor:pointer}.srt.on{color:#111;background:var(--glow);border-color:var(--glow)}
.ovlist{display:grid;gap:10px}
.ov{display:grid;gap:7px;text-align:left;width:100%;font:inherit;color:var(--ink);background:linear-gradient(180deg,color-mix(in srgb,var(--c-flags) 13%,#2a2d33),color-mix(in srgb,var(--c-flags) 5%,#1d1f24));border:1px solid color-mix(in srgb,var(--c-flags) 22%,transparent);border-top-color:color-mix(in srgb,var(--c-flags) 40%,transparent);border-radius:14px;padding:13px 15px;cursor:pointer;box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 5px 0 rgba(0,0,0,.45),0 10px 18px rgba(0,0,0,.4);transform:translateY(-2px);transition:transform .12s,box-shadow .12s}
.ov:hover{transform:translateY(-4px);box-shadow:0 1px 0 rgba(255,255,255,.1) inset,0 7px 0 rgba(0,0,0,.45),0 14px 24px rgba(0,0,0,.45)}
.ov:active{transform:translateY(3px);box-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 1px 0 rgba(0,0,0,.45),0 3px 6px rgba(0,0,0,.4)}
.ovh{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:8px}.ovt{display:flex;align-items:center;gap:8px;min-width:0}.ovt.r{justify-content:flex-end}.ovt b{font:700 1.3rem "Cormorant Garamond",sans-serif}.ovlogo{width:2.8rem;height:2.8rem;object-fit:contain;flex:none;filter:drop-shadow(0 2px 3px rgba(0,0,0,.5))}
.ovat{display:flex;flex-direction:column;align-items:center;color:var(--mute);font-size:.8rem;line-height:1.2}.ovat i{font-style:normal;white-space:nowrap}
.ovl{font-weight:600;text-align:center}.ovp{text-align:center;font-size:.88rem;font-weight:600;color:color-mix(in srgb,var(--fc) 70%,#fff)}.ovn{color:var(--mute);text-align:center}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:26px;font-size:.85rem;color:var(--mute)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;background:var(--k)}
.empty,.foot{color:var(--mute)}.foot{font-size:.88rem;margin-top:14px}
#gate{display:none}
html.gated,html.gated body{background:#000;overflow:hidden;height:100%}
html.gated #app{display:none}
html.gated #gate{display:flex;position:fixed;inset:0;z-index:50;background:#000;flex-direction:column;align-items:center;
justify-content:center;gap:clamp(40px,8vh,72px);padding:24px;text-align:center}
.gate-title{margin:0;font:400 clamp(4.2rem,22vw,12rem)/1.15 "Pinyon Script","Great Vibes","Snell Roundhand","Apple Chancery",cursive;letter-spacing:.01em;white-space:nowrap;color:#f3ecd9;
text-shadow:0 0 28px rgba(233,226,208,.14);padding:0 .08em}
.gate-rule{width:min(60vw,420px);height:1px;margin:-18px 0;background:linear-gradient(90deg,transparent,#b9a87a,transparent)}
.gbtns{display:flex;flex-direction:column;align-items:center;gap:22px}
.gbtn{font:600 1.05rem/1 "Cormorant Garamond","Playfair Display",Georgia,serif;letter-spacing:.32em;text-indent:.32em;white-space:nowrap;text-transform:uppercase;color:#efe6cf;
background:linear-gradient(180deg,#34363b 0%,#1c1d20 100%);border:1px solid #6d6246;border-top-color:#b9a87a;border-radius:10px;padding:20px 10px;cursor:pointer;width:min(86vw,380px);
box-shadow:0 1px 0 rgba(255,255,255,.14) inset,0 6px 0 #0b0b0c,0 14px 26px rgba(0,0,0,.7);transform:translateY(-3px);transition:transform .14s,box-shadow .14s,color .14s}
.gbtn:hover{color:#fff;transform:translateY(-6px);box-shadow:0 1px 0 rgba(255,255,255,.18) inset,0 9px 0 #0b0b0c,0 20px 34px rgba(0,0,0,.75)}
.gbtn:active{transform:translateY(3px);box-shadow:0 1px 0 rgba(255,255,255,.1) inset,0 1px 0 #0b0b0c,0 3px 8px rgba(0,0,0,.6)}
.gbtn:focus-visible{outline:2px solid #b9a87a;outline-offset:5px}
/* ---- upscale theme ---- */
:root{--bg:#18181a;--card:#222225;--ink:#f3ecd9;--mute:#b4ad9c;--line:#3d3b36;--glow:#c9b886}
body{letter-spacing:.005em}
h1,.t,.minehead,.grp h3,.ovt b,.mgh b{font-variant-numeric:lining-nums;font-family:"Cormorant Garamond",Georgia,serif;font-weight:700}
h1{font-size:2.3rem;letter-spacing:.03em}
.t{font-size:2.15rem}.minehead{font-size:2.4rem}.ovt b,.mgh b{font-size:1.55rem}
.grp h3{font-size:1.3rem;letter-spacing:.02em}
.lab{text-transform:uppercase;letter-spacing:.08em;font-size:.72rem}
.badge .ab{font-family:"Jost",sans-serif;font-weight:600;font-size:.72rem}
.page{border:1px solid #5e5640;border-top:3px solid var(--glow);border-radius:18px;
box-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 14px 34px rgba(0,0,0,.55)}
.pager button,#menu,.fol,.srt{background:linear-gradient(180deg,#34363b,#1e1f22);border:1px solid #5e5640;border-top-color:#b9a87a;border-radius:10px;color:#efe6cf;
letter-spacing:.04em;box-shadow:0 1px 0 rgba(255,255,255,.12) inset,0 4px 0 #0b0b0c,0 8px 14px rgba(0,0,0,.5);transform:translateY(-2px);transition:transform .12s,box-shadow .12s,color .12s}
.pager button:hover:not(:disabled),#menu:hover,.fol:hover,.srt:hover{color:#fff;border-color:#8a7d58;border-top-color:#d6c597;transform:translateY(-4px);box-shadow:0 1px 0 rgba(255,255,255,.16) inset,0 6px 0 #0b0b0c,0 12px 18px rgba(0,0,0,.55)}
.pager button:active:not(:disabled),#menu:active,.fol:active,.srt:active{transform:translateY(2px);box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 1px 0 #0b0b0c,0 2px 5px rgba(0,0,0,.5)}
.pager button:disabled{transform:none;box-shadow:none;opacity:.35}
.fol.on,.srt.on{background:linear-gradient(180deg,#eadfba,#b9a470);color:#1a1814;border-color:#eadfba}
.sortbar{display:flex;gap:10px;padding-bottom:8px}
.tip{background:linear-gradient(180deg,#f6efdc,#d9cfb2);color:#1a1814;box-shadow:0 3px 0 #0b0b0c,0 6px 10px rgba(0,0,0,.45);transform:translateY(-1px)}
.chip2{border-color:#b9a87a;color:#e6d8ae}
.ov{border-color:#5e5640;border-top-color:#b9a87a}
.t .ovlogo{width:3.6rem;height:3.6rem}
.a2wrap{display:flex;flex-direction:column;align-items:center;gap:10px;margin-top:26px;padding-bottom:6px}
.a2hs{display:inline-flex;align-items:center;gap:7px;font:600 .62rem/1 "Jost",sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#d9cfb4;background:linear-gradient(180deg,#34363b,#1e1f22);border:1px solid #5e5640;border-top-color:#b9a87a;border-radius:8px;padding:8px 11px;cursor:pointer;
box-shadow:0 1px 0 rgba(255,255,255,.12) inset,0 3px 0 #0b0b0c,0 6px 10px rgba(0,0,0,.5);transform:translateY(-1px);transition:transform .12s,box-shadow .12s}
.a2hs svg{flex:none}
.a2hs:hover{transform:translateY(-4px);color:#fff}.a2hs:active{transform:translateY(2px);box-shadow:0 1px 0 #0b0b0c}
.a2note{margin:0;max-width:340px;text-align:center;color:var(--mute);font-size:.88rem}
html.gated #gate .a2wrap{margin-top:0}
.fs{display:flex;flex-direction:column;align-items:center;margin-top:8px;line-height:1}
.fs[hidden]{display:none}
.fs em{font:600 .62rem/1 "Jost",sans-serif;letter-spacing:.28em;text-indent:.28em;text-transform:uppercase;color:var(--mute);font-style:normal;margin-bottom:3px}
.fs b{font:700 3.4rem/1 "Cormorant Garamond",Georgia,serif;font-variant-numeric:lining-nums}
.fs.w b{color:#f6efdc;text-shadow:0 0 22px rgba(233,226,208,.25)}.fs.l b{color:#8d877a}
@media (max-width:480px){.teams{grid-template-columns:minmax(0,1fr) 4.4rem minmax(0,1fr);gap:6px}.tip{font-size:.72rem;padding:2px 7px;white-space:nowrap}.net{font-size:.68rem}.t span{min-width:0}.t{font-size:1.18rem;gap:6px}.fol{font-size:.7rem;padding:4px 8px;margin-top:5px}.badge{font-size:.7rem}.row{gap:8px}.page{padding:18px 14px}.badge{width:2.4rem;height:2.4rem}.t .ovlogo{width:2.7rem;height:2.7rem}.fs b{font-size:2.8rem}.rec{font-size:.85rem}}
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
try{var app=document.getElementById('app'),day=app&&app.getAttribute('data-day'),
  nowET=new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York'}).format(new Date());
  if(day&&nowET>day){var bn=document.createElement('p');bn.className='stale';
   bn.textContent='These are the games for '+day+'. A newer slate is due; if it does not show up soon the update is delayed.';
   document.querySelector('.bar-top').appendChild(bn)}}catch(e){}
var hm=/^#([gy])(\\d+)$/.exec(location.hash);
if(hm)openBook(hm[1]==='g'?'t':'y',parseInt(hm[2],10)-1);else{root.setAttribute('data-mode','t');root.classList.add('gated')}
[].forEach.call(document.querySelectorAll('[data-go]'),function(b){b.onclick=function(){openBook(b.getAttribute('data-go'),0)}});
document.getElementById('menu').onclick=menu;
document.addEventListener('keydown',function(e){
  if(root.classList.contains('gated')){if(e.key==='Enter')openBook('t',0);return}
  if(e.key==='ArrowLeft')show(i-1,-1);else if(e.key==='ArrowRight')show(i+1,1)});
back.onclick=function(){show(i-1,-1)};fwd.onclick=function(){show(i+1,1)};
document.addEventListener('click',function(e){
  var s=e.target.closest&&e.target.closest('.srt');
  if(s){var list=document.querySelector('.ovlist'),k=s.getAttribute('data-sort');
    [].forEach.call(document.querySelectorAll('.srt'),function(b){b.classList.toggle('on',b===s)});
    [].slice.call(list.children).sort(function(a,b){return k==='int'?(b.getAttribute('data-int')-a.getAttribute('data-int'))||(a.getAttribute('data-ts')-b.getAttribute('data-ts')):a.getAttribute('data-ts')-b.getAttribute('data-ts')}).forEach(function(n){list.appendChild(n)});return}
  var g=e.target.closest&&e.target.closest('.ov');
  if(g&&mode==='t')show(parseInt(g.getAttribute('data-p'),10),1);
});
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
            score = None
            if state == "post":
                try:
                    score = {"a": int(float(side["away"].get("score"))), "h": int(float(side["home"].get("score")))}
                except (TypeError, ValueError):
                    score = None
            games.append({"id": eid, "score": score, "away": rec["away"], "home": rec["home"], "tip": rec["tip"], "state": state,
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
    return update_spreads(events, write=False)   # the 10-minute job owns those files; this build only reads them


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
function scores(){
  if(!data||!data.games)return;
  data.games.forEach(function(g){
    var pg=document.querySelector('.page[data-ev="'+g.id+'"]');if(!pg)return;
    var a=pg.querySelector('.fs[data-s="a"]'),h=pg.querySelector('.fs[data-s="h"]');if(!a||!h)return;
    var sc=g.score;
    if(g.state==='post'&&sc&&typeof sc.a==='number'&&typeof sc.h==='number'){
      [[a,sc.a,sc.h],[h,sc.h,sc.a]].forEach(function(x){x[0].innerHTML='<em>Final</em><b>'+x[1]+'</b>';x[0].className='fs '+(x[1]>x[2]?'w':'l');x[0].hidden=false});
    }else{a.hidden=true;h.hidden=true}
  });
}
function refresh(){
  if(!window.fetch)return;
  fetch('spreads.json?_='+Date.now(),{cache:'no-store'}).then(function(r){return r.ok?r.json():Promise.reject()})
   .then(function(d){if(d&&d.games){data=d;renderMine();scores()}}).catch(function(){});
}
load();sync();renderMine();scores();refresh();
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


# ---------------------------------------------------------------------------
# Prices (book spread vs. Polymarket vs. model), projected total, and the slate overview
# ---------------------------------------------------------------------------
BOOK_SD = 12.0        # points; turns a point spread into a win chance (a 3.5-point favorite is about 61%)
PRICE_GAP = 0.06      # flag when the three win chances for the home team are this far apart
TOTAL_GAP = 5.0       # flag when the projected total is this many points from the posted over/under
_SPREADS = {}         # event id -> spread record (from the 10-minute tracker)
_OVERVIEW = {}        # game index -> summary used by the overview page


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def project_total(pa_, ph_):
    """Points each team is expected to score from both teams' pace and efficiency. -> (away, home, total)"""
    poss = (pa_["pace"] + ph_["pace"]) / 2
    pts_a = poss * (pa_["off"] + ph_["def"]) / 2
    pts_h = poss * (ph_["off"] + pa_["def"]) / 2
    return pts_a, pts_h, pts_a + pts_h


def overview_page(events, payload):
    items = []
    for i, ev in enumerate(events):
        o = _OVERVIEW.get(i)
        if not o:
            try:
                comp = ev["competitions"][0]
                side = {c["homeAway"]: c for c in comp["competitors"]}
                tip_dt = parse_dt(ev["date"]).astimezone(PT)
                o = {"a": side["away"]["team"]["abbreviation"], "h": side["home"]["team"]["abbreviation"], "tip": tip_dt.strftime("%-I:%M %p PT"),
                     "ts": int(tip_dt.timestamp()), "line": None, "total": None, "proj": None, "book_h": None, "crowd_h": None, "model_h": None, "tags": [],
                     "ab": ov_logo(side["away"]["team"]), "hb": ov_logo(side["home"]["team"]), "ac": side["away"]["team"].get("color"), "hc": side["home"]["team"].get("color")}
            except Exception:
                continue
        items.append((i, o))
    if not items:
        return ""
    def score(o):
        return sum(2 if c in ("market", "injury") else 1 for c, _ in o["tags"])
    rows = []
    for i, o in items:
        line = fmt_line(o["h"], o["line"]) if o.get("line") is not None else "no line yet"
        bits = [line]
        if o.get("total") is not None:
            bits.append(f"O/U {float(o['total']):g}")
        if o.get("proj") is not None:
            bits.append(f"projects {o['proj']:.0f}")
        vals = [(lab, o.get(k)) for lab, k in (("book", "book_h"), ("Polymarket", "crowd_h"), ("model", "model_h")) if o.get(k) is not None]
        lead = next((v for _, v in vals), None)
        home_fav = lead is None or lead >= 0.5
        fav, fcol = (o["h"], o.get("hc")) if home_fav else (o["a"], o.get("ac"))
        fcol = (fcol or "").lstrip("#")
        if len(fcol) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in fcol):
            fcol = "9ca3af"
        pr = [f"{lab} {(v if home_fav else 1 - v) * 100:.0f}%" for lab, v in vals]
        n = len(o["tags"])
        rows.append(
            f'<button type="button" class="ov" data-p="{i + 1}" data-int="{score(o)}" data-ts="{o.get("ts", 0)}">'
            f'<span class="ovh"><span class="ovt">{o.get("ab", escape(o["a"]))}<b>{escape(o["a"])}</b></span><span class="ovat">at<i>{escape(o["tip"])}</i></span>'
            f'<span class="ovt r"><b>{escape(o["h"])}</b>{o.get("hb", "")}</span></span>'
            f'<span class="ovl">{escape(" · ".join(bits))}</span>'
            f'<span class="ovp" style="--fc:#{fcol}">{escape(fav)} win chance: {escape(" · ".join(pr)) if pr else "no price yet"}</span>'
            f'<small class="ovn">{n} flag{"s" if n != 1 else ""}</small></button>')
    return ('<article class="page" data-set="t" data-label="Overview"><header class="match"><h2 class="minehead">Tonight at a glance</h2>'
            '<p class="sub ctr">Tap a game to open its page</p></header>'
            '<div class="sortbar"><button type="button" class="srt on" data-sort="ts">By tip time</button><button type="button" class="srt" data-sort="int">Most flags first</button></div>'
            f'<div class="ovlist">{"".join(rows)}</div></article>')


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
    return (f'<article class="page" data-i="{idx}" data-set="t" data-label="Game {idx + 1}"><header class="match">'
            f'<div class="teams"><h2 class="t a"><span class="tn">{escape(away)}</span></h2>'
            f'<div class="mid"><span class="at">at</span><span class="tip">{escape(tip)}</span>{net}</div><h2 class="t h"><span class="tn">{escape(home)}</span></h2></div></header>'
            f'<p class="calm">Details for this game could not be loaded this hour. They should return on the next update.</p></article>')


# ---------------------------------------------------------------------------
# "Add to home screen": manifest, service worker and icons are written next to index.html on every build.
ICONS = {"icon-512.png": "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",
         "icon-192.png": "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAAwFBMVEXz7Nnf2ciajnA/PDYmJikkJCcjIyYiIiUhISQgICMgICEhHx0fHyIeHiEeHiAdHSAcHB8cHB4bGx4bGx0bGxwbGhgaGh0aGhwaGhsZGRwZGRsaGRgYGBsYGBoXFxoYGBcXFxkXFxcWFhkWFhgWFhcVFRgVFRcVFRYVFBQUFBYUFBUTExUTExQTEhMSEhQSEhMSERERERMRERIREBAQEBIQEBEQEBAQDxAPDxAPDw8PDhAODg8NDQ4MDA0LCw0LCwwzGqJhAAAf80lEQVR42u1d61pjt5I12oYmhrYxnTYxMQ4xBAdD7LEDZzgD7n7/t5q6SlWStnHncmZ+RPvz1wndgGqtuqkk1e583W7f8Hl7fXt9fcHn5fnl6XnztNmsNqv1crVcPiwfHu4f7hf393f43N3gA+P65vp6Cs/V9dUUnqtLfib4GU8uL8eT8SX/SZ+J/B08k6sJfhc+s2v8OTcz+qkw4DfMF/DbHh4eH5aPy8f1cr3arJ/Wz0/PTzAznOErzpVmvf267Wy3JAGMVxgvMJ5h4PzXa5g/DPhJD4sFzh8kuLuT2cP8ZVzJmMC4lDGmDzzjMf95GQdIAvPnbwEJaIAENEQCHIsHGCDBcrVarzfrzdPT0/OzSgAywMCpbzvMAOH/avDH70H8Hz3+87uEPkggKMJ8BPsJz/ai9RkrF/CwBFciAcjALBDPyMGCOFgKB5uMgzfmgBmw+AMDTwb/R8T/ATGZM/43qj2oP6g5NP+JIg+Yjy/w8YO/No5cEA80plNhIXJwRxwscg42xMFLxkHHWwDj/7wp8J8b/AmvGeE/nTD6E9R3QXk8gtmPxqOL+OB/j+nrY88DfDdaDyAhHNx4DrwdbJgBz0GnDf816/8D6b/Df0azV92H+cu4QOxhjPAzGpWfyIXhwdnCjUpw12IHz8rBa5Sgk+H/jPg/VfEX7xPRV91HH0PII8aji0/8jNyjX71gLoQH9UlT/GnT6xoHhR0YOyYJOoq/eiCLf9J/1J6IP2u/0f1xxB6xljmXA74amUg8sC1Mr5IlJA4yOyArMBygFXQy/J8U//WyDX/1/Fb3FXua+/no/BN94BnKh7+mXCQeoi2wP5qpBDfMwULtABhYr57Auydf+kYSdHwEAG/75PBn/Y/4M0xT8j2C/qVir7jTzGUM0we/Bn8jPIzUHsgWMC6ANVc4SHawjhy8KAWvJEAZAXbjb7RfdJ9mT/NnzIfn+pzRM6TPEP+GuRAmlAWOC2wJ1zONCLfeF62sFRgOOgn/ZAGrFeP/8FDDP/mesfj7kWIvqA9lnMWHBslkeBBrsJYw9REh2sEjqoTnwDEg+L+A/oAGKf6LOv5T9T1W91HHEXvGnGc9MB96hA/mAVlQWwAZ8GdOU1yOdhDzouUq90QvOPeOtwCPP1pA8v9J+y3+MH/We9YaxF3GIPvAQC6YBeAB/RLbAvkjiQnMwcz5IqQAOQAGIBhAOHhJHHSyHDRGgMcFWkAr/qr9hD5qPms84T04a30SD2gPiQWOzZgfYUSIdhBzU2DggaLB2kcDYMDloBvLwCLln4o/6/+E4+4Fe33RfdJ4whqe+qC/MzxEW4iWIHG58EUPwsGqtILO2+tu/O/vLP4cuxj/XPct9oNuk42uSqA8OFsYx8gc7WCWOHhgDtATLWNeKhJ0TAyreKCK/jP+0feQ9ljsD5smtIymORwMnD2QKbA/Em+U7CBlFRrPyAyynKij8y8ZSB7I6z96/6j9hD5qPs2/28Sp9j4O7HMcxQIujDUMSw6uNB4kDsgTLfNoQFbQKT0Q56AO/5h9TiYx9o7E94jfGcjkm+bD4GP/Yzn68ByJGE1XrIHjglgCyFBygFYw52hQtYJOzQI4Btfw59wn4a+6TxNrmiNEuw+fj6f5gzIxG0fyj5MtYGxGj+DtYOYkgKjkciLUGpy7ZcCsASz+szb82fecnRGqzXF/0B8o3Kf4OT1NH/kaMzEAjaLvge9Wf8R2EDmgmDyzEdlIYDnoqAW4GJzhP5vm+j+K+NNUmm6f0e0X2J/mXDAPfVa55lj9EXGQ+SITDdCOF48VK+jUYvCi8ECYPdfw7yKQ6CEH/b5if7prMA99lAB8LX03WQLFhJGNB5ECGw0kHhcMFB7I4X+ta9+k/+x9CMOjAelO0nsa35324GOfpE2nJAHJcIQ/oms4IDvQvMhFZMmJVpATgSddayyoMyA5qNZ/JP9P/p/wR/yaPqE/yNGn+X/3Xfr0TuEryoLYAoXnPomQccB5kfVEc82JluKJNBZ00jp4lwfK8Af978EvbjBy9a3u09zjw6NHEsjTO/W2gPEZf1JPOeB4gOsDyYqoZjfnnKhiBZ2XPWJwif8Qf6nkPKz9CXuacdvIWOhzjoQ/LeNgouvkzAo4r05W0GmLwbEGRCtg8kCS/8DvEfQZf/CLjH7P4H5cPPC1HsiW7OHjaeSAFKlJHPD6oPBEWKXAeGxjgWGgGoMN/lh5IPz75DvOWPst/j3VGp5zGt/RRzXK+SPh4KyLmGBiRN50zOtkrbWQBD6arWVdEBloiwEOf4wAaLzN0OAv2q/oH+ezZwkSE9YW1BthVAYSuomDCa+SCwlyK+jwOsCvAtAD3d5yDJ5qBFD84ff0hop/8j29pDu7hvKQLCFycNYDZPJoULECnxF10AcZC9iBP0XgQzTec8qdyfsY3+Ow/1B9lAvUI8vBgDlAcD5gbko5BfrSaWkFoCVpXUAMZDFgscsCkOZhBf8c/Q8fPuDHDp6/4aCXcQCD1NN5IkuBiwW6Luh4H1S3AI0AOP++4t8fOO8T0Re0aRzRo/MXFsQnkTcqOCA1ilZAnkjjcR4LnpADYWDd5oO8BTTgrRX/AdvvqcFfdScOnf+R4UFtQSTwHGBW1KAEMFqsIF8XdHbloZqFMgOfEBxcf7Xgr+ibueePMpE46DkOaH0ANA9jTjQpYkH0pGwFHe+DdsTgPno5XH9x1SH5/4T+cUT/qBxGl2ocDCwHoZ+sgDzprDUn7ezhgwj/Hs0/4c/Zp8FfdV/QpnEoj0rgOTh2diDRAHMiMOXjsY9mJMFc1vdcIxIGvA9qtQD4mb1P59ECavhH3Ynj0EtAUhhL8ByoJ8JlMqJVtQJdHUuZLmcgVSLyGPAhBFqDDZ0FJP9v0Xcz949okokJkQOMyGeRgxEEBI0F3grUD+GeDeZDndY8yEZh0P/RCNcAVH9AD9SGv2JfDqNJkQSNB7TGMZ4IciKwZLICWZslP0Q1ouSHOj4Pojz0NmcA8BjyGjjzQDX8BW0YXfOwBIfJFgo7ECs4k1rRMITvU0ZUROOYDyEDu6IwWQDoP68C1AIM/syAxT+B7uYvPERLMHagVhAlQA7ADsQKMJ+YzYpVQWTg3SjchCPMQs9NDrQLfzv3rnxUDmYB/2WKBz2RoJ/iMXBw8akbmgtZF0xVh5If0nyo05IHoQVIDGjAI2ANmmKAeKAUgVP0teh3y6E8JDuIFGhW5KxgNAIJxs4PpWAMfkjWZZ22PGimFgChHdZhhQVgBOhFBpL3SdhXJGjhIFrBx8QArgvgV48LP0T50JL9kGXA+yBcCXAU7sH8R94C+jUPlPDvto+aHTgr0KxUVsdN6HE09n5I8iFYl0UGfB70y4zzILSAECgPdQzkFmD0vwV9y8GhckC2gz8Df5aNBVi1xn3M8UUIhR/SlSXr0FoYaF0JNOFDrES0xIC98U8kmIimeR3bcbICrF5ejA9DE9c1uRGwH0oMSDXC+yD0BImBNh+0H/7Wlg0HlViQrAA9COdDs5ZI0JE4XGVgMgQrgpUA+6C6BXj/391jJA5arUD9EORDTRiKH4JIcFuujDsxDnsGeCWAsYRrQe9YgOJfjiYc0Oh0+E/e4DjKJNBYkPuh8cUPoYnrMtm/nzMDS2WgZS0AYaAbuhCH8WRDuw+yFlCZfKc+UJCmRgEbwTD6oTFocTdGgmjFaU3QaVkLEAMg/MXlXj6owkD77OMIhRX02Q9RNqEVoiaYSHB7l3bMmIHaWkCiAKgfrsV8FCh9UNUCmndnjwKUFKgfOkc/xNWJfmiKNQFlQ8ul2oDfFfhFfND3QADXQ9t8kGZBBf5N6Owzwg4/hDuIXGWcNOH7VB+asw0smIG1MlBbCzTB1OOEgf0sIFOeg7hRfHCQMXDUYgSwLqN9MzSCS1BlsyaYp5U9Lso6lUxoRgyA8cR6kDAQ86DdFuDgh/2zVstojpIElUhANTpcEzTh8KpSI6W9GrWBxIBGgRCuLAO8FtuHgdA+/Uy/2JMKBdVIQPWhyWUI02mRDXF9zjKAq7EYh3vhiFZjzEBaC5QMtON/0NRDmVj4gUZjyYd6eSzGU11UmzgKH3jfOK2LdZ8AGSjWw8hAE2JFbo84nBhoDPytwTiIDWssc1ZsGAArHtPGcWhkzzKti2WfgBlwuSgxcAKGc2lsYEcU8AwkFT/ovi+Ay4dqsZjO1GFOOfTrYq2QZl5oHuMwRo+0K0D1iDoDmQWEd/Un0RR8PtRjIyiyIVoXIwWVCqnzQjYXvcTgYRhgG/BeqOaDTPwK3fcEaCwDvbQmMLUJsYEJBNUrZwP3WhtqsQGIAVwRuvC5aH0tkBgI+yiQCHBQ5nPRiqlAFxmAfBoQzWzg0dhAWo+pF4J/bhhoz4RyBvYjQATI1wS9ej7KlYkm7GEDujsPQax3ZWygLRctfZAJUu8KEI5KGzhts4FpL3RNjTrtEtRtADTOMHBONjDYwwZMDOi+L0CxKjPr4mQDF1KjBqVosYGsLooMkAZpHHjHBiwDe5oAC9DssgHDwJgLpE243sMG7qkuCunf1X42kMWBfQUgVWv2sYELtgHIRmF5u3ccCCHVRb8pDhzsKwAexDw8+oY4ALkQCrAzDjxEG0ANSnszbXGg4oX2FkBqdIYB3S9riQNX6NqtDSxNHMjXA0344fra78/nddGWXGhvAbplZUL3Caq5EArwA+nQHrnQXRPy/eF91wMHe7pRz4BfDxS5kNTmrm9AgHw9sK6sB+5uKPXjyug3rgcOOvsFMsfA++uBCZ9iJB1y+2SVqgR4oR9C1+zOtNXl3mPgYDf+XfwerVG31ebiyRuuLUJ0Yhsw54Zqa+Im/Cw7lN+8JnaLsX006MNea2LZJ/sZdShbE28qa2LMOrg27RkYOgZ6NQasADvS6dIH2bqQMmBPDnFdKDRxh6BWF1oIAyDnzO7Rv1cXMhw0nT0laK2OxrpQdn6O6kIALZ9ZMbtkXBvVs3LIABJ1nZ2TqzBQt4KDvSRwFfZjux7rxzMTKRdNezRNuL2517pQa220izmHVKc5Eox0j/jdSHDoC1otEuwqT/f1zEesjeouGaxnwL3Y2mjcH/CVOXRWXJubmHx0WMtHPQNHuQ61WHK7BbSvBq55hwOMoG1/AP3QgpIhFMAzEHdoLAO9nmEgcZDXFEsSDr9tfyAxwALI/sBjuT+gfihUGIh+yO1RHlc4aMoCaFPD/7C2T9nP6nJjjsN0jpru3YdgzgwZBjgSyHlFSvr8WaH9/dDhYVnWPQjt+Fcru1Kbxj0ytWGpyzUhy0XjHpmJBCiAP6vyLbuUNQmiCNlOsZu/9UHnZ8X5Ua6MNsGcV6Fzc+U+8Q1YejqrUmPAWkHGAUlQ2xkQEWp79bssQM+r6O5AFwIBZ0LkRddpnzjlo0eUdTMH6bzc6Hz3WQl7UqW+t3EQ7JkVxX/3HuWYz82l/ZmfQy/dyVot5bSKPyvRDcVpIVkXIwdyXvFj6149ctCt784cNHGbPjsroTGgn60FJJPTPUoIw6FbnJV4Xts9mgWqWYWBqhX04mnRjIPDlg2auDt5ZE8vmhhADJxndVG5y4H3CMDD+B0yObFlGWhC7cxiixWcVs8LoTdtISFk+NdOSgzj/phZC0hdFAXw56flzFy6OWAZyO4NtJ7YshwIC93Qpkfp7OKxPSfRr0XhsZ4z0LooM/CYndhyZ+aQgfq53dYzc5kdCAvd0EZCBX+x4MwHXRZn5kAAe2buKTKQ/FAT7HmJdivoZ6fW9dSTObfYokfBnBw9dVmQnt01NbnstFBoFmY9jHeZinOjloFKPpROThfn5ionR+t6FDL8fR6t+EcC1AfBHvccGUh7xPHstL0/4xkwtYnWWGA5ODbnpnHUN4zDcY6/Py9nLEDzoGteDYcm+aB1Ojttzy3mDKT7S1QjPc9uD1TswGpSiwiB4bd3CNQF2ZpoOnmsNTlgID87bW/QKAN4clR2i9usYFCenv7O3h0wZ9ZrIjR6l0bx1yyO1sIpBuTndpEBf4sm3eAQPwQCVM6veysw0aByf6O4u/GhtOaDbol/mQVlJ6fnyEDl/oD6IV4TIAMtd5jiHcoaB6ff5fdn0i2U4w+FNYeEf8yiUwyOeWimQSBA7DHB9ynLOzTIQNypqd1iMp7InGHvWW90bCXgTx4VDhpzj2ngYvAn64KmZn94zgJoHrSWW0zZPb5uiHfpZ/kdDnuL290icN6o5R5TRkJwN2jOnAdqswAQoOvvb1Ru8nXD7X2bFeziwLDQcpPMW8JB5Q7TMK8G5SdGfwIBHot7ZJ6Dz5ixVqwg90SegxiVs3uU8RYiPl6CxvkfwZ/qofb+jMYArgd1w09Ld4fJMSDrMkr4CgaiFUhOlDgYuLus8S7ld5kEOBpvxnK/nu/xDRP+F+ken1rwLVcjmhD3BTb5XcpoBRit9RbBLJ5ftLf5zlMvCblNfGrvU7bcZ8VNJFf8reGfx+DsBhMI4O/xyY1u19cshPfvs547O9D73Kf5ndb8LrGjIOid7thhQvCnGKzrgJm7uxFC0VtC79Snu2TgR3fd6B6PRs4O5E5lulXp7nT35KP36g0FYWDxPzP4X/AdvqnpLSH3BsCLVm50Z3e6gSa90brrTrflYBB7edgGBqkvwOmp3uQ2FBz0B9b/n9u7xLqO8TdZFyBAajMEUezFMJCsAAJB253iWlePs+Jefbwd3Yv36Ht6B/rUCKAdezL9lwgwzfsM4VoYvGjKQ11fiefEwAKDRa2zzaTKAbFA/Rk+vtdZAv2mOQ0it4iHyf+09zTgfTHAtuiu0in7+wFPi3SfqXqvWOyAvRHMIohOUweYsq9K6uphBTgz+Q+fjxtrLTG/jS57Ak3I+y763irCATrbeK+7nYOR6W1zIIiKBKa/ivnQsAyI9zH6n/B37W30xDqawMrEAGEgt4ImPC5q3VWcHYxSdyFg4YARHfS1n1DfcvERMx7KeT5+NF7oLHUYMvov+PsIIPXQ0Kzyvpe2v5D2+LtFI4hWcFv0V5H+WtzfiTqbNarTg5gh9Q0Xfe5pgx1MjBdqpMcTdzvzfbam0+Iu/eKBTKDsL1T22FqCty07PCUOLsfaX046VJ01UaclsvUHH4sHv2oEODhzfW1q+F9LTw/tqwImsCr6nHUqXebgH7b0eEp2IHkR10yDqMSZ62/WNx8dKZCFYdbn7NLr/7Xtwcv1XNIgvUsfm4R5BigeQyxO+8bGEzk7sF3mWIDYact3auvbLm3GhvNOcwX+eV8biAJFh62sz5xyANZedBkyfZ5iXnQhfeYOBNKzofb4q/aagz5c5kha7PXH3ifh7/R/nm5Poga195lzVoA65G/2/VL6ojHpEXLAriVwp7/zHb3+huZEV9ZjjvOfTP9TbyfMokNje6pEBvJei1ir7oZfa73+ZqbXqO3zKudwOTZLj0vTb/Es9pUzK5om9Zfz+Lueown/R3SOCf/Ut7ZT7beLEQO1ruy2GDkwliAnoUfaazT1uzQPfs0QcD6KnV9Tf7lpqf9zwZ80qLSASsdXsgL4x1KtbuVgMomWEKTUE3u9Dk3P0WHqNWqDgDZ95b5mXIO7sn13b/M+i6BB0uvS9xut9Jxebbrh86qFg7Q+iD1HgxarRtp70fd9pf/+ZOcfLPqX0gc89Z++s/rD+D/CWl37f7vOzY4BXJsxB6GxPUfv733P49h1juOyavbBKPY6Fi609y7+ac+FSw/q1Pc49nOS3nKu1yjVQpvA+wFm/tw0mBnI4zHkQyBBveuo1EyVhfGx2bzQ3rv8fIrP6NyUhUI/9jx2/XYZ/9kvUf9Nj0UorNf7rxMDRd/jzSNGPdt31/ZdNz3v0RIaV3Cj+Jx6T/Nn5E7T9bXv9KXGLs6e+eZ80X+d+u0iAabnses77d498CQcYNR4rHHwi+l9zL2nsyM2oZEuyMwGflx9Otj+69H3a7/jmdN/ORsHGQSYMNRCs367ee/15Ime1rdoBZEDZwex+/eUWQjlNhj0R9U+4NAS5MBR5Hp+T6TPbqw/2Pgr+GMdAo46Ef5Pzx5/w0CKBsxBE7TvXOJgXum/PsXbFfX7nnKL1VWzincQGO23byDgt0DomYilWoDiH3vHl+8f0GhwErrrGgfeEoSF7l5XDw+oS4H0vde3D4jvnxXvgJC+69TZrxu+Z/xd33V+/4B5A8SL6z8dAlcpHts4cLbQfffu6kE4dtir7vt3D0jPdaf/y00Isee3x18Y2Bbv4AAOTjD0JQ4WlgMTl6/lDSJ4SeSg9fpwwIullxQ3ri71pR2s+8XbH6z/5zpcE05Mn93YNx4n/gUYqHHAVvC4yuyA12j+PRxTZQEjQ5Nf+MQ73M3hZcRdsJ9G9GeV92/E/IfwX0MMKHuuv+lbUL5+VQZyDn6l7GO5yjiYW0uYpXehTCOwV7Fn8+RSngm/uyVCH99/EnXfev+Y/3AVqAmLp+r7N4CAr8TAF8eBrA2enuD6HNcpTBfMuX0XTW4L1zSzK34vxyTirZ/JVN974t59oujH+oPDf7MGXYYMrY4/MfDlSxsHYDuRAxeV5+59NNEarkWC1jHF9dZUsYe4OxP8b7X+xtmD4k/9dUOo4M8EfP0KDHxVKyg4gKS0y7Ui04lUWZi7NwJ5e7B82D9V76/jdwj6qv3pHUCq/2uYxIdM/18T/izAV++JDAdNmCMH3I3aWsJc/NGtsOB54DccTeOMr64j7lHzZ173OfZy9uzwh5Sggv+b4C8CVDjgiDwH+pCDzBtpV/bSFvgNO7g7i/okc7+6vlbcZwZ7r/uiPbAPb989s1mH8KvBP71/hvFXAb6aNzI5O4DWJE/CwYry6wfzXqZ5tAXDw0wkKMZM37k00398eydjfj9PsVd6rK+4vzS0JmnD/4th4EuMBrkdQAyJHNBePtdb0ruxuAJspLh+55lF7BP6nPlz7qPaT+/AOgIAa/hvBf/IQIsdAAsBnDBXi6QvO1vCA6us2oLnQcfM4m2Han5895X4ft6DZO+P+P8GKpyvABz+SYAYDXJfBEZEazR5N8qjsHBvWbi/Iz94k56bxMeNYn5zY/8F6p/VfZo97UGuqL86vYMshPlu/A0DeUSOdoBmgBxsjDd6tLYwNzzcJi7uStQN8gl70n08g4K+B3Mfxh/rD2AALfof528ESBHZ50VgBg1VTZkDjQnJFhIPc/GsOx72mKr57PdJ9532c/0BfvF7+DsGnC96NesDaNPGNTt8R5mwkGxhoUAmHt4ZbLXW76vnT9oP+AP17+LvBdiqBFluCqr4efPsWFg+8MO24HlgLu5ppnfECv/3PP39PWK/cLov77yivur0zqWTEFL+34Z/xoC1A7tG22A0EQ6SP9L8aBF9Eo87zpbu48zj7O8T8przZLof3/0GziOI99mFfy5AxReRDI8hbJ6UA2HhUT2SWoP4pblmrMVD8Upwl5zzUdBPvoe0Hx74jXb924J/wYCNB7ZW8WMIWFN62sipCo5raAvqk8QeFve7x0JWK4r+Mum+aj/lniH8lF561Y5/KUCVA5ABNVIkwDcmrpdiC56HyASz4T8Rd9SdhVoRYR8j75PUfsDqXmz9oQ3/CgPWF1lv9FMIt1y3y22BfdLjo8KK+NI8F/6zkL8zek/YW93n2sNPgv+rWm/M/4v5VwTY+tw0WsICGoA+cf3a2oLlwXHBfKSPZGqi9ZRxCvpR96X2+TmE316s9+H8/0tt/jUGSjtgS/hXCCdSsUgsGB6iRUQussF/tyywd+g/v0B73H8l7U/r3+r8qwJsU0RgDjQ3wtBO7+0TWzA+iXigGB25YD6WCfMc+YQ96/7mBTNPiF+vzvvY9deeDCgHNjt9ZQkaXS8nWzD2YLhgvUrPUnH32GfoP/OveC28Twv+rQIYDpwlQN3nZ67dkS0wC8YemIvIR3ywsqF/E7FX9Dex7o/m2xjtt96/Pv9WAUSCwhLAwLq6Yk48CBMrtYkohXzis+J/47GXt01C3IJS9o+Z71f820arAM4bmbfnvgLH/36WNXPySWoPzEXkwzwrxT3pfcSeV13/QvUpY+8u/HcJkHmjZAtdjjLKgvJAukS4EhflZ51wT9gT+Hz253sk1/qeiP+XHXPcJcA2z0+Fhd9whfAiLEQexCLEKvhZbVjTV/oVxZ3ynSfVfJw/LN+bVe57Wr3/ngx8pbzCxGW1BUgsmhfdTTD2wM+G7WKzWZtHhuBOPidiT/4tnCjFe2r/XgKUHAgL8AsPtX70HP2ScEF2UX747/jfPdH36LkxfLNHQl+1/+3LTu3fT4AsJiRbgG2r5seXl8SDtQnhw38U9wx7SnWb32u6/+7s9xJg6zJUE5tPAlmd1F8SE2QV6dnY/3tRj/Mc3/WM76Q6iX4/or/dA/09GTAxIbMFeh/Wf724QXOrjzTvOH6jd2E59DXybrf7TG0/AWxMsCy8vZ7gO5RO4srNctH2vMSV4ot8d/xpyffsi/++AnD1sWoLt3Rq9yRKkPNRHfhvP9M3/lrV/T3R/yYBfFTYGhZe+QWBn3X19vrSNuv0/Mjf8z8F9qr7279eAOuPIgtiD7/zfJrV6x4S/MYvp2z+nWl+Qn/7DXP6FgG2OQuWh/+WaXX/5/Wl/fl3V0T9vYL9N+n+H2KgsIWt4QGY+FVf/Nk0J7+/uvH7SRP/8lee92uiUdH/sv3W6Xy7AJGHN8ODYeKzf7es+z+Q4fPb607st3+/ACYu5PZgJDnJ3+578vZqMY/IR83/A+j/YQGUhZyH7ZvoRtvzprhXsd/+5wTQTPVL4sHbxI6xdR7nD/idv0wAw4PzTJ6T1mf7Z7H/8wK4+GbHO7NOWv9l+2d//Z8WYMs7nJyxfjX6VJm7jVR/AfZ/EQMaHShOF1xUxpc/p/N/kwDWIvZ6/h8K4Pj48nfO+e8XgPj4GufNdvL175Hh7xLgPzb+EeAfAf4R4B8B/m/H/wJM369uip/o3gAAAABJRU5ErkJggg==",
         "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAMAAAAKE/YAAAAAwFBMVEXz7Nnd18aUiWw/PDYmJiokJCcjIyYiIiUhISQgICMgICEgHx0fHyIeHiEeHiAdHSAcHB8cHB4bGx4bGx0bGxwbGhkaGh0aGhwZGRwZGRsaGRgYGBsYGBoXFxoYFxYXFxkXFxgWFhkWFhgWFhcVFRgVFRcVFRYVFBQUFBYUFBUTExUTExQTEhMSEhQSEhMSERIRERMRERIREBEQEBIQEBEQEBAQDxAPDxEPDxAPDw8PDg8ODg8NDQ4MDA0LCw0LCwymAq/jAAAdEUlEQVR42u2dfVdaSbbGsQ6aBBMQ7aBGiY22tEjjwMBcuNdovv+3mv1atXdVHV+m586aP7rOOivRGNn+eM5Tu962nZ9PTz+efvx4xOtx97jdbbbrzWq9XC+Xi+XiYb64n9/fz+7v7u/ubu9ub6/x+u36+ur6anJ1Nb66vLocX44vLs/5uoDr/CzeZ/gnX/TvF5cX48sruiZX+D2u6fvd3k5v76Z38Br3s/n94v5h8bBYPqyW69Vmvd3sNhDX7hEjhEifnn4+dZ6g/aD2CG232243m/V6tVouHx4Wi/liDjHfQ8QQMzV4DWhX0i6lcbTczvA6O+M/6WNq8BX4s1GDyKlh1NfXN/SNMerZHb7YfL5YPDwsl8vVerXebDfb7W6HwVGUGG8nkVbO2zVyXj1EzkBgNk2cidAV8xpfRsIY4Wm6zuSWS5gLb6bNUU+INkZ9m1gv4F02rLfI+kdkHUnXOC9UG8L5higz5zFFfHl5IYwp5jO4yoafPTuLxC8waqA9FtrUNOo7x3q1Wq83yHqXWCNpy/lxW3Ceec5MeUKcUaFI7lwYU2yjs9EpXidy03Wm3A3vMWpkfJWUfUNRT/H1oq6XjjU9dxhvp8p59fBAinachfIkaRliVsanzJhihHaSbvwYPqvMz0Tf9A1YI5Ma64VnvYvPIrDu5IpG5/B6RsqJ80TULFpWxsr3ZKTXsdxw4eeJ+Ogs6jtqe6zPIyobWc+ihyxrrJF04rzbbaCtVtE5kPMs48xqZi0TZVUx8z2hdhyv2Ji5p+18RFhP23RtHARJv8zZ61k8Q7VMzhAZH4840iHdekn8SvxMPEW0fcE+4lknXS8XQHq5WW/WOxN1J3HOHNr5c1LzmDxDtHwWGStfaEO8hkP+kz7G5nhntBPra6trtC+ya2Jtou4IZ9AGqEM5g9ctKnpOahYto1ckxhzlUXHR55k58SY/IW0T67GynqSoxa/nD7lbk6o7LT1hwXkSXUM8QyijjiNhaEd4pcYfD4fCm4ALbXJuYT2u6XpOrPFZhI5xQ2a9I9Qdw9krGrxudpdzZjUzZ1QyOoRlPBxAxIPh4Che+HeMm5kz76htiJs9G1lPnEKQNbzfc9cziu8J6S20NTRQ0JJ6wlzPUc3sGk7LSBkZQxvgPRj4OzIn3ugpo0RbPTuxvkl5SIuDdFqc4wXO5HNJy5FxoutbZM68iTZrm5VNLqKsva41C8lU3SkUXeFs9Hxh1EyUUcnMWKLtNlnrxtiVN2k7saZ8JNM1qXpWsGZVC2nNOTCHts6Rc77AHDlyPlEdU8QQbWhtELvhDbSxt0QfUdZO15KpYtQ1B+nEvtA5B/6gU8M56Zk5k2eQltErBkfdRkJrPgy+9OH6ond/8OVDIz8OQCfaSdvk2cRadT1xrCUJWT4sUw6CQVcU3c6Z3flUPYO13HC8gy98QaSfv8iN8cvn+xx5Mzgi1sek7FNWttG15HykaslCZBxD/SJF3fGKbnOOjLOoGSk3HPCgP+gjW2qf6eK4pUH08BUDCrxh2hXWY8sa9HE3qzpIR/vCjXcOy3kcOZ9FzkPkPCRVNBjNF1HF57aLmFPcrBPyEfbsyHpcsGYHkRwkqrpT6wsxt8sduuA8PJKIkXJkXG/KHOLGL8f/2IWnQV3Esi5Ri6y5XxTSXtEtzkH+nPSMaoZXbj4eDZSyEqX2KV49uDhqipzVPRh8xP9dsr7IWc/yHITG5pF0vS9U51DOI+WML9obMOV+zviTuzLe/T7Zdg+/AynbsKbRTO7WpVcj6ejRUdGQQ0+Noi/FOSLnLuqC+hKkrIx7KdJPcvdc7Ebb4Nioku6Rska/lvx6jK9rcmvuF41Xd9Cjk6LbnEPzDeIMr9ZwjqGUhXMPI621xJu1PehzHwnfqB9ZS843lj4mPovUw2DUOA9Cqu5YRZd94SQ5R+RMgAaqZk8Z2sfi4qgTbfZu7kXhLRO/xjyk6tZO1UzaenTNOSxnzDe65LOcTljHUKgUZ2oateWdlH1EPt+1rHG0O85Vfe9UjaTBo1fRo19wDsw3Thp8/oaOc6T8USMuora8k4/0KRfpAQXOQ3gs46Kecr/IXh1V3dE82mYdLc4xOhniE3+keo6Ue58rjD9+sB8o7sRadH1Efj88GcWok4PcMGrj1ewfnbaso8YZpPGBM2dxOuScUYZYyyvqxCpbdT08+gASGSW3bvdqJV1mHdNprmjh3KCxlpxFy5Hvh7wpc9ZIr5frejjEb21VjTnIpOrVNI8qpFMejR6dFG2dA2NOnAeRc1SGcKV2oPdHjtrTTqxJ19S/NsAa+hjNQSbs1TcxA4n+sVl3aE6pomginRQ9GjXwlB/nnHs9iTljfGBu5W2kHaOOrKG/YtaSg6R+UUYDLGpWdWdr8rvSo1nRwBkewYOhjgQdZ0OZoqxfQjuy7qWocRwGWch+CL/EHMSpejqLo0X2j47J72JvmHk0KBp8qUeZHXHGzNm4BmtZCB+UTXmLRhJr9JCBRn0Mr9F3qjZePXP+0UneoVmH92hS9AGY0jGOU17kLFQP9t3FUR9I1B+crmMeQuPGPpjTiPrF6NUpr57LHAg8iUq65h3k0dQXQsyQc7zGWSljnPv7+3wb3slHyESEdVI19IugQevVYiBpvkn8o5O8w49YkqLPD0PgHDo6dME5xhwJc0u8M2Vr36huDe8i5iCokNyrC/9YIemad0ySRw+Jc1K0OkfGWSjv15rVdkQtrPsZ6xNVdfSP6VRnUSX/6KS8w3jHBEiron+hThbGhI5zD3PlyNlpuRJz0nbBWjM+UvXxCHR9rl49Tv6h+mD/6Pi8I+sN0aNDOByNcI5DcjujaPFn4dxC2dL2rJ2DCOkReMgZjWEwr6ZZMla1zT86Nu/IvIN6wwaeaK9o6xyes7SuuxxtZY3/V91a+sUjmQk5CA2NYXL/mIl/IGk/fze9td4BPzH0g6ejlxRd49xNLUXdzlpVDV6NeTX0jTbXK/IPJp3m7yLpieR38A1Odfxd82glHWMWvi5qS1tZewdRr8bx4ugUQOm4i/KP6zTq4rVFJv1AYxaXdxDpIcaM4xWcgLYeXXAuGduWs45R9wqvHlHUv3j/4Ccxjl+YtIzCU94hvWEIOjJ8UdGWczVmoX1QsiZ9OP/A0SJYCM2BpPzDOXVntYyjw0ha844mfKRM2mQdSdHen1/iXLAuVW39Y3RKD6PLP6Y8fkmkl4b09MbkHfhEAGnjHV8G4tHv4WyfyLqq+5pXQ9Q8gkFZxyeR/EPWYIT0qpzvkEwafZ7mOnLv6FUVnYfJc9J72HRmvVugjnmT5noyLg/hMoqa/UPmP4h01h/epDFLg3lA9A4m3W9XtOPcQLCdSsP4G8O69A8ZK/ZCc8njl+TU3CciaZtLm/5wfHmIdineIaRL76hzDtWATehNRN3r5b2ijBWbcKiZHpOeSU5Nmm7pD8cgjos4q9TuHSXn0Hmt7b3gH7juReOXEHj8wp5H+qB1RdBHJ5/DY9L4FHbPE2keGgrp3kuKfo0yBU36SP7xJfMPmkOFAFxOTWMunNODoM2s9NTm0qCpc52/gzGLI92rKFq07ELeo1WthtaJ7D8EeRSTfxRODTl1E2JO7eb0Vom0n8MbQ6cU5ztOsrzDk7agbcx7ITeTvRT0AYva5x/GqTGnPgmNeN6Nyz7UPfJ1lvEVPIU632FcWki3KdrEvNdUvFrDRv+QXC/lH9ASaZr/aEIkPXXjxE6Z4+HybBOGcWbJunRb3sFBmZjrPYz8VM2B1QeLmuyDcqYT3ICDoxfIfMZq1NNIGp7ETpqXTu7Bir7UOWnfH7Z7R3gtZvmSPdMr+j5RScuYHFRdGydWNT25IpMU0pTi1Ul770jiaI05Bm06RaPpwVHMPmii6RBVLZ6X5qmje/gcb4LdEWv6JNd0yjzy3jCBbl4OOhy09YmgD6Ppc4Anmr6Nmn5gTcucxyxlHr3QM7OlUdNlLm1JN8nRuq8GbUmnnFo1TXN6OA7ohQ+vaTqSRoeMs6V5jpc07Um/BTR/UWM13XPZh9f0GJ6tNk0vck2jOi5l/t/neIZ03h++QdESdLdF04Nc05dj1IfRtIwShbTTdDfEWbzTNk0X/eGb1EFB7+2/VdPjy19D90VN30dNh+BWAF7waUM6vCvogrQkH4DaavpqDPqgtZfpXcpNqz4N6qCx+Lt8+u1Bh/3Sp9s0Tf7xFp9GS3drLXbc0urTbwuaNi6Ywcundp+W8Tihbs89VNMhXI+Ne7TnHgfvJZ2PEz9ZTZe5B+XTEM1bcg/shtIagJ3H62ekbe7xJtJ2RM7qsOPxgcunL3hk2wQ7R92WewzD0K22UD5tRi71sfhbEo+cdDked/k0k+6FYb5GbkYuqmn40XQmT/Pp42Ierxy5mEFW8zrpYuZjYPLpU1rkEtIg6mw2rzZGDCGuIJ6/Mkask25HrbNMHzifNjNj/TRy4TFi1DSo1a+7VMeI2HPitIfsPYhjRDcaf5F0G+rqHJNNp5H0CT2HurIPQTfh1o4RK/Me0+tfwtCQPh/5OdN+G+n9xk0RvGOOyay8MOlsjbwXfjGadvMevOIC9oFPK8/l4Vrta/MehvXe61FnpM24JZJOOz94hnqCblbOe5i9S0gan0O3hjh64wxT6LwY9X5VHck7uD/EeQ8mndYCMGjei5DN5aW1rZw07VuqrQPkpBs/txHepOi2/vBMN+kx6anO5fH6eEb67ha+ZOJJ1/rEGuuQz33VOLfNmg7sCPGMdjPp+hY+iX5/TUfWa+PuJezri50eqU98QdVNPpEUmmJ++iAj3cu8w4zF404ECHp651YS3ZoLOnXoppUAN8ekay4tpA8K1Gm+Zt87x4Fd33JzpkNac5GVZl0J6Ia4P6+y5oKkh8Hu9Uh94iv+cVA+ixq2Wd86qCs6W3PhnQhpdevXMLRj8RWTttlHN7hdNefFDHW7qg+87UVpZ2tb1XWAAe+vGZY7mX67xjffjFviOmLsE6dNMCu2tKsmWwvof3kf6xj2gecsPXjKO5J3nLl9CPiYpZ3Ubm1c9z7io3r9m9u/xOPEF9a3zA6EatTwRLZzLte2ondcytpWaOLa+ENaG9f1rTsKurpT7KTcv1SyRtrV+ek9Hq4c+P01ab0ln5u+OLc7CpE07817iKSdUxPp5NQ202tXtduH0NQXW0LGuScr4/2Kd1z4VUQg7ff2dtw+Uw662FkzcqvjcWdNz7I2Oz7qiwGhqXGOHn3kdve6/WJI2u03lT1MGeliVx6eHWrbw2R3ivFeoCbUl4fSXo9Ptf1iOg43eQeNaSHoNOcBu4FWnY3fw9Q0brfYRZxnYtZpN6/f+xj340lrQlvUtS1MacwyihvzzBoik57rHmq3W4z8A0hnO03PClX3dXe6Z232i7WEDVGn/b1uv+mxXddiRad9NUhaz7vwvjw+DxAzvSbonl63d7pUdc7a0BbelSeSorZ7e93+JbeHOp0MmIZmxhneIu6A1DNbrGohne0XU68eDgd11kbZZhdkGfZe13D+PHDOofmd6w1xbglJyw77VSKdcupZJF336txB/O68j8WO3sJIQskZ82gaGyaPducCIGjrHRsi7U4ENGFa26fuVZ0cxLL+5PZOf+C7kHYTOfdrnKNHm1VxIG3Pupj905J/AOl8p7qeqG3TdS/bQf0xRY1XppFg9qrLPtNM0cVZFyRtvAP2T5szzPMFB31b3W9aOIjVddqR/Cnfq/6x66Pu5noe8lmXkfFo7Q1p1XMGQc/Nnl46E7BZm/yjG2YtO6iFdMpB+v3K2Qt/ioGbjzrINvWSc+7RutYSuvfid6tI2u5Vn/fCt/vpa+e2hgN/mig/f1FcbmFc96jrHtNc0VfWo4H0r+HQnf9051xY1aHrdtjk5y/4HKJnnU6Z2bMu9oSLZb2Xzm9FzjQwbDu91Q3m9BafKHrMTn2CgDJVpwxEcpCka3um6HM6t0Vxpxs+Zwe9ibOct1XO+XmAax4bNkH2eTy4s1t4LkB7RRomvHimyOnaKfuzidqc24I8o2dQN32v52NzxkV3e0/M3oMm3JvzAEx6m1SNpOFL3Cm58vSW8+u+OyfnTsq5U4mNDZpjHno9t53cAsfLTjT7k5/YvUPQ96+z5vO1LEw99/m57dwnNRO09w2fc4wzRUPWEZqUSfN5Zjpja3tFJF0/vyWsR4l16h2Ttr/QaRB3cpKuPRP0kXA+iZzPuN5H7L/tua3Q5bEhnWfmM7Z8mjmd37oJv2b1U9p0LX0jRgPKVif5olGnm96HFPS+jL2Nns9i/Yny3OfXcGM9Wk4zW1UDa+w0X2bNtSb01DjG8fJ5ZjoRnFahj2K+YfUsnMemHgKdypk3IVN0JG28umnmZQWVVD/lVP2a85AhPWFSqqHPZ/PzRidUTdDxjL6rPVHjjHk0PIes6DWpgw+75yf0QdTzWeuJ5jOtn6JnxymL6x8Z32bmg3Q2n86Lp6D1fH48x3waYx5XTo1D0MuHZUstBKoLBKS/hu/3FVUb1rG2B7oIBf3Z1segk+R9HCf0+e/4yf3UjXPdiZOSsyuFoIfGv4ZvRYWPSn0P+NHcCu5vpa6NYxPBg6Gv7dF3Fzpc8ulGK3ycWn++qJ7On6GbPRS1VMr6Hg/gizKHOm1hzSfIucIHB33ka3wMirokcSywJ9U9OK87PXd6NrWvdEY6hAXVcysrqWg9BNDHaoF9vVd1yTrW+OC3fd/W+aheaSOhqRF0LvlGpmc8g3ir9dxCU9YHyqoDUV4Nbm73U99UWct5fX7bu1x7KdUFGpgb3cKog/tASp/PNN8wJWtcHZUFGN5S+sJNquXWqdS7CmG+qDjItdZuo+qDUh+I3/YuOYnUYBqmK9YFSqCl8pWrV1PoGdeyqOYVPmDcF265etTu8dFVvFJVL7vB1kSYVlhfxEpMGjT7ttRi4npMfFPtK7MpK6t5denyjaJezTx0K9XFqrXFQmOrItiKhMqa4kZlM8K9/VRXTGsyDbXWFVR7SKBPYv28WINprDUKryWHJs48Hw3qWFYrXpU1r5pQ1J+49bXytB7hqYSz99HVFzs5Tu3k2GyN/Mgl385jTSDlbHxDKuzQPBg8hivuC21xwpw0zTcdhkOaBfGsf7P1xaTyVTdNeGmtPGXOtdt8zN1YMU9quMWaQOIb09gT0kwHBPJivTzHGvSBlTazOpu4ZY9zvlgrqLHzz1ozb0T12qROXt/sXmny+m3sdLmeZzOtK0bqqNbLK1W97oavqQaFsL6xNWzUs4Nfo9Daj+QQIzrTb7dUn5d1CW39thuvZ8wouq62abUGJOcgyBpQt7COLkK0Q7G2ckr1B6m6ImwNczOmWnHT19u8Tvbs9Yy5W86Za4R26vVjG8i8l0nX7NfWscdSm6lcqpCjONk5l71ga20q5fHE1yS0el4sv4eGOdu6lS11TSnfw97T1c2b3uX1Y7kW5Mlbjlt08MzMeXIMX/tRMGd6fgDQJWeua+oqyFIOQqybMIv1ESJr2zuqj1xdnjSvxL2HZzlj/dhLVz/2ytSPTXqmsQrM4a1wjjSrD4pBPz/XWQNqrSLrWPv6plpHNrTGDWrJ6/Rq7VjxZuY8jf0g6xl7C1fTNNaPffoJpOuse+HwYWl0fU9Rx97R1UWmyPcbOl1mosVjLl2tl2yUrFq2NZF55M2rhTwmPAwHuUPHqsg/f0bSvmeEH1QrIyNrHsvg6S5VdqqNrF7CFSK5SB57BF+xjcWXo2cYNadqpjwmhKdQOLs6vU/PP4E0RP1csIbc+neQlGWduYjR9lh5Y0851hq+F1pvGm+tOo3zdEqZORPlqa2HjJyX8BTODOdHw5mD/mlVnXQNKxkrq2uu2querdo2vNlPxpe+jenJG18VjJUyc57ROGUueia7y+shK2gmzaoudI3ZyoPW+86U7bQt+o5+UrlwD5UynoiWb9SbZxlnyDdC8JwfE2cl/VxjvYZjJqkSNTu2KNtV/ZbIK23sP+R4rZbFm8k1hDNiXsIDdVhwZueIQddZb8CsD1NtdVu912QjGPdvkTdHj/dE7mvla3RsPGPKlMk1Ui11wBXHKonzj5+WtKi61DX2SSut7zafp/6xpm0mPrFRX6uGr9KXZFomn7OuAfkzTJJbzo+ecww6urXXNXQxse63KJsr2WsPqb4dFe6Yx2viGYuW2TO4D2R3lhkO7Fa0knri/CycU9DEutT1IeYsxNopm6ImbU8jb6PwauMn7zZmc5ppaK5B64RLnOZCQW+3u7qeM9JVD2nCPrNO1ZLnWmldnER4O+YtV2KsWmbPmGtNXuQMx9U3hW8YzoZ0cmvDeoeyPlyv1sw6VbQnbc8Yl3pgYq73Df15k/5pilVRhDI7xj0+4axmmbE75Jgl3zB6fv5ZCbqi6y2x/kZjdO/Zom3Pe6pq0XiTs6ULGM8yLXMfiDW916tvEPOu4PxkOPugK7pGZcPczXrtlS3aTuqOCp/elm0qGZE29eW5eoaoGasV/R4C589teq6Qfs50jWMZ6p1WTJtYR9rs28yb/GR6d9t63eGaoDAWX8afPnoGjQZX8Fr6uy7qei5IW782tdanIaxp5Bhpq5OIl9xL1L5N/Yez6MnC2GpZOG/gXd3VOLeTrnoIsN5+o6j5d4+s8FViHym8Z0qco+efgO/Z3b29ZqJk1XL0DMqdQ/hq3Lmq5wppq2thjc/j1xD+2KzltzVY2ok3M5+lqNNFn5/ljJUyewbmzn+E8E3duVXPtaBz1kIbno/vNP+0krgXrO5F5I1ZIGl8Zu67+PfZPBKOjMkz8N3DJ2a7uQnhd9sL+nzjRdKRtXeRv8E7R3MiVtsPQpvyElFKteHnF5ZxrmXgDO/m32KyEccpFc7VoD1rob3dQREprAfOPpJ8O+qbiPtr7j/OGCctY0YHBczqan56W9CaX1vWQBsqHtKIxqpkia8e9S0eXrliw7dHlSyUcY4fOt6mouYq55agk7KZtfgIfOP/4d8ngE6ySjmJ+Ak5OOpcyNOf9DHxrTBGx8B8boUxR29+hXNb0NDPm94xKhvewq88fiRtr7l3F96scabub/o88xUdR8o8DoTve+hzDeb8XA+uLejcRUTZYEqYGWxpJpvUrfpm/xYPLy76lxURlr4vUd6A7sLfCzU/tVB+KejMsaO24QW+Y6bteJOfCHPmLuzl79JWUceRMfRc3wHE4+Mb1fxq0Im1pQ1vZYPZH//eKOQlfiLMSef+Vr6qY/ELGgNCEhkOE+W3cH456Eougg1fZsu//0XdxDIX7umWzzFhy3i3YwSPxptFzE8vRvVy0FbZifbv8Kz/A3OSXdI38RaVVy7lyzomJcP/R6P7o/CM51bXeCNp3z9GJ4HCqc3fKAPc4e8n2UhT5lnjz/OXCGOk/Ae8ZQdOy6rmp1diei3oJ0vb+DaWcf+dxjbCmzTOOl8L+/j37SbyJcLo+hhy81jV8msxv06an0fnI8wbX/T7ThrghqhJ5Vvimd9bakQYe77vTfSMHznn19sbghbWmbYh7i7WCPi/Hf9uNGXecvG/c3bxD/rlBo/6fYTyj7dRfmvQpod0vB8fD/nld5SbCPPiMg2/jn7UwxrjN1F+R9BM+7nQ9g/CjTp5TNcWtZ5uc33jX3yRGL9Py+8NOuZ+SSXi3T/+V341yt+pp39U7hKnfu7x7/KrUSzj2P29nfI7g460a7z1l9Ds/xHjjtcf+/GX0BSME+Wn/5+gjW973hz9oft1P02TffjV8vVKfnpnDO8N+il6SeJtHIVj9+3w0fONjN+t5X+ZdOYljrfG7++sJcL/AuM/EXSNt+Nei9ReTzHDePrPBe16yiz2F6I27dnOgv4Hg1bez+zfBfOCrnWKpz/xun8q6OTfz09vb38u3n9T0E/+Sj/Bc4zy2X/Nf0HQtfg54mf7vD3/G1+n82+NueAe2T79/C8N+j/W/gr6r6D/CvqvoP+19k9d2DWNwg8H0gAAAABJRU5ErkJggg=="}
MANIFEST = {"name": "Selective", "short_name": "Selective", "description": "Daily NBA matchup briefing",
            "start_url": "./", "scope": "./", "display": "standalone", "orientation": "portrait",
            "background_color": "#000000", "theme_color": "#000000",
            "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]}
SW_JS = "self.addEventListener('install',function(){self.skipWaiting()});self.addEventListener('activate',function(e){e.waitUntil(self.clients.claim())});self.addEventListener('fetch',function(){});\n"
JS_PWA = r"""(function(){
var btns=document.querySelectorAll('.a2hs'),notes=document.querySelectorAll('.a2note'),dp=null;
var standalone=(window.matchMedia&&matchMedia('(display-mode: standalone)').matches)||window.navigator.standalone===true;
if(standalone){btns.forEach(function(b){b.style.display='none'});return}
if('serviceWorker' in navigator){try{navigator.serviceWorker.register('sw.js').catch(function(){})}catch(e){}}
window.addEventListener('beforeinstallprompt',function(e){e.preventDefault();dp=e});
window.addEventListener('appinstalled',function(){btns.forEach(function(b){b.style.display='none'});notes.forEach(function(n){n.style.display='none'})});
var ua=navigator.userAgent||'',ios=/iPad|iPhone|iPod/.test(ua)||(/Macintosh/.test(ua)&&navigator.maxTouchPoints>1);
function say(t){notes.forEach(function(n){n.textContent=t;n.style.display='block'})}
btns.forEach(function(b){b.addEventListener('click',function(){
  if(dp){dp.prompt();dp.userChoice.then(function(){dp=null});return}
  if(ios){say('On iPhone or iPad: tap the Share button in Safari, then choose Add to Home Screen.');return}
  say('In your browser menu, choose Install app or Add to Home screen.');
})});
})();"""


def write_pwa_files():
    import base64
    for name, b64 in ICONS.items():
        with open(os.path.join("docs", name), "wb") as f:
            f.write(base64.b64decode(b64))
    with open("docs/manifest.webmanifest", "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f)
    with open("docs/sw.js", "w", encoding="utf-8") as f:
        f.write(SW_JS)


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
    try:
        spread_payload = spreads_for_page(events)
    except Exception:
        print("WARNING: spreads failed; the My teams page will have no lines")
        traceback.print_exc()
        spread_payload = {"generated": 0, "games": []}
    _SPREADS.update({g["id"]: g for g in spread_payload.get("games", [])})
    cards = "".join(safe_card(e, i) for i, e in enumerate(events)) or '<p class="empty">No NBA games today. Check back tomorrow morning.</p>'
    try:
        cards = overview_page(events, spread_payload) + cards
    except Exception:
        print("WARNING: overview page failed")
        traceback.print_exc()
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
    stamp = today.strftime("%A, %B %-d, %Y") + " games &middot; updated " + now.astimezone(PT).strftime("%-I:%M %p PT")
    n = len(events)
    pager = ('<div class="pager"><button id="back" type="button">&lsaquo; Back</button>'
             '<span class="count" id="count"></span><button id="fwd" type="button">Forward &rsaquo;</button></div>')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Selective</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="apple-touch-icon.png"><meta name="theme-color" content="#000000">
<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Selective"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Jost:wght@400;500;600&family=Pinyon+Script&family=Cormorant+Garamond:wght@600;700&display=swap">
<style>{CSS}</style></head><body>
<div id="gate"><h1 class="gate-title" aria-label="Selective">Selective</h1><div class="gate-rule"></div><div class="gbtns"><button class="gbtn" id="enter" type="button" data-go="t">Today's games</button><button class="gbtn" id="enter-y" type="button" data-go="y">Yesterday's summary</button></div><div class="a2wrap"><button class="a2hs" type="button" title="Add this app to your phone's home screen"><svg viewBox="0 0 16 24" width="11" height="16" aria-hidden="true"><rect x="1.5" y="1" width="13" height="22" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 7v8M4.5 11h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="19.5" r=".9" fill="currentColor"/></svg><span>Add to home screen</span></button><p class="a2note" style="display:none"></p></div></div>
<main id="app" data-day="{today:%Y-%m-%d}">
<div class="bar-top"><div class="topline"><h1>Selective</h1><button id="menu" type="button">&#8962; Menu</button></div>
<p class="sub sub-t">{stamp} &middot; {n} game{'s' if n != 1 else ''}</p><p class="sub sub-y">Yesterday's summary &middot; {y_n} game{'s' if y_n != 1 else ''}</p>{pager}</div>
<div class="book" data-book="t">{cards}</div>
<div class="book" data-book="y">{y_html}</div>
<div class="legend"><span><i style="--k:var(--c-flags)"></i>What stands out</span><span><i style="--k:var(--c-rest)"></i>Rest &amp; schedule</span><span><i style="--k:var(--c-half)"></i>First vs. second half</span><span><i style="--k:var(--c-travel)"></i>Travel</span>
<span><i style="--k:var(--c-injury)"></i>Injuries</span><span><i style="--k:var(--c-rivalry)"></i>Revenge, rivalry &amp; head to head</span>
<span><i style="--k:var(--c-match)"></i>Matchup (solid = elite, dashed = bottom 10)</span><span><i style="--k:var(--c-form)"></i>Form</span>
<span><i style="--k:var(--c-outlook)"></i>Season outlook</span><span><i style="--k:var(--c-age)"></i>Age, roster &amp; coach</span><span><i style="--k:var(--c-line)"></i>Line &amp; crowd</span><span><i style="--k:var(--c-ref)"></i>Officials</span></div>
<p class="foot">{foot}</p>
<div class="a2wrap"><button class="a2hs" type="button" title="Add this app to your phone's home screen"><svg viewBox="0 0 16 24" width="11" height="16" aria-hidden="true"><rect x="1.5" y="1" width="13" height="22" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 7v8M4.5 11h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="19.5" r=".9" fill="currentColor"/></svg><span>Add to home screen</span></button><p class="a2note" style="display:none"></p></div>
</main><script type="application/json" id="spreads">{spread_json}</script><script>{JS}</script><script>{JS_MINE}</script><script>{JS_PWA}</script></body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    write_pwa_files()
    print(f"Wrote docs/index.html with {n} games")


if __name__ == "__main__":
    if "--spreads" in sys.argv:
        spreads_only()
    else:
        main()
