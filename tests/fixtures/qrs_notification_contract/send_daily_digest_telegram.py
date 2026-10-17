#!/usr/bin/env python3
"""Send QuantSentinel daily digest via unified bot secret-name path.

Token resolution (never logged):
  TELEGRAM_TOKEN → TG_TOKEN → else gcloud Secret Manager via
  TELEGRAM_TOKEN_SECRET_NAME (default quant-sentinel-telegram-bot-token).

Chat resolution (never logged):
  QSL_GLOBAL_TELEGRAM_CHAT_ID → GLOBAL_TELEGRAM_CHAT_ID →
  STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS (first id).

Dry-run / --route-check record match metadata only; never call Telegram
and never print token or chat id values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import credential_maintenance_notify as maintenance
import daily_digest_aggregator as aggregator
import daily_digest_delivery_axes as delivery_axes  # noqa: E402
import daily_digest_notify as digest  # noqa: E402
import daily_digest_resend_guard as resend_guard

DEFAULT_SECRET_NAME = "quant-sentinel-telegram-bot-token"

# Resolution order contracts (names only; mirrors platform-config notifications).
TOKEN_ENV_PRIORITY = ("TELEGRAM_TOKEN", "TG_TOKEN")
CHAT_ENV_PRIORITY = (
    "QSL_GLOBAL_TELEGRAM_CHAT_ID",
    "GLOBAL_TELEGRAM_CHAT_ID",
    "STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS",
)
# Present in env_aliases but NOT consulted by this sender — shadow/ambiguity signal.
TOKEN_ALIAS_NOT_IN_SENDER_CHAIN = ("STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN",)


def _split_chat_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).replace(";", ",").split(",") if part.strip()]


def _env_nonempty(key: str) -> bool:
    return bool((os.environ.get(key) or "").strip())


def resolve_telegram_chat_id() -> str:
    for key in CHAT_ENV_PRIORITY:
        chats = _split_chat_ids(os.environ.get(key))
        if chats:
            return chats[0]
    return ""


def _secret_name_contract() -> str:
    return (
        os.environ.get("TELEGRAM_TOKEN_SECRET_NAME") or DEFAULT_SECRET_NAME
    ).strip() or DEFAULT_SECRET_NAME


def _load_token_from_secret_manager() -> str:
    secret_name = _secret_name_contract()
    if not secret_name:
        return ""
    command = [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret",
        secret_name,
    ]
    project = (
        os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    if project:
        command.extend(["--project", project])
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        print(
            "Unable to read Telegram token from Secret Manager (gcloud missing).",
            file=sys.stderr,
        )
        return ""
    if result.returncode != 0:
        print(
            "Unable to read Telegram token from Secret Manager "
            f"(secret_name={secret_name!r}; stderr omitted).",
            file=sys.stderr,
        )
        return ""
    return (result.stdout or "").strip()


def resolve_telegram_token() -> str:
    for key in TOKEN_ENV_PRIORITY:
        direct = (os.environ.get(key) or "").strip()
        if direct:
            return direct
    return _load_token_from_secret_manager()


def _optional_chat_fingerprint(chat_id: str, *, enabled: bool) -> str | None:
    """Optional last4 / short hash; default off so receipts stay free of chat material."""
    if not enabled or not chat_id:
        return None
    digest_hex = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:12]
    last4 = chat_id[-4:] if len(chat_id) >= 4 else chat_id
    return f"sha256_12={digest_hex};last4={last4}"


def diagnose_telegram_route(
    *,
    probe_secret_manager: bool = False,
    include_chat_fingerprint: bool = False,
) -> dict[str, Any]:
    """Read-only route match metadata. Never includes token or chat plaintext.

    probe_secret_manager: when True and no env token wins, attempt gcloud access
    solely to learn whether GCP SM yields a token (value discarded). Default False
    so dry-run / unit tests never call gcloud.
    """
    secret_name = _secret_name_contract()
    secret_name_overridden = bool(
        (os.environ.get("TELEGRAM_TOKEN_SECRET_NAME") or "").strip()
    )
    if secret_name_overridden and secret_name != DEFAULT_SECRET_NAME:
        secret_name_contract = secret_name
        secret_name_matches_default = False
    else:
        secret_name_contract = DEFAULT_SECRET_NAME
        secret_name_matches_default = secret_name == DEFAULT_SECRET_NAME

    present_token_envs = [key for key in TOKEN_ENV_PRIORITY if _env_nonempty(key)]
    present_chat_envs = [key for key in CHAT_ENV_PRIORITY if _env_nonempty(key)]
    unused_token_aliases = [
        key for key in TOKEN_ALIAS_NOT_IN_SENDER_CHAIN if _env_nonempty(key)
    ]

    warnings: list[str] = []
    token_source_kind = "missing"
    token_alias_used = False
    token_resolved = False

    if present_token_envs:
        winner = present_token_envs[0]
        token_source_kind = f"env:{winner}"
        token_alias_used = winner != "TELEGRAM_TOKEN"
        token_resolved = True
        for shadowed in present_token_envs[1:]:
            warnings.append(
                f"token_env_shadowed:{shadowed}_present_but_{winner}_wins"
            )
        # Environment injection shadows GCP Secret Manager path.
        warnings.append(
            "token_gcp_secret_manager_shadowed_by_environment:"
            f"{winner}_prevents_gcloud_access_to_{secret_name}"
        )
    elif probe_secret_manager:
        sm_token = _load_token_from_secret_manager()
        if sm_token:
            token_source_kind = "gcp_secret_manager"
            token_resolved = True
            # Discard value immediately; do not retain.
            sm_token = ""
        else:
            token_source_kind = "missing"
            token_resolved = False
    else:
        # Env absent: contract says GCP SM is next. Without probing we can only
        # report the intended fallback kind when a secret name is configured.
        if secret_name:
            token_source_kind = "gcp_secret_manager_unprobed"
            token_resolved = False  # readiness unknown without probe
        else:
            token_source_kind = "missing"
            token_resolved = False

    if unused_token_aliases:
        for key in unused_token_aliases:
            warnings.append(
                f"token_alias_present_but_not_in_sender_chain:{key}"
            )

    if secret_name_overridden and not secret_name_matches_default:
        warnings.append(
            "telegram_token_secret_name_differs_from_contract:"
            f"resolved={secret_name};contract={DEFAULT_SECRET_NAME}"
        )

    chat_source_kind = "missing"
    chat_alias_used = False
    chat_resolved = False
    chat_id_for_optional_fp = ""

    if present_chat_envs:
        winner = present_chat_envs[0]
        chats = _split_chat_ids(os.environ.get(winner))
        chat_source_kind = f"env:{winner}"
        chat_alias_used = winner != "QSL_GLOBAL_TELEGRAM_CHAT_ID"
        chat_resolved = bool(chats)
        chat_id_for_optional_fp = chats[0] if chats else ""
        for shadowed in present_chat_envs[1:]:
            warnings.append(
                f"chat_env_shadowed:{shadowed}_present_but_{winner}_wins"
            )
        if winner == "STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS" and len(chats) > 1:
            warnings.append(
                "chat_multi_id_list_first_only:"
                f"STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS_count={len(chats)}"
            )
    else:
        chat_source_kind = "missing"
        chat_resolved = False

    missing: list[str] = []
    # Wiring-name readiness (dry-run / route-check): env token OR secret-name
    # contract counts as present. Unprobed GCP is reflected in
    # token_source_kind, not as a hard missing env name.
    if not token_resolved and token_source_kind == "missing":
        missing.append(
            "TELEGRAM_TOKEN|TG_TOKEN|TELEGRAM_TOKEN_SECRET_NAME+GCP"
        )
    if not chat_resolved:
        missing.append(
            "QSL_GLOBAL_TELEGRAM_CHAT_ID|GLOBAL_TELEGRAM_CHAT_ID|"
            "STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS"
        )

    route: dict[str, Any] = {
        "schema_version": "qsl.telegram_route_diagnosis.v1",
        "channel": "quant_sentinel",
        "secret_name_contract": secret_name_contract,
        "secret_name_matches_default_contract": secret_name_matches_default,
        "token_source_kind": token_source_kind,
        "token_alias_used": token_alias_used,
        "token_env_present": present_token_envs,
        "chat_source_kind": chat_source_kind,
        "chat_alias_used": chat_alias_used,
        "chat_env_present": present_chat_envs,
        "missing": missing,
        "warnings": warnings,
        "probe_secret_manager": bool(probe_secret_manager),
    }
    fingerprint = _optional_chat_fingerprint(
        chat_id_for_optional_fp, enabled=include_chat_fingerprint
    )
    if fingerprint is not None:
        route["chat_fingerprint"] = fingerprint
    return route


def missing_delivery_requirements() -> list[str]:
    missing: list[str] = []
    token = resolve_telegram_token()
    chat = resolve_telegram_chat_id()
    if not token:
        missing.append(
            "TELEGRAM_TOKEN (Environment secret) or TELEGRAM_TOKEN_SECRET_NAME "
            f"+ GCP access to {DEFAULT_SECRET_NAME}"
        )
    if not chat:
        missing.append(
            "QSL_GLOBAL_TELEGRAM_CHAT_ID or GLOBAL_TELEGRAM_CHAT_ID "
            "(Environment secret)"
        )
    return missing


def send_telegram_message(*, token: str, chat_id: str, text: str) -> bool:
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    # Token only in URL path for Telegram Bot API; never print it.
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        print(f"Telegram send failed HTTP {exc.code}: {detail}", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Telegram send failed: {type(exc).__name__}", file=sys.stderr)
        return False
    return bool(payload.get("ok"))


def _receipt_base(*, dry_run: bool, route: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "qsl.daily_digest_delivery_receipt.v1",
        "dry_run": dry_run,
        "delivered": False,
        "channel": "quant_sentinel",
        "secret_name_contract": route.get(
            "secret_name_contract", DEFAULT_SECRET_NAME
        ),
        "token_source_kind": route.get("token_source_kind"),
        "chat_source_kind": route.get("chat_source_kind"),
        "token_alias_used": route.get("token_alias_used"),
        "chat_alias_used": route.get("chat_alias_used"),
        "warnings": list(route.get("warnings") or []),
        "route_diagnosis": route,
        "maintenance": maintenance.empty_diagnostic(),
    }


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    # B11-b: refresh delivery_axes after dry_run/delivered/status are known.
    delivery_axes.attach_delivery_axes(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render and optionally send QuantSentinel daily digest."
    )
    parser.add_argument("--business-day", default=os.environ.get("DIGEST_BUSINESS_DAY"))
    parser.add_argument("--locale", default=None)
    parser.add_argument(
        "--layout",
        default=None,
        choices=["v1", "sections_v2"],
        help="Message layout (default env DIGEST_LAYOUT or v1). sections_v2 = NOTIFY-04 four-section report.",
    )
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--no-github", action="store_true")
    parser.add_argument("--platform-config", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Render only; do not call Telegram.",
    )
    parser.add_argument(
        "--route-check",
        action="store_true",
        default=False,
        help=(
            "Read-only Telegram route diagnosis only: no digest render, "
            "no Telegram send, no secret values printed."
        ),
    )
    parser.add_argument(
        "--probe-secret-manager",
        action="store_true",
        default=False,
        help=(
            "With --route-check / dry-run: attempt gcloud Secret Manager access "
            "to resolve token_source_kind when env tokens are absent. "
            "Token value is discarded and never printed."
        ),
    )
    parser.add_argument(
        "--include-chat-fingerprint",
        action="store_true",
        default=False,
        help="Optional chat sha256_12+last4 in route diagnosis (default off).",
    )
    parser.add_argument(
        "--write-receipt",
        type=Path,
        default=None,
        help="Write delivery/route receipt JSON (no secrets/chat ids by default).",
    )
    args = parser.parse_args(argv)

    include_fp = bool(args.include_chat_fingerprint) or str(
        os.environ.get("DIGEST_ROUTE_CHAT_FINGERPRINT") or ""
    ).lower() in {"1", "true", "yes"}
    probe_sm = bool(args.probe_secret_manager) or str(
        os.environ.get("DIGEST_ROUTE_PROBE_SECRET_MANAGER") or ""
    ).lower() in {"1", "true", "yes"}

    if args.route_check:
        route = diagnose_telegram_route(
            probe_secret_manager=probe_sm,
            include_chat_fingerprint=include_fp,
        )
        receipt = _receipt_base(dry_run=True, route=route)
        receipt["status"] = "route_check"
        delivery_axes.attach_delivery_axes(receipt)
        receipt["missing"] = list(route.get("missing") or [])
        receipt["business_day"] = None
        receipt["run_count"] = 0
        receipt["total_fills"] = None
        # Safe summary: kinds and warning codes only.
        print(
            json.dumps(
                {
                    "status": "route_check",
                    "secret_name_contract": route["secret_name_contract"],
                    "token_source_kind": route["token_source_kind"],
                    "chat_source_kind": route["chat_source_kind"],
                    "token_alias_used": route["token_alias_used"],
                    "chat_alias_used": route["chat_alias_used"],
                    "missing": route["missing"],
                    "warnings": route["warnings"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.write_receipt:
            _write_receipt(args.write_receipt, receipt)
        # Exit 0 even when missing — diagnosis succeeded; readiness is in receipt.
        return 0

    dry_run = args.dry_run or str(os.environ.get("DIGEST_DRY_RUN") or "").lower() in {"1", "true", "yes"}
    day = aggregator.resolve_business_day(explicit=args.business_day)
    guard = resend_guard.evaluate(day, os.environ, dry_run=dry_run)
    print(guard.log(day, os.environ.get("GITHUB_EVENT_NAME", "local")))
    if guard.decision in {"skip", "blocked"}:
        receipt = resend_guard.skip_receipt(day, aggregator.resolve_locale(args.locale), guard)
        if args.write_receipt:
            _write_receipt(args.write_receipt, receipt)
        if guard.decision == "blocked":
            print(f"::error::Daily digest not sent: resend guard {guard.reason}.", file=sys.stderr)
        return 0 if guard.decision == "skip" else 1
    from unified_daily_digest import enabled as unified_enabled
    from notification_mode import consolidation_enabled
    maintenance_client = maintenance.MaintenanceClient.from_environment(os.environ, dry_run=dry_run)
    reminder = maintenance_client.read_due() if maintenance_client else None
    business_failed = False
    try:
        if os.environ.get("DIGEST_CANDIDATES_INPUT_STATUS") == "failure":
            raise ValueError("candidate_input_materialization_failed")
        payload, text, collected = aggregator.build_digest_payload(
            business_day=day, locale=args.locale, candidates_path=args.candidates,
            include_github=not args.no_github, config_path=args.platform_config, layout=args.layout,
        )
    except Exception:
        if reminder is None: raise
        # Expiry may be why candidates are absent. Keep the business failure and
        # unknown counts while allowing only this independently sourced reminder.
        business_failed = True
        payload, text, collected = None, "", []
        print("Daily digest construction failed; maintenance fallback only.", file=sys.stderr)

    text = digest.label_delivery_text(
        text, str(payload.locale) if payload is not None else aggregator.resolve_locale(args.locale),
        test=guard.test_message, previous_failures=0 if unified_enabled() else guard.previous_failures,
    )

    # Dry-run never probes SM unless explicitly requested (keeps CI offline).
    route = diagnose_telegram_route(
        probe_secret_manager=probe_sm if dry_run else False,
        include_chat_fingerprint=include_fp,
    )
    base_receipt = aggregator.build_receipt(payload, collected, message_chars=len(text)) if payload is not None else {"total_fills": None, "run_count": None, "business_digest_failed": True}
    receipt = _receipt_base(dry_run=dry_run, route=route)
    receipt.update(base_receipt)
    receipt["resend_guard"] = guard.to_dict()
    receipt["test_message"] = guard.test_message
    if maintenance_client:
        receipt["maintenance"] = maintenance_client.diagnostic
    receipt.update(
        {
            "business_day": payload.business_day if payload is not None else None,
            "locale": digest.normalize_locale(str(payload.locale)) if payload is not None else digest.normalize_locale(args.locale or "zh"),
            "run_count": len(payload.runs) if payload is not None else None,
            "missing": [],
        }
    )
    if consolidation_enabled() and payload is not None and (payload.notification_summary or {}).get("run_count_unknown"):
        receipt["run_count"] = None

    receipt["report_date"] = resend_guard.report_date()
    receipt["recipient_scope"] = os.environ.get("DIGEST_RECIPIENT_SCOPE", "default")
    if unified_enabled():
        receipt["languages"] = [receipt["locale"]] if consolidation_enabled() else ["zh", "en"]
    if unified_enabled() and not text and reminder is None:
        receipt.update(status="skipped_empty", delivered=False)
        if args.write_receipt:
            _write_receipt(args.write_receipt, receipt)
        return 0

    if payload is not None:
        print(text)
        print("---")

    if dry_run:
        missing = list(route.get("missing") or [])
        receipt["missing"] = missing
        receipt["status"] = "dry_run"
        delivery_axes.attach_delivery_axes(receipt)
        print(
            "Daily digest dry-run: message rendered; "
            f"token_source_kind={route['token_source_kind']}; "
            f"chat_source_kind={route['chat_source_kind']}; "
            f"warnings={len(route.get('warnings') or [])}; "
            f"delivery extras missing={missing or 'none'}."
        )
        if args.write_receipt:
            _write_receipt(args.write_receipt, receipt)
        return 0

    missing = missing_delivery_requirements()
    if missing:
        # Re-diagnose with probe so receipt reflects actual SM outcome when sending.
        route = diagnose_telegram_route(
            probe_secret_manager=True,
            include_chat_fingerprint=include_fp,
        )
        receipt["route_diagnosis"] = route
        receipt["token_source_kind"] = route.get("token_source_kind")
        receipt["chat_source_kind"] = route.get("chat_source_kind")
        receipt["warnings"] = list(route.get("warnings") or [])
        receipt["missing"] = missing
        receipt["status"] = "blocked_missing_secrets"
        delivery_axes.attach_delivery_axes(receipt)
        print(
            "Daily digest not sent: missing Environment configuration:",
            file=sys.stderr,
        )
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        if args.write_receipt:
            _write_receipt(args.write_receipt, receipt)
        return 2

    token = resolve_telegram_token()
    chat_id = resolve_telegram_chat_id()
    # After resolve: env kinds if present, else probe SM so receipt shows
    # gcp_secret_manager (value discarded inside diagnose).
    env_token_present = any(_env_nonempty(k) for k in TOKEN_ENV_PRIORITY)
    route = diagnose_telegram_route(
        probe_secret_manager=not env_token_present,
        include_chat_fingerprint=include_fp,
    )
    receipt["route_diagnosis"] = route
    receipt["token_source_kind"] = route.get("token_source_kind")
    receipt["chat_source_kind"] = route.get("chat_source_kind")
    receipt["token_alias_used"] = route.get("token_alias_used")
    receipt["chat_alias_used"] = route.get("chat_alias_used")
    receipt["warnings"] = list(route.get("warnings") or [])
    receipt["secret_name_contract"] = route.get(
        "secret_name_contract", DEFAULT_SECRET_NAME
    )

    if unified_enabled() and reminder is not None:
        from unified_daily_digest import render
        if payload is not None:
            if consolidation_enabled():
                reminder_units = len(reminder.block(receipt["locale"]).encode("utf-16-le")) // 2
                text = render(payload, max_units=max(256, 3900 - reminder_units - 20))
            else:
                text = render(payload, max_units=max(256, 3900 - len(reminder.block("zh")) - len(reminder.block("en")) - 20))
    combined = (text + "\n\n" if text else "") + (reminder.block("zh") + "\n" + reminder.block("en") if unified_enabled() and not consolidation_enabled() else reminder.block(receipt["locale"])) if reminder is not None else text
    compatible = maintenance.can_combine_reminder(text=combined, chat_id=chat_id)
    reserved = bool(reminder is not None and maintenance_client and compatible and maintenance_client.reserve(reminder))
    if consolidation_enabled() and not reserved and not text and not business_failed:
        receipt.update(status="skipped_empty", delivered=False)
        if args.write_receipt:
            _write_receipt(args.write_receipt, receipt)
        return 0
    if consolidation_enabled() and args.write_receipt:
        receipt.update(status="sending", delivered=False)
        _write_receipt(args.write_receipt, receipt)
    if reserved:
        ok = maintenance.send_strict_telegram(token=token, chat_id=chat_id, text=combined)
        delivery = maintenance_client.ack(reminder, "delivered" if ok else "unknown")
        receipt["maintenance"].update(phase=reminder.phase, delivery=delivery)
    elif business_failed:
        receipt["status"] = "business_digest_failed"
        receipt["delivered"] = False
        delivery_axes.attach_delivery_axes(receipt)
        if args.write_receipt: _write_receipt(args.write_receipt, receipt)
        return 1
    else:
        transport = maintenance.send_strict_telegram if consolidation_enabled() else send_telegram_message
        ok = transport(token=token, chat_id=chat_id, text=text)
    receipt["delivered"] = ok
    receipt["status"] = "business_digest_failed" if business_failed else "delivered" if ok else "send_failed"
    delivery_axes.attach_delivery_axes(receipt)
    if args.write_receipt:
        _write_receipt(args.write_receipt, receipt)
    if not ok:
        print("Daily digest Telegram delivery failed.", file=sys.stderr)
        return 1
    if business_failed:
        return 1
    fills = receipt.get("total_fills")
    fills_label = "unknown" if fills is None else fills
    print(
        "Daily digest delivered via QuantSentinel "
        f"(runs={len(payload.runs)}, fills={fills_label})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
