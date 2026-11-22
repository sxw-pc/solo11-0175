"""Dredge ledgers: provisional advice, signed instructions, hauled actuals.

The legacy 15 m threshold advice below is retained unchanged for
compatibility; it is a provisional sonar estimate, never a payable volume.
Signable volumes come from versioned bed profiles (canalright.bed_profile)
and are booked through dredge_instruction, while the contractor's hauled
material lands in dredge_actual.  The three ledgers never overwrite each
other.
"""
from contextlib import closing
from .store import connect, read, log


def advise(db, line_id):
    line = db.execute('SELECT * FROM bathy_line WHERE id=?', (line_id,)).fetchone()
    t = db.execute('SELECT MIN(time_ms) FROM bathy_echo WHERE line_id=?', (line_id,)).fetchone()[0]
    depth = t * 0.75
    cut = max(0, 15 - depth) * line['length_m'] * line['width_m']
    db.execute('INSERT INTO dredge_advice VALUES(?,?,?) ON CONFLICT(line_id) DO UPDATE SET depth_m=excluded.depth_m,cut_m3=excluded.cut_m3',
               (line_id, depth, cut))
    return {'line_id': line_id, 'depth_m': depth, 'cut_m3': cut}


def sign_instruction(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'dredge_instruction')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        profile = db.execute('SELECT * FROM bed_profile WHERE id=?', (doc.get('profile_id'),)).fetchone()
        if profile is None or profile['line_id'] != doc.get('line_id'):
            raise ValueError('unknown_profile')
        if doc.get('cut_m3', 0) <= 0 or not doc.get('instruction_id') or not doc.get('contractor'):
            raise ValueError('invalid_instruction')
        signed = db.execute('SELECT COALESCE(SUM(cut_m3),0) FROM dredge_instruction WHERE profile_id=?',
                            (doc['profile_id'],)).fetchone()[0]
        if signed + doc['cut_m3'] > profile['payable_m3'] + 1e-6:
            raise ValueError('instruction_exceeds_payable')
        db.execute('INSERT INTO dredge_instruction VALUES(?,?,?,?,?,?,?)',
                   (doc['instruction_id'], doc['line_id'], doc['profile_id'], doc['cut_m3'],
                    doc['contractor'], 'signed', doc['id']))
        log(db, 'dredge_instruction', doc['instruction_id'], 'signed', 'within_payable', doc['id'])
        return {'instruction_id': doc['instruction_id'], 'status': 'signed',
                'payable_remaining_m3': round(profile['payable_m3'] - signed - doc['cut_m3'], 6)}


def record_actual(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'dredge_actual')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        instruction = db.execute('SELECT * FROM dredge_instruction WHERE id=?',
                                 (doc.get('instruction_id'),)).fetchone()
        if instruction is None:
            raise ValueError('unknown_instruction')
        if doc.get('volume_m3', 0) <= 0 or not doc.get('material') or not doc.get('disposal'):
            raise ValueError('invalid_actual')
        hauled = db.execute('SELECT COALESCE(SUM(volume_m3),0) FROM dredge_actual WHERE instruction_id=?',
                            (doc['instruction_id'],)).fetchone()[0]
        cumulative = hauled + doc['volume_m3']
        status = 'within_instruction' if cumulative <= instruction['cut_m3'] + 1e-6 else 'overrun'
        db.execute('INSERT INTO dredge_actual VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['instruction_id'], doc['volume_m3'], doc['material'],
                    doc['disposal'], doc['id']))
        log(db, 'dredge_actual', doc['instruction_id'], status, doc['material'], doc['id'])
        return {'instruction_id': doc['instruction_id'], 'status': status,
                'cumulative_m3': round(cumulative, 6)}


def payment_summary(path, line_id):
    with closing(connect(path)) as db:
        advice = db.execute('SELECT * FROM dredge_advice WHERE line_id=?', (line_id,)).fetchone()
        profiles = [dict(row) for row in db.execute(
            'SELECT id,version,payable_m3,possible_m3,manual_m3,status FROM bed_profile WHERE line_id=? ORDER BY version',
            (line_id,))]
        instructed = db.execute('SELECT COALESCE(SUM(cut_m3),0) FROM dredge_instruction WHERE line_id=?',
                                (line_id,)).fetchone()[0]
        actual = db.execute('''SELECT COALESCE(SUM(a.volume_m3),0) FROM dredge_actual a
                               JOIN dredge_instruction i ON a.instruction_id=i.id WHERE i.line_id=?''',
                            (line_id,)).fetchone()[0]
        return {'line_id': line_id,
                'legacy_advice_cut_m3': advice['cut_m3'] if advice else None,
                'profiles': profiles, 'instructed_m3': instructed, 'actual_m3': actual}
