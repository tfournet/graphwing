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

func TestGraphwingInitializationAndRecordRaceTopology(t *testing.T){
 g:=loadGraph(t,"run-control-transition")
 replayIn:=edgesTo(g,"initialize_replay_check"); if len(replayIn)!=1||replayIn[0]["source"]!="initialize_existing_gate"||replayIn[0]["sourceHandle"]!="pass" {t.Fatalf("replay must be exclusive to an existing initialize pointer: %#v",replayIn)}
 if !edgeExists(g,"initialize_fresh_gate","pass","prior_ready"){t.Fatal("fresh initialization must bypass replay and fence topology")}
 fc:=config(graphNode(t,g,"fence_cas")); if fc["expectedVersion"]!="{{ TASKS.fence_pointer_get.version }}" {t.Fatalf("fence CAS must use executed fence read, got %v",fc["expectedVersion"])}
 if len(edgesTo(g,"fence_pointer_get"))!=1||edgesTo(g,"fence_pointer_get")[0]["source"]!="fence_ready" {t.Fatal("contradictions need one explicit join before the pointer reread")}
 if !edgeExists(g,"target_upsert","success","post_upsert_get"){t.Fatal("created=false upsert must always reread")}
 if !edgeExists(g,"target_upsert","failure","fence_ready")||!edgeExists(g,"post_upsert_get","failure","fence_ready")||!edgeExists(g,"readback_gate","fail","fence_ready"){t.Fatal("upsert/readback uncertainty must fence, never leak pending")}
}

func TestGraphwingNativePathsAndClosedShapes(t *testing.T){
 for _,name:=range []string{"run-control-state","run-control-reconcile","run-control-consume","run-control-consume-authorization","run-control-transition"}{b,_:=json.Marshal(loadGraph(t,name));if strings.Contains(string(b),".-1"){t.Fatalf("%s contains unsupported negative-index path",name)}}
 state:=loadGraph(t,"run-control-state")
 hs:=graphNode(t,state,"handoff_shape"); ctx:=map[string]any{"CTX":map[string]any{"INPUT":map[string]any{"handoff":map[string]any{"reason_code":"provider_switch"},"candidate_route":map[string]any{"provider":"anthropic","model":"claude-sonnet-5"}},"handoff_raw_hash":map[string]any{"value":"same"},"handoff_canonical_hash":map[string]any{"value":"same"}},"TASKS":map[string]any{"load_state":map[string]any{"data":map[string]any{"current_route":map[string]any{"provider":"openai","model":"gpt-5.6-sol"}}}}}
 if eval(t,mapping(t,hs,"closed"),ctx)!=true {t.Fatal("closed one-field handoff must validate natively")}
 he:=graphNode(t,state,"handoff_entry"); ctx["TASKS"].(map[string]any)["load_state"].(map[string]any)["data"].(map[string]any)["last_attempt"]=map[string]any{"attempt_id":"att1-prev"}
 if got:=eval(t,mapping(t,he,"handoff"),ctx).(map[string]any)["from_attempt_id"];got!="att1-prev"{t.Fatalf("handoff identity = %v",got)}
 rec:=loadGraph(t,"run-control-reconcile"); rs:=graphNode(t,rec,"receipt_shape"); receipt:=map[string]any{"receipt_id":"rec1-x","attempt_id":"att1-x","run_control_id":"rc1-x","authorization_id":"rca1-x","launch_descriptor_sha256":"d","job_id":"j","terminal_status":"failed","route":map[string]any{},"usage":map[string]any{},"progress":map[string]any{},"constraint_signals":[]any{},"verified_outcome":false}
 rctx:=map[string]any{"CTX":map[string]any{"INPUT":map[string]any{"receipt":receipt},"receipt_hash":map[string]any{"value":"same"},"receipt_canonical_hash":map[string]any{"value":"same"}},"TASKS":map[string]any{"load_state":map[string]any{"data":map[string]any{"outstanding_reservation":map[string]any{"phase":"authorization_consumed"}}}}}
 if eval(t,mapping(t,rs,"closed"),rctx)!=true {t.Fatal("closed receipt must validate without count(object)")}
 rr:=graphNode(t,rec,"receipt_replay_check"); rctx["CTX"].(map[string]any)["receipt_hash"]=map[string]any{"value":"hash"}; rctx["TASKS"].(map[string]any)["load_state"].(map[string]any)["data"].(map[string]any)["last_attempt"]=map[string]any{"reconciliation":map[string]any{"receipt_id":"rec1-x","receipt_sha256":"hash"}}
 if eval(t,mapping(t,rr,"exact"),rctx)!=true {t.Fatal("exact receipt replay must resolve under native pathresolve")}
}

func TestGraphwingAuthorizationLostResponseRecovery(t *testing.T){
 helper:=loadGraph(t,"run-control-consume-authorization")
 if !edgeExists(helper,"issued_gate","pass","consume_ready")||!edgeExists(helper,"consume_ready","out","consume") {t.Fatal("exact issued state must resume consumption through an explicit join")}
 if !edgeExists(helper,"issue","failure","post_consume_ready")||!edgeExists(helper,"consume","failure","post_consume_ready") {t.Fatal("lost issue/consume responses must reread durable authorization")}
 if !edgeExists(helper,"post_issued_gate","pass","consume_retry") {t.Fatal("issued readback must resume the one-time consume CAS")}
 if !edgeExists(helper,"consumed_gate","pass","existing_hash")||!edgeExists(helper,"existing_hash","out","authorization_existing") {t.Fatal("exact consumed state must hash and build output from reloaded value")}
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
 reconcile:=loadGraph(t,"run-control-reconcile"); raw,_:=json.Marshal(graphNode(t,reconcile,"reconcile_check")); for _,field:=range []string{"consumed_authorization_matches","consumed_hash_present","descriptor_matches","route_matches"}{if !strings.Contains(string(raw),field){t.Fatalf("receipt binding omits %s",field)}}
}
