import json,sys,subprocess
from pathlib import Path
import pytest
from canalright.rights import import_right,revoke_right,import_issuer
from canalright.rotation import import_rotation,close_canal,open_canal
from canalright.gate import import_gate,import_calibration,review_shortage
from canalright.store import report,connect
from canalright.audit import audit

def file(tmp,name,doc):
    p=tmp/name;p.write_text(json.dumps(doc),encoding='utf8');return p
def right(**changes):
    d=dict(id='e1',kind='right',right_id='W1',parcel_id='P1',owner='F1',season='2026',area=10,quota=50);d.update(changes);return d
def rotation(**changes):
    d=dict(id='e2',kind='rotation',rotation_id='R1',right_id='W1',canal_id='C1',capacity=40,start_at='2026-11-01T08:00:00Z',end_at='2026-11-01T10:00:00Z',planned=30);d.update(changes);return d
def gate(**changes):
    d=dict(id='e3',kind='gate',reading_id='G1',rotation_id='R1',gate_id='C1-G',start_meter=100,end_meter=125,calibrated=True,witness='guard',operator='op');d.update(changes);return d

def issuer(**changes):
    d=dict(id='i1',kind='issuer',issuer_id='I1',valid_from='2026-01-01T00:00:00Z',valid_to='2027-01-01T00:00:00Z',parcel_prefix='P',technician='registrar',witness='auditor');d.update(changes);return d

def calibration(**changes):
    d=dict(id='c1',kind='calibration',calibration_id='K1',gate_id='C1-G',valid_from='2026-10-01T00:00:00Z',valid_to='2026-12-01T00:00:00Z',technician='meter_lab',witness='farmer');d.update(changes);return d

def test_right_rotation_delivery_shortage(tmp_path):
    print(__import__('canalright').__file__)
    db=tmp_path/'x.sqlite';assert import_right(db,file(tmp_path,'right.json',right()))['status']=='active'
    assert import_rotation(db,file(tmp_path,'rotation.json',rotation()))['status']=='reserved'
    decision=import_gate(db,file(tmp_path,'gate.json',gate()))
    assert (decision['status'],decision['shortage'])==('shortage',5)
    assert report(db)['deliveries'][0]['reason']=='shortage'
    assert report(db)['rights'][0]['realized']==25
    assert report(db)['compensation'][0]['units']==5
    assert audit(db)['status']=='matched'

