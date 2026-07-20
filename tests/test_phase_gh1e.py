from kalshi_predictor.phase_gh1e import _category


def test_gh1e_series_categories_are_exact() -> None:
    assert _category("KXBTC") == "crypto"
    assert _category("KXTEMPNYCH") == "weather"
