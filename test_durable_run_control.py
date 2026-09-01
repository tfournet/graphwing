#!/usr/bin/env python3
"""Provider-free behavioral coverage for dormant durable run control."""
import json, threading, unittest
from run_control_model import DurableRunControlModel, RunControlConflict
import server

def evaluator_route():
 return {'route_version':'normal-v1','launcher':'codex','provider':'openai','model':'gpt-5.6-sol'}
def initialize_request(**kw):
 value={'root_workflow_id':'wf-1','root_workflow_version_id':'wfv-1','root_workflow_run_id':'run-1','purpose':'implement_slice',
  'budgets':{'attempts':4,'turns':300,'wall_seconds':7200,'tokens':500000,'provider_cost_usd':'25.000000000000'},'initial_route':evaluator_route()}
 value.update(kw); return value
def reservation_request(route=None,handoff=None):
 value={'candidate_route':route or evaluator_route(),'envelope':{'turns':40,'wall_seconds':600,'tokens':100000,'provider_cost_usd':'2.000000000000'},
  'endpoint':'/v1/agent/run','launcher_fingerprint':'sha256:'+'1'*64,'exact_request_body_sha256':'2'*64,'repository':'scratch','branch':'main',
  'head_sha':'3'*40,'task_sha256':'4'*64,'permission_profile':'workspace-write-v1','callback_binding_sha256':'5'*64}
 if handoff is not None:value['handoff']=handoff
 return value
def allow(_):return 200,{'version':'run-control-v1','decision':'continue_same_model','classification':None,'retryable':None}
def receipt(store,held,usage=None):
 state=store.load_verified(); reservation=state['outstanding_reservation']
 return {'receipt_id':'rec1-'+'8'*64,'attempt_id':held['attempt_id'],'run_control_id':state['run_control_id'],'authorization_id':held['authorization_id'],
  'launch_descriptor_sha256':held['launch_descriptor_sha256'],'job_id':'job-1','terminal_status':'failed','route':reservation['route'],
  'usage':usage or {'turns':21,'wall_seconds':300,'tokens':88000,'provider_cost_usd':'1.250000000000'},
  'progress':{'checkpoint':1,'failing_regression_present':True,'production_diff_bytes':1200,'focused_tests_green':False,'diff_fingerprint':'9'*64},
  'constraint_signals':[],'verified_outcome':False}

