"""Model purpose does not grant paper or production release authority."""

from enum import StrEnum


class ModelRole(StrEnum):
    BASELINE = "BASELINE"
    RESEARCH_CHALLENGER = "RESEARCH_CHALLENGER"
    PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
    PRODUCTION_APPROVED = "PRODUCTION_APPROVED"


def research_model_role(model_name: str) -> ModelRole:
    if model_name == "crypto_v2":
        return ModelRole.BASELINE
    if model_name in {"crypto_v3_independent", "crypto_settlement_average_research_v1"}:
        return ModelRole.RESEARCH_CHALLENGER
    raise ValueError("MODEL_ROLE_NOT_REVIEWED")