def test_quota_and_overplanning_are_atomic(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    with pytest.raises(ValueError,match='parcel_quota_exceeded'):
        import_right(db,file(tmp_path,'other.json',right(id='e4',right_id='W2',quota=60)))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    with pytest.raises(ValueError,match='right_overplanned'):
        import_rotation(db,file(tmp_path,'over.json',rotation(id='e5',rotation_id='R2',planned=25,start_at='2026-11-01T11:00:00Z',end_at='2026-11-01T12:00:00Z')))
    assert len(report(db)['rights'])==1 and len(report(db)['rotations'])==1

def test_canal_capacity_waitlist_and_closure(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    assert import_rotation(db,file(tmp_path,'wait.json',rotation(id='e4',rotation_id='R2',planned=20)))['status']=='waitlisted'
    close_canal(db,file(tmp_path,'close.json',{'id':'e5','kind':'canal_close','canal_id':'C1'}))
    assert [r['status'] for r in report(db)['rotations']]==['suspended','waitlisted']

def test_priority_replans_conflicting_canal_window(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    outcome=import_rotation(db,file(tmp_path,'priority.json',rotation(id='e4',rotation_id='R2',planned=20,priority=5)))
    assert outcome['status']=='reserved'
    assert {r['id']:r['status'] for r in report(db)['rotations']}=={'R1':'waitlisted','R2':'reserved'}

def test_right_revocation_suspends_unfulfilled_rotation(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    assert revoke_right(db,file(tmp_path,'revoke.json',{'id':'e4','kind':'right_revoke','right_id':'W1'}))['status']=='revoked'
    assert report(db)['rotations'][0]['status']=='suspended'

def test_meter_and_witness_failures_do_not_write_delivery(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    with pytest.raises(ValueError,match='meter_rollback'):
        import_gate(db,file(tmp_path,'badmeter.json',gate(end_meter=90)))
    with pytest.raises(ValueError,match='untrusted_gate_proof'):
        import_gate(db,file(tmp_path,'badwitness.json',gate(witness='op')))
    assert report(db)['deliveries']==[]

def test_public_cli(tmp_path):
    db=tmp_path/'cli.sqlite';p=file(tmp_path,'right.json',right())
    result=subprocess.run([sys.executable,'-m','canalright','right-import','--db',str(db),'--file',str(p)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['status']=='active'

def test_parcel_identity_conflict_does_not_create_second_right(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    with pytest.raises(ValueError,match='parcel_identity_conflict'):
        import_right(db,file(tmp_path,'wrong-owner.json',right(id='e4',right_id='W2',owner='F2')))
    assert len(report(db)['rights'])==1

def test_revocation_preserves_delivered_history_and_blocks_new_rotation(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    import_gate(db,file(tmp_path,'gate.json',gate()))
    revoke_right(db,file(tmp_path,'revoke.json',{'id':'e4','kind':'right_revoke','right_id':'W1'}))
    assert report(db)['deliveries'][0]['actual']==25
    assert report(db)['rights'][0]['realized']==25
    with pytest.raises(ValueError,match='right_not_active'):
        import_rotation(db,file(tmp_path,'new.json',rotation(id='e5',rotation_id='R2')))
    assert audit(db)['status']=='matched'

def test_closed_canal_blocks_new_window_and_actual_without_reservation_disputed(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    close_canal(db,file(tmp_path,'close.json',{'id':'e4','kind':'canal_close','canal_id':'C1'}))
    with pytest.raises(ValueError,match='canal_closed'):
        import_rotation(db,file(tmp_path,'new.json',rotation(id='e5',rotation_id='R2',planned=20)))
    result=import_gate(db,file(tmp_path,'gate.json',gate()))
    assert result['status']=='disputed'
    assert report(db)['compensation']==[]

def test_evidence_id_conflict_is_atomic(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    with pytest.raises(ValueError,match='evidence_id_conflict'):
        import_right(db,file(tmp_path,'changed.json',right(quota=40)))
    assert report(db)['rights'][0]['quota']==50

def test_issuer_interval_and_expired_schedule_are_independent(tmp_path):
    db=tmp_path/'x.sqlite'
    with pytest.raises(ValueError,match='issuer_or_right_interval_invalid'):
        import_right(db,file(tmp_path,'no-issuer.json',right(issuer_id='I1',signed_at='2026-01-02T00:00:00Z',effective_from='2026-10-01T00:00:00Z',effective_to='2026-12-01T00:00:00Z')))
    import_issuer(db,file(tmp_path,'issuer.json',issuer()))
    import_right(db,file(tmp_path,'right.json',right(issuer_id='I1',signed_at='2026-01-02T00:00:00Z',effective_from='2026-10-01T00:00:00Z',effective_to='2026-12-01T00:00:00Z')))
    with pytest.raises(ValueError,match='outside_right_interval'):
        import_rotation(db,file(tmp_path,'outside.json',rotation(start_at='2026-12-02T08:00:00Z',end_at='2026-12-02T10:00:00Z')))
    assert report(db)['rotations']==[]
    import_rotation(db,file(tmp_path,'inside.json',rotation()))
    assert audit(db)['status']=='matched'

def test_nonoverlapping_rights_release_quota_but_overlap_rejected(tmp_path):
    db=tmp_path/'x.sqlite';import_issuer(db,file(tmp_path,'issuer.json',issuer()))
    base=dict(issuer_id='I1',signed_at='2026-01-02T00:00:00Z')
    import_right(db,file(tmp_path,'spring.json',right(quota=80,effective_from='2026-02-01T00:00:00Z',effective_to='2026-05-01T00:00:00Z',**base)))
    with pytest.raises(ValueError,match='parcel_quota_exceeded'):
        import_right(db,file(tmp_path,'overlap.json',right(id='e4',right_id='W2',quota=30,effective_from='2026-04-01T00:00:00Z',effective_to='2026-06-01T00:00:00Z',**base)))
    import_right(db,file(tmp_path,'autumn.json',right(id='e5',right_id='W3',quota=80,effective_from='2026-08-01T00:00:00Z',effective_to='2026-11-01T00:00:00Z',**base)))
    assert len(report(db)['rights'])==2 and audit(db)['status']=='matched'

def test_closed_canal_reopens_and_promotes_waitlist(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'r1.json',rotation()))
    import_rotation(db,file(tmp_path,'r2.json',rotation(id='e4',rotation_id='R2',planned=20)))
    close_canal(db,file(tmp_path,'close.json',dict(id='e5',kind='canal_close',canal_id='C1')))
    with pytest.raises(ValueError,match='invalid_reopening_proof'):
        open_canal(db,file(tmp_path,'bad-open.json',dict(id='e6',kind='canal_open',canal_id='C1',inspector='op',operator='op')))
    open_canal(db,file(tmp_path,'open.json',dict(id='e7',kind='canal_open',canal_id='C1',inspector='auditor',operator='op')))
    assert {r['id']:r['status'] for r in report(db)['rotations']}=={'R1':'reserved','R2':'waitlisted'}
    assert audit(db)['status']=='matched'

def test_calibration_and_shortage_responsibility_reassignment(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    with pytest.raises(ValueError,match='calibration_not_effective'):
        import_gate(db,file(tmp_path,'uncalibrated.json',gate(calibration_id='K1',observed_at='2026-11-01T10:00:00Z')))
    import_calibration(db,file(tmp_path,'calibration.json',calibration()))
    with pytest.raises(ValueError,match='gate_canal_mismatch'):
        import_gate(db,file(tmp_path,'wrong-canal.json',gate(calibration_id='K1',gate_id='C2-G',observed_at='2026-11-01T10:00:00Z')))
    with pytest.raises(ValueError,match='reading_outside_rotation_window'):
        import_gate(db,file(tmp_path,'late.json',gate(calibration_id='K1',observed_at='2026-11-02T10:00:00Z')))
    import_gate(db,file(tmp_path,'gate.json',gate(calibration_id='K1',observed_at='2026-11-01T10:00:00Z')))
    with pytest.raises(ValueError,match='invalid_liability_evidence'):
        review_shortage(db,file(tmp_path,'bad-review.json',dict(id='v1',kind='liability_review',rotation_id='R1',reason='verified_drought',reviewer='op',operator='op')))
    result=review_shortage(db,file(tmp_path,'review.json',dict(id='v2',kind='liability_review',rotation_id='R1',reason='verified_drought',reviewer='district',operator='op')))
    assert result=={'rotation_id':'R1','status':'reassigned','responsible':'water_authority','units':5}
    assert report(db)['compensation'][0]['responsible']=='water_authority'
    assert audit(db)['status']=='matched'

def test_delivered_window_cannot_be_displaced_by_later_priority(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'rotation.json',rotation()))
    import_gate(db,file(tmp_path,'gate.json',gate()))
    assert import_rotation(db,file(tmp_path,'urgent.json',rotation(id='e4',rotation_id='R2',priority=9,planned=20)))['status']=='waitlisted'
    assert report(db)['rotations'][0]['status']=='reserved'
    assert audit(db)['status']=='matched'

def test_timed_maintenance_only_suspends_intersecting_windows(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'morning.json',rotation(planned=20)))
    import_rotation(db,file(tmp_path,'afternoon.json',rotation(id='e4',rotation_id='R2',planned=20,start_at='2026-11-01T14:00:00Z',end_at='2026-11-01T16:00:00Z')))
    close_canal(db,file(tmp_path,'repair.json',dict(id='e5',kind='canal_close',canal_id='C1',start_at='2026-11-01T08:30:00Z',end_at='2026-11-01T11:00:00Z')))
    assert {r['id']:r['status'] for r in report(db)['rotations']}=={'R1':'suspended','R2':'reserved'}
    open_canal(db,file(tmp_path,'reopen.json',dict(id='e6',kind='canal_open',canal_id='C1',maintenance_id='e5',inspector='auditor',operator='op')))
    assert {r['id']:r['status'] for r in report(db)['rotations']}=={'R1':'reserved','R2':'reserved'}
    assert audit(db)['status']=='matched'

def test_revocation_releases_capacity_to_other_right_without_erasing_delivery(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_right(db,file(tmp_path,'right2.json',right(id='e4',right_id='W2')))
    import_rotation(db,file(tmp_path,'r1.json',rotation()))
    assert import_rotation(db,file(tmp_path,'r2.json',rotation(id='e5',rotation_id='R2',right_id='W2')))['status']=='waitlisted'
    revoke_right(db,file(tmp_path,'revoke.json',dict(id='e6',kind='right_revoke',right_id='W1')))
    assert {r['id']:r['status'] for r in report(db)['rotations']}=={'R1':'suspended','R2':'reserved'}
    assert audit(db)['status']=='matched'

def test_same_right_cross_canal_overplanning_is_rejected(tmp_path):
    db=tmp_path/'x.sqlite';import_right(db,file(tmp_path,'right.json',right()))
    import_rotation(db,file(tmp_path,'c1.json',rotation()))
    with pytest.raises(ValueError,match='right_overplanned'):
        import_rotation(db,file(tmp_path,'c2.json',rotation(id='e4',rotation_id='R2',canal_id='C2',planned=25)))
    assert len(report(db)['rotations'])==1 and audit(db)['status']=='matched'
