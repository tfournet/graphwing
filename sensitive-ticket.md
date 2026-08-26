# Ticket 07 sensitive review canary

Create `sensitive_canary.py` with exactly:

```python
TENANT_ISOLATED = True
NO_REAL_CREDENTIALS = True
```

Do not read or use credentials, tokens, tenant data, or external APIs. Change no other file. Stage the new file, but do not commit or push. Graphwing finalization owns the commit and push.
