import json
from datetime import datetime,timedelta
from itertools import product
from .source import record
from .store import log

def routes(db,origin):
    """Shortest possible travel minutes from origin along directed edges.

    Gate state is ignored here: callers needing gate-aware multi-route
    reasoning use the explanation enumeration in :func:`explain`.
    """
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

def _load_network(db):
    edges=[]
    for r in db.execute('SELECT * FROM river_edge ORDER BY upstream,downstream'):
        gate=db.execute('SELECT * FROM river_gate WHERE edge_upstream=? AND edge_downstream=?',
                        (r['upstream'],r['downstream'])).fetchone()
        edges.append({'up':r['upstream'],'down':r['downstream'],'tmin':r['travel_minutes'],
                      'tmax':r['travel_minutes_max'] or r['travel_minutes'],
                      'gate':dict(gate) if gate else None})
    logs={}
    for g in db.execute('SELECT * FROM gate_log ORDER BY at,event_id'):
        logs.setdefault(g['gate_id'],[]).append(
            {'at':datetime.fromisoformat(g['at']),'state':g['state'],'event_id':g['event_id'],'at_iso':g['at']})
    return edges,logs

def _gate_state(gid,when,logs,assignment):
    """Return (state, verified) for gate gid at datetime when."""
    verified=None
    for entry in logs.get(gid,()):
        if entry['at']<=when: verified=entry
        else: break
    if verified is not None:
        return verified['state'],True
    return assignment[gid],False

def _walk(edges,origin,logs,assignment,start):
    """Enumerate simple paths under one gate-state assignment.

    Returns node -> list of {'edges','unverified'}; unverified holds the
    gate ids whose state at passage was assumed rather than log-verified.
    """
    adj={}
    for e in edges: adj.setdefault(e['up'],[]).append(e)
    found={origin:[{'edges':[],'unverified':frozenset()}]}
    stack=[(origin,[],frozenset([origin]),0,frozenset())]
    while stack:
        node,path,seen,cum_min,unverified=stack.pop()
        for e in adj.get(node,()):
            if e['down'] in seen: continue
            ok=True;next_unverified=unverified
            if e['gate']:
                when=start+timedelta(minutes=cum_min)
                state,is_verified=_gate_state(e['gate']['gate_id'],when,logs,assignment)
                if state!=e['gate']['gate_state']: ok=False
                elif not is_verified:
                    next_unverified=unverified|{e['gate']['gate_id']}
            if not ok: continue
            new_path=path+[e]
            found.setdefault(e['down'],[]).append(
                {'edges':new_path,'unverified':next_unverified})
            stack.append((e['down'],new_path,seen|{e['down']},cum_min+e['tmin'],next_unverified))
    return found

def _explain(db,case):
    """Enumerate every network-wide feasible propagation explanation.

    A single assignment of open/closed states to unverified gate periods
    must jointly explain every trusted high observation reachable under
    at least one assignment: the same gate is never counted open for one
    piece of evidence and closed for another inside one explanation.
    """
    edges,logs=_load_network(db)
    start=datetime.fromisoformat(case['window_start']);end=datetime.fromisoformat(case['window_end'])
    gate_ids=sorted({e['gate']['gate_id'] for e in edges if e['gate']})
    # A gate needs an assumed state variable only while its earliest log
    # does not already cover the case window; logged periods stay fixed.
    variables=[gid for gid in gate_ids
               if not logs.get(gid) or logs[gid][0]['at']>start]
    obs=observations(db)
    drafts=[]
    for combo in product(('open','closed'),repeat=len(variables)):
        assignment=dict(zip(variables,combo))
        reach=_walk(edges,case['origin'],logs,assignment,start)
        explained=set()
        for o in obs:
            if o['station'] not in reach: continue
            at=datetime.fromisoformat(o['at'])
            for p in reach[o['station']]:
                lo=sum(x['tmin'] for x in p['edges']);hi=sum(x['tmax'] for x in p['edges'])
                if start+timedelta(minutes=lo)<=at<=end+timedelta(minutes=hi):
                    explained.add(o['event_id']);break
        drafts.append({'assignment':assignment,'reach':reach,'explained':explained})
    reachable_stations=set()
    for d in drafts: reachable_stations.update(d['reach'])
    reachable_highs=[o for o in obs if o['value']>case['threshold'] and o['station'] in reachable_stations]
    required={o['event_id'] for o in reachable_highs}
    explanations=[d for d in drafts if required.issubset(d['explained'])]
    return {'drafts':drafts,'explanations':explanations,'logs':logs,
            'gate_ids':gate_ids,'variables':variables,'obs':obs,
            'reachable_highs':reachable_highs,'start':start,'end':end}

