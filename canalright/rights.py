from contextlib import closing
from .store import connect,read,log

def import_issuer(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'issuer')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if doc['valid_from']>=doc['valid_to'] or not doc['parcel_prefix'] or doc['technician']==doc['witness']:
            raise ValueError('invalid_issuer_proof')
        db.execute('INSERT INTO issuer VALUES(?,?,?,?,?)',(doc['issuer_id'],doc['valid_from'],doc['valid_to'],doc['parcel_prefix'],doc['id']))
        log(db,'issuer',doc['issuer_id'],'active','independent_registration',doc['id'])
        return {'issuer_id':doc['issuer_id'],'status':'active'}

def import_right(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'right')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if doc['area']<=0 or doc['quota']<=0 or not doc['owner'] or not doc['season']:raise ValueError('invalid_right')
        effective_from=doc.get('effective_from','')
        effective_to=doc.get('effective_to','')
        issuer_id=doc.get('issuer_id','legacy')
        if issuer_id!='legacy':
            issuer=db.execute('SELECT * FROM issuer WHERE id=?',(issuer_id,)).fetchone()
            if not issuer or not effective_from or effective_from>=effective_to or not doc['parcel_id'].startswith(issuer['parcel_prefix']) or not (issuer['valid_from']<=doc['signed_at']<issuer['valid_to']) or not (issuer['valid_from']<=effective_from and effective_to<=issuer['valid_to']):
                raise ValueError('issuer_or_right_interval_invalid')
        elif effective_from or effective_to:
            raise ValueError('issuer_required_for_interval')
        old=db.execute('SELECT * FROM parcel WHERE id=?',(doc['parcel_id'],)).fetchone()
        if old and (old['owner'],old['season'],old['area'])!=(doc['owner'],doc['season'],doc['area']):raise ValueError('parcel_identity_conflict')
        if not old:db.execute('INSERT INTO parcel VALUES(?,?,?,?)',(doc['parcel_id'],doc['owner'],doc['season'],doc['area']))
        quota=db.execute("SELECT COALESCE(SUM(quota),0) FROM water_right WHERE parcel_id=? AND status='active' AND (effective_from='' OR ?='' OR (effective_from<? AND ?<effective_to))",(doc['parcel_id'],effective_from,effective_to,effective_from)).fetchone()[0]
        if quota+doc['quota']>doc['area']*10:raise ValueError('parcel_quota_exceeded')
        db.execute('INSERT INTO water_right VALUES(?,?,?,?,?,?,?,?,?)',(doc['right_id'],doc['parcel_id'],doc['quota'],'active',doc['id'],effective_from,effective_to,issuer_id,0))
        log(db,'right',doc['right_id'],'active','signed_quota',doc['id'])
        return {'right_id':doc['right_id'],'quota':doc['quota'],'status':'active'}

def revoke_right(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=read(db,file,'right_revoke')
        if not fresh:return {'id':doc['id'],'replayed':True}
        right=db.execute('SELECT * FROM water_right WHERE id=?',(doc['right_id'],)).fetchone()
        if not right or right['status']!='active':raise ValueError('right_not_active')
        db.execute("UPDATE water_right SET status='revoked' WHERE id=?",(doc['right_id'],))
        db.execute("UPDATE rotation SET status='suspended' WHERE right_id=? AND status='reserved' AND id NOT IN (SELECT rotation_id FROM delivery)",(doc['right_id'],))
        from .rotation import replan
        for row in db.execute('SELECT DISTINCT canal_id FROM rotation WHERE right_id=?',(doc['right_id'],)):
            replan(db,row['canal_id'],doc['id'])
        log(db,'right',doc['right_id'],'revoked','unfulfilled_quota_released',doc['id'])
        return {'right_id':doc['right_id'],'status':'revoked'}
