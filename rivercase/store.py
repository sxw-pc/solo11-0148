import sqlite3

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,received_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS station_assignment(event_id TEXT PRIMARY KEY,instrument TEXT NOT NULL,station TEXT NOT NULL,start TEXT NOT NULL,end TEXT);
CREATE TABLE IF NOT EXISTS calibration_certificate(event_id TEXT PRIMARY KEY,cert_id TEXT NOT NULL,version INTEGER NOT NULL,instrument TEXT NOT NULL,station TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,coefficient REAL NOT NULL);
CREATE TABLE IF NOT EXISTS station_reading(event_id TEXT PRIMARY KEY,instrument TEXT NOT NULL,station TEXT NOT NULL,at TEXT NOT NULL,value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS station_decision(reading_id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,cert_event TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sample_bottle(event_id TEXT PRIMARY KEY,bottle_id TEXT NOT NULL UNIQUE,station TEXT NOT NULL,at TEXT NOT NULL,seal TEXT NOT NULL,replays INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sample_conflict(event_id TEXT PRIMARY KEY,bottle_id TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_handoff(event_id TEXT PRIMARY KEY,bottle_id TEXT NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lab_result(event_id TEXT PRIMARY KEY,bottle_id TEXT NOT NULL,value REAL NOT NULL,method TEXT NOT NULL,method_valid_to TEXT NOT NULL,detection_limit REAL NOT NULL,threshold REAL NOT NULL,retest_of TEXT);
CREATE TABLE IF NOT EXISTS sample_decision(bottle_id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,result_event TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS river_edge(upstream TEXT NOT NULL,downstream TEXT NOT NULL,travel_minutes INTEGER NOT NULL,PRIMARY KEY(upstream,downstream));
CREATE TABLE IF NOT EXISTS intake(id TEXT PRIMARY KEY,station TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incident_case(id TEXT PRIMARY KEY,origin TEXT NOT NULL,threshold REAL NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incident_version(case_id TEXT NOT NULL,version INTEGER NOT NULL,level TEXT NOT NULL,status TEXT NOT NULL,affected_json TEXT NOT NULL,inputs_json TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(case_id,version));
CREATE TABLE IF NOT EXISTS affected_intake(case_id TEXT NOT NULL,version INTEGER NOT NULL,intake_id TEXT NOT NULL,PRIMARY KEY(case_id,version,intake_id));
CREATE TABLE IF NOT EXISTS notice_revision(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,incident_version INTEGER NOT NULL,level TEXT NOT NULL,recipients_json TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipient_receipt(event_id TEXT PRIMARY KEY,notice_id TEXT NOT NULL,intake_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_obligation(notice_id TEXT NOT NULL,intake_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(notice_id,intake_id,kind));
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,evidence_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
def log(db,subsystem,object_id,state,reason,evidence_id=None):
    db.execute('INSERT INTO decision_log(subsystem,object_id,state,reason,evidence_id) VALUES(?,?,?,?,?)',
               (subsystem,object_id,state,reason,evidence_id))
def transition(db,table,key,value,status,reason,event_id=None,extra=None):
    old=db.execute(f'SELECT status,reason,version FROM {table} WHERE {key}=?',(value,)).fetchone()
    if old and old['status']==status and old['reason']==reason: return old['version']
    version=(old['version']+1) if old else 1
    if table=='station_decision':
        db.execute('INSERT INTO station_decision(reading_id,status,reason,cert_event,version) VALUES(?,?,?,?,?) ON CONFLICT(reading_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,cert_event=excluded.cert_event,version=excluded.version',
                   (value,status,reason,extra,version))
    elif table=='sample_decision':
        db.execute('INSERT INTO sample_decision(bottle_id,status,reason,result_event,version) VALUES(?,?,?,?,?) ON CONFLICT(bottle_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,result_event=excluded.result_event,version=excluded.version',
                   (value,status,reason,extra,version))
    log(db,table,value,status,reason,event_id)
    return version
