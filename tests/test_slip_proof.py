import json
from pathlib import Path

import pytest

from canalright.tilt import import_sensor, import_sensor_zero, import_tilt
from canalright import slip_proof
from canalright.store import report, connect
from canalright.audit import audit

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'bank-thermal-slip.json'
HANDLERS = {
    'bank_sensor': import_sensor,
    'bank_sensor_zero': import_sensor_zero,
    'bank_tilt': import_tilt,
    'bank_thermal_batch': slip_proof.import_thermal_batch,
    'bank_bracket': slip_proof.import_bracket,
    'bank_temperature': slip_proof.import_temperature,
    'bank_benchmark': slip_proof.import_benchmark,
    'bank_total_station': slip_proof.import_total_station,
    'bank_oven_calibration': slip_proof.import_oven_calibration,
    'bank_photo': slip_proof.import_photo,
    'bank_field_flag': slip_proof.import_field_flag,
    'bank_slip_review': slip_proof.review_segment,
    'bank_repair_certificate': slip_proof.issue_repair_certificate,
}


def load_fixture():
    with FIXTURE.open(encoding='utf-8') as fh:
        return json.load(fh)


def put(tmp, doc):
    path = tmp / f"{doc['id']}.json"
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def ingest(db, tmp, doc):
    return HANDLERS[doc['kind']](db, put(tmp, doc))


def build_base_db(tmp_path, name='case.sqlite', pages_dir='pages'):
    """The two worlds share identical raw pages, temperatures and old notices."""
    fx = load_fixture()
    db = tmp_path / name
    tmp = tmp_path / pages_dir
    tmp.mkdir()
    for doc in fx['pages']:
        ingest(db, tmp, doc)
    return db, tmp, fx


def review(db, tmp, fx, segment):
    doc = next(d for d in fx['reviews'] if d['segment'] == segment)
    return slip_proof.review_segment(db, put(tmp, dict(doc, id=f"{doc['id']}-{segment}-v1")))


def test_n1_one_degree_reproduces_old_repair_review(tmp_path):
    db, tmp, fx = build_base_db(tmp_path)
    # The last N1 page reads exactly 1.0 degree, so the provisional rule fires.
    advice = report(db)['bank_advice']
    n1 = next(r for r in advice if r['segment'] == 'N1')
    assert n1['status'] == 'repair_review' and n1['tilt_deg'] == 1.0
    # Without temperature proof the raw 1 degree cannot become a construction order.
    verdict = review(db, tmp, fx, 'N1')
    assert verdict['status'] == 'pending_review'
    assert verdict['closed_length_m'] == 0
    assert verdict['revisit_sensors'] == ['N1-T01']
    stored = report(db)['bank_slip_reviews']
    assert stored[0]['version'] == 1
    # The old advice row remains readable through the projection.
    assert report(db)['bank_advice'][0]['tilt_deg'] == 1.0


def test_same_raw_pages_keep_two_physical_worlds_open(tmp_path):
    """World alpha (k=0.1, no slip) and beta (k=0, slip) share every raw page."""
    db_a, tmp_a, fx_a = build_base_db(tmp_path, 'alpha.sqlite', 'pages-a')
    db_b, tmp_b, fx_b = build_base_db(tmp_path, 'beta.sqlite', 'pages-b')
    va = review(db_a, tmp_a, fx_a, 'N1')
    vb = review(db_b, tmp_b, fx_b, 'N1')
    # The feasible interval is exactly s in [0, 1]: both worlds survive.
    assert (va['s_min'], va['s_max']) == (0.0, 1.0)
    assert (vb['s_min'], vb['s_max']) == (0.0, 1.0)
    assert va['status'] == vb['status'] == 'pending_review'
    # Neither the naive nominal-coefficient patch nor the raw threshold closes.
    assert va['closed_length_m'] == vb['closed_length_m'] == 0
    assert report(db_a)['bank_tilts'] == report(db_b)['bank_tilts']
    assert report(db_a)['bank_temperatures'] == report(db_b)['bank_temperatures']


