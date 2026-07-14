import html
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import EconomicEvent
from kalshi_predictor.economic.discovery import run_phase3bd_economic_market_discovery
from kalshi_predictor.economic.features import build_economic_features
from kalshi_predictor.economic.repository import insert_economic_event
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.opportunities.reports import generate_opportunities_report
from kalshi_predictor.utils.time import utc_now

OFFICIAL_BLS_CPI_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
OFFICIAL_BLS_JOBS_URL = "https://www.bls.gov/schedule/news_release/empsit.htm"
OFFICIAL_BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule/full"
OFFICIAL_FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
EASTERN = ZoneInfo("America/New_York")
HTTP_USER_AGENT = "Mozilla/5.0 DejoiaEconomicCalendar/1.0"


@dataclass(frozen=True)
class EconomicCalendarEvent:
    event_key: str
    source: str
    source_url: str
    event_time: datetime
    category: str
    title: str
    reference_period: str | None = None
    actual_value: str | None = None
    forecast_value: str | None = None
    previous_value: str | None = None
    raw_json: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EconomicCalendarFetchResult:
    source: str
    url: str
    attempted: bool
    succeeded: bool
    events: list[EconomicCalendarEvent]
    error: str | None = None


@dataclass(frozen=True)
class Phase3BDR2Artifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, Any]


CalendarFetcher = Callable[[], list[EconomicCalendarFetchResult]]


def fetch_official_economic_calendar() -> list[EconomicCalendarFetchResult]:
    with httpx.Client(
        timeout=httpx.Timeout(30),
        follow_redirects=True,
        headers={"User-Agent": HTTP_USER_AGENT},
    ) as client:
        return [
            _fetch_source(
                client,
                source="bls_cpi_schedule",
                url=OFFICIAL_BLS_CPI_URL,
                parser=lambda text: parse_bls_release_schedule(
                    text,
                    source="bls_cpi_schedule",
                    source_url=OFFICIAL_BLS_CPI_URL,
                    event_key="cpi",
                    category="cpi",
                    release_title="Consumer Price Index",
                ),
            ),
            _fetch_source(
                client,
                source="bls_employment_situation_schedule",
                url=OFFICIAL_BLS_JOBS_URL,
                parser=lambda text: parse_bls_release_schedule(
                    text,
                    source="bls_employment_situation_schedule",
                    source_url=OFFICIAL_BLS_JOBS_URL,
                    event_key="jobs",
                    category="jobs",
                    release_title="Employment Situation",
                ),
            ),
            _fetch_source(
                client,
                source="bea_release_schedule",
                url=OFFICIAL_BEA_SCHEDULE_URL,
                parser=lambda text: parse_bea_release_schedule(
                    text,
                    source_url=OFFICIAL_BEA_SCHEDULE_URL,
                ),
            ),
            _fetch_source(
                client,
                source="federal_reserve_fomc_calendar",
                url=OFFICIAL_FED_FOMC_URL,
                parser=lambda text: parse_fed_fomc_calendar(
                    text,
                    source_url=OFFICIAL_FED_FOMC_URL,
                ),
            ),
        ]


def parse_bls_release_schedule(
    html_text: str,
    *,
    source: str,
    source_url: str,
    event_key: str,
    category: str,
    release_title: str,
) -> list[EconomicCalendarEvent]:
    events: list[EconomicCalendarEvent] = []
    for reference_period, release_date, release_time in _release_table_rows(html_text):
        event_time = _parse_eastern_datetime(release_date, release_time)
        if event_time is None:
            continue
        title = f"{release_title} for {reference_period}"
        events.append(
            EconomicCalendarEvent(
                event_key=event_key,
                source=source,
                source_url=source_url,
                event_time=event_time,
                category=category,
                title=title,
                reference_period=reference_period,
                raw_json={
                    "provider": "BLS",
                    "release_date": release_date,
                    "release_time": release_time,
                    "reference_period": reference_period,
                    "source_url": source_url,
                },
            )
        )
    return events


