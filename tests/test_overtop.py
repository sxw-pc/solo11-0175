"""Acceptance: evidence-graded overtopping determination.

The alias fixture holds three 15-minute upload pages that all read
1.2 m against a 1.4 m crest.  Two legal continuous histories fit them:
A(t)=1.2+0.3*sin^2(pi*t/15) peaks at 1.5 m twice (t=7.5 and t=22.5),
B(t)=1.2-0.3*sin^2(pi*t/15) never exceeds 1.2 m.  No amount of
interpolation over the three pages can tell them apart, so the
publication must grade the gaps instead of filling them.
"""
import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from canalright.stage import import_gauge, import_stage
from canalright.overtop import import_site, import_outage, import_buffer, import_mark, import_caldev
from canalright.store import connect, report
from canalright.audit import audit

ALIAS = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'overtop-alias.json').read_text())

T = '2026-11-22T20:%02d:00Z'


def parse(at):
    return datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()


def write(tmp, name, doc):
    p = tmp / f'{name}.json'
    p.write_text(json.dumps(doc))
    return p


def seed_alias(tmp, db, pages=None):
    import_gauge(db, write(tmp, 'gauge', ALIAS['gauge']))
    import_site(db, write(tmp, 'site', ALIAS['site']))
    for i, page in enumerate(ALIAS['pages'] if pages is None else pages):
        import_stage(db, write(tmp, f'page{i}', page))


def latest_pub(db):
    rows = report(db)['overtop_publications']
    assert rows, 'expected at least one publication'
    row = rows[-1]
    return {**row, 'windows': json.loads(row['windows']),
            'closures': json.loads(row['closures']), 'pending': json.loads(row['pending'])}


def state_at(pub, at):
    t = parse(at)
    for w in pub['windows']:
        if parse(w['start']) <= t < parse(w['end']):
            return w['state']
    raise AssertionError(f'no window covers {at}')


def covers(interval, at):
    return parse(interval['start']) <= parse(at) <= parse(interval['end'])


def test_alias_pages_leave_gaps_undetermined(tmp_path):
    db = tmp_path / 'a.sqlite'
    seed_alias(tmp_path, db)
    # acceptance 1: the provisional warning still fires on the 1.2 m page
    assert report(db)['flood_warnings'][0]['status'] == 'high_stage'
    pub = latest_pub(db)
    # linear interpolation between the three 1.2 m pages would call the
    # whole half hour safe; reading high_stage as overtopping would call
    # it flooded.  The honest answer: two undetermined gaps, 0..2 events.
    assert (pub['events_lo'], pub['events_hi']) == (0, 2)
    assert pub['closures'] == [] and pub['reopen_at'] is None
    assert len(pub['pending']) == 2
    assert state_at(pub, '2026-11-22T20:07:30Z') == 'undetermined'
    assert state_at(pub, '2026-11-22T20:22:30Z') == 'undetermined'
    assert state_at(pub, '2026-11-22T20:15:00Z') == 'clear'
    assert state_at(pub, '2026-11-22T20:00:30Z') == 'clear'


def test_alias_worlds_share_originals_but_diverge(tmp_path):
    db_a, db_b = tmp_path / 'a.sqlite', tmp_path / 'b.sqlite'
    seed_alias(tmp_path, db_a)
    for i, bp in enumerate(ALIAS['buffer_a']):
        import_buffer(db_a, write(tmp_path, f'ba{i}', bp))
    seed_alias(tmp_path, db_b)
    for i, bp in enumerate(ALIAS['buffer_b']):
        import_buffer(db_b, write(tmp_path, f'bb{i}', bp))

    def originals(db):
        with closing(connect(db)) as c:
            return [(r['id'], r['sha']) for r in c.execute(
                "SELECT id, sha FROM evidence WHERE id IN ('P0','P15','P30') ORDER BY id")]

    # acceptance 2: the three originals and the high_stage outcome are
    # byte-identical across worlds, yet A overtopped twice and B never did
    assert originals(db_a) == originals(db_b)
    assert report(db_a)['flood_warnings'] == report(db_b)['flood_warnings']
    assert report(db_a)['flood_warnings'][0]['status'] == 'high_stage'
    pa, pb = latest_pub(db_a), latest_pub(db_b)
    assert (pa['events_lo'], pa['events_hi']) == (2, 2)
    assert (pb['events_lo'], pb['events_hi']) == (0, 0)
    assert len(pa['closures']) == 2 and pb['closures'] == []
    assert any(covers(c, '2026-11-22T20:07:30Z') for c in pa['closures'])
    assert any(covers(c, '2026-11-22T20:22:30Z') for c in pa['closures'])
    assert state_at(pa, '2026-11-22T20:15:00Z') == 'clear'
    assert all(w['state'] == 'clear' for w in pb['windows'])
    # the complete 1-minute buffer certifies the recession minute
    assert pa['reopen_at'] is not None and pb['reopen_at'] is None
    assert parse('2026-11-22T20:25:00Z') < parse(pa['reopen_at']) < parse('2026-11-22T20:26:00Z')
    assert audit(db_a)['status'] == 'matched'
    assert audit(db_b)['status'] == 'matched'