def test_route1_bedrock_total_station_decides_n1_but_not_n2(tmp_path):
    fx = load_fixture()
    alpha, beta = tmp_path / 'alpha.sqlite', tmp_path / 'beta.sqlite'
    ta, tb = tmp_path / 'a', tmp_path / 'b'
    ta.mkdir(); tb.mkdir()
    for doc in fx['pages']:
        ingest(alpha, ta, doc)
        ingest(beta, tb, doc)
    ts_alpha = next(d for d in fx['route1_total_station'] if d['id'] == 'TS-ALPHA')
    ts_beta = next(d for d in fx['route1_total_station'] if d['id'] == 'TS-BETA')
    slip_proof.import_total_station(alpha, put(ta, ts_alpha))
    slip_proof.import_total_station(beta, put(tb, ts_beta))
    ra = slip_proof.review_segment(alpha, put(ta, dict(fx['reviews'][0], id='V1a')))
    rb = slip_proof.review_segment(beta, put(tb, dict(fx['reviews'][0], id='V1b')))
    assert ra['status'] == 'thermal_only' and ra['closed_length_m'] == 0
    assert rb['status'] == 'proven_displacement' and rb['closed_length_m'] == 120
    # Only beta may draw a repair certificate on the proven displacement review.
    cert = fx['repair_certificates'][0]
    slip_proof.issue_repair_certificate(beta, put(tb, cert))
    with pytest.raises(ValueError, match='no_proven_displacement_review'):
        slip_proof.issue_repair_certificate(alpha, put(ta, dict(cert, id='C1x', cert_id='RC-N1-NO')))
    # The total station proved N1 only; N2's thermal coefficient is still uncalibrated.
    n2_a = slip_proof.review_segment(alpha, put(ta, dict(fx['reviews'][1], id='V2a')))
    assert n2_a['status'] == 'pending_review'
    assert 'N2-T01' in n2_a['revisit_sensors']


def test_route2_oven_calibration_pins_shared_batch_for_both_segments(tmp_path):
    fx = load_fixture()
    alpha, beta = tmp_path / 'alpha.sqlite', tmp_path / 'beta.sqlite'
    ta, tb = tmp_path / 'a', tmp_path / 'b'
    ta.mkdir(); tb.mkdir()
    for doc in fx['pages']:
        ingest(alpha, ta, doc)
        ingest(beta, tb, doc)
    ov_alpha = next(d for d in fx['route2_oven_calibration'] if d['id'] == 'OV-ALPHA')
    ov_beta = next(d for d in fx['route2_oven_calibration'] if d['id'] == 'OV-BETA')
    slip_proof.import_oven_calibration(alpha, put(ta, ov_alpha))
    slip_proof.import_oven_calibration(beta, put(tb, ov_beta))
    # One batch table is shared: the calibrated k applies to N1 AND N2 together.
    assert report(alpha)['bank_batches'][0]['k_min'] == report(alpha)['bank_batches'][0]['k_max'] == 0.1
    ra = slip_proof.review_segment(alpha, put(ta, dict(fx['reviews'][0], id='V1a')))
    rb = slip_proof.review_segment(beta, put(tb, dict(fx['reviews'][0], id='V1b')))
    assert (ra['status'], ra['closed_length_m']) == ('thermal_only', 0)
    assert (rb['status'], rb['closed_length_m']) == ('proven_displacement', 120)
    n2_a = slip_proof.review_segment(alpha, put(ta, dict(fx['reviews'][1], id='V2a')))
    n2_b = slip_proof.review_segment(beta, put(tb, dict(fx['reviews'][1], id='V2b')))
    # k=0.1 removes the whole N2 thermal term (0.6 - 0.1*6 = 0); k=0 leaves 0.6.
    assert n2_a['s_min'] == n2_a['s_max'] == 0.0 and n2_a['status'] == 'thermal_only'
    assert n2_b['s_min'] == n2_b['s_max'] == 0.6 and n2_b['status'] == 'proven_displacement'
    assert n2_b['closed_length_m'] == 80


