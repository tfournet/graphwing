# Ticket 07 sensitive review canary

Create `tenant_access.py` with one function:

```python
def can_access(actor_tenant_id: str, resource_tenant_id: str) -> bool:
    ...
```

It must return true only when both tenant IDs are nonempty strings and exactly equal. Mismatched tenants, empty values, and non-string values must return false. Use only synthetic values and no credentials, tokens, external APIs, or tenant data. Change no other file. Stage the new file, but do not commit or push. Graphwing finalization owns the commit and push.
