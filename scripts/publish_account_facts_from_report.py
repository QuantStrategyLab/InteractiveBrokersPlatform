"""Project an archived IBKR runtime report and optionally publish one record.

The projection function is pure. The workflow-only CLI reads one report from a
fixed GCS prefix and makes a single POST only when explicitly enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


HISTORY_SCHEMA = "ibkr_account_snapshot_history.v1"
SNAPSHOT_SCHEMA = "ibkr_account_snapshot.v1"
RUNTIME_REPORT_SCHEMA = "runtime_report.v1"
SOURCE_BINDING_KIND = "deployment_runtime_account"
IBKR_ACCOUNT_FACTS_SYNC_TOKEN_ENV = "IBKR_ACCOUNT_FACTS_SYNC_TOKEN"
IBKR_ACCOUNT_FACTS_SYNC_URL = "https://qsl-strategy-switch-console.pigbibi.workers.dev/api/account-facts/sync"
IBKR_ACCOUNT_FACTS_INGRESS_DIAGNOSTIC_TARGET = "ingress-diagnostic"
_INGRESS_DIAGNOSTIC_BODY = b"{}"
_INGRESS_DIAGNOSTIC_ERROR = "invalid_account_facts_history"
_REPORT_RUN_ID = re.compile(r"^\d{8}T\d{6}Z\.json$")
_ACCOUNT_ID = re.compile(r"^(?:U|DU)\d+$")
_ALLOWED_CASH_TAGS = frozenset(
    {"$LEDGER-CashBalance", "$LEDGER-TotalCashBalance", "CashBalance", "TotalCashBalance", "SettledCash"}
)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL_TEXT = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_TARGET_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_QRS_ACCOUNT_FACTS_ERROR_CODES = frozenset(
    {
        "account_facts_account_unattributed",
        "account_facts_bindings_missing",
        "account_facts_caller_identity_forbidden",
        "account_facts_disabled",
        "account_facts_identity_mismatch",
        "account_facts_store_unavailable",
        "account_facts_sync_token_ambiguous",
        "account_facts_sync_token_invalid",
        "account_facts_sync_token_not_configured",
        "account_facts_sync_token_platform_mismatch",
        "account_facts_target_mismatch",
        "account_facts_binding_mismatch",
        "account_facts_observation_window",
        "account_facts_future_observation",
        "invalid_account_facts_account",
        "invalid_account_facts_balances",
        "invalid_account_facts_bindings",
        "invalid_account_facts_cash",
        "invalid_account_facts_date",
        "invalid_account_facts_history",
        "invalid_account_facts_money_magnitude",
        "invalid_account_facts_money_scale",
        "invalid_account_facts_scope",
        "invalid_account_facts_source_binding",
        "invalid_account_facts_target",
        "invalid_account_facts_time",
        "account_facts_stored_invalid",
        "duplicate_account_facts_binding",
        "invalid_account_facts_stored",
        "unsupported_account_facts_action",
    }
)
_HTTP_ERROR_BODY_LIMIT = 16 * 1024


class _ProjectionError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _selector(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if any(not isinstance(item, str) or not item or item != item.strip() for item in value):
        return ()
    return tuple(value)


def _exact_text(value: object) -> str:
    return value if isinstance(value, str) and value and value == value.strip() else ""


def _observed_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _ProjectionError("observation_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise _ProjectionError("observation_invalid") from None


def _decimal_text(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
        raise _ProjectionError("account_facts_invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise _ProjectionError("account_facts_invalid") from None
    if not amount.is_finite():
        raise _ProjectionError("account_facts_invalid")
    exponent = amount.as_tuple().exponent
    scale = max(-exponent, 0)
    integer_digits = max(len(amount.as_tuple().digits) + exponent, 0)
    if scale > 8 or integer_digits + scale > 15:
        raise _ProjectionError("account_facts_invalid")
    return value


def _source_report_uri(uri: str, expected_prefix: str) -> str:
    try:
        parsed_uri = urlsplit(uri)
        parsed_prefix = urlsplit(expected_prefix)
        if (
            parsed_uri.scheme != "gs"
            or not parsed_uri.netloc
            or parsed_uri.query
            or parsed_uri.fragment
            or parsed_prefix.scheme != "gs"
            or not parsed_prefix.netloc
            or parsed_prefix.query
            or parsed_prefix.fragment
            or parsed_uri.netloc != parsed_prefix.netloc
        ):
            raise ValueError
        prefix_path = parsed_prefix.path.rstrip("/")
        if not prefix_path or not parsed_uri.path.startswith(prefix_path + "/"):
            raise ValueError
        return uri
    except ValueError:
        raise _ProjectionError("report_provenance_invalid") from None


def _bound_source_id(
    *,
    project_id: str,
    service_name: str,
    runtime_revision: str,
    account_scope: str,
    account_selector: tuple[str, ...],
    deployment_selector: str,
) -> str:
    canonical_binding = {
        "account_scope": account_scope,
        "account_selector": list(account_selector),
        "deployment_selector": deployment_selector,
        "project_id": project_id,
        "runtime_revision": runtime_revision,
        "service_name": service_name,
    }
    canonical = json.dumps(
        canonical_binding,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _project_cash(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise _ProjectionError("account_facts_invalid")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise _ProjectionError("account_facts_invalid")
        currency = item.get("currency")
        cash_balance = item.get("cash_balance")
        source_tag = item.get("source_tag")
        if (
            not isinstance(currency, str)
            or _CURRENCY.fullmatch(currency) is None
            or currency in seen
            or not isinstance(source_tag, str)
            or source_tag not in _ALLOWED_CASH_TAGS
        ):
            raise _ProjectionError("account_facts_invalid")
        seen.add(currency)
        rows.append(
            {
                "currency": currency,
                "cash_balance": _decimal_text(cash_balance),  # type: ignore[arg-type]
                "source_tag": source_tag,
            }
        )
    return rows


def project_ibkr_account_facts_history(
    report: Mapping[str, Any],
    *,
    target_id: str,
    expected_report_prefix: str,
    source_report_uri: str,
    expected_project_id: str,
    expected_service_name: str,
    expected_runtime_revision: str,
    expected_account_scope: str,
    expected_account_selector: Sequence[str] | str,
    expected_deployment_selector: str,
) -> dict[str, Any]:
    """Return one stable history record or a fixed, amount-free skip reason."""
    try:
        return _project_ibkr_account_facts_history(
            report,
            target_id=target_id,
            expected_report_prefix=expected_report_prefix,
            source_report_uri=source_report_uri,
            expected_project_id=expected_project_id,
            expected_service_name=expected_service_name,
            expected_runtime_revision=expected_runtime_revision,
            expected_account_scope=expected_account_scope,
            expected_account_selector=expected_account_selector,
            expected_deployment_selector=expected_deployment_selector,
        )
    except _ProjectionError as exc:
        return {"status": "skipped", "reason": exc.reason}


def publish_ibkr_account_facts_history(
    report: Mapping[str, Any],
    *,
    now: datetime,
    source_report_uri: str,
    sync_url: str,
    sync_token: str,
    **expected: Any,
) -> dict[str, Any]:
    """Validate one fresh stored report and POST it exactly once."""
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise _ProjectionError("observation_invalid")
        facts = report.get("summary", {}).get("account_facts") if isinstance(report.get("summary"), Mapping) else None
        if not isinstance(facts, Mapping):
            raise _ProjectionError("account_facts_invalid")
        observed = _observed_timestamp(facts.get("observed_at"))
        current = now.astimezone(timezone.utc)
        if observed < current - timedelta(minutes=15) or observed > current + timedelta(minutes=5):
            raise _ProjectionError("observation_stale")
        endpoint = urlsplit(_text(sync_url))
        if (
            _text(sync_url) != IBKR_ACCOUNT_FACTS_SYNC_URL
            or endpoint.scheme != "https"
            or endpoint.netloc != "qsl-strategy-switch-console.pigbibi.workers.dev"
            or endpoint.path != "/api/account-facts/sync"
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise _ProjectionError("publish_target_invalid")
        if not _text(sync_token):
            raise _ProjectionError("publish_auth_unavailable")
        projected = project_ibkr_account_facts_history(
            report,
            source_report_uri=source_report_uri,
            **expected,
        )
        if projected.get("status") == "skipped":
            return projected
        body = json.dumps(projected, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        request = Request(
            _text(sync_url),
            data=body,
            headers={
                "Authorization": f"Bearer {_text(sync_token)}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    return _publish_failed(
                        stage="http_response",
                        category="http_status",
                        http_status=_numeric_http_status(response.status),
                        qrs_error_code="unknown",
                    )
        except HTTPError as exc:
            return _publish_failed(
                stage="http_response",
                category="http_error",
                http_status=_numeric_http_status(exc.code),
                qrs_error_code=_safe_qrs_error_code(exc),
            )
        except TimeoutError:
            return _publish_failed(stage="request", category="timeout")
        except URLError:
            return _publish_failed(stage="request", category="url_error")
        except OSError:
            return _publish_failed(stage="request", category="transport_error")
        except Exception:
            return _publish_failed(stage="unknown", category="unknown")
        return {"status": "published"}
    except _ProjectionError as exc:
        return {"status": "skipped", "reason": exc.reason}


def _numeric_http_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _safe_qrs_error_code(error: HTTPError) -> str:
    try:
        body = error.read(_HTTP_ERROR_BODY_LIMIT + 1)
    except Exception:
        return "unknown"
    return _safe_qrs_error_code_from_body(body)


def _safe_qrs_error_code_from_body(body: object) -> str:
    if not isinstance(body, bytes) or len(body) > _HTTP_ERROR_BODY_LIMIT:
        return "unknown"
    try:
        payload = json.loads(body)
    except Exception:
        return "unknown"
    code = payload.get("error") if isinstance(payload, Mapping) else None
    return code if isinstance(code, str) and code in _QRS_ACCOUNT_FACTS_ERROR_CODES else "unknown"


def _publish_failed(
    *,
    stage: str,
    category: str,
    http_status: int | None = None,
    qrs_error_code: str = "unknown",
) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": "publish_failed",
        "diagnostics": {
            "stage": stage,
            "category": category,
            "http_status": http_status,
            "qrs_error_code": qrs_error_code,
            "outcome": "unknown",
        },
    }


def _ingress_diagnostic_result(
    *,
    http_status: int | None,
    qrs_error_code: str,
    category: str = "http_error",
    stage: str = "http_response",
) -> dict[str, Any]:
    verified = http_status == 400 and qrs_error_code == _INGRESS_DIAGNOSTIC_ERROR
    return {
        "status": "verified" if verified else "skipped",
        "reason": (
            "ingress_authentication_and_schema_rejection_verified"
            if verified
            else "ingress_diagnostic_unverified"
        ),
        "diagnostics": {
            "stage": stage,
            "category": category,
            "http_status": http_status,
            "qrs_error_code": qrs_error_code,
            "outcome": "rejected_before_storage" if verified else "unknown",
        },
    }


def diagnose_account_facts_ingress(*, sync_token: str) -> dict[str, Any]:
    """Send one fixed empty schema probe to verify protected QRS ingress only."""
    if not _text(sync_token):
        return {"status": "skipped", "reason": "publish_auth_unavailable"}
    request = Request(
        IBKR_ACCOUNT_FACTS_SYNC_URL,
        data=_INGRESS_DIAGNOSTIC_BODY,
        headers={
            "Authorization": f"Bearer {_text(sync_token)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        opener = build_opener(_NoRedirect())
        with opener.open(request, timeout=15) as response:
            status = _numeric_http_status(getattr(response, "status", None))
            if status != 400:
                return _ingress_diagnostic_result(
                    http_status=status,
                    qrs_error_code="unknown",
                    category="http_status",
                )
            try:
                body = response.read(_HTTP_ERROR_BODY_LIMIT + 1)
            except Exception:
                body = None
            return _ingress_diagnostic_result(
                http_status=status,
                qrs_error_code=_safe_qrs_error_code_from_body(body),
                category="http_status",
            )
    except HTTPError as exc:
        status = _numeric_http_status(exc.code)
        code = _safe_qrs_error_code(exc)
        return _ingress_diagnostic_result(http_status=status, qrs_error_code=code)
    except TimeoutError:
        return _ingress_diagnostic_result(
            http_status=None,
            qrs_error_code="unknown",
            category="timeout",
            stage="request",
        )
    except URLError:
        return _ingress_diagnostic_result(
            http_status=None,
            qrs_error_code="unknown",
            category="url_error",
            stage="request",
        )
    except OSError:
        return _ingress_diagnostic_result(
            http_status=None,
            qrs_error_code="unknown",
            category="transport_error",
            stage="request",
        )
    except Exception:
        return _ingress_diagnostic_result(
            http_status=None,
            qrs_error_code="unknown",
            category="unknown",
            stage="unknown",
        )


def _format_cli_result(result: Mapping[str, Any]) -> str:
    status = result.get("status") if result.get("status") in {"published", "verified", "skipped"} else "skipped"
    reason = result.get("reason") if isinstance(result.get("reason"), str) else "unknown"
    diagnostic = result.get("diagnostics")
    if not isinstance(diagnostic, Mapping):
        return f"{status}:{reason}"
    stage = (
        diagnostic.get("stage")
        if diagnostic.get("stage") in {"http_response", "request", "unknown"}
        else "unknown"
    )
    category = (
        diagnostic.get("category")
        if diagnostic.get("category") in {
            "http_status", "http_error", "timeout", "url_error", "transport_error", "unknown"
        }
        else "unknown"
    )
    http_status = _numeric_http_status(diagnostic.get("http_status"))
    qrs_error_code = diagnostic.get("qrs_error_code")
    if not isinstance(qrs_error_code, str) or qrs_error_code not in _QRS_ACCOUNT_FACTS_ERROR_CODES:
        qrs_error_code = "unknown"
    outcome = diagnostic.get("outcome")
    if outcome not in {"rejected_before_storage", "unknown"}:
        outcome = "unknown"
    return (
        f"{status}:{reason}:stage={stage}:category={category}"
        f":http_status={http_status if http_status is not None else 'none'}"
        f":qrs_error_code={qrs_error_code}:outcome={outcome}"
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, *_args: object, **_kwargs: object) -> None:
        return None


def _latest_report_uri(*, prefix: str, project_id: str, now: datetime) -> str:
    current = now.astimezone(timezone.utc)
    months = (current.strftime("%Y-%m"), (current.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    candidates: list[tuple[datetime, str]] = []
    for month in months:
        month_prefix = f"{prefix.rstrip('/')}/{month}/"
        result = subprocess.run(
            ("gcloud", "storage", "ls", month_prefix, "--project", project_id),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Empty month listings are ordinary; other listing failures fail closed.
            if "matched no objects" not in result.stderr.lower() and "not found" not in result.stderr.lower():
                raise _ProjectionError("report_listing_failed")
            continue
        for uri in result.stdout.splitlines():
            normalized_uri = uri.strip()
            filename = PurePosixPath(urlsplit(normalized_uri).path).name
            if not normalized_uri.startswith(month_prefix) or not _REPORT_RUN_ID.fullmatch(filename):
                continue
            try:
                timestamp = datetime.strptime(filename[:-5], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            candidates.append((timestamp, normalized_uri))
    if not candidates:
        raise _ProjectionError("report_unavailable")
    return max(candidates, key=lambda row: row[0])[1]


def _load_gcs_report(uri: str, *, project_id: str) -> dict[str, Any]:
    result = subprocess.run(
        ("gcloud", "storage", "cat", uri, "--project", project_id),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise _ProjectionError("report_read_failed")
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise _ProjectionError("runtime_report_invalid") from None
    if not isinstance(value, dict):
        raise _ProjectionError("runtime_report_invalid")
    return value


def main() -> int:
    """Workflow-only, explicitly enabled single-report publisher."""
    try:
        target = _text(os.environ.get("IBKR_ACCOUNT_FACTS_TARGET"))
        if target == IBKR_ACCOUNT_FACTS_INGRESS_DIAGNOSTIC_TARGET:
            result = diagnose_account_facts_ingress(
                sync_token=os.environ.get(IBKR_ACCOUNT_FACTS_SYNC_TOKEN_ENV, "")
            )
            print(_format_cli_result(result))
            return 0 if result.get("status") == "verified" else 1
        if target != "live-u16608560":
            print("skipped:target_disabled")
            return 0
        prefix = _text(os.environ.get("IBKR_ACCOUNT_FACTS_REPORT_PREFIX"))
        expected_prefix = (
            "gs://qsl-runtime-logs-shared/execution-reports/"
            "interactive_brokers/tqqq_growth_income/live-u16608560"
        )
        if prefix != expected_prefix:
            print("skipped:report_prefix_mismatch")
            return 1
        expected = {
            "target_id": "ibkr-u16608560",
            "expected_report_prefix": expected_prefix,
            "expected_project_id": "interactivebrokersquant",
            "expected_service_name": "interactive-brokers-quant-live-u16608560-service",
            "expected_runtime_revision": _text(os.environ.get("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION")),
            "expected_account_scope": "live-u16608560",
            "expected_account_selector": ["U16608560"],
            "expected_deployment_selector": "live-u16608560",
        }
        if not expected["expected_runtime_revision"]:
            print("skipped:expected_revision_unavailable")
            return 1
        now = datetime.now(timezone.utc)
        uri = _latest_report_uri(prefix=expected_prefix, project_id="interactivebrokersquant", now=now)
        report = _load_gcs_report(uri, project_id="interactivebrokersquant")
        result = publish_ibkr_account_facts_history(
            report,
            now=now,
            source_report_uri=uri,
            sync_url=os.environ.get("IBKR_ACCOUNT_FACTS_SYNC_URL", ""),
            sync_token=os.environ.get(IBKR_ACCOUNT_FACTS_SYNC_TOKEN_ENV, ""),
            **expected,
        )
        print(_format_cli_result(result))
        return 0 if result["status"] == "published" else 1
    except _ProjectionError as exc:
        print(f"skipped:{exc.reason}")
        return 1
    except Exception:
        print("skipped:publisher_error")
        return 1


def _project_ibkr_account_facts_history(
    report: Mapping[str, Any],
    **expected: Any,
) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        raise _ProjectionError("runtime_report_invalid")
    target_id = _text(expected["target_id"])
    if not target_id or _TARGET_ID.fullmatch(target_id) is None:
        raise _ProjectionError("expected_target_invalid")
    source_report_uri = _source_report_uri(
        _text(expected["source_report_uri"]), _text(expected["expected_report_prefix"])
    )
    expected_fields = {
        "project_id": _text(expected["expected_project_id"]),
        "service_name": _text(expected["expected_service_name"]),
        "runtime_revision": _text(expected["expected_runtime_revision"]),
        "account_scope": _text(expected["expected_account_scope"]),
        "deployment_selector": _text(expected["expected_deployment_selector"]),
    }
    if any(not value for value in expected_fields.values()):
        raise _ProjectionError("expected_target_invalid")
    expected_selector = _selector(expected["expected_account_selector"])
    if len(expected_selector) != 1 or expected_selector[0].lower() == "default":
        raise _ProjectionError("expected_target_invalid")

    target = report.get("runtime_target")
    diagnostics = report.get("diagnostics")
    receipt = report.get("runtime_release_receipt")
    if (
        report.get("schema_version") != RUNTIME_REPORT_SCHEMA
        or report.get("platform") != "interactive_brokers"
        or report.get("deploy_target") != "cloud_run"
        or not isinstance(target, Mapping)
        or not isinstance(diagnostics, Mapping)
        or (receipt is not None and not isinstance(receipt, Mapping))
    ):
        raise _ProjectionError("runtime_report_invalid")
    actual_fields = {
        "project_id": _exact_text(report.get("project_id")),
        "service_name": _exact_text(report.get("service_name")),
        "runtime_revision": _exact_text(diagnostics.get("runtime_revision")),
        "account_scope": _exact_text(report.get("account_scope")),
        "deployment_selector": _exact_text(target.get("deployment_selector")),
    }
    actual_selector = _selector(target.get("account_selector"))
    if actual_fields != expected_fields or actual_selector != expected_selector:
        raise _ProjectionError("runtime_target_mismatch")
    receipt_revision = _text(receipt.get("runtime_revision")) if isinstance(receipt, Mapping) else ""
    if receipt_revision and receipt_revision != actual_fields["runtime_revision"]:
        raise _ProjectionError("runtime_target_mismatch")

    summary = report.get("summary")
    facts = summary.get("account_facts") if isinstance(summary, Mapping) else None
    if not isinstance(facts, Mapping) or facts.get("schema_version") != SNAPSHOT_SCHEMA:
        raise _ProjectionError("account_facts_invalid")
    account_ids = _selector(facts.get("account_ids"))
    if (
        len(account_ids) != 1
        or account_ids != expected_selector
        or _ACCOUNT_ID.fullmatch(account_ids[0]) is None
    ):
        raise _ProjectionError("account_identity_mismatch")
    if facts.get("currency") != "USD":
        raise _ProjectionError("account_facts_invalid")

    started = _observed_timestamp(report.get("started_at"))
    finished = _observed_timestamp(report.get("finished_at"))
    if finished < started:
        raise _ProjectionError("observation_invalid")
    observed = _observed_timestamp(facts.get("observed_at"))
    if observed < started or observed > finished:
        raise _ProjectionError("observation_invalid")

    net_assets = _decimal_text(facts.get("net_assets"), nullable=True)
    cash = _project_cash(facts.get("cash"))
    binding_id = _bound_source_id(
        project_id=actual_fields["project_id"],
        service_name=actual_fields["service_name"],
        runtime_revision=actual_fields["runtime_revision"],
        account_scope=actual_fields["account_scope"],
        account_selector=actual_selector,
        deployment_selector=actual_fields["deployment_selector"],
    )
    return {
        "schema_version": HISTORY_SCHEMA,
        "snapshot_schema_version": SNAPSHOT_SCHEMA,
        "account_scope": actual_fields["account_scope"],
        "target_id": target_id,
        "source_binding": {
            "kind": SOURCE_BINDING_KIND,
            "status": "bound",
            "id": binding_id,
        },
        "observed_started_at": observed.isoformat(),
        "observed_finished_at": observed.isoformat(),
        "snapshot_atomic": False,
        "observation_date": observed.date().isoformat(),
        "broker_reported_balances": [{"currency": "USD", "net_assets": net_assets}],
        "cash": cash,
        "account_ids": [account_ids[0]],
    }


__all__ = [
    "diagnose_account_facts_ingress",
    "project_ibkr_account_facts_history",
    "publish_ibkr_account_facts_history",
]


if __name__ == "__main__":
    raise SystemExit(main())
