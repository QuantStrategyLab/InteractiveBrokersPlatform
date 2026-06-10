from __future__ import annotations

import re

from scripts import cloud_run_runtime_guard as guard


def test_scheduler_job_pattern_includes_service_alias():
    pattern = guard._scheduler_job_pattern_for_services(
        ["interactive-brokers-live-u1599-tqqq-service"]
    )

    assert re.search(pattern, "interactive-brokers-live-u1599-tqqq-service-scheduler")
    assert re.search(pattern, "interactive-brokers-live-u1599-tqqq-scheduler")
    assert not re.search(pattern, "interactive-brokers-live-u1660-soxl-scheduler")
