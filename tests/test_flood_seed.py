import json
from canalright.stage import import_gauge, import_stage
from canalright.store import report


def test_single_bank_stage_is_provisional(tmp_path):
    db = tmp_path / 'canal.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_gauge(db, write('gauge', {'kind': 'stage_gauge', 'id': 'G1', 'gauge_id': 'G1',
        'canal_id': 'C1', 'bank': 'east'}))
    assert import_stage(db, write('stage', {'kind': 'stage_reading', 'id': 'S1',
        'gauge_id': 'G1', 'at': '2026-11-22T08:00:00Z', 'height_m': 1.2}))['status'] == 'high_stage'
    assert report(db)['flood_warnings'][0]['stage_m'] == 1.2
