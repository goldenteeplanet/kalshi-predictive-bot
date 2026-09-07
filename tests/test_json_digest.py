from decimal import Decimal

import pytest
from kalshi_predictor.phase4cd import read_model_differential_replay
from kalshi_predictor.ui import dashboard_settlement_timeline
from kalshi_predictor.workstation import (
    component_recovery_differential_replay,
    recovery_replay_idempotency,
)


@pytest.mark.parametrize(
    ("module", "keyword"),
    [
        (component_recovery_differential_replay, "value"),
        (recovery_replay_idempotency, "value"),
        (read_model_differential_replay, "payload"),
        (dashboard_settlement_timeline, "payload"),
    ],
)
def test_recovery_digest_preserves_bytes_and_keyword_contract(module, keyword) -> None:
    value = {"b": [True, None, 1.25], "a": "é"}
    expected = "e6aebae999287cae2eb881d130514f9f28a5f4544e36037d1dbb17028d0b7be3"
    assert module._hash(**{keyword: value}) == expected
    assert module._hash({"a": "é", "b": [True, None, 1.25]}) == expected
    with pytest.raises(TypeError):
        module._hash(Decimal("1.25"))