def test_flood_benchmark_cannot_manufacture_exemption(tmp_path):
    db, tmp, fx = build_base_db(tmp_path)
    flood_ts = next(d for d in fx['route1_total_station'] if d['id'] == 'TS-FLOOD')
    with pytest.raises(ValueError, match='benchmark_flood_disturbed'):
        slip_proof.import_total_station(db, put(tmp, flood_ts))
    verdict = review(db, tmp, fx, 'N1')
    assert verdict['status'] == 'pending_review'
    # A single afternoon crack photo cannot date the crack nor prove the morning angle.
    # It was already ingested with the base pages; the replayed page stays idempotent.
    photo = next(d for d in fx['pages'] if d['id'] == 'P1')
    assert slip_proof.import_photo(db, put(tmp, photo)) == {'id': 'P1', 'replayed': True}
    assert report(db)['bank_photos'][0]['subject'].startswith('afternoon crack')
    again = slip_proof.review_segment(
        db, put(tmp, dict(fx['reviews'][0], id='V1-photo')))
    assert again['status'] == 'pending_review'


def test_loose_anchor_blocks_thermal_exemption_and_forces_site_revisit(tmp_path):
    db, tmp, fx = build_base_db(tmp_path)
    ov = next(d for d in fx['route2_oven_calibration'] if d['id'] == 'OV-ALPHA')
    slip_proof.import_oven_calibration(db, put(tmp, ov))
    flag = fx['field_flags'][0]
    slip_proof.import_field_flag(db, put(tmp, flag))
    # Even with k pinned, an independently loose anchor invalidates subtraction for N2.
    n2 = slip_proof.review_segment(db, put(tmp, dict(fx['reviews'][1], id='V2loose')))
    assert n2['status'] == 'pending_review'
    assert 'N2-T01' in n2['revisit_sensors']
    assert any('anchor_loose' in r['reason'] for r in report(db)['bank_slip_reviews'])


def test_versions_append_and_raw_evidence_is_replayable_and_idempotent(tmp_path):
    db, tmp, fx = build_base_db(tmp_path)
    v1 = review(db, tmp, fx, 'N1')
    # Replaying the identical review page is idempotent and creates no new version.
    replay_doc = next(d for d in fx['reviews'] if d['segment'] == 'N1')
    again = slip_proof.review_segment(db, put(tmp, dict(replay_doc, id='V1-N1-v1')))
    assert again.get('replayed') is True
    # A genuinely new review page appends version 2; the old repair_review is never deleted.
    ov = next(d for d in fx['route2_oven_calibration'] if d['id'] == 'OV-BETA')
    slip_proof.import_oven_calibration(db, put(tmp, ov))
    v2 = slip_proof.review_segment(db, put(tmp, dict(replay_doc, id='V1-N1-v2')))
    assert v2['version'] == 2 and v2['status'] == 'proven_displacement'
    reviews = report(db)['bank_slip_reviews']
    assert [r['version'] for r in reviews if r['segment'] == 'N1'] == [1, 2]
    assert next(r for r in report(db)['bank_advice'] if r['segment'] == 'N1')['status'] == 'repair_review'
    # All evidence pages, including hashes, replay deterministically into a fresh ledger.
    assert audit(db)['status'] == 'matched'


def test_missing_temperature_page_keeps_segment_pending(tmp_path):
    fx = load_fixture()
    db = tmp_path / 'cold.sqlite'
    tmp = tmp_path / 'pages'
    tmp.mkdir()
    for doc in fx['pages']:
        if doc['id'] in ('M1', 'M2'):  # no temperature pages for N1 at all
            continue
        ingest(db, tmp, doc)
    doc = fx['reviews'][0]
    verdict = slip_proof.review_segment(db, put(tmp, dict(doc, id='cold-v1')))
    assert verdict['status'] == 'pending_review'
    assert verdict['revisit_sensors'] == ['N1-T01'] and verdict['closed_length_m'] == 0