class DurableRunControlTests(unittest.TestCase):
 def test_concurrent_initialize_exact_replay(self):
  store=DurableRunControlModel(); barrier=threading.Barrier(8); out=[]; errors=[]
  def run():
   barrier.wait()
   try:out.append(store.initialize(initialize_request()))
   except Exception as exc:errors.append(exc)
  threads=[threading.Thread(target=run) for _ in range(8)]
  for thread in threads:thread.start()
  for thread in threads:thread.join()
  self.assertFalse(errors);self.assertEqual(sum(x['created'] for x in out),1)
  self.assertEqual(len({x['state_sha256'] for x in out}),1);self.assertEqual(store.pointer['phase'],'stable')

 def test_pending_recovery_and_conflicting_record_fence(self):
  store=DurableRunControlModel();store.initialize(initialize_request());state=store.load_verified()
  target={**state,'logical_revision':1,'last_transition':{'operation_id':'op1-'+'2'*64,'type':'test','from_logical_revision':0,'request_sha256':'1'*64}}
  pending=store.prepare_for_test(target,'test','1'*64);self.assertFalse(store.launch_authorized())
  self.assertTrue(store.recover()['recovered']);self.assertEqual(store.recover()['state_sha256'],store.pointer['state_sha256'])
  other=DurableRunControlModel();other.initialize(initialize_request(root_workflow_run_id='run-2'));state=other.load_verified()
  target={**state,'logical_revision':1,'last_transition':{'operation_id':'op1-'+'3'*64,'type':'test','from_logical_revision':0,'request_sha256':'4'*64}}
  pending=other.prepare_for_test(target,'test','4'*64);other.records[pending['target_state_record_key']]={'conflict':True}
  with self.assertRaisesRegex(RunControlConflict,'conflicting'):other.recover()
  self.assertEqual(other.pointer['phase'],'fenced')

 def test_evaluator_stops_aggregate_limits(self):
  route=evaluator_route(); attempts=[]
  for seq,cost in ((1,'2.680000000000'),(2,'2.690000000000')):
   attempts.append({'sequence':seq,'attempt_id':f'att-{seq}',**route,'usage':{'turns':130,'wall_seconds':1800,'tokens':150000,'provider_cost_usd':cost},
    'reservation':{'attempt_id':f'att-{seq}','turns':130,'wall_seconds':1800,'tokens':150000,'provider_cost_usd':cost},'terminal_state':'terminal',
    'progress':{'checkpoint':1,'failing_regression_present':True,'production_diff_bytes':1,'focused_tests_green':False,'diff_fingerprint':'a'*64},'constraint_signals':[],'verified_outcome':False})
  body={'run_id':'rc1-'+'b'*64,'route':route,'budgets':{'attempts':4,'turns':260,'wall_seconds':7200,'tokens':500000,'provider_cost_usd':'5.370000000000'},
   'attempts':attempts,'next_attempt':3,'next_envelope':{'attempt_id':'att-3','turns':1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'0.000000000001'}}
  status,result=server.run_control_evaluate(json.dumps(body).encode());self.assertEqual(status,200);self.assertEqual(result['decision'],'terminate')
  self.assertIn('turns_exhausted',result['evidence_codes']);self.assertIn('provider_cost_exhausted',result['evidence_codes'])

 def test_reservation_binds_generated_attempt_and_single_outstanding(self):
  store=DurableRunControlModel();store.initialize(initialize_request());seen=[]
  def evaluator(body):seen.append(json.loads(body));return allow(body)
  held=store.reserve(reservation_request(),evaluator,lambda:'6'*64);state=store.load_verified()
  self.assertEqual(seen[0]['budgets'],state['immutable']['budgets']);self.assertEqual(seen[0]['attempts'],[])
  self.assertEqual(seen[0]['next_envelope']['attempt_id'],held['attempt_id']);self.assertEqual(state['outstanding_reservation']['attempt_id'],held['attempt_id'])
  with self.assertRaisesRegex(RunControlConflict,'outstanding'):store.reserve(reservation_request(),evaluator,lambda:'7'*64)
  self.assertEqual(len(seen),1)

 def test_receipt_exact_replay_unknown_usage_and_conflict(self):
  store=DurableRunControlModel();store.initialize(initialize_request());held=store.reserve(reservation_request(),allow,lambda:'6'*64)
  store.mark_authorization_consumed('6'*64,2,'7'*64);value=receipt(store,held)
  self.assertTrue(store.reconcile_receipt(value)['reconciled']);self.assertFalse(store.reconcile_receipt(value)['reconciled'])
  with self.assertRaisesRegex(RunControlConflict,'conflicting'):store.reconcile_receipt({**value,'job_id':'other'})
  unknown=DurableRunControlModel();unknown.initialize(initialize_request(root_workflow_run_id='run-unknown'));held=unknown.reserve(reservation_request(),allow,lambda:'a'*64)
  unknown.mark_authorization_consumed('a'*64,2,'b'*64);env=unknown.load_verified()['outstanding_reservation']['envelope']
  unknown.reconcile_receipt(receipt(unknown,held,{'turns':None,'wall_seconds':1,'tokens':1,'provider_cost_usd':'0'}))
  attempt=unknown.load_verified()['attempts'][0];self.assertEqual(attempt['reconciliation']['kind'],'authority_lost');self.assertEqual(attempt['charged_usage'],env)

 def test_cross_model_handoff_appends_once_and_terminal_skips_challenge(self):
  store=DurableRunControlModel();store.initialize(initialize_request(root_workflow_run_id='run-handoff'))
  first=store.reserve(reservation_request(),allow,lambda:'1'*64);store.mark_authorization_consumed('1'*64,2,'2'*64);store.reconcile_receipt(receipt(store,first))
  cross={'route_version':'availability-fallback-v1','launcher':'claude','provider':'anthropic','model':'claude-sonnet-5'}
  handoff=lambda _: (200,{'version':'run-control-v1','decision':'handoff_cross_model','classification':None,'retryable':None})
  with self.assertRaisesRegex(RunControlConflict,'explicit'):store.reserve(reservation_request(cross),handoff,lambda:'5'*64)
  store.reserve(reservation_request(cross,{'reason_code':'provider_switch'}),handoff,lambda:'5'*64)
  self.assertEqual(len(store.load_verified()['handoffs']),1)
  terminal=DurableRunControlModel();terminal.initialize(initialize_request(root_workflow_run_id='run-stop'));called=[]
  stop=lambda _: (200,{'version':'run-control-v1','decision':'terminate','classification':'budget_exhausted','retryable':False})
  self.assertFalse(terminal.reserve(reservation_request(),stop,lambda:called.append(1))['reserved']);self.assertFalse(called)

if __name__=='__main__':unittest.main()
