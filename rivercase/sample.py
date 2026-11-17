from .source import record
from .store import transition

def decide(db,bottle_id):
    bottle=db.execute('SELECT * FROM sample_bottle WHERE bottle_id=?',(bottle_id,)).fetchone()
    if not bottle: return None
    conflict=db.execute('SELECT event_id FROM sample_conflict WHERE bottle_id=?',(bottle_id,)).fetchone()
    handoffs=db.execute('SELECT * FROM custody_handoff WHERE bottle_id=? ORDER BY at,event_id',(bottle_id,)).fetchall()
    results=db.execute('SELECT * FROM lab_result WHERE bottle_id=? ORDER BY event_id',(bottle_id,)).fetchall()
    holder='collector';last=bottle['at'];chain=True
    for h in handoffs:
        if h['sender']!=holder or h['recipient']==holder or h['at']<last:
            chain=False;break
        holder=h['recipient'];last=h['at']
    if conflict:
        state,reason,result='disputed','same bottle identifier has conflicting payload',None
    elif not chain or holder!='lab' or len(handoffs)<2:
        state,reason,result='isolated','custody handoff chain incomplete',None
    elif not results:
        state,reason,result='pending','laboratory result absent',None
    elif len({(r['value'],r['method'],r['method_valid_to'],r['detection_limit']) for r in results})>1:
        state,reason,result='disputed','parallel laboratory results conflict for bottle',None
    else:
        result=results[-1]
        original=db.execute('SELECT station,at FROM sample_bottle WHERE bottle_id=?',(result['retest_of'],)).fetchone() if result['retest_of'] else None
        if result['retest_of'] and (not original or original['station']!=bottle['station'] or original['at']>=bottle['at']):
            state,reason='pending','retest target missing, upstream station mismatch, or invalid sample time'
        elif result['method_valid_to']<bottle['at']:
            state,reason='pending','analysis method expired at sampling time'
        elif result['value']<result['threshold'] and result['detection_limit']>result['threshold']:
            state,reason='pending','detection limit cannot prove a low result'
        else:
            state,reason='trusted','sealed custody and applicable method'
        result=result['event_id']
    transition(db,'sample_decision','bottle_id',bottle_id,state,reason,bottle['event_id'],result)
    return {'bottle_id':bottle_id,'status':state,'reason':reason}

def recompute(db):
    return [decide(db,r['bottle_id']) for r in db.execute('SELECT bottle_id FROM sample_bottle ORDER BY bottle_id').fetchall()]

def ingest(db,data):
    for b in data.get('bottles',[]):
        if not record(db,b['event_id'],'bottle',b): continue
        old=db.execute('SELECT * FROM sample_bottle WHERE bottle_id=?',(b['bottle_id'],)).fetchone()
        if old:
            if (old['station'],old['at'],old['seal'])==(b['station'],b['at'],b['seal']):
                db.execute('UPDATE sample_bottle SET replays=replays+1 WHERE bottle_id=?',(b['bottle_id'],))
            else:
                db.execute('INSERT INTO sample_conflict(event_id,bottle_id,reason) VALUES(?,?,?)',
                           (b['event_id'],b['bottle_id'],'same bottle number, different station/time/seal'))
        else:
            db.execute('INSERT INTO sample_bottle(event_id,bottle_id,station,at,seal) VALUES(?,?,?,?,?)',
                       (b['event_id'],b['bottle_id'],b['station'],b['at'],b['seal']))
    for h in data.get('handoffs',[]):
        if record(db,h['event_id'],'handoff',h):
            db.execute('INSERT INTO custody_handoff(event_id,bottle_id,sender,recipient,at) VALUES(?,?,?,?,?)',
                       (h['event_id'],h['bottle_id'],h['sender'],h['recipient'],h['at']))
    for r in data.get('results',[]):
        if record(db,r['event_id'],'lab_result',r):
            db.execute('INSERT INTO lab_result(event_id,bottle_id,value,method,method_valid_to,detection_limit,threshold,retest_of) VALUES(?,?,?,?,?,?,?,?)',
                       (r['event_id'],r['bottle_id'],r['value'],r['method'],r['method_valid_to'],r['detection_limit'],r['threshold'],r.get('retest_of')))
    return recompute(db)

def import_data(db,data):
    with db:
        out=ingest(db,data)
        from .incident import recompute_all
        recompute_all(db)
    return out
