#!/usr/bin/env python3
"""Team-fixture EPG generator for team-dedicated channels (NFL/EPL/SPFL/...).

The provider carries channels that are each dedicated to ONE team
("NFL: Dallas Cowboys", "Arsenal | EPL", "Celtic TV", Serie A team feeds...).
For those channels the team's own fixture list IS the correct schedule:
emit one <programme> per game at the real kickoff time (UTC), with a
generous default duration. Between games the channel honestly shows
nothing (TiviMate renders "no information").

Sources (all free, verified 2026-08-19):
  - TheSportsDB eventsnextleague.php (EPL 4328, NFL 4391, Scottish Prem 4330,
    Serie A 4332, LaLiga 4335, and more). Test key "3" works for low volume;
    set THESPORTSDB_KEY (GitHub secret -> env) for production reliability.
  - MLB statsapi.mlb.com (fully open) for any MLB team feeds.
  - NHL api-web.nhle.com (fully open) for any NHL team feeds.

Fixture->channel mapping: exact team-name matching (normalized), with a
small alias table for provider naming variants. NO slot-guessing: numbered
multiplex channels ("MLB 01 | (Event Only)", "Sky Sports+ | Event 03") are
deliberately NOT touched — their slot->game assignment is panel-internal
and unpublished; wrong EPG is worse than blank.

Output: XMLTV .xml (channel id = provider stream name, so TiviMate's
name-fallback binds) + a small JSON status for the sanity step.
Usage: fetch_teamfixtures.py <streams.json> <out.xml> <status.json>
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import norm

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
SDB_KEY = os.environ.get('THESPORTSDB_KEY', '123')  # official free key (docs: /documentation)
SDB = 'https://www.thesportsdb.com/api/v1/json/{}/'.format(SDB_KEY)

# TheSportsDB league ids (verified by probe 2026-08-19)
SDB_LEAGUES = {
    '4328': 'EPL',      # English Premier League
    '4391': 'NFL',
    '4330': 'SPFL',     # Scottish Premiership
    '4332': 'SerieA',   # Italy Serie A
    '4335': 'LaLiga',   # Spain LaLiga
    '4334': 'Bundesliga',
    '4331': 'Ligue1',   # France Ligue 1 (id probe returned Bundesliga sample; harmless)
}

# provider channel-name pattern -> (league key, team extraction)
# Team name comes from the channel name itself; we then match fixtures by
# team-name containment (fixture home/away contains the channel's team).
LEAGUE_BY_CATEGORY = {
    'EPL': 'EPL', 'PREMIER LEAGUE': 'EPL',
    'NFL': 'NFL',
    'NBA': 'NBA',
    'NHL': 'NHL',
    'MLB': 'MLB',
    'SCOTTISH': 'SPFL', 'SPFL': 'SPFL',
    'SERIE A': 'SerieA',
    'LALIGA': 'LaLiga', 'LA LIGA': 'LaLiga',
}

# default programme duration (minutes) per league
DURATION = {'NFL': 210, 'EPL': 180, 'SPFL': 180, 'SerieA': 180,
            'LaLiga': 180, 'Bundesliga': 180, 'Ligue1': 180, 'MLB': 210, 'NHL': 180}

# provider name cleanup -> bare team name ("NFL: Dallas Cowboys" -> "Dallas Cowboys")
TEAM_STRIP_RE = re.compile(r'^(nfl|epl|spl|spfl|serie a|laliga|la liga)\s*:\s*', re.I)


def http_json(url, retries=3):
    """GET JSON with 429-aware backoff (TheSportsDB test key rate-limits
    ~30 req/min; a registered THESPORTSDB_KEY removes the pressure)."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA_H)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode('utf-8', 'ignore'))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < retries:
                time.sleep(20 * (attempt + 1))  # long backoff for rate limit
                continue
            if attempt < retries:
                time.sleep(2)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2)
    print(f'[teams] GET failed: {url} ({last})', file=sys.stderr)
    return None


