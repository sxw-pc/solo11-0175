import json
from canalright.power import import_meter, import_sample
from canalright.store import report


def test_rms_bill_is_provisional(tmp_path):
    db = tmp_path / 'canal.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_meter(db, write('meter', {'kind': 'pump_meter', 'id': 'M1',
        'meter_id': 'M1', 'pump_id': 'P1', 'rate_per_kwh': 1}))
    estimate = import_sample(db, write('sample', {'kind': 'pump_sample', 'id': 'S1',
        'meter_id': 'M1', 'start_at': '2026-11-22T08:00:00Z',
        'end_at': '2026-11-22T09:00:00Z', 'voltage_rms': 230, 'current_rms': 10}))
    assert estimate['estimated_kwh'] == 2.3
    assert report(db)['pump_bills'][0]['estimated_charge'] == 2.3
