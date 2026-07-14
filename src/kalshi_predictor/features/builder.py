from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import FeatureSnapshot, MarketSnapshot
from kalshi_predictor.features.external_features import build_external_features
from kalshi_predictor.features.market_features import build_market_features
from kalshi_predictor.features.repository import insert_feature_snapshot


def build_feature_snapshot(
    session: Session,
    snapshot: MarketSnapshot,
) -> FeatureSnapshot:
    market_features = build_market_features(snapshot)
    external_features = build_external_features(session, snapshot.ticker)
    return insert_feature_snapshot(
        session,
        ticker=snapshot.ticker,
        captured_at=snapshot.captured_at,
        market_features=market_features,
        external_features=external_features,
    )

