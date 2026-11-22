import json
from canalright.quality import import_site, import_ec
from canalright.store import report


def test_conductivity_only_is_provisional(tmp_path):
    db = tmp_path / 'canal.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_site(db, write('site', {'kind': 'quality_site', 'id': 'S1',
        'site_id': 'S1', 'canal_id': 'C1', 'crop': 'rice'}))
    assert import_ec(db, write('ec', {'kind': 'quality_ec', 'id': 'E1',
        'site_id': 'S1', 'at': '2026-11-22T08:00:00Z', 'ec_us': 1200}))['status'] == 'hold'
    assert report(db)['crop_advice'][0]['ec_us'] == 1200
