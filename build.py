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
import json, math, os, unicodedata, zipfile, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
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


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


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
            "oldest": games[-1]["date"]}


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


GLOWS = ["#ff9f43", "#4dd0e1", "#ff6fb5", "#b6e35a", "#a78bfa"]


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


def badge(team):
    """Original monogram badge in the team's color (not the team's logo)."""
    col = (team.get("color") or "666666").lstrip("#")
    if len(col) != 6:
        col = "666666"
    r, g, b = (int(col[i:i + 2], 16) for i in (0, 2, 4))
    fg = "#111" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#fff"
    return f'<span class="badge" style="--bc:#{col};--bf:{fg}" aria-hidden="true">{escape(team["abbreviation"])}</span>'


def section(title, k, rows):
    rows = "".join(r for r in rows if r)
    if not rows:
        return ""
    return f'<section class="grp" style="--k:var(--c-{k})"><h3>{escape(title)}</h3>{rows}</section>'


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
    tags += rtags
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
    rest_rows = [row("Rest", rest_text(ai), rest_text(hi), "rest")]
    def f(form, k, none="-"):
        return escape(str(form[k])) if form and form.get(k) else none
    def l10(form):
        if not form:
            return "-"
        n = min(form["n_games"], 10)
        return escape(form["last10"]) + (f"<small>{n} games played</small>" if n < 10 else "")
    form_rows = [
        row("Last 10 games", l10(af), l10(hf), "form"),
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

    if fa and fh:
        for lab, key in (("Spacing", "spacing"), ("Rebounding", "rebounding"), ("Rim protection", "rim")):
            if fa.get(key) and fh.get(key):
                match_rows.append(row(lab, rank_chip(fa[key], 30), rank_chip(fh[key], 30), "match"))

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
    if market and bar:
        market = market.replace("</h3>", "</h3>" + bar, 1)
    line = odds_line(comp)
    line_html = (f'<p class="note" style="--k:var(--c-line)"><b>Line</b> {escape(line)}</p>' if line else "")

    crew = fetch_officials(ev["id"])
    crew_html = f'<p class="note" style="--k:var(--c-ref)"><b>Officials</b> {escape(", ".join(crew))}</p>' if crew else ""
    notes = rivalry_notes(away["team"], home["team"], rivalries)
    src = ('<p class="src">Crowd data: Polymarket' + (" (thin market, treat as rough)" if crowd and crowd["thin"] else "") +
           f' &middot; updated {datetime.now(PT).strftime("%-I:%M %p PT")}</p>') if crowd else ""

    tip = tip_dt.strftime("%-I:%M %p PT")
    glow = GLOWS[idx % len(GLOWS)]
    return f"""<article class="page" data-i="{idx}" style="--glow:{glow}">
  <header class="match">
    <span class="tip">{tip}</span>
    <div class="teams"><h2 class="t a"><span>{escape(away["team"]["displayName"])}</span>{badge(away["team"])}</h2><span class="at">at</span><h2 class="t h">{badge(home["team"])}<span>{escape(home["team"]["displayName"])}</span></h2></div>
  </header>
  <section class="grp flagsec" style="--k:var(--glow)"><h3>What stands out</h3>{flags_html}</section>
  {section("Rest and schedule", "rest", rest_rows)}
  {section("Form", "form", form_rows)}
  {section(f"Head to head, last {h2h['n']} meetings" if h2h else "Head to head", "rivalry", h2h_rows)}{h2h_note}
  {section(f"Season outlook (forecast as of {load_forecast()['as_of']})" if fa and fh else "Season outlook", "outlook", outlook_rows)}{out_note}
  {section("Matchup", "match", match_rows)}
  {section("Roster", "age", age_rows)}
  {section("Injuries", "injury", inj_rows)}
  {market}
  {line_html}{crew_html}{src}{notes}
</article>"""


CSS = """
:root{color-scheme:dark;--bg:#262624;--card:#30302e;--ink:#f0eee6;--mute:#b0aea5;--line:#4a4945;--glow:#ff9f43;
--c-rest:#ff9a62;--c-travel:#4dc9e6;--c-injury:#ff6b6b;--c-rivalry:#b79cff;--c-match:#4fd8b4;
--c-outlook:#6aa9ff;--c-form:#ffd24d;--c-age:#ff8fc0;--c-line:#7fd96b;--c-ref:#9db2c2}
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
.teams{display:grid;grid-template-columns:minmax(0,1fr) minmax(5.5rem,auto) minmax(0,1fr);gap:12px;align-items:baseline;margin:12px 0 4px}
.t{font:700 1.9rem/1.05 "Barlow Condensed",sans-serif;margin:0;display:flex;align-items:center;gap:10px}
.t.a{justify-content:flex-end;text-align:right}.t.h{justify-content:flex-start;text-align:left}
.badge{flex:none;width:2.6rem;height:2.6rem;border-radius:50%;background:var(--bc);color:var(--bf);display:grid;place-items:center;
font:700 .9rem/1 "Barlow Condensed",sans-serif;letter-spacing:.02em;box-shadow:0 0 0 2px color-mix(in srgb,var(--ink) 35%,transparent)}
.v.hi{font-weight:700;color:var(--k)}.v.hi::after{content:"";display:block;height:2px;border-radius:2px;background:var(--k);margin-top:2px;width:100%}
.at{color:var(--mute);font-size:.95rem;text-align:center}
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
.calm{margin:0;color:var(--mute)}
.bar{height:6px;border-radius:99px;background:color-mix(in srgb,var(--k) 22%,var(--bg));margin:4px 0 6px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--k);border-radius:99px 0 0 99px}
.note{margin:16px 0 0;padding-left:10px;border-left:3px solid var(--k)}.note b{color:var(--k);margin-right:8px}
.src{color:var(--mute);font-size:.82rem;margin:10px 0 0}
details{margin-top:18px;border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;font-weight:600;color:var(--c-rivalry)}
details ul{margin:8px 0 0;padding-left:18px}details li{margin:6px 0;color:var(--mute)}details li b{color:var(--ink)}
details a{color:var(--c-rivalry)}details .kind{font-weight:600;color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:26px;font-size:.85rem;color:var(--mute)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;background:var(--k)}
.empty,.foot{color:var(--mute)}.foot{font-size:.88rem;margin-top:14px}
@media (max-width:480px){.teams{grid-template-columns:minmax(0,1fr) 1.6rem minmax(0,1fr);gap:6px}.t span{min-width:0}.t{font-size:1.3rem;gap:6px}.badge{width:2rem;height:2rem;font-size:.7rem}.row{gap:8px}.page{padding:18px 14px}}
@media (prefers-reduced-motion:no-preference){.page.on{animation:flip .22s ease-out}}
@keyframes flip{from{opacity:0;transform:translateX(var(--dx,12px))}to{opacity:1;transform:none}}
"""

JS = """
(function(){
document.documentElement.classList.add('js');
var pages=[].slice.call(document.querySelectorAll('.page')),i=0,
back=document.getElementById('back'),fwd=document.getElementById('fwd'),cnt=document.getElementById('count');
if(!pages.length)return;
function show(n,dir){
  i=Math.max(0,Math.min(pages.length-1,n));
  pages.forEach(function(p,k){p.classList.toggle('on',k===i)});
  pages[i].style.setProperty('--dx',(dir<0?'-12px':'12px'));
  back.disabled=i===0;fwd.disabled=i===pages.length-1;
  cnt.textContent=(i+1)+' of '+pages.length;
  try{history.replaceState(null,'','#g'+(i+1))}catch(e){}
  window.scrollTo(0,0);
}
back.onclick=function(){show(i-1,-1)};fwd.onclick=function(){show(i+1,1)};
document.addEventListener('keydown',function(e){
  if(e.key==='ArrowLeft')show(i-1,-1);else if(e.key==='ArrowRight')show(i+1,1)});
var x0=null;
document.addEventListener('touchstart',function(e){x0=e.touches[0].clientX},{passive:true});
document.addEventListener('touchend',function(e){
  if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;x0=null;
  if(Math.abs(dx)>70)show(i+(dx<0?1:-1),dx<0?1:-1)},{passive:true});
var m=/^#g(\\d+)$/.exec(location.hash);show(m?parseInt(m[1],10)-1:0,1);
})();
"""


def main():
    now = datetime.now(ET)
    today = now.date()
    season = today.year + 1 if today.month >= 8 else today.year
    data = get(f"{API}/scoreboard?dates={today:%Y%m%d}")
    events = sorted(data.get("events", []), key=lambda e: e["date"])
    revenge = load_revenge()
    rivalries = load_rivalries()
    for e in events:
        for c in e["competitions"][0]["competitors"]:
            roster_names(c["team"]["id"])
            team_history(c["team"]["id"], season)
    load_injuries()
    load_crowd()
    load_forecast()
    ratings = load_ratings(today)
    cards = "".join(card(e, today, season, revenge, rivalries, ratings, i, len(events))
                    for i, e in enumerate(events)) or '<p class="empty">No NBA games today. Check back tomorrow morning.</p>'
    foot = (ratings["note"] + " " if ratings else "") + "Rotation age weights each player by projected minutes; roster average counts every player equally. Season outlook, impact and projected minutes come from your forecast file; impact is a player's contribution to team strength in that model. Form uses completed regular-season games only. Injuries and officials come from ESPN and may lag. Crowd % is the Polymarket price, a rough signal when trading is thin."
    stamp = now.astimezone(PT).strftime("%A, %B %-d, %Y")
    n = len(events)
    pager = ('<div class="pager"><button id="back" type="button">&lsaquo; Back</button>'
             '<span class="count" id="count"></span><button id="fwd" type="button">Forward &rsaquo;</button></div>') if n else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>JMAG'S NBA Daily Digest</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&family=Barlow+Condensed:wght@600;700&display=swap">
<style>{CSS}</style></head><body><main>
<div class="bar-top"><h1>JMAG'S NBA Daily Digest</h1><p class="sub">{stamp} &middot; {n} game{'s' if n != 1 else ''}</p>{pager}</div>
{cards}
<div class="legend"><span><i style="--k:var(--c-rest)"></i>Rest &amp; schedule</span><span><i style="--k:var(--c-travel)"></i>Travel</span>
<span><i style="--k:var(--c-injury)"></i>Injuries</span><span><i style="--k:var(--c-rivalry)"></i>Revenge, rivalry &amp; head to head</span>
<span><i style="--k:var(--c-match)"></i>Matchup (solid = elite, dashed = bottom 10)</span><span><i style="--k:var(--c-form)"></i>Form</span>
<span><i style="--k:var(--c-outlook)"></i>Season outlook</span><span><i style="--k:var(--c-age)"></i>Age, roster &amp; coach</span><span><i style="--k:var(--c-line)"></i>Line &amp; crowd</span><span><i style="--k:var(--c-ref)"></i>Officials</span></div>
<p class="foot">{foot}</p>
</main><script>{JS}</script></body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote docs/index.html with {n} games")


if __name__ == "__main__":
    main()
