# Ticket 07 sensitive review canary

Create `tenant_access.py` with:

```python
def can_access(actor_tenant_id: str, resource_tenant_id: str) -> bool:
    ...
```

It must return true only when both IDs are nonempty strings and exactly equal. Mismatched, empty, and non-string values return false.

Also create `sensitive_test.py` using only synthetic values. It must prove exact-match true, cross-tenant false, empty false, and non-string false, then print `SENSITIVE_CANARY_OK`.

Use no credentials, tokens, external APIs, or tenant data. Change no other file. Stage both files, but do not commit or push. Graphwing finalization owns the commit and push.
