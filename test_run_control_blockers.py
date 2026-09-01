import json, unittest
from pathlib import Path
import server
from run_control_model import DurableRunControlModel, RunControlConflict
from test_durable_run_control import initialize_request, reservation_request, evaluator_route, allow, receipt
R=Path(__file__).parent
def g(n): return json.loads((R/'graphs'/f'{n}.json').read_text())
def ns(n): return {x['id']:x for x in g(n)['spec']['nodes']}
def es(n): return {x['id']:x for x in g(n)['spec']['edges']}

class Blockers(unittest.TestCase):
 def test_01_root_identity(self):
  self.assertNotIn('WORKFLOW.',json.dumps(g('run-control-initialize'))+json.dumps(g('run-control-state')))
  self.assertIn('CTX.INPUT.root_identity',json.dumps(ns('run-control-initialize')['root_identity']))
  self.assertIn('CTX.INPUT.run_control_id',json.dumps(ns('run-control-state')['input_identity']))

 def test_02_record_race_and_init_recovery(self):
  n,e=ns('run-control-transition'),es('run-control-transition')
  self.assertTrue({'post_upsert_get','target_upsert_gate','fenced_pointer','initialize_replay_gate','prepare_loser_exact_gate','publish_loser_exact_gate'}<=set(n))
  self.assertEqual(e['x-target-contradiction-fence']['target'],'fence_ready'); self.assertEqual(e['x-fence-ready-pointer']['target'],'fenced_pointer')
  self.assertEqual(e['x-initialize-replay-success']['target'],'transition_ready'); self.assertEqual(e['x-transition-ready-result']['target'],'transition_result')

 def test_03_receipt_contract(self):
  n=ns('run-control-reconcile'); text=json.dumps(n)
  self.assertTrue({'receipt_shape','validate_usage','usage_gate','reconcile_mode','receipt_replay_gate','receipt_conflict_fence','authority_loss_ready'}<=set(n))
  for v in ('authorization_consumed','provider_cost_usd','CTX.reconcile_mode.kind'): self.assertIn(v,text)
  env={'turns':2,'wall_seconds':3,'tokens':4,'provider_cost_usd':'1.25'}
  for cost,want in [('1.250000000000',True),('1.250000000001',False),('1e0',False),('-1',False),('unknown',False)]:
   body={'usage':{**env,'provider_cost_usd':cost},'envelope':env}; status,out=server.run_control_validate_receipt(json.dumps(body).encode()); self.assertEqual((status,out['valid']),(200,want))

 def test_04_attempt_handoff_authorization(self):
  n=ns('run-control-state'); order=list(n); text=json.dumps(n)
  self.assertLess(order.index('attempt_identity'),order.index('evaluator_request')); self.assertNotIn('CTX.INPUT.next_envelope.attempt_id',text); self.assertIn('CTX.canonical_envelope',json.dumps(n['reservation_target']))
  self.assertTrue({'handoff_shape','handoff_gate','append_handoff'}<=set(n)); self.assertIn('reason_code',text)
  a=json.dumps(ns('run-control-consume-authorization')); self.assertIn('launch_descriptor.authorization_id',a); self.assertIn('CTX.INPUT.authorization_id',a)
  self.assertTrue({'consume_replay_check','consume_replay_gate'}<=set(ns('run-control-consume')))

 def test_05_validation_openapi_runtime(self):
  n=ns('run-control-initialize'); self.assertLess(list(n).index('validate_initialize'),list(n).index('initial_state'))
  bad=[initialize_request(budgets={'attempts':1,'turns':1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'1e3'}),initialize_request(budgets={'attempts':1,'turns':-1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'1'}),initialize_request(initial_route={**evaluator_route(),'extra':'no'})]
  self.assertTrue(all(server.run_control_validate_initialize(json.dumps(x).encode())[0]==400 for x in bad))
  s=json.loads((R/'openapi.json').read_text())['components']['schemas']; self.assertEqual(s['RunControlCost']['x-maximum-decimal'],'100000'); self.assertEqual(s['RunControlRequest']['properties']['run_id']['pattern'],r'^rc1-[0-9a-f]{64}$')
  body={'run_id':'abc','route':{},'budgets':{},'attempts':[],'next_attempt':1,'next_envelope':{}}
  status,out=server.run_control_evaluate(json.dumps(body).encode()); self.assertEqual((status,out['code']),(400,'bad_run_id'))

 def test_06_grounded_protocol_recovery(self):
  c=DurableRunControlModel(R/'graphs').production_contract; self.assertEqual(c['transition.target_upsert'],'action.datastore.records.upsert'); self.assertTrue(c['all_dormant'] and c['no_launch_nodes'])
  m=DurableRunControlModel(R/'graphs'); m.initialize(initialize_request()); h=m.reserve(reservation_request(),allow,lambda:'6'*64); m.mark_authorization_consumed('6'*64,2,'7'*64,authorization_id=h['authorization_id']); s=m.load_verified(); env=s['outstanding_reservation']['envelope']
  self.assertTrue(m.reconcile_receipt(receipt(m,h,{'turns':env['turns']+1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'0.1'}))['reconciled']); self.assertFalse(m.reconcile_authority_loss('receipt_usage_outside_reservation')['reconciled']); self.assertEqual(m.load_verified()['attempts'][0]['charged_usage'],env)
  x=DurableRunControlModel(R/'graphs'); x.initialize(initialize_request(root_workflow_run_id='run-auth')); h=x.reserve(reservation_request(),allow,lambda:'a'*64); aid=h['authorization_id']; self.assertTrue(x.mark_authorization_consumed('a'*64,2,'b'*64,authorization_id=aid)['consumed']); self.assertFalse(x.mark_authorization_consumed('a'*64,2,'b'*64,authorization_id=aid)['consumed'])
  with self.assertRaisesRegex(RunControlConflict,'authorization'): x.mark_authorization_consumed('a'*64,2,'b'*64,authorization_id='rca1-'+'0'*129)
  self.assertEqual(x.load_verified()['status'],'fenced')

if __name__=='__main__': unittest.main()
