#!/usr/bin/env python3
import json, re, sys
sys.path.insert(0, '/Users/shameez/workspace/epg/pipeline')
from build_mapping import is_non_linear
streams = json.load(open('/Users/shameez/workspace/epg/data/streams.json'))
pk = [s for s in streams if re.match(r'^PK\s*\|', s.get('cat_name', ''))]
lin = [s for s in pk if not is_non_linear(s.get('cat_name', ''), s.get('name', ''))]
FULL = {'24 News', 'ARY Digital Asia', 'ARY Musik', 'ARY News', 'ARY QTV', 'ARY Zindagi',
        'Aaj Entertainment', 'Express Entertainment', 'Express News', 'Geo Kahani', 'Geo TV',
        'Har Pal Geo', 'Hum News', 'Hum TV Europe', 'MTA 1 World', 'Noor TV', 'Peace TV',
        'Prime Asia TV', 'Hum Sitaray', 'TV One Global', 'Madani Channel Urdu', 'Hum Masala'}
PART = {'Geo News', 'Samaa TV', 'Abb Takk', 'News One', 'Duniya News'}
REJ = {'ATV', 'Capital TV', 'Grace Network'}
blank = {s['name'] for s in lin if s['name'] not in FULL and s['name'] not in PART and s['name'] not in REJ}
print("projected blank names:", len(blank))
NAYA = {'PTV Home', 'PTV News', 'A Plus', 'Bol Entertainment', 'Bol News', 'Dawn News',
        'Kay 2', '8XM', 'Jalwa', 'Filmax', 'Apna Channel', 'Geo Tez', 'KTN', 'AVT Khyber', 'City 41'}
PTV = {'PTV Home', 'PTV News', 'PTV Sports', 'PTV World', 'PTV Global'}
EPGBEST = {'Dawn News', 'Khyber News', 'Urdu 1', 'Geo Tez', 'KTN News', 'PTV Global',
           'APNA', 'Abb Takk', 'News One'}
naya_blank = NAYA & blank
ptv_blank = PTV & blank
eb_blank = EPGBEST & blank
print("NAYA names still blank:", len(naya_blank), sorted(naya_blank))
print("PTV-exit names still blank:", len(ptv_blank), sorted(ptv_blank))
print("epg.best names still blank:", len(eb_blank), sorted(eb_blank))
recoverable = naya_blank | ptv_blank | eb_blank
residual = blank - recoverable
print("union recoverable:", len(recoverable))
print("RESIDUAL:", len(residual))
print(sorted(residual))
