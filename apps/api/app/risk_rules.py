"""Deterministic risk normalization shared by reviews, gates and audits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


VALID_SEVERITIES = {"fatal", "major", "minor"}


def normalize_findings(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(findings):
        if not isinstance(raw, Mapping):
            raise ValueError("invalid_finding")
        item = dict(raw)
        severity = str(item.get("severity", "major")).lower()
        if severity not in VALID_SEVERITIES:
            raise ValueError("invalid_finding_severity")
        message = item.get("message") or item.get("text") or item.get("summary")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("finding_message_required")
        code = item.get("code") or item.get("rule") or f"manual-{index + 1}"
        evidence_refs = item.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or not all(isinstance(value, str) for value in evidence_refs):
            raise ValueError("invalid_finding_evidence_refs")
        item.update(
            {
                "code": str(code),
                "severity": severity,
                "message": message.strip(),
                "resolved": bool(item.get("resolved", False)),
                "evidence_refs": evidence_refs,
            }
        )
        normalized.append(item)
    return normalized


def blocking_findings(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_findings(findings)
    return [item for item in normalized if item["severity"] == "fatal" or (item["severity"] == "major" and not item["resolved"])]


def risk_summary(findings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = normalize_findings(findings)
    blocking = blocking_findings(normalized)
    return {
        "total": len(normalized),
        "fatal_count": sum(item["severity"] == "fatal" for item in normalized),
        "major_open_count": sum(item["severity"] == "major" and not item["resolved"] for item in normalized),
        "minor_open_count": sum(item["severity"] == "minor" and not item["resolved"] for item in normalized),
        "blocking_count": len(blocking),
        "status": "BLOCKED" if any(item["severity"] == "fatal" for item in blocking) else ("FAILED" if blocking else "CLEAR"),
    }


def gate_rules(target_type: str) -> list[str]:
    return [f"review:{target_type}", "risk:fatal_blocks", "risk:unresolved_major_blocks"]


def ensure_approvable(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_findings(findings)
    if blocking_findings(normalized):
        raise ValueError("review_blocked_by_findings")
    return normalized