def test_broken_buffer_sequence_cannot_certify_events(tmp_path):
    db = tmp_path / 'x.sqlite'
    seed_alias(tmp_path, db)
    # only seq 1 of 2 recovered: the second peak's window stays pending
    import_buffer(db, write(tmp_path, 'ba1', ALIAS['buffer_a'][0]))
    pub = latest_pub(db)
    assert (pub['events_lo'], pub['events_hi']) == (1, 2)
    assert len(pub['closures']) == 1
    assert state_at(pub, '2026-11-22T20:22:30Z') == 'undetermined'
    assert any(covers(w, '2026-11-22T20:22:30Z') for w in pub['pending'])


def test_crest_mark_proves_event_without_minute_reopen(tmp_path):
    db = tmp_path / 'm.sqlite'
    seed_alias(tmp_path, db)
    import_mark(db, write(tmp_path, 'mark', ALIAS['mark']))
    pub = latest_pub(db)
    # acceptance 4: from the same three pages, the sealed 1.5 m mark proves
    # at least one overtopping but cannot split the two peaks, localize a
    # closure, or certify a minute-level reopening time
    assert (pub['events_lo'], pub['events_hi']) == (1, 2)
    assert pub['closures'] == [] and pub['reopen_at'] is None
    assert len(pub['pending']) == 2
    assert audit(db)['status'] == 'matched'


def test_outage_gap_is_not_safe_and_recovery_revises_it(tmp_path):
    db = tmp_path / 'o.sqlite'
    seed_alias(tmp_path, db)
    import_outage(db, write(tmp_path, 'outage', {
        'kind': 'stage_outage', 'id': 'O1', 'site_id': 'GS1',
        'start_at': '2026-11-22T20:32:00Z', 'end_at': '2026-11-22T20:47:00Z'}))
    pub = latest_pub(db)
    # acceptance 3: the blackout is not a safe period
    assert state_at(pub, '2026-11-22T20:40:00Z') == 'undetermined'
    assert (pub['events_lo'], pub['events_hi']) == (0, 3)
    # the gauge's signed local buffer kept running on battery
    samples = [{'at': T % m, 'height_m': 1.2} for m in range(30, 48)]
    import_buffer(db, write(tmp_path, 'rec', {
        'kind': 'stage_buffer', 'id': 'R1', 'site_id': 'GS1', 'seq': 1,
        'start_at': T % 30, 'end_at': T % 47, 'cadence_sec': 60,
        'samples': samples, 'signed_by': 'gauge-hsm-1', 'signature': 'hsm-r1'}))
    pub = latest_pub(db)
    assert state_at(pub, '2026-11-22T20:40:00Z') == 'clear'
    assert (pub['events_lo'], pub['events_hi']) == (0, 2)
    # the recovery revised only the window it covers
    assert any(covers(w, '2026-11-22T20:22:30Z') for w in pub['pending'])


def test_outage_forces_undetermined_even_inside_calm_envelope(tmp_path):
    db = tmp_path / 'f.sqlite'
    import_gauge(db, write(tmp_path, 'g2', {'kind': 'stage_gauge', 'id': 'G2', 'gauge_id': 'G2',
                                            'canal_id': 'C2', 'bank': 'west'}))
    import_site(db, write(tmp_path, 's2', dict(ALIAS['site'], id='site-gs2', site_id='GS2', gauge_id='G2')))
    import_stage(db, write(tmp_path, 'q1', {'kind': 'stage_reading', 'id': 'Q1', 'gauge_id': 'G2',
                                            'at': T % 30, 'height_m': 1.2}))
    import_stage(db, write(tmp_path, 'q2', {'kind': 'stage_reading', 'id': 'Q2', 'gauge_id': 'G2',
                                            'at': T % 34, 'height_m': 1.2}))
    # four minutes apart at 1.2 m: the slew envelope alone would clear it
    assert state_at(latest_pub(db), '2026-11-22T20:32:00Z') == 'clear'
    import_outage(db, write(tmp_path, 'o2', {
        'kind': 'stage_outage', 'id': 'O2', 'site_id': 'GS2',
        'start_at': '2026-11-22T20:31:00Z', 'end_at': '2026-11-22T20:33:00Z'}))
    pub = latest_pub(db)
    assert state_at(pub, '2026-11-22T20:32:00Z') == 'undetermined'
    assert state_at(pub, '2026-11-22T20:30:30Z') == 'clear'


def test_shared_calibration_deviation_removes_false_certainty(tmp_path):
    db = tmp_path / 'c.sqlite'
    seed_alias(tmp_path, db)
    import_caldev(db, write(tmp_path, 'cal', {
        'kind': 'stage_caldev', 'id': 'K1', 'site_id': 'GS1',
        'valid_from': '2026-11-22T19:00:00Z', 'valid_to': '2026-11-22T21:00:00Z',
        'max_offset_m': 0.25}))
    pub = latest_pub(db)
    # acceptance 3: one pending elevation deviation shifts the whole
    # segment jointly -- no window may be certified clear even though
    # every page reads 1.2 m, and none may be certified overtopped either
    assert pub['windows'] and all(w['state'] == 'undetermined' for w in pub['windows'])
    assert (pub['events_lo'], pub['events_hi']) == (0, 1)
    assert audit(db)['status'] == 'matched'


