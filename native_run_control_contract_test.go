package asteval

import (
 "encoding/json"
 "os"
 "path/filepath"
 "strings"
 "testing"
)

type graphDoc struct { Spec struct { Meta map[string]any `json:"meta"`; Nodes []map[string]any `json:"nodes"`; Edges []map[string]any `json:"edges"` } `json:"spec"` }

func loadGraph(t *testing.T, name string) graphDoc {
 t.Helper(); root:=os.Getenv("GRAPHWING_ROOT"); if root=="" { t.Fatal("GRAPHWING_ROOT is required") }
 b,err:=os.ReadFile(filepath.Join(root,"graphs",name+".json")); if err!=nil { t.Fatal(err) }
 var g graphDoc; if err=json.Unmarshal(b,&g); err!=nil { t.Fatal(err) }; return g
}
func graphNode(t *testing.T,g graphDoc,id string) map[string]any { t.Helper(); for _,n:=range g.Spec.Nodes { if n["id"]==id{return n} }; t.Fatalf("missing node %s",id); return nil }
func edgesTo(g graphDoc,target string) []map[string]any { var out []map[string]any; for _,e:=range g.Spec.Edges {if e["target"]==target{out=append(out,e)}}; return out }
func edgeExists(g graphDoc,source,handle,target string) bool { for _,e:=range g.Spec.Edges {if e["source"]==source&&e["sourceHandle"]==handle&&e["target"]==target{return true}}; return false }
func config(n map[string]any) map[string]any{return n["config"].(map[string]any)}
func mapping(t *testing.T,n map[string]any,out string) any {t.Helper(); for _,raw:=range config(n)["mappings"].([]any){m:=raw.(map[string]any);if m["output"]==out{return m["expression"]}};t.Fatalf("missing mapping %s",out);return nil}
func eval(t *testing.T,expr any,ctx map[string]any) any {t.Helper();v,err:=Evaluate(expr,ctx);if err!=nil{t.Fatalf("native AST error: %v",err)};return v}
func rejects(expr any,ctx map[string]any) bool {v,err:=Evaluate(expr,ctx);return err!=nil||v!=true}

func TestGraphwingInitializationAndRecordRaceTopology(t *testing.T){
 g:=loadGraph(t,"run-control-transition")
 replayIn:=edgesTo(g,"initialize_replay_check"); if len(replayIn)!=1||replayIn[0]["source"]!="initialize_existing_gate"||replayIn[0]["sourceHandle"]!="pass" {t.Fatalf("replay must be exclusive to an existing initialize pointer: %#v",replayIn)}
 if !edgeExists(g,"initialize_fresh_gate","pass","prior_ready"){t.Fatal("fresh init topology")}
 fc:=config(graphNode(t,g,"fence_cas")); if fc["expectedVersion"]!="{{ TASKS.fence_pointer_get.version }}" {t.Fatalf("bad fence CAS %v",fc["expectedVersion"])}
 if len(edgesTo(g,"fence_pointer_get"))!=1||edgesTo(g,"fence_pointer_get")[0]["source"]!="fence_ready" {t.Fatal("bad fence join")}
 if !edgeExists(g,"target_upsert","success","post_upsert_get"){t.Fatal("missing upsert reread")}
 if !edgeExists(g,"target_upsert","failure","fence_ready")||!edgeExists(g,"post_upsert_get","failure","fence_ready")||!edgeExists(g,"readback_gate","fail","fence_ready"){t.Fatal("missing uncertainty fence")}
}

