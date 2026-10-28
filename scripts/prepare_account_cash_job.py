"""Prepare one protected cash Job definition and image input; never build or deploy."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re

from scripts import diagnose_account_facts_identity as identity
from scripts import inspect_account_facts_archive as archive
from scripts import preflight_cash_readonly_job as preflight
from scripts import observe_maintenance_cash_job as maintenance
from scripts import verify_account_cash_image as image_verifier


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/account_cash_readonly_entry.py",
    "scripts/maintenance_cash_job_entry.py",
    "scripts/patch_cash_candidate_image.py",
    "application/account_facts.py",
    "application/ibkr_cash_observations.py",
    "application/ibkr_portfolio.py",
)
ENTRY = "from scripts.account_cash_readonly_entry import main; raise SystemExit(main())"
IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
JOB = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
POSTBUILD_BASES = {
    "global_etf_rotation": "sha256:8ed44e325ff9033986249b6524453fdfd3cacb0636bf342012c6b6f76fcdfd31",
    "russell_top50_leader_rotation": "sha256:391a8bc5c551cc9b9bdaf044d21723641aa5ffe9297ef24f019465462ac299e5",
}


def prepare(env, expected, *, reader=preflight.binding._read):
    """Read the selected serving revision once and bind its Job inputs to it."""
    revisions = []

    def record(kind, *args):
        value = reader(kind, *args)
        if kind == "revisions":
            revisions.append(copy.deepcopy(value))
        return value

    evidence = preflight.inspect(env, expected=expected, reader=record)
    if len(revisions) != 1:
        raise ValueError("revision_unavailable")
    revision = revisions[0]
    base, _ = preflight.image_identity.immutable_image(revision)
    if not IMAGE.fullmatch(base):
        raise ValueError("base_image_invalid")
    sources = {name: (ROOT / name).read_bytes() for name in FILES}
    dockerfile = ("FROM " + base + "\nUSER root\n" + "".join(
        "COPY --chown=1000:1000 " + name + " /app/" + name + "\n" for name in FILES)
        + "RUN chmod 0644 " + " ".join("/app/" + name for name in FILES)
        + " && /app/.venv/bin/python /app/scripts/patch_cash_candidate_image.py\nUSER appuser\n")
    manifest = {"base_image": base, "serving_revision_sha256": evidence["serving_revision_sha256"],
        "target_binding_sha256": evidence["target_binding_sha256"],
        "report_prefix_sha256": evidence["report_prefix_sha256"],
        "service_account_sha256": evidence["service_account_sha256"],
        "network_declarations_sha256": evidence["network_declarations_sha256"],
        "secret_references_sha256": evidence["secret_references_sha256"],
        "secret_reference_count": evidence["secret_reference_count"], "files_sha256": {
        name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}}
    candidate = env.get("CASH_CANDIDATE_IMAGE", "")
    job = env.get("CASH_CANDIDATE_JOB", "")
    document = None
    if candidate or job:
        if not IMAGE.fullmatch(candidate) or not JOB.fullmatch(job):
            raise ValueError("candidate_invalid")
        mode = env.get("CASH_IMAGE_EVIDENCE_MODE", "prebuild")
        if mode == "prebuild":
            prior = json.loads(Path(env["CASH_BUILD_INPUT"]).read_text(encoding="utf-8"))
            if prior != manifest:
                raise ValueError("build_input_changed")
        elif mode == "postbuild":
            approved = POSTBUILD_BASES.get(env.get("REQUESTED_TARGET"))
            if env.get("CASH_BUILD_INPUT") or not approved or base.rsplit("@", 1)[1] != approved:
                raise ValueError("postbuild_source_mismatch")
        else:
            raise ValueError("image_evidence_mode_invalid")
        image_verifier.verify(base, candidate, sources, account=env.get("CASH_REGISTRY_ACCOUNT"))
        after = preflight.inspect(env, expected=expected, reader=reader)
        for key in ("serving_revision_sha256", "target_binding_sha256", "report_prefix_sha256",
                    "service_account_sha256", "network_declarations_sha256", "secret_references_sha256",
                    "secret_reference_count", "immutable_image_sha256"):
            if evidence[key] != after[key]:
                raise ValueError("serving_changed_after_image_verification")
        revision = copy.deepcopy(revision)
        container = revision["spec"]["containers"][0]
        container["image"] = candidate
        rows = container.setdefault("env", [])
        additions = {"CASH_READONLY_PROFILE": env["REQUESTED_TARGET"],
                     "CASH_READONLY_TARGET_SHA256": evidence["target_binding_sha256"],
                     "CASH_READONLY_REPORT_PREFIX_SHA256": evidence["report_prefix_sha256"]}
        if any(row.get("name") in additions for row in rows):
            raise ValueError("cash_environment_already_present")
        rows.extend({"name": key, "value": value} for key, value in additions.items())
        document = maintenance.job_document(revision, job, ENTRY, image_digest=candidate.rsplit("@", 1)[1])
    return dockerfile, manifest, sources, document


def main():
    try:
        slot = archive.resolve_profile_slot(os.environ)
        os.environ["IBKR_ACCOUNT_FACTS_TARGET"] = slot
        dockerfile, manifest, sources, document = prepare(os.environ, identity.resolve_expected())
        destination = Path(os.environ["CASH_CANDIDATE_DIR"])
        if not destination.is_absolute():
            raise ValueError("destination_invalid")
        destination.mkdir(mode=0o700)
        for name, content in sources.items():
            path = destination / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
        for name, content in (("Dockerfile", dockerfile), ("image-input.json", json.dumps(manifest)),
                              ("job.private.json", json.dumps(document) if document else None)):
            if content is not None:
                path = destination / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)
        print(json.dumps({"status": "prepared", "job_definition_prepared": document is not None}))
        return 0
    except Exception:
        print(json.dumps({"status": "failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
