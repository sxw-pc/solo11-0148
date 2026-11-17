import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
import rivercase
from rivercase.store import connect
from rivercase import station,sample,incident,notice,report

ROOT=Path(__file__).resolve().parents[1]
def fixture(name): return json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8-sig'))
def state(db,table,key,value):
    return db.execute(f'SELECT * FROM {table} WHERE {key}=?',(value,)).fetchone()

class RiverCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(str(Path(self.tmp.name)/'case.sqlite'))
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def prepared(self):
        station.import_data(self.db,fixture('station_valid.json'))
        sample.import_data(self.db,fixture('sample_valid.json'))
        incident.import_data(self.db,fixture('incident_valid.json'))
    def notified(self):
        self.prepared()
        notice.import_data(self.db,fixture('notice_valid.json'))
    def test_module_origin(self):
        print(rivercase.__file__)
        self.assertTrue(str(Path(rivercase.__file__).resolve()).startswith(str(ROOT)))
    def test_four_ledgers_independent(self):
        self.assertEqual(station.import_data(self.db,fixture('station_valid.json'))[0]['status'],'trusted')
        self.assertEqual(sample.import_data(self.db,fixture('sample_valid.json'))[0]['status'],'trusted')
        self.assertEqual(incident.import_data(self.db,fixture('incident_valid.json'))[0]['status'],'confirmed')
        self.assertEqual(notice.import_data(self.db,fixture('notice_valid.json'))[0]['status'],'delivered')
    def test_calibration_uses_sample_time(self):
        data=fixture('station_valid.json')
        data['certificates'][0]['valid_to']='2026-11-17T08:59:00'
        self.assertEqual(station.import_data(self.db,data)[0]['status'],'pending')
    def test_transfer_invalidates_incident_and_notice(self):
        self.notified()
        self.assertEqual(station.import_data(self.db,fixture('station_moved.json'))[0]['status'],'ineligible')
        self.assertEqual(incident.current(self.db,'E1')['level'],'candidate')
        self.assertEqual(state(self.db,'notice_revision','id','N1')['status'],'withdraw_pending')
    def test_parallel_certificate_disputed(self):
        base=fixture('station_valid.json')
        station.import_data(self.db,base)
        other=dict(base['certificates'][0],event_id='cert-A-conflict',coefficient=1.4)
        station.import_data(self.db,{'certificates':[other]})
        self.assertEqual(state(self.db,'station_decision','reading_id','read-A')['status'],'disputed')
    def test_custody_gap_isolates_high_value(self):
        data=fixture('sample_valid.json');data['handoffs']=data['handoffs'][:1]
        self.assertEqual(sample.import_data(self.db,data)[0]['status'],'isolated')
    def test_duplicate_bottle_not_independent(self):
        self.prepared()
        repeat=dict(fixture('sample_valid.json')['bottles'][0],event_id='b-B1-repeat')
        sample.import_data(self.db,{'bottles':[repeat]})
        self.assertEqual(state(self.db,'sample_bottle','bottle_id','B1')['replays'],1)
        self.assertEqual(incident.current(self.db,'E1')['level'],'warning')
        self.assertEqual(len(json.loads(incident.current(self.db,'E1')['inputs_json'])),2)
    def test_conflicting_bottle_removes_event_evidence(self):
        self.notified()
        out=sample.import_data(self.db,fixture('sample_gap.json'))
        self.assertEqual({x['bottle_id']:x['status'] for x in out}['B1'],'disputed')
        self.assertEqual(incident.current(self.db,'E1')['level'],'candidate')
        self.assertEqual(state(self.db,'notice_revision','id','N1')['status'],'withdraw_pending')
    def test_method_detection_limit_cannot_retract_warning(self):
        self.prepared()
        retest=fixture('sample_retest.json')
        retest['results'][0]['detection_limit']=20.0
        self.assertEqual(sample.import_data(self.db,retest)[-1]['status'],'pending')
        self.assertEqual(incident.current(self.db,'E1')['level'],'warning')
    def test_parallel_laboratory_results_are_disputed(self):
        self.prepared()
        r=dict(fixture('sample_valid.json')['results'][0],event_id='lab-B1-conflict',value=25.0)
        sample.import_data(self.db,{'results':[r]})
        self.assertEqual(state(self.db,'sample_decision','bottle_id','B1')['status'],'disputed')
        self.assertEqual(incident.current(self.db,'E1')['level'],'candidate')
    def test_late_retest_appends_incident_version(self):
        self.notified()
        out=incident.import_data(self.db,fixture('incident_late.json'))
        self.assertEqual(out[0]['level'],'candidate')
        self.assertEqual(self.db.execute("SELECT count(*) FROM incident_version WHERE case_id='E1'").fetchone()[0],2)
        self.assertEqual(state(self.db,'notice_revision','id','N1')['status'],'withdraw_pending')
    def test_invalid_retest_cannot_supersede_high_sample(self):
        self.prepared()
        data=fixture('sample_retest.json')
        data['bottles'][0]['station']='UPSTREAM'
        self.assertEqual(sample.import_data(self.db,data)[-1]['status'],'pending')
        self.assertEqual(incident.current(self.db,'E1')['level'],'warning')
    def test_direction_and_travel_exclude_reverse_sample(self):
        self.prepared()
        c=dict(fixture('incident_valid.json')['cases'][0],event_id='case-reverse',id='E2',origin='C')
        result=incident.import_data(self.db,{'cases':[c]})[1]
        self.assertEqual(result['level'],'clear')
        self.assertEqual(json.loads(result['affected_json']),[])
    def test_distant_intake_outside_propagation_window(self):
        self.prepared()
        added={'edges':[{'event_id':'edge-CD','upstream':'C','downstream':'D','travel_minutes':90}],
               'intakes':[{'event_id':'intake-W','id':'W','station':'D'}]}
        result=incident.import_data(self.db,added)[0]
        self.assertEqual(result['level'],'warning')
        self.assertEqual(json.loads(result['affected_json']),['X','Y'])
    def test_missing_recipient_blocks_notice(self):
        self.prepared()
        self.assertEqual(notice.import_data(self.db,fixture('notice_missing.json'))[0]['status'],'blocked')
    def test_receipt_must_match_open_obligation(self):
        self.notified()
        fake={'event_id':'r-fake','notice_id':'N1','intake_id':'Z','kind':'deliver','at':'2026-11-17T12:00:00'}
        with self.assertRaises(ValueError): notice.import_data(self.db,{'receipts':[fake]})
        self.assertIsNone(state(self.db,'recipient_receipt','event_id','r-fake'))
    def test_travel_time_excludes_early_downstream_reading(self):
        self.prepared()
        additional={'assignments':[{'event_id':'as-B','instrument':'I-B','station':'B','start':'2026-11-01T00:00:00'}],
                    'certificates':[{'event_id':'cert-B','cert_id':'C-B','version':1,'instrument':'I-B','station':'B','valid_from':'2026-11-01T00:00:00','valid_to':'2026-12-01T00:00:00','coefficient':1}],
                    'readings':[{'event_id':'read-B-early','instrument':'I-B','station':'B','at':'2026-11-17T09:10:00','value':100}]}
        station.import_data(self.db,additional)
        self.assertNotIn('read-B-early',json.loads(incident.current(self.db,'E1')['inputs_json']))
    def test_withdraw_receipts_required_for_all(self):
        self.notified()
        incident.import_data(self.db,fixture('incident_late.json'))
        one={'event_id':'withdraw-X','notice_id':'N1','intake_id':'X','kind':'withdraw','at':'2026-11-17T13:00:00'}
        notice.import_data(self.db,{'receipts':[one]})
        self.assertEqual(state(self.db,'notice_revision','id','N1')['status'],'withdraw_pending')
        two=dict(one,event_id='withdraw-Y',intake_id='Y')
        notice.import_data(self.db,{'receipts':[two]})
        self.assertEqual(state(self.db,'notice_revision','id','N1')['status'],'withdrawn')
    def test_idempotent_input_and_changed_event(self):
        data=fixture('station_valid.json')
        station.import_data(self.db,data)
        station.import_data(self.db,data)
        self.assertEqual(self.db.execute('SELECT count(*) FROM station_reading').fetchone()[0],1)
        altered=copy.deepcopy(data);altered['readings'][0]['value']=99
        with self.assertRaises(ValueError): station.import_data(self.db,altered)
    def test_partial_batch_rolls_back(self):
        data=fixture('sample_valid.json')
        sample.import_data(self.db,data)
        mixed={'bottles':[dict(data['bottles'][0],event_id='new-bottle',bottle_id='NEW'),dict(data['bottles'][0],seal='CHANGED')]}
        with self.assertRaises(ValueError): sample.import_data(self.db,mixed)
        self.assertIsNone(state(self.db,'sample_bottle','bottle_id','NEW'))
    def test_cli_demo_and_raw_replay(self):
        path=str(Path(self.tmp.name)/'cli.sqlite')
        done=subprocess.run([sys.executable,'-m','rivercase','demo','--db',path],cwd=ROOT,capture_output=True,text=True,check=True)
        data=json.loads(done.stdout)
        self.assertTrue(data['audit']['replay_ok'])
        self.assertEqual(data['audit']['journal_count'],4)
        self.assertEqual(data['report']['notice_revision'][0]['status'],'delivered')
    def test_report_contains_history_and_obligations(self):
        self.notified();station.import_data(self.db,fixture('station_moved.json'))
        out=report.build(self.db)
        self.assertEqual(len(out['incident_version']),2)
        self.assertEqual(len([x for x in out['notice_obligation'] if x['kind']=='withdraw']),2)

if __name__=='__main__':unittest.main()
