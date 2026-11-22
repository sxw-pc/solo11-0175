from contextlib import closing
from .store import connect,read,log

def import_calibration(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'calibration')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if doc['valid_from']>=doc['valid_to'] or doc['technician']==doc['witness']:
            raise ValueError('invalid_calibration_proof')
        db.execute('INSERT INTO calibration VALUES(?,?,?,?,?,?,?)',(doc['calibration_id'],doc['gate_id'],doc['valid_from'],doc['valid_to'],doc['technician'],doc['witness'],doc['id']))
        log(db,'calibration',doc['calibration_id'],'active','independent_meter_check',doc['id'])
        return {'calibration_id':doc['calibration_id'],'status':'active'}

def import_gate(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'gate')
        if not fresh:return {'id':doc['id'],'replayed':True}
        rotation=db.execute('SELECT * FROM rotation WHERE id=?',(doc['rotation_id'],)).fetchone()
        if not rotation:raise ValueError('unknown_rotation')
        if doc['end_meter']<doc['start_meter'] or doc['start_meter']<0:raise ValueError('meter_rollback')
        if not doc['gate_id'].startswith(rotation['canal_id']+'-'):
            raise ValueError('gate_canal_mismatch')
        if doc['witness']==doc['operator']:raise ValueError('untrusted_gate_proof')
        read_start=doc.get('read_start_at',rotation['start_at'])
        read_end=doc.get('read_end_at',doc.get('observed_at',rotation['end_at']))
        if not rotation['start_at']<=read_start<read_end<=rotation['end_at']:
            raise ValueError('reading_outside_rotation_window')
        if 'calibration_id' in doc:
            proof=db.execute('SELECT * FROM calibration WHERE id=?',(doc['calibration_id'],)).fetchone()
            if not proof or proof['gate_id']!=doc['gate_id'] or not (proof['valid_from']<=read_start and read_end<proof['valid_to']):
                raise ValueError('calibration_not_effective')
        elif not doc.get('calibrated'):
            raise ValueError('untrusted_gate_proof')
        if db.execute('SELECT 1 FROM gate_reading WHERE rotation_id=?',(doc['rotation_id'],)).fetchone():raise ValueError('reading_conflict')
        actual=doc['end_meter']-doc['start_meter']
        reason='unplanned_delivery' if rotation['status']!='reserved' else ('over_right' if actual>rotation['planned'] else ('shortage' if rotation['planned']-actual>2 else 'fulfilled'))
        status='disputed' if reason in ('unplanned_delivery','over_right') else reason
        db.execute('INSERT INTO gate_reading VALUES(?,?,?,?,?,?,?,?)',(doc['reading_id'],doc['rotation_id'],doc['gate_id'],doc['start_meter'],doc['end_meter'],1,doc['witness'],doc['id']))
        db.execute('INSERT INTO delivery VALUES(?,?,?,?,?,?)',(doc['rotation_id'],status,actual,max(0,rotation['planned']-actual),reason,1))
        if status!='disputed':
            db.execute('UPDATE water_right SET realized=realized+? WHERE id=?',(actual,rotation['right_id']))
        if status=='shortage':
            db.execute('INSERT INTO compensation VALUES(?,?,?,?)',(doc['rotation_id'],rotation['planned']-actual,'canal_operator','owed'))
        log(db,'delivery',doc['rotation_id'],status,reason,doc['id'])
        return {'rotation_id':doc['rotation_id'],'status':status,'actual':actual,'shortage':max(0,rotation['planned']-actual),
                'compensation':'owed' if status=='shortage' else 'none'}

def review_shortage(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'liability_review')
        if not fresh:return {'id':doc['id'],'replayed':True}
        delivery=db.execute('SELECT * FROM delivery WHERE rotation_id=?',(doc['rotation_id'],)).fetchone()
        if not delivery or delivery['status']!='shortage':raise ValueError('no_reviewable_shortage')
        if db.execute('SELECT 1 FROM liability_review WHERE rotation_id=?',(doc['rotation_id'],)).fetchone():
            raise ValueError('already_reviewed')
        if doc['reviewer']==doc['operator'] or doc['reason'] not in ('canal_failure','verified_drought','meter_fault'):
            raise ValueError('invalid_liability_evidence')
        if doc['reason']=='canal_failure':
            responsible,units,status='canal_operator',delivery['shortage'],'confirmed'
        elif doc['reason']=='verified_drought':
            responsible,units,status='water_authority',delivery['shortage'],'reassigned'
        else:
            responsible,units,status='pending_remeasurement',0,'contested'
        db.execute('INSERT INTO liability_review VALUES(?,?,?,?,?)',(doc['rotation_id'],doc['reason'],responsible,units,doc['id']))
        db.execute('UPDATE compensation SET units=?,responsible=?,status=? WHERE rotation_id=?',(units,responsible,status,doc['rotation_id']))
        log(db,'compensation',doc['rotation_id'],status,doc['reason'],doc['id'])
        return {'rotation_id':doc['rotation_id'],'status':status,'responsible':responsible,'units':units}