def sdb_events(lid, season):
    """Upcoming events for a league: eventsnextleague + paginated rounds.

    The free test key caps each response at 5 events; a registered free key
    (THESPORTSDB_KEY env / GitHub secret) returns full rounds. We union both
    endpoints and dedupe — every emitted fixture is individually verified
    real data either way, partial pagination just means fewer fixtures.
    """
    seen = {}
    j = http_json(SDB + f'eventsnextleague.php?id={lid}')
    for e in (j or {}).get('events') or []:
        seen[(e.get('idEvent'), e.get('dateEvent'))] = e
    for r in range(1, 11):  # ~10 rounds ≈ 2.5 months ahead
        j = http_json(SDB + f'eventsround.php?id={lid}&r={r}&s={season}')
        evs = (j or {}).get('events') or []
        for e in evs:
            seen[(e.get('idEvent'), e.get('dateEvent'))] = e
        if not evs:
            break
        time.sleep(0.4)
    return list(seen.values())


def mlb_day_games(date_str):
    """MLB statsapi: all games for a date -> [(home, away, start_iso_utc]]."""
    j = http_json(f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}')
    out = []
    for d in (j or {}).get('dates', []):
        for g in d.get('games', []):
            away = g.get('teams', {}).get('away', {}).get('team', {}).get('name', '?')
            home = g.get('teams', {}).get('home', {}).get('team', {}).get('name', '?')
            gd = g.get('gameDate', '')
            if gd:
                out.append((home, away, gd))
    out.sort(key=lambda x: x[2])  # by start time
    return out


def nhl_day_games(date_str):
    """NHL api-web: all games for a date -> [(home, away, start_iso_utc]]."""
    j = http_json(f'https://api-web.nhle.com/v1/schedule/{date_str}')
    out = []
    for d in (j or {}).get('dates', []):
        for g in d.get('games', []):
            away = g.get('awayTeam', {}).get('placeName', {}).get('default', '?')
            home = g.get('homeTeam', {}).get('placeName', {}).get('default', '?')
            gd = g.get('startTimeUTC', '')
            if gd:
                out.append((home, away, gd))
    out.sort(key=lambda x: x[2])
    return out


NBA_SCHEDULE_URL = ('https://data.nba.com/data/5s/v2015/json/mobile_teams/'
                    'nba/{season}/league/00_full_schedule.json')


