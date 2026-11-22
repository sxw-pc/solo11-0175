"""Provisional crop intake decision from electrical conductivity alone."""


def advise(db, site_id):
    row = db.execute('SELECT * FROM quality_reading WHERE site_id=? ORDER BY at DESC LIMIT 1',
                     (site_id,)).fetchone()
    status = 'hold' if row['ec_us'] >= 1000 else 'suitable'
    db.execute('INSERT INTO crop_advice VALUES(?,?,?) ON CONFLICT(site_id) DO UPDATE SET status=excluded.status,ec_us=excluded.ec_us',
               (site_id, status, row['ec_us']))
    return {'site_id': site_id, 'status': status}
