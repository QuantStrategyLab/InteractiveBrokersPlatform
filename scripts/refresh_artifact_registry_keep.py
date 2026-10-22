"""Protect current Cloud Run image digests before a release can replace them.

This runs only inside the existing authenticated deploy job. It never enables
cleanup or deletes an image. A failed inventory, update, or readback raises and
therefore stops the deploy before its first Cloud Run mutation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


KEEP_ID = "keep-current-pending-two-rollback"
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
Run = Callable[[list[str]], str]


def run_gcloud(args: list[str]) -> str:
    result = subprocess.run(["gcloud", *args], check=True, capture_output=True, text=True)
    return result.stdout


def read_json(run: Run, args: list[str]) -> Any:
    return json.loads(run([*args, "--format=json"]))


def resource_name(row: dict[str, Any]) -> str:
    name = row.get("metadata", {}).get("name") or row.get("name")
    short = str(name or "").rsplit("/", 1)[-1]
    if not NAME.fullmatch(short):
        raise ValueError("Cloud Run inventory has an invalid resource name")
    return short


def image_digest(image: str, image_repo: str) -> str | None:
    if not isinstance(image, str) or not image:
        raise ValueError("Cloud Run image reference is missing")
    repository_root = image_repo.split("/", 3)
    if len(repository_root) != 4:
        raise ValueError("Managed Artifact Registry image path is invalid")
    repository_prefix = "/".join(repository_root[:3]) + "/"
    if not image.startswith(repository_prefix):
        return None  # Another Artifact Registry repository is outside this policy.
    if "@" not in image:
        raise ValueError("A managed image is not pinned to an immutable digest")
    digest = image.rsplit("@", 1)[-1]
    if not SHA.fullmatch(digest):
        raise ValueError("A managed image has an invalid digest")
    return digest


def revision_images(row: dict[str, Any]) -> list[str]:
    status = row.get("status") or {}
    resolved = status.get("imageDigest")
    spec = row.get("spec") or {}
    containers = spec.get("containers") or (spec.get("template") or {}).get("spec", {}).get("containers")
    if (not isinstance(containers, list) or not containers
            or any(not isinstance(c, dict) or not c.get("image") for c in containers)):
        raise ValueError("Revision container inventory is incomplete")
    images = [c["image"] for c in containers]
    if isinstance(resolved, str) and resolved:
        image_path = images[0].split("@", 1)[0]
        if ":" in image_path.rsplit("/", 1)[-1]:
            image_path = image_path.rsplit(":", 1)[0]
        if SHA.fullmatch(resolved):
            images[0] = f"{image_path}@{resolved}"
        else:
            resolved_path, marker, digest = resolved.rpartition("@")
            if marker != "@" or resolved_path != image_path or not SHA.fullmatch(digest):
                raise ValueError("Revision resolved image digest is invalid or mismatched")
            images[0] = resolved
    return images


def job_images(row: dict[str, Any]) -> list[str]:
    # Cloud Run v1 and v2 nest Job templates differently; inspect only the
    # template/spec tree, never status or an unrelated execution record.
    node = row
    for _ in range(7):
        containers = node.get("containers") if isinstance(node, dict) else None
        if containers is not None:
            if not isinstance(containers, list) or not containers:
                break
            images = [c.get("image") for c in containers if isinstance(c, dict)]
            if len(images) != len(containers) or any(not image for image in images):
                break
            return images
        node = (node.get("template") or node.get("spec") or {}) if isinstance(node, dict) else {}
    raise ValueError("Job has no complete container image inventory")


def collect_digests(run: Run, *, project: str, region: str, image_repo: str) -> set[str]:
    common = [f"--project={project}", f"--region={region}"]
    services = read_json(run, ["run", "services", "list", *common])
    jobs = read_json(run, ["run", "jobs", "list", *common])
    if not isinstance(services, list) or not services or not isinstance(jobs, list):
        raise ValueError("Cloud Run inventory is incomplete")
    digests: set[str] = set()
    service_images = 0
    for item in services:
        service = read_json(run, ["run", "services", "describe", resource_name(item), *common])
        status = service.get("status") or {}
        traffic = status.get("traffic")
        if not isinstance(traffic, list):
            raise ValueError("Service has no resolved traffic inventory")
        revisions = {
            entry.get("revisionName")
            for entry in traffic
            if isinstance(entry, dict) and int(entry.get("percent") or 0) > 0
        }
        if any(not name for name in revisions) or (traffic and not revisions):
            raise ValueError("Service traffic has no resolved revision")
        for key in ("latestReadyRevisionName", "latestCreatedRevisionName"):
            if status.get(key):
                revisions.add(status[key])
        if not revisions:
            raise ValueError("Service has no revision to protect")
        for revision in revisions:
            if not NAME.fullmatch(str(revision)):
                raise ValueError("Service revision name is invalid")
            record = read_json(run, ["run", "revisions", "describe", revision, *common])
            for image in revision_images(record):
                digest = image_digest(image, image_repo)
                if digest:
                    digests.add(digest)
                    service_images += 1
    if not service_images:
        raise ValueError("No serving image belongs to the managed repository")
    for item in jobs:
        job_name = resource_name(item)
        job = read_json(run, ["run", "jobs", "describe", job_name, *common])
        for image in job_images(job):
            digest = image_digest(image, image_repo)
            if digest:
                digests.add(digest)
        executions = read_json(run, ["run", "jobs", "executions", "list", f"--job={job_name}", *common])
        if not isinstance(executions, list):
            raise ValueError("Job execution inventory is incomplete")
        for item in executions:
            if (item.get("status") or {}).get("completionTime"):
                continue
            execution = read_json(run, ["run", "jobs", "executions", "describe",
                                        resource_name(item), *common])
            if (execution.get("status") or {}).get("completionTime"):
                continue
            for image in job_images(execution):
                digest = image_digest(image, image_repo)
                if digest:
                    digests.add(digest)
    return digests


def policies(repository: dict[str, Any], expected_name: str) -> dict[str, Any]:
    if repository.get("name") != expected_name or repository.get("cleanupPolicyDryRun") is not True:
        raise ValueError("Repository identity or dry-run mode changed")
    policy_set = repository.get("cleanupPolicies")
    if not isinstance(policy_set, dict) or len(policy_set) != 6 or KEEP_ID not in policy_set:
        raise ValueError("Expected six cleanup policies are unavailable")
    keep = policy_set[KEEP_ID]
    condition = keep.get("condition") or {}
    prefixes = condition.get("versionNamePrefixes")
    if (keep.get("action") != "KEEP" or condition.get("tagState") != "ANY"
            or not isinstance(prefixes, list) or not prefixes
            or any(not isinstance(p, str) or not p.startswith("sha256:") for p in prefixes)):
        raise ValueError("Existing KEEP policy shape changed")
    return policy_set


def covers(prefixes: list[str], digest: str) -> bool:
    return any(digest.startswith(prefix) for prefix in prefixes)


def merged_policies(current: dict[str, Any], required: set[str]) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    prefixes = merged[KEEP_ID]["condition"]["versionNamePrefixes"]
    for digest in sorted(required):
        if not SHA.fullmatch(digest):
            raise ValueError("Required image digest is invalid")
        if not covers(prefixes, digest):
            prefixes.append(digest)
    return merged


def cli_policies(policy_set: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for name, policy in sorted(policy_set.items()):
        if policy.get("id") != name or policy.get("action") not in {"KEEP", "DELETE"}:
            raise ValueError("Unrecognized cleanup policy shape")
        row = {"name": name, "action": {"type": policy["action"].title()}}
        if "condition" in policy:
            row["condition"] = json.loads(json.dumps(policy["condition"]))
            if "tagState" in row["condition"]:
                row["condition"]["tagState"] = row["condition"]["tagState"].lower()
        elif "mostRecentVersions" in policy:
            row["mostRecentVersions"] = policy["mostRecentVersions"]
        else:
            raise ValueError("Cleanup policy has no condition")
        result.append(row)
    return result


def refresh(run: Run, *, project: str, region: str, repository_id: str,
            image_repo: str, candidate: str, rollback: list[str]) -> set[str]:
    expected_name = f"projects/{project}/locations/{region}/repositories/{repository_id}"
    if not candidate.startswith(image_repo + "@"):
        raise ValueError("Candidate is outside the selected image package")
    candidate_digest = image_digest(candidate, image_repo)
    if not candidate_digest:
        raise ValueError("Candidate is outside the managed repository")
    if len(set(rollback)) < 2:
        raise ValueError("Two distinct designated rollback digests are required")
    required = collect_digests(run, project=project, region=region, image_repo=image_repo)
    required.add(candidate_digest)
    for digest in rollback:
        if not SHA.fullmatch(digest):
            raise ValueError("Rollback digest is invalid")
        required.add(digest)
    repo_args = ["artifacts", "repositories", "describe", repository_id,
                 f"--project={project}", f"--location={region}"]
    before = read_json(run, repo_args)
    current = policies(before, expected_name)
    desired = merged_policies(current, required)
    if desired != current:
        fresh = read_json(run, repo_args)
        if fresh != before:
            raise ValueError("Repository changed before KEEP update")
        with tempfile.TemporaryDirectory() as directory:
            policy_file = Path(directory) / "policies.json"
            policy_file.write_text(json.dumps(cli_policies(desired)), encoding="utf-8")
            run(["artifacts", "repositories", "set-cleanup-policies", repository_id,
                 f"--project={project}", f"--location={region}",
                 f"--policy={policy_file}", "--dry-run", "--quiet"])
    after = read_json(run, repo_args)
    readback = policies(after, expected_name)
    if readback != desired or not all(covers(readback[KEEP_ID]["condition"]["versionNamePrefixes"], digest)
                                       for digest in required):
        raise ValueError("KEEP policy readback did not cover every required image")
    return required


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--image-repo", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--rollback-digest", action="append", default=[])
    args = parser.parse_args()
    for value in (args.project, args.region, args.repository):
        if not NAME.fullmatch(value):
            parser.error("project, region, or repository is invalid")
    protected = refresh(run_gcloud, project=args.project, region=args.region,
                        repository_id=args.repository, image_repo=args.image_repo,
                        candidate=args.candidate, rollback=args.rollback_digest)
    print(f"Verified KEEP coverage for {len(protected)} immutable images; dry-run unchanged.")


if __name__ == "__main__":
    main()