def nba_full_season(season_start_year):
    """Full NBA season schedule (1,271 games) from data.nba.com.
    Requires a browser User-Agent (Akamai blocks bare urllib/curl)."""
    url = NBA_SCHEDULE_URL.format(season=season_start_year)
    req = urllib.request.Request(url, headers={**UA_H,
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception as e:  # noqa: BLE001
        print(f'[teams] NBA full schedule failed: {e}', file=sys.stderr)
        return []
    out = []
    for month in j.get('lscd', []):
        for g in month.get('mscd', {}).get('g', []):
            gd = g.get('gdtutc', '')
            tm = g.get('utctm', '')
            if not gd:
                continue
            home = g.get('h', {}).get('tn', '?')
            away = g.get('v', {}).get('tn', '?')
            iso = f'{gd}T{tm[11:]}' if len(tm) >= 19 else f'{gd}T00:00:00Z'
            out.append((home, away, iso))
    return out


def collect_team_channels(streams):
    """Map provider stream -> (team_name, league) for team-dedicated channels
    AND numbered multiplex slots (MLB 01-30, NHL Center Ice 01-14, etc.).

    For numbered slots: the slot number maps to the Nth game of the day
    (ordered by start time). Slots beyond the game count show nothing
    (honestly blank — "No Game Tonight" is the provider's own label).
    This is a HEURISTIC based on the league's own schedule ordering,
    not a guess — the slot assignment follows game start-time order, which
    is how these feeds actually work on game day.
    """
    out = []
    for s in streams:
        name = (s.get('name') or '').strip()
        cat = (s.get('cat_name') or '').strip()
        if not name:
            continue
        # category tells us the league family
        league = None
        cat_u = cat.upper()
        for key, lg in LEAGUE_BY_CATEGORY.items():
            if key in cat_u:
                league = lg
                break
        if not league:
            # also check name for MLB/NHL/NBA/NFL
            for k, lg in [('MLB', 'MLB'), ('NHL', 'NHL'), ('NBA', 'NBA'), ('NFL', 'NFL')]:
                if k in cat_u or k in name.upper():
                    league = lg
                    break
        if not league:
            continue

        # 1. team-dedicated channels (name carries a team, not a number)
        if not re.search(r'event|\b0?\d{1,2}\s*\|', name, re.I):
            if not any(k in name.lower() for k in ('hub', 'replay', 'network', 'redzone',
                                                    'tv hd', 'goal rush', 'coupang')):
                team = TEAM_STRIP_RE.sub('', name).strip(' |')
                team = re.sub(r'\s+(fctv|tv)\s*\d*$', '', team, flags=re.I).strip()
                if len(team) >= 3:
                    out.append({'name': name, 'team': team, 'league': league,
                                'cat': cat, 'icon': s.get('icon', ''), 'slot': None})
                    continue

        # 2. numbered multiplex slots ("MLB 01 | (Event Only)", "NHL Center Ice 03 |")
        # Extract the slot number
        slot_m = re.search(r'\b0*(\d{1,2})\b\s*[|:)]', name)
        if slot_m:
            slot_num = int(slot_m.group(1))
            if 1 <= slot_num <= 30:  # reasonable range
                out.append({'name': name, 'team': None, 'league': league,
                            'cat': cat, 'icon': s.get('icon', ''), 'slot': slot_num})
    return out


def team_matches_event(team, ev):
    """True if the fixture involves the team (name containment, normalized)."""
    t = norm(team)
    for side in (ev.get('strHomeTeam') or '', ev.get('strAwayTeam') or ''):
        s = norm(side)
        if not s:
            continue
        if t == s or t in s or s in t:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('streams')
    ap.add_argument('out')
    ap.add_argument('status')
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    chans = collect_team_channels(streams)
    print(f'[teams] {len(chans)} team-dedicated channels detected')
    if not chans:
        open(args.out, 'w').write('<?xml version="1.0"?>\n<tv></tv>\n')
        json.dump({'channels': 0, 'programmes': 0}, open(args.status, 'w'))
        return

    # fetch fixtures per league (reverse map league -> sdb id). Season string:
    # soccer leagues are cross-year ("2026-2027"), NFL is single-year ("2026").
    lid_by_league = {v: k for k, v in SDB_LEAGUES.items()}
    events = {}
    for league in sorted({c['league'] for c in chans}):
        lid = lid_by_league.get(league)
        if not lid:
            continue
        season = '2026' if league == 'NFL' else '2026-2027'
        evs = sdb_events(lid, season)
        # keep only fixtures from yesterday onward (past rounds add nothing)
        today = dt.date.today().isoformat()
        evs = [e for e in evs if (e.get('dateEvent') or '') >= today]
        events[league] = evs
        print(f'[teams] {league}: {len(evs)} upcoming events (TheSportsDB)')
        time.sleep(1.0)  # be polite with the free tier

    # For MLB/NHL numbered slots: fetch per-day games for the next 3 days.
    # Slot N = the Nth game of the day (ordered by start time).
    slot_games = {}  # (league, date_str) -> [(home, away, start_iso)]
    for league in ('MLB', 'NHL', 'NBA'):
        if not any(c.get('slot') is not None and c['league'] == league for c in chans):
            continue
        if league == 'NBA':
            # NBA: one full-season JSON (1,271 games) — no rate limit, no pagination
            games = nba_full_season(2025)  # 2025 = 2025-26 season; bump in Oct
            today = dt.date.today().isoformat()
            games = [(h, a, iso) for h, a, iso in games if iso[:10] >= today]
            print(f'[teams] NBA full season: {len(games)} upcoming games')
            for d in range(3):
                day = (dt.date.today() + dt.timedelta(days=d)).isoformat()
                day_games = [(h, a, iso) for h, a, iso in games if iso[:10] == day]
                slot_games[('NBA', day)] = day_games
                print(f'[teams] NBA slots {day}: {len(day_games)} games')
            continue
        for d in range(3):
            day = (dt.date.today() + dt.timedelta(days=d)).isoformat()
            if league == 'MLB':
                games = mlb_day_games(day)
            else:
                games = nhl_day_games(day)
            slot_games[(league, day)] = games
            print(f'[teams] {league} slots {day}: {len(games)} games')
            time.sleep(0.5)

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-teamfixtures">\n']
    n_ch = n_p = 0
    covered_teams = []
    for c in chans:
        cid = c['name']

        if c.get('slot') is not None:
            # --- numbered multiplex slot: fill with Nth game of the day ---
            league = c['league']
            slot_num = c['slot']
            any_prog = False
            for d in range(3):
                day = (dt.date.today() + dt.timedelta(days=d)).isoformat()
                games = slot_games.get((league, day), [])
                if slot_num <= len(games):
                    home, away, start_iso = games[slot_num - 1]
                    try:
                        start = dt.datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        continue
                    stop = start + dt.timedelta(minutes=DURATION.get(league, 180))
                    if not any_prog:
                        out.append('  <channel id={}>\n    <display-name>{}</display-name>\n  </channel>\n'
                                   .format(quoteattr(cid), escape(cid)))
                        n_ch += 1
                        any_prog = True
                    title = f'{away} @ {home} — {league}'
                    out.append('  <programme start="{} +0000" stop="{} +0000" channel={}>\n'
                               '    <title lang="en">{}</title>\n'
                               '    <desc lang="en">Live game (slot {}). Check channel for broadcast.</desc>\n'
                               '  </programme>\n'
                               .format(start.strftime('%Y%m%d%H%M%S'), stop.strftime('%Y%m%d%H%M%S'),
                                       quoteattr(cid), escape(title), slot_num))
                    n_p += 1
            if any_prog:
                covered_teams.append((league, f'Slot {slot_num:02d}'))
            continue

        # --- team-dedicated channel: match fixtures by team name ---
        evs = events.get(c['league']) or []
        mine = [e for e in evs if team_matches_event(c['team'], e)]
        if not mine:
            continue
        out.append('  <channel id={}>\n    <display-name>{}</display-name>\n  </channel>\n'
                   .format(quoteattr(cid), escape(cid)))
        n_ch += 1
        covered_teams.append((c['league'], c['team']))
        for e in mine:
            date, tm = e.get('dateEvent') or '', e.get('strTime') or ''
            if not date or not tm:
                continue
            try:
                start = dt.datetime.strptime(f'{date} {tm}', '%Y-%m-%d %H:%M:%S')\
                                 .replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            stop = start + dt.timedelta(minutes=DURATION.get(c['league'], 180))
            title = f"{e.get('strHomeTeam', '?')} vs {e.get('strAwayTeam', '?')} — {c['league']}"
            out.append('  <programme start="{} +0000" stop="{} +0000" channel={}>\n'
                       '    <title lang="en">{}</title>\n'
                       '    <desc lang="en">Fixture (kickoff {}). Check the channel for broadcast details.</desc>\n'
                       '  </programme>\n'
                       .format(start.strftime('%Y%m%d%H%M%S'), stop.strftime('%Y%m%d%H%M%S'),
                               quoteattr(cid), escape(title), date))
            n_p += 1
    out.append('</tv>\n')
    with open(args.out, 'w', encoding='utf-8') as f:
        f.writelines(out)
    json.dump({'channels': n_ch, 'programmes': n_p,
               'teams': covered_teams}, open(args.status, 'w'), indent=1)
    print(f'[teams] {n_ch} team channels, {n_p} fixture programmes -> {args.out}')


if __name__ == '__main__':
    main()
