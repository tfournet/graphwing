#!/usr/bin/env python3
"""graphwing OpenAPI server — deterministic laptop nodes + one-job agent loops."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import io
import json
import math
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HOME = Path(os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing"))
OPENAPI_PATH = HOME / "openapi.json"
REPOS_PATH = HOME / "repos.json"
SCRIPTS_PATH = HOME / "scripts.json"
STACKS_PATH = HOME / "stacks.json"
TESTS_PATH = HOME / "tests.json"
RR_PATH = HOME / "rr.json"
KEY_PATH = HOME / "api.key"
JOBS_DIR = HOME / "jobs"
CODEOFFS_DIR = HOME / "codeoffs"
CODEOFF_WORKSPACES_DIR = HOME / "codeoff-workspaces"
RUNS_PATH = HOME / "workflow-runs.jsonl"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GRAPHWING_PORT", "8645"))
CMD_TIMEOUT = 30
CMD_MAX_BYTES = 256 * 1024
FILE_MAX_BYTES = 32 * 1024
PROMPT_MAX_CHARS = 64 * 1024
COMPACT_MAX_CHARS = 2000
COMPACT_TAIL_LINES = 40
COMPACT_FAIL_RE = re.compile(r"FAIL:|ERROR:|Error:|AssertionError|FAILED")
UNITS = ("graphwing-api", "graphwing-tunnel", "graphwing-herdr")
HERDR_SESSION = os.environ.get("GRAPHWING_HERDR_SESSION", "graphwing") or "graphwing"
HERDR_AGENT = os.environ.get("GRAPHWING_HERDR_AGENT", "graphwing") or "graphwing"
HERDR_GRAPH_LABEL = "graph"
HERDR_SOURCE = "graphwing"
HERDR_JOB_TAB_MAX = 8
# Leave a finished job's tab open long enough to actually read it. Closing on
# completion meant the interesting output vanished the moment it appeared.
# The tab-count cap still wins: a burst of jobs evicts lingering tabs early.
HERDR_TAB_LINGER_SECONDS = int(os.environ.get("GRAPHWING_HERDR_TAB_LINGER", "180"))
HERDR_LOCK = threading.Lock()


def resolve_executable(name: str, env_var: str, fallback: Path) -> Path:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        return Path(raw)
    mise = shutil.which("mise")
    if mise:
        try:
            selected = subprocess.run(
                [mise, "which", name], check=False, capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.TimeoutExpired):
            selected = None
        candidate = Path(selected.stdout.strip()) if selected and selected.returncode == 0 else None
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    return fallback


CODEX_BIN = resolve_executable("codex", "GRAPHWING_CODEX_BIN", Path.home() / ".local" / "bin" / "codex")
CLAUDE_BIN = resolve_executable("claude", "GRAPHWING_CLAUDE_BIN", Path.home() / ".local" / "bin" / "claude")
GROK_BIN = resolve_executable("grok", "GRAPHWING_GROK_BIN", Path.home() / ".local" / "bin" / "grok")
GH_BIN = resolve_executable("gh", "GRAPHWING_GH_BIN", Path.home() / ".local" / "bin" / "gh")
RR_BIN = resolve_executable("rr", "GRAPHWING_RR_BIN", Path.home() / "go" / "bin" / "rr")
AGENT_MAX_TURNS = 30
AGENT_RUN_BUDGET = 300
AGENT_MAX_CONCURRENT = 3
SCRIPT_SYNC_TIMEOUT = 25
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
NATIVE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LAUNCHER_VERSION_RE = re.compile(r"^(?:sha256:[0-9a-f]{64}|missing)$")
NATIVE_LAUNCHERS = {
    "codex": {"provider": "openai", "models": ("gpt-5.6-sol",)},
    "claude": {"provider": "anthropic", "models": ("claude-opus-5", "claude-sonnet-5")},
    "grok": {"provider": "xai", "models": ("grok-4.6",)},
}
EFFORT_VALUES = ("default", "low", "medium", "high", "max")
EFFORT_SOURCES = frozenset({"route", "explicit", "launcher_default"})
REVIEW_EFFORT_SOURCES = frozenset({"explicit", "launcher_default"})
REVIEW_EXECUTION_VERSION = "review-execution-v1"
# One review authority binds the complete diff, hashed byte-exact. A prefix hash
# would give two candidates sharing that prefix the same diff_sha256 and the same
# review authority, so an oversized diff is rejected before launch instead of
# truncated.
#
# The maximum has to cover the candidates this bound exists to review, and the
# previous 64-KiB maximum rejected the cumulative Phase 6 candidate outright. It
# is deliberately not derived from that candidate's measured size: the candidate
# is this patch, so every follow-up commit changes it and any figure recorded
# here would invalidate itself. The bound comes from the one host limit left
# instead. CMD_MAX_BYTES is the 256-KiB cap `run_cmd` applies to captured
# `git diff` output, and a diff at or over that cap comes back flagged
# truncated, which is a prefix and never reviewable. 252 KiB is the largest
# round maximum that stays clear of the capture cap, leaving 4,096 bytes of
# headroom, so an accepted diff is always the complete bytes git produced.
#
# Linux MAX_ARG_STRLEN no longer bounds this. It rejects a single argv string of
# 131,072 bytes or more, so a maximum-sized prompt cannot ride in argv; every
# review launcher transports its prompt off argv instead. Codex and Claude write
# the prompt to the child's stdin, and Grok sends it as an ACP `session/prompt`
# text block read from the job's prompt file.
REVIEW_MAX_DIFF_BYTES = 252 * 1024
REVIEW_PERMISSION_PROFILES = {
    "codex": "codex-read-only",
    "claude": "claude-plan",
    "grok": "grok-read-only-acp",
}
REVIEW_REQUEST_FIELDS = frozenset({
    "repo", "launcher", "provider", "model", "prompt", "effort", "async",
    "run_budget_seconds", "max_turns", "response_webhook_url",
    "response_webhook_token", "route_execution_profile",
})
REVIEW_IDENTITY_FIELDS = (
    "version", "kind", "launcher", "provider", "model", "requested_effort",
    "effective_effort", "effort_source", "launcher_version", "repo", "branch",
    "starting_head", "prompt_sha256", "diff_sha256", "run_budget_seconds",
    "max_turns", "permission_profile",
)
REVIEW_JOB_IDENTITY_FIELDS = (
    "launcher", "provider", "model", "requested_effort", "effective_effort",
    "effort_source", "launcher_version", "repo", "branch", "starting_head",
    "prompt_sha256", "diff_sha256", "run_budget_seconds", "max_turns",
    "permission_profile",
)
REVIEW_JOB_FIELDS = frozenset({
    "job_id", "kind", "status", *REVIEW_JOB_IDENTITY_FIELDS, "execution_identity",
    "cwd", "prompt", "response_webhook_url", "response_webhook_token", "resume_url",
    "created_at", "started_at", "finished_at", "receipt", "error", "webhook",
    "review_snapshot_sha256",
})
REVIEW_PUBLIC_JOB_FIELDS = frozenset({
    "ok", "job_id", "kind", "status", "execution_identity", "created_at",
    "started_at", "finished_at", "receipt", "poll",
})
# Native support proven against the installed launcher contracts. Graphwing's
# closed vocabulary deliberately excludes launcher-only aliases such as xhigh.
EFFORT_PROFILES = {
    ("codex", "openai", "gpt-5.6-sol"): {
        "default": "high", "low": "low", "medium": "medium", "high": "high", "max": "max",
    },
    ("codex", "openai", "gpt-5.6-terra"): {
        "default": "high", "low": "low", "medium": "medium", "high": "high", "max": "max",
    },
    ("claude", "anthropic", "claude-opus-5"): {
        "default": "default", "low": "low", "medium": "medium", "high": "high", "max": "max",
    },
    ("claude", "anthropic", "claude-sonnet-5"): {
        "default": "default", "low": "low", "medium": "medium", "high": "high", "max": "max",
    },
    ("claude", "anthropic", "claude-fable-5"): {
        "default": "default", "low": "low", "medium": "medium", "high": "high", "max": "max",
    },
    ("grok", "xai", "grok-4.6"): {
        "default": "high", "low": "low", "medium": "medium", "high": "high",
    },
}
NATIVE_EFFORT_VALUES = {
    "codex": {"low": "low", "medium": "medium", "high": "high", "max": "max"},
    "claude": {"default": None, "low": "low", "medium": "medium", "high": "high", "max": "max"},
    "grok": {"low": "low", "medium": "medium", "high": "high"},
}


def normalize_effort(
    launcher: str,
    provider: str,
    model: str,
    requested: Any,
    source: str,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Validate one launcher-neutral effort and return its explicit profile."""
    if source not in EFFORT_SOURCES:
        raise ValueError("invalid internal effort source")
    if not isinstance(requested, str) or requested not in EFFORT_VALUES:
        return None, {"error": "effort must be default, low, medium, high, or max", "code": "bad_effort"}
    supported = EFFORT_PROFILES.get((launcher, provider, model), {})
    effective = supported.get(requested)
    if effective is None:
        return None, {"error": "effort is unsupported for the selected launcher/provider/model", "code": "unsupported_effort"}
    return {
        "requested_effort": requested,
        "effective_effort": effective,
        "effort_source": source,
    }, None


def normalize_review_effort(
    launcher: str,
    provider: str,
    model: str,
    requested: Any,
    source: str,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Validate direct-review effort without routed provenance."""
    if source not in REVIEW_EFFORT_SOURCES:
        return None, {
            "error": "review effort source must be explicit or launcher_default",
            "code": "bad_effort_source",
        }
    return normalize_effort(launcher, provider, model, requested, source)


def normalize_native_review_effort(
    launcher: str,
    provider: str,
    model: str,
    requested: Any,
    source: str,
    execution_identity: dict[str, Any] | None,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Accept route provenance only from one canonical sealed review identity."""
    if source != "route":
        return normalize_review_effort(launcher, provider, model, requested, source)
    if not isinstance(execution_identity, dict):
        return None, {
            "error": (
                "review effort source must be explicit or launcher_default "
                "without canonical route evidence"
            ),
            "code": "bad_effort_source",
        }
    route_raw = execution_identity.get("route_execution_profile")
    role = route_raw.get("role") if isinstance(route_raw, dict) else None
    parsed_route, route_error = parse_route_execution_profile(
        route_raw, expected_role=str(role or ""), launcher=launcher, provider=provider,
        model=model, effort=requested,
    )
    profile, effort_error = normalize_effort(
        launcher, provider, model, requested, "route"
    )
    expected_fields = {*REVIEW_IDENTITY_FIELDS, "route_execution_profile"}
    if (
        role not in {"reviewer1", "reviewer2"}
        or route_error is not None or parsed_route != route_raw
        or effort_error is not None or profile is None
        or set(execution_identity) != expected_fields
        or execution_identity.get("version") != REVIEW_EXECUTION_VERSION
        or execution_identity.get("kind") != "review"
        or execution_identity.get("launcher") != launcher
        or execution_identity.get("provider") != provider
        or execution_identity.get("model") != model
        or execution_identity.get("requested_effort") != requested
        or any(execution_identity.get(key) != value for key, value in profile.items())
        or not isinstance(execution_identity.get("launcher_version"), str)
        or not LAUNCHER_VERSION_RE.fullmatch(execution_identity["launcher_version"])
        or not isinstance(execution_identity.get("repo"), str)
        or not SESSION_REPO_RE.fullmatch(execution_identity["repo"])
        or not isinstance(execution_identity.get("branch"), str)
        or not valid_branch(execution_identity["branch"])
        or not isinstance(execution_identity.get("starting_head"), str)
        or not GIT_SHA_RE.fullmatch(execution_identity["starting_head"])
        or not isinstance(execution_identity.get("prompt_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", execution_identity["prompt_sha256"])
        or not isinstance(execution_identity.get("diff_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", execution_identity["diff_sha256"])
        or type(execution_identity.get("run_budget_seconds")) is not int
        or not (
            REVIEW_MIN_BUDGET_SECONDS
            <= execution_identity["run_budget_seconds"]
            <= REVIEW_MAX_BUDGET_SECONDS
        )
        or type(execution_identity.get("max_turns")) is not int
        or not (
            REVIEW_MIN_REQUEST_TURNS
            <= execution_identity["max_turns"]
            <= REVIEW_MAX_REQUEST_TURNS
        )
        or execution_identity.get("permission_profile") != REVIEW_PERMISSION_PROFILES[launcher]
    ):
        return None, route_error or effort_error or {
            "error": "routed review effort does not match its canonical execution identity",
            "code": "route_execution_profile_mismatch",
        }
    return profile, None


def native_effort_value(job: dict[str, Any]) -> tuple[bool, str | None]:
    """Revalidate persisted profile evidence at the command boundary."""
    profile, error = normalize_effort(
        str(job.get("launcher") or ""),
        str(job.get("provider") or ""),
        str(job.get("model") or ""),
        job.get("requested_effort"),
        str(job.get("effort_source") or ""),
    )
    if error is not None or profile is None:
        return False, None
    if any(job.get(key) != value for key, value in profile.items()):
        return False, None
    native = NATIVE_EFFORT_VALUES.get(str(job.get("launcher") or ""), {})
    effective = str(profile["effective_effort"])
    if effective not in native:
        return False, None
    return True, native[effective]


def authoritative_route_effort_profiles(
    launcher: str, provider: str, model: str, routed_effort: Any
) -> tuple[dict[str, str], ...]:
    """Closed profiles attributable to Phase 1 route execution.

    Route-wired evidence uses ``route``. Until Phase 2 wires graph effort into
    agentRun, omission is also authoritative as the launcher's default profile.
    Caller-controlled ``explicit`` provenance is never inferred from a route.
    """
    profiles: list[dict[str, str]] = []
    for requested, source in ((routed_effort, "route"), ("default", "launcher_default")):
        profile, error = normalize_effort(launcher, provider, model, requested, source)
        if error is None and profile is not None and profile not in profiles:
            profiles.append(profile)
    return tuple(profiles)

GROK_VENDOR_NOTIFICATION_METHODS = frozenset({
    "_x.ai/announcements/update",
    "_x.ai/mcp/init_progress",
    "_x.ai/mcp/server_status",
    "_x.ai/mcp/servers_updated",
    "_x.ai/mcp_initialized",
    "_x.ai/models/update",
    "_x.ai/queue/changed",
    "_x.ai/session/prompt_complete",
    "_x.ai/session_notification",
    "_x.ai/sessions/changed",
    "_x.ai/settings/update",
})
CODEOFF_PROTOCOL_VERSION = "code-off-v1"
CODEOFF_CATEGORY_VERSION = "code-category-v1"
CODEOFF_DRAW_VERSION = "code-off-draw-v1"
CODEOFF_POLICY_VERSION = "code-off-policy-v1"
CODEOFF_AGGREGATION_VERSION = "code-off-aggregation-v1"
CODEOFF_RUBRIC_VERSION = "code-off-rubric-v1"
CODEOFF_EXECUTION_MANIFEST_VERSION = "code-off-execution-manifest-v1"
CODEOFF_INFERENCE_PROFILE_VERSION = "code-off-inference-profile-v1"
CODEOFF_ECONOMICS_VERSION = "code-off-economics-v1"
# Every code-off slot pins one closed requested effort per work role. Changing a
# value here is a versioned policy change: it moves each slot identity snapshot,
# the identities hash, and the immutable manifest file hash, so no in-flight
# experiment can silently inherit a different inference profile.
CODEOFF_SLOT_REQUESTED_EFFORT = {"author": "default", "judge": "default"}
# Code-off states its effort; it never inherits a launcher default. The pinned
# effective effort is derived under this one source, so every launch, job,
# session identity, and terminal receipt must report it too.
CODEOFF_EFFORT_SOURCE = "explicit"
CODEOFF_IDENTITY_FIELDS = (
    "launcher", "launcher_version", "provider", "exact_model",
    "requested_effort", "effective_effort", "inference_profile_version",
)
CODEOFF_AUTHOR_POOL = ["grok", "sol", "terra", "sonnet", "opus"]
CODEOFF_CATEGORIES = frozenset({"ui", "backend", "full_stack", "data", "infrastructure", "developer_tooling", "mobile", "documentation"})
CODEOFF_TAGS = frozenset({"api", "database", "auth", "security", "accessibility", "testing", "migration", "performance", "observability", "build_ci", "bug_fix", "refactor"})
CODEOFF_MODEL_MANIFEST: dict[str, Any] = {
    "version": "approved-model-manifest-v2",
    "as_of": "2026-08-26",
    "max_age_days": 30,
    "policy_creation_evidence": {
        "inventory": "Bounded native-launcher catalog inventory obtained 2026-08-26 recorded each exact provider/model identity below.",
        "canary": "Bounded native-launcher canary evidence obtained 2026-08-26 proved each exact identity below runnable at policy creation.",
    },
    "policy_semantics": {
        "latest": "Approved latest at policy creation, never a moving alias.",
        "expiration": "Expiry forces review and parks new runs pending a policy update.",
        "discovery": "No dynamic provider discovery or OpenCode dependency.",
    },
    "approval_scope": "Exact immutable code-off identities only; ordinary launcher eligibility is unchanged.",
    "limitation": "Latest means approved latest at policy creation. Expiry forces review and parks new runs; there is no dynamic provider discovery or OpenCode dependency, fallback, substitution, alias resolution, or redraw. Same-user worktrees are not an OS security boundary.",
    "models": {
        "grok": {"exact_model": "grok-4.6", "provider": "xai", "launcher": "grok", "runnable": True, "proven": True, "reason": "Exact native identity passed the bounded 2026-08-26 inventory and canary."},
        "sol": {"exact_model": "gpt-5.6-sol", "provider": "openai", "launcher": "codex", "runnable": True, "proven": True, "reason": "Exact native identity passed the bounded 2026-08-26 inventory and canary."},
        "terra": {"exact_model": "gpt-5.6-terra", "provider": "openai", "launcher": "codex", "runnable": True, "proven": True, "reason": "Exact native identity passed the bounded 2026-08-26 inventory and canary."},
        "sonnet": {"exact_model": "claude-sonnet-5", "provider": "anthropic", "launcher": "claude", "runnable": True, "proven": True, "reason": "Exact native identity passed the bounded 2026-08-26 inventory and canary."},
        "opus": {"exact_model": "claude-opus-5", "provider": "anthropic", "launcher": "claude", "runnable": True, "proven": True, "reason": "Exact native identity passed the bounded 2026-08-26 inventory and canary."},
        "fable": {"exact_model": "claude-fable-5", "provider": "anthropic", "launcher": "claude", "runnable": True, "proven": True, "reason": "Exact native identity passed the bounded 2026-08-26 inventory and canary."},
    },
}
CODEOFF_CATALOG_INVENTORY: dict[str, Any] = {
    "version": "bounded-model-catalog-v1",
    "models": {
        "grok": "grok-4.6", "sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra",
        "sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "fable": "claude-fable-5",
    },
}
CODEOFF_RUBRIC = {
    "version": CODEOFF_RUBRIC_VERSION, "dimensions": {"correctness": 40, "tests": 20, "quality": 20, "scope": 10, "maintainability": 10},
    "preference": ["candidate-1", "candidate-2", "abstain"], "aggregation": "three equal preferences; majority, then summed scores, exact tie parks",
}
CODEOFF_EXPERIMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
CODEOFF_SEED_RE = re.compile(r"^[0-9a-f]{64}$")
CODEOFF_MAX_FILES = 10000
CODEOFF_MAX_FILE_BYTES = 32 * 1024 * 1024
CODEOFF_MAX_TREE_BYTES = 100 * 1024 * 1024
CODEOFF_MAX_LOG_BYTES = 256 * 1024
CODEOFF_MAX_RATIONALE = 1200
CODEOFF_MAX_AGENT_SECONDS = 1200
AGENT_PROCESS_WAIT_GRACE_SECONDS = 60
AGENT_TERM_GRACE_SECONDS = 10
AGENT_KILL_GRACE_SECONDS = 5
CODEOFF_TERMINAL_DRAIN_SECONDS = CODEOFF_MAX_AGENT_SECONDS + AGENT_PROCESS_WAIT_GRACE_SECONDS + AGENT_TERM_GRACE_SECONDS + AGENT_KILL_GRACE_SECONDS + 45
CODEOFF_PROVENANCE_MAX_FILES = 65536
CODEOFF_PROVENANCE_MAX_BYTES = 8 * 1024 * 1024
CODEOFF_PROVENANCE_MAX_EVENTS = 10000
CODEOFF_LOCK = threading.RLock()
class CodeOffArtifactError(RuntimeError): pass
FAILURE_CLASS_CODES = {
    "none": {"success"},
    "provider_availability": {"missing_binary"},
    "timeout_budget": {"run_budget_exceeded", "reported_timeout"},
    "model_execution": {"process_exit", "spawn_failed", "model_stopped", "model_reported_error"},
    "malformed_contract": {"invalid_receipt", "adapter_contract_invalid", "missing_prompt"},
    "session_provenance": {"session_identity_missing", "session_identity_mismatch", "session_provenance_invalid", "launcher_version_mismatch"},
    "repository_state": {"repository_mismatch", "branch_mismatch", "head_mismatch"},
    "cancelled": {"cancelled"},
    "unknown": {"unknown_failure"},
}
FAILURE_CLASS_BY_CODE = {code: failure_class for failure_class, codes in FAILURE_CLASS_CODES.items() for code in codes}
PROVIDER_FAILURE_GROUPS = (
    ("provider_authentication", {"authentication_error", "authentication_failed", "invalid_api_key", "unauthorized"}),
    ("provider_quota", {"quota_exceeded", "insufficient_quota"}),
    ("provider_rate_limit", {"rate_limit_error", "rate_limit_exceeded", "rate_limited"}),
    ("provider_network", {"network_error", "connection_error", "dns_error", "tls_error"}),
    ("provider_overloaded", {"overloaded", "overloaded_error", "service_unavailable"}),
)
PROVIDER_AVAILABILITY_CODES = frozenset(
    FAILURE_CLASS_CODES["provider_availability"]
    | {"provider_http_5xx", *(code for code, _ in PROVIDER_FAILURE_GROUPS)}
)
DIAGNOSTIC_VERSION = "diagnostic-v1"
DIAGNOSTIC_METADATA = {
    **{
        code: ("workflow_failure", "execution", "closed_failure_code", "bounded workflow operation failed")
        for code in FAILURE_CLASS_BY_CODE if code != "success"
    },
    **{
        code: ("provider_availability", "provider", "structured_provider_error", "provider temporarily unavailable")
        for code in PROVIDER_AVAILABILITY_CODES
    },
    "none": ("none", "none", "success", "ok"),
    "missing_binary": ("provider_availability", "binary", "deterministic_binary_check", "writer binary unavailable"),
    "provider_authentication": ("provider_availability", "auth", "structured_provider_error", "provider authentication rejected"),
    "provider_network": ("provider_availability", "network", "structured_provider_error", "provider network unavailable"),
    "not_configured": ("local_infrastructure", "config", "deterministic_config_check", "local configuration unavailable"),
    "bad_health_config": ("local_infrastructure", "config", "deterministic_config_check", "local health probe misconfigured"),
    "health_unreachable": ("local_infrastructure", "network", "bounded_loopback_probe", "local health endpoint unreachable"),
    "health_bad_status": ("local_infrastructure", "health", "bounded_loopback_probe", "local health endpoint returned a bad status"),
    "stack_unhealthy": ("local_infrastructure", "stack", "compose_snapshot", "local stack is unhealthy"),
    "port_not_listening": ("local_infrastructure", "port", "bounded_port_probe", "local port is not listening"),
    "local_binary_missing": ("local_infrastructure", "binary", "deterministic_binary_check", "local command binary unavailable"),
    "cmd_failed": ("local_infrastructure", "command", "bounded_command", "local command failed"),
    "timeout": ("local_infrastructure", "command", "bounded_command", "local command timed out"),
    "missing_port": ("workflow_failure", "request", "validated_input", "port input is required"),
    "bad_port": ("workflow_failure", "request", "validated_input", "port input is invalid"),
    "unknown_port": ("workflow_failure", "request", "validated_input", "port is not allowlisted"),
    "unknown_stack": ("workflow_failure", "request", "validated_input", "stack is not configured"),
    "primary_execution_profile_mismatch": ("workflow_failure", "execution", "closed_failure_code", "bounded workflow operation failed"),
}


def compact_diagnostic(code: str) -> dict[str, str]:
    """Canned evidence only: never echo error prose, output, URLs, or paths."""
    safe_code = code if code in DIAGNOSTIC_METADATA else "unknown_failure"
    category, component, evidence, summary = DIAGNOSTIC_METADATA[safe_code]
    return {
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "category": category,
        "component": component,
        "code": safe_code,
        "evidence": evidence,
        "summary": summary,
    }


SLICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLICE_KINDS = frozenset({"build", "decision"})
SLICE_STATUSES = frozenset({"open", "done"})
SLICE_CLASSES = frozenset({"mechanical", "visual", "sensitive"})
SLICE_WORK_KINDS = frozenset({"go_coding", "typescript_coding", "research_ops"})
SLICE_SIZES = ("S", "M", "L")
COUNT_RE = re.compile(r"0|[1-9][0-9]*")
SLICE_ROUTE_INPUT_FIELDS = frozenset({"class", "work_kind", "size", "ac_count", "seams"})
SLICE_ROUTE_FALLBACK_FIELDS = SLICE_ROUTE_INPUT_FIELDS | frozenset({
    "primary_route", "primary_receipt",
})
SLICE_ROUTE_RECOVERY_FIELDS = SLICE_ROUTE_INPUT_FIELDS | frozenset({
    "primary_route", "primary_receipt", "fallback_route", "fallback_receipt",
    "fresh_primary_receipt",
})
ROUTE_VERSION = "normal-v1"
FALLBACK_ROUTE_VERSION = "availability-fallback-v1"
ROUTING_POLICY_V2_VERSION = "routing-policy-v2"
ROUTING_POLICY_V2_ROUTE_VERSION = "routing-policy-v2-candidate"
ROUTE_EXECUTION_PROFILE_VERSION = "route-execution-profile-v1"
ROUTE_EXECUTION_ROLES = frozenset({"writer", "reviewer1", "reviewer2"})
ROUTE_EXECUTION_PROFILE_FIELDS = frozenset({
    "version", "route_version", "role", "work_kind", "class", "size",
    "launcher", "provider", "model", "effort",
})
RECOVERY_VERSION = "provider-recovery-v1"
NORMAL_WRITER_ROUTES = {
    "go_coding": ("codex", "openai", "gpt-5.6-sol", "high"),
    "typescript_coding": ("claude", "anthropic", "claude-opus-5", "default"),
    "research_ops": ("grok", "xai", "grok-4.6", "default"),
}
AVAILABILITY_FALLBACK_WRITER_ROUTES = {
    "go_coding": ("claude", "anthropic", "claude-opus-5", "default"),
    "typescript_coding": ("codex", "openai", "gpt-5.6-sol", "high"),
    "research_ops": ("claude", "anthropic", "claude-sonnet-5", "default"),
}
NORMAL_REVIEWER_ROUTES = {
    "openai": (
        ("claude", "anthropic", "claude-sonnet-5", "default"),
        ("grok", "xai", "grok-4.6", "default"),
    ),
    "anthropic": (
        ("codex", "openai", "gpt-5.6-sol", "high"),
        ("grok", "xai", "grok-4.6", "default"),
    ),
    "xai": (
        ("codex", "openai", "gpt-5.6-sol", "high"),
        ("claude", "anthropic", "claude-opus-5", "default"),
    ),
}
# Benchmark-only candidate profiles. This table is deliberately not reachable
# from sliceRoute and its route version is not accepted as execution evidence.
_SONNET_MEDIUM = ("claude", "anthropic", "claude-sonnet-5", "medium")
_SOL_HIGH = ("codex", "openai", "gpt-5.6-sol", "high")
_OPUS_HIGH = ("claude", "anthropic", "claude-opus-5", "high")
_GROK_MEDIUM = ("grok", "xai", "grok-4.6", "medium")
_GROK_HIGH = ("grok", "xai", "grok-4.6", "high")
ROUTING_POLICY_V2_WRITER_TABLE = {
    "go_coding": {
        "mechanical": {"S": _SONNET_MEDIUM, "M": _SONNET_MEDIUM, "L": _SOL_HIGH},
        "visual": {"S": _SOL_HIGH, "M": _SOL_HIGH, "L": _SOL_HIGH},
        "sensitive": {"S": _SOL_HIGH, "M": _SOL_HIGH, "L": _SOL_HIGH},
    },
    "typescript_coding": {
        "mechanical": {"S": _SONNET_MEDIUM, "M": _SONNET_MEDIUM, "L": _OPUS_HIGH},
        "visual": {"S": _OPUS_HIGH, "M": _OPUS_HIGH, "L": _OPUS_HIGH},
        "sensitive": {"S": _OPUS_HIGH, "M": _OPUS_HIGH, "L": _OPUS_HIGH},
    },
    "research_ops": {
        "mechanical": {"S": _GROK_MEDIUM, "M": _GROK_MEDIUM, "L": _GROK_HIGH},
        "visual": {"S": _GROK_MEDIUM, "M": _GROK_MEDIUM, "L": _GROK_HIGH},
        "sensitive": {"S": _GROK_HIGH, "M": _GROK_HIGH, "L": _GROK_HIGH},
    },
}
# Reviews need enough turns to read the ticket and diff before returning a verdict.
REVIEW_MAX_TURNS = int(os.environ.get("GRAPHWING_REVIEW_MAX_TURNS", "12"))
REVIEW_MIN_REQUEST_TURNS = 1
REVIEW_MAX_REQUEST_TURNS = 30
REVIEW_DEFAULT_BUDGET_SECONDS = 200
REVIEW_MIN_BUDGET_SECONDS = 30
REVIEW_MAX_BUDGET_SECONDS = 600
SLICE_BUDGET = {
    ("mechanical", "S"): (10, 120),
    ("mechanical", "M"): (30, 300),
    ("mechanical", "L"): (50, 600),
    ("visual", "S"): (10, 180),
    ("visual", "M"): (30, 600),
    ("visual", "L"): (50, 900),
    ("sensitive", "S"): (10, 180),
    ("sensitive", "M"): (30, 600),
    ("sensitive", "L"): (50, 900),
}
REF_NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SESSION_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
JOB_LOCK = threading.Lock()
JOB_RECORD_LOCKS_LOCK = threading.Lock()
JOB_RECORD_LOCKS: dict[str, threading.RLock] = {}
REVIEW_AUTHORITIES_LOCK = threading.Lock()
REVIEW_AUTHORITIES: dict[str, Any] = {}
REPO_LOCKS_LOCK = threading.Lock()
REPO_LOCKS: dict[str, threading.RLock] = {}
RUNS_LOCK = threading.Lock()
HERDR_LINGER_LOCK = threading.Lock()
HERDR_LINGER: dict[str, float] = {}
JWKS_LOCK = threading.Lock()
JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
DOORBELL_PROMPT = "Doorbell: checks or review changed. If checks red, one fix slice. If mergeable, stop. Do not merge."
PR_EVIDENCE_VERSION = "pr-merge-evidence-v1"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def public_base_url() -> str | None:
    env = os.environ.get("GRAPHWING_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    meta_path = HOME / "cloudflared-meta.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    host = str(data.get("hostname") or "").strip().rstrip("/")
    if not host:
        return None
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"https://{host}"


def openapi_bytes() -> bytes:
    raw = OPENAPI_PATH.read_bytes()
    url = public_base_url()
    if not url:
        return raw
    spec = json.loads(raw)
    spec["servers"] = [{"url": url}]
    return (json.dumps(spec, indent=2) + "\n").encode()


def resolve_under_home(raw: str | Path | None) -> Path:
    path = Path("." if raw in (None, "") else raw)
    if not path.is_absolute():
        path = HOME / path
    return path.resolve()


def load_key() -> bytes:
    env = os.environ.get("GRAPHWING_KEY", "").encode()
    if env.strip():
        return env.strip()
    if not KEY_PATH.is_file():
        raise RuntimeError(f"missing API key: set GRAPHWING_KEY or write {KEY_PATH}")
    raw = KEY_PATH.read_bytes().strip()
    if not raw:
        raise RuntimeError(f"empty API key at {KEY_PATH}")
    return raw


def load_repos() -> dict[str, str]:
    if not REPOS_PATH.is_file():
        return {}
    raw = REPOS_PATH.read_text().strip() or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"repos.json must be an object: {REPOS_PATH}")
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}


def load_doorbell() -> dict[str, Any]:
    config: dict[str, Any] = {
        "audience": public_base_url() or "https://graphwing.tfour.net",
        "issuer": GITHUB_OIDC_ISSUER,
        "jwks_url": GITHUB_OIDC_JWKS_URL,
        "allow_repos": {
            "RewstApp/riftwing": {
                "repo": "riftwing-drive",
                "test": "go-fmt",
                "prompt": DOORBELL_PROMPT,
                "commit_message": "pr-drive doorbell",
            },
            "tfournet/graphwing": {
                "repo": "graphwing",
                "test": "catalog-compile",
                "prompt": DOORBELL_PROMPT,
                "commit_message": "pr-drive doorbell",
            },
        },
        "allow_actors": ["tfournet"],
    }
    path = HOME / "doorbell.json"
    if not path.is_file():
        return config
    overlay = json.loads(path.read_text())
    if not isinstance(overlay, dict):
        raise RuntimeError(f"doorbell.json must be an object: {path}")
    config.update(overlay)
    return config


def load_rewst_install() -> dict[str, Any]:
    path = HOME / "rewst-install.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"rewst-install.json must be an object: {path}")
    return data


REWST_HOOK_URL_KEYS = {
    "implement-slice": "implement_slice_hook_url",
    "pr-drive": "pr_drive_hook_url",
}
WATCH_RUN_INPUT_KEYS = ("repo", "pr", "ticket", "branch", "index", "test", "class", "size")
WATCH_WORKFLOW_RECENT = 8


def sanitize_run_input(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in WATCH_RUN_INPUT_KEYS:
        val = inp.get(key)
        if val in (None, ""):
            continue
        if isinstance(val, str):
            out[key] = val[:80]
        elif isinstance(val, (int, float, bool)):
            out[key] = val
        else:
            out[key] = str(val)[:80]
    return out


def extract_run_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("id", "run_id", "runId"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:80]
    nested = body.get("data")
    if isinstance(nested, dict):
        return extract_run_id(nested)
    return None


def workflow_run_title(workflow: str, inp: dict[str, Any]) -> str:
    ticket = str(inp.get("ticket") or "").strip()
    if ticket:
        name = Path(ticket).name
        if name.endswith(".md"):
            name = name[:-3]
        return f"{workflow} {name}"
    pr = inp.get("pr")
    if pr not in (None, ""):
        return f"{workflow} PR {pr}"
    repo = str(inp.get("repo") or "").strip()
    if repo:
        return f"{workflow} {repo}"
    return workflow


def public_workflow_run(row: dict[str, Any]) -> dict[str, Any]:
    inp = row.get("input") if isinstance(row.get("input"), dict) else {}
    workflow = str(row.get("workflow") or "workflow")
    return {
        "kind": workflow,
        "title": str(row.get("title") or workflow_run_title(workflow, inp)),
        "status": str(row.get("status") or "fired"),
        "repo": inp.get("repo"),
        "tab": "graph",
        "created_at": row.get("created_at"),
        "started_at": row.get("created_at"),
        "finished_at": None,
        "summary": workflow_run_title(workflow, inp),
        "run_id": row.get("run_id"),
        "source": row.get("source"),
        "error": None,
        "job_id": "",
    }


def record_workflow_run(row: dict[str, Any]) -> None:
    payload = {
        "workflow": str(row.get("workflow") or ""),
        "status": str(row.get("status") or "fired"),
        "source": str(row.get("source") or "fire"),
        "input": row.get("input") if isinstance(row.get("input"), dict) else {},
        "run_id": row.get("run_id"),
        "created_at": str(row.get("created_at") or utcnow()),
    }
    payload["title"] = workflow_run_title(payload["workflow"], payload["input"])
    try:
        with RUNS_LOCK:
            RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with RUNS_PATH.open("a") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError:
        return


def list_install_run_stubs(limit: int) -> list[dict[str, Any]]:
    try:
        install = load_rewst_install()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for key in ("implement_slice", "pr_drive", "pr_status", "verify_stack"):
        item = install.get(key)
        if not isinstance(item, dict):
            continue
        if not item.get("run_id"):
            continue
        slug = str(item.get("slug") or key.replace("_", "-"))
        inp: dict[str, Any] = {}
        rows.append(
            public_workflow_run(
                {
                    "workflow": slug,
                    "status": str(item.get("status") or "recorded"),
                    "source": "install",
                    "input": inp,
                    "run_id": item.get("run_id"),
                    "created_at": None,
                }
            )
        )
        if len(rows) >= limit:
            break
    return rows


def list_workflow_runs(limit: int) -> list[dict[str, Any]]:
    if not RUNS_PATH.is_file():
        return list_install_run_stubs(limit)
    try:
        lines = RUNS_PATH.read_text().splitlines()
    except OSError:
        return list_install_run_stubs(limit)
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or not item.get("workflow"):
            continue
        rows.append(public_workflow_run(item))
        if len(rows) >= limit:
            break
    return rows or list_install_run_stubs(limit)


def rewst_fire(body: bytes) -> tuple[int, dict[str, Any]]:
    """Proxy a run request to a Rewst webhook trigger.

    The caller sends a workflow name and its input. This process holds the
    trigger URL and the shared secret, both in $GRAPHWING_HOME, so nothing
    that fires a run needs the webhook key. Same reason `repos.json` lives
    here and not in a graph: secrets belong to the seat, not the caller.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    workflow = str(data.get("workflow") or "").strip()
    if workflow not in REWST_HOOK_URL_KEYS:
        return 400, {
            "error": f"unknown workflow '{workflow}'",
            "code": "unknown_workflow",
            "allowed": sorted(REWST_HOOK_URL_KEYS),
        }
    payload = data.get("input")
    if not isinstance(payload, dict):
        return 400, {"error": "input must be an object", "code": "bad_input"}

    install = load_rewst_install()
    url = str(install.get(REWST_HOOK_URL_KEYS[workflow]) or "").strip()
    if not url:
        return 503, {
            "error": f"no trigger url recorded for '{workflow}'",
            "code": "no_hook_url",
            "hint": f"set {REWST_HOOK_URL_KEYS[workflow]} in rewst-install.json",
        }
    if not valid_webhook_url(url):
        return 500, {"error": "recorded trigger url is not https", "code": "bad_hook_url"}
    secret = str(install.get("hook_secret") or "").strip()
    if not secret:
        return 503, {"error": "no hook_secret recorded", "code": "no_hook_secret"}

    req = Request(
        url,
        data=json.dumps({"input": payload}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-rewst-secret": secret},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", "replace")[:2000]
    except Exception as exc:
        return 502, {"ok": False, "error": str(exc)[:300], "code": "hook_failed", "workflow": workflow}
    out: dict[str, Any] = {"ok": 200 <= status < 300, "workflow": workflow, "status": status}
    try:
        out["body"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        out["body"] = {"raw": raw}
    record_workflow_run(
        {
            "workflow": workflow,
            "status": "fired" if out["ok"] else "failed",
            "source": "fire",
            "input": sanitize_run_input(payload),
            "run_id": extract_run_id(out.get("body")),
            "created_at": utcnow(),
        }
    )
    return (200 if out["ok"] else 502), out


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_jwks(url: str) -> dict[str, Any]:
    now = time.time()
    with JWKS_LOCK:
        cached = JWKS_CACHE.get(url)
        if cached and cached[0] > now:
            return cached[1]
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise ValueError("invalid JWKS")
        JWKS_CACHE[url] = (now + 3600, data)
        return data


def verify_github_oidc(token: str, config: dict[str, Any]) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT")
    header = json.loads(_base64url_decode(parts[0]))
    claims = json.loads(_base64url_decode(parts[1]))
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
        raise ValueError("invalid JWT")
    kid = header.get("kid")
    jwks = _load_jwks(str(config["jwks_url"]))
    jwk = next(
        (
            item
            for item in jwks["keys"]
            if isinstance(item, dict) and item.get("kid") == kid and item.get("kty") == "RSA"
        ),
        None,
    )
    if jwk is None:
        raise ValueError("unknown signing key")
    n = int.from_bytes(_base64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_base64url_decode(str(jwk["e"])), "big")
    signature = _base64url_decode(parts[2])
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        raise ValueError("invalid signature")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("invalid signing key")
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    actual = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("invalid signature")
    if claims.get("iss") != config.get("issuer"):
        raise ValueError("invalid issuer")
    audience = config.get("audience")
    claim_audience = claims.get("aud")
    audiences = claim_audience if isinstance(claim_audience, list) else [claim_audience]
    if audience not in audiences:
        raise ValueError("invalid audience")
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires <= time.time():
        raise ValueError("expired JWT")
    return claims


def _allowed_cwds(repos: dict[str, str] | None = None) -> set[str]:
    repos = repos if repos is not None else load_repos()
    allowed = {str(Path(p).resolve()) for p in repos.values()}
    allowed.add(str(HOME.resolve()))
    return allowed


def load_recipes(path: Path, key: str, repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get(key), list):
        raw = data[key]
    elif isinstance(data, list):
        raw = data
    else:
        raise RuntimeError(f"{path} must list {key}")
    allowed_cwds = _allowed_cwds(repos)
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        argv = item.get("argv")
        if not name or not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise RuntimeError(f"{path} entry missing name/argv: {item!r}")
        raw_cwd = item.get("cwd")
        repos_map = repos if repos is not None else load_repos()
        if isinstance(raw_cwd, str) and raw_cwd.strip() in repos_map:
            cwd = Path(repos_map[raw_cwd.strip()]).resolve()
        else:
            cwd = resolve_under_home(raw_cwd)
        if str(cwd) not in allowed_cwds:
            continue
        try:
            timeout = int(item.get("timeout_seconds"))
        except (TypeError, ValueError):
            raise RuntimeError(f"{name} timeout_seconds must be an int") from None
        if timeout < 1:
            raise RuntimeError(f"{name} timeout_seconds must be >= 1")
        resolved_argv = list(argv)
        if resolved_argv[0] == "rr":
            resolved_argv[0] = str(RR_BIN)
        out[name] = {
            "name": name,
            "argv": resolved_argv,
            "cwd": cwd,
            "timeout_seconds": timeout,
            "async": bool(item.get("async")),
        }
    return out


def load_scripts(repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    return load_recipes(SCRIPTS_PATH, "scripts", repos)


def load_tests(repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    return load_recipes(TESTS_PATH, "tests", repos)


def load_rr(repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    if not RR_PATH.is_file():
        return {}
    data = json.loads(RR_PATH.read_text())
    if isinstance(data, dict) and not data.get("recipes"):
        return {}
    if isinstance(data, list) and not data:
        return {}
    return load_recipes(RR_PATH, "recipes", repos)


def load_stacks() -> tuple[dict[str, dict[str, Any]], set[int]]:
    if not STACKS_PATH.is_file():
        return {}, set()
    data = json.loads(STACKS_PATH.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"stacks.json must be an object: {STACKS_PATH}")
    ports: set[int] = set()
    for raw in data.get("ports") or []:
        try:
            p = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= p <= 65535:
            ports.add(p)
    stacks: dict[str, dict[str, Any]] = {}
    for item in data.get("stacks") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        cwd = resolve_under_home(item.get("cwd") or ".")
        if not name or not cwd.is_dir():
            continue
        health = []
        for h in item.get("health") or []:
            if isinstance(h, dict) and h.get("name") and h.get("url"):
                health.append({"name": str(h["name"]), "url": str(h["url"])})
        sport: list[int] = []
        for raw in item.get("ports") or []:
            try:
                p = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= p <= 65535:
                sport.append(p)
                ports.add(p)
        stacks[name] = {
            "name": name,
            "cwd": cwd,
            "compose_file": str(item.get("compose_file") or "docker-compose.yml"),
            "health": health,
            "ports": sport,
        }
    if not stacks and not ports:
        raise RuntimeError(f"stacks.json has no stacks or ports: {STACKS_PATH}")
    return stacks, ports


def first_query(qs: dict[str, list[str]], name: str) -> str | None:
    vals = qs.get(name)
    if not vals:
        return None
    v = vals[0].strip()
    return v or None


def query_rel(qs: dict[str, list[str]]) -> str | None:
    """Repo-relative file for fileHead and gitDiff.

    Named `rel`, not `path`. Rewst's custom-integration connector treats a
    parameter called `path` as the request URL path, so it overwrote the
    endpoint with the value and sent GET /slices/01-ticket.md instead of
    GET /v1/file/head?... The whole walk 404'd at its first fileHead.

    `path` stays accepted so integration v9 keeps working until v10 ships.
    """
    return first_query(qs, "rel") or first_query(qs, "path")


def default_repo_name(repos: dict[str, str]) -> str:
    preferred = os.environ.get("GRAPHWING_DEFAULT_REPO", "").strip()
    if preferred and preferred in repos:
        return preferred
    return next(iter(repos), "")


def resolve_repo(name: str | None, repos: dict[str, str]) -> tuple[str, Path] | tuple[None, dict[str, Any]]:
    if not repos:
        return None, {
            "error": "no allowlisted repos; add short names in repos.json",
            "code": "no_repos",
            "allowed": [],
        }
    key = (name or default_repo_name(repos)).strip()
    if key not in repos:
        return None, {
            "error": f"unknown repo '{key}'",
            "code": "unknown_repo",
            "allowed": sorted(repos),
        }
    path = Path(repos[key])
    if not path.is_dir():
        return None, {"error": f"repo path missing: {path}", "code": "repo_missing"}
    probe = run_cmd(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], timeout=5)
    if not probe.get("ok"):
        return None, {"error": f"not a git repo: {path}", "code": "not_git"}
    return key, path


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int = CMD_TIMEOUT, env: dict[str, str] | None = None) -> dict[str, Any]:
    merged = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=merged,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"missing binary: {args[0]}", "code": "local_binary_missing", "status": 501}
    except subprocess.TimeoutExpired as exc:
        raw_out = exc.stdout or b""
        raw_err = exc.stderr or b""
        return {
            "ok": False,
            "error": "command timed out",
            "code": "timeout",
            "status": 504,
            "stdout": raw_out[:CMD_MAX_BYTES].decode("utf-8", "replace"),
            "stderr": raw_err[:CMD_MAX_BYTES].decode("utf-8", "replace"),
            "truncated": len(raw_out) > CMD_MAX_BYTES or len(raw_err) > CMD_MAX_BYTES,
        }
    stdout = proc.stdout[:CMD_MAX_BYTES]
    stderr = proc.stderr[:CMD_MAX_BYTES]
    truncated = len(proc.stdout) > CMD_MAX_BYTES or len(proc.stderr) > CMD_MAX_BYTES
    text = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    out: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": text,
        "stderr": err,
        "truncated": truncated,
    }
    if proc.returncode != 0:
        out["error"] = err.strip() or text.strip() or f"{args[0]} exited {proc.returncode}"
        out["code"] = "cmd_failed"
        out["status"] = 400
    return out


def run_git(cwd: Path, args: list[str]) -> dict[str, Any]:
    return run_cmd(["git", "-C", str(cwd), *args], cwd=cwd)


def run_git_pathspec(cwd: Path, args: list[str], paths: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="graphwing-pathspec-") as temp_dir:
        source = Path(temp_dir) / "paths"
        source.write_bytes(b"".join(os.fsencode(f":(literal){rel}") + b"\0" for rel in paths))
        return run_cmd([
            "git", "-C", str(cwd), *args, f"--pathspec-from-file={source}", "--pathspec-file-nul",
        ], cwd=cwd, env=env)


def _writer_status_paths(path: Path) -> tuple[set[str] | None, dict[str, Any] | None]:
    result = run_git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if not result.get("ok") or result.get("truncated"):
        return None, {"error": "could not snapshot complete writer paths", "code": "repository_mismatch"}
    fields, paths, index = result["stdout"].split("\0"), set(), 0
    while index < len(fields) and fields[index]:
        record = fields[index]
        if len(record) < 4:
            return None, {"error": "malformed git status", "code": "repository_mismatch"}
        paths.add(record[3:])
        if record[0] in "RC" or record[1] in "RC":
            index += 1
            if index >= len(fields) or not fields[index]:
                return None, {"error": "malformed git rename status", "code": "repository_mismatch"}
            paths.add(fields[index])
        index += 1
    return paths, None


def _writer_path_fingerprint(repo: Path, rel: str) -> str:
    digest = hashlib.sha256()
    index = run_git(repo, ["ls-files", "-s", "--", f":(literal){rel}"])
    digest.update((index.get("stdout") or "").encode())
    target = repo / rel
    try:
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode):
            digest.update(b"symlink\0" + os.readlink(target).encode())
        elif stat.S_ISREG(mode):
            digest.update(b"file\0")
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(f"mode:{mode}".encode())
    except OSError:
        digest.update(b"missing")
    return digest.hexdigest()


def capture_writer_baseline(repo: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    paths, err = _writer_status_paths(repo)
    if err:
        return None, err
    return {"dirty": {rel: _writer_path_fingerprint(repo, rel) for rel in sorted(paths or ())}}, None


def stage_writer_changes(job: dict[str, Any]) -> dict[str, Any] | None:
    repo = Path(job["cwd"])
    baseline = job.get("git_baseline")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("dirty"), dict):
        return {"error": "writer baseline is missing", "code": "repository_mismatch"}
    identity = job.get("session_identity") or {}
    if identity.get("starting_head"):
        branch, head, err = current_branch_head(repo)
        if err or branch != identity.get("branch") or head != identity.get("starting_head"):
            return {"error": "repository changed during writer job", "code": "repository_mismatch"}
    current, err = _writer_status_paths(repo)
    if err:
        return err
    inherited = set(job.get("writer_parent_paths") or ())
    dirty = baseline["dirty"]
    conflicts = sorted(rel for rel, before in dirty.items() if rel not in inherited and _writer_path_fingerprint(repo, rel) != before)
    if conflicts:
        return {"error": "writer changed pre-existing dirty paths", "code": "writer_dirty_conflict", "paths": conflicts[:8], "more_paths": max(0, len(conflicts) - 8)}
    paths = sorted(((current or set()) - set(dirty)) | (inherited & (current or set())))
    for rel in paths:
        _target, path_err = safe_relpath(repo, rel)
        if path_err:
            return path_err
    if paths:
        added = run_git_pathspec(repo, ["add"], paths)
        if not added.get("ok"):
            return {"error": "Graphwing could not stage writer paths", "code": "repository_mismatch"}
        staged, staged_err = _writer_status_paths(repo)
        if staged_err:
            return staged_err
        paths = sorted(set(paths) & (staged or set()))
    job["writer_paths"] = paths
    job["writer_fingerprints"] = {rel: _writer_path_fingerprint(repo, rel) for rel in paths}
    return None


def git_status(path: Path) -> dict[str, Any]:
    r = run_git(path, ["status", "--porcelain=v1", "-b"])
    if not r.get("ok"):
        return r
    lines = r["stdout"].splitlines()
    branch = ""
    if lines and lines[0].startswith("## "):
        branch = lines[0][3:]
        lines = lines[1:]
    return {"ok": True, "branch": branch, "dirty": bool(lines), "entries": lines, "truncated": r["truncated"]}


def git_diff(path: Path, ref: str | None, file_path: str | None) -> dict[str, Any]:
    args = ["diff"]
    if ref:
        args.append(ref)
    if file_path:
        args.extend(["--", file_path])
    r = run_git(path, args)
    if not r.get("ok"):
        return r
    return {"ok": True, "diff": r["stdout"], "truncated": r["truncated"]}


def git_log(path: Path, n: int, ref: str | None) -> dict[str, Any]:
    args = ["log", f"-n{n}", "--format=%H%x09%an%x09%ad%x09%s", "--date=iso-strict"]
    if ref:
        args.append(ref)
    r = run_git(path, args)
    if not r.get("ok"):
        return r
    commits = []
    for line in r["stdout"].splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"ok": True, "commits": commits, "truncated": r["truncated"]}


def git_branch(path: Path) -> dict[str, Any]:
    r = run_git(path, ["branch", "--list", "--no-color"])
    if not r.get("ok"):
        return r
    current = None
    names = []
    for line in r["stdout"].splitlines():
        name = line[2:].strip()
        if not name:
            continue
        names.append(name)
        if line.startswith("* "):
            current = name
    return {"ok": True, "current": current, "branches": names}


def git_show(path: Path, rev: str) -> dict[str, Any]:
    r = run_git(path, ["show", "--stat", "--format=fuller", rev])
    if not r.get("ok"):
        return r
    return {"ok": True, "rev": rev, "show": r["stdout"], "truncated": r["truncated"]}


def git_rev_parse(path: Path, rev: str) -> dict[str, Any]:
    r = run_git(path, ["rev-parse", rev])
    if not r.get("ok"):
        return r
    return {"ok": True, "rev": rev, "sha": r["stdout"].strip()}


def git_worktree_list(path: Path) -> dict[str, Any]:
    r = run_git(path, ["worktree", "list", "--porcelain"])
    if not r.get("ok"):
        return r
    trees = []
    cur: dict[str, Any] = {}
    for line in r["stdout"].splitlines():
        if not line:
            if cur:
                trees.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:]
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
    if cur:
        trees.append(cur)
    return {"ok": True, "worktrees": trees}


def safe_relpath(repo: Path, rel: str) -> tuple[Path, None] | tuple[None, dict[str, Any]]:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or rel.startswith("~") or "\x00" in rel:
        return None, {"error": "path must be relative and stay inside the repo", "code": "bad_path"}
    if ".." in Path(rel).parts:
        return None, {"error": "path must be relative and stay inside the repo", "code": "bad_path"}
    target = (repo / rel).resolve()
    try:
        target.relative_to(repo.resolve())
    except ValueError:
        return None, {"error": "path escapes repo", "code": "bad_path"}
    return target, None


def safe_file(repo: Path, rel: str) -> tuple[Path, None] | tuple[None, dict[str, Any]]:
    target, err = safe_relpath(repo, rel)
    if err:
        return None, err
    if not target.is_file():
        return None, {"error": f"not a file: {rel}", "code": "not_file"}
    return target, None


def valid_branch(name: str) -> bool:
    if not isinstance(name, str) or not name or len(name) > 255:
        return False
    # Phase 1 intentionally exposes one portable ASCII subset, not every name
    # accepted by git-check-ref-format. Keep this set aligned with BranchName.
    if re.fullmatch(r"[A-Za-z0-9._/-]+", name) is None:
        return False
    if name == "@" or name.startswith(("-", "/")) or name.endswith("/"):
        return False
    if ".." in name or "@{" in name or "//" in name:
        return False
    if any(ord(char) < 32 or ord(char) == 127 or char in " ~^:?*[\\" for char in name):
        return False
    parts = name.split("/")
    if any(
        not part or part.startswith(".") or part.endswith(".")
        or part.lower().endswith(".lock")
        for part in parts
    ):
        return False
    return True


def valid_push_ref(ref: str) -> bool:
    if not isinstance(ref, str) or not ref.strip():
        return False
    ref = ref.strip()
    if ref.startswith("+") or ref.startswith(":") or ":" in ref or ref.startswith("-"):
        return False
    if "*" in ref or "?" in ref:
        return False
    if ref == "HEAD":
        return True
    return valid_branch(ref)


def valid_remote(name: str) -> bool:
    return isinstance(name, str) and bool(REMOTE_NAME_RE.fullmatch(name)) and not name.startswith("-")


def repo_from_body(data: dict[str, Any], repos: dict[str, str]):
    raw = data.get("repo")
    if not isinstance(raw, str) or not raw.strip():
        return None, {"error": "repo is required", "code": "missing_repo"}
    key = raw.strip()
    if "/" in key or key.startswith(".") or key.startswith("~") or "\\" in key:
        return None, {
            "error": "repo must be an allowlisted short name, not a filesystem path",
            "code": "bad_repo",
            "allowed": sorted(repos),
        }
    return resolve_repo(key, repos)


def repo_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with REPO_LOCKS_LOCK:
        return REPO_LOCKS.setdefault(key, threading.RLock())


def repo_write_locked(operation: Any) -> Any:
    """Serialize only Graphwing mutations/evidence for the same checkout."""
    def locked(body: bytes, repos: dict[str, str]):
        try:
            raw = json.loads(body)
            path = repos.get(raw.get("repo")) if isinstance(raw, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            path = None
        with repo_lock(Path(path)) if path else nullcontext():
            return operation(body, repos)
    return locked


def git_write_result(name: str, r: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    payload = {
        "ok": bool(r.get("ok")),
        "repo": name,
        "stdout": r.get("stdout") or "",
        "stderr": r.get("stderr") or "",
        "truncated": bool(r.get("truncated")),
        "returncode": r.get("returncode"),
    }
    if not r.get("ok"):
        payload["error"] = r.get("error")
        payload["code"] = r.get("code")
        return int(r.get("status", 400)), payload
    return 200, payload


def _codeoff_git_gate(data: dict[str, Any], repo_name: str, repo: Path, phase: str) -> dict[str, Any] | None:
    experiment_id = data.get("codeoff_experiment_id")
    receipt_hash = data.get("final_verification_hash")
    if experiment_id in (None, "") and receipt_hash in (None, ""):
        return None
    if not isinstance(experiment_id, str) or not isinstance(receipt_hash, str):
        return {"error": "both codeoff_experiment_id and final_verification_hash are required", "code": "codeoff_gate_incomplete"}
    root, manifest, state, err = _codeoff_load(experiment_id, active=False)
    if err:
        return err
    assert root and manifest and state
    final = state.get("final_verification") or {}
    if not (state.get("finalized") and state.get("status") == "completed" and final.get("commit_eligible") is True and final.get("push_eligible") is True and hmac.compare_digest(str(final.get("receipt_hash") or ""), receipt_hash)):
        return {"error": "code-off final verification is not eligible or does not match", "code": "codeoff_gate_rejected"}
    if manifest.get("repo") != repo_name:
        return {"error": "code-off receipt belongs to a different repo", "code": "codeoff_repo_mismatch"}
    if phase == "commit" and data.get("message", "").strip() != manifest.get("commit_message"):
        return {"error": "commit message differs from the immutable code-off contract", "code": "codeoff_commit_mismatch"}
    try:
        snapshot = codeoff_tree_snapshot(repo, manifest["base_sha"])
    except (OSError, RuntimeError):
        return {"error": "could not verify promoted code-off tree", "code": "codeoff_tree_unavailable"}
    if snapshot["manifest_hash"] != final.get("tree_manifest_hash"):
        return {"error": "repo tree no longer equals the frozen winner", "code": "codeoff_tree_mismatch"}
    branch, head, git_err = current_branch_head(repo)
    if git_err or branch != manifest["branch"] or (phase == "commit" and (head != manifest["base_sha"] or not run_git(repo, ["diff", "--cached", "--quiet", manifest["base_sha"]]).get("ok"))):
        return {"error": "repo HEAD is not eligible for this code-off operation", "code": "codeoff_head_mismatch"}
    if phase == "push":
        parents = run_git(repo, ["rev-list", "--parents", "-n", "1", "HEAD"])
        message = run_git(repo, ["show", "-s", "--format=%B", "HEAD"]); committed_tree = run_git(repo, ["rev-parse", "HEAD^{tree}"])
        lineage = str(parents.get("stdout") or "").split()
        if head == manifest["base_sha"]:
            return {"error": "verified winner has not been committed", "code": "codeoff_head_mismatch"}
        if not parents.get("ok") or lineage != [head, manifest["base_sha"]] or not message.get("ok") or str(message.get("stdout") or "").strip() != manifest["commit_message"] or not committed_tree.get("ok") or str(committed_tree.get("stdout") or "").strip() != state.get("promotion", {}).get("git_tree"):
            return {"error": "push HEAD is not the single immutable winner commit", "code": "codeoff_push_mismatch"}
    return None


@repo_write_locked
def git_checkout(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    branch = data.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        return 400, {"error": "branch is required", "code": "missing_branch"}
    branch = branch.strip()
    if not valid_branch(branch):
        return 400, {"error": "branch looks like a path or is not a safe ref", "code": "bad_branch"}
    create = data.get("create")
    if create not in (None, False, True):
        return 400, {"error": "create must be a boolean", "code": "bad_create"}
    if create:
        # "create if needed", not "create or fail". Structure commits the slice
        # map on the story branch before the run fires, so by the time Graph
        # checks out, the branch already exists and `checkout -b` dies with
        # returncode 128. Switch to it instead.
        exists = run_git(resolved, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if exists.get("ok"):
            out = run_git(resolved, ["checkout", branch])
        else:
            out = run_git(resolved, ["checkout", "-b", branch])
    else:
        verify = run_git(resolved, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if not verify.get("ok"):
            return 400, {"error": f"unknown branch '{branch}'", "code": "unknown_branch", "repo": name}
        out = run_git(resolved, ["checkout", branch])
    return git_write_result(name, out)


@repo_write_locked
def git_restore(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if "force" in data or "force_with_lease" in data:
        return 400, {"error": "force is not allowed", "code": "force_rejected"}
    for field in ("remote", "ref", "delete"):
        if field in data:
            return 400, {"error": f"{field} is not accepted", "code": "extra_field"}
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    clean_untracked = data.get("clean_untracked", False)
    if not isinstance(clean_untracked, bool):
        return 400, {"error": "clean_untracked must be a boolean", "code": "bad_clean_untracked"}
    out = run_git(resolved, ["reset", "--hard", "HEAD"])
    if not out.get("ok") or not clean_untracked:
        return git_write_result(name, out)
    return git_write_result(name, run_git(resolved, ["clean", "-fd"]))


@repo_write_locked
def git_commit(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    assert isinstance(resolved, Path)
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return 400, {"error": "message is required", "code": "empty_message"}
    if data.get("add_all") is True:
        return 400, {"error": "add_all is not enabled; pass add as relative paths or commit staged files", "code": "add_all_disabled"}
    gate_err = _codeoff_git_gate(data, name, resolved, "commit")
    if gate_err:
        return 409, gate_err
    add = data.get("add")
    if isinstance(add, str):
        add = [add]
    if add is not None and (not isinstance(add, list) or not all(isinstance(x, str) for x in add)):
        return 400, {"error": "add must be a list of relative paths or one path string", "code": "bad_add"}
    rels: list[str] = []
    for rel in add or []:
        _target, perr = safe_relpath(resolved, rel)
        if perr:
            perr["ok"] = False
            return 400, perr
        rels.append(rel)
    writer_id = data.get("writer_job_id")
    if writer_id == "":
        writer_id = None
    codeoff_commit = (
        data.get("codeoff_experiment_id") not in (None, "")
        and data.get("final_verification_hash") not in (None, "")
    )
    if writer_id is None and not codeoff_commit:
        return 400, {
            "error": "ordinary commits require writer_job_id",
            "code": "writer_job_required",
        }
    writer_paths: list[str] = []
    if writer_id is not None:
        writer = read_job(writer_id) if isinstance(writer_id, str) and JOB_ID_RE.fullmatch(writer_id) else None
        if not is_agent_job_record(writer):
            writer = None
        descendants: dict[str, list[dict[str, Any]]] = {}
        for path in JOBS_DIR.glob("*/job.json"):
            child = read_job(path.parent.name) or {}
            parent_id = child.get("writer_parent_job_id")
            if (
                parent_id and is_agent_job_record(child)
                and child.get("status") == "completed"
                and (child.get("receipt") or {}).get("status") == "ok"
            ):
                descendants.setdefault(parent_id, []).append(child)
        visited_writer_jobs: set[str] = set()
        while writer:
            current_writer_id = writer.get("job_id")
            if not isinstance(current_writer_id, str) or current_writer_id in visited_writer_jobs:
                return 409, {"error": "writer resume chain is cyclic", "code": "writer_job_mismatch"}
            visited_writer_jobs.add(current_writer_id)
            children = descendants.get(current_writer_id, [])
            if len(children) > 1:
                return 409, {"error": "writer resume chain is ambiguous", "code": "writer_job_mismatch"}
            if not children:
                break
            writer = children[0]
        receipt = writer.get("receipt") if isinstance(writer, dict) else None
        identity = writer.get("session_identity") if isinstance(writer, dict) else None
        branch, head, state_err = current_branch_head(resolved)
        if (
            not writer or writer.get("status") != "completed" or not isinstance(receipt, dict)
            or receipt.get("status") != "ok" or writer.get("repo") != name
            or Path(str(writer.get("cwd") or "")).resolve() != resolved.resolve()
            or not isinstance(identity, dict) or branch != identity.get("branch")
            or head != identity.get("starting_head") or state_err
            or validate_writer_evidence(writer.get("job_id"), name, resolved, str(branch or "")) is not None
        ):
            return 409, {"error": "writer job is not valid for this commit", "code": "writer_job_mismatch"}
        writer_paths = writer.get("writer_paths") if isinstance(writer.get("writer_paths"), list) else []
        dirty_before = set((writer.get("git_baseline") or {}).get("dirty") or {})
        overlap = sorted(rel for rel in rels if rel in dirty_before and rel not in writer_paths)
        if overlap:
            return 409, {"error": "commit paths overlap pre-existing dirty state", "code": "writer_dirty_conflict", "paths": overlap[:8], "more_paths": max(0, len(overlap) - 8)}
        changed = sorted(rel for rel in writer_paths if _writer_path_fingerprint(resolved, rel) != (writer.get("writer_fingerprints") or {}).get(rel))
        if changed:
            return 409, {"error": "writer paths changed after their receipt", "code": "writer_paths_changed", "paths": changed[:8], "more_paths": max(0, len(changed) - 8)}
    commit_paths = list(dict.fromkeys([*writer_paths, *rels]))
    if writer_id is not None and not commit_paths:
        return 400, {"ok": False, "repo": name, "code": "nothing_staged", "error": "writer job produced no commit paths"}
    if writer_id is None:
        if rels:
            added = run_git_pathspec(resolved, ["add"], rels)
            if not added.get("ok"):
                return git_write_result(name, added)
        out = run_git(resolved, ["commit", "-m", message.strip()])
    else:
        if rels:
            added = run_git_pathspec(resolved, ["add"], rels)
            if not added.get("ok"):
                return git_write_result(name, added)
        with tempfile.TemporaryDirectory(prefix="graphwing-commit-") as temp_dir:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp_dir) / "index")}
            prepared = run_cmd(["git", "-C", str(resolved), "read-tree", "HEAD"], cwd=resolved, env=env)
            if not prepared.get("ok"):
                return git_write_result(name, prepared)
            added = run_git_pathspec(resolved, ["add"], commit_paths, env=env)
            if not added.get("ok"):
                return git_write_result(name, added)
            out = run_cmd(["git", "-C", str(resolved), "commit", "-m", message.strip()], cwd=resolved, env=env)
    if not out.get("ok"):
        blob = ((out.get("stdout") or "") + (out.get("stderr") or "")).lower()
        # An empty commit is still a failed slice, per the lock. It is just a
        # different failure from a broken commit, and saying so is the
        # difference between "the writer had nothing to do" and "the commit
        # is broken" — which read identically in the trace and cost two rounds
        # of misdiagnosis.
        if "nothing to commit" in blob or "no changes added to commit" in blob:
            return 400, {"ok": False, "repo": name, "code": "nothing_staged",
                         "error": "nothing was staged; the writer made no changes",
                         "stdout": out.get("stdout") or "", "stderr": out.get("stderr") or ""}
    return git_write_result(name, out)


@repo_write_locked
def git_push(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    gate_err = _codeoff_git_gate(data, name, resolved, "push")
    if gate_err:
        return 409, gate_err
    if data.get("force") or data.get("force_with_lease") or data.get("delete"):
        return 400, {"error": "force/delete push is not allowed", "code": "force_rejected"}
    remote = data.get("remote") if data.get("remote") not in ("", None) else "origin"
    if not isinstance(remote, str) or not valid_remote(remote):
        return 400, {"error": "remote must be a named remote, not a URL or flag", "code": "bad_remote"}
    remotes = run_git(resolved, ["remote"])
    if not remotes.get("ok"):
        return git_write_result(name, remotes)
    known = {line.strip() for line in (remotes.get("stdout") or "").splitlines() if line.strip()}
    if remote not in known:
        return 400, {"error": f"unknown remote '{remote}'", "code": "unknown_remote", "allowed": sorted(known)}
    ref = data.get("ref")
    if ref in ("", None):
        ref = "HEAD"
    if not isinstance(ref, str) or not valid_push_ref(ref):
        return 400, {"error": "ref rejected (no force, no delete, no refspec)", "code": "bad_ref"}
    out = run_git(resolved, ["push", remote, ref])
    return git_write_result(name, out)


def file_head(repo: Path, rel: str) -> dict[str, Any]:
    target, err = safe_file(repo, rel)
    if err:
        err["ok"] = False
        err["status"] = 400
        return err
    data = target.read_bytes()[:FILE_MAX_BYTES]
    truncated = target.stat().st_size > FILE_MAX_BYTES
    return {
        "ok": True,
        "path": rel,
        "bytes": len(data),
        "truncated": truncated,
        "text": data.decode("utf-8", "replace"),
    }


def parse_slice_index(data: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    if not isinstance(data, dict):
        return None, {"ok": False, "error": "index must be an object", "code": "bad_index", "status": 400}
    raw = data.get("tickets")
    if not isinstance(raw, list) or not raw:
        return None, {"ok": False, "error": "index.tickets must be a non-empty list", "code": "bad_index", "status": 400}
    tickets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, {"ok": False, "error": "ticket must be an object", "code": "bad_ticket", "status": 400}
        tid = item.get("id")
        path = item.get("path")
        if not isinstance(tid, str) or not SLICE_ID_RE.fullmatch(tid):
            return None, {"ok": False, "error": "ticket id is invalid", "code": "bad_ticket_id", "status": 400}
        if tid in seen:
            return None, {"ok": False, "error": f"duplicate ticket id '{tid}'", "code": "dup_ticket_id", "status": 400}
        if not isinstance(path, str) or not path.strip() or ".." in path or path.startswith("/"):
            return None, {"ok": False, "error": "ticket path is invalid", "code": "bad_ticket_path", "status": 400}
        kind = item.get("kind") or "build"
        status = item.get("status") or "open"
        blocked = item.get("blocked_by") or []
        if kind not in SLICE_KINDS:
            return None, {"ok": False, "error": f"unknown kind '{kind}'", "code": "bad_ticket_kind", "status": 400}
        if status not in SLICE_STATUSES:
            return None, {"ok": False, "error": f"unknown status '{status}'", "code": "bad_ticket_status", "status": 400}
        if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
            return None, {"ok": False, "error": "blocked_by must be a list of ids", "code": "bad_blocked_by", "status": 400}
        seen.add(tid)
        tickets.append(
            {"id": tid, "path": path.strip(), "kind": kind, "status": status, "blocked_by": list(blocked)}
        )
    ids = {t["id"] for t in tickets}
    for t in tickets:
        for b in t["blocked_by"]:
            if b not in ids:
                return None, {
                    "ok": False,
                    "error": f"blocked_by unknown id '{b}'",
                    "code": "bad_blocked_by",
                    "status": 400,
                }
    return tickets, None


def load_slice_index(repo: Path, rel: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    target, err = safe_file(repo, rel)
    if err:
        err["ok"] = False
        err["status"] = 400
        return None, err
    try:
        data = json.loads(target.read_text())
    except json.JSONDecodeError:
        return None, {"ok": False, "error": "index is not JSON", "code": "bad_index", "status": 400}
    return parse_slice_index(data)


def write_slice_index(
    repo: Path, rel: str, tickets: list[dict[str, Any]], extra: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    target, err = safe_relpath(repo, rel)
    if err:
        err["ok"] = False
        err["status"] = 400
        return err
    target.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = dict(extra or {})
    doc["tickets"] = tickets
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(target)
    return None


def index_extra(repo: Path, rel: str) -> dict[str, Any]:
    target, err = safe_file(repo, rel)
    if err or target is None:
        return {}
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "tickets"}


def slice_ready(ticket: dict[str, Any], done: set[str]) -> bool:
    return ticket["status"] == "open" and all(b in done for b in ticket["blocked_by"])


def slice_frontier(tickets: list[dict[str, Any]], ticket_path: str | None = None) -> dict[str, Any]:
    done = {t["id"] for t in tickets if t["status"] == "done"}
    if ticket_path:
        matches = [t for t in tickets if t["path"] == ticket_path]
        if not matches:
            return {"ok": False, "error": f"unknown ticket path '{ticket_path}'", "code": "unknown_ticket", "status": 400}
        t = matches[0]
        if not slice_ready(t, done):
            return {"ok": False, "error": "ticket is not on the frontier", "code": "ticket_blocked", "status": 409}
        return {"ok": True, "kind": t["kind"], "id": t["id"], "path": t["path"]}
    for t in tickets:
        if slice_ready(t, done):
            return {"ok": True, "kind": t["kind"], "id": t["id"], "path": t["path"]}
    return {"ok": True, "kind": "empty"}


def slice_frontier_op(qs: dict[str, list[str]], repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    name, resolved = resolve_repo(first_query(qs, "repo"), repos)
    if name is None:
        return 400, resolved
    index = first_query(qs, "index")
    if not index:
        return 400, {"error": "index is required", "code": "missing_index"}
    tickets, err = load_slice_index(resolved, index)
    if err:
        return int(err.get("status", 400)), err
    assert tickets is not None
    out = slice_frontier(tickets, first_query(qs, "ticket") or None)
    out["repo"] = name
    out["index"] = index
    return (200 if out.get("ok") else int(out.get("status", 400))), out


def slice_complete(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    index = data.get("index")
    if not isinstance(index, str) or not index.strip():
        return 400, {"error": "index is required", "code": "missing_index"}
    tid = data.get("id")
    if not isinstance(tid, str) or not tid.strip():
        return 400, {"error": "id is required", "code": "missing_ticket_id"}
    tickets, load_err = load_slice_index(resolved, index.strip())
    if load_err:
        return int(load_err.get("status", 400)), load_err
    assert tickets is not None
    found = None
    for t in tickets:
        if t["id"] == tid.strip():
            found = t
            break
    if found is None:
        return 400, {"error": f"unknown ticket id '{tid}'", "code": "unknown_ticket"}
    found["status"] = "done"
    write_err = write_slice_index(resolved, index.strip(), tickets, extra=index_extra(resolved, index.strip()))
    if write_err:
        return int(write_err.get("status", 400)), write_err
    return 200, {"ok": True, "repo": name, "index": index.strip(), "id": found["id"], "path": found["path"]}


PR_MAX_ATTEMPTS = int(os.environ.get("GRAPHWING_PR_MAX_ATTEMPTS", "3"))


def pr_continue(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Kick the next pr-drive attempt, or stop at the ceiling.

    pr-drive took one swing at a red PR and ended. Driving to green means
    looping, and Rewst forbids unbounded cycles inside one run, so the next
    attempt is a fresh run kicked at kick_url, the way slice_continue walks
    the slice map.

    The ceiling is not optional. A writer that cannot fix a finding would
    otherwise re-run forever, and each attempt costs a full agent session.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    number = str(data.get("pr") or data.get("number") or "").strip()
    if not number:
        return 400, {"error": "pr is required", "code": "missing_pr"}

    attempt, aerr = parse_optional_int(data, "attempt", 0, 99)
    if aerr:
        return 400, aerr
    attempt = attempt or 1
    ceiling, cerr = parse_optional_int(data, "max_attempts", 1, 99)
    if cerr:
        return 400, cerr
    ceiling = ceiling or PR_MAX_ATTEMPTS

    kick_url = data.get("kick_url")
    if isinstance(kick_url, str) and kick_url.strip() and not valid_webhook_url(kick_url):
        return 400, {"error": "kick_url must be https", "code": "bad_kick_url"}

    out = {"ok": True, "repo": name, "pr": number, "attempt": attempt,
           "max_attempts": ceiling, "kicked": False}
    if attempt >= ceiling:
        return 200, {**out, "code": "attempts_exhausted",
                     "error": f"{attempt} of {ceiling} attempts used"}
    if not kick_url:
        return 200, {**out, "code": "no_kick_url"}

    token = data.get("kick_token") or None
    payload = {
        "repo": name,
        # The next run reads this as CTX.INPUT.pr_number. A plain `pr` key never
        # arrives: the graph declared one and every reference resolved empty, so
        # pr-drive died at ghPrView with no `number` on the query string.
        "pr_number": number,
        "test": data.get("test"),
        "attempt": attempt + 1,
        "max_attempts": ceiling,
        "auto_merge": bool(data.get("auto_merge")),
        "kick_url": kick_url,
        "kick_token": token,
    }
    hook = post_receipt(kick_url, payload, token=token)
    out["kicked"] = bool(hook.get("ok"))
    out["kick"] = hook
    if not out["kicked"]:
        return 502, {**out, "ok": False, "error": "kick failed", "code": "kick_failed"}
    return 200, out


def slice_continue(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    index = data.get("index")
    if not isinstance(index, str) or not index.strip():
        return 400, {"error": "index is required", "code": "missing_index"}
    tickets, load_err = load_slice_index(resolved, index.strip())
    if load_err:
        return int(load_err.get("status", 400)), load_err
    assert tickets is not None
    nxt = slice_frontier(tickets)
    out = {
        "ok": True,
        "repo": name,
        "index": index.strip(),
        "kind": nxt.get("kind"),
        "id": nxt.get("id"),
        "path": nxt.get("path"),
        "kicked": False,
    }
    recovery_fields = (
        "prior_primary_route", "prior_primary_receipt", "prior_fallback_route",
        "prior_fallback_receipt",
    )
    recovery_values = {key: data.get(key) for key in recovery_fields}
    recovery_version = data.get("recovery_version")
    fresh_primary_receipt = data.get("fresh_primary_receipt")
    recovery_supplied = recovery_version not in (None, "") or any(
        data.get(key) not in (None, "")
        for key in (*recovery_fields, "fresh_primary_receipt")
    )
    if recovery_supplied and (
        recovery_version != RECOVERY_VERSION
        or any(not isinstance(value, dict) for value in recovery_values.values())
        or (
            fresh_primary_receipt not in (None, "")
            and not isinstance(fresh_primary_receipt, dict)
        )
    ):
        return 400, {
            "error": "complete versioned recovery evidence is required",
            "code": "malformed_recovery_evidence",
        }
    if nxt.get("kind") != "build":
        return 200, out
    kick_url = data.get("kick_url")
    if kick_url in ("", None):
        return 200, out
    if not isinstance(kick_url, str) or not valid_webhook_url(kick_url):
        return 400, {"error": "kick_url must be https", "code": "bad_kick_url"}
    token = data.get("kick_token")
    if token in ("", None):
        token = None
    elif not isinstance(token, str):
        return 400, {"error": "kick_token must be a string", "code": "bad_kick_token"}
    payload = {
        "repo": name,
        "branch": data.get("branch"),
        "index": index.strip(),
        "ticket": nxt["path"],
        "test": data.get("test"),
        "commit_message": data.get("commit_message"),
        "iters_left": data.get("iters_left"),
        "class": data.get("class"),
        "work_kind": data.get("work_kind"),
        "size": data.get("size"),
        "ac_count": data.get("ac_count"),
        "seams": data.get("seams"),
        "kick_url": kick_url,
        "kick_token": token,
    }
    if recovery_version == RECOVERY_VERSION:
        payload["recovery_version"] = RECOVERY_VERSION
        payload.update(recovery_values)
        if fresh_primary_receipt not in (None, ""):
            payload["fresh_primary_receipt"] = fresh_primary_receipt
    hook = post_receipt(kick_url, payload, token=token)
    out["kicked"] = bool(hook.get("ok"))
    out["kick"] = hook
    if not out["kicked"]:
        return 502, {**out, "ok": False, "error": "kick failed", "code": "kick_failed"}
    return 200, out


def slice_e2e(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    test_name = data.get("test")
    if test_name in ("", None):
        return 200, {"ok": True, "kind": "skip"}
    if not isinstance(test_name, str):
        return 400, {"error": "test must be a string", "code": "bad_test"}
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    index = data.get("index")
    if not isinstance(index, str) or not index.strip():
        return 400, {"error": "index is required", "code": "missing_index"}
    index = index.strip()
    tickets, load_err = load_slice_index(resolved, index)
    if load_err:
        return int(load_err.get("status", 400)), load_err
    assert tickets is not None
    extra = index_extra(resolved, index)
    catalog = load_tests(repos)
    spec = catalog.get(test_name.strip())
    if spec is None:
        return 400, {"error": f"unknown test '{test_name}'", "code": "unknown_test", "allowed": sorted(catalog)}
    result = run_cmd(list(spec["argv"]), cwd=spec["cwd"], timeout=int(spec["timeout_seconds"]))
    compact = compact_cmd_signal(result)
    if result.get("ok"):
        return 200, {"ok": True, "kind": "green", "repo": name, "compact": "ok"}
    reds = int(extra.get("e2e_reds") or 0) + 1
    extra["e2e_reds"] = reds
    if reds >= 3:
        write_err = write_slice_index(resolved, index, tickets, extra=extra)
        if write_err:
            return int(write_err.get("status", 400)), write_err
        return 200, {"ok": True, "kind": "park", "repo": name, "e2e_reds": reds, "compact": compact}
    hits = [ln.split("FAIL:", 1)[-1].strip() for ln in compact.splitlines() if "FAIL:" in ln]
    rel_dir = str(Path(index).parent)
    if hits:
        tid = f"e2e-{reds}"
        rel_md = f"{rel_dir}/{tid}.md"
        md_target, perr = safe_relpath(resolved, rel_md)
        if perr:
            return 400, perr
        assert md_target is not None
        md_target.parent.mkdir(parents=True, exist_ok=True)
        body_md = "E2E failed. Ticket from failing names.\n\n" + "\n".join(f"- {h}" for h in hits[:8]) + "\n"
        md_target.write_text(body_md)
        tickets.append(
            {"id": tid, "path": rel_md, "blocked_by": [], "kind": "build", "status": "open"}
        )
        write_err = write_slice_index(resolved, index, tickets, extra=extra)
        if write_err:
            return int(write_err.get("status", 400)), write_err
        return 200, {
            "ok": True,
            "kind": "auto",
            "repo": name,
            "id": tid,
            "path": rel_md,
            "e2e_reds": reds,
            "compact": compact,
        }
    draft = f"{rel_dir}/e2e-draft-{reds}.md"
    draft_path, perr = safe_relpath(resolved, draft)
    if perr:
        return 400, perr
    assert draft_path is not None
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text("E2E failed. Draft — ACK before it becomes a ticket.\n\n" + compact + "\n")
    write_err = write_slice_index(resolved, index, tickets, extra=extra)
    if write_err:
        return int(write_err.get("status", 400)), write_err
    return 200, {
        "ok": True,
        "kind": "draft",
        "repo": name,
        "path": draft,
        "e2e_reds": reds,
        "compact": compact,
    }


def parse_optional_int(data: dict[str, Any], key: str, lo: int, hi: int) -> tuple[int | None, dict[str, Any] | None]:
    raw = data.get(key)
    if raw in ("", None):
        return None, None
    # Rewst renders graph inputs as strings, so a plain ASCII decimal string is
    # the only accepted text form. Booleans, floats, signs, separators, and
    # non-ASCII digits are never silently coerced into a routing count.
    bad = {"error": f"{key} must be an integer", "code": f"bad_{key}"}
    if isinstance(raw, bool):
        return None, bad
    if isinstance(raw, int):
        n = raw
    elif isinstance(raw, str) and COUNT_RE.fullmatch(raw):
        n = int(raw)
    else:
        return None, bad
    if n < lo or n > hi:
        return None, {"error": f"{key} out of range", "code": f"bad_{key}"}
    return n, None


def bump_slice_size(size: str, class_name: str, ac_count: int | None, seams: int | None) -> str:
    i = SLICE_SIZES.index(size)
    bump = 0
    if ac_count is not None and ac_count >= 6:
        bump = 1
    if class_name == "visual" and (seams is None or seams == 1):
        bump = max(bump, 1)
    return SLICE_SIZES[min(i + bump, len(SLICE_SIZES) - 1)]


def routing_policy_v2_candidate(
    class_name: str,
    size_floor: str,
    ac_count: int | None = None,
    seams: int | None = None,
    work_kind: str | None = None,
) -> dict[str, Any]:
    """Return one non-active candidate route for deterministic benchmarking."""
    if class_name not in SLICE_CLASSES or size_floor not in SLICE_SIZES:
        raise ValueError("unsupported candidate class or size")
    if work_kind not in SLICE_WORK_KINDS:
        raise ValueError("work_kind is required")
    # The candidate is closed around the same typed task-shape counts as the
    # active route, so a benchmark cannot be fed values the public route rejects.
    counts: dict[str, int | None] = {}
    for key, value, ceiling in (("ac_count", ac_count, 99), ("seams", seams, 20)):
        parsed, count_error = parse_optional_int({key: value}, key, 0, ceiling)
        if count_error is not None:
            raise ValueError(f"unsupported candidate {key}")
        counts[key] = parsed
    ac_count, seams = counts["ac_count"], counts["seams"]
    sized = bump_slice_size(size_floor, class_name, ac_count, seams)
    if seams is None or seams > 1:
        sized = "L"
    writer = ROUTING_POLICY_V2_WRITER_TABLE[work_kind][class_name][sized]
    reason = (
        f"Candidate {ROUTING_POLICY_V2_VERSION}: {work_kind}, {class_name} effective "
        f"size {sized} selects {writer[2]} at {writer[3]} effort; benchmark evidence "
        "is required before activation."
    )
    route = build_slice_route(
        class_name, size_floor, ac_count, seams, work_kind, writer,
        ROUTING_POLICY_V2_ROUTE_VERSION, reason, effective_size=sized,
    )
    return {
        **route,
        "policy_version": ROUTING_POLICY_V2_VERSION,
        "policy_status": "benchmark_candidate",
    }


def slice_route_lookup(
    class_name: str,
    size_floor: str,
    ac_count: int | None = None,
    seams: int | None = None,
    work_kind: str | None = None,
) -> dict[str, Any]:
    if work_kind is None:
        raise ValueError("work_kind is required")
    return build_slice_route(
        class_name,
        size_floor,
        ac_count,
        seams,
        work_kind,
        NORMAL_WRITER_ROUTES[work_kind],
        ROUTE_VERSION,
        f"work_kind={work_kind}",
    )


def route_execution_profile(route: dict[str, Any], role: str) -> dict[str, str] | None:
    """Project one exact, closed execution profile from a server-selected route."""
    if role not in ROUTE_EXECUTION_ROLES:
        return None
    prefix = "" if role == "writer" else f"{role}_"
    values = {
        "launcher": route.get(f"{prefix}launcher"),
        "provider": route.get(f"{prefix}provider"),
        "model": route.get(f"{prefix}model"),
        "effort": route.get(f"{prefix}effort"),
    }
    if any(not isinstance(value, str) or value == "none" for value in values.values()):
        return None
    return {
        "version": ROUTE_EXECUTION_PROFILE_VERSION,
        "route_version": str(route.get("route_version") or ""),
        "role": role,
        "work_kind": str(route.get("work_kind") or ""),
        "class": str(route.get("class") or ""),
        "size": str(route.get("size") or ""),
        **values,
    }


def parse_route_execution_profile(
    raw: Any, *, expected_role: str, launcher: str, provider: str, model: str,
    effort: Any,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Corroborate route provenance against the versioned server route catalog."""
    if not isinstance(raw, dict) or set(raw) != ROUTE_EXECUTION_PROFILE_FIELDS:
        return None, {
            "error": "route_execution_profile must be one exact closed object",
            "code": "bad_route_execution_profile",
        }
    if (
        raw.get("version") != ROUTE_EXECUTION_PROFILE_VERSION
        or raw.get("route_version") not in {ROUTE_VERSION, FALLBACK_ROUTE_VERSION}
        or raw.get("role") != expected_role
        or raw.get("work_kind") not in SLICE_WORK_KINDS
        or raw.get("class") not in SLICE_CLASSES
        or raw.get("size") not in SLICE_SIZES
        or any(not isinstance(raw.get(key), str) for key in ROUTE_EXECUTION_PROFILE_FIELDS)
    ):
        return None, {
            "error": "route_execution_profile is not a supported route selection",
            "code": "bad_route_execution_profile",
        }
    work_kind = str(raw["work_kind"])
    route_version = str(raw["route_version"])
    if route_version == ROUTE_VERSION:
        writer = NORMAL_WRITER_ROUTES[work_kind]
        unavailable_provider = None
        reason = f"work_kind={work_kind}"
    else:
        writer = AVAILABILITY_FALLBACK_WRITER_ROUTES[work_kind]
        unavailable_provider = NORMAL_WRITER_ROUTES[work_kind][1]
        reason = "availability_fallback:validated"
    expected_route = build_slice_route(
        str(raw["class"]), str(raw["size"]), None, 0, work_kind, writer,
        route_version, reason, unavailable_provider,
    )
    expected = route_execution_profile(expected_route, expected_role)
    if (
        expected is None or raw != expected
        or (raw["launcher"], raw["provider"], raw["model"], raw["effort"])
        != (launcher, provider, model, effort)
    ):
        return None, {
            "error": "route_execution_profile does not match the request and route catalog",
            "code": "route_execution_profile_mismatch",
        }
    profile, profile_error = normalize_effort(launcher, provider, model, effort, "route")
    if profile_error is not None or profile is None:
        return None, profile_error or {
            "error": "route effort is unsupported", "code": "unsupported_effort"
        }
    return dict(raw), None


def build_slice_route(
    class_name: str,
    size_floor: str,
    ac_count: int | None,
    seams: int | None,
    work_kind: str,
    writer: tuple[str, str, str, str],
    route_version: str,
    reason: str,
    unavailable_provider: str | None = None,
    *,
    effective_size: str | None = None,
) -> dict[str, Any]:
    sized = effective_size or bump_slice_size(size_floor, class_name, ac_count, seams)
    turns, wait = SLICE_BUDGET[(class_name, sized)]
    launcher, provider, model, effort = writer
    if class_name == "sensitive":
        review_count = 2
    elif class_name == "mechanical" and sized == "S":
        review_count = 0
    else:
        review_count = 1
    reviewer_candidates = list(NORMAL_REVIEWER_ROUTES[provider])
    if unavailable_provider:
        reviewer_candidates.sort(key=lambda reviewer: reviewer[1] == unavailable_provider)
    review_effort = "high" if class_name == "sensitive" else "medium"
    reviewers = [(*reviewer[:3], review_effort) for reviewer in reviewer_candidates[:review_count]]
    while len(reviewers) < 2:
        reviewers.append(("none", "none", "none", "none"))
    reviewer1, reviewer2 = reviewers
    route = {
        "ok": True,
        "route_version": route_version,
        "class": class_name,
        "work_kind": work_kind,
        "size_floor": size_floor,
        "size": sized,
        "launcher": launcher,
        "provider": provider,
        "model": model,
        "effort": effort,
        "reason": reason,
        "max_turns": turns,
        "run_budget_seconds": wait,
        "reviewer1": reviewer1[2],
        "reviewer1_launcher": reviewer1[0],
        "reviewer1_provider": reviewer1[1],
        "reviewer1_model": reviewer1[2],
        "reviewer1_effort": reviewer1[3],
        "reviewer1_reason": "different_provider_from_writer" if reviewer1[0] != "none" else "not_required_by_review_policy",
        "reviewer2": reviewer2[2],
        "reviewer2_launcher": reviewer2[0],
        "reviewer2_provider": reviewer2[1],
        "reviewer2_model": reviewer2[2],
        "reviewer2_effort": reviewer2[3],
        "reviewer2_reason": "different_provider_from_writer_and_reviewer1" if reviewer2[0] != "none" else "not_required_by_review_policy",
        "review": "none" if reviewer1[0] == "none" else (
            f"{reviewer1[2]}_{reviewer2[2]}" if reviewer2[0] != "none" else reviewer1[2]
        ),
    }
    for role in ("writer", "reviewer1", "reviewer2"):
        profile = route_execution_profile(route, role)
        if profile is not None:
            route[f"{role}_execution_profile"] = profile
    return route


def parse_slice_route_common(data: dict[str, Any]) -> tuple[str, str, str, int | None, int | None, dict[str, Any] | None]:
    class_name = str(data.get("class") or "mechanical").strip()
    size_floor = str(data.get("size") or "M").strip()
    work_kind = str(data.get("work_kind") or "").strip()
    if class_name not in SLICE_CLASSES:
        return "", "", "", None, None, {"error": "class must be mechanical, visual, or sensitive", "code": "bad_class"}
    if size_floor not in SLICE_SIZES:
        return "", "", "", None, None, {"error": "size must be S, M, or L", "code": "bad_size"}
    if work_kind not in SLICE_WORK_KINDS:
        return "", "", "", None, None, {
            "error": "work_kind must be go_coding, typescript_coding, or research_ops",
            "code": "bad_work_kind",
        }
    ac_count, ac_err = parse_optional_int(data, "ac_count", 0, 99)
    if ac_err:
        return "", "", "", None, None, ac_err
    seams, seams_err = parse_optional_int(data, "seams", 0, 20)
    if seams_err:
        return "", "", "", None, None, seams_err
    return class_name, size_floor, work_kind, ac_count, seams, None


def slice_route(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    unexpected = sorted(set(data) - SLICE_ROUTE_INPUT_FIELDS)
    if unexpected:
        return 400, {
            "error": "slice route request contains unsupported fields",
            "code": "unexpected_fields",
            "fields": unexpected,
        }
    class_name, size_floor, work_kind, ac_count, seams, common_err = parse_slice_route_common(data)
    if common_err:
        return 400, common_err
    return 200, slice_route_lookup(class_name, size_floor, ac_count, seams, work_kind)


def derive_slice_fallback_route(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Derive the one allowed fallback from an already-decoded request object."""
    class_name, size_floor, work_kind, ac_count, seams, common_err = parse_slice_route_common(data)
    if common_err:
        return 400, common_err
    primary_route = data.get("primary_route")
    if not isinstance(primary_route, dict):
        return 400, {"error": "primary_route is required", "code": "missing_primary_route"}
    receipt = data.get("primary_receipt")
    if not isinstance(receipt, dict):
        return 400, {"error": "primary_receipt is required", "code": "missing_primary_receipt"}

    expected_primary_route = slice_route_lookup(class_name, size_floor, ac_count, seams, work_kind)
    if primary_route != expected_primary_route:
        return 400, {"error": "primary route does not match normal route", "code": "primary_provider_mismatch"}
    normal_launcher, normal_provider, normal_model, _ = NORMAL_WRITER_ROUTES[work_kind]

    eligible = receipt.get("failover_eligible")
    if not isinstance(eligible, bool):
        return 400, {"error": "failover_eligible must be a boolean", "code": "bad_failover_eligible"}
    if (
        receipt.get("status") != "error"
        or receipt.get("role") != "primary"
        or receipt.get("failure_class") != "provider_availability"
        or not eligible
    ):
        return 400, {"error": "receipt is not availability-fallback eligible", "code": "not_fallback_eligible"}
    session_identity = receipt.get("session_identity")
    if not isinstance(session_identity, dict) or any(
        session_identity.get(key) != value
        for key, value in (("launcher", normal_launcher), ("provider", normal_provider), ("model", normal_model))
    ):
        return 400, {"error": "receipt session_identity does not match normal route", "code": "primary_provider_mismatch"}
    parsed_identity, identity_err = parse_session_identity(session_identity)
    if (
        identity_err
        or parsed_identity != session_identity
        or session_identity.get("route_execution_profile")
        != primary_route.get("writer_execution_profile")
    ):
        code = "primary_execution_profile_mismatch"
        return 400, {
            "error": "primary receipt execution profile does not match normal route",
            "code": code,
            "diagnostic": compact_diagnostic(code),
        }
    failure_code = str(receipt.get("failure_code") or "unknown_failure").strip() or "unknown_failure"
    if failure_code not in PROVIDER_AVAILABILITY_CODES:
        return 400, {"error": "receipt is not availability-fallback eligible", "code": "not_fallback_eligible"}
    expected_efforts = authoritative_route_effort_profiles(
        normal_launcher, normal_provider, normal_model, primary_route.get("effort")
    )
    receipt_source = receipt.get("effort_source")
    receipt_effort, receipt_effort_err = normalize_effort(
        normal_launcher, normal_provider, normal_model,
        receipt.get("requested_effort"),
        receipt_source if isinstance(receipt_source, str) and receipt_source in EFFORT_SOURCES else "route",
    )
    launcher_version = session_identity.get("launcher_version")
    if (
        not expected_efforts
        or receipt_effort_err is not None or receipt_effort is None
        or receipt_source not in EFFORT_SOURCES
        or receipt_effort not in expected_efforts
        or any(session_identity.get(key) != value for key, value in receipt_effort.items())
        or any(receipt.get(key) != session_identity.get(key) for key in AGENT_PROFILE_FIELDS)
        or not isinstance(launcher_version, str)
        or not LAUNCHER_VERSION_RE.fullmatch(launcher_version)
        or receipt.get("effective_effort") != session_identity.get("effective_effort")
        or receipt.get("launcher_version") != launcher_version
    ):
        code = "primary_execution_profile_mismatch"
        return 400, {
            "error": "primary receipt execution profile does not match normal route",
            "code": code,
            "diagnostic": compact_diagnostic(code),
        }
    job_id = receipt.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return 400, {"error": "invalid primary receipt job_id", "code": "bad_job_id"}
    stored_job = read_job(job_id)
    stored_receipt = stored_job.get("receipt") if isinstance(stored_job, dict) else None
    receipt_fields = (
        "status", "job_id", "session_identity", "launcher", "provider", "model",
        "requested_effort", "effective_effort", "effort_source", "launcher_version",
        "failure_class", "failure_code", "failover_eligible",
    )
    if (
        not is_agent_job_record(stored_job)
        or not isinstance(stored_receipt, dict)
        or stored_job.get("status") != "failed"
        or stored_job.get("session_identity") != session_identity
        or any(receipt.get(key) != stored_receipt.get(key) for key in receipt_fields)
        or any(stored_job.get(key) != session_identity.get(key) for key in AGENT_PROFILE_FIELDS)
    ):
        return 400, {"error": "primary receipt does not match its stored job", "code": "primary_receipt_mismatch"}
    fallback = build_slice_route(
        class_name,
        size_floor,
        ac_count,
        seams,
        work_kind,
        AVAILABILITY_FALLBACK_WRITER_ROUTES[work_kind],
        FALLBACK_ROUTE_VERSION,
        f"availability_fallback:{failure_code}",
        normal_provider,
    )
    selected = {
        key: fallback[key]
        for key in ("route_version", "launcher", "provider", "model", "effort", "reason")
    }
    fallback.update(
        {
            "role": "availability_fallback",
            "primary_route_version": primary_route.get("route_version"),
            "primary_reason": primary_route.get("reason"),
            "fallback_reason": fallback["reason"],
            "fallback_code": failure_code,
            "selected_alternate_route": selected,
        }
    )
    return 200, fallback


def slice_route_fallback(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    unexpected = sorted(set(data) - SLICE_ROUTE_FALLBACK_FIELDS)
    if unexpected:
        return 400, {
            "error": "slice fallback request contains unsupported fields",
            "code": "unexpected_fields",
            "fields": unexpected,
        }
    return derive_slice_fallback_route(data)


def resolve_launcher_binary_now(launcher: str) -> Path:
    """Resolve the configured launcher at the request/worker boundary."""
    fallbacks = {
        "codex": Path.home() / ".local" / "bin" / "codex",
        "claude": Path.home() / ".local" / "bin" / "claude",
        "grok": Path.home() / ".local" / "bin" / "grok",
    }
    if launcher not in fallbacks:
        raise ValueError("unknown launcher")
    return resolve_executable(
        launcher,
        f"GRAPHWING_{launcher.upper()}_BIN",
        fallbacks[launcher],
    )


class PinnedLauncher:
    def __init__(self, fd: int, fingerprint: str):
        self.fd = fd
        self.path = Path(f"/proc/self/fd/{fd}")
        self.fingerprint = fingerprint

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


@dataclass(frozen=True)
class NativeReviewExecutionContext:
    """One server-authored native review invocation and its claimed identity."""

    authority_identity: dict[str, Any]
    execution_identity: dict[str, Any]
    repo: str
    resolved: Path
    prompt: str
    diff_text: str
    launcher: str
    provider: str
    model: str
    requested_effort: str
    effective_effort: str
    effort_source: str
    route_execution_profile: dict[str, str] | None
    run_budget_seconds: int
    max_turns: int
    permission_profile: str
    pinned_launcher: PinnedLauncher
    job_id: str | None = None


def pin_launcher(binary: Path) -> PinnedLauncher:
    """Copy one opened launcher into a sealed Linux memfd owned by the caller."""
    source_fd = -1
    sealed_fd = -1
    try:
        source_fd = os.open(binary, os.O_RDONLY | os.O_CLOEXEC)
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("launcher is not a regular file")
        sealed_fd = os.memfd_create("graphwing-launcher", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(sealed_fd, view)
                if written <= 0:
                    raise OSError("could not pin launcher artifact")
                view = view[written:]
        os.fchmod(sealed_fd, stat.S_IMODE(info.st_mode))
        os.lseek(sealed_fd, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(sealed_fd, fcntl.F_ADD_SEALS, seals)
        pinned = PinnedLauncher(sealed_fd, f"sha256:{digest.hexdigest()}")
        sealed_fd = -1
        return pinned
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if sealed_fd >= 0:
            os.close(sealed_fd)


@contextmanager
def pinned_launcher(binary: Path):
    """Copy one opened launcher into a sealed Linux memfd for exact execution."""
    pinned: PinnedLauncher | None = None
    try:
        pinned = pin_launcher(binary)
        yield pinned
    finally:
        if pinned is not None:
            pinned.close()


def launcher_version_fingerprint(binary: Path) -> str | None:
    """Return a path-free immutable launcher artifact identity without executing it."""
    try:
        with pinned_launcher(binary) as pinned:
            return pinned.fingerprint
    except FileNotFoundError:
        return "missing"
    except OSError:
        return None


RECOVERY_RECEIPT_FIELDS = (
    "status", "job_id", "session_identity", "launcher", "provider", "model",
    "requested_effort", "effective_effort", "effort_source", "launcher_version",
    "failure_class", "failure_code", "failover_eligible",
)


def recovery_job_evidence(
    receipt: Any, role: str, route: dict[str, Any], receipt_status: str, job_status: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(receipt, dict):
        return None, {"error": "recovery receipt must be an object", "code": "malformed_recovery_evidence"}
    job_id, identity = receipt.get("job_id"), receipt.get("session_identity")
    if (
        not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id)
        or receipt.get("role") != role or receipt.get("status") != receipt_status
        or not isinstance(identity, dict)
    ):
        return None, {"error": "recovery receipt is malformed", "code": "malformed_recovery_evidence"}
    parsed_identity, identity_err = parse_session_identity(identity)
    if identity_err or parsed_identity != identity or (receipt_status == "ok" and not identity.get("native_session_id")):
        return None, {"error": "recovery session identity is malformed", "code": "malformed_recovery_evidence"}
    if (
        any(identity.get(key) != route.get(key) for key in ("launcher", "provider", "model"))
        or identity.get("route_execution_profile")
        != route.get("writer_execution_profile")
    ):
        return None, {"error": "recovery receipt route identity mismatches", "code": "recovery_evidence_mismatch"}
    source = receipt.get("effort_source")
    route_profiles = authoritative_route_effort_profiles(
        route["launcher"], route["provider"], route["model"], route.get("effort")
    )
    receipt_profile, receipt_profile_err = normalize_effort(
        route["launcher"], route["provider"], route["model"], receipt.get("requested_effort"),
        source if isinstance(source, str) and source in EFFORT_SOURCES else "route",
    )
    if (
        not route_profiles or receipt_profile_err or receipt_profile is None
        or receipt_profile not in route_profiles
        or any(identity.get(key) != value for key, value in receipt_profile.items())
        or any(receipt.get(key) != identity.get(key) for key in AGENT_PROFILE_FIELDS)
        or source not in EFFORT_SOURCES
    ):
        return None, {"error": "recovery execution profile mismatches", "code": "recovery_evidence_mismatch"}
    if receipt_status == "ok" and (
        receipt.get("failure_class") != "none" or receipt.get("failure_code") != "none"
        or receipt.get("failover_eligible") is not False
    ):
        return None, {"error": "successful recovery receipt is malformed", "code": "malformed_recovery_evidence"}
    job = read_job(job_id)
    if not is_agent_job_record(job):
        return None, {"error": "recovery receipt has no durable job", "code": "recovery_evidence_mismatch"}
    assert isinstance(job, dict)
    if job.get("status") in ("queued", "running"):
        return None, {"error": "recovery job is not terminal", "code": "nonterminal_recovery_evidence"}
    stored = job.get("receipt")
    if (
        job.get("status") != job_status or not isinstance(stored, dict)
        or job.get("session_identity") != identity
        or any(job.get(key) != identity.get(key) for key in AGENT_PROFILE_FIELDS)
        or any(receipt.get(key) != stored.get(key) for key in RECOVERY_RECEIPT_FIELDS)
    ):
        return None, {"error": "recovery receipt does not match its durable job", "code": "recovery_evidence_mismatch"}
    return job, None


def recovery_job_time(job: dict[str, Any], field: str) -> str | None:
    raw = job.get(field)
    return raw if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw) else None


def recovery_job_precedes(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_finished = recovery_job_time(first, "finished_at")
    second_created = recovery_job_time(second, "created_at")
    second_finished = recovery_job_time(second, "finished_at")
    return bool(first_finished and second_created and second_finished and first_finished <= second_created <= second_finished)


def recovery_route_identity(route: dict[str, Any], receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "job_id": receipt["job_id"],
        **{key: route[key] for key in ("route_version", "launcher", "provider", "model")},
        "effective_effort": receipt["effective_effort"],
        "launcher_version": receipt["launcher_version"],
    }


def derive_slice_recovery_route(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Select normal or the existing fallback from prior, durable evidence only."""
    class_name, size_floor, work_kind, ac_count, seams, common_err = parse_slice_route_common(data)
    if common_err:
        return 400, common_err
    required = ("primary_route", "primary_receipt", "fallback_route", "fallback_receipt")
    if any(data.get(key) is None for key in required):
        return 400, {"error": "complete prior primary and fallback evidence is required", "code": "missing_recovery_evidence"}
    if any(not isinstance(data.get(key), dict) for key in required):
        return 400, {"error": "recovery routes and receipts must be objects", "code": "malformed_recovery_evidence"}
    primary_route, primary_receipt = data["primary_route"], data["primary_receipt"]
    fallback_route, fallback_receipt = data["fallback_route"], data["fallback_receipt"]
    expected_primary = slice_route_lookup(class_name, size_floor, ac_count, seams, work_kind)
    if primary_route != expected_primary:
        return 400, {"error": "prior primary route mismatches normal route", "code": "recovery_evidence_mismatch"}

    fallback_keys = ("class", "size", "work_kind", "ac_count", "seams", "primary_route", "primary_receipt")
    fallback_request = {key: data[key] for key in fallback_keys if key in data}
    fallback_status, expected_fallback = derive_slice_fallback_route(fallback_request)
    if fallback_status != 200:
        return fallback_status, expected_fallback
    if fallback_route != expected_fallback:
        return 400, {"error": "prior fallback route mismatches deterministic fallback", "code": "recovery_evidence_mismatch"}

    primary_job, evidence_err = recovery_job_evidence(primary_receipt, "primary", expected_primary, "error", "failed")
    if evidence_err:
        return 400, evidence_err
    fallback_job, evidence_err = recovery_job_evidence(fallback_receipt, "availability_fallback", expected_fallback, "ok", "completed")
    if evidence_err:
        return 400, evidence_err
    assert primary_job is not None and fallback_job is not None
    primary_identity, fallback_identity = primary_receipt["session_identity"], fallback_receipt["session_identity"]
    if any(primary_identity.get(key) != fallback_identity.get(key) for key in ("repo", "branch")):
        return 400, {"error": "prior jobs do not share repository provenance", "code": "recovery_evidence_mismatch"}
    if primary_receipt["job_id"] == fallback_receipt["job_id"] or not recovery_job_precedes(primary_job, fallback_job):
        return 400, {"error": "prior fallback evidence is stale", "code": "stale_recovery_evidence"}

    failure_code, fresh_receipt = primary_receipt["failure_code"], data.get("fresh_primary_receipt")
    fresh_job: dict[str, Any] | None = None
    if failure_code == "missing_binary":
        if fresh_receipt not in (None, ""):
            return 400, {"error": "fresh primary receipt is not used for local binary recovery", "code": "malformed_recovery_evidence"}
        available = resolve_launcher_binary_now(expected_primary["launcher"]).is_file()
        selected, decision, basis = (
            (expected_primary, "primary_recovered", "configured_binary_available") if available
            else (expected_fallback, "fallback_retained", "configured_binary_unavailable")
        )
    elif fresh_receipt in (None, ""):
        selected, decision, basis = expected_fallback, "fallback_retained", "fresh_primary_success_required"
    else:
        fresh_job, evidence_err = recovery_job_evidence(fresh_receipt, "primary", expected_primary, "ok", "completed")
        if evidence_err:
            return 400, evidence_err
        assert fresh_job is not None
        if fresh_receipt["job_id"] in {primary_receipt["job_id"], fallback_receipt["job_id"]} or not recovery_job_precedes(fallback_job, fresh_job):
            return 400, {"error": "fresh primary evidence is stale", "code": "stale_recovery_evidence"}
        selected, decision, basis = expected_primary, "primary_recovered", "fresh_primary_success"

    evidence: dict[str, Any] = {
        "recovery_version": RECOVERY_VERSION, "basis": basis, "failure_code": failure_code,
        "primary": recovery_route_identity(expected_primary, primary_receipt),
        "fallback": recovery_route_identity(expected_fallback, fallback_receipt),
    }
    if fresh_job is not None:
        evidence["fresh_primary"] = recovery_route_identity(expected_primary, fresh_receipt)
    return 200, {
        "ok": True, "recovery_version": RECOVERY_VERSION, "decision": decision,
        "role": "primary" if selected["route_version"] == ROUTE_VERSION else "availability_fallback",
        "selected_route": selected, "evidence": evidence,
    }


def slice_route_recovery(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    unexpected = sorted(set(data) - SLICE_ROUTE_RECOVERY_FIELDS)
    if unexpected:
        return 400, {
            "error": "slice recovery request contains unsupported fields",
            "code": "unexpected_fields",
            "fields": unexpected,
        }
    return derive_slice_recovery_route(data)


def validate_writer_evidence(job_id: Any, repo_name: str, repo: Path, branch: str) -> dict[str, Any] | None:
    job = read_job(job_id) if isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id) else None
    receipt = job.get("receipt") if isinstance(job, dict) else None
    identity = job.get("session_identity") if isinstance(job, dict) else None
    parsed, identity_err = parse_session_identity(identity)
    profile = trusted_agent_profile(job) if isinstance(job, dict) else None
    if not (
        isinstance(job, dict) and is_agent_job_record(job) and job.get("status") == "completed"
        and job.get("repo") == repo_name and job.get("cwd") == str(repo.resolve())
        and isinstance(receipt, dict) and receipt.get("status") == "ok" and receipt.get("job_id") == job_id
        and receipt.get("failure_class") == "none" and receipt.get("failure_code") == "none" and receipt.get("failover_eligible") is False
        and isinstance(identity, dict) and not identity_err and parsed == identity and identity.get("native_session_id") and identity.get("repo") == repo_name
        and identity.get("branch") == branch and receipt.get("session_identity") == identity
        and profile is not None
        and all(receipt.get(key) == job.get(key) for key in AGENT_PROFILE_FIELDS)
    ):
        return {"error": "writer/session job is incomplete or mismatched", "code": "writer_evidence_mismatch"}
    return None


def validate_merge_test(data: dict[str, Any], repo_name: str, repo: Path, number: str, live: dict[str, Any]) -> dict[str, Any] | None:
    job_id = data.get("evidence_job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return {"error": "valid evidence_job_id is required", "code": "bad_test_evidence_id"}
    path = job_path(job_id)
    if not path.is_file():
        return {"error": "persisted final test evidence is missing", "code": "missing_test_evidence"}
    job = read_job(job_id)
    if not isinstance(job, dict):
        return {"error": "persisted final test evidence is corrupt", "code": "corrupt_test_evidence"}
    evidence, receipt = job.get("merge_evidence"), job.get("receipt")
    if not isinstance(evidence, dict) or evidence.get("version") != PR_EVIDENCE_VERSION:
        return {"error": "legacy or unstamped test job cannot authorize merge", "code": "legacy_test_evidence"}
    if job.get("kind") != "test":
        return {"error": "evidence job is not a named test", "code": "wrong_test_evidence_kind"}
    if job.get("status") in {"queued", "running"}:
        return {"error": "final test evidence is not terminal", "code": "test_evidence_running"}
    if job.get("status") != "completed" or job.get("returncode") != 0 or not isinstance(receipt, dict) or receipt.get("status") != "ok" or receipt.get("job_id") != job_id:
        return {"error": "final named test did not pass", "code": "test_evidence_failed"}
    head, branch, recipe = live.get("headRefOid"), live.get("headRefName"), data.get("test")
    expected = {"branch": branch, "sha": head, "clean": True}
    if not (
        isinstance(recipe, str) and recipe and job.get("script") == recipe == evidence.get("recipe")
        and job.get("repo") == repo_name and job.get("cwd") == str(repo.resolve()) == evidence.get("cwd") and evidence.get("repo") == repo_name
        and evidence.get("pr") == number and evidence.get("run_id") == str(data.get("run_id") or "")
        and evidence.get("expected_head") == head == evidence.get("live_head")
        and evidence.get("head_ref") == branch and evidence.get("start") == expected and evidence.get("final") == expected
    ):
        code = "head_moved" if head not in {evidence.get("expected_head"), evidence.get("live_head")} else "test_evidence_mismatch"
        return {"error": "final test evidence does not match this PR/run/head", "code": code}
    mode = evidence.get("writer_evidence_mode")
    if mode not in {"initial_green", "writer"}:
        return {"error": "merge evidence has no valid writer evidence mode", "code": "writer_evidence_mode_invalid"}
    has_writer = "writer_job_id" in evidence
    if (mode == "writer") != has_writer:
        return {"error": "merge evidence writer mode and ID disagree", "code": "writer_evidence_mode_invalid"}
    if mode == "writer":
        return validate_writer_evidence(evidence.get("writer_job_id"), repo_name, repo, branch)
    return None


@repo_write_locked
def gh_pr_merge(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Merge once, from strict live state and one persisted final test job."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed_fields = {"repo", "number", "auto_merge", "run_id", "test", "evidence_job_id", "merge_method", "codeoff_experiment_id", "final_verification_hash"}
    unexpected = sorted(set(data) - allowed_fields)
    if unexpected:
        return 400, {"error": "request contains unsupported fields", "code": "unexpected_fields", "fields": unexpected}
    repo_name = str(data.get("repo") or "").strip()
    repo_path = repos.get(repo_name)
    if not repo_path:
        return 400, {"error": f"unknown repo '{repo_name}'", "code": "unknown_repo",
                     "allowed": sorted(repos)}
    number = str(data.get("number") or "").strip()
    if not number.isdigit():
        return 400, {"error": "number is required", "code": "missing_number"}
    # Not `method`: the connector reads a field of that name as the HTTP verb,
    # so the graph's method: "squash" made Rewst issue `SQUASH /v1/gh/pr/merge`.
    # Same trap as `path` being read as the URL path.
    how = str(data.get("merge_method") or "squash").strip()
    if how not in {"squash", "merge", "rebase"}:
        return 400, {"error": "method must be squash, merge, or rebase", "code": "bad_method"}
    experiment_pair = (data.get("codeoff_experiment_id") not in (None, ""), data.get("final_verification_hash") not in (None, ""))
    if experiment_pair[0] != experiment_pair[1]:
        return 400, {"error": "both code-off evidence fields are required", "code": "codeoff_gate_incomplete"}
    auto_merge = data.get("auto_merge") is True or str(data.get("auto_merge") or "").lower() == "true"
    allowed, why = pr_merge_allowed({}, auto_merge=auto_merge, run_id=str(data.get("run_id") or ""))
    if not allowed and why and why["code"] in {"auto_merge_not_requested", "no_run_id"}:
        return 409, {"ok": False, "merged": False, "repo": repo_name, "number": number, **why}

    repo = Path(repo_path)
    codeoff_err = _codeoff_git_gate(data, repo_name, repo, "push") if experiment_pair[0] else None
    if codeoff_err:
        return 409, codeoff_err
    view = fresh_pr_view(repo, number)
    if not view.get("ok"):
        return int(view.get("status", 400)), view
    vd = view.get("data") or {}
    checks = annotate_pr_checks(gh_json(repo, ["pr", "checks", number, "--json",
        "name,state,bucket,link"]))
    if not checks.get("ok"):
        return int(checks.get("status", 502)), checks
    confirmed = fresh_pr_view(repo, number)
    if not confirmed.get("ok"):
        return int(confirmed.get("status", 502)), confirmed
    if (vd.get("headRefName"), vd.get("headRefOid")) != ((confirmed.get("data") or {}).get("headRefName"), (confirmed.get("data") or {}).get("headRefOid")):
        return 409, {"ok": False, "merged": False, "repo": repo_name, "number": number, "error": "PR head moved during final verification", "code": "head_moved"}
    view, vd = confirmed, confirmed.get("data") or {}
    state = {
        "all_green": bool(checks.get("all_green")),
        "mergeable": vd.get("mergeable"),
        "is_draft": vd.get("isDraft"), "holds": view.get("holds"), "remote_state": view.get("remote_state"),
        "checks_state": checks.get("checks_state"), "review_decision": view.get("review_decision"),
        "head_ref": vd.get("headRefName"), "head": vd.get("headRefOid"),
    }
    if str(vd.get("number")) != number:
        why = {"error": "fresh PR number is inconsistent", "code": "pr_state_mismatch"}
    elif view.get("remote_state") != "ready":
        why = {"error": "fresh PR state is not remotely ready", "code": str(view.get("remote_state") or "malformed")}
    elif checks.get("checks_state") != "green":
        why = {"error": "declared GitHub checks are not terminal passing", "code": str(checks.get("checks_state") or "malformed_checks")}
    else:
        why = validate_merge_test(data, repo_name, repo, number, vd)
    if why:
        return 409, {"ok": False, "merged": False, "repo": repo_name, "number": number,
                     "state": state, **why}
    head = str(vd["headRefOid"])
    out = gh_text(repo, ["pr", "merge", number, "--" + how, "--delete-branch", "--match-head-commit", head])
    moved = not out.get("ok") and "head" in (str(out.get("error") or "") + str(out.get("stderr") or "")).lower()
    return (200 if out.get("ok") else 409), {
        "ok": bool(out.get("ok")), "merged": bool(out.get("ok")), "repo": repo_name,
        "number": number, "method": how, "state": state,
        "error": out.get("error"), "code": "head_moved" if moved else (None if out.get("ok") else "merge_refused"),
        "retryable": False if not out.get("ok") else None,
    }


def pr_merge_allowed(
    state: dict[str, Any], auto_merge: bool, run_id: str = ""
) -> tuple[bool, dict[str, Any] | None]:
    """Decide whether a run may merge this PR.

    Off unless the run explicitly asked. The operator lock puts merge on the
    engineer; this exists only for the runs where they said auto-merge, and
    absence of that flag is a refusal rather than a default.

    Every condition is re-checked here against freshly read PR state instead
    of trusting whatever the graph concluded upstream. A node deciding "green"
    on its own is exactly what let a queue receipt pass as a test result.
    """
    if not auto_merge:
        return False, {"error": "auto_merge was not requested for this run",
                       "code": "auto_merge_not_requested"}
    if not str(run_id or "").strip():
        # auto_merge alone only stops a graph merging by accident. Any holder
        # of the API key could still set the flag by hand, which is how
        # riftwing#3523 got merged from a shell. Merge belongs to a run.
        return False, {"error": "merge is only reachable from inside a run; run_id is required",
                       "code": "no_run_id"}
    if state.get("is_draft"):
        return False, {"error": "pull request is a draft", "code": "is_draft"}
    if not state.get("all_green"):
        return False, {"error": "checks are not all green", "code": "not_green"}
    if str(state.get("mergeable") or "").upper() != "MERGEABLE":
        return False, {"error": f"pull request is {state.get('mergeable')}", "code": "not_mergeable"}
    holds = state.get("holds") or []
    if holds:
        return False, {"error": f"blocking labels present: {', '.join(holds)}", "code": "held"}
    return True, None


FINDINGS_MARKER = "<!-- engineering-findings-json"
FINDINGS_SEVERITIES = ("blocker", "critical", "major", "minor")


def pr_findings_from(labels: list[str], comment_bodies: list[str]) -> dict[str, Any]:
    """Turn a PR's labels and review comments into a fix brief.

    pr-drive used to take its instructions from CTX.INPUT.prompt, so getting a
    PR green started with a human reading the review. Both reviewers already
    publish a machine-readable block; parse that and skip the prose.

    Two reviewers routinely raise the same defect, so findings are deduped by
    fingerprint: that is one thing to fix, not two. A malformed block is an
    error rather than an empty list, because "nothing found" and "could not
    read what was found" must not look alike to the walker.
    """
    grade = next((l[len("grade-"):] for l in labels if l.startswith("grade-")), None)
    holds = sorted(l for l in labels if l.startswith("hold:"))

    findings: dict[str, dict[str, Any]] = {}
    for body in comment_bodies:
        start = 0
        while True:
            i = body.find(FINDINGS_MARKER, start)
            if i < 0:
                break
            end = body.find("-->", i)
            if end < 0:
                return {"ok": False, "blocking": True, "code": "unparsable_findings",
                        "error": "findings marker is not closed", "grade": grade, "holds": holds}
            raw = body[i + len(FINDINGS_MARKER):end]
            start = end + 3
            try:
                block = json.loads(raw)
            except ValueError as exc:
                return {"ok": False, "blocking": True, "code": "unparsable_findings",
                        "error": f"findings block is not JSON: {exc}", "grade": grade, "holds": holds}
            for f in block.get("findings") or []:
                fp = str(f.get("fingerprint") or "")
                if fp and fp not in findings:
                    findings[fp] = f

    ordered = sorted(
        findings.values(),
        key=lambda f: (
            FINDINGS_SEVERITIES.index(f.get("severity"))
            if f.get("severity") in FINDINGS_SEVERITIES
            else len(FINDINGS_SEVERITIES),
            str(f.get("fingerprint")),
        ),
    )
    counts = {s: sum(1 for f in ordered if f.get("severity") == s) for s in FINDINGS_SEVERITIES}
    return {
        "ok": True,
        "grade": grade,
        "holds": holds,
        # "Any finding at all" never converges: at grade A the reviewer still
        # lists nits, so a loop gated on that spends writer sessions forever.
        # review-hold.yml clears hold:pm-review at A/A-, so that is the real
        # merge bar. Findings on an A-graded PR stay in the payload as
        # advisory. No grade means the audit has not spoken yet, which is not
        # the same as clear.
        "blocking": bool(holds) or grade is None or grade.rstrip("+-") != "A",
        "findings": ordered,
        "brief": render_findings_brief(ordered),
        **counts,
    }


def render_findings_brief(findings: list[dict[str, Any]]) -> str:
    """The text the writer sees. Remedies only; reviewer reasoning stays out."""
    if not findings:
        return "No blocking findings."
    lines = ["Fix every finding below. Do not restructure anything else.", ""]
    for i, f in enumerate(findings, 1):
        loc = f.get("location") or {}
        where = str(loc.get("path") or "?")
        if loc.get("line"):
            where += ":%s" % loc["line"]
        lines.append("%d. [%s/%s] %s" % (i, f.get("severity", "?"), f.get("category", "?"),
                                         f.get("fingerprint", "?")))
        lines.append("   where:  %s" % where)
        lines.append("   remedy: %s" % (f.get("remedy") or "(none given)"))
        lines.append("")
    return "\n".join(lines).rstrip()


def review_said_nothing(text: str) -> bool:
    """True when the reviewer never emitted a verdict.

    A reviewer that ran out of turns, lost its connection, or died is not a
    reviewer that said no. The lock treats "timeout, no verdict" as a retry,
    and only a finished chain that still nacks as a strike. Callers get this
    as `no_verdict` so a provider blip is diagnosable in the trace instead of
    looking like an opinion.
    """
    upper = (text or "").upper()
    if any(t in upper for t in ("VERDICT: PASS", "VERDICT:PASS", '"VERDICT": "PASS"')):
        return False
    if any(t in upper for t in ("VERDICT: NACK", "VERDICT: REQUEST_CHANGES", "VERDICT: FAIL")):
        return False
    return parse_receipt_text(text or "") is None


def parse_review_verdict(text: str) -> tuple[str, str]:
    raw = text or ""
    upper = raw.upper()
    if "VERDICT: PASS" in upper or '"VERDICT": "PASS"' in upper or "VERDICT:PASS" in upper:
        verdict = "PASS"
    elif "VERDICT: NACK" in upper or "VERDICT: REQUEST_CHANGES" in upper or "VERDICT: FAIL" in upper:
        verdict = "NACK"
    else:
        parsed = parse_receipt_text(raw)
        if parsed and str(parsed.get("verdict") or parsed.get("status") or "").upper() in {"PASS", "OK"}:
            verdict = "PASS"
        elif parsed:
            verdict = "NACK"
        else:
            verdict = "NACK"
    summary = raw.strip().splitlines()
    line = next((ln.strip() for ln in summary if ln.strip()), "no review output")
    return verdict, line[:500]


def complete_review_diff(resolved: Path) -> tuple[str, str | None]:
    """Return the complete review diff, or a closed failure code; never a prefix."""
    result = git_diff(resolved, "HEAD", None)
    diff_text = result.get("diff")
    if not result.get("ok") or not isinstance(diff_text, str):
        return "", "git_state_unavailable"
    if result.get("truncated") or len(diff_text.encode()) > REVIEW_MAX_DIFF_BYTES:
        return "", "review_diff_too_large"
    return diff_text, None


def review_diff_rejection(code: str) -> dict[str, str]:
    """Author the one sanitized acceptance error for an unreviewable diff."""
    if code == "review_diff_too_large":
        return {
            "error": (
                "review diff exceeds the reviewable maximum of "
                f"{REVIEW_MAX_DIFF_BYTES} bytes"
            ),
            "code": "review_diff_too_large",
        }
    return {
        "error": "review repository identity is unavailable",
        "code": "git_state_unavailable",
    }


def native_review_result(context: NativeReviewExecutionContext) -> dict[str, Any]:
    """Run one read-only review through a proven direct native launcher."""
    usage_started = time.monotonic()
    if not review_runtime_matches_identity(context):
        return {
            "ok": False, "verdict": "NACK", "no_verdict": True,
            "error": "review execution identity changed before launch",
            "code": "review_execution_identity_mismatch",
        }
    launcher = context.launcher
    provider = context.provider
    model = context.model
    spec = NATIVE_LAUNCHERS.get(launcher)
    if spec is None:
        return {"ok": False, "verdict": "NACK", "no_verdict": True,
                "error": "review launcher must be codex, claude, or grok", "code": "bad_launcher"}
    if provider != spec["provider"] or model not in spec["models"]:
        return {"ok": False, "verdict": "NACK", "no_verdict": True,
                "error": f"invalid provider/model for {launcher} reviewer", "code": "bad_model_identity"}
    effort_profile, effort_error = normalize_native_review_effort(
        launcher, provider, model, context.requested_effort, context.effort_source,
        context.execution_identity,
    )
    if effort_error is not None or effort_profile is None:
        return {
            "ok": False, "verdict": "NACK", "no_verdict": True,
            **(effort_error or {"error": "unsupported effort profile", "code": "unsupported_effort"}),
        }
    binary = context.pinned_launcher.path
    resolved = context.resolved
    prompt = context.prompt
    diff_text = context.diff_text
    run_budget_seconds = context.run_budget_seconds
    max_turns = context.max_turns
    job_id = context.job_id
    body_prompt = (
        "Spec-review only. Do not edit files, commit, or push.\n"
        "Return exactly:\nVERDICT: PASS\nor\nVERDICT: NACK\n"
        f"Ticket:\n{prompt[:8000]}\n\nDiff:\n{diff_text}\n"
    )
    receipt_prompt = body_prompt + (
        "\nFor this transport, put the verdict in summary and return exactly one JSON object: "
        '{"status":"ok","sha":null,"pr_url":null,"summary":"VERDICT: PASS"}'
        " or the same object with status error and summary VERDICT: NACK."
    )
    returncode = 1
    timed_out = False
    text = ""
    ephemeral = job_id is None
    error: str | None = None
    native_usage: Any = None
    adapter_job = {
        "launcher": launcher, "provider": provider, "model": model,
        **effort_profile, "max_turns": max_turns, "session_identity": None,
    }
    if launcher == "grok":
        run_id = job_id or uuid.uuid4().hex
        path = job_dir(run_id)
        path.mkdir(parents=True, exist_ok=True)
        path.joinpath("prompt.txt").write_text(receipt_prompt)
        grok_job = read_job(run_id) or {}
        grok_job.update({
            "job_id": run_id,
            "kind": "review",
            "cwd": str(resolved),
            "launcher": launcher,
            "provider": provider,
            "model": model,
            **effort_profile,
            "run_budget_seconds": run_budget_seconds,
            "session_identity": None,
        })
        returncode, timed_out, _session_id, error = run_grok_acp(
            grok_job, binary, mode="review"
        )
        native_usage = grok_job.pop("_native_usage", None)
        text = read_bounded_output(path / "last-message.txt")
    else:
        run_id = job_id or uuid.uuid4().hex
        path = job_dir(run_id)
        path.mkdir(parents=True, exist_ok=True)
        if launcher == "codex":
            cmd = codex_command(
                adapter_job, path / "prompt.txt", str(resolved), binary, sandbox="read-only"
            )
            run_input = receipt_prompt.encode()
        else:
            # The prompt carries the complete diff, which can exceed the Linux
            # single-argument limit, so it goes to stdin exactly as Codex's does.
            cmd = claude_command(
                adapter_job, None, str(resolved), binary,
                permission_mode="plan", output_format="json",
            )
            run_input = body_prompt.encode()
        try:
            proc = subprocess.run(
                cmd, cwd=str(resolved), env={k: v for k, v in os.environ.items()},
                input=run_input, capture_output=True, timeout=run_budget_seconds,
                pass_fds=launcher_pass_fds(binary),
            )
            returncode = proc.returncode
            stdout = (proc.stdout or b"").decode("utf-8", "replace")
            native_usage = stdout
            text = read_bounded_output(path / "last-message.txt") or stdout
            if launcher == "codex":
                parsed = parse_receipt_text(text) or parse_receipt_text(stdout)
                text = str(parsed.get("summary") or "") if parsed else ""
        except subprocess.TimeoutExpired:
            timed_out, error = True, "review timed out"
        except OSError as exc:
            error = f"review spawn failed: {exc}"
    if launcher == "grok":
        parsed = parse_receipt_text(text)
        text = str(parsed.get("summary") or "") if parsed else ""
    verdict, summary = parse_review_verdict(text)
    no_verdict = bool(error) or review_said_nothing(text)
    if timed_out:
        code = "timeout"
    elif error:
        code = "review_failed"
    else:
        code = None
    result = {
        "ok": verdict == "PASS" and returncode == 0 and not no_verdict,
        "verdict": verdict,
        "no_verdict": no_verdict,
        "launcher": launcher,
        "provider": provider,
        "model": model,
        **effort_profile,
        "reviewer": model,
        "summary": error or summary,
        "compact": compact_cmd_signal({"ok": verdict == "PASS", "stdout": text, "stderr": error or ""}),
        "returncode": returncode,
    }
    if code:
        result["code"] = code
    usage, usage_diagnostic = normalize_native_usage(
        launcher, native_usage, time.monotonic() - usage_started,
    )
    result.update({"usage": usage, "usage_diagnostic": usage_diagnostic})
    if ephemeral:
        shutil.rmtree(path, ignore_errors=True)
    return result


def review_receipt(
    job: dict[str, Any], result: dict[str, Any], *,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = identity or trusted_review_identity(job)
    code = result.get("code")
    no_verdict = bool(result.get("no_verdict"))
    verdict = result.get("verdict") if result.get("verdict") in ("PASS", "NACK") else None
    if code == "review_execution_identity_mismatch" or identity is None:
        status, verdict, no_verdict = "error", None, True
        code, summary = "review_execution_identity_mismatch", "review execution identity mismatch"
    elif code == "not_implemented":
        status, verdict, no_verdict = "error", None, True
        summary = "review launcher unavailable"
    elif code == "timeout":
        status, verdict, no_verdict = "error", None, True
        summary = "review timed out"
    elif code == "review_failed":
        status, verdict, no_verdict = "error", None, True
        summary = "review execution failed"
    elif result.get("ok") and verdict == "PASS" and not no_verdict:
        status, code, summary = "ok", None, "review passed"
    elif verdict == "NACK" and not no_verdict:
        status, code, summary = "nack", None, "review requested changes"
    else:
        status, code, summary = "error", "review_failed", "review returned no verdict"
        verdict, no_verdict = None, True
    usage = result.get("usage")
    usage_diagnostic = result.get("usage_diagnostic")
    if not (
        (valid_normalized_usage(usage) and usage_diagnostic is None)
        or (
            usage is None
            and usage_diagnostic in {
                "usage_not_reported", "usage_incomplete", "usage_malformed",
            }
        )
    ):
        usage, usage_diagnostic = None, "usage_not_reported"
    return {
        "ok": status == "ok",
        "status": status,
        "job_id": job["job_id"],
        "kind": "review",
        "verdict": verdict,
        "no_verdict": no_verdict,
        "execution_identity": identity,
        "summary": summary,
        "code": code,
        "usage": json.loads(json.dumps(usage)) if usage is not None else None,
        "usage_diagnostic": usage_diagnostic,
    }


def run_review_job(job_id: str) -> None:
    authority = claim_review_authority(job_id)
    if authority is None:
        # A duplicate worker leaves the worker that already claimed authority alone.
        if review_authority(job_id) is None:
            close_review_authority_unavailable(job_id)
        return
    try:
        snapshot = authority.snapshot()
        if snapshot is None:
            return
        job = review_mark_running(job_id, snapshot, authority)
        if job is None:
            return
        identity = trusted_review_identity(job)
        if identity is None:
            return
        if (
            authority.launcher.fingerprint != identity["launcher_version"]
            or not review_record_matches_running_authority(
                read_job(job_id), snapshot, authority
            )
        ):
            result = {
                "ok": False, "verdict": "NACK", "no_verdict": True,
                "code": "review_execution_identity_mismatch",
            }
        else:
            sealed_identity = snapshot.get("execution_identity")
            diff_text, diff_code = complete_review_diff(Path(snapshot["cwd"]))
            if not isinstance(sealed_identity, dict) or diff_code is not None:
                # An unreadable or oversized diff can no longer reproduce the exact
                # bytes sealed into this authority, so the worker never launches.
                result = {
                    "ok": False, "verdict": "NACK", "no_verdict": True,
                    "code": "review_execution_identity_mismatch",
                }
            else:
                result = native_review_result(NativeReviewExecutionContext(
                    authority_identity=json.loads(json.dumps(sealed_identity)),
                    execution_identity=identity,
                    repo=snapshot["repo"],
                    resolved=Path(snapshot["cwd"]),
                    prompt=snapshot["prompt"],
                    diff_text=diff_text,
                    launcher=snapshot["launcher"],
                    provider=snapshot["provider"],
                    model=snapshot["model"],
                    requested_effort=snapshot["requested_effort"],
                    effective_effort=snapshot["effective_effort"],
                    effort_source=snapshot["effort_source"],
                    route_execution_profile=sealed_identity.get("route_execution_profile"),
                    run_budget_seconds=snapshot["run_budget_seconds"],
                    max_turns=snapshot["max_turns"],
                    permission_profile=snapshot["permission_profile"],
                    pinned_launcher=authority.launcher,
                    job_id=snapshot["job_id"],
                ))
        receipt = review_receipt(job, result, identity=identity)
        terminal, installed = review_terminal_transition(job_id, snapshot, authority, receipt)
        if terminal is None:
            return
        if installed:
            hook = deliver_webhook(terminal, receipt)
            if hook is not None:
                review_set_webhook_result(job_id, terminal, hook)
        herdr_job_done(terminal)
    finally:
        release_review_authority(job_id)


def enqueue_review(job: dict[str, Any]) -> None:
    herdr_follow_job(job)
    thread = threading.Thread(
        target=run_review_job, args=(job["job_id"],), name=f"graphwing-review-{job['job_id'][:8]}", daemon=True
    )
    thread.start()


def review_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Run or queue one immutable, read-only native review."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if not set(data) <= REVIEW_REQUEST_FIELDS:
        return 400, {"error": "review request has unsupported fields", "code": "unexpected_fields"}
    launcher = str(data.get("launcher") or "").strip()
    native_spec = NATIVE_LAUNCHERS.get(launcher)
    if native_spec is None:
        return 400, {"error": "launcher must be codex, claude, or grok", "code": "bad_launcher"}
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    if provider != native_spec["provider"] or model not in native_spec["models"]:
        return 400, {"error": f"invalid provider/model for {launcher} reviewer", "code": "bad_model_identity"}
    requested_effort = data.get("effort", "default")
    route_profile: dict[str, str] | None = None
    if "route_execution_profile" in data:
        raw_route = data.get("route_execution_profile")
        role = raw_route.get("role") if isinstance(raw_route, dict) else None
        if role not in {"reviewer1", "reviewer2"}:
            return 400, {
                "error": "routed review role must be reviewer1 or reviewer2",
                "code": "bad_route_execution_profile",
            }
        route_profile, route_error = parse_route_execution_profile(
            raw_route, expected_role=str(role), launcher=launcher, provider=provider,
            model=model, effort=requested_effort,
        )
        if route_error is not None or route_profile is None:
            return 400, route_error or {
                "error": "invalid route execution profile", "code": "bad_route_execution_profile"
            }
        effort_source = "route"
        effort_profile, effort_error = normalize_effort(
            launcher, provider, model, requested_effort, effort_source
        )
    else:
        effort_source = "explicit" if "effort" in data else "launcher_default"
        effort_profile, effort_error = normalize_review_effort(
            launcher, provider, model, requested_effort, effort_source
        )
    if effort_error is not None or effort_profile is None:
        return 400, effort_error or {
            "error": "unsupported effort profile", "code": "unsupported_effort"
        }
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 400, {"error": "prompt is required", "code": "missing_prompt"}
    prompt = prompt.strip()
    name, resolved = repo_from_body(data, repos)
    if name is None:
        assert isinstance(resolved, dict)
        code = resolved.get("code")
        messages = {
            "missing_repo": "review repo is required",
            "bad_repo": "review repo must be an allowlisted short name",
            "no_repos": "no review repos are allowlisted",
            "unknown_repo": "review repo is not allowlisted",
            "repo_missing": "review repo is unavailable",
            "not_git": "review repo is not a git worktree",
        }
        safe_code = code if isinstance(code, str) and code in messages else "bad_repo"
        return 400, {"error": messages[safe_code], "code": safe_code}
    assert isinstance(resolved, Path)
    resolved = resolved.resolve()
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    async_requested = data.get("async", False)
    if not isinstance(async_requested, bool):
        return 400, {"error": "async must be a boolean", "code": "bad_async"}
    run_budget_seconds = data.get("run_budget_seconds", REVIEW_DEFAULT_BUDGET_SECONDS)
    if (
        type(run_budget_seconds) is not int
        or not REVIEW_MIN_BUDGET_SECONDS <= run_budget_seconds <= REVIEW_MAX_BUDGET_SECONDS
    ):
        return 400, {
            "error": (
                "run_budget_seconds must be an integer from "
                f"{REVIEW_MIN_BUDGET_SECONDS} through {REVIEW_MAX_BUDGET_SECONDS}"
            ),
            "code": "bad_run_budget",
        }
    max_turns = data.get("max_turns", REVIEW_MAX_TURNS)
    if (
        type(max_turns) is not int
        or not REVIEW_MIN_REQUEST_TURNS <= max_turns <= REVIEW_MAX_REQUEST_TURNS
    ):
        return 400, {
            "error": (
                "max_turns must be an integer from "
                f"{REVIEW_MIN_REQUEST_TURNS} through {REVIEW_MAX_REQUEST_TURNS}"
            ),
            "code": "bad_max_turns",
        }
    branch, starting_head, git_err = current_branch_head(resolved)
    if (
        git_err or not isinstance(branch, str) or not valid_branch(branch)
        or not isinstance(starting_head, str) or not GIT_SHA_RE.fullmatch(starting_head)
    ):
        return 409, {"error": "review repository identity is unavailable", "code": "git_state_unavailable"}
    diff_text, diff_code = complete_review_diff(resolved)
    if diff_code is not None:
        return 409, review_diff_rejection(diff_code)
    binary = resolve_launcher_binary_now(launcher)
    accepted_launcher: PinnedLauncher | None = None
    try:
        accepted_launcher = pin_launcher(binary)
    except FileNotFoundError:
        return 501, {
            "error": "review launcher identity is unavailable",
            "code": "not_implemented",
        }
    except OSError:
        return 409, {
            "error": "review launcher identity changed during acceptance",
            "code": "review_execution_identity_mismatch",
        }
    launcher_version = accepted_launcher.fingerprint
    identity = {
        "version": REVIEW_EXECUTION_VERSION,
        "kind": "review",
        "launcher": launcher,
        "provider": provider,
        "model": model,
        **effort_profile,
        "launcher_version": launcher_version,
        "repo": name,
        "branch": branch,
        "starting_head": starting_head,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
        "run_budget_seconds": run_budget_seconds,
        "max_turns": max_turns,
        "permission_profile": REVIEW_PERMISSION_PROFILES[launcher],
    }
    if route_profile is not None:
        identity["route_execution_profile"] = route_profile
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id, "kind": "review", "status": "queued",
        **{key: identity[key] for key in REVIEW_JOB_IDENTITY_FIELDS},
        "execution_identity": identity,
        "cwd": str(resolved), "prompt": prompt,
        "response_webhook_url": webhook_url,
        "response_webhook_token": webhook_token,
        "resume_url": webhook_url,
        "created_at": utcnow(), "started_at": None, "finished_at": None,
        "receipt": None, "error": None, "webhook": None,
    }
    if not webhook_url and not async_requested:
        try:
            result = native_review_result(NativeReviewExecutionContext(
                authority_identity=json.loads(json.dumps(identity)),
                execution_identity=identity,
                repo=name,
                resolved=resolved,
                prompt=prompt,
                diff_text=diff_text,
                launcher=launcher,
                provider=provider,
                model=model,
                requested_effort=effort_profile["requested_effort"],
                effective_effort=effort_profile["effective_effort"],
                effort_source=effort_profile["effort_source"],
                route_execution_profile=route_profile,
                run_budget_seconds=run_budget_seconds,
                max_turns=max_turns,
                permission_profile=REVIEW_PERMISSION_PROFILES[launcher],
                pinned_launcher=accepted_launcher,
            ))
        except OSError:
            result = {
                "ok": False, "verdict": "NACK", "no_verdict": True,
                "code": "review_execution_identity_mismatch",
            }
        finally:
            accepted_launcher.close()
            accepted_launcher = None
        receipt = review_receipt(job, result, identity=identity)
        if result.get("code") == "not_implemented":
            return 501, {
                "error": "review launcher identity is unavailable",
                "code": "not_implemented",
            }
        if result.get("code") == "timeout":
            return 504, receipt
        if result.get("code") == "review_execution_identity_mismatch":
            return 409, receipt
        return 200, receipt

    try:
        with JOB_LOCK:
            if active_job_count() >= AGENT_MAX_CONCURRENT:
                return 409, {"error": "too many in-flight review jobs", "code": "busy"}
            job_dir(job_id).mkdir(parents=True, exist_ok=True)
            snapshot = review_execution_snapshot(job)
            if snapshot is None:
                return 409, {"error": "review execution identity is invalid", "code": "review_execution_identity_mismatch"}
            snapshot_hash = write_review_snapshot(job_id, snapshot)
            job["review_snapshot_sha256"] = snapshot_hash
            register_review_authority(job_id, snapshot, accepted_launcher)
            accepted_launcher = None  # registry owns both sealed descriptors
            try:
                write_job(job)
                accepted = public_review_job(job)
                if (
                    set(accepted) != REVIEW_PUBLIC_JOB_FIELDS
                    or accepted.get("ok") is not True
                    or accepted.get("job_id") != job_id
                    or accepted.get("kind") != "review"
                    or accepted.get("status") != "queued"
                    or accepted.get("execution_identity") != identity
                    or accepted.get("created_at") != job["created_at"]
                    or accepted.get("started_at") is not None
                    or accepted.get("finished_at") is not None
                    or accepted.get("receipt") is not None
                    or accepted.get("poll") != f"/v1/review/jobs/{job_id}"
                ):
                    release_review_authority(job_id)
                    return 409, {
                        "error": "review record is not canonical",
                        "code": "review_record_invalid",
                    }
                # Keep immutable canonical bytes across worker handoff. The worker may
                # reach terminal state and release queued authority before enqueue returns.
                accepted_bytes = _review_snapshot_bytes(accepted)
            except Exception:
                release_review_authority(job_id)
                raise
        try:
            enqueue_review(job)
        except Exception:
            release_review_authority(job_id)
            raise
        return 202, json.loads(accepted_bytes)
    finally:
        if accepted_launcher is not None:
            accepted_launcher.close()


def gh_json(path: Path, args: list[str]) -> dict[str, Any]:
    r = run_cmd([str(GH_BIN), *args], cwd=path)
    verdict_exit = args[:2] == ["pr", "checks"] and r.get("returncode") in (1, 8)
    if not r.get("ok") and not verdict_exit:
        return r
    try:
        parsed = json.loads(r["stdout"] or "null")
    except json.JSONDecodeError:
        return {"ok": False, "error": "gh returned non-JSON", "code": "gh_json", "status": 502}
    return {"ok": True, "data": parsed, "truncated": r["truncated"], "gh_exit": r.get("returncode", 0)}


def gh_text(path: Path, args: list[str]) -> dict[str, Any]:
    """Run gh where the output is prose. `gh pr merge` prints a sentence, and
    routing that through gh_json reported a successful merge as a failure."""
    return run_cmd([str(GH_BIN), *args], cwd=path)


PR_VIEW_FIELDS = "number,title,state,url,headRefName,headRefOid,baseRefName,mergeable,isDraft,reviewDecision,mergeStateStatus,labels"


def fresh_pr_view(path: Path, number: str) -> dict[str, Any]:
    return annotate_pr_view(gh_json(path, ["pr", "view", number, "--json", PR_VIEW_FIELDS]))


GH_CHECK_PASS = frozenset({"pass", "passing", "skipping", "skipped", "skip", "success", "neutral"})
GH_CHECK_FAIL = frozenset({"fail", "failure", "cancel", "cancelled", "cancelling", "error", "timed_out", "action_required", "startup_failure", "stale"})
GH_CHECK_PENDING = frozenset({"pending", "queued", "in_progress", "expected", "waiting", "requested"})


def annotate_pr_view(out: dict[str, Any]) -> dict[str, Any]:
    """Strictly classify the remote-only PR state for Graph switches."""
    if not out.get("ok"):
        return out
    data = out.get("data")
    data = data if isinstance(data, dict) else {}
    raw_review = data.get("reviewDecision")
    review_decision = str(raw_review or "").strip().upper() if raw_review is None or isinstance(raw_review, str) else "?"
    reviews_blocking = review_decision in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
    out["review_decision"] = review_decision
    out["merge_state"] = str(data.get("mergeStateStatus") or "")
    out["reviews_blocking"] = reviews_blocking
    out["reviews_ok"] = review_decision in {"", "APPROVED"}
    labels = data.get("labels")
    label_rows = labels if isinstance(labels, list) else []
    well_formed = (
        isinstance(out.get("data"), dict) and str(data.get("number") or "").isdigit()
        and data.get("state") in {"OPEN", "CLOSED", "MERGED"}
        and isinstance(data.get("isDraft"), bool) and valid_branch(data.get("headRefName"))
        and bool(GIT_SHA_RE.fullmatch(str(data.get("headRefOid") or "")))
        and data.get("mergeable") in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}
        and data.get("mergeStateStatus") in {"CLEAN", "BLOCKED", "BEHIND", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"}
        and review_decision in {"", "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
        and isinstance(labels, list) and all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels)
    )
    holds = sorted(label["name"] for label in label_rows if isinstance(label, dict) and label.get("name", "").startswith("hold:"))
    if review_decision not in {"", "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}: state = "unknown"
    elif not well_formed: state = "malformed"
    elif data["state"] != "OPEN": state = "closed"
    elif data["isDraft"]: state = "draft"
    elif holds: state = "held"
    elif reviews_blocking: state = "blocking_review"
    elif data["mergeable"] == "UNKNOWN" or data["mergeStateStatus"] == "UNKNOWN": state = "unknown"
    elif data["mergeable"] == "CONFLICTING" and data["mergeStateStatus"] == "DIRTY": state = "conflicting"
    elif data["mergeable"] == "MERGEABLE" and data["mergeStateStatus"] == "CLEAN": state = "ready"
    else: state = "inconsistent_merge_state"
    out.update({"holds": holds, "remote_state": state, "remote_reason": state, "remote_ready": state == "ready"})
    return out


def annotate_pr_checks(out: dict[str, Any]) -> dict[str, Any]:
    """Classify a nonempty, structurally known GitHub check set."""
    if not out.get("ok"):
        return out
    rows = out.get("data")
    failing: list[str] = []
    pending: list[str] = []
    unknown: list[str] = []
    valid_rows = rows if isinstance(rows, list) else []
    malformed = not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip() for row in valid_rows)
    inconsistent = False
    for row in valid_rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip(): continue
        name = str(row["name"]).strip()
        values = [str(row[key]).strip().lower().replace("-", "_") for key in ("bucket", "state") if row.get(key) not in (None, "")]
        kinds = [{**{x: "pass" for x in GH_CHECK_PASS}, **{x: "fail" for x in GH_CHECK_FAIL}, **{x: "pending" for x in GH_CHECK_PENDING}}.get(value) for value in values]
        if not values: malformed = True
        elif None in kinds: unknown.append(name)
        elif len(set(kinds)) != 1: inconsistent = True
        elif kinds[0] == "fail": failing.append(name)
        elif kinds[0] == "pending": pending.append(name)
    if malformed: state = "malformed"
    elif not rows: state = "no_checks"
    elif inconsistent: state = "inconsistent"
    elif unknown: state = "unknown"
    elif failing: state = "red"
    elif pending: state = "pending"
    else: state = "green"
    out["failing"] = failing
    out["pending"] = pending
    out["unknown"] = unknown
    out["any_red"] = bool(failing)
    out["all_green"] = state == "green"
    out["no_checks"] = state == "no_checks"
    out["checks_state"] = state
    return out


def units_status() -> dict[str, Any]:
    units = {}
    all_ok = True
    for name in UNITS:
        r = run_cmd(["systemctl", "--user", "is-active", name], timeout=5)
        state = (r.get("stdout") or r.get("error") or "unknown").strip()
        units[name] = {"active": r.get("ok") is True and state == "active", "state": state}
        if not units[name]["active"]:
            all_ok = False
    return {"ok": True, "healthy": all_ok, "units": units}


def podman_env() -> dict[str, str]:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    docker_host = os.environ.get("DOCKER_HOST") or f"unix://{runtime}/podman/podman.sock"
    return {
        **os.environ,
        "HOME": str(Path.home()),
        "XDG_RUNTIME_DIR": runtime,
        "DOCKER_HOST": docker_host,
        "GIT_TERMINAL_PROMPT": "0",
    }


def probe_loopback(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost"):
        return {
            "ok": False, "url": url, "error": "health url must be loopback http(s)",
            "diagnostic": compact_diagnostic("bad_health_config"),
        }
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=2) as resp:
            code = getattr(resp, "status", 200)
            ok = 200 <= code < 500
            return {
                "ok": ok, "url": url, "status": code,
                "diagnostic": compact_diagnostic("none" if ok else "health_bad_status"),
            }
    except Exception as exc:
        return {
            "ok": False, "url": url, "error": str(exc)[:160],
            "diagnostic": compact_diagnostic("health_unreachable"),
        }


def parse_compose_ps(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict):
        rows = [parsed]
    else:
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "name": row.get("Name") or row.get("Names") or row.get("Service"),
                "service": row.get("Service"),
                "state": row.get("State") or row.get("Status"),
                "health": row.get("Health"),
                "exit_code": row.get("ExitCode"),
            }
        )
    return out


def stack_status(name: str | None) -> dict[str, Any]:
    stacks, _ports = load_stacks()
    if not stacks:
        return {
            "ok": False, "error": "stacks.json is not configured", "code": "not_configured", "status": 501,
            "diagnostic": compact_diagnostic("not_configured"),
        }
    key = (name or "").strip()
    if not key:
        key = "riftwing" if "riftwing" in stacks else next(iter(stacks))
    if key not in stacks:
        return {
            "ok": False, "error": f"unknown stack '{key}'", "code": "unknown_stack",
            "allowed": sorted(stacks), "status": 400, "diagnostic": compact_diagnostic("unknown_stack"),
        }
    spec = stacks[key]
    cwd: Path = spec["cwd"]
    compose = spec["compose_file"]
    args = ["podman", "compose"]
    if compose:
        args.extend(["-f", compose])
    args.extend(["ps", "-a", "--format", "json"])
    r = run_cmd(args, cwd=cwd, timeout=20, env=podman_env())
    containers = parse_compose_ps(r.get("stdout") or "") if r.get("ok") else []
    health = [{"name": h["name"], **probe_loopback(h["url"])} for h in spec["health"]]
    health_ok = all(h.get("ok") for h in health) if health else bool(containers)
    running = [c for c in containers if str(c.get("state") or "").lower() in ("running", "up")]
    code = "none" if bool(running) and health_ok else str(r.get("code") or "stack_unhealthy")
    if r.get("ok") and not health_ok and health:
        diagnostic = next((h["diagnostic"] for h in health if not h.get("ok")), compact_diagnostic(code))
    else:
        diagnostic = compact_diagnostic(code)
    return {
        "ok": True,
        "stack": key,
        "cwd": str(cwd),
        "healthy": bool(running) and health_ok,
        "containers": containers,
        "health": health,
        "compose_ok": bool(r.get("ok")),
        "compose_error": None if r.get("ok") else (r.get("error") or "compose ps failed"),
        "diagnostic": diagnostic,
    }


def port_probe(port: int) -> dict[str, Any]:
    r = run_cmd(["ss", "-ltnH", f"sport = :{port}"], timeout=5)
    listening = bool(r.get("ok") and (r.get("stdout") or "").strip())
    code = "none" if listening else str(r.get("code") or "port_not_listening")
    return {"port": port, "listening": listening, "diagnostic": compact_diagnostic(code)}


def port_check(raw: str | None) -> dict[str, Any]:
    _stacks, allowed = load_stacks()
    if not allowed:
        return {
            "ok": False, "error": "stacks.json is not configured", "code": "not_configured", "status": 501,
            "diagnostic": compact_diagnostic("not_configured"),
        }
    if not raw or not raw.strip():
        return {
            "ok": False, "error": "port or ports is required", "code": "missing_port", "status": 400,
            "diagnostic": compact_diagnostic("missing_port"),
        }
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    ports: list[int] = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            return {
                "ok": False, "error": f"port must be an integer: {p}", "code": "bad_port", "status": 400,
                "diagnostic": compact_diagnostic("bad_port"),
            }
        if n not in allowed:
            return {
                "ok": False,
                "error": f"port {n} is not allowlisted",
                "code": "unknown_port",
                "allowed": sorted(allowed),
                "status": 400,
                "diagnostic": compact_diagnostic("unknown_port"),
            }
        ports.append(n)
    results = [port_probe(n) for n in ports]
    diagnostic = next((r["diagnostic"] for r in results if not r["listening"]), compact_diagnostic("none"))
    return {"ok": True, "ports": results, "diagnostic": diagnostic}


def herdr_agents() -> dict[str, Any]:
    r = run_cmd(["herdr", "--session", HERDR_SESSION, "agent", "list"], timeout=10)
    if not r.get("ok"):
        return r
    try:
        parsed = json.loads(r["stdout"] or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "herdr returned non-JSON", "code": "herdr_json", "status": 502}
    agents = (parsed.get("result") or {}).get("agents") or []
    return {"ok": True, "session": HERDR_SESSION, "agents": agents}


def herdr_enabled() -> bool:
    return os.environ.get("GRAPHWING_HERDR", "1").strip().lower() not in ("0", "false", "off", "no")


def herdr_cli(args: list[str], timeout: int = 10) -> dict[str, Any] | None:
    r = run_cmd(["herdr", "--session", HERDR_SESSION, *args], timeout=timeout)
    if not r.get("ok"):
        return None
    try:
        parsed = json.loads(r["stdout"] or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    result = parsed.get("result")
    return result if isinstance(result, dict) else parsed


def herdr_send(pane_id: str, text: str) -> None:
    run_cmd(["herdr", "--session", HERDR_SESSION, "pane", "send-text", pane_id, text], timeout=8)


def herdr_echo(pane_id: str, line: str) -> None:
    safe = "".join(c if (c.isprintable() and c != "'") else " " for c in line).strip()[:240]
    if not safe:
        return
    herdr_send(pane_id, f"printf '%s\\n' '{safe}'\n")


def herdr_report(pane_id: str, agent: str, state: str, message: str | None = None) -> None:
    args = ["pane", "report-agent", pane_id, "--source", HERDR_SOURCE, "--agent", agent, "--state", state]
    if message:
        args.extend(["--message", message[:120]])
    herdr_cli(args)


def herdr_ensure_graph_pane() -> str | None:
    listed = herdr_cli(["tab", "list"]) or {}
    tabs = listed.get("tabs") if isinstance(listed.get("tabs"), list) else []
    tab_id = None
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        label = str(tab.get("label") or "")
        if label in (HERDR_GRAPH_LABEL, "graphwing-probe"):
            tab_id = str(tab.get("tab_id") or "")
            if label != HERDR_GRAPH_LABEL and tab_id:
                herdr_cli(["tab", "rename", tab_id, HERDR_GRAPH_LABEL])
            break
    if not tab_id:
        created = herdr_cli(["tab", "create", "--label", HERDR_GRAPH_LABEL, "--no-focus", "--cwd", str(HOME)])
        if not created:
            return None
        root = created.get("root_pane") if isinstance(created.get("root_pane"), dict) else {}
        pane_id = str(root.get("pane_id") or "")
        if pane_id:
            herdr_report(pane_id, HERDR_GRAPH_LABEL, "idle", "graphwing")
            return pane_id
        tab_id = str((created.get("tab") or {}).get("tab_id") or "")
    if not tab_id:
        return None
    panes = herdr_cli(["pane", "list"]) or {}
    for pane in panes.get("panes") or []:
        if isinstance(pane, dict) and pane.get("tab_id") == tab_id:
            return str(pane.get("pane_id") or "") or None
    return None


def herdr_log(line: str) -> None:
    if not herdr_enabled():
        return
    with HERDR_LOCK:
        pane_id = herdr_ensure_graph_pane()
        if not pane_id:
            return
        herdr_echo(pane_id, f"{utcnow()} {line}")
        herdr_report(pane_id, HERDR_GRAPH_LABEL, "idle", line[:120])


def herdr_job_tab_label(job: dict[str, Any]) -> str:
    job_id = str(job.get("job_id") or "")
    tag = {"script": "s", "test": "t", "rr": "r"}.get(str(job.get("kind") or ""), "s" if job.get("script") else "a")
    return f"gw-{tag}-{job_id[:8]}"


def herdr_close_tab(tab_id: str) -> None:
    if tab_id:
        with HERDR_LINGER_LOCK:
            HERDR_LINGER.pop(tab_id, None)
        herdr_cli(["tab", "close", tab_id])


def herdr_tab_lingering(tab_id: str) -> bool:
    """True while a finished tab is inside its reading window."""
    with HERDR_LINGER_LOCK:
        deadline = HERDR_LINGER.get(tab_id)
    return bool(deadline and time.time() < deadline)


def herdr_close_tab_later(tab_id: str, delay: float) -> None:
    if not tab_id:
        return
    if delay <= 0:
        herdr_close_tab(tab_id)
        return
    with HERDR_LINGER_LOCK:
        HERDR_LINGER[tab_id] = time.time() + delay
    timer = threading.Timer(delay, herdr_close_tab, args=(tab_id,))
    timer.daemon = True
    timer.start()


def herdr_prune_job_tabs() -> None:
    listed = herdr_cli(["tab", "list"]) or {}
    tabs = [t for t in (listed.get("tabs") or []) if isinstance(t, dict) and str(t.get("label") or "").startswith("gw-")]
    live: list[dict[str, Any]] = []
    for tab in tabs:
        tid = str(tab.get("tab_id") or "")
        if str(tab.get("agent_status") or "") == "done":
            if not herdr_tab_lingering(tid):
                herdr_close_tab(tid)
                continue
        live.append(tab)
    extra = len(live) - HERDR_JOB_TAB_MAX
    if extra <= 0:
        return
    for tab in live[:extra]:
        herdr_close_tab(str(tab.get("tab_id") or ""))


def herdr_follow_job(job: dict[str, Any]) -> None:
    if not herdr_enabled():
        return
    job_id = str(job.get("job_id") or "")
    if not JOB_ID_RE.fullmatch(job_id):
        return
    label = herdr_job_tab_label(job)
    log_path = Path(job.get("log_ref") or (job_dir(job_id) / "stdout.log"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
    except OSError:
        return
    with HERDR_LOCK:
        herdr_prune_job_tabs()
        created = herdr_cli(
            ["tab", "create", "--label", label, "--no-focus", "--cwd", str(job.get("cwd") or HOME)]
        )
        if not created:
            return
        root = created.get("root_pane") if isinstance(created.get("root_pane"), dict) else {}
        pane_id = str(root.get("pane_id") or "")
        tab_id = str((created.get("tab") or {}).get("tab_id") or root.get("tab_id") or "")
        if not pane_id:
            return
        job["herdr_pane_id"] = pane_id
        job["herdr_tab_id"] = tab_id
        write_job(job)
        herdr_report(pane_id, label, "working", job.get("script") or job.get("launcher") or "job")
        herdr_send(pane_id, f"tail -F '{log_path}'\n")


def herdr_job_done(job: dict[str, Any]) -> None:
    if not herdr_enabled():
        return
    receipt = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
    summary = str(receipt.get("summary") or job.get("status") or "done")[:80]
    kind = job.get("kind") or job.get("script") or "agent"
    herdr_log(f"done {kind} {str(job.get('job_id') or '')[:8]} {job.get('status')} {summary}")
    tab_id = str(job.get("herdr_tab_id") or "")
    label = herdr_job_tab_label(job)
    if not tab_id:
        listed = herdr_cli(["tab", "list"]) or {}
        for tab in listed.get("tabs") or []:
            if isinstance(tab, dict) and str(tab.get("label") or "") == label:
                tab_id = str(tab.get("tab_id") or "")
                break
    herdr_close_tab_later(tab_id, HERDR_TAB_LINGER_SECONDS)


def summarize_op(path: str, status: int, payload: dict[str, Any] | bytes) -> str:
    bits = [path, str(status)]
    if not isinstance(payload, dict):
        return " ".join(bits)
    for key in ("repo", "name", "job_id", "branch", "code"):
        val = payload.get(key)
        if val not in (None, ""):
            bits.append(str(val)[:48])
    if payload.get("ok") is False and payload.get("error"):
        bits.append(str(payload["error"])[:60])
    rec = payload.get("receipt")
    if isinstance(rec, dict) and rec.get("summary"):
        bits.append(str(rec["summary"])[:60])
    return " ".join(bits)


_ANNOUNCE_SKIP_PREFIX = ("/v1/agent/jobs/", "/v1/review/jobs/")
_ANNOUNCE_SKIP = {
    "/v1/health",
    "/health",
    "/openapi.json",
    "/v1/openapi.json",
    "/v1/herdr/agents",
    "/v1/units/status",
    "/v1/watch",
}


def herdr_announce(method: str, path: str, status: int, payload: dict[str, Any] | bytes) -> None:
    if not herdr_enabled():
        return
    if status == 401 or path in _ANNOUNCE_SKIP or path.startswith(_ANNOUNCE_SKIP_PREFIX):
        return
    line = summarize_op(path, status, payload)

    def _run() -> None:
        try:
            herdr_log(f"{method} {line}")
        except Exception:
            return

    threading.Thread(target=_run, name="graphwing-herdr", daemon=True).start()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def job_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def job_record_lock(job_id: str) -> threading.RLock:
    """Serialize trusted daemon writers for one ephemeral job record."""
    key = str(job_path(job_id).resolve())
    with JOB_RECORD_LOCKS_LOCK:
        return JOB_RECORD_LOCKS.setdefault(key, threading.RLock())


def _read_job_unlocked(job_id: str) -> dict[str, Any] | None:
    path = job_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_job(job_id: str) -> dict[str, Any] | None:
    with job_record_lock(job_id):
        return _read_job_unlocked(job_id)


def _write_job_unlocked(job: dict[str, Any]) -> None:
    path = job_path(job["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(job, indent=2) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


JOB_TERMINAL_TRANSITION_LOCAL = threading.local()


def _job_terminal_transition_keys() -> set[str]:
    keys = getattr(JOB_TERMINAL_TRANSITION_LOCAL, "keys", None)
    if keys is None:
        keys = set()
        JOB_TERMINAL_TRANSITION_LOCAL.keys = keys
    return keys


def write_job(job: dict[str, Any]) -> bool:
    """Write ordinary records, never touch an existing review record.

    Review lifecycle and callback bookkeeping use narrow guarded APIs below.
    Returning False makes every ordinary write to an accepted review, including
    a value-equal idempotency attempt, an explicit no-op under the per-job lock
    that never invokes the atomic writer.
    """
    job_id = job["job_id"]
    key = str(job_path(job_id).resolve())
    if key in _job_terminal_transition_keys():
        return False
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if isinstance(current, dict) and current.get("kind") == "review":
            return False
        if (
            current != job and isinstance(current, dict)
            and current.get("kind") == "agent"
            and current.get("status") in ("completed", "failed")
        ):
            return False
        _write_job_unlocked(job)
        return True


def _review_snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()


class ReviewAuthority:
    """Process-local sealed launch and full running-record CAS authority."""

    def __init__(self, snapshot: dict[str, Any], launcher: PinnedLauncher):
        self.snapshot_bytes = _review_snapshot_bytes(snapshot)
        self.snapshot_hash = hashlib.sha256(self.snapshot_bytes).hexdigest()
        self.snapshot_fd = self._seal_bytes("graphwing-review-snapshot", self.snapshot_bytes)
        self.running_record_bytes: bytes | None = None
        self.running_record_hash: str | None = None
        self.running_record_fd = -1
        self.launcher = launcher
        self.claimed = False

    @staticmethod
    def _seal_bytes(name: str, raw: bytes) -> int:
        fd = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("could not seal review authority")
                view = view[written:]
            os.lseek(fd, 0, os.SEEK_SET)
            seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _read_sealed_record(fd: int, expected: bytes, digest: str) -> dict[str, Any] | None:
        try:
            raw = os.pread(fd, len(expected) + 1, 0)
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        if raw != expected or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
            return None
        return parsed if isinstance(parsed, dict) else None

    def snapshot(self) -> dict[str, Any] | None:
        return self._read_sealed_record(
            self.snapshot_fd, self.snapshot_bytes, self.snapshot_hash
        )

    def seal_running_record(self, record: dict[str, Any]) -> bool:
        """Bind the one exact canonical queued->running transition once."""
        if self.running_record_fd >= 0:
            return False
        raw = _review_snapshot_bytes(record)
        fd = self._seal_bytes("graphwing-review-running-record", raw)
        self.running_record_bytes = raw
        self.running_record_hash = hashlib.sha256(raw).hexdigest()
        self.running_record_fd = fd
        return True

    def running_record(self) -> dict[str, Any] | None:
        if (
            self.running_record_fd < 0 or self.running_record_bytes is None
            or self.running_record_hash is None
        ):
            return None
        return self._read_sealed_record(
            self.running_record_fd, self.running_record_bytes, self.running_record_hash
        )

    def close(self) -> None:
        for attr in ("snapshot_fd", "running_record_fd"):
            fd = getattr(self, attr)
            if fd >= 0:
                os.close(fd)
                setattr(self, attr, -1)
        self.launcher.close()


def register_review_authority(
    job_id: str, snapshot: dict[str, Any], launcher: PinnedLauncher
) -> ReviewAuthority:
    authority = ReviewAuthority(snapshot, launcher)
    with REVIEW_AUTHORITIES_LOCK:
        if job_id in REVIEW_AUTHORITIES:
            authority.close()
            raise RuntimeError("review authority already exists")
        REVIEW_AUTHORITIES[job_id] = authority
    return authority


def review_authority(job_id: str) -> ReviewAuthority | None:
    with REVIEW_AUTHORITIES_LOCK:
        authority = REVIEW_AUTHORITIES.get(job_id)
        return authority if isinstance(authority, ReviewAuthority) else None


def claim_review_authority(job_id: str) -> ReviewAuthority | None:
    with REVIEW_AUTHORITIES_LOCK:
        authority = REVIEW_AUTHORITIES.get(job_id)
        if not isinstance(authority, ReviewAuthority) or authority.claimed:
            return None
        authority.claimed = True
        return authority


def release_review_authority(job_id: str) -> None:
    with REVIEW_AUTHORITIES_LOCK:
        authority = REVIEW_AUTHORITIES.pop(job_id, None)
    if isinstance(authority, ReviewAuthority):
        authority.close()


def trusted_review_identity(job: Any) -> dict[str, Any] | None:
    """Validate a canonical review identity without classifying it as an agent."""
    if not isinstance(job, dict) or job.get("kind") != "review":
        return None
    identity = job.get("execution_identity")
    if not isinstance(identity, dict):
        return None
    source = identity.get("effort_source")
    expected_identity_fields = set(REVIEW_IDENTITY_FIELDS)
    if source == "route":
        expected_identity_fields.add("route_execution_profile")
    if set(identity) != expected_identity_fields:
        return None
    if (
        identity.get("version") != REVIEW_EXECUTION_VERSION
        or identity.get("kind") != "review"
        or job.get("job_id") is None
        or not isinstance(job.get("job_id"), str)
        or not JOB_ID_RE.fullmatch(job["job_id"])
        or job.get("status") not in ("queued", "running", "completed", "failed")
    ):
        return None
    launcher = identity.get("launcher")
    provider = identity.get("provider")
    model = identity.get("model")
    spec = NATIVE_LAUNCHERS.get(launcher) if isinstance(launcher, str) else None
    if source == "route":
        route_raw = identity.get("route_execution_profile")
        route_role = route_raw.get("role") if isinstance(route_raw, dict) else None
        if route_role not in {"reviewer1", "reviewer2"}:
            return None
        parsed_route, route_err = parse_route_execution_profile(
            route_raw, expected_role=str(route_role), launcher=str(launcher or ""),
            provider=str(provider or ""), model=str(model or ""),
            effort=identity.get("requested_effort"),
        )
        if route_err is not None or parsed_route != route_raw:
            return None
        normalized, effort_err = normalize_effort(
            str(launcher or ""), str(provider or ""), str(model or ""),
            identity.get("requested_effort"), "route",
        )
    else:
        normalized, effort_err = normalize_review_effort(
            str(launcher or ""), str(provider or ""), str(model or ""),
            identity.get("requested_effort"), str(source or ""),
        )
    if (
        spec is None or provider != spec["provider"] or model not in spec["models"]
        or effort_err or normalized is None
        or identity.get("effective_effort") != normalized["effective_effort"]
        or not isinstance(identity.get("launcher_version"), str)
        or not LAUNCHER_VERSION_RE.fullmatch(identity["launcher_version"])
        or not isinstance(identity.get("repo"), str)
        or not SESSION_REPO_RE.fullmatch(identity["repo"])
        or not isinstance(identity.get("branch"), str)
        or not valid_branch(identity["branch"])
        or not isinstance(identity.get("starting_head"), str)
        or not GIT_SHA_RE.fullmatch(identity["starting_head"])
        or not isinstance(identity.get("prompt_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", identity["prompt_sha256"])
        or not isinstance(identity.get("diff_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", identity["diff_sha256"])
        or type(identity.get("run_budget_seconds")) is not int
        or not REVIEW_MIN_BUDGET_SECONDS <= identity["run_budget_seconds"] <= REVIEW_MAX_BUDGET_SECONDS
        or type(identity.get("max_turns")) is not int
        or not REVIEW_MIN_REQUEST_TURNS <= identity["max_turns"] <= REVIEW_MAX_REQUEST_TURNS
        or identity.get("permission_profile") != REVIEW_PERMISSION_PROFILES[launcher]
        or any(job.get(key) != identity.get(key) for key in REVIEW_JOB_IDENTITY_FIELDS)
    ):
        return None
    prompt = job.get("prompt")
    if (
        not isinstance(prompt, str)
        or hashlib.sha256(prompt.encode()).hexdigest() != identity["prompt_sha256"]
        or not isinstance(job.get("cwd"), str)
    ):
        return None
    return dict(identity)


def review_execution_snapshot(job: Any) -> dict[str, Any] | None:
    identity = trusted_review_identity(job)
    if identity is None or not isinstance(job, dict):
        return None
    return {
        key: json.loads(json.dumps(job.get(key)))
        for key in (
            "job_id", "kind", *REVIEW_JOB_IDENTITY_FIELDS, "execution_identity",
            "cwd", "prompt", "response_webhook_url", "response_webhook_token",
            "resume_url", "created_at",
        )
    }


def write_review_snapshot(job_id: str, snapshot: dict[str, Any]) -> str:
    raw = _review_snapshot_bytes(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    path = job_dir(job_id) / "review-snapshot.json"
    if path.exists():
        if path.read_bytes() != raw:
            raise OSError("review snapshot already exists with different bytes")
        return digest
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("could not write review snapshot")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return digest


def read_review_snapshot(job_id: str) -> dict[str, Any] | None:
    path = job_dir(job_id) / "review-snapshot.json"
    try:
        raw = path.read_bytes()
        snapshot = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("job_id") != job_id
        or snapshot.get("kind") != "review"
        or _review_snapshot_bytes(snapshot) != raw
    ):
        return None
    return snapshot


def canonical_review_record(job: Any, allowed_statuses: tuple[str, ...]) -> bool:
    """Validate one exact closed persisted review state without disk authority."""
    if (
        not isinstance(job, dict) or set(job) != REVIEW_JOB_FIELDS
        or job.get("status") not in allowed_statuses
        or trusted_review_identity(job) is None
        or not valid_review_timestamps(job)
        or not valid_review_operational_fields(job)
    ):
        return False
    return bool(
        job.get("status") not in ("completed", "failed")
        or sanitized_review_receipt(job) is not None
    )


def review_record_matches_snapshot(job: Any, snapshot: dict[str, Any]) -> bool:
    if not canonical_review_record(job, ("queued", "running", "completed", "failed")):
        return False
    expected_hash = hashlib.sha256(_review_snapshot_bytes(snapshot)).hexdigest()
    return bool(
        hmac.compare_digest(str(job.get("review_snapshot_sha256") or ""), expected_hash)
        and review_execution_snapshot(job) == snapshot
    )


def review_record_matches_authority(
    job: Any, snapshot: dict[str, Any], authority: ReviewAuthority
) -> bool:
    return bool(
        authority.snapshot() == snapshot
        and hmac.compare_digest(
            authority.snapshot_hash,
            hashlib.sha256(_review_snapshot_bytes(snapshot)).hexdigest(),
        )
        and review_record_matches_snapshot(job, snapshot)
    )


def review_record_matches_running_authority(
    job: Any, snapshot: dict[str, Any], authority: ReviewAuthority
) -> bool:
    """Compare the complete canonical running value to sealed CAS bytes."""
    sealed = authority.running_record()
    if (
        sealed is None or not canonical_review_record(job, ("running",))
        or not review_record_matches_authority(job, snapshot, authority)
        or authority.running_record_bytes is None or authority.running_record_hash is None
    ):
        return False
    raw = _review_snapshot_bytes(job)
    return bool(
        raw == authority.running_record_bytes
        and hmac.compare_digest(hashlib.sha256(raw).hexdigest(), authority.running_record_hash)
        and job == sealed
    )


def review_mark_running(
    job_id: str, snapshot: dict[str, Any], authority: ReviewAuthority
) -> dict[str, Any] | None:
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if (
            not review_record_matches_authority(current, snapshot, authority)
            or current.get("status") != "queued"
        ):
            return None
        running = json.loads(json.dumps(current))
        running.update({
            "status": "running", "started_at": utcnow(), "finished_at": None,
            "receipt": None, "error": None, "webhook": None,
        })
        if (
            not canonical_review_record(running, ("running",))
            or not authority.seal_running_record(running)
            or not review_record_matches_running_authority(running, snapshot, authority)
        ):
            return None
        _write_job_unlocked(running)
        return running


def close_review_authority_unavailable(job_id: str) -> dict[str, Any] | None:
    """Close a restarted review without trusting or callbacking mutable identity."""
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if not (
            isinstance(current, dict) and current.get("job_id") == job_id
            and current.get("kind") == "review"
            and current.get("status") in ("queued", "running")
        ):
            return None
        current.update({
            "status": "failed", "finished_at": utcnow(), "receipt": None,
            "error": "review process authority unavailable after restart",
            "review_recovery_code": "review_authority_unavailable",
            "returncode": None,
        })
        current.pop("pid", None)
        _write_job_unlocked(current)
        return current


def review_terminal_transition(
    job_id: str, snapshot: dict[str, Any], authority: ReviewAuthority,
    receipt: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    """CAS the exact sealed running value to one canonical terminal value."""
    with job_record_lock(job_id):
        key = str(job_path(job_id).resolve())
        transition_keys = _job_terminal_transition_keys()
        transition_keys.add(key)
        try:
            persisted = _read_job_unlocked(job_id)
            if (
                not isinstance(persisted, dict)
                or not review_record_matches_running_authority(persisted, snapshot, authority)
                or receipt.get("execution_identity") != persisted.get("execution_identity")
            ):
                return None, False
            terminal = json.loads(json.dumps(persisted))
            terminal.update({
                "status": (
                    "failed" if receipt.get("code") == "review_execution_identity_mismatch"
                    else "completed"
                ),
                "finished_at": utcnow(), "receipt": json.loads(json.dumps(receipt)),
                "error": None, "webhook": None,
            })
            if not canonical_review_record(terminal, ("completed", "failed")):
                return None, False
            _write_job_unlocked(terminal)
            seal_terminal_receipt_authority("review", receipt)
            return terminal, True
        except (RecursionError, MemoryError):
            return None, False
        finally:
            transition_keys.discard(key)


def review_set_webhook_result(
    job_id: str, terminal_revision: dict[str, Any], hook: dict[str, Any]
) -> bool:
    """Apply only webhook bookkeeping to the exact committed terminal revision."""
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if (
            not isinstance(current, dict)
            or not canonical_review_record(terminal_revision, ("completed", "failed"))
            or terminal_revision.get("webhook") is not None
            or current != terminal_revision
            or _review_snapshot_bytes(current) != _review_snapshot_bytes(terminal_revision)
        ):
            return False
        status = hook.get("status") if isinstance(hook, dict) else None
        webhook = (
            {"ok": hook["ok"], "status": status}
            if isinstance(hook, dict) and type(hook.get("ok")) is bool
            and type(status) is int and 100 <= status <= 599
            else {"ok": False}
        )
        updated = json.loads(json.dumps(terminal_revision))
        updated["webhook"] = webhook
        if not canonical_review_record(updated, ("completed", "failed")):
            return False
        _write_job_unlocked(updated)
        return True


REVIEW_RECEIPT_FIELDS = frozenset({
    "ok", "status", "job_id", "kind", "verdict", "no_verdict",
    "execution_identity", "summary", "code", "usage", "usage_diagnostic",
})
REVIEW_RECEIPT_STATES = {
    ("ok", "PASS", False, None, "review passed", True),
    ("nack", "NACK", False, None, "review requested changes", False),
    ("error", None, True, "timeout", "review timed out", False),
    ("error", None, True, "not_implemented", "review launcher unavailable", False),
    ("error", None, True, "review_failed", "review execution failed", False),
    ("error", None, True, "review_failed", "review returned no verdict", False),
    (
        "error", None, True, "review_execution_identity_mismatch",
        "review execution identity mismatch", False,
    ),
}


def sanitized_review_receipt(job: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the exact closed receipt and return an allowlisted projection."""
    stored = job.get("receipt")
    identity = trusted_review_identity(job)
    if (
        not isinstance(stored, dict) or set(stored) != REVIEW_RECEIPT_FIELDS
        or identity is None or stored.get("execution_identity") != identity
        or stored.get("job_id") != job.get("job_id") or stored.get("kind") != "review"
        or type(stored.get("ok")) is not bool or type(stored.get("no_verdict")) is not bool
    ):
        return None
    state = (
        stored.get("status"), stored.get("verdict"), stored.get("no_verdict"),
        stored.get("code"), stored.get("summary"), stored.get("ok"),
    )
    if state not in REVIEW_RECEIPT_STATES:
        return None
    if (
        job.get("status") == "failed"
        and stored.get("code") != "review_execution_identity_mismatch"
    ) or (
        job.get("status") == "completed"
        and stored.get("code") == "review_execution_identity_mismatch"
    ):
        return None
    try:
        projected = {
            key: json.loads(json.dumps(stored[key]))
            for key in REVIEW_RECEIPT_FIELDS
            if key not in {"usage", "usage_diagnostic"}
        }
        usage, diagnostic = authorized_receipt_usage("review", stored)
        projected.update({"usage": usage, "usage_diagnostic": diagnostic})
        return projected
    except (RecursionError, MemoryError):
        return None


def valid_review_timestamps(job: Any) -> bool:
    """Accept only exact server-authored second-resolution UTC lifecycle stamps."""
    if not isinstance(job, dict):
        return False

    def valid(value: Any) -> bool:
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
        ):
            return False
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return False
        return True

    status = job.get("status")
    created = job.get("created_at")
    started = job.get("started_at")
    finished = job.get("finished_at")
    if not valid(created):
        return False
    if status == "queued":
        return started is None and finished is None
    if status == "running":
        return valid(started) and finished is None
    if status == "completed":
        return valid(started) and valid(finished)
    if status == "failed":
        return (started is None or valid(started)) and valid(finished)
    return False


def valid_review_operational_fields(job: Any) -> bool:
    """Validate mutable review lifecycle fields before any public projection."""
    if not isinstance(job, dict):
        return False
    status = job.get("status")
    receipt = job.get("receipt")
    error = job.get("error")
    webhook = job.get("webhook")
    if status in ("queued", "running"):
        return receipt is None and error is None and webhook is None
    if status not in ("completed", "failed") or not isinstance(receipt, dict):
        return False
    if error is not None and not (
        status == "failed" and error == "review execution identity mismatch"
    ):
        return False
    if webhook is None:
        return True
    if not isinstance(webhook, dict) or type(webhook.get("ok")) is not bool:
        return False
    if set(webhook) == {"ok"}:
        return webhook["ok"] is False
    return bool(
        set(webhook) == {"ok", "status"}
        and type(webhook.get("status")) is int
        and 100 <= webhook["status"] <= 599
    )


def is_review_job_record(job: Any) -> bool:
    if (
        not isinstance(job, dict) or job.get("kind") != "review"
        or set(job) != REVIEW_JOB_FIELDS
        or not valid_review_timestamps(job)
        or not valid_review_operational_fields(job)
    ):
        return False
    status = job.get("status")
    if status == "queued":
        authority = review_authority(str(job.get("job_id") or ""))
        snapshot = authority.snapshot() if authority is not None else None
        return bool(
            authority is not None and snapshot is not None
            and review_record_matches_authority(job, snapshot, authority)
        )
    if status == "running":
        authority = review_authority(str(job.get("job_id") or ""))
        snapshot = authority.snapshot() if authority is not None else None
        return bool(
            authority is not None and snapshot is not None
            and review_record_matches_running_authority(job, snapshot, authority)
        )
    if status in ("completed", "failed"):
        snapshot = read_review_snapshot(str(job.get("job_id") or ""))
        return bool(
            snapshot is not None
            and review_record_matches_snapshot(job, snapshot)
            and sanitized_review_receipt(job) is not None
        )
    return False


def public_review_job(job: dict[str, Any]) -> dict[str, Any]:
    if not is_review_job_record(job):
        return {"error": "review record is not canonical", "code": "review_record_invalid"}
    identity = trusted_review_identity(job)
    receipt = sanitized_review_receipt(job) if job.get("status") in ("completed", "failed") else None
    return {
        "ok": True,
        "job_id": job.get("job_id"),
        "kind": "review",
        "status": job.get("status"),
        "execution_identity": identity,
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "receipt": receipt,
        "poll": f"/v1/review/jobs/{job.get('job_id')}",
    }


def review_job_response(job_id: str) -> tuple[int, dict[str, Any]]:
    job = read_job(job_id)
    if not isinstance(job, dict) or job.get("kind") != "review":
        return 404, {"error": "review job not found", "code": "not_found"}
    if job.get("review_recovery_code") == "review_authority_unavailable":
        return 409, {
            "error": "review process authority is unavailable after restart",
            "code": "review_authority_unavailable",
        }
    try:
        canonical = is_review_job_record(job)
    except (RecursionError, MemoryError):
        canonical = False
    if not canonical:
        code = (
            "review_authority_unavailable"
            if job.get("status") in ("queued", "running") and review_authority(job_id) is None
            else "review_record_invalid"
        )
        return 409, {
            "error": (
                "review process authority is unavailable after restart"
                if code == "review_authority_unavailable"
                else "review record is not canonical"
            ),
            "code": code,
        }
    try:
        return 200, public_review_job(job)
    except (RecursionError, MemoryError):
        return 409, {
            "error": "review record is not canonical",
            "code": "review_record_invalid",
        }


def review_job_wait(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if set(data) != {"job_id", "timeout_seconds"}:
        return 400, {"error": "review wait requires only job_id and timeout_seconds", "code": "unexpected_fields"}
    job_id = data.get("job_id")
    timeout = data.get("timeout_seconds")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return 400, {"error": "invalid job_id", "code": "bad_job_id"}
    if isinstance(timeout, str):
        if not re.fullmatch(r"[0-9]{1,4}", timeout):
            return 400, {"error": "timeout_seconds must be 1..1800", "code": "bad_timeout"}
        timeout = int(timeout)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        return 400, {"error": "timeout_seconds must be 1..1800", "code": "bad_timeout"}
    deadline = time.monotonic() + timeout
    while True:
        status, payload = review_job_response(job_id)
        if status != 200:
            return status, payload
        if payload.get("status") not in ("queued", "running"):
            return 200, payload
        if time.monotonic() >= deadline:
            return 408, {
                "error": "review did not finish before the bounded wait",
                "code": "job_wait_timeout",
            }
        time.sleep(0.2)


def review_runtime_matches_identity(context: NativeReviewExecutionContext) -> bool:
    """Bind every claimed identity field to the exact invocation authority."""
    if not (
        isinstance(context, NativeReviewExecutionContext)
        and isinstance(context.execution_identity, dict)
        and isinstance(context.authority_identity, dict)
        and isinstance(context.repo, str)
        and isinstance(context.resolved, Path)
        and isinstance(context.prompt, str)
        and isinstance(context.diff_text, str)
        and len(context.diff_text.encode()) <= REVIEW_MAX_DIFF_BYTES
        and all(isinstance(value, str) for value in (
            context.launcher, context.provider, context.model,
            context.requested_effort, context.effective_effort,
            context.effort_source, context.permission_profile,
        ))
        and (
            context.route_execution_profile is None
            or isinstance(context.route_execution_profile, dict)
        )
        and type(context.run_budget_seconds) is int
        and type(context.max_turns) is int
        and (context.job_id is None or isinstance(context.job_id, str))
        and isinstance(context.pinned_launcher, PinnedLauncher)
        and context.pinned_launcher.fd >= 0
        and context.pinned_launcher.path
            == Path(f"/proc/self/fd/{context.pinned_launcher.fd}")
        and isinstance(context.pinned_launcher.fingerprint, str)
        and LAUNCHER_VERSION_RE.fullmatch(context.pinned_launcher.fingerprint)
    ):
        return False
    identity = context.execution_identity
    authority = context.authority_identity
    branch, head, err = current_branch_head(context.resolved)
    source = identity.get("effort_source")
    expected_fields = set(REVIEW_IDENTITY_FIELDS)
    if source == "route":
        expected_fields.add("route_execution_profile")
        route_raw = identity.get("route_execution_profile")
        route_role = route_raw.get("role") if isinstance(route_raw, dict) else None
        if route_role not in {"reviewer1", "reviewer2"}:
            return False
        parsed_route, route_err = parse_route_execution_profile(
            route_raw, expected_role=str(route_role),
            launcher=str(identity.get("launcher") or ""),
            provider=str(identity.get("provider") or ""),
            model=str(identity.get("model") or ""),
            effort=identity.get("requested_effort"),
        )
        if route_err is not None or parsed_route != route_raw:
            return False
        normalized, effort_err = normalize_effort(
            str(identity.get("launcher") or ""),
            str(identity.get("provider") or ""),
            str(identity.get("model") or ""),
            identity.get("requested_effort"), "route",
        )
    else:
        normalized, effort_err = normalize_review_effort(
            str(identity.get("launcher") or ""),
            str(identity.get("provider") or ""),
            str(identity.get("model") or ""),
            identity.get("requested_effort"), str(source or ""),
        )
    return bool(
        identity == authority
        and set(identity) == expected_fields
        and identity.get("version") == REVIEW_EXECUTION_VERSION
        and identity.get("kind") == "review"
        and identity.get("launcher") == context.launcher
        and identity.get("provider") == context.provider
        and identity.get("model") == context.model
        and identity.get("requested_effort") == context.requested_effort
        and identity.get("effective_effort") == context.effective_effort
        and identity.get("effort_source") == context.effort_source
        and identity.get("route_execution_profile") == context.route_execution_profile
        and identity.get("launcher_version") == context.pinned_launcher.fingerprint
        and launcher_version_fingerprint(context.pinned_launcher.path)
            == context.pinned_launcher.fingerprint
        and identity.get("repo") == context.repo
        and context.resolved == context.resolved.resolve()
        and identity.get("run_budget_seconds") == context.run_budget_seconds
        and identity.get("max_turns") == context.max_turns
        and identity.get("permission_profile") == context.permission_profile
        and context.permission_profile == REVIEW_PERMISSION_PROFILES.get(context.launcher)
        and effort_err is None and normalized is not None
        and identity.get("effective_effort") == normalized["effective_effort"]
        and err is None
        and branch == identity.get("branch")
        and head == identity.get("starting_head")
        and hashlib.sha256(context.prompt.encode()).hexdigest() == identity.get("prompt_sha256")
        and hashlib.sha256(context.diff_text.encode()).hexdigest() == identity.get("diff_sha256")
    )


AGENT_PROFILE_FIELDS = (
    "launcher", "provider", "model", "requested_effort", "effective_effort",
    "effort_source", "launcher_version",
)


def trusted_agent_profile(job: dict[str, Any]) -> dict[str, Any] | None:
    """Return a complete corroborated profile, never a best-effort projection."""
    if job.get("kind") != "agent":
        return None
    launcher, provider, model = (job.get(key) for key in ("launcher", "provider", "model"))
    source = job.get("effort_source")
    requested = job.get("requested_effort")
    if not (
        isinstance(launcher, str) and launcher in NATIVE_LAUNCHERS
        and isinstance(provider, str) and isinstance(model, str)
        and source in EFFORT_SOURCES and requested in EFFORT_VALUES
    ):
        return None
    if (launcher, provider, model) not in EFFORT_PROFILES:
        return None
    normalized, effort_err = normalize_effort(launcher, provider, model, requested, source)
    identity = job.get("session_identity")
    parsed, identity_err = parse_session_identity(identity)
    launcher_version = job.get("launcher_version")
    if (
        effort_err or normalized is None
        or job.get("effective_effort") != normalized["effective_effort"]
        or not isinstance(launcher_version, str) or not LAUNCHER_VERSION_RE.fullmatch(launcher_version)
        or identity_err or parsed != identity or not isinstance(identity, dict)
        or any(identity.get(key) != job.get(key) for key in AGENT_PROFILE_FIELDS)
        or job.get("repo") != identity.get("repo")
        or any(
            key in job and job.get(key) != identity.get(key)
            for key in ("branch", "starting_head")
        )
    ):
        return None
    return {key: job.get(key) for key in AGENT_PROFILE_FIELDS} | {"session_identity": identity}


def receipt_profile_matches_job(job: dict[str, Any], receipt: Any) -> bool:
    """Require one complete canonical profile on job, receipt, and identity."""
    profile = trusted_agent_profile(job)
    return bool(
        profile is not None and isinstance(receipt, dict)
        and receipt.get("job_id") == job.get("job_id")
        and receipt.get("session_identity") == profile["session_identity"]
        and all(receipt.get(key) == profile[key] for key in AGENT_PROFILE_FIELDS)
    )


def sanitized_agent_receipt(job: dict[str, Any]) -> dict[str, Any] | None:
    stored = job.get("receipt")
    if not isinstance(stored, dict):
        return None
    profile = trusted_agent_profile(job)
    receipt_profile_valid = receipt_profile_matches_job(job, stored)
    if not receipt_profile_valid:
        classified = classify_agent_failure("session_provenance_invalid")
        return {
            "status": "error", "job_id": job.get("job_id"),
            **{key: None for key in AGENT_PROFILE_FIELDS}, "session_identity": None,
            "summary": classified["diagnostic"]["summary"], **classified,
            "usage": None, "usage_diagnostic": "usage_authority_mismatch",
        }
    assert profile is not None
    status = stored.get("status") if stored.get("status") in ("ok", "error", "timeout") else "error"
    code = stored.get("failure_code")
    if status == "ok" and code == "none" and profile["session_identity"].get("native_session_id"):
        classified = classify_agent_failure("success")
    elif isinstance(code, str) and code in FAILURE_CLASS_BY_CODE and code != "success":
        classified = classify_agent_failure(code)
    else:
        status = "error"
        classified = classify_agent_failure("invalid_receipt")
    usage, usage_diagnostic = authorized_receipt_usage("agent", stored)
    return {
        "status": status, "job_id": job.get("job_id"), **profile,
        "summary": classified["diagnostic"]["summary"], **classified,
        "usage": usage, "usage_diagnostic": usage_diagnostic,
    }


def is_agent_job_record(job: Any) -> bool:
    """Recognize only records created by the native agent job path."""
    if not isinstance(job, dict) or job.get("kind") != "agent":
        return False
    launcher, provider, model = (job.get(key) for key in ("launcher", "provider", "model"))
    identity = job.get("session_identity")
    parsed_identity, identity_err = parse_session_identity(identity)
    native_spec = NATIVE_LAUNCHERS.get(launcher) if isinstance(launcher, str) else None
    ordinary = bool(
        native_spec and provider == native_spec["provider"] and model in native_spec["models"]
    )
    workspace = job.get("codeoff_workspace")
    codeoff = bool(
        isinstance(workspace, dict)
        and set(workspace) == {"experiment_id", "slot"}
        and isinstance(workspace.get("experiment_id"), str)
        and CODEOFF_EXPERIMENT_RE.fullmatch(workspace["experiment_id"])
        and workspace.get("slot") in {"author-1", "author-2", "judge-fable", "judge-1", "judge-2"}
        and isinstance(provider, str) and isinstance(model, str)
        and (launcher, provider, model) in EFFORT_PROFILES
    )
    return bool(
        isinstance(job.get("job_id"), str) and JOB_ID_RE.fullmatch(job["job_id"])
        and (ordinary or codeoff)
        and isinstance(identity, dict) and identity_err is None and parsed_identity == identity
        and trusted_agent_profile(job) is not None
    )


def agent_execution_snapshot(job: Any) -> dict[str, Any] | None:
    """Capture immutable launch inputs while excluding safe worker bookkeeping."""
    if not is_agent_job_record(job):
        return None
    assert isinstance(job, dict)
    immutable = {
        key: job.get(key)
        for key in (
            "job_id", "kind", "repo", "cwd", "launcher", "provider", "model",
            "requested_effort", "effective_effort", "effort_source", "launcher_version",
            "branch", "starting_head", "launch_native_session_id",
            "max_turns", "run_budget_seconds", "codeoff_workspace", "prompt_hash",
            "git_baseline", "resume_job_id", "response_webhook_url",
            "response_webhook_token", "resume_url",
        )
    }
    immutable["session_identity"] = dict(job["session_identity"])
    prompt = job.get("prompt")
    immutable["prompt_sha256"] = (
        hashlib.sha256(prompt.encode()).hexdigest() if isinstance(prompt, str) else None
    )
    return immutable


def agent_record_matches_execution_snapshot(job: Any, snapshot: dict[str, Any]) -> bool:
    return agent_execution_snapshot(job) == snapshot


def _agent_provenance_failure_receipt(job_id: str) -> dict[str, Any]:
    classified = classify_agent_failure("session_provenance_invalid")
    return {
        "status": "error", "job_id": job_id,
        **{key: None for key in AGENT_PROFILE_FIELDS}, "session_identity": None,
        "summary": classified["diagnostic"]["summary"], **classified,
        "usage": None, "usage_diagnostic": "usage_malformed",
    }


def agent_mark_running(
    job_id: str, snapshot: dict[str, Any]
) -> dict[str, Any] | None:
    """Atomically claim a queued agent record for this worker."""
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if (
            current is None or current.get("status") != "queued"
            or not agent_record_matches_execution_snapshot(current, snapshot)
        ):
            return None
        current["status"] = "running"
        current["started_at"] = utcnow()
        _write_job_unlocked(current)
        return current


def agent_update_running(
    job_id: str, snapshot: dict[str, Any], updates: dict[str, Any]
) -> bool:
    """Apply operational bookkeeping without accepting identity replacement."""
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if (
            current is None or current.get("status") != "running"
            or not agent_record_matches_execution_snapshot(current, snapshot)
        ):
            return False
        current.update(updates)
        _write_job_unlocked(current)
        return True


def agent_terminal_transition(
    job_id: str,
    snapshot: dict[str, Any],
    receipt: dict[str, Any],
    execution_identity: dict[str, Any] | None,
    returncode: int,
    timed_out: bool,
    error: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Validate and install one terminal result in one per-job critical section.

    A trusted daemon writer that replaced or terminalized the record wins.  An
    alien record is never overwritten; a still-agent drift is closed with a
    sanitized receipt while preserving the drifted immutable fields for audit.
    """
    def reject_or_sanitize(current: dict[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
        if (
            isinstance(current, dict) and current.get("job_id") == job_id
            and current.get("kind") == "agent"
            and current.get("status") in ("queued", "running")
            and not agent_record_matches_execution_snapshot(current, snapshot)
        ):
            failure = _agent_provenance_failure_receipt(job_id)
            current.update({
                "status": "failed", "finished_at": utcnow(),
                "receipt": failure,
                "error": "persisted agent execution identity changed during execution",
                "returncode": returncode,
            })
            current.pop("execution_identity", None)
            _write_job_unlocked(current)
            seal_terminal_receipt_authority("agent", failure)
            return current, False
        return None, False

    codeoff_context = CODEOFF_LOCK if isinstance(snapshot.get("codeoff_workspace"), dict) else nullcontext()
    # Match launch ordering: enclosing code-off evidence, then one job record.
    # Side-effectful collaborators run before the final guarded CAS.  Any
    # trusted write they perform is therefore visible to the exact final reread.
    with codeoff_context:
        with job_record_lock(job_id):
            prepared_from = _read_job_unlocked(job_id)
            if not (
                prepared_from is not None and prepared_from.get("status") == "running"
                and agent_record_matches_execution_snapshot(prepared_from, snapshot)
            ):
                return reject_or_sanitize(prepared_from)
            prepared_from = json.loads(json.dumps(prepared_from))

        current = json.loads(json.dumps(prepared_from))
        terminal_receipt = json.loads(json.dumps(receipt))
        if isinstance(terminal_receipt.get("session_identity"), dict):
            current["session_identity"] = dict(terminal_receipt["session_identity"])
        if terminal_receipt["status"] == "ok" and current.get("git_baseline"):
            try:
                with repo_lock(Path(current["cwd"])):
                    staging_err = stage_writer_changes(current)
            except Exception:
                staging_err = {
                    "error": "Graphwing could not verify or stage writer paths",
                    "code": "repository_mismatch",
                }
            if staging_err:
                terminal_receipt["status"] = "error"
                terminal_receipt["summary"] = staging_failure_summary(staging_err)
                current["error"] = staging_failure_summary(staging_err)
                terminal_receipt.update(classify_agent_failure("repository_mismatch"))
        terminal_receipt["summary"] = terminal_receipt["diagnostic"]["summary"]
        current["finished_at"] = utcnow()
        current["returncode"] = returncode
        current["receipt"] = terminal_receipt
        if error:
            current["error"] = error
        if execution_identity is not None and terminal_receipt["status"] == "ok":
            current["execution_identity"] = execution_identity
        else:
            current.pop("execution_identity", None)
        if (
            terminal_receipt["status"] == "ok" and current.get("codeoff_workspace")
            and not _codeoff_record_terminal_manifest_locked(current, terminal_receipt)
        ):
            terminal_receipt = _agent_provenance_failure_receipt(job_id)
            current["receipt"] = terminal_receipt
            current["error"] = "code-off terminal execution manifest could not be bound"
            current.pop("execution_identity", None)
        current["status"] = "completed" if terminal_receipt["status"] == "ok" else "failed"
        if timed_out:
            current["error"] = "agent run budget exceeded"
        elif terminal_receipt["status"] != "ok" and not current.get("error"):
            current["error"] = terminal_receipt.get("summary") or f"agent exited {returncode}"

        with job_record_lock(job_id):
            key = str(job_path(job_id).resolve())
            transition_keys = _job_terminal_transition_keys()
            transition_keys.add(key)
            try:
                persisted = _read_job_unlocked(job_id)
                if persisted != prepared_from:
                    return reject_or_sanitize(persisted)
                if not (
                    persisted.get("status") == "running"
                    and agent_record_matches_execution_snapshot(persisted, snapshot)
                ):
                    return reject_or_sanitize(persisted)
                _write_job_unlocked(current)
                seal_terminal_receipt_authority("agent", terminal_receipt)
                return current, True
            finally:
                transition_keys.discard(key)


def agent_store_webhook_result(
    job_id: str, snapshot: dict[str, Any], receipt: dict[str, Any], hook: dict[str, Any]
) -> bool:
    """Store callback bookkeeping only if the guarded terminal record remains."""
    with job_record_lock(job_id):
        current = _read_job_unlocked(job_id)
        if not isinstance(current, dict) or current.get("kind") != "agent":
            return False
        current_snapshot = agent_execution_snapshot(current)
        if current_snapshot is not None:
            current_identity = current_snapshot.get("session_identity")
            expected_identity = snapshot.get("session_identity")
            if isinstance(current_identity, dict) and isinstance(expected_identity, dict):
                current_identity["native_session_id"] = expected_identity.get("native_session_id")
        immutable_matches = current_snapshot == snapshot
        provenance_failure = receipt.get("failure_code") == "session_provenance_invalid"
        if not (
            (immutable_matches or provenance_failure)
            and current.get("status") in ("completed", "failed")
            and current.get("receipt") == receipt
        ):
            return False
        current["webhook"] = hook
        _write_job_unlocked(current)
        return True


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    profile = trusted_agent_profile(job)
    identity = profile["session_identity"] if profile else None
    return {
        "ok": True,
        "kind": "agent",
        **{key: job.get(key) for key in ("job_id", "status")},
        "repo": identity["repo"] if identity else None,
        **({key: profile[key] for key in AGENT_PROFILE_FIELDS} if profile else {key: None for key in AGENT_PROFILE_FIELDS}),
        "session_identity": identity,
        **{key: job.get(key) for key in ("created_at", "started_at", "finished_at")},
        "receipt": sanitized_agent_receipt(job),
    }


def agent_job_wait(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if set(data) != {"job_id", "timeout_seconds"}:
        return 400, {"error": "job wait requires only job_id and timeout_seconds", "code": "unexpected_fields"}
    job_id = data.get("job_id")
    timeout = data.get("timeout_seconds")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return 400, {"error": "invalid job_id", "code": "bad_job_id"}
    if isinstance(timeout, str):
        if not re.fullmatch(r"[0-9]{1,4}", timeout):
            return 400, {"error": "timeout_seconds must be 1..1800", "code": "bad_timeout"}
        timeout = int(timeout)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        return 400, {"error": "timeout_seconds must be 1..1800", "code": "bad_timeout"}
    deadline = time.monotonic() + timeout
    while True:
        job = read_job(job_id)
        if not is_agent_job_record(job):
            return 404, {"error": "job not found", "code": "not_found"}
        assert isinstance(job, dict)
        if job.get("status") not in ("queued", "running"):
            return 200, public_job(job)
        if time.monotonic() >= deadline:
            return 408, {"ok": False, "error": "job did not finish before the bounded wait", "code": "job_wait_timeout", "job_id": job_id}
        time.sleep(0.2)


def active_job_count() -> int:
    if not JOBS_DIR.is_dir():
        return 0
    n = 0
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("status") in ("queued", "running"):
            n += 1
    return n


PROCESS_JOB_KINDS = frozenset({"agent", "script", "test", "review", "rr"})


def interrupted_job_receipt(job: dict[str, Any], kind: str) -> dict[str, Any]:
    summary = f"{kind} job interrupted by Graphwing restart"[:500]
    if kind == "agent":
        profile = trusted_agent_profile(job)
        classified = classify_agent_failure("cancelled" if profile else "session_provenance_invalid")
        return {
            "status": "error", "job_id": job["job_id"],
            **({key: profile[key] for key in AGENT_PROFILE_FIELDS} if profile else {key: None for key in AGENT_PROFILE_FIELDS}),
            "session_identity": profile["session_identity"] if profile else None,
            "summary": classified["diagnostic"]["summary"], **classified,
            "usage": None, "usage_diagnostic": "usage_not_reported",
        }
    return {
        "status": "error", "job_id": job["job_id"],
        "log_ref": job.get("log_ref"), "summary": summary,
    }


def recover_interrupted_agent_jobs() -> int:
    """At process startup only, close process-owned jobs no process can still own."""
    recovered: list[dict[str, Any]] = []
    with JOB_LOCK:
        paths = list(JOBS_DIR.glob("*/job.json")) if JOBS_DIR.is_dir() else []
    for path in paths:
        candidate_id = path.parent.name
        with job_record_lock(candidate_id):
            job = _read_job_unlocked(candidate_id) or {}
            raw_kind = job.get("kind")
            kind = raw_kind if isinstance(raw_kind, str) else None
            launcher = job.get("launcher")
            job_id = job.get("job_id")
            if (
                job.get("status") not in ("queued", "running")
                or kind not in PROCESS_JOB_KINDS
                or (kind == "agent" and not is_agent_job_record(job))
                or not isinstance(job_id, str)
                or not JOB_ID_RE.fullmatch(job_id)
                or job_id != path.parent.name
            ):
                continue
            if kind == "review":
                closed = close_review_authority_unavailable(job_id)
                if closed is not None:
                    recovered.append(closed)
                continue
            receipt = interrupted_job_receipt(job, kind)
            job.update({
                "status": "failed", "finished_at": utcnow(), "receipt": receipt,
                "error": receipt["summary"], "returncode": None,
            })
            job.pop("pid", None)
            _write_job_unlocked(job)
            recovered.append(job)
    for job in recovered:
        if job.get("kind") == "review":
            continue
        try:
            delivered = deliver_webhook(job, job["receipt"])
        except Exception:
            delivered = {"ok": False}
        if delivered is not None:
            with job_record_lock(job["job_id"]):
                current = _read_job_unlocked(job["job_id"])
                if (
                    isinstance(current, dict)
                    and current.get("kind") == job.get("kind")
                    and current.get("status") == "failed"
                    and current.get("receipt") == job.get("receipt")
                ):
                    current["webhook"] = {
                        key: delivered[key] for key in ("ok", "status") if key in delivered
                    }
                    _write_job_unlocked(current)
    return len(recovered)


WATCH_RECENT = 8
WATCH_TITLE_CHARS = 72
WATCH_ERROR_CHARS = 120
WATCH_ACTIVE = frozenset({"queued", "running"})
WATCH_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")
WATCH_TICKET_SLUG_RE = re.compile(r"^\d{2,}[-_][A-Za-z0-9._-]+")
WATCH_SLICE_PATH_RE = re.compile(r"(?:^|\s)((?:slices|tickets)/[A-Za-z0-9._/-]+\.md)\b")


def clip_text(text: Any, n: int) -> str | None:
    if text is None:
        return None
    first = str(text).strip().splitlines()[0].strip() if str(text).strip() else ""
    if not first:
        return None
    if len(first) > n:
        return first[: n - 1] + "…"
    return first


def prompt_title(prompt: Any) -> str | None:
    text = str(prompt or "").strip()
    if not text:
        return None
    path = WATCH_SLICE_PATH_RE.search(text)
    if path:
        return Path(path.group(1)).stem
    first = text.splitlines()[0].strip()
    heading = WATCH_HEADING_RE.match(first)
    if heading:
        rest = heading.group(1).strip()
        if ":" in rest:
            slug = rest.split(":", 1)[0].strip()
            if WATCH_TICKET_SLUG_RE.match(slug) or "-" in slug:
                return clip_text(slug, WATCH_TITLE_CHARS)
        return clip_text(rest, WATCH_TITLE_CHARS)
    return clip_text(first, WATCH_TITLE_CHARS)


def job_title(job: dict[str, Any]) -> str:
    kind = str(job.get("kind") or "agent")
    if kind in ("script", "test", "rr"):
        return str(job.get("script") or kind)
    if kind == "review":
        who = str(job.get("reviewer") or "").strip()
        return f"review {who}".strip() if who else "review"
    ticket = str(job.get("ticket") or "").strip()
    if ticket:
        name = Path(ticket).name
        if name.endswith(".md"):
            name = name[:-3]
        return clip_text(name, WATCH_TITLE_CHARS) or kind
    pr = job.get("pr")
    if pr not in (None, ""):
        return f"PR {pr}"
    return prompt_title(job.get("prompt")) or kind


def watch_job(job: dict[str, Any]) -> dict[str, Any]:
    tab_id = job.get("herdr_tab_id")
    return {
        "job_id": str(job.get("job_id") or ""),
        "status": str(job.get("status") or ""),
        "kind": str(job.get("kind") or "agent"),
        "title": job_title(job),
        "repo": job.get("repo"),
        "tab": herdr_job_tab_label(job),
        "herdr_tab_id": str(tab_id) if tab_id else None,
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": clip_text(job.get("error"), WATCH_ERROR_CHARS),
        "summary": clip_text(
            (job.get("receipt") or {}).get("summary") if isinstance(job.get("receipt"), dict) else None,
            80,
        ),
    }


def watch_snapshot() -> dict[str, Any]:
    """Compact panel payload: units plus in-flight and recent jobs. No secrets."""
    units = units_status()
    active: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    if JOBS_DIR.is_dir():
        for path in JOBS_DIR.glob("*/job.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            job_id = str(data.get("job_id") or "")
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            row = watch_job(data)
            if row["status"] in WATCH_ACTIVE:
                active.append(row)
            else:
                finished.append(row)
    active.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    finished.sort(key=lambda j: str(j.get("finished_at") or j.get("created_at") or ""), reverse=True)
    recent = finished[:WATCH_RECENT]
    queued = sum(1 for j in active if j["status"] == "queued")
    running = sum(1 for j in active if j["status"] == "running")
    failed_recent = sum(1 for j in recent if j["status"] == "failed")
    api = (units.get("units") or {}).get("graphwing-api") or {}
    api_active = bool(api.get("active"))
    return {
        "ok": True,
        "service": "graphwing",
        "units": units.get("units") or {},
        # herdr and the tunnel are optional. The bar's traffic light is the API.
        "units_healthy": api_active,
        "api_active": api_active,
        "counts": {
            "queued": queued,
            "running": running,
            "active": len(active),
            "failed_recent": failed_recent,
        },
        "active": active,
        "recent": recent,
        "workflows": {"recent": list_workflow_runs(WATCH_WORKFLOW_RECENT)},
    }


def resolve_run_cwd(raw: str | None, repos: dict[str, str]) -> tuple[str, Path] | tuple[None, dict[str, Any]]:
    key = (raw or default_repo_name(repos)).strip()
    if not key:
        key = default_repo_name(repos)
    if "/" in key or key.startswith("."):
        return None, {
            "error": "cwd must be an allowlisted repo short name, not a filesystem path",
            "code": "bad_cwd",
            "allowed": sorted(repos),
        }
    return resolve_repo(key, repos)


def parse_json_object(body: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not body:
        return {}, None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {"error": "body must be JSON", "code": "bad_json"}
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, {"error": "body must be a JSON object", "code": "bad_json"}
    return data, None


def _request_header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    direct = headers.get(name) if hasattr(headers, "get") else None
    if direct is not None:
        return str(direct)
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return ""


def doorbell_pr_drive(body: bytes, headers: Any) -> tuple[int, dict[str, Any]]:
    authorization = _request_header(headers, "Authorization").strip()
    if not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        return 401, {"error": "GitHub Actions OIDC bearer token required", "code": "missing_oidc"}
    token = authorization[7:].strip()
    try:
        config = load_doorbell()
        claims = verify_github_oidc(token, config)
    except Exception:
        return 401, {"error": "invalid GitHub Actions OIDC token", "code": "invalid_oidc"}

    repository = str(claims.get("repository") or "")
    actor = str(claims.get("actor") or "")
    allow_repos = config.get("allow_repos")
    if not isinstance(allow_repos, dict) or repository not in allow_repos:
        return 403, {"error": "repository is not allowed", "code": "repo_not_allowed"}
    allow_actors = config.get("allow_actors")
    if not isinstance(allow_actors, list) or actor not in allow_actors:
        return 403, {"error": "actor is not allowed", "code": "actor_not_allowed"}

    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    pr = data.get("pr")
    if pr in (None, ""):
        return 200, {"ok": True, "skipped": True, "reason": "missing_pr"}

    mapping = allow_repos[repository]
    if isinstance(mapping, str):
        mapping = {"repo": mapping}
    if not isinstance(mapping, dict) or not str(mapping.get("repo") or "").strip():
        return 403, {"error": "repository is not allowed", "code": "repo_not_allowed"}
    mapped_repo = str(mapping["repo"]).strip()
    defaults = {
        "riftwing-drive": {"test": "go-fmt"},
        "graphwing": {"test": "catalog-compile"},
    }
    settings = defaults.get(mapped_repo, {})
    hook_payload = {
        "repo": mapped_repo,
        "pr": pr,
        "test": str(mapping.get("test") or settings.get("test") or ""),
        "prompt": str(mapping.get("prompt") or DOORBELL_PROMPT),
        "commit_message": str(mapping.get("commit_message") or "pr-drive doorbell"),
    }
    try:
        install = load_rewst_install()
    except Exception:
        install = {}
    hook_url = str(install.get("pr_drive_hook_url") or "").strip()
    hook_secret = str(install.get("hook_secret") or install.get("pr_drive_hook_secret") or "").strip()
    if not hook_url or not hook_secret:
        return 503, {"error": "pr-drive hook is not configured", "code": "hook_unconfigured"}

    request = Request(
        hook_url,
        data=json.dumps(hook_payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-rewst-secret": hook_secret},
    )
    try:
        response = urlopen(request, timeout=20)
        rewst_status = int(getattr(response, "status", 200))
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:
        return 502, {"error": "pr-drive hook failed", "code": "hook_failed"}
    if not 200 <= rewst_status < 300:
        return 502, {"error": "pr-drive hook failed", "code": "hook_failed"}
    record_workflow_run(
        {
            "workflow": "pr-drive",
            "status": "fired",
            "source": "doorbell",
            "input": sanitize_run_input(hook_payload),
            "created_at": utcnow(),
        }
    )
    return 200, {
        "ok": True,
        "mapped_repo": mapped_repo,
        "pr": pr,
        "rewst_status": rewst_status,
    }


def valid_webhook_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.netloc.startswith(".")


def parse_webhook_fields(data: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any] | None]:
    url = data.get("response_webhook_url")
    if url in ("", None):
        url = data.get("resume_url")
    if url in ("", None):
        url = None
    elif not isinstance(url, str) or not valid_webhook_url(url):
        return None, None, {"error": "response_webhook_url must be https", "code": "bad_webhook_url"}
    token = data.get("response_webhook_token")
    if token in ("", None):
        token = None
    elif not isinstance(token, str):
        return None, None, {"error": "response_webhook_token must be a string", "code": "bad_webhook_token"}
    else:
        token = token.strip() or None
        if token and len(token) > 2048:
            return None, None, {"error": "response_webhook_token too large", "code": "bad_webhook_token"}
    return url, token, None


def current_branch_head(repo: Path) -> tuple[str | None, str | None, dict[str, Any] | None]:
    branch = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    head = run_git(repo, ["rev-parse", "HEAD"])
    if not branch.get("ok") or not head.get("ok"):
        return None, None, {"error": "could not read current git identity", "code": "git_state_unavailable"}
    return str(branch["stdout"]).strip(), str(head["stdout"]).strip(), None


def evidence_git_state(repo: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    branch, sha, err = current_branch_head(repo)
    status = run_git(repo, ["status", "--porcelain=v1"])
    if err or not status.get("ok") or not GIT_SHA_RE.fullmatch(str(sha or "")):
        return None, err or {"error": "could not read clean git state", "code": "git_state_unavailable"}
    return {"branch": branch, "sha": sha, "clean": not bool(str(status.get("stdout") or "").strip())}, None


def parse_session_identity(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if raw in (None, ""):
        return None, None
    if not isinstance(raw, dict):
        return None, {"error": "session_identity must be an object", "code": "bad_session_identity"}
    allowed = {
        "launcher", "provider", "model", "requested_effort", "effective_effort",
        "effort_source", "launcher_version", "repo", "branch", "starting_head",
        "native_session_id",
    }
    source = raw.get("effort_source")
    if source == "route":
        allowed.add("route_execution_profile")
    if set(raw) != allowed:
        return None, {"error": "session_identity has missing or unsupported fields", "code": "bad_session_identity"}
    out: dict[str, Any] = {}
    for key in (
        "launcher", "provider", "model", "requested_effort", "effective_effort",
        "effort_source", "launcher_version", "repo", "branch", "starting_head",
        "native_session_id",
    ):
        value = raw.get(key)
        if value is None and key == "native_session_id":
            out[key] = None
        elif not isinstance(value, str) or not value or value != value.strip():
            return None, {"error": f"session_identity.{key} is required", "code": "bad_session_identity"}
        else:
            out[key] = value
    if (
        not SESSION_REPO_RE.fullmatch(out["repo"])
        or len(out["branch"]) > 255
        or not valid_branch(out["branch"])
        or not GIT_SHA_RE.fullmatch(out["starting_head"])
    ):
        return None, {"error": "session_identity repository provenance is invalid", "code": "bad_session_identity"}
    session_id = out["native_session_id"]
    if session_id is not None and not NATIVE_SESSION_RE.fullmatch(session_id):
        return None, {"error": "session_identity.native_session_id is invalid", "code": "bad_session_identity"}
    if source == "route":
        route_raw = raw.get("route_execution_profile")
        parsed_route, route_err = parse_route_execution_profile(
            route_raw, expected_role="writer", launcher=out["launcher"],
            provider=out["provider"], model=out["model"],
            effort=out["requested_effort"],
        )
        if route_err is not None or parsed_route != route_raw:
            return None, {
                "error": "session_identity route execution profile is invalid",
                "code": "bad_session_identity",
            }
        out["route_execution_profile"] = parsed_route
    normalized, effort_err = normalize_effort(
        out["launcher"], out["provider"], out["model"],
        out["requested_effort"], out["effort_source"],
    )
    if (
        effort_err or normalized is None
        or out["effective_effort"] != normalized["effective_effort"]
        or not LAUNCHER_VERSION_RE.fullmatch(out["launcher_version"])
    ):
        return None, {"error": "session_identity execution profile is invalid", "code": "bad_session_identity"}
    return out, None


def validate_native_resume_provenance(
    data: dict[str, Any], requested_identity: dict[str, Any], launcher: str
) -> dict[str, Any] | None:
    resume_job_id = data.get("resume_job_id")
    if resume_job_id in (None, ""):
        return {"error": f"resume_job_id is required for {launcher} resume", "code": "missing_resume_job_id"}
    if not isinstance(resume_job_id, str) or not JOB_ID_RE.fullmatch(resume_job_id):
        return {"error": "resume_job_id must be 32 lowercase hex characters", "code": "bad_resume_job_id"}
    prior = read_job(resume_job_id)
    receipt = prior.get("receipt") if isinstance(prior, dict) else None
    profile = trusted_agent_profile(prior) if isinstance(prior, dict) else None
    if not (
        isinstance(prior, dict)
        and is_agent_job_record(prior)
        and prior.get("status") == "completed"
        and prior.get("session_identity") == requested_identity
        and profile is not None
        and all(prior.get(key) == requested_identity.get(key) for key in AGENT_PROFILE_FIELDS)
        and isinstance(receipt, dict)
        and receipt.get("status") == "ok"
        and receipt.get("job_id") == resume_job_id
        and receipt.get("session_identity") == requested_identity
        and all(receipt.get(key) == prior.get(key) for key in AGENT_PROFILE_FIELDS)
    ):
        return {"error": f"resume session_identity is not traceable to a successful {launcher} job", "code": "untraceable_resume_session"}
    return None


def parse_native_session_id(text: str, launcher: str = "codex") -> str | None:
    if not isinstance(text, str) or len(text) > _PROVIDER_OUTPUT_MAX_CHARS:
        return None
    for line in (text or "").splitlines():
        try:
            event = strict_json_object(line)
        except ValueError:
            continue
        if launcher == "codex" and event.get("type") == "thread.started":
            session_id = event.get("thread_id")
        elif launcher == "claude" and event.get("type") == "result":
            session_id = event.get("session_id")
        else:
            continue
        if isinstance(session_id, str) and NATIVE_SESSION_RE.fullmatch(session_id.strip()):
            return session_id.strip()
    return None


USAGE_VERSION = "normalized-usage-v1"
# One bounded Graphwing job is limited to 80 turns and 1,200 execution
# seconds. One billion tokens, USD 100,000, and 1,800 wall seconds are closed
# safety ceilings far above any supported job while preventing absurd provider
# numbers from entering receipts or numeric conversions.
USAGE_MAX_COUNT = 1_000_000_000
USAGE_MAX_COST_USD = 100_000
USAGE_MAX_WALL_SECONDS = 1_800
USAGE_MAX_TURNS = 80
USAGE_FIELDS = frozenset({
    "usage_version", "fresh_input_tokens", "cached_input_tokens",
    "cache_write_tokens", "output_tokens", "reasoning_tokens",
    "provider_cost_usd", "wall_seconds", "turns_observed",
})
USAGE_DIAGNOSTICS = frozenset({
    "usage_not_reported", "usage_incomplete", "usage_malformed",
    "usage_authority_unavailable", "usage_authority_mismatch",
})
TERMINAL_RECEIPT_AUTHORITY_LOCK = threading.Lock()
TERMINAL_RECEIPT_AUTHORITY: dict[tuple[str, str], str] = {}


def valid_normalized_usage(usage: Any) -> bool:
    if not isinstance(usage, dict) or set(usage) != USAGE_FIELDS:
        return False
    if usage.get("usage_version") != USAGE_VERSION:
        return False
    if any(_usage_count(usage.get(key)) is None for key in (
        "fresh_input_tokens", "cached_input_tokens", "cache_write_tokens",
        "output_tokens", "reasoning_tokens",
    )):
        return False
    cost = usage.get("provider_cost_usd")
    turns = usage.get("turns_observed")
    return bool(
        _usage_number(usage.get("wall_seconds"), USAGE_MAX_WALL_SECONDS) is not None
        and (cost is None or _usage_number(cost, USAGE_MAX_COST_USD) is not None)
        and (turns is None or _usage_count(turns, USAGE_MAX_TURNS) is not None)
    )


def _terminal_receipt_digest(kind: str, receipt: dict[str, Any]) -> str:
    canonical = {"kind": kind, "receipt": receipt}
    if not _provider_json_is_bounded(canonical):
        raise ValueError("terminal receipt exceeds canonical resource limits")
    try:
        payload = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode()
    except RecursionError as exc:
        raise ValueError("terminal receipt exceeds canonical depth") from exc
    return hashlib.sha256(payload).hexdigest()


def seal_terminal_receipt_authority(kind: str, receipt: dict[str, Any]) -> None:
    job_id = receipt.get("job_id") if isinstance(receipt, dict) else None
    usage = receipt.get("usage") if isinstance(receipt, dict) else None
    diagnostic = receipt.get("usage_diagnostic") if isinstance(receipt, dict) else None
    if (
        kind not in ("agent", "review")
        or not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id)
        or not (
            (valid_normalized_usage(usage) and diagnostic is None)
            or (usage is None and diagnostic in {
                "usage_not_reported", "usage_incomplete", "usage_malformed",
            })
        )
    ):
        raise ValueError("terminal receipt usage is not canonical")
    digest = _terminal_receipt_digest(kind, receipt)
    with TERMINAL_RECEIPT_AUTHORITY_LOCK:
        TERMINAL_RECEIPT_AUTHORITY[(kind, job_id)] = digest


def clear_terminal_receipt_authority(kind: str, job_id: str) -> None:
    with TERMINAL_RECEIPT_AUTHORITY_LOCK:
        TERMINAL_RECEIPT_AUTHORITY.pop((kind, job_id), None)


def authorized_receipt_usage(
    kind: str, receipt: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(receipt, dict):
        return None, "usage_authority_mismatch"
    job_id = receipt.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return None, "usage_authority_mismatch"
    with TERMINAL_RECEIPT_AUTHORITY_LOCK:
        expected = TERMINAL_RECEIPT_AUTHORITY.get((kind, job_id))
    if expected is None:
        return None, "usage_authority_unavailable"
    try:
        actual = _terminal_receipt_digest(kind, receipt)
    except (TypeError, ValueError):
        return None, "usage_authority_mismatch"
    if not hmac.compare_digest(expected, actual):
        return None, "usage_authority_mismatch"
    usage, diagnostic = receipt.get("usage"), receipt.get("usage_diagnostic")
    if valid_normalized_usage(usage) and diagnostic is None:
        return json.loads(json.dumps(usage)), None
    if usage is None and diagnostic in {
        "usage_not_reported", "usage_incomplete", "usage_malformed",
    }:
        return None, diagnostic
    return None, "usage_authority_mismatch"


def _usage_count(value: Any, maximum: int = USAGE_MAX_COUNT) -> int | None:
    return value if type(value) is int and 0 <= value <= maximum else None


class _ExactJSONDecimal(Decimal):
    """Marker for non-integer numbers parsed from provider-authored JSON."""


def _usage_number(
    value: Any, maximum: int | float = USAGE_MAX_COST_USD,
) -> float | int | None:
    """Validate exact built-in JSON numbers before any lossy conversion."""
    if type(value) is int:
        return value if 0 <= value <= maximum else None
    if type(value) is float:
        return value if math.isfinite(value) and 0 <= value <= maximum else None
    if type(value) is _ExactJSONDecimal:
        upper = Decimal(str(maximum))
        if not value.is_finite() or value < 0 or value > upper:
            return None
        converted = float(value)
        if not math.isfinite(converted) or (value != 0 and converted == 0):
            return None
        return converted
    return None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


_PROVIDER_JSON_MAX_RAW_BYTES = CMD_MAX_BYTES + FILE_MAX_BYTES
_PROVIDER_JSON_MAX_DEPTH = 32
_PROVIDER_JSON_MAX_NODES = 8192
_PROVIDER_JSON_MAX_CONTAINERS = 2048
_PROVIDER_JSON_MAX_ARRAY_ITEMS = 1024
_PROVIDER_JSON_MAX_OBJECT_ITEMS = 1024
_PROVIDER_JSON_MAX_TOTAL_ARRAY_ITEMS = 4096
_PROVIDER_JSON_MAX_TOTAL_OBJECT_ITEMS = 4096
_PROVIDER_JSON_MAX_KEY_CHARS = 1024
_PROVIDER_JSON_MAX_STRING_CHARS = CMD_MAX_BYTES
_PROVIDER_JSON_MAX_TOTAL_STRING_CHARS = _PROVIDER_JSON_MAX_RAW_BYTES
_PROVIDER_JSON_MAX_EVENTS = 4096
_PROVIDER_OUTPUT_MAX_CHARS = _PROVIDER_JSON_MAX_RAW_BYTES + 1


def _provider_json_is_bounded(value: Any) -> bool:
    """Iteratively validate a finite, explicitly bounded built-in JSON tree."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = containers = array_items = object_items = string_chars = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _PROVIDER_JSON_MAX_NODES or depth > _PROVIDER_JSON_MAX_DEPTH:
            return False
        if type(item) is dict:
            containers += 1
            size = len(item)
            object_items += size
            if (
                containers > _PROVIDER_JSON_MAX_CONTAINERS
                or size > _PROVIDER_JSON_MAX_OBJECT_ITEMS
                or object_items > _PROVIDER_JSON_MAX_TOTAL_OBJECT_ITEMS
            ):
                return False
            for key, child in item.items():
                if type(key) is not str or len(key) > _PROVIDER_JSON_MAX_KEY_CHARS:
                    return False
                string_chars += len(key)
                if string_chars > _PROVIDER_JSON_MAX_TOTAL_STRING_CHARS:
                    return False
                pending.append((child, depth + 1))
            continue
        if type(item) is list:
            containers += 1
            size = len(item)
            array_items += size
            if (
                containers > _PROVIDER_JSON_MAX_CONTAINERS
                or size > _PROVIDER_JSON_MAX_ARRAY_ITEMS
                or array_items > _PROVIDER_JSON_MAX_TOTAL_ARRAY_ITEMS
            ):
                return False
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is str:
            size = len(item)
            string_chars += size
            if (
                size > _PROVIDER_JSON_MAX_STRING_CHARS
                or string_chars > _PROVIDER_JSON_MAX_TOTAL_STRING_CHARS
            ):
                return False
            continue
        if item is None or type(item) in {bool, int}:
            continue
        if type(item) is float:
            if math.isfinite(item):
                continue
            return False
        if type(item) is _ExactJSONDecimal:
            if item.is_finite():
                continue
            return False
        return False
    return True


def strict_json_object(raw: str | bytes) -> dict[str, Any]:
    """Parse one resource-bounded JSON object, rejecting duplicate keys."""
    if not isinstance(raw, (str, bytes)) or len(raw) > _PROVIDER_JSON_MAX_RAW_BYTES:
        raise ValueError("malformed JSON object")
    if isinstance(raw, str) and len(raw.encode("utf-8", "replace")) > _PROVIDER_JSON_MAX_RAW_BYTES:
        raise ValueError("malformed JSON object")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_float=_ExactJSONDecimal,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, DecimalException, RecursionError) as exc:
        raise ValueError("malformed JSON object") from exc
    if type(value) is not dict or not _provider_json_is_bounded(value):
        raise ValueError("JSON value is not an object")
    return value


def _usage_events(text: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if text is None:
        return None, "usage_not_reported"
    if not isinstance(text, str):
        return None, "usage_malformed"
    if (
        len(text) > _PROVIDER_JSON_MAX_RAW_BYTES
        or len(text.encode("utf-8", "replace")) > _PROVIDER_JSON_MAX_RAW_BYTES
    ):
        return None, "usage_malformed"
    if not text.strip():
        return None, "usage_not_reported"
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(events) >= _PROVIDER_JSON_MAX_EVENTS:
            return None, "usage_malformed"
        try:
            event = strict_json_object(line)
        except ValueError:
            return None, "usage_malformed"
        events.append(event)
    return (events, None) if events else (None, "usage_not_reported")


_CLAUDE_RESULT_REQUIRED_FIELDS = {
    "type", "subtype", "is_error", "duration_ms",
    "duration_api_ms", "num_turns", "result", "stop_reason", "session_id",
    "total_cost_usd", "usage", "modelUsage", "permission_denials",
    "uuid",
}
_CLAUDE_RESULT_NULLABLE_REQUIRED_FIELDS = {"stop_reason"}
_CLAUDE_RESULT_OPTIONAL_FIELDS = {
    "ttft_ms", "ttft_stream_ms", "time_to_request_ms", "user_message_uuid",
    "request_sent_wall_ms", "time_to_request_from_spawn_ms",
    "warm_spare_claimed", "time_origin_ms", "api_error_status",
    "subagent_stats", "queued_turn_count", "structured_output",
    "deferred_tool_use", "terminal_reason", "fast_mode_state",
    "fast_mode_disabled_reason", "origin",
}
_CLAUDE_USAGE_FIELDS = {
    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "output_tokens", "server_tool_use", "service_tier", "cache_creation",
    "inference_geo", "iterations", "speed",
}
_CLAUDE_CACHE_CREATION_FIELDS = {
    "ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens",
}
_CLAUDE_ITERATION_FIELDS = {
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "cache_creation", "type",
}
_CLAUDE_MODEL_USAGE_REQUIRED_FIELDS = {
    "inputTokens", "outputTokens", "cacheReadInputTokens",
    "cacheCreationInputTokens", "webSearchRequests", "costUSD",
    "contextWindow", "maxOutputTokens",
}
_CLAUDE_MODEL_USAGE_OPTIONAL_FIELDS = {
    "canonicalModel", "provider", "costBasis",
}
_CLAUDE_COST_BASES = {"list", "managed", "unknown"}
_CLAUDE_FAST_MODE_STATES = {"off", "cooldown", "on"}
_CLAUDE_FAST_MODE_DISABLED_REASONS = {
    "free", "preference", "extra_usage_disabled", "network_error", "unknown",
    "not_first_party", "disabled_by_env", "model_not_allowed",
    "sdk_opt_in_required", "pending",
}
_CLAUDE_TERMINAL_REASONS = {
    "blocking_limit", "rapid_refill_breaker", "prompt_too_long", "image_error",
    "model_error", "api_error", "malformed_tool_use_exhausted",
    "aborted_streaming", "aborted_tools", "stop_hook_prevented", "hook_stopped",
    "tool_deferred", "max_turns", "background_requested", "completed",
    "budget_exhausted", "structured_output_retry_exhausted",
    "tool_deferred_unavailable", "turn_setup_failed",
}
_CLAUDE_SUBAGENT_FIELDS = {
    "spawned", "requested", "started_in_background", "by_type", "max_depth",
    "spawned_by_subagents", "completed", "failed", "killed", "refused",
}
_CLAUDE_ORIGIN_KINDS = {
    "human", "channel", "peer", "task-notification", "coordinator",
    "unclassified", "observer", "auto-continuation", "observer-activity",
}
# Claude emits duration counters and epoch-millisecond correlation timestamps in
# the same optional telemetry envelope. Ten trillion accepts dates well beyond
# the supported runtime horizon while remaining finite, safely integral where
# the contract says integer, and decisively rejects unbounded provider values.
_CLAUDE_TELEMETRY_MAX = 10_000_000_000_000
_CLAUDE_METADATA_MAX_CHARS = 1024


def _claude_metadata_string_is_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _CLAUDE_METADATA_MAX_CHARS
        and value.isprintable()
    )


def _claude_telemetry_integer_is_valid(value: Any) -> bool:
    return _usage_count(value, _CLAUDE_TELEMETRY_MAX) is not None


def _claude_telemetry_number_is_valid(value: Any) -> bool:
    return _usage_number(value, _CLAUDE_TELEMETRY_MAX) is not None


def _claude_subagent_stats_is_valid(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _CLAUDE_SUBAGENT_FIELDS:
        return False
    scalar_fields = _CLAUDE_SUBAGENT_FIELDS - {"requested", "by_type", "killed", "refused"}
    if not all(_claude_telemetry_integer_is_valid(value.get(field)) for field in scalar_fields):
        return False
    requested = value.get("requested")
    killed = value.get("killed")
    refused = value.get("refused")
    by_type = value.get("by_type")
    return (
        isinstance(requested, dict)
        and set(requested) == {"background", "foreground", "unset"}
        and all(_claude_telemetry_integer_is_valid(item) for item in requested.values())
        and isinstance(killed, dict)
        and set(killed) == {"parent", "user", "system"}
        and all(_claude_telemetry_integer_is_valid(item) for item in killed.values())
        and isinstance(refused, dict)
        and set(refused) == {"depth_limit", "concurrency_limit", "budget"}
        and all(_claude_telemetry_integer_is_valid(item) for item in refused.values())
        and isinstance(by_type, dict)
        and all(
            _claude_metadata_string_is_valid(kind)
            and _claude_telemetry_integer_is_valid(count)
            for kind, count in by_type.items()
        )
    )


def _claude_json_record_is_valid(value: Any) -> bool:
    # JSON object keys are strings by construction; values remain arbitrary JSON
    # exactly as in the native record schema and are never projected.
    return isinstance(value, dict)


def _claude_deferred_tool_use_is_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "name", "input"}
        and _claude_metadata_string_is_valid(value.get("id"))
        and _claude_metadata_string_is_valid(value.get("name"))
        and _claude_json_record_is_valid(value.get("input"))
    )


def _claude_origin_is_valid(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("kind"), str)
        or value["kind"] not in _CLAUDE_ORIGIN_KINDS
    ):
        return False
    kind = value["kind"]
    if kind in {"human", "coordinator", "unclassified", "auto-continuation", "observer-activity"}:
        return set(value) == {"kind"}
    if kind == "channel":
        return set(value) == {"kind", "server"} and _claude_metadata_string_is_valid(
            value.get("server")
        )
    if kind == "observer":
        return (
            set(value) == {"kind", "from", "senderTaskId"}
            and _claude_metadata_string_is_valid(value.get("from"))
            and _claude_metadata_string_is_valid(value.get("senderTaskId"))
        )
    if kind == "task-notification":
        return (
            set(value) <= {"kind", "subkind"}
            and (
                "subkind" not in value
                or (
                    isinstance(value["subkind"], str)
                    and value["subkind"] in {
                        "scheduled-trigger", "peer-send-message", "projects-relay",
                    }
                )
            )
        )
    allowed = {
        "kind", "from", "fromMode", "name", "fromSession", "inbound_origin",
        "senderTaskId", "body", "verifiedPeerPid",
    }
    if not ({"kind", "from"} <= set(value) <= allowed):
        return False
    metadata_fields = {
        "from", "name", "fromSession", "inbound_origin", "senderTaskId",
    }
    if not all(
        field not in value or _claude_metadata_string_is_valid(value[field])
        for field in metadata_fields
    ):
        return False
    return (
        (
            "fromMode" not in value
            or (
                isinstance(value["fromMode"], str)
                and value["fromMode"] in {"bypass", "prompting"}
            )
        )
        and (
            "body" not in value
            or isinstance(value["body"], str)
        )
        and (
            "verifiedPeerPid" not in value
            or _claude_telemetry_number_is_valid(value["verifiedPeerPid"])
        )
    )


def _claude_permission_denials_is_valid(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    return all(
        isinstance(item, dict)
        and set(item) == {"tool_name", "tool_use_id", "tool_input"}
        and _claude_metadata_string_is_valid(item.get("tool_name"))
        and _claude_metadata_string_is_valid(item.get("tool_use_id"))
        and _claude_json_record_is_valid(item.get("tool_input"))
        for item in value
    )


def _claude_cache_creation_is_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _CLAUDE_CACHE_CREATION_FIELDS
        and all(_usage_count(item) is not None for item in value.values())
    )


def _claude_usage_envelope_is_valid(detail: dict[str, Any]) -> bool:
    server_tools = detail.get("server_tool_use")
    if not (
        set(detail) == _CLAUDE_USAGE_FIELDS
        and isinstance(server_tools, dict)
        and set(server_tools) == {"web_search_requests", "web_fetch_requests"}
        and all(_usage_count(item) is not None for item in server_tools.values())
        and _claude_cache_creation_is_valid(detail.get("cache_creation"))
        and all(
            isinstance(detail.get(field), str)
            for field in ("service_tier", "inference_geo", "speed")
        )
        and isinstance(detail.get("iterations"), list)
    ):
        return False
    for iteration in detail["iterations"]:
        if not (
            isinstance(iteration, dict)
            and set(iteration) == _CLAUDE_ITERATION_FIELDS
            and isinstance(iteration.get("type"), str)
            and all(
                _usage_count(iteration.get(field)) is not None
                for field in (
                    "input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            and _claude_cache_creation_is_valid(iteration.get("cache_creation"))
        ):
            return False
    return True


def _claude_model_usage_is_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for model, detail in value.items():
        if not (
            isinstance(model, str)
            and _claude_metadata_string_is_valid(model)
            and isinstance(detail, dict)
            and _CLAUDE_MODEL_USAGE_REQUIRED_FIELDS <= set(detail)
            and set(detail) <= (
                _CLAUDE_MODEL_USAGE_REQUIRED_FIELDS
                | _CLAUDE_MODEL_USAGE_OPTIONAL_FIELDS
            )
            and all(
                _usage_count(detail.get(field)) is not None
                for field in _CLAUDE_MODEL_USAGE_REQUIRED_FIELDS - {"costUSD"}
            )
            and _usage_number(detail.get("costUSD")) is not None
            and (
                "canonicalModel" not in detail
                or _claude_metadata_string_is_valid(detail["canonicalModel"])
            )
            and (
                "provider" not in detail
                or _claude_metadata_string_is_valid(detail["provider"])
            )
            and (
                "costBasis" not in detail
                or (
                    isinstance(detail["costBasis"], str)
                    and detail["costBasis"] in _CLAUDE_COST_BASES
                )
            )
        ):
            return False
    return True


def _claude_success_result_is_valid(result: dict[str, Any]) -> bool:
    integer_telemetry = {
        "duration_ms", "duration_api_ms", "num_turns", "ttft_ms",
        "ttft_stream_ms", "time_to_request_ms", "time_to_request_from_spawn_ms",
        "queued_turn_count",
    }
    if not (
        _CLAUDE_RESULT_REQUIRED_FIELDS <= set(result)
        and set(result) <= (
            _CLAUDE_RESULT_REQUIRED_FIELDS | _CLAUDE_RESULT_OPTIONAL_FIELDS
        )
        and result.get("type") == "result"
        and result.get("subtype") == "success"
        and result.get("is_error") is False
        and all(
            field not in result or _claude_telemetry_integer_is_valid(result[field])
            for field in integer_telemetry
        )
        and isinstance(result.get("result"), str)
        and _usage_number(result.get("total_cost_usd")) is not None
        and (
            result.get("stop_reason") is None
            or _claude_metadata_string_is_valid(result["stop_reason"])
        )
        and _claude_metadata_string_is_valid(result.get("session_id"))
        and _claude_metadata_string_is_valid(result.get("uuid"))
        and _claude_permission_denials_is_valid(result.get("permission_denials"))
        and _claude_model_usage_is_valid(result.get("modelUsage"))
    ):
        return False
    return (
        (
            "api_error_status" not in result
            or result["api_error_status"] is None
            or _claude_telemetry_integer_is_valid(result["api_error_status"])
        )
        and (
            "user_message_uuid" not in result
            or _claude_metadata_string_is_valid(result["user_message_uuid"])
        )
        and all(
            field not in result or _claude_telemetry_number_is_valid(result[field])
            for field in ("request_sent_wall_ms", "time_origin_ms")
        )
        and (
            "warm_spare_claimed" not in result
            or type(result["warm_spare_claimed"]) is bool
        )
        and (
            "subagent_stats" not in result
            or _claude_subagent_stats_is_valid(result["subagent_stats"])
        )
        and (
            "deferred_tool_use" not in result
            or _claude_deferred_tool_use_is_valid(result["deferred_tool_use"])
        )
        and (
            "terminal_reason" not in result
            or (
                isinstance(result["terminal_reason"], str)
                and result["terminal_reason"] in _CLAUDE_TERMINAL_REASONS
            )
        )
        and (
            "fast_mode_state" not in result
            or (
                isinstance(result["fast_mode_state"], str)
                and result["fast_mode_state"] in _CLAUDE_FAST_MODE_STATES
            )
        )
        and (
            "fast_mode_disabled_reason" not in result
            or (
                isinstance(result["fast_mode_disabled_reason"], str)
                and result["fast_mode_disabled_reason"]
                in _CLAUDE_FAST_MODE_DISABLED_REASONS
            )
        )
        and (
            "origin" not in result
            or _claude_origin_is_valid(result["origin"])
        )
    )


def _closed_usage(
    fresh: Any, cached: Any, cache_write: Any, output: Any, reasoning: Any,
    provider_cost: Any, wall_seconds: Any, turns: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    counts = tuple(_usage_count(value) for value in (
        fresh, cached, cache_write, output, reasoning,
    ))
    if any(value is None for value in counts):
        return None, "usage_malformed"
    wall = _usage_number(wall_seconds, USAGE_MAX_WALL_SECONDS)
    if wall is None:
        return None, "usage_malformed"
    cost = None if provider_cost is None else _usage_number(
        provider_cost, USAGE_MAX_COST_USD,
    )
    if provider_cost is not None and cost is None:
        return None, "usage_malformed"
    observed = None if turns is None else _usage_count(turns, USAGE_MAX_TURNS)
    if turns is not None and observed is None:
        return None, "usage_malformed"
    return {
        "usage_version": USAGE_VERSION,
        "fresh_input_tokens": counts[0],
        "cached_input_tokens": counts[1],
        "cache_write_tokens": counts[2],
        "output_tokens": counts[3],
        "reasoning_tokens": counts[4],
        "provider_cost_usd": cost,
        "wall_seconds": float(wall),
        "turns_observed": observed,
    }, None


def _normalize_native_usage(
    launcher: str, native: Any, wall_seconds: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize one complete supported native report without retaining it.

    Claude 2.1.250 result usage has no separate reasoning counter. Codex 0.150.1
    reports cache-write and reasoning counters explicitly. Grok 1.0.5 returns
    the ACP Usage shape, but its observed total is input plus output and the ACP
    schema does not establish that cache counters are subsets of input. Nonzero
    Grok cache counters therefore cannot be completely normalized. No adapter
    trusts provider-authored turn counts or infers API-dollar cost.
    """
    if _usage_number(wall_seconds, USAGE_MAX_WALL_SECONDS) is None:
        return None, "usage_malformed"
    if launcher == "grok":
        if native is None:
            return None, "usage_not_reported"
        if not _provider_json_is_bounded(native):
            return None, "usage_malformed"
        if not isinstance(native, dict):
            return None, "usage_malformed"
        keys = set(native)
        allowed = {"stopReason", "usage", "userMessageId", "_meta"}
        if "stopReason" not in keys or not keys <= allowed:
            return None, "usage_malformed"
        stop_reason = native["stopReason"]
        if not isinstance(stop_reason, str) or stop_reason not in {
            "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled",
        }:
            return None, "usage_malformed"
        user_message_id = native.get("userMessageId")
        if "userMessageId" in native and user_message_id is not None and not isinstance(
            user_message_id, str,
        ):
            return None, "usage_malformed"
        meta = native.get("_meta")
        if "_meta" in native and meta is not None and not isinstance(meta, dict):
            return None, "usage_malformed"
        detail = native.get("usage")
        if detail is None:
            return None, "usage_not_reported"
        if not isinstance(detail, dict):
            return None, "usage_malformed"
        required = {
            "totalTokens", "inputTokens", "outputTokens", "thoughtTokens",
            "cachedReadTokens", "cachedWriteTokens",
        }
        if set(detail) != required:
            return None, "usage_incomplete" if set(detail) < required else "usage_malformed"
        values = {key: _usage_count(detail[key]) for key in required}
        if any(value is None for value in values.values()):
            return None, "usage_malformed"
        total = values["totalTokens"]
        input_tokens = values["inputTokens"]
        output = values["outputTokens"]
        thought = values["thoughtTokens"]
        cached = values["cachedReadTokens"]
        cache_write = values["cachedWriteTokens"]
        if any(value is None for value in (
            total, input_tokens, output, thought, cached, cache_write,
        )):
            return None, "usage_malformed"
        assert total is not None and input_tokens is not None and output is not None
        assert thought is not None and cached is not None and cache_write is not None
        if total != input_tokens + output:
            return None, "usage_malformed"
        if cached or cache_write:
            return None, "usage_incomplete"
        return _closed_usage(
            input_tokens, cached, cache_write, output, thought, None,
            wall_seconds, None,
        )

    events, event_error = _usage_events(native)
    if event_error is not None or events is None:
        return None, event_error
    if launcher == "claude":
        if len(events) != 1:
            return None, "usage_malformed"
        result = events[0]
        if not _claude_success_result_is_valid(result):
            return None, "usage_malformed"
        detail = result.get("usage")
        if not isinstance(detail, dict):
            return None, "usage_malformed"
        token_fields = {
            "input_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "output_tokens",
        }
        if not token_fields <= set(detail):
            return None, "usage_incomplete"
        if not _claude_usage_envelope_is_valid(detail):
            return None, "usage_malformed"
        return _closed_usage(
            detail["input_tokens"], detail["cache_read_input_tokens"],
            detail["cache_creation_input_tokens"], detail["output_tokens"], 0,
            result.get("total_cost_usd"), wall_seconds, None,
        )
    if launcher == "codex":
        completed = [event for event in events if event.get("type") == "turn.completed"]
        if not completed:
            return None, "usage_not_reported"
        if len(completed) != 1:
            return None, "usage_malformed"
        event = completed[0]
        if set(event) != {"type", "usage"}:
            return None, "usage_malformed"
        detail = event.get("usage")
        if not isinstance(detail, dict):
            return (None, "usage_not_reported") if detail is None else (None, "usage_malformed")
        fields = {
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens",
        }
        if set(detail) != fields:
            return None, "usage_incomplete" if set(detail) < fields else "usage_malformed"
        input_tokens = _usage_count(detail["input_tokens"])
        cached = _usage_count(detail["cached_input_tokens"])
        cache_write = _usage_count(detail["cache_write_input_tokens"])
        if (
            input_tokens is None or cached is None or cache_write is None
            or cached + cache_write > input_tokens
        ):
            return None, "usage_malformed"
        return _closed_usage(
            input_tokens - cached - cache_write, cached, cache_write,
            detail["output_tokens"], detail["reasoning_output_tokens"],
            None, wall_seconds, None,
        )
    return None, "usage_malformed"


def normalize_native_usage(
    launcher: str, native: Any, wall_seconds: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fail closed if a recursive provider/library path exceeds its stack."""
    try:
        return _normalize_native_usage(launcher, native, wall_seconds)
    except RecursionError:
        return None, "usage_malformed"


def record_native_session(job: dict[str, Any], session_id: str | None) -> str | None:
    launcher = str(job.get("launcher") or "")
    if launcher not in NATIVE_LAUNCHERS:
        return None
    identity = job.get("session_identity")
    if not session_id:
        return f"missing structured {launcher} session identity"
    expected = identity.get("native_session_id") if isinstance(identity, dict) else None
    if expected and session_id != expected:
        return f"{launcher} session identity changed during resume"
    if isinstance(identity, dict):
        identity["native_session_id"] = session_id
    return None


def compact_cmd_signal(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "ok"
    blob = "\n".join(
        [
            str(result.get("error") or ""),
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
        ]
    )
    lines = [ln.rstrip() for ln in blob.splitlines() if ln.strip()]
    hits = [ln for ln in lines if COMPACT_FAIL_RE.search(ln)]
    tail = lines[-COMPACT_TAIL_LINES:]
    seen: set[str] = set()
    out: list[str] = []
    for ln in hits + tail:
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    text = "\n".join(out).strip() or "failed (no compact lines)"
    return text[:COMPACT_MAX_CHARS]


def wrap_prompt(job_id: str, prompt: str, cwd: str) -> str:
    root = str(Path(cwd).resolve())
    return (
        f"GRAPHWING_JOB {job_id}\n"
        "You are a Graph node on this laptop, not the story owner. One job only. "
        "Do not research+write+review+ship.\n"
        f"Working directory is {root}. Create and edit files only inside that directory. "
        "Do not write to any other path.\n"
        "Do not stage files. Graphwing stages only paths attributed to this job after your receipt.\n"
        "Do not git commit, git push, or open a PR. Graph owns those ops.\n"
        "Do not `git checkout` another branch. Graph already checked out the job branch.\n"
        "Finish with a single JSON object and nothing after it:\n"
        '{"status":"ok"|"error","sha":null,"pr_url":null,"summary":"<one line>"}\n\n'
        f"Task:\n{prompt}\n"
    )


def parse_receipt_text(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or len(text) > _PROVIDER_OUTPUT_MAX_CHARS:
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    candidates: list[str] = [raw]
    for line in raw.splitlines():
        try:
            event = strict_json_object(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) and event.get("type") == "item.completed" else None
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            candidates.append(item["text"].strip())
        if (
            isinstance(event, dict)
            and event.get("type") == "result"
            and event.get("is_error") is False
            and isinstance(event.get("result"), str)
        ):
            candidates.append(event["result"].strip())
    if "```" in raw:
        for part in raw.split("```"):
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].lstrip()
            if p.startswith("{"):
                candidates.append(p)
    for source in tuple(candidates):
        start = source.rfind("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(source)):
                ch = source[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(source[start : i + 1])
                        break
    for candidate in reversed(candidates):
        try:
            obj = strict_json_object(candidate)
        except ValueError:
            continue
        if obj.get("status") in ("ok", "error", "timeout"):
            return obj
    return None


def structured_provider_failure(text: str) -> str | None:
    """Return an allowlisted provider code from adapter-owned JSON, never prose."""
    if not isinstance(text, str) or len(text) > _PROVIDER_OUTPUT_MAX_CHARS:
        return None
    for line in (text or "").splitlines():
        try:
            event = strict_json_object(line)
        except ValueError:
            continue
        roots: list[dict[str, Any]] = []
        error = event.get("error")
        if isinstance(error, dict):
            roots.append(error)
        if event.get("is_error") is True or event.get("type") in ("error", "request_error"):
            roots.append(event)
        for root in roots:
            pending = [root]
            while pending:
                item = pending.pop()
                for key in ("error", "data"):
                    nested = item.get(key)
                    if isinstance(nested, dict):
                        pending.append(nested)
                for key in ("code", "type", "error_code", "error_type"):
                    value = item.get(key)
                    if isinstance(value, str):
                        token = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
                        for failure_code, tokens in PROVIDER_FAILURE_GROUPS:
                            if token in tokens:
                                return failure_code
                for key in ("status", "status_code", "http_status"):
                    value = item.get(key)
                    try:
                        status = int(value)
                    except (TypeError, ValueError):
                        continue
                    if status == 429:
                        return "provider_rate_limit"
                    if 500 <= status <= 599:
                        return "provider_http_5xx"
    return None


def classify_agent_failure(evidence_code: str, structured_output: str = "") -> dict[str, Any]:
    failure_class = FAILURE_CLASS_BY_CODE.get(evidence_code)
    provider_code = None
    if failure_class in (None, "model_execution", "unknown"):
        provider_code = structured_provider_failure(structured_output)
    if provider_code:
        failure_class, failure_code = "provider_availability", provider_code
    elif failure_class:
        failure_code = "none" if evidence_code == "success" else evidence_code
    else:
        failure_class, failure_code = "unknown", "unknown_failure"
    return {
        "failure_class": failure_class,
        "failure_code": failure_code,
        "failover_eligible": failure_class == "provider_availability",
        "diagnostic": compact_diagnostic(failure_code),
    }


def normalize_receipt(
    job: dict[str, Any],
    parsed: dict[str, Any] | None,
    returncode: int,
    timed_out: bool,
    evidence_code: str | None = None,
    structured_output: str = "",
    *,
    usage: dict[str, Any] | None = None,
    usage_diagnostic: str | None = "usage_not_reported",
) -> dict[str, Any]:
    raw_requested = job.get("requested_effort")
    raw_source = job.get("effort_source")
    requested_effort = raw_requested if raw_requested in EFFORT_VALUES else "default"
    effort_source = raw_source if raw_source in EFFORT_SOURCES else "launcher_default"
    profile, profile_err = normalize_effort(
        str(job.get("launcher") or ""), str(job.get("provider") or ""),
        str(job.get("model") or ""), requested_effort, effort_source,
    )
    identity = job.get("session_identity")
    effective_effort = job.get("effective_effort") or (
        identity.get("effective_effort") if isinstance(identity, dict) else None
    ) or (profile or {}).get("effective_effort") or "default"
    launcher_version = job.get("launcher_version") or (
        identity.get("launcher_version") if isinstance(identity, dict) else None
    ) or "missing"
    parsed_identity, identity_err = parse_session_identity(identity)
    profile_complete = bool(
        job.get("kind") == "agent"
        and raw_requested in EFFORT_VALUES and raw_source in EFFORT_SOURCES
        and profile_err is None and profile is not None
        and job.get("effective_effort") == profile["effective_effort"]
        and isinstance(job.get("launcher_version"), str)
        and LAUNCHER_VERSION_RE.fullmatch(job["launcher_version"])
        and identity_err is None and parsed_identity == identity
        and isinstance(identity, dict)
        and all(identity.get(key) == job.get(key) for key in AGENT_PROFILE_FIELDS)
        and job.get("repo") == identity.get("repo")
        and all(
            key not in job or job.get(key) == identity.get(key)
            for key in ("branch", "starting_head")
        )
    )
    failure: str | None = None
    if timed_out:
        status, failure, evidence = "timeout", "agent run budget exceeded", "run_budget_exceeded"
    elif returncode != 0:
        status, failure, evidence = "error", f"{job.get('launcher') or 'agent'} exited {returncode}", "process_exit"
    elif parsed is None:
        status, failure, evidence = "error", "missing or invalid final receipt", "invalid_receipt"
    else:
        status = str(parsed.get("status") or "error")
        evidence = "success" if status == "ok" else (
            "reported_timeout" if status == "timeout" else "model_reported_error"
        )
    if not profile_complete:
        status, failure, evidence = (
            "error", "execution profile provenance is incomplete", "session_provenance_invalid",
        )
    if status == "ok" and job.get("launcher") in NATIVE_LAUNCHERS and (
        not isinstance(identity, dict) or not identity.get("native_session_id")
    ):
        status, failure, evidence = (
            "error", f"missing structured {job.get('launcher')} session identity",
            "session_identity_missing",
        )
    if not (
        (valid_normalized_usage(usage) and usage_diagnostic is None)
        or (
            usage is None
            and usage_diagnostic in {
                "usage_not_reported", "usage_incomplete", "usage_malformed",
            }
        )
    ):
        usage, usage_diagnostic = None, "usage_malformed"
    rec = {
        "status": status,
        "job_id": job["job_id"],
        "launcher": job.get("launcher") if profile_complete else None,
        "provider": job.get("provider") if profile_complete else None,
        "model": job.get("model") if profile_complete else None,
        "session_identity": parsed_identity if profile_complete else None,
        "requested_effort": requested_effort if profile_complete else None,
        "effective_effort": effective_effort if profile_complete else None,
        "effort_source": effort_source if profile_complete else None,
        "launcher_version": launcher_version if profile_complete else None,
        "usage": json.loads(json.dumps(usage)) if usage is not None else None,
        "usage_diagnostic": usage_diagnostic,
    }
    rec.update(classify_agent_failure(evidence_code or evidence, structured_output))
    rec["summary"] = rec["diagnostic"]["summary"]
    return rec


def post_receipt(url: str, receipt: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Rewst-Token"] = token
    try:
        data = json.dumps(receipt).encode()
    except (RecursionError, MemoryError, TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "receipt serialization failed"}
    req = Request(url, data=data, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            return {"ok": 200 <= getattr(resp, "status", 200) < 300, "status": getattr(resp, "status", 200)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def deliver_webhook(job: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any] | None:
    url = job.get("response_webhook_url") or job.get("resume_url")
    if not url:
        return None
    return post_receipt(str(url), receipt, token=job.get("response_webhook_token"))


def native_job_env(job: dict[str, Any]) -> dict[str, str]:
    """Graph cwd wins over inherited terminal state."""
    cwd = str(Path(job["cwd"]).resolve())
    env = {k: v for k, v in os.environ.items() if k not in ("TERMINAL_CWD", "PWD")}
    env.update(
        {
            "GRAPHWING_JOB_ID": str(job["job_id"]),
            "GIT_TERMINAL_PROMPT": "0",
            "GH_PROMPT_DISABLED": "1",
            "TERMINAL_CWD": cwd,
            "PWD": cwd,
        }
    )
    if job.get("codeoff_workspace"):
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def launcher_pass_fds(binary: Path) -> tuple[int, ...]:
    match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(binary))
    return (int(match.group(1)),) if match else ()


def claude_command(
    job: dict[str, Any], prompt: str | None, cwd: str, binary: Path | None = None, *,
    permission_mode: Literal["acceptEdits", "plan"] = "acceptEdits",
    output_format: Literal["json", "text"] = "json",
) -> list[str]:
    """Build one `claude -p` argv. A None prompt means the caller writes stdin.

    Linux MAX_ARG_STRLEN rejects a single argv string of 131,072 bytes or more,
    so a maximum-sized review prompt cannot travel as the trailing argument.
    `claude -p` takes its input from either that argument or stdin, and refuses
    to start with neither, so passing None here obliges the caller to supply the
    prompt on the child's stdin.
    """
    binary = binary or CLAUDE_BIN
    valid, effort = native_effort_value(job)
    if not valid:
        raise ValueError("unsupported effort profile")
    command = [
        str(binary),
        "-p",
        "--output-format",
        output_format,
        "--permission-mode",
        permission_mode,
        "--max-turns",
        str(job["max_turns"]),
        "--add-dir", cwd,
        "--model", str(job["model"]),
    ]
    if permission_mode == "acceptEdits":
        command.extend(["--allowedTools", "Bash"])
    if effort is not None:
        command.extend(["--effort", effort])
    session_id = (job.get("session_identity") or {}).get("native_session_id")
    if session_id:
        command.extend(["--resume", str(session_id)])
    if prompt is not None:
        command.append(prompt)
    return command


def spawn_claude(
    job: dict[str, Any], binary: Path | None = None
) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    binary = binary or CLAUDE_BIN
    if not binary.is_file():
        return None, {"error": f"missing claude binary: {binary}", "code": "missing_binary"}
    prompt_path = job_dir(job["job_id"]) / "prompt.txt"
    stdout_path = job_dir(job["job_id"]) / "stdout.log"
    stderr_path = job_dir(job["job_id"]) / "stderr.log"
    try:
        prompt = prompt_path.read_text()
    except OSError as exc:
        return None, {"error": f"missing prompt: {exc}", "code": "missing_prompt"}
    stdout_f = stdout_path.open("wb")
    stderr_f = stderr_path.open("wb")
    cwd = str(Path(job["cwd"]).resolve())
    env = {k: v for k, v in os.environ.items()}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "PWD": cwd})
    if job.get("codeoff_workspace"):
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        cmd = claude_command(job, prompt, cwd, binary)
    except ValueError:
        stdout_f.close()
        stderr_f.close()
        return None, {"error": "unsupported effort profile", "code": "unsupported_effort"}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_f,
            stderr=stderr_f,
            cwd=cwd,
            env=env,
            start_new_session=True,
            pass_fds=launcher_pass_fds(binary),
        )
    except OSError as exc:
        stdout_f.close()
        stderr_f.close()
        return None, {"error": f"spawn failed: {exc}", "code": "spawn_failed"}
    stdout_f.close()
    stderr_f.close()
    return proc, None


def codex_command(
    job: dict[str, Any], prompt_path: Path, cwd: str, binary: Path | None = None, *,
    sandbox: Literal["workspace-write", "read-only"] = "workspace-write",
) -> list[str]:
    binary = binary or CODEX_BIN
    valid, effort = native_effort_value(job)
    if not valid or effort is None:
        raise ValueError("unsupported effort profile")
    # Codex 0.151.0 enables features.code_mode_host by default and resolves
    # codex-code-mode-host as a sibling of its own executable. Graphwing execs
    # the sealed memfd copy at /proc/self/fd/N, whose sibling is
    # /codex-code-mode-host, so the host launch fails before any work starts.
    # Pinning the launcher is the invariant, so the feature is turned off here.
    command = [
        str(binary), "exec", "--json", "--model", str(job["model"]),
        "-c", f"model_reasoning_effort={effort}",
        "-c", "features.code_mode_host=false",
        "-C", cwd, "--sandbox", sandbox,
        "--output-last-message", str(prompt_path.parent / "last-message.txt"),
    ]
    session_id = (job.get("session_identity") or {}).get("native_session_id")
    if session_id:
        command.extend(["resume", str(session_id)])
    command.append("-")
    return command


def spawn_codex(
    job: dict[str, Any], binary: Path | None = None
) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    binary = binary or CODEX_BIN
    if not binary.is_file():
        return None, {"error": f"missing codex binary: {binary}", "code": "missing_binary"}
    prompt_path = job_dir(job["job_id"]) / "prompt.txt"
    stdout_path = job_dir(job["job_id"]) / "stdout.log"
    stderr_path = job_dir(job["job_id"]) / "stderr.log"
    cwd = str(Path(job["cwd"]).resolve())
    stdin_f = prompt_path.open("rb")
    stdout_f = stdout_path.open("wb")
    stderr_f = stderr_path.open("wb")
    try:
        command = codex_command(job, prompt_path, cwd, binary)
    except ValueError:
        stdin_f.close()
        stdout_f.close()
        stderr_f.close()
        return None, {"error": "unsupported effort profile", "code": "unsupported_effort"}
    try:
        proc = subprocess.Popen(
            command, stdin=stdin_f, stdout=stdout_f, stderr=stderr_f,
            cwd=cwd, env=native_job_env(job), start_new_session=True,
            pass_fds=launcher_pass_fds(binary),
        )
    except OSError as exc:
        stdin_f.close()
        stdout_f.close()
        stderr_f.close()
        return None, {"error": f"spawn failed: {exc}", "code": "spawn_failed"}
    stdin_f.close()
    stdout_f.close()
    stderr_f.close()
    return proc, None


def grok_acp_command(
    job: dict[str, Any], binary: Path | None = None, *,
    mode: Literal["writer", "review"],
) -> list[str]:
    if mode not in ("writer", "review"):
        raise ValueError("invalid Grok ACP mode")
    binary = binary or GROK_BIN
    valid, effort = native_effort_value(job)
    if not valid or effort is None:
        raise ValueError("unsupported effort profile")
    command = [str(binary), "agent"]
    if mode == "writer":
        command.append("--always-approve")
    command.extend([
        "--model", str(job["model"]), "--reasoning-effort", effort, "stdio",
    ])
    return command


def run_grok_acp(
    job: dict[str, Any], binary: Path | None = None, *,
    mode: Literal["writer", "review"],
) -> tuple[int, bool, str | None, str | None]:
    """Run one bounded ACP writer or read-only review turn."""
    if mode not in ("writer", "review"):
        job["_adapter_failure_code"] = "adapter_contract_invalid"
        return 1, False, None, "invalid Grok ACP mode"
    binary = binary or GROK_BIN
    path = job_dir(job["job_id"])
    stdout_path = path / "stdout.log"
    stderr_path = path / "stderr.log"
    cwd = str(Path(job["cwd"]).resolve())
    env = native_job_env(job)
    env["GROK_DISABLE_AUTOUPDATER"] = "1"
    try:
        command = grok_acp_command(job, binary, mode=mode)
    except ValueError as exc:
        job["_adapter_failure_code"] = (
            "adapter_contract_invalid" if str(exc) == "invalid Grok ACP mode" else "unsupported_effort"
        )
        return 1, False, None, str(exc)
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, start_new_session=True, bufsize=0,
            pass_fds=launcher_pass_fds(binary),
        )
    except OSError as exc:
        job["_adapter_failure_code"] = "spawn_failed"
        return 1, False, None, f"spawn failed: {exc}"
    job["pid"] = proc.pid
    if mode == "writer":
        snapshot = agent_execution_snapshot(job)
        if snapshot is None or not agent_update_running(job["job_id"], snapshot, {"pid": proc.pid}):
            job["_adapter_failure_code"] = "session_provenance_invalid"
            _kill_proc(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            return 1, False, None, "persisted agent execution identity changed during execution"
    else:
        write_job(job)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    def drain_stderr() -> None:
        remaining = FILE_MAX_BYTES
        with stderr_path.open("wb") as log:
            while True:
                part = os.read(proc.stderr.fileno(), 4096)
                if not part:
                    return
                if remaining:
                    kept = part[:remaining]
                    log.write(kept)
                    remaining -= len(kept)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    deadline = time.monotonic() + int(job.get("run_budget_seconds") or AGENT_RUN_BUDGET)
    request_id = 0
    wire = b""
    logged_bytes = 0
    chunks: list[str] = []
    chunk_bytes = 0
    session_id: str | None = (job.get("session_identity") or {}).get("native_session_id")
    pending_session_id: str | None = None

    def write_message(message: dict[str, Any]) -> None:
        payload = (json.dumps(message) + "\n").encode()
        sent = 0
        while sent < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [proc.stdin], [], remaining)[1]:
                raise TimeoutError("Grok ACP request timed out")
            written = os.write(proc.stdin.fileno(), payload[sent : sent + 4096])
            if not written:
                raise BrokenPipeError("Grok ACP stdin closed")
            sent += written

    def read_message(log: Any) -> dict[str, Any]:
        nonlocal wire, logged_bytes
        while b"\n" not in wire:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                raise TimeoutError("Grok ACP request timed out")
            part = os.read(proc.stdout.fileno(), 4096)
            if not part:
                code = proc.poll()
                raise RuntimeError(f"Grok ACP process exited {code if code is not None else 'early'}")
            wire += part
            if b"\n" not in wire and len(wire) > CMD_MAX_BYTES:
                raise ValueError("Grok ACP wire exceeded output limit")
        raw, wire = wire.split(b"\n", 1)
        if len(raw) > CMD_MAX_BYTES:
            raise ValueError("Grok ACP wire exceeded output limit")
        entry = raw + b"\n"
        if logged_bytes + len(entry) <= CMD_MAX_BYTES:
            log.write(entry)
            log.flush()
            logged_bytes += len(entry)
        try:
            message = strict_json_object(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("malformed Grok ACP wire") from exc
        if message.get("jsonrpc") != "2.0":
            raise ValueError("malformed Grok ACP wire")
        return message

    def request(log: Any, method: str, params: dict[str, Any], collect: bool = False) -> dict[str, Any]:
        nonlocal request_id, chunk_bytes, pending_session_id
        request_id += 1
        write_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = read_message(log)
            if message.get("method") == "session/request_permission" and mode == "review":
                permission_id = message.get("id")
                if type(permission_id) not in (int, str):
                    job["_adapter_failure_code"] = "adapter_contract_invalid"
                    raise ValueError("malformed Grok review permission request")
                write_message({
                    "jsonrpc": "2.0",
                    "id": permission_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
                job["_adapter_failure_code"] = "adapter_contract_invalid"
                raise RuntimeError("Grok review requested tool permission")
            if message.get("method") == "session/update":
                if any(key in message for key in ("id", "result", "error")):
                    raise ValueError("malformed Grok session update")
                update_params = message.get("params")
                update = update_params.get("update") if isinstance(update_params, dict) else None
                if (
                    not isinstance(update, dict)
                    or not isinstance(update.get("sessionUpdate"), str)
                ):
                    job["_adapter_failure_code"] = "session_identity_mismatch"
                    raise ValueError("malformed or changed Grok session update")
                update_session_id = update_params.get("sessionId")
                if method == "session/new" and session_id is None:
                    if (
                        not isinstance(update_session_id, str)
                        or not NATIVE_SESSION_RE.fullmatch(update_session_id)
                    ):
                        job["_adapter_failure_code"] = "session_identity_mismatch"
                        raise ValueError("malformed or changed Grok session update")
                    if pending_session_id is None:
                        pending_session_id = update_session_id
                    elif update_session_id != pending_session_id:
                        job["_adapter_failure_code"] = "session_identity_mismatch"
                        raise ValueError("malformed or changed Grok session update")
                elif session_id is None or update_session_id != session_id:
                    job["_adapter_failure_code"] = "session_identity_mismatch"
                    raise ValueError("malformed or changed Grok session update")
                if update.get("sessionUpdate") == "agent_message_chunk":
                    content = update.get("content")
                    text = content.get("text") if isinstance(content, dict) else None
                    if not isinstance(content, dict) or content.get("type") != "text" or not isinstance(text, str):
                        raise ValueError("malformed Grok agent message chunk")
                    if collect:
                        chunk_bytes += len(text.encode())
                        if chunk_bytes > FILE_MAX_BYTES:
                            raise ValueError("Grok assistant output exceeded limit")
                        chunks.append(text)
                continue
            if message.get("method") in GROK_VENDOR_NOTIFICATION_METHODS:
                if any(key in message for key in ("id", "result", "error")):
                    raise ValueError("malformed Grok vendor notification")
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                raise ValueError("unexpected Grok ACP response")
            if ("result" in message) == ("error" in message):
                raise ValueError(f"invalid Grok ACP {method} response")
            if "error" in message:
                job["_adapter_failure_code"] = "unknown_failure"
                raise RuntimeError(f"Grok ACP {method} error")
            result = message["result"]
            if not isinstance(result, dict):
                raise ValueError(f"invalid Grok ACP {method} result")
            return result

    timed_out = False
    error: str | None = None
    returncode = 1
    try:
        with stdout_path.open("wb") as stdout_f:
            initialized = request(stdout_f, "initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            })
            if type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1:
                raise ValueError("unsupported Grok ACP protocol version")
            auth_methods = initialized.get("authMethods", [])
            if not isinstance(auth_methods, list):
                raise ValueError("malformed Grok ACP authentication methods")
            method_by_id: dict[str, dict[str, Any]] = {}
            for method in auth_methods:
                method_id = method.get("id") if isinstance(method, dict) else None
                if (
                    not isinstance(method_id, str)
                    or not method_id.strip()
                    or method_id in method_by_id
                ):
                    raise ValueError("malformed Grok ACP authentication methods")
                method_by_id[method_id] = method
            if auth_methods:
                method_id = "xai.api_key" if os.environ.get("XAI_API_KEY") else "cached_token"
                selected = method_by_id.get(method_id)
                if selected is None or selected.get("type", "agent") != "agent":
                    job["_adapter_failure_code"] = "adapter_contract_invalid"
                    raise RuntimeError("unsupported Grok ACP authentication")
                request(stdout_f, "authenticate", {"methodId": method_id, "_meta": {"headless": True}})
            if session_id:
                capabilities = initialized.get("agentCapabilities")
                if not isinstance(capabilities, dict) or capabilities.get("loadSession") is not True:
                    job["_adapter_failure_code"] = "session_provenance_invalid"
                    raise RuntimeError("Grok ACP does not support session/load")
                loaded = request(stdout_f, "session/load", {
                    "sessionId": session_id, "cwd": cwd, "mcpServers": [],
                })
                if loaded.get("sessionId") not in (None, session_id):
                    job["_adapter_failure_code"] = "session_identity_mismatch"
                    raise ValueError("Grok session identity changed during load")
            else:
                created = request(stdout_f, "session/new", {"cwd": cwd, "mcpServers": []})
                candidate = created.get("sessionId")
                if not isinstance(candidate, str) or not NATIVE_SESSION_RE.fullmatch(candidate.strip()):
                    job["_adapter_failure_code"] = "session_identity_missing"
                    raise ValueError("missing structured Grok session identity")
                candidate = candidate.strip()
                if pending_session_id is not None and candidate != pending_session_id:
                    job["_adapter_failure_code"] = "session_identity_mismatch"
                    raise ValueError("Grok session identity changed during creation")
                session_id = candidate
                pending_session_id = None
            prompted = request(stdout_f, "session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": path.joinpath("prompt.txt").read_text()}],
            }, collect=True)
            job["_native_usage"] = prompted
            if prompted.get("stopReason") != "end_turn":
                job["_adapter_failure_code"] = (
                    "cancelled" if prompted.get("stopReason") in ("cancelled", "canceled")
                    else "model_stopped"
                )
                raise RuntimeError("Grok ACP prompt did not end_turn")
            final_message = "".join(chunks)
            path.joinpath("last-message.txt").write_text(final_message)
            parsed = parse_receipt_text(final_message)
            if not (
                isinstance(parsed, dict)
                and set(parsed) == {"status", "sha", "pr_url", "summary"}
                and parsed["status"] in ("ok", "error", "timeout")
                and (parsed["sha"] is None or isinstance(parsed["sha"], str))
                and (parsed["pr_url"] is None or isinstance(parsed["pr_url"], str))
                and isinstance(parsed["summary"], str)
                and bool(parsed["summary"].strip())
            ):
                job["_adapter_failure_code"] = "invalid_receipt"
                raise ValueError("missing or invalid final receipt")
            returncode = 0
    except TimeoutError as exc:
        timed_out, error = True, str(exc)
    except ValueError as exc:
        job.setdefault("_adapter_failure_code", "adapter_contract_invalid")
        error = str(exc)
        code = proc.poll()
        if code not in (None, 0):
            returncode = code
    except (BrokenPipeError, OSError, RuntimeError) as exc:
        job.setdefault("_adapter_failure_code", "process_exit")
        error = str(exc)
        code = proc.poll()
        if code not in (None, 0):
            returncode = code
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        killed_for_teardown = False
        try:
            exit_code = proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            killed_for_teardown = True
            _kill_proc(proc)
            exit_code = proc.returncode
        if not killed_for_teardown and exit_code not in (None, 0):
            returncode = exit_code
            if error is None:
                error = f"Grok ACP process exited {exit_code}"
        proc.stdout.close()
        stderr_thread.join(timeout=1)
        proc.stderr.close()
        if error:
            summary = (error + "\n").encode()[-FILE_MAX_BYTES:]
            with stderr_path.open("a+b") as fh:
                fh.seek(0, os.SEEK_END)
                fh.truncate(min(fh.tell(), FILE_MAX_BYTES - len(summary)))
                fh.write(summary)
    return returncode, timed_out, session_id, error


def spawn_writer(
    job: dict[str, Any], binary: Path | None = None
) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    if job.get("launcher") == "codex":
        return spawn_codex(job, binary)
    if job.get("launcher") == "claude":
        return spawn_claude(job, binary)
    return None, {"error": "unknown native launcher", "code": "bad_launcher"}


def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=AGENT_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=AGENT_KILL_GRACE_SECONDS)


def read_bounded_output(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            first = fh.read(FILE_MAX_BYTES)
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size <= len(first):
                data = first
            else:
                fh.seek(max(len(first), size - CMD_MAX_BYTES))
                data = first + b"\n" + fh.read(CMD_MAX_BYTES)
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def _codeoff_jsonl(path: Path) -> list[dict[str, Any]] | None:
    """Read a complete, bounded native JSONL record; partial proof is no proof."""
    try:
        if not path.is_file() or path.stat().st_size > CODEOFF_PROVENANCE_MAX_BYTES:
            return None
        events: list[dict[str, Any]] = []
        total = 0
        with path.open("rb") as stream:
            for raw in stream:
                total += len(raw)
                if total > CODEOFF_PROVENANCE_MAX_BYTES or len(events) >= CODEOFF_PROVENANCE_MAX_EVENTS:
                    return None
                try:
                    event = strict_json_object(raw)
                except ValueError:
                    return None
                events.append(event)
        return events
    except OSError:
        return None


def _codeoff_provenance_source(launcher: Any) -> str | None:
    return {
        "claude": "claude-result-v1",
        "codex": "codex-rollout-v1",
        "grok": "grok-acp-v1",
    }.get(launcher)


def _codeoff_identity_values(raw: Any, model_key: str = "model") -> tuple[str, str, str] | None:
    if not isinstance(raw, dict):
        return None
    values = (raw.get("launcher"), raw.get("provider"), raw.get(model_key))
    return values if all(isinstance(value, str) and value for value in values) else None  # type: ignore[return-value]


def _codeoff_compact_execution_identity(job: dict[str, Any]) -> dict[str, str] | None:
    session = job.get("session_identity")
    source = _codeoff_provenance_source(job.get("launcher"))
    if (
        not isinstance(session, dict) or source is None
        or _codeoff_identity_values(session) != _codeoff_identity_values(job)
    ):
        return None
    values = {key: session.get(key) for key in ("launcher", "provider", "model", "native_session_id")}
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return {"version": "code-off-execution-identity-v1", "source": source, **values}  # type: ignore[arg-type]


def _codeoff_claude_provenance(job: dict[str, Any]) -> bool:
    events = _codeoff_jsonl(job_dir(job["job_id"]) / "stdout.log")
    results = [event for event in events or [] if event.get("type") == "result"]
    if len(results) != 1:
        return False
    result = results[0]
    session = job.get("session_identity") or {}
    usage = result.get("modelUsage")
    model = job.get("model")
    if not (
        result.get("subtype") == "success" and result.get("is_error") is False
        and result.get("session_id") == session.get("native_session_id")
        and session.get("model") == model
        and isinstance(usage, dict) and set(usage) == {model}
    ):
        return False
    detail = usage.get(model)
    return isinstance(detail, dict) and detail.get("canonicalModel") == model and detail.get("provider") == "firstParty"


def _codeoff_codex_rollout(job: dict[str, Any]) -> Path | None:
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    sessions = home / "sessions"
    native_id = str((job.get("session_identity") or {}).get("native_session_id") or "")
    matches: list[Path] = []
    scanned = 0
    try:
        if not sessions.is_dir() or sessions.is_symlink():
            return None
        def traversal_error(error: OSError) -> None:
            raise error

        for root, dirs, files in os.walk(sessions, onerror=traversal_error, followlinks=False):
            scanned += len(dirs) + len(files)
            if scanned > CODEOFF_PROVENANCE_MAX_FILES:
                return None
            for name in files:
                if name.endswith(".jsonl") and Path(name).stem.endswith(native_id):
                    matches.append(Path(root) / name)
                    if len(matches) > 1:
                        return None
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _codeoff_codex_provenance(job: dict[str, Any]) -> bool:
    rollout = _codeoff_codex_rollout(job)
    events = _codeoff_jsonl(rollout) if rollout else None
    if events is None:
        return False
    metas = [event.get("payload") for event in events if event.get("type") == "session_meta"]
    turns = [event.get("payload") for event in events if event.get("type") == "turn_context"]
    session = job.get("session_identity") or {}
    if len(metas) != 1 or not turns or not isinstance(metas[0], dict):
        return False
    meta = metas[0]
    git = meta.get("git")
    cwd, model = str(Path(job["cwd"]).resolve()), job.get("model")
    if not (
        meta.get("id") == session.get("native_session_id")
        and meta.get("originator") == "codex_exec" and meta.get("source") == "exec"
        and meta.get("model_provider") in ("openai", "openai-api")
        and meta.get("cwd") == cwd
        and isinstance(git, dict) and git.get("commit_hash") == session.get("starting_head")
    ):
        return False
    return all(isinstance(turn, dict) and turn.get("cwd") == cwd and turn.get("model") == model for turn in turns)


def _codeoff_execution_identity(
    job: dict[str, Any], adapter_evidence: Any
) -> dict[str, str] | None:
    """Return only the compact receipt after launcher-native proof succeeds."""
    launcher = job.get("launcher")
    if _codeoff_provenance_source(launcher) is None or adapter_evidence is not None:
        return None
    compact = _codeoff_compact_execution_identity(job)
    if compact is None:
        return None
    if launcher == "claude" and not _codeoff_claude_provenance(job):
        return None
    if launcher == "codex" and not _codeoff_codex_provenance(job):
        return None
    return compact


def staging_failure_summary(failure: dict[str, Any]) -> str:
    summary = str(failure.get("error") or "Graphwing could not verify or stage writer paths")[:300]
    raw_paths = failure.get("paths") if isinstance(failure.get("paths"), list) else []
    paths = []
    for raw in raw_paths[:3]:
        safe = "".join(char if char.isprintable() and char not in "\r\n" else "?" for char in str(raw))[:80]
        if safe:
            paths.append(safe)
    return (summary + (": " + ", ".join(paths) if paths else ""))[:500]


def run_agent_job(job_id: str) -> None:
    job = read_job(job_id)
    if not isinstance(job, dict) or job.get("kind") != "agent":
        return
    if not is_agent_job_record(job):
        expected = json.loads(json.dumps(job))
        failure = normalize_receipt(
            job, {"status": "error"}, 1, False,
            evidence_code="session_provenance_invalid",
        )
        with job_record_lock(job_id):
            current = _read_job_unlocked(job_id)
            if current != expected:
                return
            current.update({
                "status": "failed", "started_at": current.get("started_at") or utcnow(),
                "finished_at": utcnow(), "error": "agent job provenance is invalid",
                "receipt": failure,
            })
            _write_job_unlocked(current)
            job = current
        hook = deliver_webhook(job, job["receipt"])
        if hook is not None:
            with job_record_lock(job_id):
                current = _read_job_unlocked(job_id)
                if (
                    isinstance(current, dict) and current.get("kind") == "agent"
                    and current.get("status") == "failed"
                    and current.get("receipt") == job.get("receipt")
                ):
                    current["webhook"] = hook
                    _write_job_unlocked(current)
        herdr_job_done(job)
        return
    execution_snapshot = agent_execution_snapshot(job)
    assert execution_snapshot is not None
    claimed = agent_mark_running(job_id, execution_snapshot)
    if claimed is None:
        return
    job = claimed
    usage_started = time.monotonic()
    binary = resolve_launcher_binary_now(str(job["launcher"]))
    try:
        with pinned_launcher(binary) as pinned:
            if pinned.fingerprint != job.get("launcher_version"):
                proc, err = None, {
                    "error": "launcher version changed after request validation",
                    "code": "launcher_version_mismatch",
                }
            elif job.get("launcher") == "grok":
                returncode, timed_out, session_id, protocol_error = run_grok_acp(
                    job, pinned.path, mode="writer"
                )
                proc, err = None, None
            else:
                proc, err = spawn_writer(job, pinned.path)
    except FileNotFoundError:
        proc, err = None, {"error": "configured launcher is missing", "code": "missing_binary"}
    except OSError:
        proc, err = None, {"error": "launcher artifact could not be pinned", "code": "launcher_version_mismatch"}
    if err:
        usage, usage_diagnostic = normalize_native_usage(
            str(job.get("launcher") or ""), None,
            time.monotonic() - usage_started,
        )
        receipt = normalize_receipt(
            job, {"status": "error", "summary": err.get("error")}, 1, False,
            evidence_code=str(err.get("code") or "unknown_failure"),
            usage=usage, usage_diagnostic=usage_diagnostic,
        )
        stored, transitioned = agent_terminal_transition(
            job_id, execution_snapshot, receipt, None, 1, False,
            str(err.get("error") or "agent launch failed"),
        )
        if isinstance(stored, dict) and stored.get("kind") == "agent":
            terminal_receipt = stored.get("receipt")
            if isinstance(terminal_receipt, dict):
                hook = deliver_webhook(stored, terminal_receipt)
                if hook is not None:
                    agent_store_webhook_result(job_id, execution_snapshot, terminal_receipt, hook)
            herdr_job_done(stored)
        return
    if proc is not None:
        job["pid"] = proc.pid
        if not agent_update_running(job_id, execution_snapshot, {"pid": proc.pid}):
            _kill_proc(proc)
            return
        timed_out = False
        wait_for = int(job.get("run_budget_seconds") or AGENT_RUN_BUDGET) + AGENT_PROCESS_WAIT_GRACE_SECONDS
        try:
            returncode = proc.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_proc(proc)
            returncode = proc.returncode if proc.returncode is not None else -9
        session_id = None
        protocol_error = None
    stdout = read_bounded_output(job_dir(job_id) / "stdout.log")
    final_message = read_bounded_output(job_dir(job_id) / "last-message.txt")
    parsed = parse_receipt_text(final_message) or parse_receipt_text(stdout)
    session_id = session_id or parse_native_session_id(stdout, str(job.get("launcher") or ""))
    session_error: str | None = None
    session_evidence: str | None = None
    if (returncode == 0 and not timed_out) or (session_id and structured_provider_failure(stdout)):
        expected_session = (job.get("session_identity") or {}).get("native_session_id")
        session_error = record_native_session(job, session_id)
        if session_error:
            session_evidence = (
                "session_identity_mismatch" if expected_session and session_id
                else "session_identity_missing"
            )
    adapter_evidence = job.pop("_adapter_failure_code", None)
    native_usage = job.pop("_native_usage", stdout)
    usage, usage_diagnostic = normalize_native_usage(
        str(job.get("launcher") or ""), native_usage,
        time.monotonic() - usage_started,
    )
    failure_summary = protocol_error if protocol_error and not timed_out else session_error
    receipt = normalize_receipt(
        job, parsed, returncode, timed_out,
        evidence_code=session_evidence or adapter_evidence, structured_output=stdout,
        usage=usage, usage_diagnostic=usage_diagnostic,
    )
    if failure_summary:
        receipt["status"] = "error"
        receipt["summary"] = failure_summary
        job["error"] = failure_summary
    execution_identity = None
    if receipt["status"] == "ok" and job.get("codeoff_workspace"):
        execution_identity = _codeoff_execution_identity(job, adapter_evidence)
        if execution_identity is None:
            receipt["status"] = "error"
            receipt["summary"] = "code-off execution provenance was absent, ambiguous, or mismatched"
            receipt.update(classify_agent_failure("session_provenance_invalid"))
        else:
            receipt["execution_identity"] = execution_identity
    refreshed = read_job(job_id)
    if agent_record_matches_execution_snapshot(refreshed, execution_snapshot):
        job = refreshed
    else:
        receipt["status"] = "error"
        receipt.pop("execution_identity", None)
        execution_identity = None
        receipt.update(classify_agent_failure("session_provenance_invalid"))
        receipt["summary"] = receipt["diagnostic"]["summary"]
        job["error"] = "persisted agent execution identity changed during execution"
    stored, transitioned = agent_terminal_transition(
        job_id, execution_snapshot, receipt, execution_identity, returncode, timed_out,
        str(job["error"]) if job.get("error") else None,
    )
    if not transitioned:
        if isinstance(stored, dict) and stored.get("kind") == "agent":
            failure = stored.get("receipt")
            if isinstance(failure, dict):
                hook = deliver_webhook(job, failure)
                if hook is not None:
                    agent_store_webhook_result(job_id, execution_snapshot, failure, hook)
            herdr_job_done(stored)
        return
    assert isinstance(stored, dict)
    terminal_receipt = stored["receipt"]
    hook = deliver_webhook(stored, terminal_receipt)
    if hook is not None:
        agent_store_webhook_result(job_id, execution_snapshot, terminal_receipt, hook)
        stored = read_job(job_id) or stored
    herdr_job_done(stored)


def enqueue_agent(job: dict[str, Any]) -> None:
    herdr_follow_job(job)
    thread = threading.Thread(target=run_agent_job, args=(job["job_id"],), name=f"graphwing-job-{job['job_id'][:8]}", daemon=True)
    thread.start()


def enqueue_script(job: dict[str, Any]) -> None:
    herdr_follow_job(job)
    thread = threading.Thread(target=run_script_job, args=(job["job_id"],), name=f"graphwing-script-{job['job_id'][:8]}", daemon=True)
    thread.start()


def script_receipt(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    timed_out = result.get("code") == "timeout"
    if timed_out:
        status = "timeout"
        summary = "script timed out"
    elif result.get("ok"):
        status = "ok"
        summary = f"{job.get('script')} exited {result.get('returncode', 0)}"
    else:
        status = "error"
        summary = str(result.get("error") or f"{job.get('script')} failed")[:500]
    return {
        "status": status,
        "job_id": job["job_id"],
        "log_ref": job["log_ref"],
        "summary": summary[:500],
    }


def prepare_merge_evidence(data: dict[str, Any], spec: dict[str, Any], repos: dict[str, str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    fields = ("evidence_mode", "repo", "pr", "run_id", "expected_head", "writer_job_id")
    if not any(data.get(key) not in (None, "") for key in fields):
        return None, None
    required = fields[:-1]
    if data.get("evidence_mode") != "pr_merge" or any(not isinstance(data.get(key), str) or not data[key].strip() for key in required):
        return None, {"error": "complete pr_merge evidence context is required", "code": "merge_evidence_incomplete", "status": 400}
    repo_name, resolved = repo_from_body(data, repos)
    if repo_name is None:
        return None, {**resolved, "status": 400}
    assert isinstance(resolved, Path)
    number, run_id, expected = data["pr"].strip(), data["run_id"].strip(), data["expected_head"].strip()
    writer = data.get("writer_job_id")
    if not number.isdigit() or not GIT_SHA_RE.fullmatch(expected) or len(run_id) > 200 or (writer not in (None, "") and (not isinstance(writer, str) or not JOB_ID_RE.fullmatch(writer))):
        return None, {"error": "invalid pr_merge evidence context", "code": "bad_merge_evidence", "status": 400}
    if Path(spec["cwd"]).resolve() != resolved.resolve():
        return None, {"error": "named test recipe belongs to another repo", "code": "evidence_recipe_repo_mismatch", "status": 409}
    view = fresh_pr_view(resolved, number)
    if not view.get("ok"):
        return None, {**view, "status": int(view.get("status", 502))}
    vd = view.get("data") or {}
    if str(vd.get("number")) != number or not view.get("remote_ready"):
        return None, {"error": "PR is not remotely ready for final evidence", "code": str(view.get("remote_state") or "pr_state_invalid"), "status": 409}
    if vd.get("headRefOid") != expected:
        return None, {"error": "declared PR head is stale", "code": "head_moved", "status": 409}
    if writer not in (None, ""):
        writer_err = validate_writer_evidence(
            writer, repo_name, resolved, str(vd.get("headRefName") or "")
        )
        if writer_err:
            return None, {**writer_err, "status": 409}
    evidence = {"version": PR_EVIDENCE_VERSION,
                "writer_evidence_mode": "writer" if writer not in (None, "") else "initial_green",
                "repo": repo_name, "cwd": str(resolved.resolve()), "pr": number,
                "run_id": run_id, "recipe": str(spec["name"]), "expected_head": expected, "live_head": vd["headRefOid"],
                "head_ref": vd["headRefName"], "start": None, "final": None}
    if writer not in (None, ""):
        evidence["writer_job_id"] = writer
    return evidence, None


def execute_script_job(job: dict[str, Any]) -> dict[str, Any]:
    evidence = job.get("merge_evidence")
    if not isinstance(evidence, dict):
        return run_cmd(list(job["argv"]), cwd=Path(job["cwd"]), timeout=int(job["timeout_seconds"]))
    with repo_lock(Path(job["cwd"])):
        before, err = evidence_git_state(Path(job["cwd"]))
        if err or before != evidence.get("start"):
            result = {"ok": False, "returncode": 1, "stdout": "", "stderr": "", "error": "merge-evidence start state moved", "code": "evidence_state_changed"}
        else:
            result = run_cmd(list(job["argv"]), cwd=Path(job["cwd"]), timeout=int(job["timeout_seconds"]))
        final, final_err = evidence_git_state(Path(job["cwd"]))
        evidence["final"] = final
        if final_err or final != evidence.get("start"):
            result = {**result, "ok": False, "error": "merge-evidence test changed git state", "code": "evidence_state_changed"}
    return result


def run_script_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    result = execute_script_job(job)
    write_job(job)
    jdir = job_dir(job_id)
    try:
        (jdir / "stdout.log").write_text(result.get("stdout") or "")
        (jdir / "stderr.log").write_text(result.get("stderr") or result.get("error") or "")
    except OSError:
        pass
    receipt = script_receipt(job, result)
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["returncode"] = result.get("returncode")
    job["receipt"] = receipt
    job["status"] = "completed" if receipt["status"] == "ok" else "failed"
    if receipt["status"] != "ok":
        job["error"] = receipt.get("summary")
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def named_cmd_run(
    kind: str,
    catalog: dict[str, dict[str, Any]],
    body: bytes,
    catalog_name: str,
    unknown_code: str,
    repos: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required", "code": "missing_name"}
    name = name.strip()
    if data.get("argv") is not None:
        return 400, {"error": f"argv is not accepted; use a {catalog_name} name", "code": "argv_forbidden"}
    allowed = {"name", "response_webhook_url", "response_webhook_token", "resume_url"}
    if kind == "test": allowed.update({"evidence_mode", "repo", "pr", "run_id", "expected_head", "writer_job_id"})
    unexpected = sorted(set(data) - allowed)
    if unexpected:
        return 400, {"error": "request contains unsupported fields", "code": "unexpected_fields", "fields": unexpected}
    spec = catalog.get(name)
    if spec is None:
        return 400, {"error": f"unknown {kind} '{name}'", "code": unknown_code, "allowed": sorted(catalog)}
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    evidence, evidence_err = prepare_merge_evidence(data, spec, repos) if kind == "test" else (None, None)
    if evidence_err:
        return int(evidence_err.pop("status", 400)), evidence_err
    timeout = int(spec["timeout_seconds"])
    async_run = bool(spec["async"]) or timeout > SCRIPT_SYNC_TIMEOUT or evidence is not None or webhook_url is not None
    if not async_run:
        result = run_cmd(list(spec["argv"]), cwd=spec["cwd"], timeout=timeout)
        if result.get("code") == "local_binary_missing":
            return 501, {"error": result.get("error"), "code": "not_implemented"}
        if result.get("code") == "timeout":
            return 504, {
                "ok": False,
                "name": name,
                "kind": kind,
                "error": result.get("error"),
                "code": "timeout",
                "stdout": result.get("stdout") or "",
                "stderr": result.get("stderr") or "",
                "truncated": bool(result.get("truncated")),
                "compact": compact_cmd_signal(result),
            }
        return 200, {
            "ok": bool(result.get("ok")),
            "name": name,
            "kind": kind,
            "returncode": result.get("returncode"),
            "stdout": result.get("stdout") or "",
            "stderr": result.get("stderr") or "",
            "truncated": bool(result.get("truncated")),
            "compact": compact_cmd_signal(result),
        }
    with (repo_lock(Path(spec["cwd"])) if evidence is not None else nullcontext()), JOB_LOCK:
        if evidence is not None:
            start, state_err = evidence_git_state(Path(spec["cwd"]))
            if state_err:
                return 409, state_err
            if not start["clean"]:
                return 409, {"error": "merge-evidence test must start clean", "code": "dirty_evidence_start"}
            if start["branch"] != evidence["head_ref"] or start["sha"] != evidence["live_head"]:
                return 409, {"error": "local checkout does not match live PR head", "code": "evidence_checkout_mismatch"}
            evidence["start"] = start
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        log_ref = str(job_dir(job_id) / "stdout.log")
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "script": name,
            "argv": list(spec["argv"]),
            "cwd": str(spec["cwd"]),
            "timeout_seconds": timeout,
            "response_webhook_url": webhook_url,
            "response_webhook_token": webhook_token,
            "resume_url": webhook_url,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "receipt": None,
            "log_ref": log_ref,
            "error": None,
            "webhook": None,
        }
        if evidence is not None:
            job["merge_evidence"] = evidence
            job["repo"] = evidence["repo"]
        job_dir(job_id).mkdir(parents=True, exist_ok=True)
        write_job(job)
    enqueue_script(job)
    return 202, {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "kind": kind,
        "name": name,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def script_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return named_cmd_run("script", load_scripts(repos), body, "scripts.json", "unknown_script", repos)


def test_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return named_cmd_run("test", load_tests(repos), body, "tests.json", "unknown_test", repos)


def rr_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    catalog = load_rr(repos)
    if not catalog:
        return 501, {"error": "rr is not configured", "code": "not_configured"}
    return named_cmd_run("rr", catalog, body, "rr.json", "unknown_rr", repos)


def codeoff_canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def _codeoff_atomic_bytes(path: Path, data: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if not immutable:
            tmp.replace(path); return
        try:
            os.link(tmp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise RuntimeError(f"immutable record already differs: {path.name}")
    finally:
        tmp.unlink(missing_ok=True)

def _codeoff_atomic_json(path: Path, value: Any, *, immutable: bool = False) -> None:
    _codeoff_atomic_bytes(path, codeoff_canonical_json(value) + b"\n", immutable=immutable)

def _codeoff_id(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(raw, str) or not CODEOFF_EXPERIMENT_RE.fullmatch(raw):
        return None, {"error": "experiment_id must be 8-64 lowercase letters, digits, or hyphens", "code": "bad_experiment_id"}
    return raw, None

def _codeoff_body(body: bytes, allowed: set[str], *, exact: bool = False) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    data, err = parse_json_object(body)
    if err:
        return None, err
    assert data is not None
    unexpected = sorted(set(data) ^ allowed if exact else set(data) - allowed)
    if unexpected:
        return None, {"error": "request has missing or unsupported fields" if exact else "request contains unsupported fields", "code": "unexpected_fields", "fields": unexpected}
    return data, None

def codeoff_workspace_path(experiment_id: str, slot: str) -> Path:
    if not CODEOFF_EXPERIMENT_RE.fullmatch(experiment_id) or not re.fullmatch(r"(?:author-[12]|judge-(?:fable|[12]))", slot):
        raise ValueError("invalid opaque code-off workspace reference")
    return CODEOFF_WORKSPACES_DIR / experiment_id / slot

def _codeoff_root(experiment_id: str) -> Path:
    return CODEOFFS_DIR / experiment_id

def _codeoff_artifact(root: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    _codeoff_atomic_bytes(root / "artifacts" / digest, data, immutable=True)
    return digest

def _codeoff_read_artifact(root: Path, digest: str) -> bytes:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CodeOffArtifactError("invalid artifact hash")
    data = (root / "artifacts" / digest).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise CodeOffArtifactError("artifact hash mismatch")
    return data

def codeoff_slot_work_role(slot: Any) -> str:
    return "author" if isinstance(slot, str) and slot.startswith("author-") else "judge"

def codeoff_identity_snapshot_hash(identity: dict[str, Any]) -> str:
    return hashlib.sha256(codeoff_canonical_json(identity)).hexdigest()

def codeoff_identity_is_pinned(slot: str, identity: Any) -> bool:
    """Require one complete, self-consistent inference identity for a slot."""
    if not isinstance(identity, dict) or not all(
        isinstance(identity.get(field), str) and identity.get(field)
        for field in CODEOFF_IDENTITY_FIELDS
    ):
        return False
    launcher, provider, model = (
        identity["launcher"], identity["provider"], identity["exact_model"]
    )
    profile, error = normalize_effort(
        launcher, provider, model, identity["requested_effort"], CODEOFF_EFFORT_SOURCE,
    ) if identity["requested_effort"] in EFFORT_VALUES else (None, {"code": "bad_effort"})
    return bool(
        launcher in NATIVE_LAUNCHERS
        and LAUNCHER_VERSION_RE.fullmatch(identity["launcher_version"])
        and identity["inference_profile_version"] == CODEOFF_INFERENCE_PROFILE_VERSION
        and identity["requested_effort"] == CODEOFF_SLOT_REQUESTED_EFFORT[codeoff_slot_work_role(slot)]
        and error is None and profile is not None
        and identity["effective_effort"] == profile["effective_effort"]
    )

def codeoff_manifest_identity_error(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Fail closed unless every immutable slot identity is fully pinned."""
    identities = manifest.get("identities")
    snapshots = manifest.get("identity_snapshot_hashes")
    if not isinstance(identities, dict) or not isinstance(snapshots, dict) or set(identities) != set(snapshots):
        return {"error": "code-off slot identities are not fully pinned", "code": "codeoff_identity_unpinned"}
    unpinned = sorted(
        slot for slot, identity in identities.items()
        if not codeoff_identity_is_pinned(slot, identity)
        or snapshots.get(slot) != codeoff_identity_snapshot_hash(identity)
    )
    if (
        unpinned
        or manifest.get("inference_profile_version") != CODEOFF_INFERENCE_PROFILE_VERSION
        or manifest.get("identities_hash") != hashlib.sha256(codeoff_canonical_json(identities)).hexdigest()
    ):
        return {
            "error": "code-off slot identities are not fully pinned",
            "code": "codeoff_identity_unpinned", "slots": unpinned,
        }
    return None

def _codeoff_launch_execution_manifest(job: dict[str, Any]) -> dict[str, Any] | None:
    """Project the complete server-validated launch authority for one slot."""
    workspace = job.get("codeoff_workspace")
    identity = job.get("session_identity")
    required_job_fields = {
        "job_id", "kind", "repo", "branch", "starting_head", "launcher", "provider",
        "model", *AGENT_PROFILE_FIELDS, "launch_native_session_id", "codeoff_workspace",
        "prompt_hash", "max_turns", "run_budget_seconds", "session_identity",
    }
    if not (
        is_agent_job_record(job) and required_job_fields <= job.keys()
        and isinstance(workspace, dict) and isinstance(identity, dict)
        and all(job.get(key) == identity.get(key) for key in ("repo", "branch", "starting_head"))
    ):
        return None
    slot = workspace.get("slot")
    return {
        "version": CODEOFF_EXECUTION_MANIFEST_VERSION,
        "kind": "agent",
        "work_role": "author" if isinstance(slot, str) and slot.startswith("author-") else "judge",
        "experiment_id": workspace.get("experiment_id"),
        "slot": slot,
        "job_id": job.get("job_id"),
        **{key: job.get(key) for key in AGENT_PROFILE_FIELDS},
        "repo": identity.get("repo"),
        "branch": identity.get("branch"),
        "starting_head": identity.get("starting_head"),
        "launch_native_session_id": job.get("launch_native_session_id"),
        "codeoff_workspace": workspace,
        "prompt_hash": job.get("prompt_hash"),
        "max_turns": job.get("max_turns"),
        "run_budget_seconds": job.get("run_budget_seconds"),
    }

def _codeoff_terminal_execution_manifest(
    job: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any] | None:
    launch = _codeoff_launch_execution_manifest(job)
    session = job.get("session_identity")
    execution = job.get("execution_identity")
    if not (
        launch is not None and receipt_profile_matches_job(job, receipt)
        and isinstance(session, dict)
        and isinstance(session.get("native_session_id"), str)
        and NATIVE_SESSION_RE.fullmatch(session["native_session_id"])
        and receipt.get("session_identity") == session
        and isinstance(execution, dict)
        and receipt.get("execution_identity") == execution
    ):
        return None
    return {**launch, "native_session_id": session["native_session_id"], "execution_identity": execution}

def _codeoff_execution_manifest_hashes(
    root: Path, state: dict[str, Any], slot: str, job_id: str
) -> tuple[str, str] | None:
    """Resolve hashes from immutable events and corroborate mutable state."""
    try:
        events = _codeoff_events(root, state)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    launches = [
        event["data"].get("execution_manifest_hash")
        for event in events
        if event.get("kind") == "agent_launched"
        and event.get("data", {}).get("slot") == slot
        and event.get("data", {}).get("job_id") == job_id
    ]
    terminals = [
        event["data"].get("execution_manifest_hash")
        for event in events
        if event.get("kind") == "agent_terminal_manifest"
        and event.get("data", {}).get("slot") == slot
        and event.get("data", {}).get("job_id") == job_id
    ]
    entry = state.get("execution_manifests", {}).get(slot, {}).get(job_id)
    if not (
        len(launches) == len(terminals) == 1
        and isinstance(entry, dict)
        and set(entry) == {"launch_manifest_hash", "terminal_manifest_hash"}
        and entry.get("launch_manifest_hash") == launches[0]
        and entry.get("terminal_manifest_hash") == terminals[0]
        and all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (*launches, *terminals)
        )
    ):
        return None
    return launches[0], terminals[0]

def _codeoff_record_terminal_manifest_locked(
    job: dict[str, Any], receipt: dict[str, Any]
) -> bool:
    """Bind provider-issued session attribution into immutable code-off evidence."""
    workspace = job.get("codeoff_workspace")
    if not isinstance(workspace, dict):
        return True
    root, manifest, state, err = _codeoff_load(workspace.get("experiment_id"), active=True)
    if err or root is None or manifest is None or state is None:
        return False
    slot, job_id = workspace.get("slot"), job.get("job_id")
    entry = state.get("execution_manifests", {}).get(slot, {}).get(job_id)
    launch = _codeoff_launch_execution_manifest(job)
    terminal = _codeoff_terminal_execution_manifest(job, receipt)
    if not (
        isinstance(slot, str) and isinstance(job_id, str)
        and isinstance(entry, dict) and launch is not None and terminal is not None
    ):
        return False
    try:
        launch_hash = hashlib.sha256(codeoff_canonical_json(launch)).hexdigest()
        if entry.get("launch_manifest_hash") != launch_hash:
            return False
        terminal_hash = _codeoff_artifact(root, codeoff_canonical_json(terminal))
        prior = entry.get("terminal_manifest_hash")
        if prior not in (None, terminal_hash):
            return False
        if prior == terminal_hash:
            return True
        entry["terminal_manifest_hash"] = terminal_hash
        _codeoff_commit_event(root, state, "agent_terminal_manifest", {
            "slot": slot, "job_id": job_id,
            "execution_manifest_hash": terminal_hash,
            "launch_manifest_hash": launch_hash,
        })
        return True
    except (OSError, RuntimeError):
        return False

def _codeoff_record_terminal_manifest(job: dict[str, Any]) -> bool:
    """Fixture/recovery seam for an already persisted successful terminal job."""
    receipt = job.get("receipt")
    if not isinstance(receipt, dict):
        return False
    with CODEOFF_LOCK:
        return _codeoff_record_terminal_manifest_locked(job, receipt)

def _codeoff_manifest_current(manifest: dict[str, Any], now: datetime | None = None) -> bool:
    try:
        as_of = datetime.strptime(str(manifest["as_of"]), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        max_age = int(manifest["max_age_days"])
    except (KeyError, TypeError, ValueError):
        return False
    checked = now or datetime.now(timezone.utc)
    age = (checked.date() - as_of.date()).days
    return 0 <= age <= max_age

def _codeoff_rank(seed: bytes, domain: str, values: list[str]) -> list[str]:
    prefix = b"graphwing/code-off/draw-v1\0" + domain.encode() + b"\0"
    canonical = {value: index for index, value in enumerate(values)}
    return sorted(
        values,
        key=lambda value: (
            hmac.new(seed, prefix + value.encode(), hashlib.sha256).digest(),
            canonical[value],
        ),
    )

def codeoff_draw(seed_hex: str) -> dict[str, Any]:
    if not isinstance(seed_hex, str) or not CODEOFF_SEED_RE.fullmatch(seed_hex):
        raise ValueError("seed must be exactly 32 lowercase hex bytes")
    seed = bytes.fromhex(seed_hex)
    authors = _codeoff_rank(seed, "authors", list(CODEOFF_AUTHOR_POOL))[:2]
    remainder = [logical for logical in CODEOFF_AUTHOR_POOL if logical not in authors]
    random_judges = _codeoff_rank(seed, "judges", remainder)[:2]
    return {
        "algorithm_version": CODEOFF_DRAW_VERSION,
        "policy_version": CODEOFF_POLICY_VERSION,
        "ordered_pool": list(CODEOFF_AUTHOR_POOL),
        "authors": authors,
        "fixed_judge": "fable",
        "random_judges": random_judges,
        "seed_commitment": hashlib.sha256(b"graphwing/code-off/seed-commitment/v1\0" + seed).hexdigest(),
    }

def _codeoff_event(root: Path, state: dict[str, Any], kind: str, data: dict[str, Any]) -> str:
    sequence = int(state.get("event_count") or 0) + 1
    event = {
        "sequence": sequence,
        "at": utcnow(),
        "kind": kind,
        "data": data,
        "previous_hash": state.get("event_head") or "0" * 64,
    }
    event["hash"] = hashlib.sha256(codeoff_canonical_json(event)).hexdigest()
    _codeoff_atomic_json(root / "events" / f"{sequence:08d}-{event['hash']}.json", event, immutable=True)
    state["event_count"] = sequence
    state["event_head"] = event["hash"]
    return event["hash"]

def _codeoff_events(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    sequence, current, events = state.get("event_count"), state.get("event_head"), []
    if type(sequence) is not int or sequence < 0 or not isinstance(current, str):
        raise RuntimeError("event chain pointer is invalid")
    while sequence:
        event = json.loads((root / "events" / f"{sequence:08d}-{current}.json").read_text())
        claimed = event.get("hash")
        unhashed = {key: value for key, value in event.items() if key != "hash"}
        if event.get("sequence") != sequence or claimed != current or hashlib.sha256(codeoff_canonical_json(unhashed)).hexdigest() != claimed:
            raise RuntimeError("event chain is invalid")
        events.append(event)
        current, sequence = event.get("previous_hash"), sequence - 1
    if current != "0" * 64:
        raise RuntimeError("event chain genesis is invalid")
    return list(reversed(events))

def _codeoff_load(experiment_id: Any, *, active: bool = False) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    exp, err = _codeoff_id(experiment_id)
    if err:
        return None, None, None, err
    assert exp is not None
    root = _codeoff_root(exp)
    try:
        manifest_bytes = (root / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        state = json.loads((root / "state.json").read_text())
    except FileNotFoundError:
        return None, None, None, {"error": "experiment not found", "code": "experiment_not_found"}
    except (OSError, json.JSONDecodeError):
        return None, None, None, {"error": "experiment record is unreadable", "code": "record_unreadable"}
    if hashlib.sha256(manifest_bytes).hexdigest() != state.get("manifest_file_hash"):
        return None, None, None, {"error": "immutable manifest hash mismatch", "code": "manifest_tampered"}
    try:
        _codeoff_events(root, state)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None, None, None, {"error": "event hash chain is invalid", "code": "event_chain_tampered"}
    final_path = root / "final.json"
    if state.get("finalized"):
        final_bytes = codeoff_canonical_json(_codeoff_final_value(manifest, state)) + b"\n"
        if hashlib.sha256(final_bytes).hexdigest() != state.get("final_file_hash"):
            return None, None, None, {"error": "final record hash differs", "code": "final_tampered"}
        try:
            if not final_path.is_file() or final_path.is_symlink() or final_path.read_bytes() != final_bytes:
                return None, None, None, {"error": "final record differs", "code": "final_tampered"}
        except OSError:
            return None, None, None, {"error": "final record is unreadable", "code": "record_unreadable"}
    elif os.path.lexists(final_path):
        return None, None, None, {"error": "unexpected final record", "code": "final_tampered"}
    if active and state.get("finalized"):
        return None, None, None, {"error": "experiment is finalized and cannot reopen", "code": "experiment_finalized"}
    return root, manifest, state, None

def _codeoff_write_state(root: Path, state: dict[str, Any]) -> None:
    _codeoff_atomic_json(root / "state.json", state)

def _codeoff_commit_event(root: Path, state: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
    _codeoff_event(root, state, kind, data)
    _codeoff_write_state(root, state)

def _codeoff_elapsed(start: str | None, end: str | None) -> float | None:
    try:
        return max(0.0, (datetime.strptime(str(end), "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(str(start), "%Y-%m-%dT%H:%M:%SZ")).total_seconds())
    except (TypeError, ValueError):
        return None

CODEOFF_USAGE_TOKEN_FIELDS = (
    "fresh_input_tokens", "cached_input_tokens", "cache_write_tokens",
    "output_tokens", "reasoning_tokens",
)
# A slot attempt the graph has not driven to a terminal state cannot fund a
# number. That is the one diagnostic code-off adds to the receipt-level closed
# set; deriving the code-off set from `USAGE_DIAGNOSTICS` keeps the published
# `CodeOffEconomicsSlot.usage_diagnostics` enum from drifting away from what
# `_codeoff_slot_usage` can actually emit.
CODEOFF_USAGE_NONTERMINAL = "usage_authority_nonterminal"
CODEOFF_USAGE_DIAGNOSTICS = USAGE_DIAGNOSTICS | {CODEOFF_USAGE_NONTERMINAL}


def _codeoff_slot_usage(job_id: Any) -> tuple[dict[str, Any] | None, str]:
    """Only an authority-sealed terminal agent receipt may fund economics.

    A failed attempt is still a paid-for attempt: `run_agent_job` seals its
    terminal receipt exactly as it seals a successful one, so its exact usage
    belongs in the record. Excluding it would understate real spend and would
    report an ordinary failure as if it were a provenance fault.
    """
    try:
        job = read_job(job_id) if isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id) else None
    except (OSError, ValueError, json.JSONDecodeError):
        job = None
    if not is_agent_job_record(job):
        return None, "usage_authority_unavailable"
    assert isinstance(job, dict)
    if job.get("status") not in ("completed", "failed"):
        return None, CODEOFF_USAGE_NONTERMINAL
    receipt = job.get("receipt")
    if not receipt_profile_matches_job(job, receipt):
        return None, "usage_authority_mismatch"
    usage, diagnostic = authorized_receipt_usage("agent", receipt)
    return usage, diagnostic or "usage_reported"


def _codeoff_usage_totals(attempts: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Sum exact normalized usage; nullable fields never become a false zero.

    Token and wall totals are honest lower bounds over the attempts that did
    report, and `missing_usage_attempts` says how many did not. Cost and turns
    are all-or-nothing: one unreported attempt makes the whole total unknown,
    so a partial sum can never be read as the real spend for the group.
    """
    counted = [usage for usage in attempts if usage is not None]
    complete = bool(attempts) and len(counted) == len(attempts)
    costs = [usage.get("provider_cost_usd") for usage in counted]
    turns = [usage.get("turns_observed") for usage in counted]
    return {
        "usage_version": USAGE_VERSION,
        "attempts": len(attempts),
        "usage_attempts": len(counted),
        "missing_usage_attempts": len(attempts) - len(counted),
        **{
            field: sum(int(usage.get(field) or 0) for usage in counted)
            for field in CODEOFF_USAGE_TOKEN_FIELDS
        },
        "wall_seconds": round(sum(float(usage.get("wall_seconds") or 0) for usage in counted), 6),
        "turns_observed": (
            sum(int(value) for value in turns)
            if complete and all(value is not None for value in turns) else None
        ),
        "provider_cost_usd": (
            round(sum(float(value) for value in costs), 6)
            if complete and all(value is not None for value in costs) else None
        ),
    }


def _codeoff_test_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    """Keep only the sanitized shape of one code-off test receipt for economics."""
    return {
        "pass": receipt.get("pass"),
        "receipt_hash": receipt.get("receipt_hash"),
        "elapsed_seconds": receipt.get("elapsed_seconds"),
    }


def _codeoff_economics(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Sanitized aggregate for two authors, three judges, tests, and final verification."""
    identities = manifest.get("identities") or {}
    slots: dict[str, Any] = {}
    grouped: dict[str, list[dict[str, Any] | None]] = {"author": [], "judge": []}
    for slot in sorted(identities):
        identity = identities[slot] if isinstance(identities.get(slot), dict) else {}
        role = codeoff_slot_work_role(slot)
        attempts: list[dict[str, Any] | None] = []
        diagnostics: set[str] = set()
        for job_id in state.get("agent_jobs", {}).get(slot, []) or []:
            usage, diagnostic = _codeoff_slot_usage(job_id)
            attempts.append(usage)
            if usage is None:
                diagnostics.add(diagnostic)
        grouped[role].extend(attempts)
        slots[slot] = {
            "work_role": role,
            **{field: identity.get(field) for field in CODEOFF_IDENTITY_FIELDS},
            "logical_model": identity.get("logical_model"),
            "usage": _codeoff_usage_totals(attempts),
            "usage_diagnostics": sorted(diagnostics),
        }
    candidate_slots = state.get("candidates") or {}
    candidate_runs = [
        candidate_slots[slot]["tests"]
        for slot in sorted(candidate_slots)
        if isinstance((candidate_slots.get(slot) or {}).get("tests"), dict)
    ]
    # `candidate_pass` is a claim about the whole author field, so it is a
    # verdict only when every author candidate actually ran its recipes. A park
    # after one candidate tested green must not report the pair as passing.
    candidate_pass = (
        all(run.get("pass") is True for run in candidate_runs)
        if candidate_slots and len(candidate_runs) == len(candidate_slots) else None
    )
    final_verification = state.get("final_verification") if isinstance(state.get("final_verification"), dict) else None
    final_summary = state.get("final_test_summary") if isinstance(state.get("final_test_summary"), dict) else None
    everything = grouped["author"] + grouped["judge"]
    return {
        "version": CODEOFF_ECONOMICS_VERSION,
        "usage_version": USAGE_VERSION,
        "protocol_version": CODEOFF_PROTOCOL_VERSION,
        "inference_profile_version": manifest.get("inference_profile_version"),
        "experiment_id": manifest.get("experiment_id"),
        "status": state.get("status"),
        "reason": state.get("reason"),
        "identities_hash": manifest.get("identities_hash"),
        "slots": slots,
        "authors": _codeoff_usage_totals(grouped["author"]),
        "judges": _codeoff_usage_totals(grouped["judge"]),
        "inference": _codeoff_usage_totals(everything),
        "tests": {
            "candidate_runs": len(candidate_runs),
            "expected_candidate_runs": len(candidate_slots),
            "candidate_pass": candidate_pass,
            "candidate_elapsed_seconds": round(sum(float(run.get("elapsed_seconds") or 0) for run in candidate_runs), 6),
            "final_runs": 1 if final_summary else 0,
            "final_pass": final_summary.get("pass") if final_summary else None,
            "final_elapsed_seconds": final_summary.get("elapsed_seconds") if final_summary else None,
        },
        "final_verification": {
            "present": final_verification is not None,
            "receipt_hash": final_verification.get("receipt_hash") if final_verification else None,
            "tests_pass": final_verification.get("tests_pass") if final_verification else None,
            "no_mutation": final_verification.get("no_mutation") if final_verification else None,
            "commit_eligible": final_verification.get("commit_eligible") if final_verification else None,
        },
    }


def _codeoff_final_value(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": CODEOFF_PROTOCOL_VERSION,
        "experiment_id": manifest["experiment_id"],
        "status": state["status"],
        "reason": state.get("reason"),
        "started_at": manifest["created_at"],
        "finished_at": state["finished_at"],
        "elapsed_seconds": _codeoff_elapsed(manifest["created_at"], state["finished_at"]),
        "manifest_file_hash": state["manifest_file_hash"],
        "event_head": state["event_head"],
        "aggregation": state.get("aggregation"),
        "promotion": state.get("promotion"),
        "final_verification": state.get("final_verification"),
        "economics": state.get("economics"),
        "economics_hash": state.get("economics_hash"),
    }

def _codeoff_finalize_record(root: Path, manifest: dict[str, Any], state: dict[str, Any], status: str, reason: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if state.get("finalized"):
        raise RuntimeError("finalized experiment cannot reopen")
    state.update({"status": status, "reason": reason, "finalized": True, "finished_at": utcnow()})
    if extra:
        state.update(extra)
    economics = _codeoff_economics(manifest, state)
    state["economics"] = economics
    state["economics_hash"] = _codeoff_artifact(root, codeoff_canonical_json(economics))
    _codeoff_event(root, state, "finalized", {"status": status, "reason": reason, "economics_hash": state["economics_hash"]})
    final = _codeoff_final_value(manifest, state)
    final_bytes = codeoff_canonical_json(final) + b"\n"
    state["final_file_hash"] = hashlib.sha256(final_bytes).hexdigest()
    _codeoff_atomic_bytes(root / "final.json", final_bytes, immutable=True)
    try:
        _codeoff_write_state(root, state)
    except BaseException:
        (root / "final.json").unlink(missing_ok=True)
        raise
    return final

def _codeoff_fs_entries(root: Path) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    total = 0

    def walk(directory: Path, prefix: str = "") -> None:
        nonlocal total
        children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        for child in children:
            rel = f"{prefix}/{child.name}" if prefix else child.name
            if ".git" in Path(rel).parts:
                continue
            try:
                rel.encode("utf-8")
                info = child.stat(follow_symlinks=False)
            except UnicodeEncodeError as exc:
                raise CodeOffArtifactError("candidate_freeze_failed") from exc
            mode = f"{stat.S_IMODE(info.st_mode):04o}"
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": rel, "type": "directory", "mode": mode})
                walk(Path(child.path), rel)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(child.path)
                try: target.encode("utf-8")
                except UnicodeEncodeError as exc: raise CodeOffArtifactError("candidate_freeze_failed") from exc
                normalized = Path(os.path.normpath((Path(rel).parent / target).as_posix()))
                if Path(target).is_absolute() or normalized.is_absolute() or (normalized.parts and normalized.parts[0] == "..") or ".git" in normalized.parts: raise CodeOffArtifactError("candidate_unsafe_symlink")
                entries.append({"path": rel, "type": "symlink", "mode": mode, "target": target})
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > CODEOFF_MAX_FILE_BYTES:
                    raise RuntimeError("candidate file exceeds artifact limit")
                data = Path(child.path).read_bytes()
                total += len(data)
                if total > CODEOFF_MAX_TREE_BYTES:
                    raise RuntimeError("candidate tree exceeds artifact limit")
                entries.append({"path": rel, "type": "file", "mode": mode, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            else:
                raise RuntimeError("candidate tree contains an unsupported file type")
            if len(entries) > CODEOFF_MAX_FILES:
                raise RuntimeError("candidate tree exceeds file-count limit")

    walk(root)
    return entries, total

def _codeoff_changes(root: Path, base_sha: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="gw-codeoff-index-") as td:
        index = str(Path(td) / "index")
        env = {"GIT_INDEX_FILE": index}
        read = run_cmd(["git", "-C", str(root), "read-tree", base_sha], cwd=root, env=env)
        add = run_cmd(["git", "-C", str(root), "add", "-A"], cwd=root, env=env)
        diff = run_cmd(["git", "-C", str(root), "diff", "--cached", "--name-status", "-z", "--find-renames", base_sha], cwd=root, env=env)
    if not read.get("ok") or not add.get("ok") or not diff.get("ok") or diff.get("truncated"):
        raise OSError("could not compare candidate with base")
    fields = (diff.get("stdout") or "").split("\x00")
    changes: list[dict[str, Any]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status_code = fields[i]
        i += 1
        if i >= len(fields) or not fields[i]:
            raise RuntimeError("malformed git change record")
        first = fields[i]
        i += 1
        if status_code.startswith(("R", "C")):
            if i >= len(fields) or not fields[i]:
                raise RuntimeError("malformed git rename record")
            second = fields[i]
            i += 1
            changes.append({"status": status_code, "old_path": first, "path": second})
        else:
            changes.append({"status": status_code, "path": first})
    return changes

def _codeoff_ignored(root: Path) -> bool:
    result = run_git(root, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"])
    if not result.get("ok") or result.get("truncated"): raise OSError("could not verify ignored candidate paths")
    return bool(result.get("stdout"))

def codeoff_tree_snapshot(root: Path, base_sha: str | None = None) -> dict[str, Any]:
    if base_sha is None:
        got = run_git(root, ["rev-parse", "HEAD"])
        if not got.get("ok"):
            raise RuntimeError("candidate base is unavailable")
        base_sha = str(got["stdout"]).strip()
    entries, total = _codeoff_fs_entries(root)
    manifest = {
        "format": "code-off-tree-v1",
        "base_sha": base_sha,
        "entries": entries,
        "changes": _codeoff_changes(root, base_sha),
    }
    raw = codeoff_canonical_json(manifest)
    return {"manifest": manifest, "manifest_bytes": raw, "manifest_hash": hashlib.sha256(raw).hexdigest(), "bytes": total}

def _codeoff_tar(root: Path, entries: list[dict[str, Any]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry["path"])
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = int(entry["mode"], 8)
            kind = entry["type"]
            if kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry["target"]
                archive.addfile(info)
            else:
                data = (root / entry["path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise RuntimeError("candidate mutated while freezing")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()

def _codeoff_extract(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        names, symlinks = set(), {Path(member.name) for member in members if member.issym()}
        for member in members:
            rel = Path(member.name)
            if not rel.parts or rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts or member.islnk() or rel in names or any(parent in symlinks for parent in rel.parents):
                raise CodeOffArtifactError("unsafe candidate artifact")
            names.add(rel)
            if member.issym():
                link = Path(member.linkname)
                normalized = Path(os.path.normpath((rel.parent / link).as_posix()))
                if not member.linkname or link.is_absolute() or normalized.is_absolute() or (normalized.parts and normalized.parts[0] == ".."):
                    raise CodeOffArtifactError("unsafe candidate artifact")
            elif not (member.isdir() or member.isfile()):
                raise CodeOffArtifactError("candidate artifact has unsupported entry")
        destination.mkdir(parents=True, exist_ok=False)
        directory_modes: list[tuple[Path, int]] = []
        try:
            for member in [*filter(lambda item: item.isdir(), members), *filter(lambda item: item.isfile(), members), *filter(lambda item: item.issym(), members)]:
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directory_modes.append((target, member.mode))
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise CodeOffArtifactError("candidate artifact is incomplete")
                    target.write_bytes(source.read())
                    target.chmod(member.mode)
            for directory, mode in reversed(directory_modes):
                directory.chmod(mode)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise

def _codeoff_sanitize_log(raw: str) -> str:
    clipped = raw.encode("utf-8", "replace")[:CODEOFF_MAX_LOG_BYTES].decode("utf-8", "replace")
    clipped = re.sub(r"(?i)\b(token|password|secret|authorization)\b\s*[:=]\s*\S+", r"\1=[REDACTED]", clipped)
    clipped = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", clipped)
    clipped = re.sub(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b", "[REDACTED_TOKEN]", clipped)
    return re.sub(r"https://[^\s]+(?:hook|callback|token=)[^\s]*", "[REDACTED_URL]", clipped, flags=re.I)

def _codeoff_run_tests(record_root: Path, manifest: dict[str, Any], cwd: Path, phase: str) -> dict[str, Any]:
    started = utcnow()
    receipts = []
    all_pass = True
    for spec in manifest.get("test_specs") or []:
        timeout = min(int(spec["timeout_seconds"]), int(manifest["budgets"]["test_seconds"]))
        before = time.monotonic()
        result = run_cmd(list(spec["argv"]), cwd=cwd, timeout=timeout, env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
        elapsed = round(time.monotonic() - before, 6)
        passed = bool(result.get("ok"))
        all_pass = all_pass and passed
        log = codeoff_canonical_json({
            "name": spec["name"], "stdout": _codeoff_sanitize_log(result.get("stdout") or ""),
            "stderr": _codeoff_sanitize_log(result.get("stderr") or result.get("error") or ""),
            "truncated": bool(result.get("truncated")),
        })
        receipts.append({"name": spec["name"], "pass": passed, "returncode": result.get("returncode"), "elapsed_seconds": elapsed, "log_hash": _codeoff_artifact(record_root, log)})
    finished = utcnow()
    receipt = {"version": "code-off-test-receipt-v1", "phase": phase, "started_at": started, "finished_at": finished, "elapsed_seconds": _codeoff_elapsed(started, finished), "pass": all_pass, "tests": receipts}
    receipt["receipt_hash"] = _codeoff_artifact(record_root, codeoff_canonical_json(receipt))
    return receipt

def _codeoff_author_prompt(task: str, base_sha: str, base_tree: str, tests: list[str], toolchain: dict[str, str], budgets: dict[str, int]) -> bytes:
    header = (
        "CODE-OFF AUTHOR CONTRACT code-off-v1\n"
        "Work only in the current isolated worktree. Do not inspect parent directories, other worktrees, sessions, branches, or model/provider metadata. "
        "Do not use network, commit, push, or open a PR. Implement the task and leave the complete candidate tree in place.\n"
        f"Base SHA: {base_sha}\nBase tree: {base_tree}\nNamed tests: {json.dumps(tests, separators=(',', ':'))}\n"
        f"Toolchain: {json.dumps(toolchain, sort_keys=True, separators=(',', ':'))}\nBudgets: {json.dumps(budgets, sort_keys=True, separators=(',', ':'))}\n"
        "Finish with exactly one JSON object: {\"status\":\"ok\"|\"error\",\"sha\":null,\"pr_url\":null,\"summary\":\"one line\"}\n\nTask:\n"
    )
    return header.encode() + task.encode()

def _codeoff_launch_plan(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for slot, identity in manifest["identities"].items():
        key = slot.replace("-", "_")
        out[key] = {
            "slot": slot, "logical_model": identity["logical_model"],
            "launcher": identity["launcher"], "provider": identity["provider"],
            "model": identity["exact_model"], "max_turns": manifest["budgets"]["max_turns"],
            "run_budget_seconds": manifest["budgets"]["author_seconds" if slot.startswith("author-") else "judge_seconds"],
            "effort": identity.get("requested_effort"),
            "effective_effort": identity.get("effective_effort"),
            "launcher_version": identity.get("launcher_version"),
            "inference_profile_version": identity.get("inference_profile_version"),
        }
    return out

def _codeoff_prepared_result(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    selection = manifest["selection"]
    return {
        "ok": True, "experiment_id": manifest["experiment_id"], "status": "prepared",
        "authors": selection["authors"], "random_judges": selection["random_judges"],
        "seed_commitment": selection["seed_commitment"], "prompt_hash": manifest["prompt_hash"],
        "rubric_hash": manifest["rubric_hash"], "manifest_file_hash": state["manifest_file_hash"],
        "workspace_slots": ["author-1", "author-2"], "launches": _codeoff_launch_plan(manifest),
    }

def _codeoff_replay_prepared(record_root: Path, locked: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    root, manifest, state, err = _codeoff_load(locked["experiment_id"], active=True)
    untouched = (
        not err and state is not None and state.get("status") == "prepared"
        and state.get("reason") is None and state.get("finalized") is False
        and state.get("event_count") == 2 and state.get("aggregation") is None
        and state.get("judges") == {} and state.get("candidates") == {"author-1": {}, "author-2": {}}
        and state.get("agent_jobs") == {slot: [] for slot in locked["identities"]}
        and state.get("execution_manifests") == {slot: {} for slot in locked["identities"]}
    )
    exact = manifest is not None and all(manifest.get(key) == value for key, value in locked.items())
    workspaces_exact = untouched and exact and all(
        codeoff_workspace_path(locked["experiment_id"], slot).is_dir()
        and codeoff_tree_snapshot(codeoff_workspace_path(locked["experiment_id"], slot), locked["base_sha"])["manifest_hash"] == locked["base_snapshot_hash"]
        for slot in ("author-1", "author-2")
    )
    if root != record_root or not workspaces_exact:
        return 409, {"error": "experiment_id already exists; only an exact untouched prepare may resume", "code": "experiment_exists"}
    assert manifest is not None and state is not None
    return 200, _codeoff_prepared_result(manifest, state)

def _codeoff_parse_interpolated(data: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("tags", "tests", "toolchain", "budgets"):
        raw = data.get(key)
        if not isinstance(raw, str):
            continue
        if len(raw) > 8192:
            return {"error": f"{key} JSON string exceeds 8192 characters", "code": f"bad_{key}"}
        try:
            data[key] = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            return {"error": f"{key} must be native JSON or a whole-value JSON string", "code": f"bad_{key}"}
    return None

def _codeoff_remove_worktree(repo: Path, path: Path) -> None:
    if path.exists() and not run_git(repo, ["worktree", "remove", "--force", str(path)]).get("ok"):
        shutil.rmtree(path, ignore_errors=True)
    run_git(repo, ["worktree", "prune"])

def codeoff_prepare(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id", "repo", "base_sha", "seed", "category", "tags", "category_source", "prompt", "tests", "toolchain", "budgets", "commit_message"})
    if err:
        return 400, err
    assert data is not None
    interpolated_err = _codeoff_parse_interpolated(data)
    if interpolated_err:
        return 400, interpolated_err
    experiment_id, eid_err = _codeoff_id(data.get("experiment_id"))
    if eid_err:
        return 400, eid_err
    category = data.get("category")
    if category not in CODEOFF_CATEGORIES:
        return 400, {"error": "category is not in code-category-v1", "code": "bad_category", "allowed": sorted(CODEOFF_CATEGORIES)}
    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list) or len(raw_tags) > 12 or not all(isinstance(tag, str) for tag in raw_tags) or not set(raw_tags) <= CODEOFF_TAGS:
        return 400, {"error": "tags must use code-category-v1", "code": "bad_tags", "allowed": sorted(CODEOFF_TAGS)}
    category_source = data.get("category_source")
    if not isinstance(category_source, str) or not category_source.strip() or len(category_source) > 80:
        return 400, {"error": "category_source is required and bounded", "code": "bad_category_source"}
    seed = data.get("seed")
    if not isinstance(seed, str) or not CODEOFF_SEED_RE.fullmatch(seed):
        return 400, {"error": "seed must be exactly 32 lowercase hex bytes", "code": "bad_seed"}
    task = data.get("prompt")
    if not isinstance(task, str) or not task.strip() or len(task) > PROMPT_MAX_CHARS:
        return 400, {"error": "prompt is required and bounded", "code": "bad_prompt"}
    names = data.get("tests")
    if not isinstance(names, list) or not 1 <= len(names) <= 10 or not all(isinstance(name, str) and 1 <= len(name) <= 120 for name in names) or len(set(names)) != len(names):
        return 400, {"error": "tests must be 1-10 unique named recipes", "code": "bad_tests"}
    toolchain = data.get("toolchain")
    if not isinstance(toolchain, dict) or not 1 <= len(toolchain) <= 20 or not all(isinstance(k, str) and k and isinstance(v, str) and v and len(k) <= 40 and len(v) <= 120 for k, v in toolchain.items()):
        return 400, {"error": "toolchain must be a bounded string map", "code": "bad_toolchain"}
    budgets = data.get("budgets")
    limits = {"author_seconds": (30, CODEOFF_MAX_AGENT_SECONDS), "judge_seconds": (30, CODEOFF_MAX_AGENT_SECONDS), "test_seconds": (1, 1200), "max_turns": (1, 80)}
    if not isinstance(budgets, dict) or set(budgets) != set(limits) or any(type(budgets.get(key)) is not int or not lo <= budgets[key] <= hi for key, (lo, hi) in limits.items()):
        return 400, {"error": "budgets must contain the exact versioned keys", "code": "bad_budgets"}
    commit_message = data.get("commit_message")
    if not isinstance(commit_message, str) or not commit_message.strip() or len(commit_message) > 300:
        return 400, {"error": "commit_message is required and bounded", "code": "bad_commit_message"}
    name, repo = repo_from_body(data, repos)
    if name is None:
        return 400, repo
    base_sha = data.get("base_sha")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        return 400, {"error": "base_sha must be an exact object id", "code": "bad_base_sha"}
    branch, head, git_err = current_branch_head(repo)
    if git_err or head != base_sha:
        return 409, {"error": "repo HEAD does not equal base_sha", "code": "base_mismatch"}
    status = run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    ignored = run_git(repo, ["ls-files", "--others", "--ignored", "--exclude-standard"])
    if not status.get("ok") or not ignored.get("ok") or status.get("stdout") or ignored.get("stdout"):
        return 409, {"error": "base worktree must contain only the exact committed tree", "code": "base_not_pristine"}
    tree = run_git(repo, ["rev-parse", f"{base_sha}^{{tree}}"])
    if not tree.get("ok"):
        return 400, {"error": "base tree is unavailable", "code": "bad_base_sha"}
    base_tree = str(tree["stdout"]).strip()
    catalog = load_tests(repos)
    specs = []
    for test_name in names:
        spec = catalog.get(test_name)
        if spec is None or Path(spec["cwd"]).resolve() != Path(repo).resolve():
            return 400, {"error": f"test '{test_name}' is not allowlisted for repo", "code": "unknown_test", "allowed": sorted(catalog)}
        specs.append({"name": test_name, "argv": list(spec["argv"]), "timeout_seconds": int(spec["timeout_seconds"])})
    embedded = json.loads(json.dumps(CODEOFF_MODEL_MANIFEST))
    catalog_inventory = json.loads(json.dumps(CODEOFF_CATALOG_INVENTORY))
    manifest_checked_at = datetime.now(timezone.utc)
    embedded_hash = hashlib.sha256(codeoff_canonical_json(embedded)).hexdigest()
    current = _codeoff_manifest_current(embedded, manifest_checked_at)
    catalog_models = catalog_inventory.get("models")
    valid_catalog = catalog_inventory.get("version") == "bounded-model-catalog-v1" and isinstance(catalog_models, dict) and set(catalog_models) == set(embedded.get("models", {})) and all(isinstance(model, str) and model for model in catalog_models.values())
    catalog_reasons = ["bounded_catalog_inventory_invalid"] if not valid_catalog else [
        f"catalog_identity_newer:{logical}:{exact_model}"
        for logical, exact_model in catalog_models.items()
        if embedded["models"][logical].get("exact_model") != exact_model
    ]
    draw = codeoff_draw(seed)
    slot_logical = {"author-1": draw["authors"][0], "author-2": draw["authors"][1], "judge-fable": "fable", "judge-1": draw["random_judges"][0], "judge-2": draw["random_judges"][1]}
    identities: dict[str, dict[str, Any]] = {}
    for slot, logical in slot_logical.items():
        approved = embedded.get("models", {}).get(logical) or {}
        identities[slot] = {
            "logical_model": logical, "exact_model": approved.get("exact_model"), "provider": approved.get("provider"), "launcher": approved.get("launcher"),
            "launcher_version": None,
            "requested_effort": CODEOFF_SLOT_REQUESTED_EFFORT[codeoff_slot_work_role(slot)],
            "effective_effort": None,
            "inference_profile_version": CODEOFF_INFERENCE_PROFILE_VERSION,
            "runnable": approved.get("runnable") is True, "proven": approved.get("proven") is True, "reason": approved.get("reason") or "identity absent from manifest",
            "manifest_version": embedded.get("version"), "manifest_as_of": embedded.get("as_of"),
            "manifest_max_age_days": embedded.get("max_age_days"), "manifest_current": current,
        }
    # Ineligible manifests park before any launcher artifact is even resolved;
    # only an otherwise-approved draw earns an exact wrapper fingerprint pin.
    reasons = ([] if current else ["approved_model_manifest_stale"]) + catalog_reasons + [
        f"identity_unproven:{slot}:{identity['logical_model']}:{identity['exact_model']}"
        for slot, identity in identities.items() if not identity["runnable"] or not identity["proven"]
    ]
    if not reasons:
        fingerprints: dict[str, str | None] = {}
        for slot, identity in identities.items():
            launcher = identity["launcher"]
            if launcher not in fingerprints:
                fingerprints[launcher] = (
                    launcher_version_fingerprint(resolve_launcher_binary_now(launcher))
                    if launcher in NATIVE_LAUNCHERS else None
                )
            profile, effort_err = normalize_effort(
                str(launcher), str(identity["provider"]), str(identity["exact_model"]),
                identity["requested_effort"], CODEOFF_EFFORT_SOURCE,
            )
            identity["launcher_version"] = fingerprints[launcher]
            identity["effective_effort"] = None if effort_err else profile["effective_effort"]  # type: ignore[index]
            if not codeoff_identity_is_pinned(slot, identity):
                reasons.append(f"inference_identity_unpinned:{slot}:{identity['logical_model']}")
    identity_snapshot_hashes = {
        slot: codeoff_identity_snapshot_hash(identity) for slot, identity in identities.items()
    }
    fable_provider = identities["judge-fable"]["provider"]
    overlaps = [{"fixed_judge": "judge-fable", "other": slot, "provider": fable_provider} for slot, identity in identities.items() if slot != "judge-fable" and identity["provider"] == fable_provider]
    canonical_prompt = _codeoff_author_prompt(task, base_sha, base_tree, names, dict(sorted(toolchain.items())), budgets)
    if len(canonical_prompt) > PROMPT_MAX_CHARS + 8192:
        return 400, {"error": "canonical prompt exceeds limit", "code": "prompt_too_large"}
    base_snapshot = codeoff_tree_snapshot(repo, base_sha)
    assert experiment_id is not None
    record_root = _codeoff_root(experiment_id)
    workspace_root = CODEOFF_WORKSPACES_DIR / experiment_id
    locked = {
        "protocol_version": CODEOFF_PROTOCOL_VERSION, "experiment_id": experiment_id,
        "repo": name, "branch": branch, "base_sha": base_sha,
        "base_tree": base_tree, "base_snapshot_hash": base_snapshot["manifest_hash"],
        "classification": {"version": CODEOFF_CATEGORY_VERSION, "category": category, "tags": sorted(set(raw_tags))},
        "original_classification": {"category": category, "tags": raw_tags}, "category_source": category_source.strip(),
        "selection": {**draw, "seed": seed}, "identities": identities,
        "identity_snapshot_hashes": identity_snapshot_hashes,
        "identities_hash": hashlib.sha256(codeoff_canonical_json(identities)).hexdigest(),
        "inference_profile_version": CODEOFF_INFERENCE_PROFILE_VERSION,
        "fable_provider_overlap": overlaps,
        "approved_model_manifest": embedded, "approved_model_manifest_current": current,
        "bounded_catalog_inventory": catalog_inventory, "approved_model_manifest_hash": embedded_hash,
        "limitation": embedded.get("limitation"),
        "task": task, "prompt_hash": hashlib.sha256(canonical_prompt).hexdigest(),
        "rubric_hash": hashlib.sha256(codeoff_canonical_json(CODEOFF_RUBRIC)).hexdigest(),
        "tests": names, "test_specs": specs, "toolchain": dict(sorted(toolchain.items())), "budgets": budgets,
        "artifact_limits": {"max_files": CODEOFF_MAX_FILES, "max_file_bytes": CODEOFF_MAX_FILE_BYTES, "max_tree_bytes": CODEOFF_MAX_TREE_BYTES, "max_log_bytes": CODEOFF_MAX_LOG_BYTES},
        "worktree_isolation": {"version": "opaque-worktree-v1", "slots": ["author-1", "author-2"], "caller_paths": False, "os_security_boundary": False},
        "aggregation_policy_version": CODEOFF_AGGREGATION_VERSION, "rubric_version": CODEOFF_RUBRIC_VERSION,
        "commit_message": commit_message.strip(),
    }
    with CODEOFF_LOCK:
        if record_root.exists():
            return _codeoff_replay_prepared(record_root, locked)
        if workspace_root.exists():
            return 409, {"error": "opaque workspace already exists", "code": "workspace_exists"}
        CODEOFFS_DIR.mkdir(parents=True, exist_ok=True)
        record_build = Path(tempfile.mkdtemp(prefix=f".prepare-{experiment_id}-", dir=CODEOFFS_DIR))
        created: list[Path] = []
        try:
            prompt_hash = _codeoff_artifact(record_build, canonical_prompt)
            rubric_hash = _codeoff_artifact(record_build, codeoff_canonical_json(CODEOFF_RUBRIC))
            manifest = {
                **locked, "created_at": manifest_checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "approved_model_manifest_checked_at": manifest_checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            _codeoff_atomic_json(record_build / "manifest.json", manifest, immutable=True)
            manifest_file_hash = hashlib.sha256((record_build / "manifest.json").read_bytes()).hexdigest()
            state = {"status": "preparing", "reason": None, "finalized": False, "manifest_file_hash": manifest_file_hash, "event_count": 0, "event_head": "0" * 64, "agent_jobs": {slot: [] for slot in slot_logical}, "execution_manifests": {slot: {} for slot in slot_logical}, "candidates": {"author-1": {}, "author-2": {}}, "judges": {}, "aggregation": None}
            _codeoff_commit_event(record_build, state, "prepared_manifest", {"manifest_file_hash": manifest_file_hash, "prompt_hash": prompt_hash, "rubric_hash": rubric_hash, "identities_hash": locked["identities_hash"]})
            if reasons:
                _codeoff_finalize_record(record_build, manifest, state, "parked", ";".join(reasons))
                record_build.replace(record_root)
                return 200, {"ok": True, "experiment_id": experiment_id, "status": "parked", "reasons": reasons, "authors": draw["authors"], "random_judges": draw["random_judges"], "seed_commitment": draw["seed_commitment"], "manifest_file_hash": manifest_file_hash, "launches": _codeoff_launch_plan(manifest)}
            workspace_root.mkdir(parents=True, mode=0o700)
            for slot in ("author-1", "author-2"):
                path = codeoff_workspace_path(experiment_id, slot)
                created.append(path)
                if not run_git(repo, ["worktree", "add", "--detach", str(path), base_sha]).get("ok"):
                    raise RuntimeError(f"isolated worktree creation failed: {slot}")
                if codeoff_tree_snapshot(path, base_sha)["manifest_hash"] != base_snapshot["manifest_hash"]:
                    raise RuntimeError(f"isolated worktree base mismatch: {slot}")
            state["status"] = "prepared"
            _codeoff_commit_event(record_build, state, "workspaces_created", {"slots": ["author-1", "author-2"], "isolation_version": "opaque-worktree-v1"})
            record_build.replace(record_root)
        except BaseException:
            for path in reversed(created):
                _codeoff_remove_worktree(repo, path)
            shutil.rmtree(workspace_root, ignore_errors=True)
            shutil.rmtree(record_build, ignore_errors=True)
            raise
    return 200, _codeoff_prepared_result(manifest, state)

def _codeoff_terminal_identity_matches(
    job: dict[str, Any], receipt: dict[str, Any], manifest_identity: dict[str, Any]
) -> bool:
    expected = _codeoff_identity_values(manifest_identity, "exact_model")
    execution = _codeoff_compact_execution_identity(job)
    source = _codeoff_provenance_source(manifest_identity.get("launcher"))
    surfaces = (
        job, job.get("session_identity"), receipt.get("session_identity"),
        job.get("execution_identity"), receipt.get("execution_identity"),
    )
    # The pinned inference profile is authority: the whole requested/effective/
    # source effort triple and the exact launcher wrapper fingerprint must agree
    # on the job, both session identities, and the terminal receipt.
    pinned = (
        (
            manifest_identity.get("requested_effort"),
            manifest_identity.get("effective_effort"),
            CODEOFF_EFFORT_SOURCE,
            manifest_identity.get("launcher_version"),
        )
        if isinstance(manifest_identity, dict) else (None, None, None, None)
    )

    def profile(surface: Any) -> tuple[Any, ...] | None:
        if not isinstance(surface, dict):
            return None
        return tuple(surface.get(key) for key in (
            "requested_effort", "effective_effort", "effort_source", "launcher_version",
        ))

    profile_surfaces = (
        job, job.get("session_identity"), receipt.get("session_identity"), receipt,
    )
    return bool(
        expected is not None and source is not None and execution is not None
        and receipt_profile_matches_job(job, receipt)
        and all(_codeoff_identity_values(surface) == expected for surface in surfaces)
        and all(profile(surface) == pinned for surface in profile_surfaces)
        and receipt.get("session_identity") == job.get("session_identity")
        and execution.get("source") == source
        and job.get("execution_identity") == execution
        and receipt.get("execution_identity") == execution
    )


def _codeoff_validate_job(job_id: Any, state: dict[str, Any], manifest: dict[str, Any], slot: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id) or job_id not in state.get("agent_jobs", {}).get(slot, []):
        return None, {"error": "job is not pinned to this experiment slot", "code": "job_mismatch"}
    job = read_job(job_id)
    if not is_agent_job_record(job):
        return None, {"error": "slot requires its exact successful terminal agent receipt", "code": "agent_receipt_mismatch"}
    assert isinstance(job, dict)
    if job.get("status") in ("queued", "running"): return None, {"error": "slot agent has not reached a terminal receipt", "code": "agent_not_terminal"}
    identity = manifest["identities"][slot]
    receipt = job.get("receipt")
    root = _codeoff_root(manifest["experiment_id"])
    hashes = _codeoff_execution_manifest_hashes(root, state, slot, job_id)
    manifests_match = False
    if hashes is not None and isinstance(receipt, dict):
        try:
            launch_manifest = json.loads(_codeoff_read_artifact(root, hashes[0]))
            terminal_manifest = json.loads(_codeoff_read_artifact(root, hashes[1]))
            manifests_match = (
                launch_manifest == _codeoff_launch_execution_manifest(job)
                and terminal_manifest == _codeoff_terminal_execution_manifest(job, receipt)
            )
        except (CodeOffArtifactError, OSError, json.JSONDecodeError):
            manifests_match = False
    if not (job.get("job_id") == job_id and manifests_match and job.get("status") == "completed" and isinstance(receipt, dict) and receipt.get("status") == "ok" and receipt.get("job_id") == job_id and _codeoff_terminal_identity_matches(job, receipt, identity) and job.get("codeoff_workspace") == {"experiment_id": manifest["experiment_id"], "slot": slot}):
        return None, {"error": "slot requires its exact successful terminal agent receipt", "code": "agent_receipt_mismatch"}
    return job, None

def _codeoff_verify_artifact(root: Path, manifest: dict[str, Any], repo: Path, candidate: dict[str, Any], phase: str) -> dict[str, Any]:
    parent = CODEOFF_WORKSPACES_DIR / manifest["experiment_id"]
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".verify-{phase}-{uuid.uuid4().hex}"
    extracted = parent / f".extract-{phase}-{uuid.uuid4().hex}"
    if not run_git(repo, ["worktree", "add", "--detach", str(staging), manifest["base_sha"]]).get("ok"):
        _codeoff_remove_worktree(repo, staging); raise RuntimeError("git-faithful verification worktree creation failed")
    try:
        frozen_manifest = _codeoff_read_artifact(root, candidate["tree_manifest_hash"])
        _codeoff_extract(_codeoff_read_artifact(root, candidate["artifact_hash"]), extracted)
        _codeoff_clear_tree(staging)
        for child in os.scandir(extracted):
            Path(child.path).replace(staging / child.name)
        extracted.rmdir()
        if _codeoff_ignored(staging): raise CodeOffArtifactError("candidate artifact contains ignored paths")
        before = codeoff_tree_snapshot(staging, manifest["base_sha"])
        _branch, head, git_err = current_branch_head(staging)
        if before["manifest_bytes"] != frozen_manifest or before["manifest_hash"] != candidate["tree_manifest_hash"] or git_err or head != manifest["base_sha"] or not run_git(staging, ["diff", "--cached", "--quiet", manifest["base_sha"]]).get("ok"):
            raise CodeOffArtifactError("frozen artifact does not materialize as its exact git tree")
        tests = _codeoff_run_tests(root, manifest, staging, phase)
        after = codeoff_tree_snapshot(staging, manifest["base_sha"])
        _branch, after_head, after_err = current_branch_head(staging)
        no_mutation = after["manifest_hash"] == candidate["tree_manifest_hash"] and not after_err and after_head == manifest["base_sha"] and run_git(staging, ["diff", "--cached", "--quiet", manifest["base_sha"]]).get("ok") is True
        git_tree = None
        if no_mutation:
            added = run_git(staging, ["add", "-A"])
            tree = run_git(staging, ["write-tree"])
            if not added.get("ok") or not tree.get("ok"):
                raise RuntimeError("verified winner could not be represented as a git tree")
            git_tree = str(tree["stdout"]).strip()
        return {"tests": tests, "no_mutation": no_mutation, "git_faithful": True, "git_tree": git_tree, "entries": after["manifest"]["entries"]}
    finally:
        shutil.rmtree(extracted, ignore_errors=True)
        _codeoff_remove_worktree(repo, staging)

def _codeoff_park_candidate(experiment_id: Any, slot: str, code: str) -> tuple[int, dict[str, Any]]:
    with CODEOFF_LOCK:
        root, manifest, state, err = _codeoff_load(experiment_id, active=True)
        if err: return 409, err
        assert root and manifest and state
        _codeoff_finalize_record(root, manifest, state, "parked", f"{code}:{slot}")
    return 422, {"ok": False, "error": "candidate failed an immutable terminal gate", "code": code, "experiment_id": manifest["experiment_id"], "slot": slot, "status": "parked"}

def codeoff_candidate(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id", "slot", "action", "job_id"})
    if err:
        return 400, err
    assert data is not None
    slot, action = data.get("slot"), data.get("action")
    if slot not in ("author-1", "author-2"):
        return 400, {"error": "slot must be author-1 or author-2", "code": "bad_slot"}
    if action not in ("freeze", "test"):
        return 400, {"error": "action must be freeze or test", "code": "bad_action"}
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409 if load_err["code"] == "experiment_finalized" else 400, load_err
        assert root and manifest and state
        identity_err = codeoff_manifest_identity_error(manifest)
        if identity_err:
            return 409, identity_err
        candidate = state["candidates"][slot]
        workspace = codeoff_workspace_path(manifest["experiment_id"], slot)
        if action == "freeze":
            if set(data) != {"experiment_id", "slot", "action", "job_id"}:
                return 400, {"error": "freeze requires job_id", "code": "missing_job_id"}
            if candidate.get("tree_manifest_hash"):
                return 409, {"error": "candidate is already frozen", "code": "candidate_frozen"}
            job, job_err = _codeoff_validate_job(data.get("job_id"), state, manifest, slot)
            if job_err:
                if job_err["code"] == "agent_receipt_mismatch": return _codeoff_park_candidate(data.get("experiment_id"), slot, job_err["code"])
                return 409, job_err
            try:
                if _codeoff_ignored(workspace): return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_ignored_paths")
                snapshot = codeoff_tree_snapshot(workspace, manifest["base_sha"])
                tar_bytes = _codeoff_tar(workspace, snapshot["manifest"]["entries"])
                if codeoff_tree_snapshot(workspace, manifest["base_sha"])["manifest_hash"] != snapshot["manifest_hash"]:
                    raise RuntimeError("candidate mutated while freezing")
                tree_manifest_hash = _codeoff_artifact(root, snapshot["manifest_bytes"])
                artifact_hash = _codeoff_artifact(root, tar_bytes)
            except CodeOffArtifactError as exc:
                return _codeoff_park_candidate(data.get("experiment_id"), slot, str(exc) if str(exc).startswith("candidate_") else "candidate_freeze_failed")
            except RuntimeError:
                return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_freeze_failed")
            receipt = {"version": "code-off-freeze-receipt-v1", "slot": slot, "job_id": job["job_id"], "identity": manifest["identities"][slot], "execution_identity": job["execution_identity"], "started_at": job.get("started_at"), "finished_at": job.get("finished_at"), "elapsed_seconds": _codeoff_elapsed(job.get("started_at"), job.get("finished_at")), "tree_manifest_hash": tree_manifest_hash, "artifact_hash": artifact_hash, "base_sha": manifest["base_sha"], "base_tree": manifest["base_tree"], "bytes": snapshot["bytes"]}
            receipt_hash = _codeoff_artifact(root, codeoff_canonical_json(receipt))
            candidate.update({**receipt, "freeze_receipt_hash": receipt_hash, "tests": None})
            state["status"] = "candidates_running"
            _codeoff_commit_event(root, state, "candidate_frozen", {"slot": slot, "tree_manifest_hash": tree_manifest_hash, "artifact_hash": artifact_hash, "receipt_hash": receipt_hash})
            return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "slot": slot, "status": "frozen", "tree_manifest_hash": tree_manifest_hash, "artifact_hash": artifact_hash, "freeze_receipt_hash": receipt_hash}
        if set(data) != {"experiment_id", "slot", "action"}:
            return 400, {"error": "test accepts only the opaque experiment and slot", "code": "unexpected_fields"}
        if not candidate.get("artifact_hash"):
            return 409, {"error": "candidate must be frozen before testing", "code": "candidate_not_frozen"}
        if candidate.get("tests"):
            return 409, {"error": "candidate tests are already recorded", "code": "candidate_tested"}
        expected = {key: candidate[key] for key in ("artifact_hash", "tree_manifest_hash")}
    try:
        current = codeoff_tree_snapshot(workspace, manifest["base_sha"])
        if current["manifest_hash"] != expected["tree_manifest_hash"]: return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_mutated")
        verified = _codeoff_verify_artifact(root, manifest, workspace, candidate, "candidate")
    except (CodeOffArtifactError, FileNotFoundError, tarfile.TarError):
        return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_artifact_invalid")
    if not verified["no_mutation"]:
        return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_test_mutated")
    try: current = codeoff_tree_snapshot(workspace, manifest["base_sha"])
    except CodeOffArtifactError: return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_mutated")
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409 if load_err["code"] == "experiment_finalized" else 400, load_err
        assert root and manifest and state
        candidate = state["candidates"][slot]
        if any(candidate.get(key) != value for key, value in expected.items()) or current["manifest_hash"] != expected["tree_manifest_hash"]:
            return _codeoff_park_candidate(data.get("experiment_id"), slot, "candidate_mutated")
        if candidate.get("tests"):
            return 409, {"error": "candidate tests are already recorded", "code": "candidate_tested"}
        tests = verified["tests"]
        candidate["tests"] = tests
        if all(state["candidates"][item].get("tests") for item in ("author-1", "author-2")):
            state["status"] = "candidates_tested"
        _codeoff_commit_event(root, state, "candidate_tested", {"slot": slot, "pass": tests["pass"], "receipt_hash": tests["receipt_hash"]})
    return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "slot": slot, "status": "tested", "tests_pass": tests["pass"], "test_receipt_hash": tests["receipt_hash"], "log_hashes": [item["log_hash"] for item in tests["tests"]]}

def _codeoff_leakage(root: Path, source: Path, base_sha: str, changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    signals = (
        ("protocol_identity", r"\b(?:codeoff_workspace|logical_model|exact_model|session_identity|identity_snapshot_hash|code-off author contract)\b|\b(?:provider|launcher)\s*[:=]\s*[\"']?(?:openai|anthropic|xai|codex|claude|grok)\b"),
        ("exact_model", r"\b(?:gpt-5\.6-(?:sol|terra)|claude-(?:sonnet|opus|fable)-5|grok-4\.6)\b"),
        ("self_identity", r"\b(?:i am|authored by|generated by)\b[^\n]{0,96}\b(?:grok|sol|terra|sonnet|opus|fable|openai|anthropic|xai|codex|claude)\b"),
    )
    findings = []
    for change in sorted(changes, key=lambda item: item.get("path", "")):
        rel = change.get("path", "")
        path = root / rel
        if path.is_symlink(): raw = os.readlink(path).encode("utf-8", "surrogateescape")
        elif not path.is_file(): continue
        else: raw = path.read_bytes()
        text = rel.lower() + "\n" + raw.decode("utf-8", "ignore").lower()
        prior = ""
        if not str(change.get("status", "")).startswith("A"):
            base = subprocess.run(["git", "-C", str(source), "show", f"{base_sha}:{change.get('old_path', rel)}"], check=False, capture_output=True, timeout=CMD_TIMEOUT)
            if base.returncode: raise OSError("could not compare candidate leakage with base")
            prior = change.get("old_path", rel).lower() + "\n" + base.stdout.decode("utf-8", "ignore").lower()
        for signal, pattern in signals:
            if Counter(re.findall(pattern, text)) - Counter(re.findall(pattern, prior)):
                findings.append({"path_hash": hashlib.sha256(rel.encode()).hexdigest(), "signal": signal})
    return findings[:200]

def _codeoff_init_blind_repo(path: Path) -> None:
    env = {"GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    for args in (["init", "-q", "-b", "blind"], ["config", "user.email", "blind@graphwing.invalid"], ["config", "user.name", "graphwing-blind"], ["add", "-A"]):
        result = run_cmd(["git", "-C", str(path), *args], cwd=path, env=env)
        if not result.get("ok"):
            raise RuntimeError("could not initialize blind judge workspace")
    result = run_cmd(["git", "-C", str(path), "commit", "-q", "-m", "blind bundle"], cwd=path, env=env)
    if not result.get("ok"):
        raise RuntimeError("could not freeze blind judge workspace")

def codeoff_blind(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id"}, exact=True)
    if err:
        return 400, err
    assert data is not None
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409 if load_err["code"] == "experiment_finalized" else 400, load_err
        assert root and manifest and state
        identity_err = codeoff_manifest_identity_error(manifest)
        if identity_err:
            return 409, identity_err
        if state.get("blind_bundle_hashes") or state.get("status") in ("blinded", "judging"):
            return 409, {"error": "blind bundles are already frozen", "code": "already_blinded"}
        for judge_slot in ("judge-fable", "judge-1", "judge-2"):
            shutil.rmtree(codeoff_workspace_path(manifest["experiment_id"], judge_slot), ignore_errors=True)
        if not all(state["candidates"][slot].get("tests") for slot in ("author-1", "author-2")):
            return 409, {"error": "both frozen candidates must be tested", "code": "candidates_incomplete"}
        for slot in ("author-1", "author-2"):
            current = codeoff_tree_snapshot(codeoff_workspace_path(manifest["experiment_id"], slot), manifest["base_sha"])
            if current["manifest_hash"] != state["candidates"][slot]["tree_manifest_hash"]:
                _codeoff_finalize_record(root, manifest, state, "parked", f"candidate_mutated:{slot}"); return 422, {"error": "candidate changed after freeze", "code": "candidate_mutated", "slot": slot}
        leakage = []
        for author_slot in ("author-1", "author-2"):
            candidate = state["candidates"][author_slot]
            try: tree = json.loads(_codeoff_read_artifact(root, candidate["tree_manifest_hash"]))
            except (CodeOffArtifactError, FileNotFoundError, tarfile.TarError, json.JSONDecodeError): _codeoff_finalize_record(root, manifest, state, "parked", f"candidate_artifact_invalid:{author_slot}"); return 422, {"ok": False, "error": "frozen candidate artifact failed integrity", "code": "candidate_artifact_invalid", "slot": author_slot}
            scan = Path(tempfile.mkdtemp(prefix=f".scan-{author_slot}-", dir=CODEOFF_WORKSPACES_DIR / manifest["experiment_id"]))
            scan.rmdir()
            try:
                _codeoff_extract(_codeoff_read_artifact(root, candidate["artifact_hash"]), scan)
                source = codeoff_workspace_path(manifest["experiment_id"], author_slot)
                leakage.extend({"candidate": author_slot, **finding} for finding in _codeoff_leakage(scan, source, manifest["base_sha"], tree["changes"]))
            except (CodeOffArtifactError, FileNotFoundError, tarfile.TarError):
                _codeoff_finalize_record(root, manifest, state, "parked", f"candidate_artifact_invalid:{author_slot}"); return 422, {"ok": False, "error": "frozen candidate artifact failed integrity", "code": "candidate_artifact_invalid", "slot": author_slot}
            finally:
                shutil.rmtree(scan, ignore_errors=True)
        if leakage:
            _codeoff_finalize_record(root, manifest, state, "parked", "candidate_identity_leakage")
            return 422, {"ok": False, "error": "frozen candidate contains private protocol or author identity leakage", "code": "candidate_identity_leakage", "experiment_id": manifest["experiment_id"], "status": "parked", "leakage_findings": len(leakage)}
        seed = bytes.fromhex(manifest["selection"]["seed"])
        judges = {}
        public = []
        for slot in ("judge-fable", "judge-1", "judge-2"):
            order = _codeoff_rank(seed, f"blind:{manifest['identities'][slot]['logical_model']}", ["author-1", "author-2"])
            labels = {f"candidate-{index + 1}": author_slot for index, author_slot in enumerate(order)}
            workspace = codeoff_workspace_path(manifest["experiment_id"], slot)
            if workspace.exists():
                return 409, {"error": "judge workspace already exists", "code": "workspace_exists"}
            workspace.mkdir(parents=True)
            bundle_candidates = {}
            try:
                for label, author_slot in labels.items():
                    candidate = state["candidates"][author_slot]
                    _codeoff_extract(_codeoff_read_artifact(root, candidate["artifact_hash"]), workspace / label)
                    bundle_candidates[label] = {"tree_manifest_hash": candidate["tree_manifest_hash"], "artifact_hash": candidate["artifact_hash"]}
                bundle = {"version": "code-off-blind-bundle-v1", "rubric_hash": manifest["rubric_hash"], "candidates": bundle_candidates, "leakage_count": 0}
                bundle_hash = _codeoff_artifact(root, codeoff_canonical_json(bundle))
                _codeoff_init_blind_repo(workspace)
            except (OSError, RuntimeError, tarfile.TarError):
                shutil.rmtree(workspace, ignore_errors=True)
                _codeoff_finalize_record(root, manifest, state, "parked", f"blind_bundle_failed:{slot}")
                return 409, {"error": "blind bundle construction failed", "code": "blind_bundle_failed", "slot": slot}
            snapshot_hash = hashlib.sha256(codeoff_canonical_json(manifest["identities"][slot])).hexdigest()
            judges[slot] = {"labels": labels, "judge_snapshot_hash": snapshot_hash, "bundle_hash": bundle_hash}
            public.append({"slot": slot, "bundle_hash": bundle_hash, "judge_snapshot_hash": snapshot_hash, "leakage_count": 0})
        private = {"version": "code-off-blinding-v1", "judges": judges}
        _codeoff_atomic_json(root / "private" / "blinding.json", private, immutable=True)
        state["status"] = "blinded"
        state["blind_bundle_hashes"] = {slot: item["bundle_hash"] for slot, item in judges.items()}
        _codeoff_commit_event(root, state, "blind_bundles_created", {"bundle_hashes": state["blind_bundle_hashes"], "leakage_findings": 0})
    return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "status": "blinded", "bundles": public, "leakage_findings": 0}

def _codeoff_judge_prompt(manifest: dict[str, Any], private: dict[str, Any], slot: str) -> bytes:
    expected = private["judges"][slot]["judge_snapshot_hash"]
    return (
        "CODE-OFF BLIND JUDGE CONTRACT code-off-rubric-v1\n"
        "Compare candidate-1 and candidate-2 in the current blind workspace. Do not inspect .git, parent directories, APIs, sessions, or model/provider metadata. Do not use network. "
        "Use the exact rubric and write judge-result.json with no extra keys. Scores: correctness 0..40, tests 0..20, quality 0..20, scope 0..10, maintainability 0..10. "
        "Each candidate needs scores, boolean pass, and rationale <=1200 characters. Top level needs rubric_version, judge_snapshot_hash, candidates, preference candidate-1|candidate-2|abstain, and rationale.\n"
        f"Locked task category: {manifest['classification']['category']}; tags: {','.join(manifest['classification']['tags'])}\nTask:\n{manifest['task']}\n"
        f"Judge snapshot hash: {expected}\nRubric hash: {manifest['rubric_hash']}\n"
        "Then finish with exactly one JSON object: {\"status\":\"ok\"|\"error\",\"sha\":null,\"pr_url\":null,\"summary\":\"one line\"}\n"
    ).encode()

def parse_codeoff_judge_result(raw: Any, expected_snapshot_hash: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict) or set(raw) != {"rubric_version", "judge_snapshot_hash", "candidates", "preference", "rationale"}:
        return None, {"error": "judge result must be the exact structured object", "code": "invalid_judge_result"}
    if raw["rubric_version"] != CODEOFF_RUBRIC_VERSION or raw["judge_snapshot_hash"] != expected_snapshot_hash:
        return None, {"error": "judge rubric or exact snapshot differs", "code": "judge_snapshot_mismatch"}
    if raw["preference"] not in CODEOFF_RUBRIC["preference"] or not isinstance(raw["rationale"], str) or not raw["rationale"].strip() or len(raw["rationale"]) > CODEOFF_MAX_RATIONALE:
        return None, {"error": "judge preference/rationale is invalid", "code": "invalid_judge_result"}
    candidates = raw.get("candidates")
    if not isinstance(candidates, dict) or set(candidates) != {"candidate-1", "candidate-2"}:
        return None, {"error": "judge must score exactly both blind candidates", "code": "invalid_judge_result"}
    parsed_candidates = {}
    dimensions = CODEOFF_RUBRIC["dimensions"]
    for label, candidate in candidates.items():
        if not isinstance(candidate, dict) or set(candidate) != {"scores", "pass", "rationale"} or type(candidate.get("pass")) is not bool or not isinstance(candidate.get("rationale"), str) or not candidate["rationale"].strip() or len(candidate["rationale"]) > CODEOFF_MAX_RATIONALE:
            return None, {"error": f"{label} result is invalid", "code": "invalid_judge_result"}
        scores = candidate.get("scores")
        if not isinstance(scores, dict) or set(scores) != set(dimensions):
            return None, {"error": f"{label} scores have wrong keys", "code": "invalid_judge_result"}
        for key, maximum in dimensions.items():
            if type(scores[key]) is not int or not 0 <= scores[key] <= maximum:
                return None, {"error": f"{label}.{key} is out of range", "code": "invalid_judge_result"}
        parsed_candidates[label] = {"scores": dict(scores), "total": sum(scores.values()), "pass": candidate["pass"], "rationale": candidate["rationale"].strip()}
    return {"rubric_version": CODEOFF_RUBRIC_VERSION, "judge_snapshot_hash": expected_snapshot_hash, "candidates": parsed_candidates, "preference": raw["preference"], "rationale": raw["rationale"].strip()}, None

def codeoff_judge(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id", "slot", "job_id"}, exact=True)
    if err:
        return 400, err
    assert data is not None
    slot = data.get("slot")
    if slot not in ("judge-fable", "judge-1", "judge-2"):
        return 400, {"error": "invalid judge slot", "code": "bad_slot"}
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409 if load_err["code"] == "experiment_finalized" else 400, load_err
        assert root and manifest and state
        identity_err = codeoff_manifest_identity_error(manifest)
        if identity_err:
            return 409, identity_err
        if state.get("status") not in ("blinded", "judging"):
            return 409, {"error": "blind bundles must be frozen before judging", "code": "not_blinded"}
        if slot in state["judges"]:
            return 409, {"error": "judge receipt is already immutable", "code": "judge_recorded"}
        job, job_err = _codeoff_validate_job(data.get("job_id"), state, manifest, slot)
        if job_err:
            return 409, job_err
        private = json.loads((root / "private" / "blinding.json").read_text())
        path = codeoff_workspace_path(manifest["experiment_id"], slot) / "judge-result.json"
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024:
                raise ValueError("judge result exceeds limit")
            raw = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            _codeoff_finalize_record(root, manifest, state, "parked", f"invalid_judge_result:{slot}")
            return 422, {"error": "judge result file is missing or invalid", "code": "invalid_judge_result", "slot": slot}
        parsed, parse_err = parse_codeoff_judge_result(raw, private["judges"][slot]["judge_snapshot_hash"])
        if parse_err:
            _codeoff_finalize_record(root, manifest, state, "parked", f"{parse_err['code']}:{slot}")
            return 422, parse_err
        assert parsed and job
        receipt = {"version": "code-off-judge-receipt-v1", "slot": slot, "identity": manifest["identities"][slot], "execution_identity": job["execution_identity"], "job_id": job["job_id"], "started_at": job.get("started_at"), "finished_at": job.get("finished_at"), "elapsed_seconds": _codeoff_elapsed(job.get("started_at"), job.get("finished_at")), **parsed}
        receipt_hash = _codeoff_artifact(root, codeoff_canonical_json(receipt))
        state["judges"][slot] = {**parsed, "receipt_hash": receipt_hash}
        state["status"] = "judging"
        _codeoff_commit_event(root, state, "judge_recorded", {"slot": slot, "receipt_hash": receipt_hash, "preference": parsed["preference"]})
    return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "slot": slot, "status": "recorded", "judge_receipt_hash": receipt_hash, "preference": parsed["preference"], "totals": {label: item["total"] for label, item in parsed["candidates"].items()}}

def aggregate_codeoff_results(mapping: dict[str, dict[str, str]], receipts: dict[str, dict[str, Any]], tests: dict[str, bool]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    expected = {"judge-fable", "judge-1", "judge-2"}
    missing = sorted(expected - set(receipts))
    if missing or set(mapping) != expected:
        return None, {"error": "all three fixed judge slots are required; no replacement", "code": "missing_judges", "missing": missing}
    votes = {"author-1": 0, "author-2": 0}
    totals = {"author-1": 0, "author-2": 0}
    preferences = {}
    for judge in sorted(expected):
        labels = mapping[judge]
        receipt = receipts[judge]
        preference = receipt["preference"]
        actual = labels.get(preference) if preference != "abstain" else None
        preferences[judge] = actual or "abstain"
        if actual:
            votes[actual] += 1
        for label in ("candidate-1", "candidate-2"):
            totals[labels[label]] += int(receipt["candidates"][label]["total"])
    winner = next((slot for slot, count in votes.items() if count >= 2), None)
    basis = "preference-majority"
    if winner is None:
        basis = "summed-scores"
        if totals["author-1"] == totals["author-2"]:
            return {"version": CODEOFF_AGGREGATION_VERSION, "decision": "parked", "reason": "exact_score_tie", "winner": None, "basis": basis, "votes": votes, "preferences": preferences, "summed_scores": totals, "tests": tests, "promotion_eligible": False}, None
        winner = max(totals, key=totals.get)
    return {"version": CODEOFF_AGGREGATION_VERSION, "decision": "winner", "reason": None, "winner": winner, "basis": basis, "votes": votes, "preferences": preferences, "summed_scores": totals, "tests": tests, "promotion_eligible": tests.get(winner) is True}, None

def codeoff_aggregate(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id"}, exact=True)
    if err:
        return 400, err
    assert data is not None
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409 if load_err["code"] == "experiment_finalized" else 400, load_err
        assert root and manifest and state
        identity_err = codeoff_manifest_identity_error(manifest)
        if identity_err:
            return 409, identity_err
        private = json.loads((root / "private" / "blinding.json").read_text()) if (root / "private" / "blinding.json").is_file() else {"judges": {}}
        mapping = {slot: item["labels"] for slot, item in private["judges"].items()}
        tests = {slot: state["candidates"][slot].get("tests", {}).get("pass") is True for slot in ("author-1", "author-2")}
        aggregation, aggregate_err = aggregate_codeoff_results(mapping, state["judges"], tests)
        if aggregate_err:
            _codeoff_finalize_record(root, manifest, state, "parked", aggregate_err["code"])
            return 409, aggregate_err
        assert aggregation
        state["aggregation"] = aggregation
        _codeoff_event(root, state, "aggregated", {"decision": aggregation["decision"], "winner": aggregation["winner"], "basis": aggregation["basis"], "promotion_eligible": aggregation["promotion_eligible"]})
        if aggregation["decision"] == "parked":
            _codeoff_finalize_record(root, manifest, state, "parked", aggregation["reason"])
        elif not aggregation["promotion_eligible"]:
            _codeoff_finalize_record(root, manifest, state, "parked", "winner_tests_failed")
        else:
            state["status"] = "aggregated"
            _codeoff_write_state(root, state)
    return 200, {"ok": True, "experiment_id": manifest["experiment_id"], **aggregation}

def _codeoff_clear_tree(root: Path) -> None:
    for child in os.scandir(root):
        if child.name == ".git":
            continue
        path = Path(child.path)
        if child.is_dir(follow_symlinks=False):
            shutil.rmtree(path)
        else:
            path.unlink()

def _codeoff_apply_verified(verified: dict[str, Any], target: Path, base_sha: str) -> None:
    tree = verified.get("git_tree")
    if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise RuntimeError("verified git tree is unavailable")
    if not run_git(target, ["read-tree", "--reset", "-u", tree]).get("ok"):
        raise RuntimeError("git refused verified tree application")
    for entry in verified["entries"]:
        if entry["type"] == "directory":
            path = target / entry["path"]
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(int(entry["mode"], 8))
        elif entry["type"] == "file":
            (target / entry["path"]).chmod(int(entry["mode"], 8))
    if not run_git(target, ["reset", "--mixed", base_sha]).get("ok"):
        raise RuntimeError("git index reset failed after verified tree application")

def _codeoff_restore_base(target: Path, manifest: dict[str, Any]) -> bool:
    hard = run_git(target, ["reset", "--hard", manifest["base_sha"]])
    clean = run_git(target, ["clean", "-fdx"])
    try:
        snapshot = codeoff_tree_snapshot(target, manifest["base_sha"])
    except RuntimeError:
        return False
    branch, head, err = current_branch_head(target)
    return bool(hard.get("ok") and clean.get("ok") and snapshot["manifest_hash"] == manifest["base_snapshot_hash"] and not err and branch == manifest["branch"] and head == manifest["base_sha"])

def codeoff_finalize(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id"}, exact=True)
    if err:
        return 400, err
    assert data is not None
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409 if load_err["code"] == "experiment_finalized" else 400, load_err
        assert root and manifest and state
        identity_err = codeoff_manifest_identity_error(manifest)
        if identity_err:
            return 409, identity_err
        aggregation = state.get("aggregation") or {}
        if state.get("status") != "aggregated" or aggregation.get("promotion_eligible") is not True:
            return 409, {"error": "a tested aggregate winner is required", "code": "not_promotion_eligible"}
        winner = aggregation["winner"]
        candidate = dict(state["candidates"][winner])
        repo_raw = repos.get(manifest["repo"])
        repo = Path(repo_raw) if isinstance(repo_raw, str) and repo_raw else Path("/__graphwing_missing_repo__")
        if not repo.is_dir():
            return 409, {"error": "target repo is no longer allowlisted", "code": "repo_unavailable"}
        branch, head, git_err = current_branch_head(repo)
        if git_err or branch != manifest["branch"] or head != manifest["base_sha"] or not run_git(repo, ["diff", "--cached", "--quiet", manifest["base_sha"]]).get("ok"):
            return 409, {"error": "target repo identity changed", "code": "promotion_base_mismatch"}
        target_before = codeoff_tree_snapshot(repo, manifest["base_sha"])
        winner_path = codeoff_workspace_path(manifest["experiment_id"], winner)
        winner_now = codeoff_tree_snapshot(winner_path, manifest["base_sha"])
        _codeoff_read_artifact(root, candidate["artifact_hash"])
        if target_before["manifest_hash"] != manifest["base_snapshot_hash"]:
            return 409, {"error": "target worktree is not the exact pristine base", "code": "promotion_base_mutated"}
        if winner_now["manifest_hash"] != candidate["tree_manifest_hash"]:
            return 409, {"error": "frozen winner equality check failed", "code": "winner_mutated"}
    verified = _codeoff_verify_artifact(root, manifest, repo, candidate, "final")
    verification, exact = verified["tests"], verified["no_mutation"]
    eligible = verification["pass"] and exact
    if not eligible:
        with CODEOFF_LOCK:
            root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
            if load_err:
                return 409, load_err
            assert root and manifest and state
            final_receipt = {"version": "code-off-final-verification-v1", "winner": winner, "artifact_hash": candidate["artifact_hash"], "tree_manifest_hash": candidate["tree_manifest_hash"], "no_mutation": exact, "tests_pass": verification["pass"], "test_receipt_hash": verification["receipt_hash"], "commit_eligible": False, "push_eligible": False}
            receipt_hash = _codeoff_artifact(root, codeoff_canonical_json(final_receipt))
            final_receipt["receipt_hash"] = receipt_hash
            state["final_verification"] = final_receipt
            state["final_test_summary"] = _codeoff_test_summary(verification)
            _codeoff_event(root, state, "final_verification", {"winner": winner, "receipt_hash": receipt_hash, "tests_pass": verification["pass"], "no_mutation": exact, "eligible": False})
            _codeoff_finalize_record(root, manifest, state, "parked", "final_tests_failed" if not verification["pass"] else "final_tree_mutated")
        return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "status": "parked", **final_receipt}
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=True)
        if load_err:
            return 409, load_err
        assert root and manifest and state
        current_candidate = state["candidates"][winner]
        target_now = codeoff_tree_snapshot(repo, manifest["base_sha"])
        winner_now = codeoff_tree_snapshot(winner_path, manifest["base_sha"])
        if target_now["manifest_hash"] != manifest["base_snapshot_hash"] or current_branch_head(repo) != (manifest["branch"], manifest["base_sha"], None) or not run_git(repo, ["diff", "--cached", "--quiet", manifest["base_sha"]]).get("ok"):
            return 409, {"error": "target changed during isolated verification", "code": "promotion_base_mutated"}
        if winner_now["manifest_hash"] != candidate["tree_manifest_hash"] or current_candidate.get("artifact_hash") != candidate["artifact_hash"]:
            return 409, {"error": "winner changed during isolated verification", "code": "winner_mutated"}
        try:
            _codeoff_apply_verified(verified, repo, manifest["base_sha"])
            after = codeoff_tree_snapshot(repo, manifest["base_sha"])
            after_branch, after_head, after_err = current_branch_head(repo)
            exact = after["manifest_hash"] == candidate["tree_manifest_hash"] and not after_err and after_branch == manifest["branch"] and after_head == manifest["base_sha"] and run_git(repo, ["diff", "--cached", "--quiet", manifest["base_sha"]]).get("ok") is True
            if not exact:
                raise RuntimeError("applied tree differs from verified winner")
        except (OSError, RuntimeError):
            restored = _codeoff_restore_base(repo, manifest)
            return 500, {"ok": False, "error": "verified winner application failed and target was restored" if restored else "verified winner application and base restoration failed", "code": "promotion_apply_failed" if restored else "promotion_restore_failed", "retryable": restored}
        final_receipt = {"version": "code-off-final-verification-v1", "winner": winner, "artifact_hash": candidate["artifact_hash"], "tree_manifest_hash": candidate["tree_manifest_hash"], "promoted_tree_hash": after["manifest_hash"], "no_mutation": True, "tests_pass": True, "test_receipt_hash": verification["receipt_hash"], "commit_eligible": True, "push_eligible": True}
        try:
            receipt_hash = _codeoff_artifact(root, codeoff_canonical_json(final_receipt))
            final_receipt["receipt_hash"] = receipt_hash
            state["promotion"] = {"winner": winner, "artifact_hash": candidate["artifact_hash"], "tree_manifest_hash": candidate["tree_manifest_hash"], "promoted_tree_hash": after["manifest_hash"], "git_tree": verified["git_tree"]}
            state["final_verification"] = final_receipt
            state["final_test_summary"] = _codeoff_test_summary(verification)
            _codeoff_event(root, state, "final_verification", {"winner": winner, "receipt_hash": receipt_hash, "tests_pass": True, "no_mutation": True, "eligible": True})
            _codeoff_finalize_record(root, manifest, state, "completed", None)
        except (OSError, RuntimeError):
            try:
                disk = json.loads((root / "state.json").read_text())
                committed = disk.get("finalized") and disk.get("final_verification", {}).get("receipt_hash") == final_receipt.get("receipt_hash")
            except (OSError, json.JSONDecodeError):
                committed = False
            if not committed and not _codeoff_restore_base(repo, manifest):
                return 500, {"ok": False, "error": "final receipt persistence failed and base restoration failed", "code": "promotion_restore_failed", "retryable": False}
            if not committed:
                raise
    return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "status": "completed", **final_receipt}

def _codeoff_audit_judges(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Project judge receipts without republishing per-judge blind ordering.

    Before finalization only the receipt hash is public. After finalization the
    audit needs each judge's decision, so scores and the preference are stated
    in author-slot terms. The blind label a judge saw is never emitted.

    Key order is authority too: the wire body is not canonicalized, so emitting
    each judge's totals in the order that judge's own result file listed the
    blind labels would republish the exact per-judge shuffle. Totals are keyed
    by author slot and sorted by author slot, so the projection reads the same
    for every judge whatever ordering that judge saw.
    """
    judges = state.get("judges", {})
    if state.get("finalized") is not True:
        return {slot: {"receipt_hash": value.get("receipt_hash")} for slot, value in judges.items()}
    try:
        private = json.loads((root / "private" / "blinding.json").read_text())
        mapping = {slot: item["labels"] for slot, item in private["judges"].items()}
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        mapping = {}
    projected = {}
    for slot, value in judges.items():
        labels = mapping.get(slot) or {}
        preference = value.get("preference")
        scored = value.get("candidates") or {}
        projected[slot] = {
            "receipt_hash": value.get("receipt_hash"),
            "preference": "abstain" if preference == "abstain" else labels.get(preference),
            "totals": {
                author_slot: scored[label].get("total")
                for label, author_slot in sorted(labels.items(), key=lambda item: item[1])
                if isinstance(scored.get(label), dict)
            },
        }
    return projected


def codeoff_audit(qs: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    with CODEOFF_LOCK: root, manifest, state, err = _codeoff_load(first_query(qs, "experiment_id"), active=False)
    if err:
        return 404 if err["code"] == "experiment_not_found" else 400, err
    assert root and manifest and state
    identities = {slot: {key: identity.get(key) for key in ("logical_model", "exact_model", "provider", "launcher", "runnable", "proven", "manifest_version", "manifest_current", *CODEOFF_IDENTITY_FIELDS)} for slot, identity in manifest["identities"].items()}
    selection = {key: manifest["selection"][key] for key in ("algorithm_version", "policy_version", "ordered_pool", "authors", "fixed_judge", "random_judges", "seed_commitment")}
    candidates = {slot: {key: value.get(key) for key in ("tree_manifest_hash", "artifact_hash", "freeze_receipt_hash") if value.get(key)} | ({"tests_pass": value["tests"]["pass"], "test_receipt_hash": value["tests"]["receipt_hash"]} if value.get("tests") else {}) for slot, value in state["candidates"].items()}
    judges = _codeoff_audit_judges(root, state)
    return 200, {"ok": True, "protocol_version": manifest["protocol_version"], "experiment_id": manifest["experiment_id"], "status": state["status"], "reason": state.get("reason"), "finalized": state["finalized"], "classification": manifest["classification"], "original_classification": manifest["original_classification"], "category_source": manifest["category_source"], "selection": selection, "identities": identities, "identity_snapshot_hashes": manifest.get("identity_snapshot_hashes"), "identities_hash": manifest.get("identities_hash"), "inference_profile_version": manifest.get("inference_profile_version"), "fable_provider_overlap": manifest["fable_provider_overlap"], "base_sha": manifest["base_sha"], "base_tree": manifest["base_tree"], "prompt_hash": manifest["prompt_hash"], "rubric_hash": manifest["rubric_hash"], "manifest_file_hash": state["manifest_file_hash"], "event_head": state["event_head"], "event_count": state["event_count"], "candidates": candidates, "judges": judges, "aggregation": state.get("aggregation") if state.get("finalized") or not state.get("aggregation") else {key: value for key, value in state["aggregation"].items() if key != "preferences"}, "promotion": state.get("promotion"), "final_verification": state.get("final_verification"), "final_file_hash": state.get("final_file_hash"), "economics": state.get("economics"), "economics_hash": state.get("economics_hash")}

def _codeoff_wait_for_jobs(state: dict[str, Any], timeout_seconds: int = CODEOFF_TERMINAL_DRAIN_SECONDS) -> dict[str, Any] | None:
    job_ids = [job_id for jobs in state.get("agent_jobs", {}).values() for job_id in jobs]
    deadline = time.monotonic() + timeout_seconds
    while True:
        active = []
        for job_id in job_ids:
            job = read_job(job_id)
            if not is_agent_job_record(job):
                return {"error": "launched code-off job record is unavailable", "code": "agent_job_missing", "job_id": job_id}
            assert isinstance(job, dict)
            if job.get("status") in ("queued", "running"):
                active.append(job_id)
        if not active:
            return None
        if time.monotonic() >= deadline:
            return {"error": "launched code-off jobs did not finish before terminalization", "code": "agent_jobs_active", "job_ids": active}
        time.sleep(0.2)


def _codeoff_terminal_result(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The durable terminal receipt Rewst persists as the permanent record."""
    return {
        "ok": True, "experiment_id": manifest["experiment_id"],
        "status": state["status"], "reason": state.get("reason"), "finalized": True,
        "economics": state.get("economics"), "economics_hash": state.get("economics_hash"),
    }


def codeoff_terminal(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id", "outcome"}, exact=True)
    if err:
        return 400, err
    assert data is not None
    outcome = data.get("outcome")
    if outcome not in ("failed", "succeeded"):
        return 400, {"error": "outcome must be failed or succeeded", "code": "bad_outcome"}
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=False)
        if load_err:
            if load_err.get("code") == "experiment_not_found":
                if outcome == "succeeded":
                    return 409, {"error": "successful graph terminal requires completed durable finalization", "code": "not_finalized"}
                return 200, {"ok": True, "experiment_id": data["experiment_id"], "status": "absent", "reason": None, "finalized": False, "economics": None, "economics_hash": None}
            return 400, load_err
        assert root and manifest and state
        if state.get("finalized"):
            return 200, _codeoff_terminal_result(manifest, state)
        if outcome == "succeeded":
            return 409, {"error": "successful graph terminal requires completed durable finalization", "code": "not_finalized"}
        launched = state.get("agent_jobs", {})
    drain_err = _codeoff_wait_for_jobs(state)
    if drain_err:
        return 409, drain_err
    with CODEOFF_LOCK:
        root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=False)
        if load_err:
            return 400, load_err
        assert root and manifest and state
        if state.get("finalized"):
            return 200, _codeoff_terminal_result(manifest, state)
        if state.get("agent_jobs", {}) != launched:
            return 409, {"error": "code-off launches changed during terminalization", "code": "agent_jobs_changed"}
        _codeoff_finalize_record(root, manifest, state, "parked", "graph_terminal_failure")
        return 200, _codeoff_terminal_result(manifest, state)


def codeoff_cleanup(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = _codeoff_body(body, {"experiment_id"}, exact=True)
    if err:
        return 400, err
    assert data is not None
    root, manifest, state, load_err = _codeoff_load(data.get("experiment_id"), active=False)
    if load_err:
        return 400, load_err
    assert root and manifest and state
    if not state.get("finalized"):
        return 409, {"error": "cleanup is refused before finalization", "code": "not_finalized"}
    workspace_root = CODEOFF_WORKSPACES_DIR / manifest["experiment_id"]
    repo_raw = repos.get(manifest["repo"])
    repo = Path(repo_raw) if isinstance(repo_raw, str) and repo_raw else Path("/__graphwing_missing_repo__")
    removed = []
    for slot in ("author-1", "author-2"):
        path = codeoff_workspace_path(manifest["experiment_id"], slot)
        if path.exists() and repo.is_dir():
            result = run_git(repo, ["worktree", "remove", "--force", str(path)])
            if not result.get("ok"):
                return 500, {"error": "git refused code-off worktree cleanup", "code": "cleanup_failed", "slot": slot}
            removed.append(slot)
    for slot in ("judge-fable", "judge-1", "judge-2"):
        path = codeoff_workspace_path(manifest["experiment_id"], slot)
        if path.exists():
            shutil.rmtree(path)
            removed.append(slot)
    if workspace_root.exists() and not any(workspace_root.iterdir()):
        workspace_root.rmdir()
    return 200, {"ok": True, "experiment_id": manifest["experiment_id"], "status": "cleaned", "removed_slots": removed, "record_preserved": True}

def _codeoff_boundary(operation: Any, *args: Any) -> tuple[int, dict[str, Any]]:
    try:
        return operation(*args)
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired, tarfile.TarError, json.JSONDecodeError):
        return 500, {"ok": False, "error": "code-off operation was interrupted; retry the same immutable request", "code": "codeoff_runtime_failure", "retryable": True}

def _resolve_codeoff_agent(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict) or set(raw) != {"experiment_id", "slot"}:
        return None, {"error": "codeoff_workspace accepts only experiment_id and slot", "code": "bad_codeoff_workspace"}
    slot = raw.get("slot")
    if slot not in ("author-1", "author-2", "judge-fable", "judge-1", "judge-2"):
        return None, {"error": "unknown code-off slot", "code": "bad_codeoff_workspace"}
    root, manifest, state, err = _codeoff_load(raw.get("experiment_id"), active=True)
    if err:
        return None, err
    assert root and manifest and state
    identity_err = codeoff_manifest_identity_error(manifest)
    if identity_err:
        return None, identity_err
    workspace = codeoff_workspace_path(manifest["experiment_id"], slot)
    if not workspace.is_dir():
        return None, {"error": "opaque code-off workspace is unavailable", "code": "workspace_unavailable"}
    if slot.startswith("author-"):
        if state["status"] not in ("prepared", "authors_running", "candidates_running", "candidates_tested"):
            return None, {"error": "author launch is not allowed in this stage", "code": "bad_experiment_stage"}
        prompt_hash = manifest["prompt_hash"]
        prompt = _codeoff_read_artifact(root, prompt_hash)
        budget = manifest["budgets"]["author_seconds"]
    else:
        if state["status"] not in ("blinded", "judging"):
            return None, {"error": "judge launch requires frozen blind bundles", "code": "bad_experiment_stage"}
        private = json.loads((root / "private" / "blinding.json").read_text())
        prompt = _codeoff_judge_prompt(manifest, private, slot)
        prompt_hash = hashlib.sha256(prompt).hexdigest()
        budget = manifest["budgets"]["judge_seconds"]
    return {"root": root, "manifest": manifest, "state": state, "slot": slot, "cwd": workspace, "repo_name": f"codeoff:{manifest['experiment_id']}:{slot}", "prompt": prompt, "prompt_hash": prompt_hash, "identity": manifest["identities"][slot], "budget": budget, "max_turns": manifest["budgets"]["max_turns"]}, None


def agent_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {
        "prompt", "launcher", "provider", "model", "max_turns", "run_budget_seconds",
        "session_identity", "resume_job_id", "cwd", "response_webhook_url",
        "response_webhook_token", "resume_url", "codeoff_workspace", "effort",
        "route_execution_profile",
    }
    unexpected = sorted(set(data) - allowed)
    if unexpected:
        return 400, {"error": "request contains unsupported fields", "code": "unexpected_fields", "fields": unexpected}
    codeoff: dict[str, Any] | None = None
    if data.get("codeoff_workspace") is not None:
        if data.get("prompt") not in (None, "") or data.get("cwd") not in (None, ""):
            return 400, {"error": "code-off jobs resolve prompt and cwd from the opaque reference", "code": "bad_codeoff_workspace"}
        codeoff, codeoff_err = _resolve_codeoff_agent(data.get("codeoff_workspace"))
        if codeoff_err:
            return 409 if codeoff_err.get("code") in ("experiment_finalized", "bad_experiment_stage", "codeoff_identity_unpinned") else 400, codeoff_err
        assert codeoff is not None
        prompt_bytes = codeoff["prompt"]
        prompt = prompt_bytes.decode("utf-8")
        repo_name, resolved = codeoff["repo_name"], codeoff["cwd"]
    else:
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return 400, {"error": "prompt is required", "code": "missing_prompt"}
        prompt = prompt.strip()
        if len(prompt) > PROMPT_MAX_CHARS:
            return 400, {"error": "prompt too large", "code": "prompt_too_large"}
        cwd_raw = data.get("cwd")
        if cwd_raw is not None and not isinstance(cwd_raw, str):
            return 400, {"error": "cwd must be a repo short name", "code": "bad_cwd"}
        repo_name, resolved = resolve_run_cwd(cwd_raw, repos)
        if repo_name is None:
            return 400, resolved
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    launcher = data.get("launcher")
    if not isinstance(launcher, str) or not launcher.strip():
        return 400, {"error": "launcher is required", "code": "missing_launcher"}
    launcher = launcher.strip()
    native_spec = NATIVE_LAUNCHERS.get(launcher)
    if native_spec is None:
        return 400, {"error": "launcher must be claude, codex, or grok", "code": "bad_launcher"}
    provider = data.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return 400, {"error": "provider is required", "code": "missing_provider"}
    provider = provider.strip()
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        return 400, {"error": "model is required", "code": "missing_model"}
    model = model.strip()
    if len(model) > 80:
        return 400, {"error": "model is invalid", "code": "bad_model"}
    if codeoff is not None:
        exact = codeoff["identity"]
        if (launcher, provider, model) != (exact["launcher"], exact["provider"], exact["exact_model"]):
            return 400, {"error": "code-off identity differs from the immutable snapshot; fallback/substitution is forbidden", "code": "codeoff_identity_mismatch"}
    elif provider != native_spec["provider"] or model not in native_spec["models"]:
        return 400, {"error": f"invalid provider/model for {launcher} launcher", "code": "bad_model_identity"}
    requested_identity, identity_err = parse_session_identity(data.get("session_identity"))
    if identity_err:
        return 400, identity_err
    route_profile: dict[str, str] | None = None
    requested_effort = data.get("effort", "default")
    if requested_identity is not None:
        requested_effort = requested_identity["requested_effort"]
        if "effort" in data and data.get("effort") != requested_effort:
            return 400, {
                "error": "resume effort must equal the pinned session identity",
                "code": "session_identity_mismatch",
            }
        source = requested_identity["effort_source"]
        if source == "route":
            if data.get("route_execution_profile") != requested_identity.get("route_execution_profile"):
                return 400, {
                    "error": "resume route evidence must equal the pinned session identity",
                    "code": "session_identity_mismatch",
                }
            route_profile, route_error = parse_route_execution_profile(
                data.get("route_execution_profile"), expected_role="writer",
                launcher=launcher, provider=provider, model=model, effort=requested_effort,
            )
            if route_error is not None or route_profile is None:
                return 400, route_error or {
                    "error": "invalid route execution profile", "code": "bad_route_execution_profile"
                }
        elif "route_execution_profile" in data:
            return 400, {
                "error": "direct resume cannot add route attribution",
                "code": "session_identity_mismatch",
            }
        effort, effort_err = normalize_effort(
            launcher, provider, model, requested_effort, source
        )
    elif "route_execution_profile" in data:
        route_profile, route_error = parse_route_execution_profile(
            data.get("route_execution_profile"), expected_role="writer",
            launcher=launcher, provider=provider, model=model, effort=requested_effort,
        )
        if route_error is not None or route_profile is None:
            return 400, route_error or {
                "error": "invalid route execution profile", "code": "bad_route_execution_profile"
            }
        effort, effort_err = normalize_effort(
            launcher, provider, model, requested_effort, "route"
        )
    else:
        effort, effort_err = normalize_effort(
            launcher, provider, model, requested_effort,
            "explicit" if "effort" in data else "launcher_default",
        )
    if effort_err:
        return 400, effort_err
    assert effort is not None
    # Effective effort alone is not the identity: `default` and `high` collapse
    # to one effective value on every codex and grok model, so a slot could
    # otherwise record a requested effort or source the snapshot never pinned.
    if codeoff is not None and effort != {
        "requested_effort": codeoff["identity"].get("requested_effort"),
        "effective_effort": codeoff["identity"].get("effective_effort"),
        "effort_source": CODEOFF_EFFORT_SOURCE,
    }:
        return 400, {"error": "code-off effort profile differs from the immutable snapshot; fallback/substitution is forbidden", "code": "codeoff_identity_mismatch"}
    turns, turns_err = parse_optional_int(data, "max_turns", 1, 80)
    if turns_err:
        return 400, turns_err
    budget, budget_err = parse_optional_int(data, "run_budget_seconds", 30, CODEOFF_MAX_AGENT_SECONDS)
    if budget_err:
        return 400, budget_err
    if codeoff is not None and (turns != codeoff["max_turns"] or budget != codeoff["budget"]):
        return 400, {"error": "code-off job must use the immutable turn and time budgets", "code": "codeoff_budget_mismatch"}
    binary = resolve_launcher_binary_now(launcher)
    launcher_version = launcher_version_fingerprint(binary)
    if launcher_version is None:
        classified = classify_agent_failure("launcher_version_mismatch")
        return 501, {
            "error": "launcher version is unavailable",
            "code": "launcher_version_mismatch",
            **classified,
        }
    if codeoff is not None and launcher_version != codeoff["identity"].get("launcher_version"):
        return 409, {"error": "code-off launcher wrapper fingerprint differs from the immutable snapshot", "code": "codeoff_identity_mismatch"}
    if not binary.is_file() and not webhook_url:
        classified = classify_agent_failure("missing_binary")
        return 501, {"error": f"{launcher} binary missing: {binary}", "code": "missing_binary", **classified}
    if codeoff is None:
        top = run_git(resolved, ["rev-parse", "--show-toplevel"])
        if not top.get("ok") or Path(str(top.get("stdout") or "").strip()).resolve() != resolved.resolve():
            return 400, {"error": "ordinary writer repo alias must be the Git worktree top level", "code": "repo_not_toplevel"}
    branch, head, git_err = current_branch_head(resolved)
    if git_err:
        return 400, git_err
    session_identity = {
        "launcher": launcher, "provider": provider, "model": model,
        "requested_effort": effort["requested_effort"],
        "effective_effort": effort["effective_effort"],
        "effort_source": effort["effort_source"],
        "launcher_version": launcher_version,
        "repo": repo_name, "branch": branch, "starting_head": head, "native_session_id": None,
    }
    if route_profile is not None:
        session_identity["route_execution_profile"] = route_profile
    resume_parent: dict[str, Any] | None = None
    if requested_identity is not None:
        if requested_identity.get("native_session_id") is None:
            return 400, {"error": "resume session_identity requires native_session_id", "code": "missing_native_session"}
        expected = dict(session_identity, native_session_id=requested_identity.get("native_session_id"))
        if requested_identity != expected:
            return 400, {"error": f"session_identity does not match the current {launcher} job", "code": "session_identity_mismatch"}
        provenance_err = validate_native_resume_provenance(data, requested_identity, launcher)
        if provenance_err:
            return 400, provenance_err
        if codeoff is None:
            resume_parent = read_job(str(data["resume_job_id"])) or {}
            try:
                same_cwd = (
                    isinstance(resume_parent.get("cwd"), str)
                    and Path(resume_parent["cwd"]).resolve() == resolved.resolve()
                )
            except (OSError, RuntimeError, ValueError):
                same_cwd = False
            if resume_parent.get("repo") != repo_name or not same_cwd:
                return 400, {
                    "error": "resume job belongs to another repository checkout",
                    "code": "resume_repository_mismatch",
                }
        session_identity = requested_identity
    elif data.get("resume_job_id") not in (None, ""):
        return 400, {"error": f"{launcher} resume_job_id requires session_identity", "code": "launcher_state_mismatch"}
    writer_parent_paths: list[str] = []
    if resume_parent is not None:
        parent_paths = resume_parent.get("writer_paths")
        if isinstance(parent_paths, list):
            writer_parent_paths = [path for path in parent_paths if isinstance(path, str)]
    with (CODEOFF_LOCK if codeoff is not None else nullcontext()), (repo_lock(resolved) if codeoff is None else nullcontext()), JOB_LOCK:
        if codeoff is not None:
            fresh_root, fresh_manifest, fresh_state, fresh_err = _codeoff_load(codeoff["manifest"]["experiment_id"], active=True)
            if fresh_err:
                return 409, fresh_err
            assert fresh_root and fresh_manifest and fresh_state
            prior_jobs = fresh_state.get("agent_jobs", {}).get(codeoff["slot"], [])
            resume_job_id = data.get("resume_job_id")
            if resume_job_id not in (None, "") and resume_job_id not in prior_jobs:
                return 400, {"error": "resume job is not pinned to this code-off slot", "code": "codeoff_resume_mismatch"}
            if resume_job_id in (None, "") and prior_jobs:
                return 409, {"error": "slot is already launched; replacement/redraw is forbidden", "code": "slot_already_launched"}
            codeoff.update({"root": fresh_root, "manifest": fresh_manifest, "state": fresh_state})
        if codeoff is None:
            for path in JOBS_DIR.glob("*/job.json"):
                active = read_job(path.parent.name) or {}
                if active.get("status") not in ("queued", "running") or Path(str(active.get("cwd") or "")).resolve() != resolved.resolve():
                    continue
                raw_kind = str(active.get("kind") or "")
                blocker_kind = "codeoff" if active.get("codeoff_workspace") else (raw_kind if raw_kind in {"script", "test", "rr", "review"} else "agent")
                blocker_id = str(active.get("job_id") or path.parent.name)
                return 409, {
                    "error": f"{blocker_kind} job owns this checkout", "code": "writer_checkout_busy",
                    "blocker_kind": blocker_kind, "blocker_job_id": blocker_id if JOB_ID_RE.fullmatch(blocker_id) else "unknown",
                }
            git_baseline, baseline_err = capture_writer_baseline(resolved)
            if baseline_err:
                return 409, baseline_err
        else:
            git_baseline = None
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        log_ref = str(job_dir(job_id) / "stdout.log")
        job = {
            "job_id": job_id,
            "kind": "agent",
            "status": "queued",
            "repo": repo_name,
            "branch": branch,
            "starting_head": head,
            "cwd": str(resolved),
            "prompt": prompt if codeoff is None else f"[code-off prompt {codeoff['prompt_hash']}]",
            "launcher": launcher,
            "provider": provider,
            "model": model,
            **effort,
            "launcher_version": launcher_version,
            "session_identity": session_identity,
            "launch_native_session_id": session_identity.get("native_session_id"),
            "max_turns": turns or AGENT_MAX_TURNS,
            "run_budget_seconds": budget or AGENT_RUN_BUDGET,
            "response_webhook_url": webhook_url,
            "response_webhook_token": webhook_token,
            "resume_url": webhook_url,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "receipt": None,
            "log_ref": log_ref,
            "error": None,
            "webhook": None,
        }
        if git_baseline is not None:
            job["git_baseline"] = git_baseline
            job["writer_parent_paths"] = writer_parent_paths
            job["writer_parent_job_id"] = data.get("resume_job_id")
        if codeoff is not None:
            job["codeoff_workspace"] = {"experiment_id": codeoff["manifest"]["experiment_id"], "slot": codeoff["slot"]}
            job["prompt_hash"] = codeoff["prompt_hash"]
        job_dir(job_id).mkdir(parents=True, exist_ok=True)
        prompt_path = job_dir(job_id) / "prompt.txt"
        if codeoff is not None and codeoff["slot"].startswith("author-"):
            prompt_path.symlink_to(codeoff["root"] / "artifacts" / codeoff["prompt_hash"])
        elif codeoff is not None:
            prompt_path.write_bytes(prompt_bytes)
        else:
            prompt_path.write_text(wrap_prompt(job_id, prompt, str(resolved)))
        write_job(job)
        if codeoff is not None:
            try:
                state = codeoff["state"]
                execution_manifest = _codeoff_launch_execution_manifest(job)
                if execution_manifest is None:
                    raise RuntimeError("code-off launch manifest was incomplete")
                execution_manifest_hash = _codeoff_artifact(
                    codeoff["root"], codeoff_canonical_json(execution_manifest)
                )
                state["agent_jobs"][codeoff["slot"]].append(job_id)
                state["execution_manifests"][codeoff["slot"]][job_id] = {
                    "launch_manifest_hash": execution_manifest_hash,
                    "terminal_manifest_hash": None,
                }
                state["status"] = "authors_running" if codeoff["slot"].startswith("author-") else "judging"
                _codeoff_commit_event(codeoff["root"], state, "agent_launched", {"slot": codeoff["slot"], "job_id": job_id, "identity_snapshot_hash": hashlib.sha256(codeoff_canonical_json(codeoff["identity"])).hexdigest(), "execution_manifest_hash": execution_manifest_hash, "prompt_hash": codeoff["prompt_hash"]})
            except (OSError, RuntimeError):
                shutil.rmtree(job_dir(job_id), ignore_errors=True)
                return 500, {"ok": False, "error": "code-off launch persistence was interrupted; retry the same slot", "code": "codeoff_runtime_failure", "retryable": True}
    enqueue_agent(job)
    return 202, {
        "ok": True,
        "kind": "agent",
        "job_id": job_id,
        "status": "queued",
        "repo": repo_name,
        "launcher": launcher,
        "provider": provider,
        "model": model,
        **effort,
        "session_identity": session_identity,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def json_out(status: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    return status, payload, "application/json"


def repo_or_error(qs: dict[str, list[str]], repos: dict[str, str]):
    name, resolved = resolve_repo(first_query(qs, "repo"), repos)
    if name is None:
        return None, json_out(400, resolved)
    return (name, resolved), None


def dispatch_inner(
    method: str,
    path: str,
    qs: dict[str, list[str]],
    authed: bool,
    body: bytes,
    headers: Any = None,
) -> tuple[int, dict[str, Any] | bytes, str]:
    if method == "POST" and path == "/v1/doorbell/pr-drive":
        status, payload = doorbell_pr_drive(body, headers)
        return json_out(status, payload)

    repos = load_repos()
    if method == "GET" and path in ("/v1/health", "/health"):
        return json_out(200, {"ok": True, "service": "graphwing", "repos": sorted(repos)})
    if method == "GET" and path in ("/openapi.json", "/v1/openapi.json"):
        return 200, openapi_bytes(), "application/json"

    if not authed:
        return json_out(401, {"error": "invalid or missing X-Graphwing-Key", "code": "unauthorized"})

    if method == "GET" and path == "/v1/units/status":
        out = units_status()
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)
    if method == "GET" and path == "/v1/watch":
        return json_out(200, watch_snapshot())

    if method == "POST" and path == "/v1/code-off/prepare":
        status, payload = _codeoff_boundary(codeoff_prepare, body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/candidate":
        status, payload = _codeoff_boundary(codeoff_candidate, body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/blind":
        status, payload = _codeoff_boundary(codeoff_blind, body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/judge":
        status, payload = _codeoff_boundary(codeoff_judge, body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/aggregate":
        status, payload = _codeoff_boundary(codeoff_aggregate, body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/finalize":
        status, payload = _codeoff_boundary(codeoff_finalize, body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/code-off/audit":
        status, payload = _codeoff_boundary(codeoff_audit, qs)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/terminal":
        status, payload = _codeoff_boundary(codeoff_terminal, body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/code-off/cleanup":
        status, payload = _codeoff_boundary(codeoff_cleanup, body, repos)
        return json_out(status, payload)

    if method == "GET" and path == "/v1/herdr/agents":
        out = herdr_agents()
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)

    if method == "GET" and path == "/v1/stack/status":
        out = stack_status(first_query(qs, "stack"))
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)
    if method == "GET" and path == "/v1/port/check":
        out = port_check(first_query(qs, "port") or first_query(qs, "ports"))
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)
    if method == "POST" and path == "/v1/test/run":
        status, payload = test_run(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/rr/run":
        status, payload = rr_run(body, repos)
        return json_out(status, payload)

    if method == "POST" and path == "/v1/git/checkout":
        status, payload = git_checkout(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/restore":
        status, payload = git_restore(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/commit":
        status, payload = git_commit(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/push":
        status, payload = git_push(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/script/run":
        status, payload = script_run(body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/slice/frontier":
        status, payload = slice_frontier_op(qs, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/complete":
        status, payload = slice_complete(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/continue":
        status, payload = slice_continue(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/rewst/fire":
        status, payload = rewst_fire(body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/route":
        status, payload = slice_route(body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/route/fallback":
        status, payload = slice_route_fallback(body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/route/recovery":
        status, payload = slice_route_recovery(body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/review/run":
        status, payload = review_run(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/review/jobs/wait":
        status, payload = review_job_wait(body)
        return json_out(status, payload)
    if method == "GET" and path.startswith("/v1/review/jobs/"):
        job_id = path.removeprefix("/v1/review/jobs/").strip("/")
        if not JOB_ID_RE.fullmatch(job_id):
            return json_out(400, {"error": "invalid job_id", "code": "bad_job_id"})
        status, payload = review_job_response(job_id)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/e2e":
        status, payload = slice_e2e(body, repos)
        return json_out(status, payload)

    if path.startswith("/v1/git/") or path.startswith("/v1/gh/") or path == "/v1/file/head":
        got, err = repo_or_error(qs, repos)
        if err:
            return err
        repo_name, repo_path = got
        if method == "GET" and path == "/v1/git/status":
            out = git_status(repo_path)
        elif method == "GET" and path == "/v1/git/diff":
            out = git_diff(repo_path, first_query(qs, "ref"), query_rel(qs))
        elif method == "GET" and path == "/v1/git/log":
            try:
                n = int(first_query(qs, "n") or "20")
            except ValueError:
                return json_out(400, {"error": "n must be an integer", "code": "bad_n"})
            out = git_log(repo_path, max(1, min(n, 100)), first_query(qs, "ref"))
        elif method == "GET" and path == "/v1/git/branch":
            out = git_branch(repo_path)
        elif method == "GET" and path == "/v1/git/show":
            rev = first_query(qs, "rev")
            if not rev:
                return json_out(400, {"error": "rev is required", "code": "missing_rev"})
            out = git_show(repo_path, rev)
        elif method == "GET" and path == "/v1/git/rev-parse":
            out = git_rev_parse(repo_path, first_query(qs, "rev") or "HEAD")
        elif method == "GET" and path == "/v1/git/worktrees":
            out = git_worktree_list(repo_path)
        elif method == "GET" and path == "/v1/file/head":
            rel = query_rel(qs)
            if not rel:
                return json_out(400, {"error": "rel is required", "code": "missing_rel"})
            out = file_head(repo_path, rel)
        elif method == "GET" and path == "/v1/gh/pr/list":
            try:
                n = int(first_query(qs, "n") or "10")
            except ValueError:
                return json_out(400, {"error": "n must be an integer", "code": "bad_n"})
            n = max(1, min(n, 50))
            state = first_query(qs, "state") or "open"
            out = gh_json(repo_path, ["pr", "list", "--limit", str(n), "--state", state, "--json", "number,title,state,url,headRefName,baseRefName"])
        elif method == "GET" and path == "/v1/gh/pr/view":
            number = first_query(qs, "number")
            if not number:
                return json_out(400, {"error": "number is required", "code": "missing_number"})
            out = fresh_pr_view(repo_path, number)
        elif method == "GET" and path == "/v1/gh/pr/findings":
            number = first_query(qs, "number")
            if not number:
                return json_out(400, {"error": "number is required", "code": "missing_number"})
            raw = gh_json(repo_path, ["pr", "view", number, "--json", "labels,comments"])
            if not raw.get("ok"):
                out = raw
            else:
                d = raw.get("data") or {}
                out = pr_findings_from(
                    labels=[l.get("name", "") for l in (d.get("labels") or [])],
                    comment_bodies=[c.get("body", "") for c in (d.get("comments") or [])],
                )
                # Fold the check state in here. The graph cannot read it from
                # the checks node: that output is ~21KB and Rewst replaces it
                # with an artifact stub, so TASKS.checks.all_green is null.
                ck = annotate_pr_checks(
                    gh_json(repo_path, ["pr", "checks", number, "--json", "name,state,bucket,link"])
                )
                out["all_green"] = bool(ck.get("all_green"))
                out["any_red"] = bool(ck.get("any_red"))
                out["failing"] = (ck.get("failing") or [])[:20]
                # Flat, not nested under "data": the connector already exposes
                # this as TASKS.<node>.data.<field>, so nesting made every path
                # a double .data.data and drive_snap read nulls.
                out["status"] = 200 if out.get("ok") else 422
                # The findings list is for humans reading the trace; the brief
                # is what the writer consumes. Keep the payload small.
                out["findings"] = out.get("findings", [])[:20]
        elif method == "GET" and path == "/v1/gh/pr/checks":
            number = first_query(qs, "number")
            if not number:
                return json_out(400, {"error": "number is required", "code": "missing_number"})
            out = annotate_pr_checks(
                gh_json(repo_path, ["pr", "checks", number, "--json", "name,state,bucket,link"])
            )
        else:
            out = None
        if out is not None:
            out["repo"] = repo_name
            return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)

    if method == "POST" and path == "/v1/pr/continue":
        status, payload = pr_continue(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/gh/pr/merge":
        status, payload = gh_pr_merge(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/agent/run":
        status, payload = agent_run(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/agent/jobs/wait":
        status, payload = agent_job_wait(body)
        return json_out(status, payload)
    if method == "GET" and path.startswith("/v1/agent/jobs/"):
        job_id = path.removeprefix("/v1/agent/jobs/").strip("/")
        if not JOB_ID_RE.fullmatch(job_id):
            return json_out(400, {"error": "invalid job_id", "code": "bad_job_id"})
        job = read_job(job_id)
        if not is_agent_job_record(job):
            return json_out(404, {"error": "job not found", "code": "not_found"})
        assert isinstance(job, dict)
        return json_out(200, public_job(job))

    return json_out(404, {"error": "not found", "code": "not_found"})


def dispatch(
    method: str,
    path: str,
    qs: dict[str, list[str]],
    authed: bool,
    body: bytes,
    headers: Any = None,
) -> tuple[int, dict[str, Any] | bytes, str]:
    status, payload, ctype = dispatch_inner(method, path, qs, authed, body, headers)
    herdr_announce(method, path, status, payload)
    return status, payload, ctype


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"graphwing {self.address_string()} {fmt % args}", file=__import__("sys").stderr)

    def _authed(self) -> bool:
        got = (self.headers.get("X-Graphwing-Key") or "").encode()
        try:
            expected = load_key()
        except Exception:
            return False
        return hmac.compare_digest(got, expected)

    def _send(self, status: int, payload: dict[str, Any] | bytes, content_type: str) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        status, payload, ctype = dispatch(
            "GET", parsed.path, parse_qs(parsed.query), self._authed(), b"", self.headers
        )
        self._send(status, payload, ctype)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        if length > 1_000_000:
            self._send(413, {"error": "body too large", "code": "too_large"}, "application/json")
            return
        body = self.rfile.read(length) if length else b""
        status, payload, ctype = dispatch(
            "POST", parsed.path, parse_qs(parsed.query), self._authed(), body, self.headers
        )
        self._send(status, payload, ctype)


def main() -> None:
    try:
        load_key()
    except Exception as exc:
        raise SystemExit(f"API key missing: set GRAPHWING_KEY or write {KEY_PATH}") from exc
    load_repos()
    load_scripts()
    load_tests()
    if RR_PATH.is_file():
        load_rr()
    if STACKS_PATH.is_file():
        load_stacks()
    if not OPENAPI_PATH.is_file():
        raise SystemExit(f"missing {OPENAPI_PATH}")
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    recover_interrupted_agent_jobs()
    print(f"graphwing listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
