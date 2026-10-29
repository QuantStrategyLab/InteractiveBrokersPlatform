from pathlib import Path


def test_initial_state_sync_is_opt_in_private_and_narrow():
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    caller = (root / 'sync-cloud-run-env.yml').read_text()
    protected = (root / 'sync-cloud-run-env-protected.yml').read_text()
    assert 'initial_state_source_run_id:' in caller
    assert "initial_state_source_run_id: ${{ inputs.initial_state_source_run_id }}" in caller
    assert "initial_state_ledger_requires_one_disabled_env_only_target" in protected
    assert 'env -u IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_PATH' in protected
    assert 'IBKR_INITIAL_SYNC_PLAN_PATH' in protected
    assert 'validate_initial_state_baseline' in protected
    assert 'initial_state_service_changed' in protected
    assert 'initial_state_update_failed_or_unknown' in protected
    assert '--initial-state-only' in protected
    staged = protected.split('if [ -n "${INPUT_INITIAL_STATE_SOURCE_RUN_ID:-}" ]; then\n            gcloud run services describe', 1)[1].split('          if [ "${RUNTIME_SWITCH_ONLY:-false}"', 1)[0]
    assert 'build_initial_state_stage' in staged and 'method="PUT"' in staged
    for forbidden in ('--update-secrets', '--remove-secrets', '--remove-env-vars', 'scheduler jobs', 'add-iam-policy-binding'):
        assert forbidden not in staged
    for step in ('Sync Cloud Run environment', 'Verify strategy plugin mounts'):
        block = protected.split(f'      - name: {step}\n', 1)[1].split('\n      - name:', 1)[0]
        assert "inputs.initial_state_source_run_id == ''" in block.splitlines()[0]