def explain(db,case):
    """Feasible explanations, intake impact classes and source conflicts."""
    data=_explain(db)
    explanations=data['explanations'];logs=data['logs']
    start=data['start'];end=data['end'];highs=data['reachable_highs']
    # Pairwise conflicts: two trusted highs never jointly explainable.
    explain_sets={o['event_id']:set() for o in highs}
    for idx,x in enumerate(explanations):
        for o in highs:
            if o['event_id'] in x['explained']: explain_sets[o['event_id']].add(idx)
    conflicts=[]
    for i,a in enumerate(highs):
        for b in highs[i+1:]:
            if not (explain_sets[a['event_id']] & explain_sets[b['event_id']]):
                conflicts.append({'a':a['event_id'],'b':b['event_id'],
                                  'a_station':a['station'],'b_station':b['station']})
        if not explain_sets[a['event_id']]:
            conflicts.append({'a':a['event_id'],'b':None,'a_station':a['station'],'b_station':None})
    # A trusted high at the origin anchors the release window; otherwise
    # the case window itself bounds propagation.
    origin_times=[datetime.fromisoformat(o['at']) for o in highs if o['station']==case['origin']]
    plume_start=min(origin_times) if origin_times else start
    intakes=db.execute('SELECT * FROM intake ORDER BY id').fetchall()
    windows={};definite=[];possible=[];unreachable=[]
    for i in intakes:
        per=[]
        for x in explanations:
            paths=x['reach'].get(i['station'])
            if not paths: continue
            eligible=[]
            for p in paths:
                lo=sum(e['tmin'] for e in p['edges']);hi=sum(e['tmax'] for e in p['edges'])
                if plume_start+timedelta(minutes=lo)<=end: eligible.append((p,lo,hi))
            if not eligible: continue
            lo=min(x_lo for _,x_lo,_ in eligible);hi=max(x_hi for _,_,x_hi in eligible)
            unverified=set()
            for p,_,_ in eligible: unverified.update(p['unverified'])
            per.append((lo,hi,unverified))
        if not per:
            unreachable.append(i['id']);continue
        w_start=min(plume_start+timedelta(minutes=lo) for lo,_,_ in per)
        w_end=max(end+timedelta(minutes=hi) for _,hi,_ in per)
        unverified_gates=sorted({g for _,_,u in per for g in u})
        windows[i['id']]={'start':w_start.isoformat(),'end':w_end.isoformat(),
                          'unverified_gates':unverified_gates}
        if len(per)==len(explanations): definite.append(i['id'])
        else: possible.append(i['id'])
    gate_summary=[]
    for gid in data['gate_ids']:
        assumed=sorted({x['assignment'][gid] for x in explanations if gid in x['assignment']})
        gate_summary.append({'gate_id':gid,
                             'verified':[{'at':e['at_iso'],'state':e['state'],'event_id':e['event_id']}
                                         for e in logs.get(gid,())],
                             'assumed_states':assumed})
    return {'explanations':explanations,'definite':sorted(definite),'possible':sorted(possible),
            'unreachable':sorted(unreachable),'windows':windows,'conflicts':conflicts,
            'gate_summary':gate_summary,'variables':data['variables'],
            'reachable_highs':highs,'start':start,'end':end,'plume_start':plume_start}

