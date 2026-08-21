import importlib.util
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "cliaus_historical_monthly_harvest.py"
_SPEC = importlib.util.spec_from_file_location("cliaus_historical_monthly_harvest", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_expanding_samples_use_only_prior_complete_months() -> None:
    rows = []
    start = date(2020, 1, 1)
    for offset in range((date(2022, 1, 1) - start).days):
        day = start + timedelta(days=offset)
        rows.append((day, Decimal("0.1") if day.day == 25 else Decimal("0")))

    samples = _MODULE.expanding_samples(rows, cutoff_day=20, minimum_training_months=12)

    assert len(samples) >= 11
    assert samples[0]["month"] == "2021-01"
    assert samples[0]["training_months"] == "12"
    assert Decimal(samples[0]["month_to_date_inches"]) == 0
    assert Decimal(samples[0]["actual_total_inches"]) == Decimal("0.1")
