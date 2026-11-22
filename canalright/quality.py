"""Capture a canal's crop intake conductivity reading."""
from contextlib import closing
from .store import connect, read


def import_site(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'quality_site')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['site_id'] or not doc['crop']:
            raise ValueError('invalid_site')
        db.execute('INSERT INTO quality_site VALUES(?,?,?,?)',
                   (doc['site_id'], doc['canal_id'], doc['crop'], doc['id']))
        return {'site_id': doc['site_id']}


def import_ec(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'quality_ec')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        site = db.execute('SELECT * FROM quality_site WHERE id=?', (doc['site_id'],)).fetchone()
        if site is None or doc['ec_us'] < 0:
            raise ValueError('invalid_ec')
        db.execute('INSERT INTO quality_reading VALUES(?,?,?,?)',
                   (doc['id'], doc['site_id'], doc['at'], doc['ec_us']))
        from .crop import advise
        return advise(db, doc['site_id'])
