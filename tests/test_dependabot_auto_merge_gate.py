"""Synthetic offline fixtures for the Dependabot auto-merge gate; no network."""
import unittest

from scripts import dependabot_auto_merge_gate as gate

REPO = "QuantStrategyLab/ExamplePlatform"
SHA = "a" * 40
REQUIRED = ["test", "Commit message convention"]
STATUSES = ["risk-gate", "pr-template"]


def commit(sha=SHA, author="dependabot[bot]", committer="web-flow", verified=True):
    return {
        "sha": sha,
        "author": {"login": author},
        "committer": {"login": committer},
        "commit": {"committer": {"email": "noreply@github.com"}, "verification": {"verified": verified}},
    }


def pull(**overrides):
    value = {
        "state": "open",
        "draft": False,
        "user": {"login": "dependabot[bot]"},
        "labels": [{"name": "dependencies"}],
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "head": {"sha": SHA, "repo": {"full_name": REPO}},
        "commits": 1,
        "changed_files": 1,
        "title": "chore(deps): bump certifi from 2026.6.17 to 2026.10.19",
        "body": "update-type: version-update:semver-minor",
    }
    value.update(overrides)
    return value


def lock_file(name="uv.lock"):
    return {"filename": name, "status": "modified"}


def action_file(name=".github/workflows/ci.yml", new="b" * 40):
    old = "c" * 40
    patch = (
        "@@ -1,3 +1,3 @@\n context\n"
        f"-      - uses: actions/checkout@{old} # v5\n"
        f"+      - uses: actions/checkout@{new} # v6\n"
    )
    return {"filename": name, "status": "modified", "patch": patch, "additions": 1, "deletions": 1}


def run(name, conclusion="success", status="completed", id_=1, app=15368):
    return {"name": name, "status": status, "conclusion": conclusion, "id": id_, "app": {"id": app}}


def ok_statuses():
    return [{"context": "risk-gate", "state": "success"}, {"context": "pr-template", "state": "success"}]


class EligibilityTest(unittest.TestCase):
    def check(self, pr, files, commits=None):
        return gate.eligibility(pr, files, commits or [commit()], REPO, "main", SHA)

    def test_dependency_minor_is_eligible(self):
        self.assertEqual(self.check(pull(), [lock_file()]), (True, "dependencies"))

    def test_patch_requires_same_minor(self):
        pr = pull(title="chore(deps): bump x from 1.2.3 to 1.3.0", body="update-type: version-update:semver-patch")
        self.assertFalse(self.check(pr, [lock_file()])[0])
        pr = pull(title="chore(deps): bump x from 1.2.3 to 1.2.4", body="update-type: version-update:semver-patch")
        self.assertTrue(self.check(pr, [lock_file()])[0])

    def test_major_and_pre_one_are_refused(self):
        self.assertFalse(self.check(pull(body="update-type: version-update:semver-major"), [lock_file()])[0])
        pr = pull(title="chore(deps): bump pyasn1 from 0.6.4 to 0.6.5", body="update-type: version-update:semver-patch")
        self.assertFalse(self.check(pr, [lock_file()])[0])

    def test_missing_update_type_is_refused(self):
        self.assertFalse(self.check(pull(body=""), [lock_file()])[0])

    def test_docker_and_protected_workflows_need_a_human(self):
        self.assertFalse(self.check(pull(), [lock_file("Dockerfile")])[0])
        for name in (
            ".github/workflows/execution-report-heartbeat-protected.yml",
            ".github/workflows/runtime-target-lifecycle.yml",
            ".github/workflows/risk-gate.yml",
            ".github/workflows/deploy-pilot-no-traffic.yml",
        ):
            pr = pull(body="update-type: version-update:semver-minor")
            self.assertFalse(self.check(pr, [action_file(name)])[0], name)

    def test_pinned_action_bump_in_ci_is_eligible(self):
        self.assertEqual(self.check(pull(), [action_file()]), (True, "actions"))

    def test_action_bump_with_extra_edit_is_refused(self):
        file = action_file()
        file["patch"] += "+      run: echo extra\n"
        file["additions"] = 2
        self.assertFalse(self.check(pull(), [file])[0])

    def test_action_bump_must_keep_the_same_action(self):
        file = action_file()
        file["patch"] = file["patch"].replace("+      - uses: actions/checkout", "+      - uses: actions/evil")
        self.assertFalse(self.check(pull(), [file])[0])

    def test_mixed_files_are_refused(self):
        self.assertFalse(self.check(pull(changed_files=2), [lock_file(), action_file()])[0])

    def test_untrusted_pull_requests_are_refused(self):
        cases = [
            pull(draft=True),
            pull(labels=[]),
            pull(user={"login": "someone"}),
            pull(head={"sha": SHA, "repo": {"full_name": "fork/ExamplePlatform"}}),
            pull(base={"ref": "dev", "repo": {"full_name": REPO}}),
        ]
        for pr in cases:
            self.assertFalse(self.check(pr, [lock_file()])[0])

    def test_human_or_unsigned_commits_are_refused(self):
        for bad in (commit(author="human"), commit(committer="human"), commit(verified=False)):
            self.assertFalse(self.check(pull(), [lock_file()], [bad])[0])

    def test_head_mismatch_and_incomplete_lists_are_refused(self):
        self.assertFalse(gate.eligibility(pull(), [lock_file()], [commit()], REPO, "main", "d" * 40)[0])
        self.assertFalse(self.check(pull(commits=2), [lock_file()])[0])
        self.assertFalse(self.check(pull(changed_files=3), [lock_file()])[0])

    def test_renamed_or_added_files_are_refused(self):
        self.assertFalse(self.check(pull(), [{"filename": "uv.lock", "status": "added"}])[0])
        self.assertFalse(self.check(pull(), [{"filename": "uv.lock", "status": "modified", "previous_filename": "x"}])[0])


class CheckStateTest(unittest.TestCase):
    def state(self, runs, statuses=None):
        return gate.check_state(runs, ok_statuses() if statuses is None else statuses, REQUIRED, STATUSES)[0]

    def test_all_green(self):
        runs = [run("test"), run("Commit message convention"), run("CodeRabbit", "skipped")]
        self.assertEqual(self.state(runs), "green")

    def test_non_required_red_blocks(self):
        runs = [run("test"), run("Commit message convention", "failure"), run("extra", "cancelled")]
        self.assertEqual(self.state(runs), "red")
        self.assertEqual(self.state([run("test"), run("Commit message convention"), run("other", "timed_out")]), "red")

    def test_pending_and_missing_wait(self):
        self.assertEqual(self.state([run("test"), run("Commit message convention", None, "in_progress")]), "pending")
        self.assertEqual(self.state([run("test")]), "pending")
        self.assertEqual(self.state([run("test"), run("Commit message convention")], []), "pending")

    def test_red_status_blocks(self):
        bad = [{"context": "risk-gate", "state": "failure"}, {"context": "pr-template", "state": "success"}]
        self.assertEqual(self.state([run("test"), run("Commit message convention")], bad), "red")

    def test_latest_run_wins_over_superseded_cancelled_run(self):
        runs = [run("test"), run("Commit message convention"), run("Evaluate", "cancelled", id_=1), run("Evaluate", "success", id_=2)]
        self.assertEqual(self.state(runs), "green")
        runs[-1] = run("Evaluate", "failure", id_=3)
        self.assertEqual(self.state(runs), "red")


if __name__ == "__main__":
    unittest.main()
