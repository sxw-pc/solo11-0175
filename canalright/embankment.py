"""Provisional repair flag from the latest raw tilt magnitude.

This advice deliberately looks only at whether the newest absolute
inclination reaches 0.5 degrees. It carries no temperature coefficient and no
bedrock-referenced displacement, so ``repair_review`` is a notice that triggers
review, never a construction order. The verified verdict
(thermal-only / proven displacement / still pending) is produced by
``canalright.slip_proof`` and is versioned separately; old advice rows are
never overwritten or deleted.
"""

TILT_THRESHOLD_DEG = 0.5


def advise(db, segment, source_id=None):
    row = db.execute('''SELECT t.* FROM bank_tilt t JOIN bank_sensor s ON s.id=t.sensor_id
        WHERE s.segment=? ORDER BY t.at DESC LIMIT 1''', (segment,)).fetchone()
    status = 'repair_review' if abs(row['tilt_deg']) >= TILT_THRESHOLD_DEG else 'watch'
    db.execute('INSERT INTO bank_advice VALUES(?,?,?) ON CONFLICT(segment) DO UPDATE SET status=excluded.status,tilt_deg=excluded.tilt_deg',
               (segment, status, row['tilt_deg']))
    if source_id:
        db.execute('INSERT INTO decision_log(object_type,object_id,status,reason,evidence_id) VALUES(?,?,?,?,?)',
                   ('bank_advice', segment, status, 'raw_tilt_threshold_provisional', source_id))
    return {'segment': segment, 'status': status, 'tilt_deg': row['tilt_deg']}
