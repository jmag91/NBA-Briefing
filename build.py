"""NBA morning briefing builder - stage 1.
Pulls today's slate from ESPN's public endpoints, checks schedule fatigue
(checklist #1) and playoff revenge (checklist #2), and writes docs/index.html.
Free: needs no API key and no extra packages.
"""
import json, os, urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
_cache = {}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def team_games(tid, season):
    """Every regular-season game for a team as {et_date: played_on_road}."""
    data = get(f"{API}/teams/{tid}/schedule?season={season}&seasontype=2")
    out = {}
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        me = next(c for c in comp["competitors"] if str(c["team"]["id"]) == str(tid))
        out[parse_dt(ev["date"]).astimezone(ET).date()] = me["homeAway"] == "away"
    return out


def rest_info(tid, today):
    season = today.year + 1 if today.month >= 8 else today.year
    try:
        if tid not in _cache:
            _cache[tid] = team_games(tid, season)
        games = _cache[tid]
    except Exception as e:
        print(f"WARNING: no schedule for team {tid}: {e}")
        return None
    prior = [d for d in games if d < today]
    day = lambda k: today - timedelta(days=k)
    return {
        "b2b": day(1) in games,
        "third_in_four": sum(day(k) in games for k in (1, 2, 3)) >= 2,
        "days_off": (today - max(prior)).days - 1 if prior else None,
    }


def load_revenge():
    path = os.path.join(os.path.dirname(__file__), "data", "revenge.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def load_rivalries():
    path = os.path.join(os.path.dirname(__file__), "data", "rivalries.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def roster_names(tid):
    """Lower-cased names of the players currently on a team's roster."""
    try:
        data = get(f"{API}/teams/{tid}/roster")
    except Exception as e:
        print(f"WARNING: no roster for team {tid}: {e}")
        return None
    names = set()
    for a in data.get("athletes", []):
        for p in a.get("items", [a]):
            if p.get("displayName"):
                names.add(p["displayName"].strip().lower())
    for c in data.get("coach", []):
        full = f'{c.get("firstName", "")} {c.get("lastName", "")}'.strip().lower()
        if full:
            names.add(full)
    return names


def rivalry_notes(a_team, h_team, rivalries):
    """Incidents for this matchup, kept only if an involved player is on either roster today."""
    key = "-".join(sorted([a_team["abbreviation"], h_team["abbreviation"]]))
    incidents = rivalries.get(key, [])
    if not incidents:
        return ""
    ra, rh = roster_names(a_team["id"]), roster_names(h_team["id"])
    if ra is None or rh is None:
        return ""
    current = ra | rh
    shown = [i for i in incidents
             if any(p["role"] != "referee" and p["name"].strip().lower() in current for p in i["people"])]
    if not shown:
        return ""
    items = "".join(
        f'<li><b>{escape(i["date"])}</b> <span class="kind">{escape(i.get("type", ""))}</span> {escape(i["summary"])} '
        f'<a href="{escape(i["source_url"], quote=True)}" rel="noopener">{escape(i["source_name"])}</a></li>'
        for i in sorted(shown, key=lambda i: i["date"], reverse=True))
    return f'<details><summary>Rivalry notes ({len(shown)})</summary><ul>{items}</ul></details>'


def team_line(info):
    if info is None:
        return "Rest data unavailable"
    bits = []
    if info["b2b"]:
        bits.append("2nd night of a back-to-back")
    if info["third_in_four"]:
        bits.append("3rd game in 4 nights")
    if not bits:
        d = info["days_off"]
        bits.append("First game of the season" if d is None else f"{d} day{'s' if d != 1 else ''} rest")
    return " &middot; ".join(bits)


def card(ev, today, revenge, rivalries):
    comp = ev["competitions"][0]
    side = {c["homeAway"]: c for c in comp["competitors"]}
    away, home = side["away"], side["home"]
    ai, hi = rest_info(away["team"]["id"], today), rest_info(home["team"]["id"], today)
    tags = []
    if (ai and hi and ai["b2b"] and ai["third_in_four"]
            and hi["days_off"] is not None and hi["days_off"] >= 1):
        tags.append(("fatigue", "Road back-to-back, 3rd in 4 nights, vs. rested opponent"))
    a, h = away["team"]["abbreviation"], home["team"]["abbreviation"]
    for r in revenge:
        if {r["winner"], r["loser"]} == {a, h}:
            tags.append(("revenge", f"Playoff revenge: {r['winner']} beat {r['loser']} {r['result']} ({r['round']})"))
    tip = parse_dt(ev["date"]).astimezone(PT).strftime("%-I:%M %p PT")
    tag_html = "".join(f'<span class="tag {c}">{escape(t)}</span>' for c, t in tags)
    name = lambda c: escape(c["team"]["displayName"])
    notes = rivalry_notes(away["team"], home["team"], rivalries)
    return f"""<article class="card">
  <div class="top"><span class="tip">{tip}</span>{tag_html}</div>
  <h2>{name(away)} <small>at</small> {name(home)}</h2>
  <dl><dt>{escape(a)}</dt><dd>{team_line(ai)}</dd><dt>{escape(h)}</dt><dd>{team_line(hi)}</dd></dl>
  {notes}
</article>"""


CSS = """
:root{--bg:#edf0f3;--card:#fff;--ink:#14202b;--mute:#5b6b7a;--line:#d5dce3;
--fatigue:#b3261e;--revenge:#6240b5;--mismatch:#0b6e5f;--tip:#14202b;--tipfg:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#10161d;--card:#18212b;--ink:#e8edf2;--mute:#93a3b3;
--line:#2a3643;--fatigue:#ff8a80;--revenge:#b9a3ff;--mismatch:#5fd4bf;--tip:#e8edf2;--tipfg:#10161d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 "Barlow",system-ui,sans-serif;padding:max(16px,env(safe-area-inset-top)) 16px 40px}
main{max-width:720px;margin:0 auto}h1{font:700 2rem "Barlow Condensed",sans-serif;margin:8px 0 2px}
.sub{color:var(--mute);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px;margin-bottom:12px}.top{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tip{background:var(--tip);color:var(--tipfg);font-weight:600;font-size:.85rem;padding:2px 9px;border-radius:99px}
.tag{font-size:.85rem;font-weight:600;padding:2px 9px;border-radius:99px;border:1.5px solid currentColor}
.fatigue{color:var(--fatigue)}.revenge{color:var(--revenge)}.mismatch{color:var(--mismatch)}
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
    data = get(f"{API}/scoreboard?dates={today:%Y%m%d}")
    events = sorted(data.get("events", []), key=lambda e: e["date"])
    revenge = load_revenge()
    rivalries = load_rivalries()
    cards = "".join(card(e, today, revenge, rivalries) for e in events) or '<p class="empty">No NBA games today.</p>'
    stamp = now.astimezone(PT).strftime("%A, %B %-d, %Y")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>NBA Morning Briefing</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&family=Barlow+Condensed:wght@600;700&display=swap">
<style>{CSS}</style></head><body><main>
<h1>NBA Morning Briefing</h1><p class="sub">{stamp} &middot; {len(events)} game{'s' if len(events) != 1 else ''}</p>
{cards}
<p class="foot">Coming next: injuries, rotation age, offense/defense mismatches, matchup deep dives.</p>
</main></body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote docs/index.html with {len(events)} games")


if __name__ == "__main__":
    main()
