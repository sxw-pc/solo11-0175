"""Provisional repair flag from raw tilt magnitude."""


def advise(db, segment):
    row = db.execute('''SELECT t.* FROM bank_tilt t JOIN bank_sensor s ON s.id=t.sensor_id
        WHERE s.segment=? ORDER BY t.at DESC LIMIT 1''', (segment,)).fetchone()
    status = 'repair_review' if abs(row['tilt_deg']) >= 0.5 else 'watch'
    db.execute('INSERT INTO bank_advice VALUES(?,?,?) ON CONFLICT(segment) DO UPDATE SET status=excluded.status,tilt_deg=excluded.tilt_deg',
               (segment, status, row['tilt_deg']))
    return {'segment': segment, 'status': status}
