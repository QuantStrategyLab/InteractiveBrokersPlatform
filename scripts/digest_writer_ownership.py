"""Read the primary target's serving metadata before a legacy candidate upload."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping


def runtime_owns_slot(env: Mapping[str, str], *, runner=subprocess.run) -> bool:
    if env.get("STORE_TARGET") != "primary-live":
        return False
    project = env.get("GCP_PROJECT_ID", "").strip()
    service = env.get("IBKR_ACCOUNT_FACTS_SERVICE_NAME", "").strip()
    if not project or not service:
        raise ValueError("primary_service_binding_missing")

    def read(args):
        result = runner(["gcloud", "run", *args, "--project", project, "--format=json", "--quiet"],
                        capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("primary_writer_read_failed")
        return json.loads(result.stdout)

    services = read(["services", "list", "--platform=managed"])
    matches = [row for row in services if row.get("metadata", {}).get("name") == service]
    if len(matches) != 1:
        raise ValueError("primary_service_not_unique")
    current = matches[0]
    active = [row for row in current.get("status", {}).get("traffic", []) if row.get("percent", 0)]
    if len(active) != 1 or active[0].get("percent") != 100 or not active[0].get("revisionName"):
        raise ValueError("primary_serving_revision_unknown")
    region = current["metadata"]["labels"]["cloud.googleapis.com/location"]
    revision = read(["revisions", "describe", active[0]["revisionName"], "--region", region])
    values = {row["name"]: row.get("value", "")
              for row in revision["spec"]["containers"][0].get("env", [])}
    if values.get("IBKR_UNIFIED_NOTIFICATION_ENABLED", "").strip().lower() != "true":
        return False
    if (values.get("DIGEST_STORE_TARGET") != env["STORE_TARGET"]
            or not values.get("DIGEST_OPAQUE_ACCOUNT_UID", "").strip()
            or not values.get("DIGEST_TARGET_ID", "").strip()
            or not env.get("DIGEST_CANDIDATES_GCS_URI", "").strip()
            or values.get("DIGEST_CANDIDATES_GCS_URI", "").rstrip("/")
            != env["DIGEST_CANDIDATES_GCS_URI"].rstrip("/")):
        raise ValueError("primary_writer_binding_incomplete")
    return True


def main() -> int:
    try:
        owns = runtime_owns_slot(os.environ)
    except Exception:
        # Cloud metadata and client errors may contain private names or config.
        print("digest_writer_ownership status=unknown reason=primary_writer_not_verified")
        return 2
    print("digest_writer_ownership owner=" + ("runtime" if owns else "legacy"))
    return 0 if owns else 1


if __name__ == "__main__":
    raise SystemExit(main())
