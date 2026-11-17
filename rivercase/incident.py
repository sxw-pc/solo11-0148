import json
from datetime import datetime,timedelta
from .source import record
from .store import log

def routes(db,origin):
    distances={origin:0};queue=[origin]
    while queue:
        node=queue.pop(0)
        for edge in db.execute('SELECT downstream,travel_minutes FROM river_edge WHERE upstream=?',(node,)):
            distance=distances[node]+edge['travel_minutes']
            if edge['downstream'] not in distances or distance<distances[edge['downstream']]:
                distances[edge['downstream']]=distance;queue.append(edge['downstream'])
    return distances

def current(db,case_id):
    return db.execute('SELECT * FROM incident_version WHERE case_id=? ORDER BY version DESC LIMIT 1',(case_id,)).fetchone()

def observations(db):
    out=[]
    for r in db.execute("SELECT r.event_id,r.station,r.at,r.value FROM station_reading r JOIN station_decision d ON d.reading_id=r.event_id WHERE d.status='trusted'"):
        out.append(dict(r,source='station'))
    retested=set()
    sample_rows=db.execute("SELECT b.bottle_id,b.station,b.at,l.value,l.retest_of FROM sample_bottle b JOIN sample_decision d ON d.bottle_id=b.bottle_id JOIN lab_result l ON l.event_id=d.result_event WHERE d.status='trusted'").fetchall()
    for r in sample_rows:
        if r['retest_of']: retested.add(r['retest_of'])
    for r in sample_rows:
        if r['bottle_id'] not in retested:
            out.append({'event_id':r['bottle_id'],'station':r['station'],'at':r['at'],'value':r['value'],'source':'lab'})
    return out

def evaluate(db,case):
    distance=routes(db,case['origin'])
    start=datetime.fromisoformat(case['window_start']);end=datetime.fromisoformat(case['window_end'])
    valid=[]
    for o in observations(db):
        if o['station'] not in distance: continue
        at=datetime.fromisoformat(o['at']);travel=timedelta(minutes=distance[o['station']])
        if start+travel<=at<=end+travel:
            valid.append(o)
    high=[o for o in valid if o['value']>case['threshold']]
    level='warning' if len(high)>=2 else ('candidate' if high else 'clear')
    state='confirmed' if level=='warning' else ('pending' if level=='candidate' else 'closed')
    affected=[]
    if level=='warning':
        affected=[i['id'] for i in db.execute('SELECT * FROM intake ORDER BY id') if i['station'] in distance]
    inputs=sorted(o['event_id'] for o in valid)
    reason=f'{len(high)} independent high observations; directed reachability from {case["origin"]}'
    old=current(db,case['id'])
    if old and (old['level'],old['status'],old['affected_json'],old['inputs_json'])==(level,state,json.dumps(affected),json.dumps(inputs)):
        return dict(old)
    version=old['version']+1 if old else 1
    db.execute('INSERT INTO incident_version(case_id,version,level,status,affected_json,inputs_json,reason) VALUES(?,?,?,?,?,?,?)',
               (case['id'],version,level,state,json.dumps(affected),json.dumps(inputs),reason))
    for intake_id in affected:
        db.execute('INSERT INTO affected_intake(case_id,version,intake_id) VALUES(?,?,?)',(case['id'],version,intake_id))
    log(db,'incident',case['id'],state,reason)
    from .notice import reconcile_case
    reconcile_case(db,case['id'])
    return dict(current(db,case['id']))

def recompute_all(db):
    return [evaluate(db,c) for c in db.execute('SELECT * FROM incident_case ORDER BY id').fetchall()]

def import_data(db,data):
    with db:
        if data.get('sample_data'):
            from .sample import ingest
            ingest(db,data['sample_data'])
        for edge in data.get('edges',[]):
            record(db,edge['event_id'],'river_edge',edge)
            db.execute('INSERT INTO river_edge(upstream,downstream,travel_minutes) VALUES(?,?,?) ON CONFLICT(upstream,downstream) DO UPDATE SET travel_minutes=excluded.travel_minutes',
                       (edge['upstream'],edge['downstream'],edge['travel_minutes']))
        for i in data.get('intakes',[]):
            record(db,i['event_id'],'intake',i)
            db.execute('INSERT INTO intake(id,station) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET station=excluded.station',(i['id'],i['station']))
        for c in data.get('cases',[]):
            if record(db,c['event_id'],'incident_case',c):
                db.execute('INSERT INTO incident_case(id,origin,threshold,window_start,window_end) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET origin=excluded.origin,threshold=excluded.threshold,window_start=excluded.window_start,window_end=excluded.window_end',
                           (c['id'],c['origin'],c['threshold'],c['window_start'],c['window_end']))
        return recompute_all(db)
