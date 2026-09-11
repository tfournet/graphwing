# 01-wait-pending

Context: Disposable Graphwing canary for live `action.wait.webhook` pending dispatch.

Acceptance criteria:
- Create `canary-115.txt` containing exactly `ping`.
- Do not edit any other file.
- Do not commit or push.

Scope:
- In: `canary-115.txt`
- Out: catalog source, tests, graphs, docs

Constraints:
- Production comments pin ADR/section only.
- Mechanical S. One writer.

Verification:
- Named recipe `always-fail` (expected red after the writer starts).
