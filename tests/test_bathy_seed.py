import json
from canalright.bathy import import_line, import_echo
from canalright.store import report


def test_first_echo_dredge_is_provisional(tmp_path):
    db = tmp_path / 'canal.sqlite'
    def write(name, doc):
        file = tmp_path / f'{name}.json'
        file.write_text(json.dumps(doc))
        return file
    import_line(db, write('line', {'kind': 'bathy_line', 'id': 'L1',
        'line_id': 'L1', 'canal_id': 'C1', 'length_m': 10, 'width_m': 5}))
    advice = import_echo(db, write('echo', {'kind': 'bathy_echo', 'id': 'E1',
        'line_id': 'L1', 'beam_id': 'B1', 'time_ms': 20, 'strength': 0.8}))
    assert advice['depth_m'] == 15
    assert report(db)['dredge_advice'][0]['cut_m3'] == 0
