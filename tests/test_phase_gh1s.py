from kalshi_predictor.phase_gh1s import _row


def test_gh1s_preview_rows_default_to_explicit_safety() -> None:
    assert _row("T", "crypto_v2", False, "AMBIGUOUS")["safe_to_apply"] is False
    assert _row("T", "weather_v2", True, "EXACT_METADATA_MATCH")["safe_to_apply"] is True