def parse_bea_release_schedule(
    html_text: str,
    *,
    source_url: str,
) -> list[EconomicCalendarEvent]:
    events: list[EconomicCalendarEvent] = []
    for row_html in re.findall(
        r"<tr\b[^>]*>(.*?)</tr>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title_match = re.search(
            r'<td[^>]*class="[^"]*release-title[^"]*"[^>]*>(.*?)</td>',
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        date_match = re.search(
            r'<div[^>]*class="release-date"[^>]*>(.*?)</div>',
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        time_match = re.search(
            r"<small[^>]*>(.*?)</small>",
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not title_match or not date_match or not time_match:
            continue
        title = _strip_html(title_match.group(1))
        event_key = _bea_event_key(title)
        if event_key is None:
            continue
        release_date = _strip_html(date_match.group(1))
        release_time = _strip_html(time_match.group(1))
        event_time = _parse_eastern_datetime(f"{release_date}, {utc_now().year}", release_time)
        if event_time is None:
            continue
        events.append(
            EconomicCalendarEvent(
                event_key=event_key,
                source="bea_release_schedule",
                source_url=source_url,
                event_time=event_time,
                category=event_key,
                title=title,
                raw_json={
                    "provider": "BEA",
                    "release_date": release_date,
                    "release_time": release_time,
                    "source_url": source_url,
                },
            )
        )
    return events


def parse_fed_fomc_calendar(
    html_text: str,
    *,
    source_url: str,
) -> list[EconomicCalendarEvent]:
    events: list[EconomicCalendarEvent] = []
    year_match = re.search(r">(20\d{2}) FOMC Meetings<", html_text)
    year = int(year_match.group(1)) if year_match else utc_now().year
    section = html_text
    if year_match:
        next_panel = re.search(
            r'<div class="panel panel-default"><div class="panel-heading"><h4><a id=',
            html_text[year_match.end() :],
            flags=re.IGNORECASE,
        )
        section_end = (
            year_match.end() + next_panel.start()
            if next_panel is not None
            else len(html_text)
        )
        section = html_text[year_match.start() : section_end]
    pattern = (
        r'fomc-meeting__month[^>]*>\s*<strong>(.*?)</strong>.*?'
        r'fomc-meeting__date[^>]*>(.*?)</div>'
    )
    for month_html, date_html in re.findall(pattern, section, flags=re.IGNORECASE | re.DOTALL):
        month = _strip_html(month_html)
        date_text = _strip_html(date_html).replace("*", "")
        day = _last_day_in_range(date_text)
        event_time = _parse_eastern_datetime(f"{month} {day}, {year}", "2:00 PM")
        if event_time is None:
            continue
        events.append(
            EconomicCalendarEvent(
                event_key="fed",
                source="federal_reserve_fomc_calendar",
                source_url=source_url,
                event_time=event_time,
                category="fed",
                title=f"FOMC rate decision for {month} {date_text}, {year}",
                reference_period=f"{month} {date_text}, {year}",
                raw_json={
                    "provider": "Federal Reserve",
                    "meeting_month": month,
                    "meeting_dates": date_text,
                    "assumed_decision_time_et": "2:00 PM",
                    "source_url": source_url,
                },
            )
        )
    return events


def select_current_calendar_events(
    events: list[EconomicCalendarEvent],
    *,
    now: datetime | None = None,
    days_ahead: int = 180,
    lookback_days: int = 45,
) -> list[EconomicCalendarEvent]:
    now = now or utc_now()
    lower = now - timedelta(days=lookback_days)
    upper = now + timedelta(days=days_ahead)
    selected: list[EconomicCalendarEvent] = []
    for event_key in ("cpi", "jobs", "fed", "gdp"):
        scoped = [
            event
            for event in events
            if event.event_key == event_key and lower <= event.event_time <= upper
        ]
        upcoming = sorted(
            (event for event in scoped if event.event_time >= now),
            key=lambda event: event.event_time,
        )
        if upcoming:
            selected.append(upcoming[0])
            continue
        past = sorted(
            (event for event in scoped if event.event_time < now),
            key=lambda event: event.event_time,
            reverse=True,
        )
        if past:
            selected.append(past[0])
    return selected


def run_phase3bd_r2_economic_calendar_freshness(
    session: Session,
    *,
    calendar_fetcher: CalendarFetcher | None = None,
    kalshi_client: KalshiClient | None = None,
    opportunity_output_path: str | Path = "reports/opportunities_economic_v1.md",
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
    days_ahead: int = 180,
    lookback_days: int = 45,
) -> dict[str, Any]:
    generated_at = utc_now()
    fetch_results = (calendar_fetcher or fetch_official_economic_calendar)()
    all_events = [event for result in fetch_results for event in result.events]
    selected_events = select_current_calendar_events(
        all_events,
        now=generated_at,
        days_ahead=days_ahead,
        lookback_days=lookback_days,
    )
    inserted, skipped_existing = _insert_selected_events(session, selected_events)
    feature_summary = build_economic_features(session)
    discovery_payload = run_phase3bd_economic_market_discovery(
        session,
        client=kalshi_client,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        include_orderbooks=False,
    )
    opportunity_path, opportunity_summary = generate_opportunities_report(
        session,
        model_name="economic_v1",
        limit=opportunity_limit,
        output_path=opportunity_output_path,
    )
    payload = {
        "phase": "3BD-R2",
        "generated_at": generated_at.isoformat(),
        "mode": "PAPER_READ_ONLY_CALENDAR_FRESHNESS",
        "live_demo_execution": "blocked",
        "summary": {
            "sources_attempted": len(fetch_results),
            "sources_succeeded": sum(1 for result in fetch_results if result.succeeded),
            "events_seen": len(all_events),
            "selected_current_events": len(selected_events),
            "events_inserted": inserted,
            "events_skipped_existing": skipped_existing,
            "features_inserted": feature_summary.features_inserted,
            "calendar_only_events": sum(
                1 for event in selected_events if event.actual_value is None
            ),
            "actual_value_events": sum(
                1 for event in selected_events if event.actual_value is not None
            ),
            "economic_links": discovery_payload["summary"]["links_created"]
            + discovery_payload["summary"]["links_skipped_existing"],
            "forecasts_inserted": discovery_payload["summary"]["forecasts_inserted"],
            "opportunity_markets_scanned": opportunity_summary.markets_scanned,
            "rankings_inserted": opportunity_summary.rankings_inserted,
            "opportunities_detected": opportunity_summary.opportunities_detected,
            "top_opportunity_ticker": opportunity_summary.top_opportunity_ticker,
            "top_opportunity_score": str(opportunity_summary.top_opportunity_score or "n/a"),
            "status": _status(
                fetch_results,
                selected_events,
                opportunity_summary.opportunities_detected,
            ),
        },
        "sources": [_fetch_result_payload(result) for result in fetch_results],
        "selected_events": [_event_payload(event) for event in selected_events],
        "phase3bd_discovery_summary": discovery_payload["summary"],
        "opportunity_report": str(opportunity_path),
        "recommended_next_action": _recommended_next_action(
            selected_events=selected_events,
            opportunities_detected=opportunity_summary.opportunities_detected,
        ),
    }
    return payload


def write_phase3bd_r2_economic_calendar_freshness_report(
    *,
    session: Session,
    output_dir: Path,
    calendar_fetcher: CalendarFetcher | None = None,
    kalshi_client: KalshiClient | None = None,
    opportunity_output_path: str | Path = "reports/opportunities_economic_v1.md",
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
    days_ahead: int = 180,
    lookback_days: int = 45,
) -> Phase3BDR2Artifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = run_phase3bd_r2_economic_calendar_freshness(
        session,
        calendar_fetcher=calendar_fetcher,
        kalshi_client=kalshi_client,
        opportunity_output_path=opportunity_output_path,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
        days_ahead=days_ahead,
        lookback_days=lookback_days,
    )
    json_path = output_dir / "phase3bd_r2_economic_calendar_freshness.json"
    markdown_path = output_dir / "phase3bd_r2_economic_calendar_freshness.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return Phase3BDR2Artifacts(json_path=json_path, markdown_path=markdown_path, payload=payload)


def _fetch_source(
    client: httpx.Client,
    *,
    source: str,
    url: str,
    parser: Callable[[str], list[EconomicCalendarEvent]],
) -> EconomicCalendarFetchResult:
    try:
        response = client.get(url)
        response.raise_for_status()
        events = parser(response.text)
        return EconomicCalendarFetchResult(
            source=source,
            url=url,
            attempted=True,
            succeeded=True,
            events=events,
        )
    except Exception as exc:
        return EconomicCalendarFetchResult(
            source=source,
            url=url,
            attempted=True,
            succeeded=False,
            events=[],
            error=str(exc),
        )


def _insert_selected_events(
    session: Session,
    events: list[EconomicCalendarEvent],
) -> tuple[int, int]:
    inserted = 0
    skipped_existing = 0
    for event in events:
        exists = session.scalar(
            select(func.count())
            .select_from(EconomicEvent)
            .where(EconomicEvent.event_key == event.event_key)
            .where(EconomicEvent.source == event.source)
            .where(EconomicEvent.event_time == event.event_time)
            .where(EconomicEvent.title == event.title)
        )
        if exists:
            skipped_existing += 1
            continue
        insert_economic_event(
            session,
            event_key=event.event_key,
            source=event.source,
            event_time=event.event_time,
            category=event.category,
            title=event.title,
            actual_value=event.actual_value,
            forecast_value=event.forecast_value,
            previous_value=event.previous_value,
            raw_json=dict(event.raw_json or {}),
        )
        inserted += 1
    return inserted, skipped_existing


def _release_table_rows(html_text: str) -> list[tuple[str, str, str]]:
    rows = []
    for row_match in re.finditer(
        r"<tr[^>]*>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        rows.append(tuple(_strip_html(value) for value in row_match.groups()))
    return rows


def _parse_eastern_datetime(date_text: str, time_text: str) -> datetime | None:
    cleaned_date = re.sub(r"\s+", " ", date_text.replace(".", "")).strip()
    cleaned_time = re.sub(r"\s+", " ", time_text).strip()
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            parsed = datetime.strptime(f"{cleaned_date} {cleaned_time}", fmt)
            return parsed.replace(tzinfo=EASTERN).astimezone(ZoneInfo("UTC"))
        except ValueError:
            continue
    return None


def _bea_event_key(title: str) -> str | None:
    text = title.casefold()
    if "gross domestic product" in text or re.search(r"\bgdp\b", text):
        return "gdp"
    if "personal income and outlays" in text or "pce" in text:
        return "cpi"
    return None


def _last_day_in_range(date_text: str) -> int:
    numbers = [int(value) for value in re.findall(r"\d+", date_text)]
    return numbers[-1] if numbers else 1


def _strip_html(value: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(stripped)).strip()


def _event_payload(event: EconomicCalendarEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload["event_time"] = event.event_time.isoformat()
    payload["raw_json"] = dict(event.raw_json or {})
    return payload


def _fetch_result_payload(result: EconomicCalendarFetchResult) -> dict[str, Any]:
    return {
        "source": result.source,
        "url": result.url,
        "attempted": result.attempted,
        "succeeded": result.succeeded,
        "events": len(result.events),
        "error": result.error,
    }


def _status(
    fetch_results: list[EconomicCalendarFetchResult],
    selected_events: list[EconomicCalendarEvent],
    opportunities_detected: int,
) -> str:
    if not any(result.succeeded for result in fetch_results):
        return "BLOCKED_BY_SOURCE_FETCH"
    if not selected_events:
        return "WAITING_FOR_CURRENT_CALENDAR_EVENTS"
    if opportunities_detected > 0:
        return "ACTIVE_WITH_OPPORTUNITIES"
    return "ACTIVE_CALENDAR_ONLY"


def _recommended_next_action(
    *,
    selected_events: list[EconomicCalendarEvent],
    opportunities_detected: int,
) -> str:
    if opportunities_detected > 0:
        return "Review economic_v1 opportunity cards; keep paper/read-only gates in place."
    if selected_events and all(event.actual_value is None for event in selected_events):
        return (
            "Calendar freshness is repaired; next add actual/consensus value capture for released "
            "CPI/jobs/GDP/Fed events to improve signal strength."
        )
    return "Keep Phase 3BD-R2 in the safe refresh loop and rerank when new calendar data arrives."


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BD-R2 Economic Calendar Freshness",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Mode: PAPER / READ ONLY. Live/demo execution remains blocked.",
        "",
        "## Summary",
        "",
        f"- Status: {summary['status']}",
        f"- Sources succeeded: {summary['sources_succeeded']} / {summary['sources_attempted']}",
        f"- Events seen: {summary['events_seen']}",
        f"- Selected current events: {summary['selected_current_events']}",
        f"- Events inserted: {summary['events_inserted']}",
        f"- Existing events skipped: {summary['events_skipped_existing']}",
        f"- Economic forecasts inserted: {summary['forecasts_inserted']}",
        f"- Rankings inserted: {summary['rankings_inserted']}",
        f"- Opportunities detected: {summary['opportunities_detected']}",
        "",
        "## Selected Calendar Events",
        "",
        "| Key | Time | Title | Source | Values |",
        "| --- | --- | --- | --- | --- |",
    ]
    for event in payload["selected_events"]:
        value_state = "actual" if event.get("actual_value") is not None else "calendar-only"
        lines.append(
            "| "
            + " | ".join(
                [
                    event["event_key"],
                    event["event_time"],
                    event["title"].replace("|", "\\|"),
                    event["source"],
                    value_state,
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Sources",
            "",
            "| Source | Events | Status | URL |",
            "| --- | ---: | --- | --- |",
        ]
    )
    for source in payload["sources"]:
        status = "ok" if source["succeeded"] else f"error: {source['error']}"
        lines.append(f"| {source['source']} | {source['events']} | {status} | {source['url']} |")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)