def test_missing_upload_page_is_not_interpolated(tmp_path):
    db = tmp_path / 'g.sqlite'
    seed_alias(tmp_path, db, pages=[ALIAS['pages'][0], ALIAS['pages'][2]])
    pub = latest_pub(db)
    # the 30-minute gap is one pending segment, not a filled-in line
    assert state_at(pub, '2026-11-22T20:15:00Z') == 'undetermined'
    assert (pub['events_lo'], pub['events_hi']) == (0, 1)


def test_recovered_buffer_revises_only_covered_window(tmp_path):
    db = tmp_path / 'r.sqlite'
    seed_alias(tmp_path, db)
    before = latest_pub(db)
    import_buffer(db, write(tmp_path, 'ba1', ALIAS['buffer_a'][0]))
    mid = latest_pub(db)
    assert mid['version'] == before['version'] + 1
    assert len(mid['closures']) == 1
    # the window the new page does not cover is carried over byte-identically
    assert mid['pending'][-1] == before['pending'][-1]
    import_buffer(db, write(tmp_path, 'ba2', ALIAS['buffer_a'][1]))
    final = latest_pub(db)
    assert (final['events_lo'], final['events_hi']) == (2, 2)
    versions = [p['version'] for p in report(db)['overtop_publications']]
    assert len(versions) >= 3 and versions == sorted(set(versions))


def test_publications_keep_evidence_version_and_raw_bytes(tmp_path):
    db = tmp_path / 'p.sqlite'
    seed_alias(tmp_path, db)
    for i, bp in enumerate(ALIAS['buffer_a']):
        import_buffer(db, write(tmp_path, f'ba{i}', bp))
    pubs = report(db)['overtop_publications']
    assert len(pubs) >= 3
    with closing(connect(db)) as c:
        raws = {(r['site_id'], r['version']): (r['sha'], r['raw'], r['evidence_seq'])
                for r in c.execute('SELECT site_id, version, sha, raw, evidence_seq FROM overtop_publication')}
        max_seq = c.execute('SELECT MAX(seq) FROM evidence').fetchone()[0]
    # acceptance 5: every version keeps its evidence version and raw bytes
    for p in pubs:
        sha, raw, seq = raws[(p['site_id'], p['version'])]
        assert hashlib.sha256(raw).hexdigest() == sha == p['sha']
        assert json.loads(raw)['windows'] == json.loads(p['windows'])
        assert seq == p['evidence_seq']
    assert pubs[-1]['evidence_seq'] == max_seq
    # old warnings and publication decisions stay reviewable
    kinds = {(d['object_type'], d['status']) for d in report(db)['decisions']}
    assert ('flood_warning', 'high_stage') in kinds
    assert any(k[0] == 'overtop_publication' for k in kinds)
    assert audit(db)['status'] == 'matched'


def test_buffer_and_mark_validation(tmp_path):
    db = tmp_path / 'v.sqlite'
    seed_alias(tmp_path, db)
    with pytest.raises(ValueError, match='untrusted_buffer_page'):
        import_buffer(db, write(tmp_path, 'bx', dict(ALIAS['buffer_a'][0], id='BX', signed_by='intern')))
    with pytest.raises(ValueError, match='buffer_page_incomplete'):
        import_buffer(db, write(tmp_path, 'by', dict(ALIAS['buffer_a'][0], id='BY',
                                                     samples=ALIAS['buffer_a'][0]['samples'][:10])))
    clash = dict(ALIAS['buffer_a'][0], id='BZ', seq=9,
                 samples=[dict(s, height_m=1.3) if s['at'].endswith('20:00:00Z') else s
                          for s in ALIAS['buffer_a'][0]['samples']])
    with pytest.raises(ValueError, match='sample_conflict'):
        import_buffer(db, write(tmp_path, 'bz', clash))
    import_buffer(db, write(tmp_path, 'ba1', ALIAS['buffer_a'][0]))
    with pytest.raises(ValueError, match='buffer_seq_conflict'):
        import_buffer(db, write(tmp_path, 'dup', dict(ALIAS['buffer_a'][0], id='BA1B')))
    with pytest.raises(ValueError, match='crest_mark_conflict'):
        import_mark(db, write(tmp_path, 'm9', dict(ALIAS['mark'], id='M9', mark_m=1.9)))
    with pytest.raises(ValueError, match='invalid_crest_mark'):
        import_mark(db, write(tmp_path, 'm8', dict(ALIAS['mark'], id='M8', witness='rescue-lead')))
    # replay is idempotent and does not republish
    before = len(report(db)['overtop_publications'])
    assert import_buffer(db, write(tmp_path, 'again', ALIAS['buffer_a'][0]))['replayed'] is True
    assert len(report(db)['overtop_publications']) == before
