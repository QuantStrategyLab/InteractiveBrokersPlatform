from application.execution_evidence import extract_execution_evidence


def summary(**kwargs):
    value = {key: [] for key in ("orders_filled", "orders_pending", "orders_partially_filled",
        "option_orders_filled", "option_orders_pending", "option_orders_partially_filled", "orders_skipped", "option_orders_skipped")}
    value.update(kwargs)
    return value


def test_broker_acknowledgements_are_order_evidence_not_fills():
    order = {"symbol": "TQQQ", "side": "buy", "broker_order_id": "synthetic", "status": "Submitted"}
    result = extract_execution_evidence(summary(orders_pending=[order]), dry_run=False)
    assert result == ([order], None)


def test_order_fill_snapshots_are_not_execution_fills_without_execution_ids():
    order = {"symbol": "TQQQ", "side": "buy", "broker_order_id": "synthetic", "status": "Filled"}
    assert extract_execution_evidence(summary(orders_filled=[order]), dry_run=False) == ([order], None)


def test_unknown_incomplete_duplicate_and_dry_run_do_not_claim_complete_evidence():
    order = {"broker_order_id": "synthetic", "status": "Submitted"}
    for source, dry in ((summary(orders_pending=[order], outcome_unknown=True), False),
                        ({"orders_pending": [order]}, False),
                        (summary(orders_pending=[order, order]), False),
                        (summary(orders_pending=[order]), True), (summary(), False)):
        assert extract_execution_evidence(source, dry_run=dry) == (None, None)


def test_only_explicit_identified_fill_records_are_linked():
    fill = {"execution_id": "synthetic-fill", "quantity": 2}
    order = {"broker_order_id": "synthetic-order", "status": "Filled", "fills": [fill]}
    assert extract_execution_evidence(summary(orders_filled=[order]), dry_run=False) == ([order], [fill])
    missing_id = {**order, "fills": [{"quantity": 2}]}
    assert extract_execution_evidence(summary(orders_filled=[missing_id]), dry_run=False) == ([missing_id], None)


def test_incomplete_fill_coverage_across_pending_orders_is_unknown():
    filled = {"broker_order_id": "one", "status": "Filled", "fills": [{"execution_id": "fill-one"}]}
    pending = {"broker_order_id": "two", "status": "Submitted"}
    orders, fills = extract_execution_evidence(summary(orders_filled=[filled], orders_pending=[pending]), dry_run=False)
    assert len(orders) == 2 and fills is None


def test_rejected_broker_order_or_halted_submission_keeps_evidence_unknown():
    accepted = {"broker_order_id": "one", "status": "Submitted"}
    rejected = {"status": "Rejected", "reason": "Rejected"}
    assert extract_execution_evidence(summary(orders_pending=[accepted], orders_skipped=[rejected]), dry_run=False) == (None, None)
    assert extract_execution_evidence(summary(orders_pending=[accepted], submission_halted=True), dry_run=False) == (None, None)
