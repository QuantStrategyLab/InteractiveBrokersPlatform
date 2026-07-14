from pathlib import Path


def test_env_sync_creates_non_trading_health_warmup_with_retries() -> None:
    workflow = Path(".github/workflows/sync-cloud-run-env.yml").read_text(encoding="utf-8")

    assert 'scheduler.get("probe_time")' in workflow
    assert 'warmup_job_name="${cloud_run_service%-service}-warmup-scheduler"' in workflow
    assert 'warmup_uri="${service_url}/health"' in workflow
    assert '--http-method=GET' in workflow
    assert '--max-retry-attempts=2' in workflow
    assert workflow.count('--max-retry-attempts=2') == 2
    assert '--attempt-deadline=60s' in workflow
    assert 'warmup_uri="${service_url}/healthz"' not in workflow
    assert 'warmup_uri="${service_url}/probe"' not in workflow
