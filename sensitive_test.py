from tenant_access import can_access

assert can_access("tenant-a", "tenant-a") is True
assert can_access("tenant-a", "tenant-b") is False
assert can_access("", "tenant-a") is False
assert can_access("tenant-a", "") is False
assert can_access(None, "tenant-a") is False
assert can_access("tenant-a", None) is False
print("SENSITIVE_CANARY_OK")
