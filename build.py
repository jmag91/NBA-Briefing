"""NBA morning briefing builder - stage 1.
Pulls today's slate from ESPN's public endpoints, checks schedule fatigue
(checklist #1) and playoff revenge (checklist #2), and writes docs/index.html.
Free: needs no API key and no extra packages.
"""
import json, os, unicodedata, urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
_cache = {}


def norm(s):
    """Lower-case a name and strip accents so Doncic matches Doncic with a hacek."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


REVENGE_JSON = r'''[
 {
  "winner": "DET",
  "loser": "ORL",
  "result": "4-3",
  "round": "First Round"
 },
 {
  "winner": "CLE",
  "loser": "TOR",
  "result": "4-3",
  "round": "First Round"
 },
 {
  "winner": "PHI",
  "loser": "BOS",
  "result": "4-3",
  "round": "First Round"
 },
 {
  "winner": "NY",
  "loser": "ATL",
  "result": "4-2",
  "round": "First Round"
 },
 {
  "winner": "OKC",
  "loser": "PHX",
  "result": "4-0",
  "round": "First Round"
 },
 {
  "winner": "LAL",
  "loser": "HOU",
  "result": "4-2",
  "round": "First Round"
 },
 {
  "winner": "SA",
  "loser": "POR",
  "result": "4-1",
  "round": "First Round"
 },
 {
  "winner": "MIN",
  "loser": "DEN",
  "result": "4-2",
  "round": "First Round"
 },
 {
  "winner": "NY",
  "loser": "PHI",
  "result": "4-0",
  "round": "Second Round"
 },
 {
  "winner": "CLE",
  "loser": "DET",
  "result": "4-3",
  "round": "Second Round"
 },
 {
  "winner": "OKC",
  "loser": "LAL",
  "result": "4-0",
  "round": "Second Round"
 },
 {
  "winner": "SA",
  "loser": "MIN",
  "result": "4-2",
  "round": "Second Round"
 },
 {
  "winner": "NY",
  "loser": "CLE",
  "result": "4-0",
  "round": "Conference Finals"
 },
 {
  "winner": "SA",
  "loser": "OKC",
  "result": "4-3",
  "round": "Conference Finals"
 },
 {
  "winner": "NY",
  "loser": "SA",
  "result": "4-1",
  "round": "NBA Finals"
 }
]'''

RIVALRY_JSON = r'''{
 "ATL-MIN": [
  {
   "date": "2026-02-09",
   "type": "Fight / ejections",
   "summary": "Timberwolves 138-116: Mouhamed Gueye pushed Naz Reid from behind, Reid approached him, and the two grabbed each other's jerseys. Both got technicals and were ejected, and the league fined each $35,000.",
   "people": [
    {
     "name": "Mouhamed Gueye",
     "role": "player"
    },
    {
     "name": "Naz Reid",
     "role": "player"
    }
   ],
   "source_name": "NBA official release",
   "source_url": "https://official.nba.com/hawks-mouhamed-gueye-and-timberwolves-naz-reid-fined/"
  }
 ],
 "ATL-NY": [
  {
   "date": "2026-04-30",
   "type": "Fight / ejections",
   "summary": "2026 first round, Game 6 (Knicks won 140-89): Mitchell Robinson and Dyson Daniels became entangled battling for position on a free throw, and both were given technicals and ejected. Onyeka Okongwu and Jalen Brunson helped separate them. The league fined Robinson $50,000, more than Daniels' $25,000, partly because of an inappropriate postgame social media post about the incident.",
   "people": [
    {
     "name": "Mitchell Robinson",
     "role": "player"
    },
    {
     "name": "Dyson Daniels",
     "role": "player"
    },
    {
     "name": "Onyeka Okongwu",
     "role": "player"
    },
    {
     "name": "Jalen Brunson",
     "role": "player"
    }
   ],
   "source_name": "AP via Atlanta News First",
   "source_url": "https://www.atlantanewsfirst.com/2026/05/02/hawks-dyson-daniels-fined-25k-fighting-knicks-mitchell-robinson-game-6-loss/"
  }
 ],
 "BOS-PHI": [
  {
   "date": "2026-05-05",
   "type": "Player vs. officials",
   "summary": "After Boston's 109-100 Game 7 loss to the 76ers in the first round, Jaylen Brown said on a livestream that officials had an agenda to call fouls against him for pushing off and that some referees needed to be investigated. The league fined him $50,000. He had already been fined $35,000 in January for a similar rant.",
   "people": [
    {
     "name": "Jaylen Brown",
     "role": "player"
    }
   ],
   "source_name": "AP via The Sun Chronicle",
   "source_url": "https://www.thesunchronicle.com/sports/celtics-jaylen-brown-fined-50-000-by-the-nba-for-public-criticism-of-playoff-officiating/article_2b855851-976e-505e-9e99-689a998e90ba.html"
  }
 ],
 "BOS-SA": [
  {
   "date": "2026-01-12",
   "type": "Player vs. officials",
   "summary": "Jaylen Brown was fined $35,000 for public criticism of the officiating, in comments to the press and on social media after Boston's 100-95 home loss to San Antonio on Jan. 10.",
   "people": [
    {
     "name": "Jaylen Brown",
     "role": "player"
    }
   ],
   "source_name": "NBA official release",
   "source_url": "https://official.nba.com/bostons-brown-fined/"
  }
 ],
 "CHA-DET": [
  {
   "date": "2026-02-09",
   "type": "Fight / suspensions",
   "summary": "Pistons 110, Hornets 104: after Moussa Diabate fouled Jalen Duren in the third quarter, a fight broke out and all four players involved were ejected. Isaiah Stewart, who left the bench area to join in, was suspended seven games, partly because of his history of unsportsmanlike acts. Miles Bridges and Diabate got four games each, and Duren two games for initiating the altercation.",
   "people": [
    {
     "name": "Isaiah Stewart",
     "role": "player"
    },
    {
     "name": "Miles Bridges",
     "role": "player"
    },
    {
     "name": "Moussa Diabate",
     "role": "player"
    },
    {
     "name": "Jalen Duren",
     "role": "player"
    }
   ],
   "source_name": "NBA official release",
   "source_url": "https://official.nba.com/nba-announces-penalties-from-pistons-hornets-game/"
  }
 ],
 "CLE-PHX": [
  {
   "date": "2026-01-31",
   "type": "Coach vs. official",
   "summary": "Cavaliers coach Kenny Atkinson was fined $50,000 for aggressively pursuing, berating and making inadvertent contact with an official during Cleveland's loss to the Suns on Jan. 30. He was upset about a no-call on a defensive play near the perimeter.",
   "people": [
    {
     "name": "Kenny Atkinson",
     "role": "coach"
    }
   ],
   "source_name": "AP via Yuma Sun",
   "source_url": "https://www.yumasun.com/sports/cavaliers-coach-kenny-atkinson-fined-50k-for-actions-following-ejection-in-loss-vs-suns/article_aa1f0fc9-cc30-54c9-8346-32609a0222c7.html"
  }
 ],
 "DEN-MIN": [
  {
   "date": "2026-04-25",
   "type": "Fight / ejections",
   "summary": "2026 first round, Game 4 (Timberwolves won 112-96): in the final seconds, Nikola Jokic confronted and shoved Jaden McDaniels after McDaniels took an uncontested layup with the game decided. Julius Randle then shoved Bruce Brown. Jokic and Randle were ejected; the league fined Jokic $50,000 and Randle $35,000 and did not suspend either.",
   "people": [
    {
     "name": "Nikola Jokic",
     "role": "player"
    },
    {
     "name": "Julius Randle",
     "role": "player"
    },
    {
     "name": "Jaden McDaniels",
     "role": "player"
    },
    {
     "name": "Bruce Brown",
     "role": "player"
    }
   ],
   "source_name": "Eurohoops (NBA release)",
   "source_url": "https://www.eurohoops.net/en/nba-news/1960825/nikola-jokic-sanction-denver-nuggets-nba-playoffs/"
  }
 ],
 "IND-NY": [
  {
   "date": "2024-05-10",
   "type": "Coach vs. officials",
   "summary": "2024 East semifinals: Pacers coach Rick Carlisle was fined $35,000 for publicly criticizing the officiating and questioning the league's integrity after Game 2. He singled out an uncalled Josh Hart shove on Tyrese Haliburton, who was dealing with back problems.",
   "people": [
    {
     "name": "Rick Carlisle",
     "role": "coach"
    },
    {
     "name": "Josh Hart",
     "role": "player"
    },
    {
     "name": "Tyrese Haliburton",
     "role": "player"
    }
   ],
   "source_name": "CBS Sports",
   "source_url": "https://www.cbssports.com/nba/news/pacers-rick-carlisle-fined-35000-for-postgame-comments-after-game-2-loss-vs-knicks-in-nba-playoffs/"
  },
  {
   "date": "2024-05-10",
   "type": "Public feud",
   "summary": "2024 East semifinals: Josh Hart called Carlisle's officiating complaints disrespectful to the Knicks. The Pacers had submitted 78 calls from Games 1 and 2 to the league office for review.",
   "people": [
    {
     "name": "Josh Hart",
     "role": "player"
    },
    {
     "name": "Rick Carlisle",
     "role": "coach"
    }
   ],
   "source_name": "Sports Illustrated",
   "source_url": "https://www.si.com/nba/knicks-josh-hart-rips-pacers-coach-rick-carlisle-over-officiating-complaints"
  },
  {
   "date": "2024-05-14",
   "type": "Scuffle",
   "summary": "2024 East semifinals, Game 5: Donte DiVincenzo and Myles Turner scuffled and both received technicals. Isaiah Jackson, Alec Burks and Isaiah Hartenstein also got technicals in a separate flare-up. DiVincenzo said afterward the Pacers were trying to act like tough guys.",
   "people": [
    {
     "name": "Donte DiVincenzo",
     "role": "player"
    },
    {
     "name": "Myles Turner",
     "role": "player"
    },
    {
     "name": "Isaiah Jackson",
     "role": "player"
    },
    {
     "name": "Alec Burks",
     "role": "player"
    },
    {
     "name": "Isaiah Hartenstein",
     "role": "player"
    }
   ],
   "source_name": "CBS Sports",
   "source_url": "https://www.cbssports.com/nba/news/knicks-donte-divincenzo-says-pacers-are-trying-to-be-tough-guys-after-game-5-altercation-with-myles-turner/"
  },
  {
   "date": "2025-05-21",
   "type": "Trash talk",
   "summary": "2025 East finals, Game 1: Tyrese Haliburton copied Reggie Miller's choke celebration after a game-tying shot that forced overtime. Indiana won 138-135 in overtime.",
   "people": [
    {
     "name": "Tyrese Haliburton",
     "role": "player"
    }
   ],
   "source_name": "ESPN (via 101.7 The Team)",
   "source_url": "https://www.1017theteam.com/news/%f0%9f%91%8a-haliburton-choke-latest-in-pacers-knicks-beef"
  }
 ],
 "LAL-NY": [
  {
   "date": "2026-03-08",
   "type": "Player vs. official",
   "summary": "Luka Doncic was fined $50,000 for rubbing his fingers together in a money gesture toward an official after not getting a charge call in the Lakers' 110-97 win. LA Magazine identified the official as Tre Maddox. Doncic had 15 technical fouls at the time, one short of an automatic one-game suspension.",
   "people": [
    {
     "name": "Luka Doncic",
     "role": "player"
    },
    {
     "name": "Tre Maddox",
     "role": "referee"
    }
   ],
   "source_name": "NBA official release",
   "source_url": "https://official.nba.com/lakers-doncic-fined"
  }
 ],
 "NY-PHI": [
  {
   "date": "2024-04-23",
   "type": "Coach vs. officials",
   "summary": "2024 first round: after losing Games 1 and 2 in New York, the 76ers said they planned to file a grievance over the officiating. Joel Embiid said Tyrese Maxey was fouled on the play that led to the Knicks' go-ahead steal in Game 2 and called the officiating unacceptable.",
   "people": [
    {
     "name": "Joel Embiid",
     "role": "player"
    },
    {
     "name": "Tyrese Maxey",
     "role": "player"
    },
    {
     "name": "Nick Nurse",
     "role": "coach"
    }
   ],
   "source_name": "AP via TSN",
   "source_url": "https://www.tsn.ca/nba/76ers-plan-to-file-grievance-about-officiating-during-first-two-games-of-series-against-knicks-1.2108224"
  },
  {
   "date": "2024-04-25",
   "type": "Flagrant foul",
   "summary": "2024 first round, Game 3: Joel Embiid got a flagrant 1 for grabbing the legs of Mitchell Robinson, and also appeared to strike Robinson in the groin on a shot. Donte DiVincenzo called the play dirty and Josh Hart called it reckless.",
   "people": [
    {
     "name": "Joel Embiid",
     "role": "player"
    },
    {
     "name": "Mitchell Robinson",
     "role": "player"
    },
    {
     "name": "Donte DiVincenzo",
     "role": "player"
    },
    {
     "name": "Josh Hart",
     "role": "player"
    }
   ],
   "source_name": "USA Today via Yahoo",
   "source_url": "https://au.sports.yahoo.com/york-knicks-call-joel-embiids-053013638.html"
  },
  {
   "date": "2024-04-30",
   "type": "Flagrant foul",
   "summary": "2024 first round, Game 5: Embiid was assessed a flagrant 1 on review for a smack across Jalen Brunson's face. Philadelphia won 112-106 in overtime to force a Game 6.",
   "people": [
    {
     "name": "Joel Embiid",
     "role": "player"
    },
    {
     "name": "Jalen Brunson",
     "role": "player"
    }
   ],
   "source_name": "Sportskeeda",
   "source_url": "https://sportskeeda.com/basketball/news-playing-draymond-defense-playoffs-nba-fans-berate-dirty-joel-embiid-wild-smack-across-jalen-brunson-s-face"
  }
 ],
 "OKC-PHX": [
  {
   "date": "2026-04-23",
   "type": "Player vs. officials",
   "summary": "2026 first round: Devin Booker was fined $35,000 for public criticism of the officiating after Phoenix's Game 2 loss in Oklahoma City. The Thunder went on to sweep the series.",
   "people": [
    {
     "name": "Devin Booker",
     "role": "player"
    }
   ],
   "source_name": "AP via Daily Courier",
   "source_url": "https://www.dcourier.com/sports/suns-guard-devin-booker-fined-35-000-for-public-criticism-of-officials-after-thunder-game/article_98b8619b-7b84-51fb-8148-18cc75d54acf.html"
  }
 ],
 "OKC-WSH": [
  {
   "date": "2026-03-21",
   "type": "Fight / suspensions",
   "summary": "Late in the first half of Oklahoma City's 132-111 win, Jaylin Williams and Justin Champagnie began shoving under the basket, Anthony Gill and Ajay Mitchell joined in, and the scuffle spilled into the seating area. Champagnie, Williams, Mitchell and Cason Wallace were ejected. Mitchell and Champagnie were suspended one game each; Williams was fined $50,000 and Wallace and Gill $35,000 each.",
   "people": [
    {
     "name": "Justin Champagnie",
     "role": "player"
    },
    {
     "name": "Ajay Mitchell",
     "role": "player"
    },
    {
     "name": "Jaylin Williams",
     "role": "player"
    },
    {
     "name": "Cason Wallace",
     "role": "player"
    },
    {
     "name": "Anthony Gill",
     "role": "player"
    }
   ],
   "source_name": "NBA official release",
   "source_url": "https://official.nba.com/nba-announces-penalties-from-thunder-wizards-game"
  }
 ]
}'''


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
    return json.loads(REVENGE_JSON)


def load_rivalries():
    return json.loads(RIVALRY_JSON)


_rosters = {}


def roster_names(tid):
    """Lower-cased names of the players and head coach currently with a team."""
    if tid in _rosters:
        return _rosters[tid]
    try:
        data = get(f"{API}/teams/{tid}/roster")
    except Exception as e:
        print(f"WARNING: no roster for team {tid}: {e}")
        _rosters[tid] = None
        return None
    names, coaches = set(), []
    for a in data.get("athletes", []):
        for p in a.get("items", [a]):
            if p.get("displayName"):
                names.add(norm(p["displayName"]))
    n_players = len(names)
    for c in data.get("coach", []):
        full = norm(f'{c.get("firstName", "")} {c.get("lastName", "")}')
        if full:
            names.add(full)
            coaches.append(full.title())
    print(f"Roster check team {tid}: {n_players} players, coach: {', '.join(coaches) or 'NOT FOUND'}")
    _rosters[tid] = names
    return names


def rivalry_notes(a_team, h_team, rivalries):
    """Incidents for this matchup, kept only if an involved player is on either roster today."""
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
    for e in events:
        for c in e["competitions"][0]["competitors"]:
            roster_names(c["team"]["id"])
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