func TestGraphwingNativePathsAndClosedShapes(t *testing.T){
 for _,n:=range []string{"run-control-state","run-control-reconcile","run-control-consume","run-control-consume-authorization","run-control-transition"}{b,_:=json.Marshal(loadGraph(t,n));if strings.Contains(string(b),".-1"){t.Fatal(n+" has negative path")}}
 state:=loadGraph(t,"run-control-state"); hs:=graphNode(t,state,"handoff_shape"); c:=map[string]any{"CTX":map[string]any{"INPUT":map[string]any{"handoff":map[string]any{"reason_code":"provider_switch"},"candidate_route":map[string]any{"provider":"anthropic","model":"claude-sonnet-5"}},"handoff_raw_hash":map[string]any{"value":"x"},"handoff_canonical_hash":map[string]any{"value":"x"}},"TASKS":map[string]any{"load_state":map[string]any{"data":map[string]any{"current_route":map[string]any{"provider":"openai","model":"gpt-5.6-sol"}}}}}
 for _,x:=range []string{"cross_model","closed","reason_allowed"}{if eval(t,mapping(t,hs,x),c)!=true{t.Fatal(x)}}; if config(graphNode(t,state,"handoff_gate"))["group"]!="AND"{t.Fatal("handoff gate is not AND")}
 in:=c["CTX"].(map[string]any)["INPUT"].(map[string]any); h:=in["handoff"].(map[string]any);h["reason_code"]="bad";h["extra"]=1;c["CTX"].(map[string]any)["handoff_raw_hash"]=map[string]any{"value":"raw"};for _,x:=range []string{"closed","reason_allowed"}{if eval(t,mapping(t,hs,x),c)!=false{t.Fatal("bad handoff "+x)}};delete(h,"extra");h["reason_code"]="provider_switch";in["candidate_route"]=map[string]any{"provider":"openai","model":"gpt-5.6-sol"};if eval(t,mapping(t,hs,"cross_model"),c)!=false{t.Fatal("same-model handoff")}
 c["TASKS"].(map[string]any)["load_state"].(map[string]any)["data"].(map[string]any)["last_attempt"]=map[string]any{"attempt_id":"prev"};if eval(t,mapping(t,graphNode(t,state,"handoff_entry"),"handoff"),c).(map[string]any)["from_attempt_id"]!="prev"{t.Fatal("handoff identity")}
 rec:=loadGraph(t,"run-control-reconcile");rs:=graphNode(t,rec,"receipt_shape");route:=map[string]any{"route_version":"normal-v1","launcher":"codex","provider":"openai","model":"gpt-5.6-sol"};prog:=map[string]any{"checkpoint":float64(2),"failing_regression_present":true,"production_diff_bytes":float64(17),"focused_tests_green":false,"diff_fingerprint":strings.Repeat("a",64)};receipt:=map[string]any{"receipt_id":"rec1-x","attempt_id":"att1-x","run_control_id":"rc1-x","authorization_id":"rca1-x","launch_descriptor_sha256":strings.Repeat("d",64),"job_id":"job1-x","terminal_status":"failed","route":route,"usage":map[string]any{"turns":float64(1),"wall_seconds":float64(2),"tokens":float64(3),"provider_cost_usd":"0.1"},"progress":prog,"constraint_signals":[]any{},"verified_outcome":false};auth:=map[string]any{"authorization_id":"rca1-x","challenge":"challenge","consumed_kv_version":float64(2),"consumed_value_sha256":strings.Repeat("e",64)};res:=map[string]any{"phase":"authorization_consumed","attempt_id":"att1-x","route":route,"launch_descriptor_sha256":strings.Repeat("d",64),"launch_descriptor":map[string]any{"authorization_id":"rca1-x","server_instance_challenge":"challenge"},"authorization":auth};r:=map[string]any{"CTX":map[string]any{"INPUT":map[string]any{"receipt":receipt},"receipt_hash":map[string]any{"value":"x"},"receipt_canonical_hash":map[string]any{"value":"x"}},"TASKS":map[string]any{"load_state":map[string]any{"data":map[string]any{"run_control_id":"rc1-x","outstanding_reservation":res}}}}
 for _,x:=range []string{"closed","consumed","terminal_status_typed","constraints_typed"}{if eval(t,mapping(t,rs,x),r)!=true{t.Fatal(x)}};canon:=graphNode(t,rec,"receipt_canonical");if _,e:=Evaluate(mapping(t,canon,"value"),r);e!=nil{t.Fatal(e)};for _,q:=range []struct{f string;v any}{{"receipt_id",map[string]any{}},{"job_id",[]any{}},{"progress","bad"}}{old:=receipt[q.f];receipt[q.f]=q.v;if !rejects(mapping(t,canon,"value"),r){t.Fatal("bad "+q.f)};receipt[q.f]=old};for _,q:=range []struct{f,m string;v any}{{"terminal_status","terminal_status_typed",map[string]any{}},{"constraint_signals","constraints_typed","bad"}}{old:=receipt[q.f];receipt[q.f]=q.v;if !rejects(mapping(t,rs,q.m),r){t.Fatal("bad "+q.f)};receipt[q.f]=old};r["CTX"].(map[string]any)["receipt_hash"]=map[string]any{"value":"raw"};if eval(t,mapping(t,rs,"closed"),r)!=false{t.Fatal("open receipt")}
 rc:=graphNode(t,rec,"reconcile_check");for _,x:=range []string{"consumed_authorization_matches","consumed_challenge_matches","consumed_version_matches","consumed_hash_typed"}{if eval(t,mapping(t,rc,x),r)!=true{t.Fatal(x)}};auth["challenge"]="drift";if eval(t,mapping(t,rc,"consumed_challenge_matches"),r)!=false{t.Fatal("authorization drift")}
 r["CTX"].(map[string]any)["receipt_hash"]=map[string]any{"value":"hash"};r["TASKS"].(map[string]any)["load_state"].(map[string]any)["data"].(map[string]any)["last_attempt"]=map[string]any{"reconciliation":map[string]any{"receipt_id":"rec1-x","receipt_sha256":"hash"}};if eval(t,mapping(t,graphNode(t,rec,"receipt_replay_check"),"exact"),r)!=true{t.Fatal("receipt replay")}
}

