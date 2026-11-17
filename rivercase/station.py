from .source import record
from .store import transition

def decide(db,reading):
    assignments=db.execute('SELECT * FROM station_assignment WHERE instrument=? AND start<=? ORDER BY start DESC,event_id',
                           (reading['instrument'],reading['at'])).fetchall()
    if not assignments:
        state,reason,cert='pending','instrument assignment missing',None
    elif assignments[0]['station']!=reading['station'] or (assignments[0]['end'] and reading['at']>=assignments[0]['end']):
        state,reason,cert='ineligible','station transfer excludes old authorization',None
    else:
        certs=db.execute('SELECT * FROM calibration_certificate WHERE instrument=? AND station=? AND valid_from<=? AND valid_to>=? ORDER BY version DESC,event_id',
                         (reading['instrument'],reading['station'],reading['at'],reading['at'])).fetchall()
        if not certs:
            state,reason,cert='pending','calibration invalid at sampling time',None
        else:
            top=[c for c in certs if c['version']==certs[0]['version']]
            conflict=len({(c['cert_id'],c['coefficient'],c['valid_from'],c['valid_to']) for c in top})>1 and len({c['cert_id'] for c in top})==1
            if conflict:
                state,reason,cert='disputed','parallel certificate coefficients conflict',None
            else:
                state,reason,cert='trusted','assignment and calibration valid at sample time',top[0]['event_id']
    transition(db,'station_decision','reading_id',reading['event_id'],state,reason,reading['event_id'],cert)
    return {'reading_id':reading['event_id'],'status':state,'reason':reason}

def recompute(db):
    return [decide(db,r) for r in db.execute('SELECT * FROM station_reading ORDER BY at,event_id').fetchall()]

def import_data(db,data):
    with db:
        for a in data.get('assignments',[]):
            if record(db,a['event_id'],'assignment',a):
                db.execute('INSERT INTO station_assignment(event_id,instrument,station,start,end) VALUES(?,?,?,?,?)',
                           (a['event_id'],a['instrument'],a['station'],a['start'],a.get('end')))
        for c in data.get('certificates',[]):
            if record(db,c['event_id'],'certificate',c):
                db.execute('INSERT INTO calibration_certificate(event_id,cert_id,version,instrument,station,valid_from,valid_to,coefficient) VALUES(?,?,?,?,?,?,?,?)',
                           (c['event_id'],c['cert_id'],c['version'],c['instrument'],c['station'],c['valid_from'],c['valid_to'],c['coefficient']))
        for r in data.get('readings',[]):
            if record(db,r['event_id'],'reading',r):
                db.execute('INSERT INTO station_reading(event_id,instrument,station,at,value) VALUES(?,?,?,?,?)',
                           (r['event_id'],r['instrument'],r['station'],r['at'],r['value']))
        result=recompute(db)
        from .incident import recompute_all
        recompute_all(db)
    return result
