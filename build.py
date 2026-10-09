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
import json, os, unicodedata, urllib.parse, urllib.request
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

    # basic rest
    played_dates = {g["date"] for g in prior}
    b2b = day(1) in played_dates
    third_in_four = sum(day(k) in played_dates for k in (1, 2, 3)) >= 2
    days_off = (today - max(g["date"] for g in prior)).days - 1 if prior else None

    # consecutive road games ending with most recent
    road_streak = 0
    for g in reversed(prior):
        if not g["home"]:
            road_streak += 1
        else:
            break

    # 4th road game in last 6 nights (looking at prior 5 days + today if road)
    recent_road = sum(1 for g in prior if not g["home"] and (today - g["date"]).days <= 5)

    # previous game result (for letdown)
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
    """Compute last-10, streak, home/road, close-game, vs-winning from history."""
    hist = info.get("hist") or []
    if not hist:
        return None

    last10 = hist[-10:]
    wins10 = sum(1 for g in last10 if g["win"])
    losses10 = len(last10) - wins10

    # current streak
    streak_w = 0
    streak_l = 0
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

    # close games (margin <= 5)
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
    }        "streak": streak,
        "home": home_rec,
        "road": road_rec,
        "close": close_rec,
        "n_games": len(hist),
    


# ---------------------------------------------------------------------------
# Revenge / rivalries (unchanged loaders)
# ---------------------------------------------------------------------------
def load_revenge():
    path = os.path.join(os.path.dirname(__file__) or ".", "data", "revenge.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rivalries():
    path = os.path.join(os.path.dirname(__file__) or ".", "data", "rivalries.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Injuries (league-wide) + per-game officials (from summary)
# ---------------------------------------------------------------------------
_injuries_by_team = None   # abbr -> list of {name, status, comment}


def load_injuries():
    """Fetch current injury report. Returns dict abbr -> list of injury dicts."""
    global _injuries_by_team
    if _injuries_by_team is not None:
        return _injuries_by_team
    _injuries_by_team = {}
    try:
        data = get(f"{API}/injuries")
    except Exception as e:
        print(f"WARNING: injuries fetch failed: {e}")
        return _injuries_by_team
    # Map displayName -> abbr via a quick teams lookup if needed; ESPN uses full names
    # Build a name->abbr map from known teams
    name_to_abbr = {}
    try:
        teams = get(f"{API}/teams")
        for t in teams["sports"][0]["leagues"][0]["teams"]:
            team = t["team"]
            abbr = team["abbreviation"]
            # ESPN sometimes uses UTAH instead of UTA; normalize known quirks
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
            # try last word heuristic
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
    """Return (tags, html) for notable injuries on both sides."""
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
            parts.append(f'<div class="prof"><b>{escape(abbr)} OUT</b>{escape(names)}{escape(more)}</div>')
        if dtd and len(dtd) >= 3:
            tags.append(("injury", f"{abbr} {len(dtd)} day-to-day"))
            if not outs:  # only add a line if we didn't already list OUTs
                dnames = ", ".join(p["name"] for p in dtd[:4])
                parts.append(f'<div class="prof"><b>{escape(abbr)} DTD</b>{escape(dnames)}</div>')
    return tags, "".join(parts)


def fetch_officials(event_id):
    """Best-effort referee crew from game summary. Returns list of names or []."""
    try:
        summary = get(f"{API}/summary?event={event_id}")
        officials = (summary.get("gameInfo") or {}).get("officials") or []
        return [o.get("displayName") or o.get("fullName") for o in officials if o.get("displayName") or o.get("fullName")]
    except Exception as e:
        print(f"WARNING: officials for {event_id}: {e}")
        return []


# ---------------------------------------------------------------------------
# Rosters + ages
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Ratings (offense / defense) – unchanged core, still free
# ---------------------------------------------------------------------------
NBA_STATS = "https://stats.nba.com/stats/leaguedashteamstats"
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com", "Accept": "application/json",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
}
GOOD, BAD = 10, 10
BLEND_GAMES = 20


def nba_stats_ratings(season_end):
    params = {"Conference": "", "DateFrom": "", "DateTo": "", "Division": "", def espn_ratings(season_end):
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
    row = lambda t, r: (f'<div class="prof"><b>{escape(t["team"]["abbreviation"])}</b>'
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
    line = (f'<div class="prof"><b>Avg age</b>{escape(aa)} {ma:.1f} &middot; {escape(ha)} {mh:.1f} '
            f'&middot; gap {abs(gap):.1f} yrs ({escape(older)} older)</div>')
    return tags, line


# ---------------------------------------------------------------------------
# Odds (best-effort – ESPN sometimes includes them)
# ---------------------------------------------------------------------------
def odds_line(comp):
    """Return a short string like 'DAL -3.5 · O/U 224.5' or None."""
    odds = comp.get("odds")
    if not odds:
        return None
    # odds can be a list or a dict depending on ESPN response shape
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
# Card building
# ---------------------------------------------------------------------------
def team_line(info, form):
    if info is None:
        return "Rest data unavailable"
    bits = []
    if info["b2b"]:
        bits.append("2nd night of a back-to-back")
    if info["third_in_four"]:
        bits.append("3rd game in 4 nights")
    if info.get("recent_road_in_6", 0) >= 3:
        bits.append(f"{info['recent_road_in_6']} road games in last 5 nights")
    if not bits:
        d = info["days_off"]
        bits.append("First game of the season" if d is None else f"{d} day{'s' if d != 1 else ''} rest")
    if form and form["n_games"] >= 3:
        bits.append(f"L10 {form['last10']} ({form['streak']})")
    return " · ".join(bits)


def card(ev, today, season, revenge, rivalries, ratings):
    comp = ev["competitions"][0]
    side = {c["homeAway"]: c for c in comp["competitors"]}
    away, home = side["away"], side["home"]
    a_abbr, h_abbr = away["team"]["abbreviation"], home["team"]["abbreviation"]
    a_id, h_id = away["team"]["id"], home["team"]["id"]

    ai = rest_and_travel(a_id, a_abbr, today, season)
    hi = rest_and_travel(h_id, h_abbr, today, season)
    af = form_summary(ai)
    hf = form_summary(hi)

    tags = []
    itags, inj_html = injury_block(a_abbr, h_abbr)
    tags += itags

    # --- Fatigue / travel tags ---
    if (ai["b2b"] and ai["third_in_four"]
            and hi["days_off"] is not None and hi["days_off"] >= 1):
        tags.append(("fatigue", "Road B2B + 3rd in 4 nights vs. rested opponent"))
    elif ai["b2b"] and (hi["days_off"] or 0) >= 2:
        tags.append(("fatigue", f"Road B2B vs. {h_abbr} with {hi['days_off']} days rest"))
    road_in_window = ai.get("recent_road_in_6", 0) + 1  # +1 for today's road game
    if road_in_window >= 4:
        tags.append(("fatigue", f"{a_abbr} {road_in_window}th road game in 6 nights"))

    # Altitude
    if h_abbr in ("DEN", "UTA") and a_abbr not in ("DEN", "UTA"):
        tags.append(("altitude", f"Altitude: {a_abbr} visits {h_abbr}"))

    # West-to-east travel for a night game
    tip_dt = parse_dt(ev["date"]).astimezone(PT)
    if a_abbr in WEST_COAST and h_abbr not in WEST_COAST and tip_dt.hour >= 16:
        tags.append(("travel", f"West-to-east: {a_abbr} CSS = """
:root{color-scheme:dark;--bg:#262624;--card:#30302e;--ink:#f0eee6;--mute:#b0aea5;--line:#4a4945;--glow:#ff9f43;
--fatigue:#ff8a80;--revenge:#b9a3ff;--mismatch:#5fd4bf;--lockdown:#8ab4ff;--weak:#ffb86b;--tip:#f0eee6;--tipfg:#262624;
--altitude:#ffd166;--travel:#78d5e3;--form:#c3a6ff;--schedule:#f4a261;--injury:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 "Barlow",system-ui,sans-serif;padding:max(16px,env(safe-area-inset-top)) 16px 40px}
main{max-width:720px;margin:0 auto}h1{font:700 2rem "Barlow Condensed",sans-serif;margin:8px 0 2px}
.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--glow);border-top:4px solid var(--glow);
box-shadow:0 0 0 1px color-mix(in srgb,var(--glow) 22%,transparent),0 0 26px -4px color-mix(in srgb,var(--glow) 55%,transparent);
border-radius:12px;padding:14px 16px;margin-bottom:18px}
.card:nth-of-type(5n+1){--glow:#ff9f43}.card:nth-of-type(5n+2){--glow:#4dd0e1}.card:nth-of-type(5n+3){--glow:#ff6fb5}
.card:nth-of-type(5n+4){--glow:#b6e35a}.card:nth-of-type(5n+5){--glow:#a78bfa}
.top{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tip{background:var(--tip);color:var(--tipfg);font-weight:600;font-size:.85rem;padding:2px 9px;border-radius:99px}
.tag{font-size:.85rem;font-weight:600;padding:2px 9px;border-radius:99px;border:1.5px solid currentColor}
.fatigue{color:var(--fatigue)}.revenge{color:var(--revenge)}.mismatch{color:var(--mismatch)}.age{color:var(--weak)}
.lockdown{color:var(--lockdown)}.altitude{color:var(--altitude)}.travel{color:var(--travel)}
.form{color:var(--form)}.schedule{color:var(--schedule)}.injury{color:var(--injury)}
.prof{margin-top:6px;font-size:.9rem}.prof b{margin-right:8px}.chip{display:inline-block;margin:2px 6px 2px 0;padding:1px 9px;border-radius:99px;border:1.5px solid var(--line);color:var(--mute)}.chip.elite{color:var(--mismatch);border-color:currentColor;font-weight:600}.chip.weak{color:var(--weak);border-color:currentColor;font-weight:600}
h2{font:600 1.5rem "Barlow Condensed",sans-serif;margin:8px 0}h2 small{color:var(--mute);font-weight:400}
dl{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:0}dt{font-weight:600}dd{margin:0;color:var(--mute)}
.empty,.foot{color:var(--mute)}.foot{font-size:.9rem;margin-top:24px}
details{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}summary{cursor:pointer;font-weight:600}
details ul{margin:8px 0 0;padding-left:18px}details li{margin:6px 0;color:var(--mute)}details li b{color:var(--ink)}
details a{color:var(--revenge)}details .kind{font-weight:600;color:var(--ink)}
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
            # pre-warm history cache
            team_history(c["team"]["id"], season)
    load_injuries()  # pre-warm injury report
    ratings = load_ratings(today)
    cards = "".join(card(e, today, season, revenge, rivalries, ratings) for e in events) or '<p class="empty">No NBA games today.</p>'
    foot = (ratings["note"] + " " if ratings else "") + "Average age is the mean of every player on the roster. Form uses completed regular-season games only. Injuries and officials are best-effort from ESPN."
    stamp = now.astimezone(PT).strftime("%A, %B %-d, %Y")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>JMAG'S NBA Daily Digest</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&family=Barlow+Condensed:wght@600;700&display=swap">
<style>{CSS}</style></head><body><main>
<h1>JMAG'S NBA Daily Digest</h1><p class="sub">{stamp} &middot; {len(events)} game{'s' if len(events) != 1 else ''}</p>
{cards}
<p class="foot">{foot}</p>
</main></body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote docs/index.html with {len(events)} games")


if __name__ == "__main__":
    main()
