"""Apply the reviewed deadline contract to the four allowlisted IBKR run jobs."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit


RUN_JOB_NAMES = (
    "interactive-brokers-quant-live-u15998061-scheduler",
    "interactive-brokers-quant-live-u16608560-scheduler",
    "interactive-brokers-quant-live-u18336562-scheduler",
    "interactive-brokers-quant-live-u18308207-scheduler",
)
WARMUP_JOB_NAMES = tuple(name.replace("-scheduler", "-warmup-scheduler") for name in RUN_JOB_NAMES)
RUN_DEADLINE = "330s"
WARMUP_DEADLINE = "60s"
SCHEDULER_SERVICE_ACCOUNT = "ibkr-platform-scheduler@interactivebrokersquant.iam.gserviceaccount.com"

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class ConfigOnlySyncError(RuntimeError):
    """The existing jobs do not satisfy the config-only safety contract."""


def _run_gcloud(
    command: list[str],
    *,
    run_command: RunCommand,
) -> subprocess.CompletedProcess[str]:
    return run_command(command, text=True, capture_output=True, check=True)


def _describe_job(
    name: str,
    *,
    project: str,
    location: str,
    run_command: RunCommand,
) -> dict[str, Any]:
    completed = _run_gcloud(
        [
            "gcloud",
            "scheduler",
            "jobs",
            "describe",
            name,
            f"--project={project}",
            f"--location={location}",
            "--format=json",
        ],
        run_command=run_command,
    )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigOnlySyncError(f"Unable to parse existing job {name}") from exc
    if not isinstance(payload, dict):
        raise ConfigOnlySyncError(f"Existing job {name} did not return an object")
    return payload


def _validate_job(
    job: dict[str, Any],
    *,
    expected_name: str,
    expected_method: str,
    expected_path: str,
    expected_deadline: str | None,
) -> None:
    actual_name = str(job.get("name") or "").rsplit("/", 1)[-1]
    if actual_name != expected_name:
        raise ConfigOnlySyncError(f"Expected existing job {expected_name}, got {actual_name or '<missing>'}")
    if job.get("state") != "ENABLED":
        raise ConfigOnlySyncError(f"Existing job {expected_name} is not ENABLED")

    target = job.get("httpTarget")
    if not isinstance(target, dict):
        raise ConfigOnlySyncError(f"Existing job {expected_name} has no HTTP target")
    method = str(target.get("httpMethod") or "")
    if method != expected_method:
        raise ConfigOnlySyncError(f"Existing job {expected_name} expected method {expected_method}")
    uri = urlsplit(str(target.get("uri") or ""))
    if expected_name.endswith("-warmup-scheduler"):
        expected_service = expected_name.removesuffix("-warmup-scheduler") + "-service"
    else:
        expected_service = expected_name.removesuffix("-scheduler") + "-service"
    hostname = uri.hostname or ""
    if (
        uri.scheme != "https"
        or not hostname.startswith(f"{expected_service}-")
        or not hostname.endswith(".run.app")
        or uri.netloc != hostname
        or uri.query
        or uri.fragment
    ):
        raise ConfigOnlySyncError(f"Existing job {expected_name} has an unexpected destination")
    if uri.path != expected_path:
        raise ConfigOnlySyncError(f"Existing job {expected_name} expected path {expected_path}")

    oidc = target.get("oidcToken")
    if not isinstance(oidc, dict):
        raise ConfigOnlySyncError(f"Existing job {expected_name} has no OIDC target")
    if oidc.get("serviceAccountEmail") != SCHEDULER_SERVICE_ACCOUNT:
        raise ConfigOnlySyncError(f"Existing job {expected_name} has an unexpected OIDC identity")
    if oidc.get("audience") != f"https://{hostname}":
        raise ConfigOnlySyncError(f"Existing job {expected_name} has an unexpected OIDC audience")
    if expected_deadline is not None and job.get("attemptDeadline") != expected_deadline:
        raise ConfigOnlySyncError(
            f"Existing job {expected_name} expected deadline {expected_deadline}"
        )


def _preflight(
    *,
    project: str,
    location: str,
    run_command: RunCommand,
) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    for name in RUN_JOB_NAMES:
        job = _describe_job(name, project=project, location=location, run_command=run_command)
        _validate_job(
            job,
            expected_name=name,
            expected_method="POST",
            expected_path="/run",
            expected_deadline=None,
        )
        jobs[name] = job
    for name in WARMUP_JOB_NAMES:
        job = _describe_job(name, project=project, location=location, run_command=run_command)
        _validate_job(
            job,
            expected_name=name,
            expected_method="GET",
            expected_path="/health",
            expected_deadline=WARMUP_DEADLINE,
        )
        jobs[name] = job
    return jobs


def sync_deadlines(
    *,
    project: str,
    location: str,
    run_command: RunCommand = subprocess.run,
) -> None:
    jobs = _preflight(project=project, location=location, run_command=run_command)
    update_failures: list[str] = []
    for name in RUN_JOB_NAMES:
        if jobs[name].get("attemptDeadline") == RUN_DEADLINE:
            continue
        try:
            _run_gcloud(
                [
                    "gcloud",
                    "scheduler",
                    "jobs",
                    "update",
                    "http",
                    name,
                    f"--project={project}",
                    f"--location={location}",
                    f"--attempt-deadline={RUN_DEADLINE}",
                    "--quiet",
                ],
                run_command=run_command,
            )
        except subprocess.CalledProcessError:
            update_failures.append(name)

    verified = _preflight(project=project, location=location, run_command=run_command)
    not_converged: list[str] = []
    for name in RUN_JOB_NAMES:
        if verified[name].get("attemptDeadline") != RUN_DEADLINE:
            not_converged.append(name)
    failed_names = list(dict.fromkeys([*update_failures, *not_converged]))
    if failed_names:
        raise ConfigOnlySyncError(f"Run deadline convergence failed for: {', '.join(failed_names)}")
    print("Verified four allowlisted run deadlines and four unchanged warmup deadlines.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", required=True)
    args = parser.parse_args()
    try:
        sync_deadlines(project=args.project, location=args.location)
    except (ConfigOnlySyncError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Config-only deadline sync failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
