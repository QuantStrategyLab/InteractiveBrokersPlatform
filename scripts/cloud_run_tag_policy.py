"""Versioned Option B operator policy; intended canonical owner: QuantPlatformKit.

Pure allocation is not traffic authority. Callers persist exclusive no-replay
state, re-read current routing, use provider CAS where available, and verify
owned tag removal plus restoration. Never infer ownership from a short tag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re

CONTRACT_VERSION = "cloud-run-tags/v1"
TAGGED_URL_BUDGET = 46
SERVICE = re.compile(r"[a-z](?:[a-z0-9-]{0,47}[a-z0-9])?")
TAG = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class Allocation:
    status: str
    tag: str | None = field(repr=False)
    operation_fingerprint: str = field(repr=False)


def headroom(service_name: str) -> int:
    if not isinstance(service_name, str) or not SERVICE.fullmatch(service_name):
        raise ValueError("INVALID_SERVICE_NAME")
    return TAGGED_URL_BUDGET - len(service_name)


def allocate_tag(service_name: str, preferred_tag: str, *, existing_tags,
                 operation_id: str) -> Allocation:
    room = headroom(service_name)
    if (not isinstance(preferred_tag, str) or not TAG.fullmatch(preferred_tag)
            or not isinstance(operation_id, str) or not operation_id
            or not isinstance(existing_tags, (list, tuple, set, frozenset))
            or any(not isinstance(tag, str) or not TAG.fullmatch(tag) for tag in existing_tags)):
        raise ValueError("INVALID_TAG_ALLOCATION_INPUT")
    fingerprint = hashlib.sha256((CONTRACT_VERSION + "\0" + service_name + "\0" +
                                  preferred_tag + "\0" + operation_id).encode()).hexdigest()
    if room <= 0:
        return Allocation("TAG_IMPOSSIBLE", None, fingerprint)
    # A leading letter is required even when the entire budget is one byte.
    shortened = chr(ord('a') + int(fingerprint[:2], 16) % 26) + fingerprint[2:]
    tag = preferred_tag if len(preferred_tag) <= room else shortened[:room]
    if tag in existing_tags:
        # No implicit same-owner reuse: persisted interrupted state needs
        # reconciliation, and an occupied tag alone never proves ownership.
        return Allocation("REFUSE", None, fingerprint)
    return Allocation("ALLOCATED", tag, fingerprint)


def lifecycle_for_impossible_tag(*, service_name: str, approved_service: str | None = None,
                                 warmup_traffic_approved: bool = False,
                                 no_traffic: bool = False) -> str:
    headroom(service_name)
    if (headroom(service_name) <= 0 and warmup_traffic_approved is True
            and no_traffic is False and approved_service == service_name):
        return "MICRO_SPLIT"
    return "STOP"


def preflight(service_name: str, preferred_tag: str | None = None) -> dict:
    """Identity passthrough: classify budget without authorizing any lifecycle."""
    room = headroom(service_name)
    if preferred_tag is None:
        return {"contract_version": CONTRACT_VERSION, "headroom": room,
                "tag_status": "TAG_IMPOSSIBLE" if room <= 0 else "TAG_AVAILABLE"}
    if not isinstance(preferred_tag, str) or not TAG.fullmatch(preferred_tag):
        raise ValueError("INVALID_TAG_ALLOCATION_INPUT")
    status = ("TAG_IMPOSSIBLE" if room <= 0 else
              "TAG_FITS" if len(preferred_tag) <= room else "TAG_SHORTEN_REQUIRED")
    return {"contract_version": CONTRACT_VERSION, "headroom": room,
            "tag_status": status}


def claim_operation(path: Path, operation_id: str) -> None:
    """Single-use entry point claim; keep after success/failure for reconciliation."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("operation restart requires reconciliation; no replay") from None
    with os.fdopen(fd, "w") as stream:
        json.dump({"contract_version": CONTRACT_VERSION,
                   "operation_fingerprint": hashlib.sha256(operation_id.encode()).hexdigest()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
