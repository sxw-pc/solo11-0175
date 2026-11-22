import json
from canalright.tilt import import_sensor, import_tilt
from canalright.store import report


def test_raw_tilt_is_provisional(tmp_path):
    db = tmp_path / 'canal.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_sensor(db, write('sensor', {'kind': 'bank_sensor', 'id': 'B1',
        'sensor_id': 'B1', 'canal_id': 'C1', 'segment': 'N1'}))
    result = import_tilt(db, write('tilt', {'kind': 'bank_tilt', 'id': 'T1',
        'sensor_id': 'B1', 'at': '2026-11-22T08:00:00Z', 'tilt_deg': 1.0}))
    assert result['status'] == 'repair_review'
    assert report(db)['bank_advice'][0]['tilt_deg'] == 1.0
