#!/usr/bin/env python3
"""Run Graphwing contracts through the real Riftwing Go evaluator."""
import json, os, subprocess, tempfile, unittest
from pathlib import Path
from jsonschema import Draft202012Validator
import server

def evaluator_route():
 return {'route_version':'normal-v1','launcher':'codex','provider':'openai','model':'gpt-5.6-sol'}

ROOT=Path(__file__).resolve().parent
RIFTWING=Path(os.environ.get('RIFTWING_GO','/home/tim/work/riftwing/sc-109005/rewst-go'))

class NativeRunControlContracts(unittest.TestCase):
 def test_native_riftwing_ast_path_and_action_contracts(self):
  self.assertTrue((RIFTWING/'go.mod').is_file(),f'missing Riftwing checkout: {RIFTWING}')
  virtual=RIFTWING/'internal/asteval/graphwing_run_control_contract_test.go'
  with tempfile.TemporaryDirectory() as td:
   overlay=Path(td)/'overlay.json'; overlay.write_text(json.dumps({'Replace':{str(virtual):str(ROOT/'native_run_control_contract_test.go')}}))
   env={**os.environ,'GRAPHWING_ROOT':str(ROOT)}
   subprocess.run(['go','test','-overlay',str(overlay),'./internal/asteval','-run','TestGraphwing','-count=1'],cwd=RIFTWING,env=env,check=True)
  subprocess.run(['go','test','./services/worker/nodes','-run','TestHandleDatastoreKvCompareAndSwap_MissingExpectedVersion_CoercesToZero','-count=1'],cwd=RIFTWING,check=True)
 def test_openapi_runtime_differential_parity(self):
  document=json.loads((ROOT/'openapi.json').read_text()); schemas=document['components']['schemas']
  request_schema={'$schema':'https://json-schema.org/draft/2020-12/schema','components':{'schemas':schemas},'$ref':'#/components/schemas/RunControlRequest'}
  validator=Draft202012Validator(request_schema)
  cost=schemas['RunControlCost']; self.assertEqual((cost.get('pattern'),cost.get('x-maximum-decimal')),(r'^(?:100000(?:\.0{1,12})?|(?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,12})?)$','100000'))
  identifier=schemas['RunControlAttemptId']['pattern']; self.assertEqual(identifier,r'^att1-[0-9a-f]{64}$')
  base={'run_id':'rc1-'+'b'*64,'route':evaluator_route(),'budgets':{'attempts':1,'turns':1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'1'},'attempts':[],'next_attempt':1,'next_envelope':{'attempt_id':'att1-'+'a'*64,'turns':1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'1'}}
  for mutate,code in [
   (lambda b:b['budgets'].__setitem__('provider_cost_usd','100001'),'bad_budgets'),
   (lambda b:b['budgets'].__setitem__('provider_cost_usd','100000.000000000001'),'bad_budgets'),
   (lambda b:b['next_envelope'].__setitem__('attempt_id','a..b'),'bad_next_envelope'),
   (lambda b:b.__setitem__('route',{'route_version':'normal-v1','launcher':'codex','provider':'anthropic','model':'claude-sonnet-5'}),'bad_route')]:
   body=json.loads(json.dumps(base)); mutate(body)
   self.assertFalse(validator.is_valid(body),body)
   status,out=server.run_control_evaluate(json.dumps(body).encode()); self.assertEqual((status,out['code']),(400,code))
  for route in [
   evaluator_route(),
   {'route_version':'normal-v1','launcher':'claude','provider':'anthropic','model':'claude-opus-5'},
   {'route_version':'availability-fallback-v1','launcher':'claude','provider':'anthropic','model':'claude-sonnet-5'},
   {'route_version':'normal-v1','launcher':'grok','provider':'xai','model':'grok-4.6'}]:
   body=json.loads(json.dumps(base)); body['route']=route
   self.assertTrue(validator.is_valid(body),list(validator.iter_errors(body)))
   self.assertEqual(server.run_control_evaluate(json.dumps(body).encode())[0],200)
 def test_recorded_260_turn_537_incident_stops_before_reservation(self):
  attempts=[]
  for sequence,cost in ((1,'2.680000000000'),(2,'2.690000000000')):
   attempt_id='att1-'+str(sequence)*64
   attempts.append({'sequence':sequence,'attempt_id':attempt_id,**evaluator_route(),
    'usage':{'turns':130,'wall_seconds':1800,'tokens':150000,'provider_cost_usd':cost},
    'reservation':{'attempt_id':attempt_id,'turns':130,'wall_seconds':1800,'tokens':150000,'provider_cost_usd':cost},
    'terminal_state':'terminal','progress':{'checkpoint':1,'failing_regression_present':True,'production_diff_bytes':1,'focused_tests_green':False,'diff_fingerprint':'a'*64},'constraint_signals':[],'verified_outcome':False})
  body={'run_id':'rc1-'+'b'*64,'route':evaluator_route(),'budgets':{'attempts':4,'turns':260,'wall_seconds':7200,'tokens':500000,'provider_cost_usd':'5.370000000000'},'attempts':attempts,'next_attempt':3,'next_envelope':{'attempt_id':'att1-'+'3'*64,'turns':1,'wall_seconds':1,'tokens':1,'provider_cost_usd':'0.000000000001'}}
  status,out=server.run_control_evaluate(json.dumps(body).encode())
  self.assertEqual((status,out['decision']),(200,'terminate')); self.assertIn('turns_exhausted',out['evidence_codes']); self.assertIn('provider_cost_exhausted',out['evidence_codes'])

if __name__=='__main__':unittest.main()
