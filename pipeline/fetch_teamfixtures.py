#!/usr/bin/env python3
"""Team-fixture EPG generator for team-dedicated channels.

The provider carries channels dedicated to ONE team ("NFL: Dallas Cowboys",
"Arsenal | EPL ˢᴰ", "Celtic TV", Serie A / LaLiga team feeds). For those the
team's own fixture list IS the correct schedule: emit one <programme> per
upcoming game at the real kickoff time (UTC). Between games the channel
honestly shows nothing.

This is the ONLY sports data that is deterministic from public sources.
Numbered multiplex slots ("MLB 01 | (Event Only)", "NHL Center Ice 03 |",
"UFC 01 |", "Sky Sports+ | Event 03") are deliberately NOT touched: their
slot->game assignment is panel-internal and unpublished, and games routinely
tie on start time — so any fill would show the wrong game. Wrong EPG is
worse than blank. (Verified 2026-08-19: MLB had 15 games but only 12 unique
start times; the pressure-test confirmed no public source publishes the
slot->game mapping.)

Sources: TheSportsDB eventsround.php (free key 123) for EPL/NFL/SPFL/
SerieA/LaLiga. Free key rate-limits ~23 req/min — we fetch only the rounds
covering today..today+28d, paced, with 429 backoff.

Outputs (3 files):
  <out.xml>     XMLTV, channel id = provider stream name (TiviMate name-fallback)
  <claim.json>  list of stream names we populated — build_mapping uses this to
                let these channels through is_non_linear() (their categories
                contain "EPL"/"NFL" which would otherwise drop them)
  <status.json> per-league counts

Usage: fetch_teamfixtures.py <streams.json> <out.xml> <claim.json> <status.json>
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import norm

UA_H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
SDB_KEY = os.environ.get('THESPORTSDB_KEY', '123')  # official free key (docs: /documentation)
SDB = 'https://www.thesportsdb.com/api/v1/json/{}/'.format(SDB_KEY)

# TheSportsDB league ids (verified 2026-08-19). Only leagues whose team channels
# actually appear in this provider's lineup.
SDB_LEAGUES = {
    'EPL': '4328',      # English Premier League
    'NFL': '4391',
    'SPFL': '4330',     # Scottish Premiership
    'SerieA': '4332',
    'LaLiga': '4335',
}

# category keyword -> league key
LEAGUE_BY_CATEGORY = {
    'EPL': 'EPL', 'PREMIER LEAGUE': 'EPL',
    'NFL': 'NFL',
    'SCOTTISH': 'SPFL', 'SPFL': 'SPFL',
    'SERIE A': 'SerieA',
    'LALIGA': 'LaLiga', 'LA LIGA': 'LaLiga',
}

# default programme duration (minutes) per league
DURATION = {'NFL': 210, 'EPL': 180, 'SPFL': 180, 'SerieA': 180, 'LaLiga': 180}

# provider name cleanup -> bare team name ("NFL: Dallas Cowboys" -> "Dallas Cowboys")
TEAM_STRIP_RE = re.compile(r'^(nfl|epl|spl|spfl|serie a|laliga|la liga)\s*:\s*', re.I)

# how far ahead to fetch fixtures (the guide is rebuilt daily)
HORIZON_DAYS = 28


def http_json(url, retries=3):
    """GET JSON with 429-aware backoff (TheSportsDB free key rate-limits)."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA_H)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode('utf-8', 'ignore'))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < retries:
                time.sleep(20 * (attempt + 1))
                continue
            if attempt < retries:
                time.sleep(2)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2)
    print(f'[teams] GET failed: {url} ({last})', file=sys.stderr)
    return None


def season_string(league):
    """Season string from today's date (no hardcoded years).

    European soccer seasons run Aug-May ("2026-2027"); NFL regular season
    Sep-Feb ("2026"). If we're in Jan-Jul the "current" season started last
    calendar year.
    """
    today = dt.date.today()
    if league == 'NFL':
        start = today.year if today.month >= 8 else today.year - 1
        return str(start)
    start = today.year if today.month >= 7 else today.year - 1
    return f'{start}-{start + 1}'


def sdb_events(lid, season):
    """Upcoming events for a league: paginated rounds covering today..+HORIZON.

    Rounds are chronological. Fetch rounds 1..N, stopping once a round's
    earliest date is beyond the horizon (we don't need fixtures further out
    for a daily guide). Rate-limit friendly: ~3-4 rounds per league.
    """
    today = dt.date.today()
    horizon = today + dt.timedelta(days=HORIZON_DAYS)
    seen = {}
    empty_streak = 0
    for r in range(1, 39):
        j = http_json(f'{SDB}eventsround.php?id={lid}&r={r}&s={season}')
        evs = (j or {}).get('events') or []
        if not evs:
            empty_streak += 1
            if empty_streak >= 2:
                break  # no more published rounds
            continue
        empty_streak = 0
        dates = sorted(set(e.get('dateEvent') or '' for e in evs if e.get('dateEvent')))
        for e in evs:
            de = e.get('dateEvent') or ''
            # per-event horizon bound: a round can span the horizon boundary
            # (one in-window event + later out-of-window events in the same
            # round); only in-window events belong (AUDIT-4: 80 leaked).
            if today.isoformat() <= de <= horizon.isoformat():
                seen[(e.get('idEvent'), de)] = e
        if dates and dates[0] > horizon.isoformat():
            break  # this round is entirely beyond the horizon
        time.sleep(0.6)  # stay under the free-key rate limit
    return list(seen.values())