func TestGraphwingAuthorizationLostResponseRecovery(t *testing.T){
 helper:=loadGraph(t,"run-control-consume-authorization")
 if !edgeExists(helper,"issued_gate","pass","consume_ready")||!edgeExists(helper,"consume_ready","out","consume") {t.Fatal("bad issued resume")}
 if !edgeExists(helper,"issue","failure","post_consume_ready")||!edgeExists(helper,"consume","failure","post_consume_ready") {t.Fatal("missing auth reread")}
 if !edgeExists(helper,"post_issued_gate","pass","consume_retry") {t.Fatal("missing consume retry")}
 if !edgeExists(helper,"consumed_gate","pass","existing_hash")||!edgeExists(helper,"existing_hash","out","authorization_existing") {t.Fatal("bad consumed replay")}
 a:=graphNode(t,helper,"authorization_existing"); raw,_:=json.Marshal(a); if strings.Contains(string(raw),"TASKS.consume") {t.Fatal("consumed replay output cannot read absent consume task")}
 outer:=loadGraph(t,"run-control-consume"); if !edgeExists(outer,"consume_replay_gate","pass","consume_replay_result") {t.Fatal("state replay must build a complete output from reloaded state")}
 gate:=graphNode(t,outer,"authorization_gate"); raw,_=json.Marshal(gate); if strings.Contains(string(raw),"TASKS.authorize") {t.Fatal("authorization gate must consume a joined executed/reloaded projection")}
}

func TestGraphwingDormantSafetyAndExactPins(t *testing.T){
 names:=[]string{"run-control-initialize","run-control-state","run-control-reconcile","run-control-consume","run-control-consume-authorization","run-control-transition"}
 for _,name:=range names{
  g:=loadGraph(t,name); raw,_:=json.Marshal(g)
  if g.Spec.Meta["dormant"]!=true {t.Fatalf("%s is not dormant",name)}
  if strings.Contains(string(raw),"transforms.codeExpression")||strings.Contains(string(raw),"namespace("){t.Fatalf("%s uses procedural expression",name)}
  for _,n:=range g.Spec.Nodes{
   typ,_:=n["type"].(string); if strings.Contains(strings.ToLower(typ),"agentrun")||typ=="action.graphwing"{t.Fatalf("%s contains launch node %s",name,typ)}
   if typ=="action.subworkflow"{c:=config(n); pin,_:=c["workflowVersionId"].(string);if pin==""||strings.Contains(strings.ToLower(pin),"latest"){t.Fatalf("%s subworkflow is not exactly pinned",name)}}
  }
 }
 transition:=loadGraph(t,"run-control-transition")
 if !edgeExists(transition,"pending_pointer","out","prepare_cas")||!edgeExists(transition,"stable_pointer","out","publish_cas"){t.Fatal("transition must remain stable-to-pending-to-stable")}
 if !edgeExists(transition,"prepare_loser_exact_gate","pass","pending_ready")||!edgeExists(transition,"prepare_loser_exact_gate","fail","fence_ready"){t.Fatal("concurrent exact loser must recover while drift fences")}
 state:=loadGraph(t,"run-control-state"); appendNode:=graphNode(t,state,"append_handoff"); if config(appendNode)["operation"]!="concat"{t.Fatal("handoff history must append exactly once")}
 reconcile:=loadGraph(t,"run-control-reconcile"); raw,_:=json.Marshal(graphNode(t,reconcile,"reconcile_check")); for _,field:=range []string{"consumed_authorization_matches","consumed_hash_typed","descriptor_matches","route_matches"}{if !strings.Contains(string(raw),field){t.Fatalf("receipt binding omits %s",field)}}
}
