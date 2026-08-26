# Ticket 07 mechanical correction canary

Create `canary_value.py` containing a single module constant `VALUE`.

Acceptance: `python3 canary_test.py` exits 0 only when `VALUE == 42`.

Canary protocol:
1. On the initial writer turn, create `canary_value.py` with `VALUE = 41` and stop. Do not edit `canary_test.py`.
2. After the real failed check is returned in the correction turn, update the same file to `VALUE = 42`.
3. Do not commit or push. Graphwing finalization owns the one build commit and push.
4. Change no other files.
