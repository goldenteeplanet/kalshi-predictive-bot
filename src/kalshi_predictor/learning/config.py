from collections.abc import Mapping
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.utils.decimals import decimal_to_str


def learning_categories(settings: Settings) -> set[str]:
    return {
        item.strip().lower()
        for item in settings.learning_allowed_categories.split(",")
        if item.strip()
    }


def learning_paper_settings(settings: Settings) -> Settings:
    if not settings.learning_mode:
        return settings
    return settings.model_copy(
        update={
            "paper_min_edge": settings.learning_min_edge,
            "paper_max_order_quantity": settings.learning_max_paper_order_qty,
            "paper_max_position_per_market": settings.learning_max_paper_positions_per_market,
            "opportunity_min_edge": settings.learning_min_edge,
            "opportunity_min_score": settings.learning_min_opportunity_score,
            "opportunity_max_spread": settings.learning_max_spread,
            "opportunity_min_liquidity": settings.learning_min_liquidity,
            "execution_enabled": False,
            "execution_dry_run": True,
            "overnight_run_demo": False,
            "autopilot_dry_run": True,
        }
    )


def learning_config_payload(settings: Settings) -> Mapping[str, Any]:
    return {
        "LEARNING_MODE": settings.learning_mode,
        "LEARNING_MODEL_NAME": settings.learning_model_name,
        "LEARNING_TARGET_SETTLED_TRADES": settings.learning_target_settled_trades,
        "LEARNING_MIN_EDGE": decimal_to_str(settings.learning_min_edge),
        "LEARNING_MIN_OPPORTUNITY_SCORE": decimal_to_str(
            settings.learning_min_opportunity_score
        ),
        "LEARNING_MAX_PAPER_ORDER_QTY": settings.learning_max_paper_order_qty,
        "LEARNING_MAX_PAPER_POSITIONS_PER_MARKET": (
            settings.learning_max_paper_positions_per_market
        ),
        "LEARNING_MAX_DAILY_PAPER_TRADES": settings.learning_max_daily_paper_trades,
        "LEARNING_MIN_TRADES_PER_CYCLE": settings.learning_min_trades_per_cycle,
        "LEARNING_TARGET_TRADES_PER_CYCLE": settings.learning_target_trades_per_cycle,
        "LEARNING_PRIORITIZE_FAST_SETTLEMENT": settings.learning_prioritize_fast_settlement,
        "LEARNING_MAX_DAYS_TO_SETTLEMENT": settings.learning_max_days_to_settlement,
        "LEARNING_ALLOWED_CATEGORIES": sorted(learning_categories(settings)),
        "LEARNING_BLOCK_DEMO_EXECUTION": settings.learning_block_demo_execution,
        "LEARNING_BLOCK_LIVE_EXECUTION": settings.learning_block_live_execution,
        "LEARNING_INCLUDE_WATCHLIST": settings.learning_include_watchlist,
        "LEARNING_MIN_LIQUIDITY": decimal_to_str(settings.learning_min_liquidity),
        "LEARNING_MAX_SPREAD": decimal_to_str(settings.learning_max_spread),
        "LEARNING_DUPLICATE_COOLDOWN_HOURS": settings.learning_duplicate_cooldown_hours,
        "LEARNING_CANDIDATE_SCAN_LIMIT": settings.learning_candidate_scan_limit,
        "MODEL_CONFIDENCE_MIN_SETTLED_TRADES": (
            settings.model_confidence_min_settled_trades
        ),
        "MODEL_CONFIDENCE_EXPLORATION_WEIGHT": decimal_to_str(
            settings.model_confidence_exploration_weight
        ),
    }
