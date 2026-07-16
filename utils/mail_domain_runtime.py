"""In-process main-domain scheduler used by the Gold Miner panel."""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from utils import config as cfg


_LOCK = threading.RLock()
_STATE: dict[str, dict[str, Any]] = {}
_TRACKING = False
_PINPOINT_DOMAIN = ""
_PINPOINT_REMAINING = 0
_FAILURE_TYPES = {"discarded_email", "cloudflare_temp_email_network", "capacity_exceeded"}
_SUPPORTED_MODES = {"cloudflare_temp_email", "freemail", "cloudmail", "openai_cpa"}


def _config() -> dict[str, Any]:
    return getattr(cfg, "_c", {}) if isinstance(getattr(cfg, "_c", {}), dict) else {}


def _domains(raw: Any = None) -> list[str]:
    seen, result = set(), []
    for item in str(raw if raw is not None else _config().get("mail_domains", "")).split(","):
        domain = item.strip().lower().strip(".")
        if domain and domain not in seen:
            seen.add(domain)
            result.append(domain)
    return result


def _enabled() -> bool:
    return (
        str(getattr(cfg, "EMAIL_API_MODE", "")).strip() in _SUPPORTED_MODES
        and bool(_config().get("enable_mail_domain_runtime_control", False))
    )


def _failure_types() -> set[str]:
    value = _config().get("mail_domain_failure_types", ["discarded_email"])
    if not isinstance(value, list):
        value = ["discarded_email"]
    selected = {str(item).strip().lower() for item in value}
    return selected & _FAILURE_TYPES or {"discarded_email"}


def _threshold() -> int:
    try:
        return max(0, int(_config().get("mail_domain_fail_threshold", 3)))
    except (TypeError, ValueError):
        return 3


def _cooldown_seconds() -> int:
    try:
        return max(0, int(_config().get("mail_domain_fail_cooldown_sec", 600)))
    except (TypeError, ValueError):
        return 600


def _disabled() -> set[str]:
    value = _config().get("disabled_mail_domains", [])
    if not isinstance(value, list):
        return set()
    return {str(item).strip().lower().strip(".") for item in value if str(item).strip()}


def _normalize(domain: str) -> str:
    domain = str(domain or "").lower().strip().strip(".")
    if "@" in domain:
        domain = domain.rsplit("@", 1)[-1]
    for root in _domains():
        if domain == root or domain.endswith("." + root):
            return root
    return ""


def _new_state() -> dict[str, Any]:
    return {
        "failures": 0,
        "successes": 0,
        "picks": 0,
        "failure_counts": {},
        "last_failure_reason": "",
        "last_used_at": 0.0,
        "last_success_at": 0.0,
        "last_failure_at": 0.0,
        "cooldown_until": 0.0,
        "cooldown_reason": "",
    }


def _state(domain: str) -> dict[str, Any]:
    return _STATE.setdefault(domain, _new_state())


def _refresh_failures(item: dict[str, Any]) -> int:
    counts = item.get("failure_counts") or {}
    item["failures"] = sum(max(0, int(counts.get(kind, 0))) for kind in _failure_types())
    return item["failures"]


def _available(domains: list[str], now: float) -> list[str]:
    disabled = _disabled()
    return [
        domain for domain in domains
        if domain not in disabled and float(_state(domain).get("cooldown_until", 0)) <= now
    ]


def start() -> None:
    global _TRACKING
    with _LOCK:
        _TRACKING = _enabled()


def stop() -> None:
    global _TRACKING
    with _LOCK:
        _TRACKING = False


def sync_config() -> None:
    global _PINPOINT_DOMAIN, _PINPOINT_REMAINING, _TRACKING
    with _LOCK:
        if not _enabled():
            _TRACKING = False
        configured = set(_domains())
        for domain in list(_STATE):
            if domain not in configured:
                _STATE.pop(domain, None)
        if _PINPOINT_DOMAIN not in configured:
            _PINPOINT_DOMAIN = ""
            _PINPOINT_REMAINING = 0


def begin_batch(batch_size: int) -> None:
    """Reserve one root domain for a single concurrent registration batch."""
    global _PINPOINT_DOMAIN, _PINPOINT_REMAINING
    if not _enabled() or not bool(_config().get("mail_domain_pinpoint_burst_mode", False)):
        return
    now = time.time()
    with _LOCK:
        candidates = _available(_domains(), now)
        if not candidates:
            _PINPOINT_DOMAIN = ""
            _PINPOINT_REMAINING = 0
            return
        if _PINPOINT_DOMAIN not in candidates:
            _PINPOINT_DOMAIN = candidates[0]
        _PINPOINT_REMAINING = max(0, int(batch_size or 0))


