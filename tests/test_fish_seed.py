import json
from canalright.sonar import import_sonar, import_echo
from canalright.store import report


def test_echo_hints_are_provisional(tmp_path):
    db = tmp_path / 'canal.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    for name in ('A', 'B'):
        import_sonar(db, write(f'sonar-{name}', {'kind': 'fish_sonar', 'id': f'sonar-{name}',
            'sonar_id': name, 'canal_id': 'C1', 'bank': name}))
        import_echo(db, write(f'echo-{name}', {'kind': 'fish_echo', 'id': f'echo-{name}',
            'sonar_id': name, 'track_hint': 'T1', 'at': '2026-11-22T08:00:00Z', 'range_m': 5}))
    assert report(db)['fish_passage'][0]['provisional_count'] == 2
