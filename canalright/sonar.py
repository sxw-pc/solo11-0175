"""Capture canal sonar echoes with device-local track hints."""
from contextlib import closing
from .store import connect, read


def import_sonar(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_sonar')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['sonar_id'] or not doc['canal_id']:
            raise ValueError('invalid_sonar')
        db.execute('INSERT INTO fish_sonar VALUES(?,?,?,?)',
                   (doc['sonar_id'], doc['canal_id'], doc['bank'], doc['id']))
        return {'sonar_id': doc['sonar_id']}


def import_echo(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_echo')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        sonar = db.execute('SELECT * FROM fish_sonar WHERE id=?', (doc['sonar_id'],)).fetchone()
        if sonar is None or not doc['track_hint'] or doc['range_m'] < 0:
            raise ValueError('invalid_echo')
        db.execute('INSERT INTO fish_echo VALUES(?,?,?,?,?)',
                   (doc['id'], doc['sonar_id'], doc['track_hint'], doc['at'], doc['range_m']))
        from .passage import count
        return count(db, sonar['canal_id'])
