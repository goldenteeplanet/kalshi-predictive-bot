from __future__ import annotations

RIGHTS_GATED_CORPORA = frozenset({"PredictionMarketBench", "Prediction_Markets_Public"})
APPROVED_SYNTHETIC_ONLY = frozenset(
    {
        "AgentTrader",
        "prediction-market-backtester",
        "kalshi-kit",
        "15-minute-crypto-bot-concepts",
        "Homerun-execution-models",
    }
)


def corpus_access_status(name: str, *, authorization_registered: bool = False) -> str:
    if name in RIGHTS_GATED_CORPORA and not authorization_registered:
        return "BLOCKED_PENDING_RIGHTS"
    return "ADAPTER_FIXTURES_ALLOWED" if name in APPROVED_SYNTHETIC_ONLY else "APPROVED"


def require_corpus_access(name: str, *, authorization_registered: bool = False) -> None:
    status = corpus_access_status(name, authorization_registered=authorization_registered)
    if status == "BLOCKED_PENDING_RIGHTS":
        raise PermissionError(f"{name}:BLOCKED_PENDING_RIGHTS")