def collect_team_channels(streams):
    """Return list of team-dedicated channel dicts: {name, team, league, cat, icon}.

    Detects by CATEGORY (stable) rather than name (channels can rename), so
    a renamed event channel still gets classified. Only team channels are
    returned — numbered slots and event hubs are skipped.
    """
    out = []
    for s in streams:
        name = (s.get('name') or '').strip()
        cat = (s.get('cat_name') or '').strip()
        if not name:
            continue
        league = None
        cat_u = cat.upper()
        for key, lg in LEAGUE_BY_CATEGORY.items():
            if key in cat_u:
                league = lg
                break
        if not league:
            # NFL team channels may also carry "NFL:" in the name
            if 'NFL' in name.upper() and 'NFL:' in name.upper():
                league = 'NFL'
        if not league:
            continue

        # skip numbered slots, event hubs, replays, networks, 24/7 loops
        ln = name.lower()
        if re.search(r'\bevent\b|\breplay\b', ln):
            continue
        if any(k in ln for k in ('hub', 'network', 'redzone', 'goal rush',
                                 'coupang', '24/7')):
            continue
        # numbered-slot names: "MLB 01 |", "NHL Center Ice 03 |", "UFC 01 |",
        # "Boxing 01 |", "Flo Hockey 01 |" — skip anything that is just a
        # number/event designator, not a team.
        if re.search(r'\b0?\d{1,2}\s*[|:)]', name):
            continue

        team = TEAM_STRIP_RE.sub('', name).strip(' |')
        # Club feeds are commonly named "Arsenal | EPL ˢᴰ" / "Chelsea | EPL
        # HD". Keep the stable portion before the pipe; category/quality
        # decorations must not become part of the team identity (AUDIT-6: all
        # 21 EPL clubs were otherwise detected but 0 claimed).
        if '|' in name:
            parts = [p.strip() for p in name.split('|') if p.strip()]
            if parts and parts[0].upper() in {'EPL','NFL','SPFL','SC','SERIE A','LALIGA','LA LIGA'} and len(parts) > 1:
                team = parts[1]
            elif parts:
                team = parts[0]
        team = re.sub(r'\s+(fctv|tv)\s*\d*$', '', team, flags=re.I).strip()
        team = re.sub(r'\s+[ˢᵈᴴᴰFHDfhdSD]+$', '', team).strip()
        # Club-branded broadcasters are not team fixture channels.
        if team.upper() in {'LFC','MUTV','LFC TV'}:
            continue
        if len(team) < 3:
            continue
        out.append({'name': name, 'stream_id': str(s.get('stream_id') or ''),
                    'team': team, 'league': league,
                    'cat': cat, 'icon': s.get('icon', '')})
    return out


def team_matches_event(team, ev):
    """Match a team channel only to the exact normalized fixture team name.

    Substring containment caused Dundee to receive Dundee United fixtures.
    Identity-sensitive sports matching must prefer blank over a wrong team's
    schedule; future naming exceptions belong in a reviewed alias table.

    Broadcaster channels (Celtic TV, Real Madrid TV) are club-branded linear
    channels, not fixture feeds — rejected before the name normalizer strips
    their distinguishing 'TV' token (AUDIT-7 P1-6).
    """
    t = norm(team)
    if not t:
        return False
    for side in (ev.get('strHomeTeam') or '', ev.get('strAwayTeam') or ''):
        if t == norm(side):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('streams')
    ap.add_argument('out')
    ap.add_argument('claim')
    ap.add_argument('status')
    args = ap.parse_args()

    streams = json.load(open(args.streams))
    chans = collect_team_channels(streams)
    print(f'[teams] {len(chans)} team-dedicated channels detected')
    if not chans:
        open(args.out, 'w').write('<?xml version="1.0"?>\n<tv></tv>\n')
        json.dump([], open(args.claim, 'w'))
        json.dump({'channels': 0, 'programmes': 0}, open(args.status, 'w'))
        return

    # fetch fixtures per league (dedupe by league; seasons computed from date)
    events = {}
    for league in sorted({c['league'] for c in chans}):
        lid = SDB_LEAGUES.get(league)
        if not lid:
            continue
        season = season_string(league)
        evs = sdb_events(lid, season)
        events[league] = evs
        print(f'[teams] {league} ({season}): {len(evs)} upcoming events (TheSportsDB)')
        time.sleep(1.0)

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="hermes-teamfixtures">\n']
    n_ch = n_p = 0
    covered = []
    claimed = []
    for c in chans:
        evs = events.get(c['league']) or []
        mine = [e for e in evs if team_matches_event(c['team'], e)]
        if not mine:
            continue
        cid = c['name']
        out.append('  <channel id={}>\n    <display-name>{}</display-name>\n  </channel>\n'
                   .format(quoteattr(cid), escape(cid)))
        n_ch += 1
        claimed.append(c['stream_id'])
        covered.append((c['stream_id'], c['league'], c['team']))
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
    json.dump(claimed, open(args.claim, 'w'))
    json.dump({'channels': n_ch, 'programmes': n_p, 'teams': covered},
              open(args.status, 'w'), indent=1)
    print(f'[teams] {n_ch} team channels, {n_p} fixture programmes -> {args.out}')
    print(f'[teams] claim list: {len(claimed)} channels -> {args.claim}')


if __name__ == '__main__':
    main()
