from __future__ import annotations

import time

import pytest

from application.runtime_deadline import (
    RuntimeDeadlineExceeded,
    classify_runtime_deadline_stage,
    enforce_runtime_deadline,
    recycle_current_process_after_response,
)


def test_runtime_deadline_interrupts_stalled_main_thread_work() -> None:
    started = time.monotonic()

    with pytest.raises(RuntimeDeadlineExceeded, match="test operation exceeded the 1s"):
        with enforce_runtime_deadline(1, operation="test operation"):
            time.sleep(5)

    assert time.monotonic() - started < 2


def test_disabled_runtime_deadline_does_not_interrupt() -> None:
    with enforce_runtime_deadline(0, operation="disabled operation"):
        pass


def _synthetic_operation(module, name, action):
    namespace = {"__name__": module, "action": action}
    exec(f"def {name}():\n    private_value = 'SYNTHETIC_PRIVATE_VALUE'\n    action()", namespace)
    return namespace[name]


def test_real_alarm_classifies_observed_quote_wait_without_private_data() -> None:
    operation = _synthetic_operation(
        "application.execution_service", "get_market_prices", lambda: time.sleep(5)
    )
    with pytest.raises(RuntimeDeadlineExceeded) as caught:
        with enforce_runtime_deadline(1, operation="SYNTHETIC_PRIVATE_VALUE"):
            operation()
    assert classify_runtime_deadline_stage(caught.value) == "market_quote"


@pytest.mark.parametrize("module,name,stage", [
    ("strategy_runtime", "_build_historical_close_map", "historical_price_fallback"),
    ("application.runtime_strategy_adapters", "get_historical_close", "historical_data"),
    ("quant_platform_kit.common.execution_state", "has_marker", "execution_dedup_read"),
    ("quant_platform_kit.common.execution_state", "record_marker", "execution_state_write"),
    ("application.ibkr_portfolio", "fetch_portfolio_snapshot", "portfolio_snapshot"),
    ("application.runtime_broker_adapters", "connect_ib", "broker_connection"),
    ("strategy_runtime", "evaluate", "strategy_evaluation"),
])
def test_deadline_stage_mapping(module, name, stage) -> None:
    def fail():
        raise RuntimeDeadlineExceeded("SYNTHETIC_PRIVATE_VALUE")

    with pytest.raises(RuntimeDeadlineExceeded) as caught:
        _synthetic_operation(module, name, fail)()
    assert classify_runtime_deadline_stage(caught.value) == stage


@pytest.mark.parametrize("exception_type", [RuntimeError, TimeoutError])
def test_ordinary_exceptions_are_not_runtime_deadlines(exception_type) -> None:
    def fail():
        raise exception_type("SYNTHETIC_PRIVATE_VALUE")

    with pytest.raises(exception_type) as caught:
        _synthetic_operation("application.execution_service", "get_market_prices", fail)()
    assert classify_runtime_deadline_stage(caught.value) == "unknown"


def test_unknown_module_and_missing_traceback_keep_unknown() -> None:
    def fail():
        raise RuntimeDeadlineExceeded("SYNTHETIC_PRIVATE_VALUE")

    with pytest.raises(RuntimeDeadlineExceeded) as caught:
        _synthetic_operation("unrelated", "get_market_prices", fail)()
    assert classify_runtime_deadline_stage(caught.value) == "unknown"
    assert classify_runtime_deadline_stage(RuntimeDeadlineExceeded("private")) == "unknown"


def test_specific_qualification_stage_wins_over_enclosing_quote_phase() -> None:
    def fail():
        raise RuntimeDeadlineExceeded("private")

    qualify = _synthetic_operation("ib_insync.ib", "qualifyContracts", fail)
    quote = _synthetic_operation("application.execution_service", "get_market_prices", qualify)
    with pytest.raises(RuntimeDeadlineExceeded) as caught:
        quote()
    assert classify_runtime_deadline_stage(caught.value) == "contract_qualification"


def test_recycle_current_process_sends_sigterm_after_delay() -> None:
    calls = []

    thread = recycle_current_process_after_response(
        delay_seconds=0,
        process_id=1234,
        kill_fn=lambda process_id, signum: calls.append((process_id, signum)),
    )
    thread.join(timeout=1)

    assert calls == [(1234, 15)]
