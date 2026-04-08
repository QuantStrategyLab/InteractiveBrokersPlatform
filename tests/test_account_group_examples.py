from pathlib import Path

from runtime_config_support import parse_account_group_configs


def test_default_account_group_example_is_valid():
    example_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "examples"
        / "ibkr-account-groups.default.json"
    )

    configs = parse_account_group_configs(example_path.read_text(encoding="utf-8"))
    default_group = configs["default"]

    assert default_group.ib_gateway_instance_name == "interactive-brokers-quant-instance"
    assert default_group.ib_gateway_zone == "us-central1-c"
    assert default_group.ib_gateway_mode == "paper"
    assert default_group.ib_gateway_ip_mode == "internal"
    assert default_group.ib_client_id == 1
    assert default_group.service_name == "interactive-brokers-quant-service"
    assert default_group.account_ids == ("DU1234567",)
