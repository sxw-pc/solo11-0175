from contextlib import closing
from .store import connect,read,log

def replan(db,canal_id,evidence_id):
    canal=db.execute('SELECT * FROM canal WHERE id=?',(canal_id,)).fetchone()
    if canal['status']!='open':return
    candidates=db.execute("SELECT r.* FROM rotation r JOIN water_right w ON w.id=r.right_id WHERE r.canal_id=? AND r.status IN ('reserved','waitlisted') AND w.status='active' ORDER BY r.priority DESC,r.start_at,r.id",(canal_id,)).fetchall()
    locked=[r for r in candidates if db.execute('SELECT 1 FROM delivery WHERE rotation_id=?',(r['id'],)).fetchone()]
    reserved=list(locked)
    for candidate in candidates:
        if candidate in locked:continue
        blocked=db.execute("SELECT 1 FROM maintenance WHERE canal_id=? AND status='closed' AND start_at<? AND end_at>?",(canal_id,candidate['end_at'],candidate['start_at'])).fetchone()
        other=db.execute("SELECT COALESCE(SUM(planned),0) FROM rotation WHERE right_id=? AND canal_id<>? AND status='reserved'",(candidate['right_id'],canal_id)).fetchone()[0]
        own=sum(x['planned'] for x in reserved if x['right_id']==candidate['right_id'])
        quota=db.execute('SELECT quota FROM water_right WHERE id=?',(candidate['right_id'],)).fetchone()[0]
        overlap=sum(x['planned'] for x in reserved if x['start_at']<candidate['end_at'] and x['end_at']>candidate['start_at'])
        outcome='reserved' if not blocked and other+own+candidate['planned']<=quota and overlap+candidate['planned']<=canal['capacity'] else 'waitlisted'
        if outcome=='reserved':reserved.append(candidate)
        if candidate['status']!=outcome:
            db.execute('UPDATE rotation SET status=? WHERE id=?',(outcome,candidate['id']))
            log(db,'rotation',candidate['id'],outcome,'priority_capacity_or_right_replan',evidence_id)

def import_rotation(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'rotation')
        if not fresh:return {'id':doc['id'],'replayed':True}
        right=db.execute('SELECT * FROM water_right WHERE id=?',(doc['right_id'],)).fetchone()
        if not right or right['status']!='active':raise ValueError('right_not_active')
        if doc['start_at']>=doc['end_at'] or doc['planned']<=0:raise ValueError('invalid_window')
        if right['effective_from'] and not (right['effective_from']<=doc['start_at'] and doc['end_at']<=right['effective_to']):raise ValueError('outside_right_interval')
        canal=db.execute('SELECT * FROM canal WHERE id=?',(doc['canal_id'],)).fetchone()
        if canal and canal['capacity']!=doc['capacity']:raise ValueError('canal_capacity_conflict')
        if not canal:db.execute('INSERT INTO canal VALUES(?,?,?)',(doc['canal_id'],doc['capacity'],'open'))
        if canal and canal['status']!='open':raise ValueError('canal_closed')
        planned=db.execute("SELECT COALESCE(SUM(planned),0) FROM rotation WHERE right_id=? AND status='reserved'",(doc['right_id'],)).fetchone()[0]
        if planned+doc['planned']>right['quota']:raise ValueError('right_overplanned')
        priority=doc.get('priority',0)
        if not isinstance(priority,int) or not 0<=priority<=9:raise ValueError('invalid_priority')
        db.execute('INSERT INTO rotation VALUES(?,?,?,?,?,?,?,?,?)',(doc['rotation_id'],doc['right_id'],doc['canal_id'],doc['start_at'],doc['end_at'],doc['planned'],priority,'waitlisted',doc['id']))
        replan(db,doc['canal_id'],doc['id'])
        status=db.execute('SELECT status FROM rotation WHERE id=?',(doc['rotation_id'],)).fetchone()['status']
        return {'rotation_id':doc['rotation_id'],'status':status}

def close_canal(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'canal_close')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM canal WHERE id=?',(doc['canal_id'],)).fetchone():raise ValueError('unknown_canal')
        if 'start_at' in doc or 'end_at' in doc:
            if not doc.get('start_at') or not doc.get('end_at') or doc['start_at']>=doc['end_at']:
                raise ValueError('invalid_maintenance_window')
            db.execute('INSERT INTO maintenance VALUES(?,?,?,?,?,?)',(doc['id'],doc['canal_id'],doc['start_at'],doc['end_at'],'closed',doc['id']))
            db.execute("UPDATE rotation SET status='suspended' WHERE canal_id=? AND status='reserved' AND start_at<? AND end_at>? AND id NOT IN (SELECT rotation_id FROM delivery)",(doc['canal_id'],doc['end_at'],doc['start_at']))
            replan(db,doc['canal_id'],doc['id'])
            log(db,'canal',doc['canal_id'],'restricted','window_maintenance',doc['id'])
            return {'canal_id':doc['canal_id'],'status':'restricted','maintenance_id':doc['id']}
        db.execute("UPDATE canal SET status='closed' WHERE id=?",(doc['canal_id'],))
        db.execute("UPDATE rotation SET status='suspended' WHERE canal_id=? AND status='reserved' AND id NOT IN (SELECT rotation_id FROM delivery)",(doc['canal_id'],))
        log(db,'canal',doc['canal_id'],'closed','maintenance',doc['id'])
        return {'canal_id':doc['canal_id'],'status':'closed'}

def open_canal(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'canal_open')
        if not fresh:return {'id':doc['id'],'replayed':True}
        canal=db.execute('SELECT * FROM canal WHERE id=?',(doc['canal_id'],)).fetchone()
        if not canal or doc['inspector']==doc['operator']:
            raise ValueError('invalid_reopening_proof')
        if doc.get('maintenance_id'):
            maintenance=db.execute("SELECT * FROM maintenance WHERE id=? AND canal_id=? AND status='closed'",(doc['maintenance_id'],doc['canal_id'])).fetchone()
            if not maintenance:raise ValueError('invalid_reopening_proof')
            db.execute("UPDATE maintenance SET status='open' WHERE id=?",(doc['maintenance_id'],))
        else:
            if canal['status']!='closed':raise ValueError('invalid_reopening_proof')
            db.execute("UPDATE canal SET status='open' WHERE id=?",(doc['canal_id'],))
        db.execute("UPDATE rotation SET status='waitlisted' WHERE canal_id=? AND status='suspended' AND right_id IN (SELECT id FROM water_right WHERE status='active')",(doc['canal_id'],))
        replan(db,doc['canal_id'],doc['id'])
        log(db,'canal',doc['canal_id'],'open','inspection_releases_waitlist',doc['id'])
        return {'canal_id':doc['canal_id'],'status':'open'}
