"""Owner-approved AR policy maintenance; cloud diagnostics stay private."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from scripts.refresh_artifact_registry_keep import (
    KEEP_ID, NAME, SHA, collect_digests, image_digest, read_json,
)

POLICY_NAMES = {
    KEEP_ID, "keep-newest-ten-per-package", "keep-recent-2-per-package",
    "delete-aged-tagged", "delete-aged-untagged", "delete-untagged-after-14d",
}


def safe_gcloud(args):
    try:
        return subprocess.run(["gcloud", *args], check=True, capture_output=True,
                              text=True).stdout
    except subprocess.CalledProcessError:
        raise RuntimeError("Cloud command failed; stop without retry") from None


def api_policies(rows):
    if not isinstance(rows, list) or len(rows) != 6:
        raise ValueError("Exactly six reviewed policies are required")
    result = {}
    for row in rows:
        name = row.get("name")
        if name not in POLICY_NAMES or name in result:
            raise ValueError("Unexpected or duplicate policy name")
        action = row.get("action")
        if action not in ({"type": "Keep"}, {"type": "Delete"}):
            raise ValueError("Invalid policy action")
        policy = {"id": name, "action": action["type"].upper()}
        if "condition" in row:
            if set(row) != {"name", "action", "condition"}:
                raise ValueError("Unexpected policy fields")
            condition = dict(row["condition"])
            if name == KEEP_ID:
                if action != {"type": "Keep"} or condition != {
                    "tagState": "any", "versionNamePrefixes": ["sha256:"]
                }:
                    raise ValueError("The blanket SHA256 KEEP must not be narrowed")
            elif name not in {"delete-aged-tagged", "delete-aged-untagged", "delete-untagged-after-14d"} or action != {"type": "Delete"} or condition != {
                "tagState": "tagged" if name == "delete-aged-tagged" else "untagged",
                "olderThan": {"delete-aged-tagged": "2592000s",
                              "delete-aged-untagged": "604800s",
                              "delete-untagged-after-14d": "1209600s"}.get(name),
            }:
                raise ValueError("Delete policies must retain their reviewed age bounds")
            condition["tagState"] = condition["tagState"].upper()
            policy["condition"] = condition
        else:
            count = {"keep-newest-ten-per-package": 10, "keep-recent-2-per-package": 2}.get(name)
            if (set(row) != {"name", "action", "mostRecentVersions"}
                    or action != {"type": "Keep"} or count is None
                    or row["mostRecentVersions"] != {"keepCount": count}):
                raise ValueError("Unexpected retention policy")
            policy["mostRecentVersions"] = row["mostRecentVersions"]
        result[name] = policy
    if set(result) != POLICY_NAMES or KEEP_ID not in result:
        raise ValueError("Required policy names are missing")
    return result


def repository_state(record, expected_name):
    if not isinstance(record, dict) or record.get("name") != expected_name:
        raise ValueError("Repository identity is invalid")
    if type(record.get("cleanupPolicyDryRun")) is not bool:
        raise ValueError("Repository cleanup mode is missing or invalid")
    if not isinstance(record.get("cleanupPolicies", {}), dict):
        raise ValueError("Repository policy state is invalid")
    if not record.get("updateTime"):
        raise ValueError("Repository update identity is missing")
    return record["cleanupPolicyDryRun"]


def maintain(run, *, project, region, repository_id, image_repo, candidate,
             policy_file, mode, dry_run):
    if mode not in {"inspect", "repair"} or type(dry_run) is not bool:
        raise ValueError("Invalid maintenance mode")
    if any(not NAME.fullmatch(v) for v in (project, region, repository_id)):
        raise ValueError("Invalid target configuration")
    expected_name = f"projects/{project}/locations/{region}/repositories/{repository_id}"
    expected_image = f"{region}-docker.pkg.dev/{project}/{repository_id}/interactivebrokersplatform/cloud-run"
    if image_repo != expected_image or not candidate.startswith(image_repo + "@"):
        raise ValueError("Candidate does not match the protected target")
    digest = image_digest(candidate, image_repo)
    if not digest or not SHA.fullmatch(digest):
        raise ValueError("Candidate digest is invalid")
    rows = json.loads(policy_file.read_text())
    desired = api_policies(rows)
    args = ["artifacts", "repositories", "describe", repository_id,
            f"--project={project}", f"--location={region}"]
    before = read_json(run, args)
    before_mode = repository_state(before, expected_name)
    required = collect_digests(run, project=project, region=region, image_repo=image_repo)
    required.add(digest)
    # Blanket KEEP covers serving, pending, rollback and future SHA256 images,
    # including images appearing between inventory and a concurrent deployment.
    if not required or any(not SHA.fullmatch(item) for item in required):
        raise ValueError("Immutable image inventory is invalid")
    written = False
    if mode == "repair" and not dry_run and before.get("cleanupPolicies", {}) != desired:
        fresh = read_json(run, args)
        repository_state(fresh, expected_name)
        if fresh != before:
            raise ValueError("Repository changed before policy update")
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policies.json"
            policy.write_text(json.dumps(rows))
            run(["artifacts", "repositories", "set-cleanup-policies", repository_id,
                 f"--project={project}", f"--location={region}", f"--policy={policy}", "--quiet"])
        written = True
    after = read_json(run, args)
    if repository_state(after, expected_name) != before_mode:
        raise ValueError("Repository cleanup mode changed")
    if written or (mode == "repair" and not dry_run):
        if after.get("cleanupPolicies") != desired:
            raise ValueError("Policy readback mismatch; stop without retry")
    elif after != before:
        raise ValueError("Repository changed during inspection")
    return {"written": written, "dry_run": dry_run, "mode": mode,
            "protected_digest_count": len(required), "proposed_policy_count": len(desired),
            "before_policy_count": len(before.get("cleanupPolicies", {})),
            "after_policy_count": len(after.get("cleanupPolicies", {})),
            "cleanup_mode_preserved": True, "blanket_keep": True}


def require_update_permission(project, region, repository):
    token = safe_gcloud(["auth", "print-access-token"]).strip()
    if not token:
        raise RuntimeError("Cloud authentication returned no access token")
    resource = f"projects/{project}/locations/{region}/repositories/{repository}"
    request = urllib.request.Request(
        f"https://artifactregistry.googleapis.com/v1/{resource}:testIamPermissions",
        data=json.dumps({"permissions": ["artifactregistry.repositories.get",
                                         "artifactregistry.repositories.update"]}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            permissions = set(json.load(response).get("permissions", []))
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise RuntimeError("AR permission check denied: owner must verify roles/artifactregistry.repoAdmin on the selected repository; no IAM change attempted") from None
        raise RuntimeError("AR permission verification failed; stop without retry") from None
    except Exception:
        raise RuntimeError("AR permission verification failed; stop without retry") from None
    if permissions != {"artifactregistry.repositories.get", "artifactregistry.repositories.update"}:
        raise RuntimeError("Missing AR get/update permission: owner must grant roles/artifactregistry.repoAdmin on the selected repository; no IAM change attempted")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["inspect", "repair"], required=True)
    parser.add_argument("--dry-run", choices=["true", "false"], default="true")
    args = parser.parse_args()
    try:
        project = os.environ["IBKR_GCP_PROJECT_ID"]
        region = os.environ["CLOUD_RUN_REGION"]
        repository = "cloud-run-source-deploy"
        digest = os.environ["IBKR_AR_CANDIDATE_DIGEST"]
        if not NAME.fullmatch(project) or not NAME.fullmatch(region) or not SHA.fullmatch(digest):
            raise ValueError("Protected target configuration is invalid")
        require_update_permission(project, region, repository)
        image_repo = f"{region}-docker.pkg.dev/{project}/{repository}/interactivebrokersplatform/cloud-run"
        result = maintain(safe_gcloud, project=project, region=region,
                          repository_id=repository, image_repo=image_repo,
                          candidate=image_repo + "@" + digest,
                          policy_file=Path("config/artifact-registry-cleanup-policies.json"),
                          mode=args.mode, dry_run=args.dry_run == "true")
    except RuntimeError as error:
        # Only our constant diagnostics reach logs. No cloud stderr or identifiers.
        print(str(error))
        raise SystemExit(1) from None
    except Exception:
        print("Maintenance validation failed; stop without retry")
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
