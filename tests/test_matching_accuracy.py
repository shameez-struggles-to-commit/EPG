"""Identity ambiguity regressions: file order is not feed evidence."""
import json
import subprocess
import sys
from pathlib import Path

from pipeline.matcher import SourceIndex, norm
from pipeline.build_pipeline import _is_placeholder_title, material_title_conflict

ROOT = Path(__file__).resolve().parents[1]


def mapping(tmp_path, streams, indexes, callsigns=None):
    for name, value in [('streams', streams), ('sources_index', indexes),
                        ('callsigns', callsigns or {})]:
        (tmp_path / (name + '.json')).write_text(json.dumps(value))
    cmd = [sys.executable, str(ROOT / 'pipeline/build_mapping.py'),
           '--streams', str(tmp_path / 'streams.json'),
           '--sources-index', str(tmp_path / 'sources_index.json'),
           '--callsigns', str(tmp_path / 'callsigns.json'),
           '-o', str(tmp_path / 'mapping.json')]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads((tmp_path / 'mapping.json').read_text())


def stream(name, sid='1', country='UK'):
    return {'name': name, 'stream_id': sid, 'cat_name': country + ' | General',
            'epg_channel_id': ''}


def candidates(result, sid='1'):
    return result.get(sid, {}).get('candidates', [])


def test_plus_service_does_not_normalize_to_base():
    assert norm('Movies 24+') != norm('Movies 24')
    assert norm('Movies 24 plus1') == norm('Movies 24 +1')
    assert norm('Movies24+1') != norm('Movies24')


def test_base_service_never_chooses_plus_service_by_file_order(tmp_path):
    idx = SourceIndex()
    idx.add('Movies24+', 'Movies24+.uk')
    idx.add('Movies24', 'Movies24.uk')
    result = mapping(tmp_path, [stream('Movies24')], {'epgshare01:UK1': idx.by_name})
    assert [(c['source_id'], c['method']) for c in candidates(result)] == [('Movies24.uk', 'exact')]


def test_multi_id_exact_and_alias_are_rejected_across_sources(tmp_path):
    result = mapping(tmp_path, [stream('Movies24'), stream('Sony TV Asia', '2', 'IN')], {
        'epgshare01:UK1': {norm('Movies24'): ['variant-a', 'variant-b']},
        'tvepg': {norm('Sony TV'): ['sony-a', 'sony-b']},
    })
    assert candidates(result, '1') == []
    assert candidates(result, '2') == []


def test_provider_name_bucket_does_not_pick_last_id(tmp_path):
    (tmp_path / 'provider_index.json').write_text(json.dumps({
        'ids': {'alpha.gb': 'Alpha TV', 'beta.gb': 'Alpha TV'},
    }))
    # Mapping helper supplies this optional index only for this test.
    for name, value in [('streams', [stream('Alpha TV')]), ('sources_index', {})]:
        (tmp_path / (name + '.json')).write_text(json.dumps(value))
    subprocess.run([sys.executable, str(ROOT / 'pipeline/build_mapping.py'),
                    '--streams', str(tmp_path / 'streams.json'),
                    '--sources-index', str(tmp_path / 'sources_index.json'),
                    '--provider-index', str(tmp_path / 'provider_index.json'),
                    '-o', str(tmp_path / 'mapping.json')], check=True, capture_output=True)
    assert candidates(json.loads((tmp_path / 'mapping.json').read_text())) == []


def test_plus_one_does_not_fuzzy_to_unrelated_plus_one_service(tmp_path):
    result = mapping(tmp_path, [stream('Channel 5 +1')], {
        'epgshare01:UK1': {norm('5STAR +1'): ['5STAR+1.uk']},
    })
    assert candidates(result) == []


def test_multi_id_fuzzy_bucket_is_not_picked_by_source_order(tmp_path):
    result = mapping(tmp_path, [stream('Movies 24 Prime Extra')], {
        'epgshare01:UK1': {norm('Movies 24 Prime'): ['one', 'two']},
    })
    assert candidates(result) == []


def test_ambiguous_exact_does_not_fall_through_to_numbered_fuzzy(tmp_path):
    result = mapping(tmp_path, [stream('Sky Sport Bundesliga', country='DE')], {
        'epgshare01:DE1': {
            norm('Sky Sport Bundesliga'): ['bundle-a', 'bundle-b'],
            norm('Sky Sport Bundesliga 1'): ['numbered-one'],
        },
    })
    assert candidates(result) == []


def test_bare_callsign_does_not_pick_dt2_before_dt(tmp_path):
    result = mapping(tmp_path, [stream('FOX: TN | Nashville | WZTV', country='US')], {}, {
        'epgshare01:US_LOCALS1': {'wztv': ['WZTV-DT2', 'WZTV-DT']},
    })
    assert candidates(result) == []


def test_explicit_subchannel_can_resolve_its_own_id(tmp_path):
    result = mapping(tmp_path, [stream('FOX: TN | Nashville | WZTV-DT2', country='US')], {}, {
        'epgshare01:US_LOCALS1': {'wztv': ['WZTV-DT2', 'WZTV-DT']},
    })
    assert [(c['source_id'], c['method']) for c in candidates(result)] == [('WZTV-DT2', 'callsign')]


def test_unknown_programme_labels_only_are_filler():
    for title in ('To Be Announced', '  TO BE ANNOUNCED  ', 'TBA'):
        assert _is_placeholder_title(title)
    for title in ('To Be Announced: News Special', 'News', 'Shopping', 'Home Shopping Live'):
        assert not _is_placeholder_title(title)
    assert not material_title_conflict(['To Be Announced', 'eZwell Massage Mattress'])
    assert material_title_conflict(['News', 'eZwell Massage Mattress'])