def evaluate(db,case):
    model=explain(db,case)
    explanations=model['explanations']
    valid=[]
    explain_inputs=set()
    for x in explanations: explain_inputs.update(x['explained'])
    for o in observations(db):
        if o['event_id'] in explain_inputs: valid.append(o)
    high=[o for o in valid if o['value']>case['threshold']]
    disputed=bool(model['conflicts']) or not explanations
    if disputed:
        level,state='candidate','disputed'
    elif len(high)>=2:
        level,state='warning','confirmed'
    elif high:
        level,state='candidate','pending'
    else:
        level,state='clear','closed'
    affected=model['definite'] if state=='confirmed' else []
    possible=model['possible'] if state!='closed' else []
    inputs=sorted(o['event_id'] for o in valid)
    if disputed:
        reason=f'gate-state conflict: no network-wide explanation covers all trusted highs; {len(model["conflicts"])} conflicting source pair(s)'
    else:
        reason=f'{len(high)} independent high observations; {len(explanations)} jointly feasible gate-state explanation(s) over directed river network'
    affected_s=json.dumps(affected);inputs_s=json.dumps(inputs)
    possible_s=json.dumps(possible);unreachable_s=json.dumps(model['unreachable'])
    windows_s=json.dumps(model['windows'],sort_keys=True)
    gates_s=json.dumps(model['gate_summary'],sort_keys=True)
    conflicts_s=json.dumps(model['conflicts'],sort_keys=True)
    old=current(db,case['id'])
    if old and (old['level'],old['status'],old['affected_json'],old['inputs_json'],
                old['possible_json'],old['unreachable_json'],old['windows_json'],
                old['gates_json'],old['conflicts_json'])==(
                    level,state,affected_s,inputs_s,possible_s,unreachable_s,windows_s,gates_s,conflicts_s):
        from .notice import reconcile_duty
        reconcile_duty(db,case['id'])
        return dict(old)
    version=old['version']+1 if old else 1
    db.execute('INSERT INTO incident_version(case_id,version,level,status,affected_json,inputs_json,reason,possible_json,unreachable_json,windows_json,gates_json,conflicts_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (case['id'],version,level,state,affected_s,inputs_s,reason,
                possible_s,unreachable_s,windows_s,gates_s,conflicts_s))
    for intake_id in affected:
        db.execute('INSERT INTO affected_intake(case_id,version,intake_id) VALUES(?,?,?)',(case['id'],version,intake_id))
    log(db,'incident',case['id'],state,reason)
    from .notice import reconcile_case,reconcile_duty
    reconcile_case(db,case['id'])
    reconcile_duty(db,case['id'])
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
            db.execute('INSERT INTO river_edge(upstream,downstream,travel_minutes,travel_minutes_max) VALUES(?,?,?,?) ON CONFLICT(upstream,downstream) DO UPDATE SET travel_minutes=excluded.travel_minutes,travel_minutes_max=excluded.travel_minutes_max',
                       (edge['upstream'],edge['downstream'],edge['travel_minutes'],edge.get('travel_minutes_max')))
        for g in data.get('gates',[]):
            if record(db,g['event_id'],'river_gate',g):
                db.execute('INSERT INTO river_gate(event_id,gate_id,edge_upstream,edge_downstream,gate_state) VALUES(?,?,?,?,?)',
                           (g['event_id'],g['gate_id'],g['edge_upstream'],g['edge_downstream'],g['gate_state']))
        for gl in data.get('gate_logs',[]):
            if record(db,gl['event_id'],'gate_log',gl):
                db.execute('INSERT INTO gate_log(event_id,gate_id,state,at) VALUES(?,?,?,?)',
                           (gl['event_id'],gl['gate_id'],gl['state'],gl['at']))
        for i in data.get('intakes',[]):
            record(db,i['event_id'],'intake',i)
            db.execute('INSERT INTO intake(id,station,shutdown_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET station=excluded.station,shutdown_at=excluded.shutdown_at',
                       (i['id'],i['station'],i.get('shutdown_at')))
        for c in data.get('cases',[]):
            if record(db,c['event_id'],'incident_case',c):
                db.execute('INSERT INTO incident_case(id,origin,threshold,window_start,window_end) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET origin=excluded.origin,threshold=excluded.threshold,window_start=excluded.window_start,window_end=excluded.window_end',
                           (c['id'],c['origin'],c['threshold'],c['window_start'],c['window_end']))
        return recompute_all(db)