def pick_main_domain(domains: list[str]) -> str | None:
    """Choose one configured root domain, excluding disabled and cooling entries."""
    normalized = [domain for domain in (_normalize(item) for item in domains) if domain]
    if not normalized:
        return None
    if not _enabled():
        return random.choice(normalized)

    global _PINPOINT_DOMAIN, _PINPOINT_REMAINING
    now = time.time()
    with _LOCK:
        candidates = _available(normalized, now)
        if not candidates:
            return None
        pinpoint = bool(_config().get("mail_domain_pinpoint_burst_mode", False))
        if pinpoint and _PINPOINT_REMAINING > 0 and _PINPOINT_DOMAIN in candidates:
            selected = _PINPOINT_DOMAIN
            _PINPOINT_REMAINING -= 1
        elif pinpoint:
            selected = candidates[0]
            _PINPOINT_DOMAIN = selected
        elif bool(_config().get("mail_domain_prefer_low_failure_mode", False)):
            selected = min(
                candidates,
                key=lambda domain: (_refresh_failures(_state(domain)), _state(domain)["picks"], _state(domain)["last_used_at"]),
            )
        else:
            selected = random.choice(candidates)
        item = _state(selected)
        item["picks"] += 1
        item["last_used_at"] = now
        return selected


def record_success(domain: str) -> dict[str, Any] | None:
    domain = _normalize(domain)
    if not domain or not _enabled() or not _TRACKING:
        return None
    with _LOCK:
        item = _state(domain)
        item["successes"] += 1
        item["last_success_at"] = time.time()
        return _row(domain, item, time.time())


def record_failure(domain: str, reason: str = "discarded_email") -> dict[str, Any] | None:
    domain = _normalize(domain)
    reason = str(reason or "").strip().lower()
    if not domain or reason not in _failure_types() or not _enabled() or not _TRACKING:
        return None

    global _PINPOINT_DOMAIN
    now = time.time()
    with _LOCK:
        item = _state(domain)
        item["last_failure_at"] = now
        item["last_failure_reason"] = reason
        counts = item["failure_counts"]
        counts[reason] = int(counts.get(reason, 0)) + 1
        failures = _refresh_failures(item)
        threshold = _threshold()
        if threshold and failures >= threshold:
            item["cooldown_until"] = now + _cooldown_seconds()
            item["cooldown_reason"] = reason
            item["failures"] = 0
            if _PINPOINT_DOMAIN == domain:
                _PINPOINT_DOMAIN = ""
        return _row(domain, item, now)


def _row(domain: str, item: dict[str, Any], now: float) -> dict[str, Any]:
    until = float(item.get("cooldown_until", 0) or 0)
    return {
        "domain": domain,
        "fail_count": int(item.get("failures", 0) or 0),
        "success_count": int(item.get("successes", 0) or 0),
        "pick_count": int(item.get("picks", 0) or 0),
        "failure_counts": dict(item.get("failure_counts") or {}),
        "last_failure_reason": str(item.get("last_failure_reason") or ""),
        "cooldown_until": until,
        "cooldown_remaining_sec": max(0, int(until - now)),
        "cooldown_reason": str(item.get("cooldown_reason") or ""),
        "is_available": until <= now,
        "is_disabled": domain in _disabled(),
    }


def stats() -> list[dict[str, Any]]:
    now = time.time()
    with _LOCK:
        rows = []
        for domain in _domains():
            item = _state(domain)
            if item.get("cooldown_until", 0) <= now:
                item["cooldown_until"] = 0.0
                item["cooldown_reason"] = ""
            _refresh_failures(item)
            rows.append(_row(domain, item, now))
        return rows


def clear_counters(domain: str) -> bool:
    domain = _normalize(domain)
    if not domain:
        return False
    with _LOCK:
        item = _state(domain)
        item["failures"] = 0
        item["failure_counts"] = {}
        item["last_failure_reason"] = ""
        return True


def clear_cooldown(domain: str) -> bool:
    domain = _normalize(domain)
    if not domain:
        return False
    with _LOCK:
        item = _state(domain)
        item["cooldown_until"] = 0.0
        item["cooldown_reason"] = ""
        return True


def clear_all_cooldowns() -> int:
    with _LOCK:
        count = sum(1 for item in _STATE.values() if item.get("cooldown_until", 0))
        for item in _STATE.values():
            item["cooldown_until"] = 0.0
            item["cooldown_reason"] = ""
        return count
