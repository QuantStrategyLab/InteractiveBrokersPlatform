#!/usr/bin/env python3
"""Fail-closed gate for the Dependabot auto-merge workflow.

The workflow runs from the default branch after a PR workflow completes. This
script decides whether the Dependabot PR may have auto-merge enabled:

* the PR is a same-repository, verified Dependabot update that touches only
  dependency manifests or SHA-pinned action version bumps in non-protected
  workflows (the same exclusions as the GdeiAssistant dependabot policy);
* EVERY check run and commit status on the PR head is green, including
  non-required ones such as the commit message check.

Anything else is skipped with a reason in the step summary and never merged.
Standard library only; the GitHub CLI supplies authentication via GH_TOKEN.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
import time

DEPENDABOT_LOGINS = {"dependabot[bot]"}
GREEN_CONCLUSIONS = {"success", "skipped", "neutral"}

# Dependency manifests that may change without a human. Dockerfiles, vendored
# wheels, tests and docs always need a human, even in a Dependabot PR.
DEPENDENCY_FILE = re.compile(r"^(?:uv\.lock|pyproject\.toml|(?:[\w.-]+/)*requirements[\w.-]*\.txt)$")
# Workflows where a pinned-action version bump may merge unattended. Everything
# else (deploy, runtime, secrets, governance) is reviewed by a person.
ACTION_FILES = {".github/workflows/ci.yml", ".github/workflows/docs-check.yml"}
# Same list as the GdeiAssistant policy: credential-bearing or policy workflows.
PROTECTED_WORKFLOW = re.compile(
    r"(deploy|release|publish|ghcr|pages|azure|risk-gate|governance|review-signal|dependabot|codeql|hygiene)",
    re.IGNORECASE,
)
ACTION_USES_LINE = re.compile(r"^[+-](\s*(?:-\s*)?uses:\s*)([\w.-]+/[\w./-]+)@([a-f0-9]{40})(?:\s+#.*)?$")
VERSION = re.compile(r"^v?([1-9]\d*)\.(\d+)(?:\.(\d+))?$")
UPDATE_TYPE = re.compile(r"update-type:\s*version-update:semver-(major|minor|patch)")
FROM_TO = re.compile(r"\bfrom (\S+) to (\S+)")


def action_version_only(file):
    """True when the diff only moves SHA-pinned `uses:` lines of the same actions."""
    name = file.get("filename", "")
    if name not in ACTION_FILES or not isinstance(file.get("patch"), str):
        return False
    if PROTECTED_WORKFLOW.search(posixpath.basename(name)):
        return False
    removed, added = [], []
    for line in file["patch"].split("\n"):
        if not line or line[0] not in "+-" or line.startswith(("---", "+++")):
            continue
        match = ACTION_USES_LINE.match(line)
        if not match:
            return False
        (removed if line[0] == "-" else added).append(match.group(1) + match.group(2))
    return bool(removed) and len(removed) == file.get("deletions") and len(added) == file.get("additions") and removed == added


def files_allowed(files):
    """Return (kind, reason). kind is 'actions', 'dependencies' or '' when refused."""
    if not files:
        return "", "no changed files"
    if any(f.get("status") != "modified" or f.get("previous_filename") for f in files):
        return "", "files were added, removed or renamed"
    if all(action_version_only(f) for f in files):
        return "actions", "pinned action version bump"
    if all(DEPENDENCY_FILE.match(f.get("filename", "")) for f in files):
        return "dependencies", "dependency manifests only"
    names = ", ".join(sorted(f.get("filename", "?") for f in files))
    return "", f"changed files need a human: {names}"[:300]


def trusted_pull_request(pr, repo, default_branch, head_sha, commits):
    """Return a reason string when the PR is not a verified same-repo Dependabot PR, else ''."""
    labels = {item.get("name", "") for item in pr.get("labels", [])}
    if pr.get("state") != "open" or pr.get("draft"):
        return "not an open, ready pull request"
    if (pr.get("user") or {}).get("login") not in DEPENDABOT_LOGINS or "dependencies" not in labels:
        return "not a Dependabot dependency PR"
    if (pr.get("base") or {}).get("ref") != default_branch or (pr.get("base") or {}).get("repo", {}).get("full_name") != repo:
        return "base is not this repository default branch"
    head = pr.get("head") or {}
    if (head.get("repo") or {}).get("full_name") != repo:
        return "head is not in this repository"
    if not re.fullmatch(r"[a-f0-9]{40}", head_sha or "") or head.get("sha") != head_sha:
        return "PR head does not match the completed run"
    if not commits or len(commits) != pr.get("commits") or commits[-1].get("sha") != head_sha:
        return "commit list is incomplete"
    for commit in commits:
        detail = commit.get("commit") or {}
        # Dependabot commits are authored by the bot and committed by GitHub web-flow; a human push fails this.
        if (
            (commit.get("author") or {}).get("login") not in DEPENDABOT_LOGINS
            or (commit.get("committer") or {}).get("login") != "web-flow"
            or (detail.get("committer") or {}).get("email") != "noreply@github.com"
            or (detail.get("verification") or {}).get("verified") is not True
        ):
            return "a commit is not a verified Dependabot commit"
    return ""


def version_update_allowed(kind, pr):
    """Patch/minor only; for non-action dependencies also require a stable >=1.0 version."""
    types = UPDATE_TYPE.findall(pr.get("body") or "")
    if len(types) != 1 or types[0] == "major":
        return "update type is missing, ambiguous or major"
    if kind == "actions":
        return ""
    match = FROM_TO.search(pr.get("title") or "")
    previous = VERSION.match(match.group(1)) if match else None
    new = VERSION.match(match.group(2)) if match else None
    if not previous or not new or previous.group(1) != new.group(1):
        return "versions are unknown, pre-1.0, prerelease or cross-major"
    prev_minor, new_minor = int(previous.group(2)), int(new.group(2))
    prev_patch, new_patch = int(previous.group(3) or 0), int(new.group(3) or 0)
    if types[0] == "patch":
        ok = prev_minor == new_minor and new_patch > prev_patch
    else:
        ok = new_minor > prev_minor
    return "" if ok else "version change does not match the declared update type"


def eligibility(pr, files, commits, repo, default_branch, head_sha):
    """Return (eligible, reason)."""
    reason = trusted_pull_request(pr, repo, default_branch, head_sha, commits)
    if reason:
        return False, reason
    if len(files) != pr.get("changed_files"):
        return False, "changed file list is incomplete"
    kind, reason = files_allowed(files)
    if not kind:
        return False, reason
    reason = version_update_allowed(kind, pr)
    return (False, reason) if reason else (True, reason or kind)


def check_state(check_runs, statuses, required_checks, required_statuses):
    """Return ('green'|'pending'|'red', details) over every check run and commit status."""
    latest = {}
    for run in check_runs:
        key = (run.get("name"), (run.get("app") or {}).get("id"))
        if key not in latest or run.get("id", 0) > latest[key].get("id", 0):
            latest[key] = run
    red, pending = [], []
    for (name, _app), run in sorted(latest.items(), key=lambda item: str(item[0])):
        if run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") not in GREEN_CONCLUSIONS:
            red.append(f"{name}={run.get('conclusion')}")
    by_name = {name for name, _app in latest}
    for status in statuses:
        if status.get("state") in ("failure", "error"):
            red.append(f"{status.get('context')}={status.get('state')}")
        elif status.get("state") != "success":
            pending.append(str(status.get("context")))
    status_state = {s.get("context"): s.get("state") for s in statuses}
    for name in required_checks:
        if name not in by_name:
            pending.append(f"{name} (not reported yet)")
    for name in required_statuses:
        if name not in status_state:
            pending.append(f"{name} (not reported yet)")
    if red:
        return "red", sorted(red)
    if pending:
        return "pending", sorted(pending)
    return "green", []


def gh(args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def api(route):
    return json.loads(gh(["api", route]))


def pages(route):
    return [item for page in json.loads(gh(["api", "--paginate", "--slurp", route])) for item in page]


def split_names(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def summary(lines):
    text = "\n".join(lines) + "\n"
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)


def fetch_checks(repo, sha):
    check_runs = []
    for page in json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100"])):
        check_runs.extend(page.get("check_runs", []))
    statuses = api(f"repos/{repo}/commits/{sha}/status?per_page=100").get("statuses", [])
    return check_runs, statuses


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["BRANCH_NAME"]
    expected_head = os.environ["EXPECTED_HEAD_SHA"]
    default_branch = os.environ["DEFAULT_BRANCH"]
    required_checks = split_names(os.environ.get("REQUIRED_CHECKS"))
    required_statuses = split_names(os.environ.get("REQUIRED_STATUSES"))
    wait_seconds = int(os.environ.get("CHECK_WAIT_SECONDS", "480"))
    poll_seconds = int(os.environ.get("CHECK_POLL_SECONDS", "20"))
    owner = repo.split("/")[0]

    prs = api(f"repos/{repo}/pulls?state=open&head={owner}:{branch}&per_page=2")
    if len(prs) != 1:
        summary(["## Auto-Merge Gate", f"- Skipped: expected exactly one open PR for `{branch}`, found {len(prs)}."])
        return 0
    number = prs[0]["number"]
    pr = api(f"repos/{repo}/pulls/{number}")
    commits = pages(f"repos/{repo}/pulls/{number}/commits?per_page=100")
    files = pages(f"repos/{repo}/pulls/{number}/files?per_page=100")
    eligible, reason = eligibility(pr, files, commits, repo, default_branch, expected_head)
    lines = ["## Auto-Merge Gate", f"- PR: {pr.get('html_url')}", f"- Head: `{expected_head}`", f"- Eligibility: `{reason}`"]
    if not eligible:
        summary(lines + ["- Decision: `skip` (needs a human)"])
        return 0

    deadline = time.monotonic() + wait_seconds
    while True:
        current = api(f"repos/{repo}/pulls/{number}")
        if current.get("state") != "open" or current["head"]["sha"] != expected_head:
            summary(lines + ["- Decision: `skip` (PR closed or head changed while waiting)"])
            return 0
        check_runs, statuses = fetch_checks(repo, expected_head)
        state, details = check_state(check_runs, statuses, required_checks, required_statuses)
        if state == "green":
            break
        if state == "red":
            summary(lines + [f"- Decision: `skip` (not green: {', '.join(details)})"])
            return 0
        if time.monotonic() >= deadline:
            summary(lines + [f"- Decision: `skip` (still pending after {wait_seconds}s: {', '.join(details)}); a later run re-evaluates"])
            return 0
        time.sleep(poll_seconds)

    # Every check run and commit status on the head is green. Branch protection still decides when the merge lands.
    gh(["pr", "merge", str(number), "--repo", repo, "--auto", "--rebase", "--delete-branch", "--match-head-commit", expected_head])
    summary(lines + ["- Checks: all check runs and statuses green", "- Decision: `auto-merge enabled`"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        detail = re.sub(r"\b(gh[pousr]_|github_pat_)\w+", "***", (error.stderr or "").strip())
        print(f"Auto-merge gate failed closed: {detail[:300]}", file=sys.stderr)
        sys.exit(1)
