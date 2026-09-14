"""Bounded KNYC observation research, never forecast or settlement certification.

API shapes verified 2026-09-08 against official /services/latest, /time-series
and /metadata at docs.synopticdata.com. Token injection occurs below HTTPX's
request logging boundary; exported URLs never contain the credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

_BASE = "https://api.synopticdata.com/v2/stations/"
_SERVICES = frozenset({"metadata", "latest", "timeseries"})
_MAX_BYTES = 1_048_576


class SynopticError(RuntimeError):
    """Fixed public code; no provider error, request or response is retained."""


class _SecretFilter(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.__token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.__token in message:
            record.msg = message.replace(self.__token, "[REDACTED]")
            record.args = ()
        if record.exc_info and self.__token in str(record.exc_info[1]):
            record.exc_info = None
            record.exc_text = "SYNOPTIC_TRANSPORT_ERROR_REDACTED"
        return True


@dataclass(frozen=True)
class SynopticOriginal:
    url: str
    received_at: datetime
    sha256: str
    payload: bytes = field(repr=False)
    provenance_role: str = "THIRD_PARTY_OBSERVATION_RESEARCH_ONLY"


@dataclass(frozen=True)
class AirTemperatureObservation:
    sensor_id: str
    observed_at: datetime
    value_c: Decimal | None
    unit: str
    qc: Any


@dataclass(frozen=True)
class SynopticResult:
    station: dict[str, Any] | None
    observations: tuple[AirTemperatureObservation, ...]
    original: SynopticOriginal
    status: str
    qc_summary: dict[str, Any] | None


class _TokenTransport(httpx.BaseTransport):
    """HTTPX sees a clean URL; only the underlying transport sees the token."""

    def __init__(self, token: str, transport: httpx.BaseTransport):
        self.__token = token
        self.__transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        clean = request.url
        if str(clean).split("?")[0] not in {_BASE + service for service in _SERVICES}:
            raise SynopticError("SYNOPTIC_ENDPOINT_NOT_ALLOWED")
        outbound = httpx.Request(
            "GET",
            clean.copy_add_param("token", self.__token),
            headers=request.headers,
            extensions=request.extensions,
        )
        try:
            return self.__transport.handle_request(outbound)
        finally:
            # Exceptions and MockTransport responses may reference this request.
            outbound.url = clean

    def close(self) -> None:
        self.__transport.close()


def _utc(value: Any) -> datetime:
    try:
        at = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError
        return at.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise SynopticError("SYNOPTIC_UTC_TIMESTAMP_REQUIRED") from None


def _temperature(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | Decimal | str):
        raise SynopticError("SYNOPTIC_TEMPERATURE_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise SynopticError("SYNOPTIC_TEMPERATURE_INVALID") from None
    if not number.is_finite():
        raise SynopticError("SYNOPTIC_TEMPERATURE_INVALID")
    return number


def _invalid_constant(_: str) -> None:
    raise SynopticError("SYNOPTIC_JSON_INVALID")


class SynopticResearchClient:
    """One station, air_temp only; no retries, files, ledger or trading imports."""

    def __init__(
        self, token: str, *, request_budget: int = 3, transport: httpx.MockTransport | None = None
    ):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise SynopticError("SYNOPTIC_TOKEN_FORMAT_INVALID")
        if type(request_budget) is not int or not 1 <= request_budget <= 10:
            raise SynopticError("SYNOPTIC_BUDGET_INVALID")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise SynopticError("SYNOPTIC_TEST_TRANSPORT_INVALID")
        self.__token = token
        self.__remaining = request_budget
        self.__lock = threading.Lock()
        self.__client = httpx.Client(
            transport=_TokenTransport(token, transport or httpx.HTTPTransport(retries=0)),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10),
        )
        self.__filter = _SecretFilter(token)
        names = {
            "httpx",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
            "httpcore.socks",
            "httpcore.connection_pool",
        }
        names.update(
            name
            for name in logging.Logger.manager.loggerDict
            if name.startswith(("httpx", "httpcore"))
        )
        self.__loggers = tuple(logging.getLogger(name) for name in names)
        for logger in self.__loggers:
            logger.addFilter(self.__filter)

    def __enter__(self) -> SynopticResearchClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.__client.close()
        for logger in self.__loggers:
            logger.removeFilter(self.__filter)

    @property
    def remaining_requests(self) -> int:
        return self.__remaining

    def _get(self, service: str, params: dict[str, str | int]) -> SynopticResult:
        if service not in _SERVICES or "token" in params:
            raise SynopticError("SYNOPTIC_ENDPOINT_NOT_ALLOWED")
        if any(self.__token in str(value) for value in params.values()):
            raise SynopticError("SYNOPTIC_CREDENTIAL_IN_ARGUMENT_REJECTED")
        params = dict(params, stid="KNYC", vars="air_temp", output="json", sensorvars=1)
        clean_url = httpx.URL(_BASE + service, params=params)
        with self.__lock:
            if self.__remaining <= 0:
                raise SynopticError("SYNOPTIC_LOCAL_QUOTA_EXHAUSTED")
            self.__remaining -= 1
            started = time.monotonic()
            try:
                with self.__client.stream(
                    "GET",
                    clean_url,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        code = {
                            401: "AUTHENTICATION_FAILED",
                            403: "ACCESS_DENIED",
                            429: "RATE_LIMITED",
                        }.get(response.status_code, "HTTP_FAILURE")
                        if 300 <= response.status_code < 400:
                            code = "REDIRECT_REJECTED"
                        raise SynopticError("SYNOPTIC_" + code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise SynopticError("SYNOPTIC_ENCODING_REJECTED")
                    if (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                        != "application/json"
                    ):
                        raise SynopticError("SYNOPTIC_CONTENT_TYPE_INVALID")
                    chunks = bytearray()
                    for chunk in response.iter_bytes(chunk_size=16_384):
                        if len(chunks) + len(chunk) > _MAX_BYTES:
                            raise SynopticError("SYNOPTIC_RESPONSE_TOO_LARGE")
                        if time.monotonic() - started > 30:
                            raise SynopticError("SYNOPTIC_RESPONSE_DEADLINE")
                        chunks.extend(chunk)
                    raw = bytes(chunks)
                    if self.__token.encode() in raw:
                        raise SynopticError("SYNOPTIC_CREDENTIAL_ECHO_REJECTED")
                    try:
                        body = json.loads(
                            raw, parse_float=Decimal, parse_constant=_invalid_constant
                        )
                    except (ValueError, UnicodeError):
                        raise SynopticError("SYNOPTIC_JSON_INVALID") from None
                    if self.__token in json.dumps(body, default=str):
                        raise SynopticError("SYNOPTIC_CREDENTIAL_ECHO_REJECTED")
                    original = SynopticOriginal(
                        str(clean_url), datetime.now(UTC), hashlib.sha256(raw).hexdigest(), raw
                    )
                    try:
                        return self._parse(body, service, original, params)
                    except (ValueError, TypeError, AttributeError, KeyError):
                        raise SynopticError("SYNOPTIC_SCHEMA_INVALID") from None
            except httpx.HTTPError:
                raise SynopticError("SYNOPTIC_TRANSPORT_FAILED") from None

    def metadata(self) -> SynopticResult:
        return self._get("metadata", {"complete": 1})

    def latest(self, *, within_minutes: int = 120) -> SynopticResult:
        if type(within_minutes) is not int or not 1 <= within_minutes <= 120:
            raise SynopticError("SYNOPTIC_WINDOW_INVALID")
        return self._get("latest", dict(self._observation_params(), within=within_minutes))

    def timeseries(
        self,
        *,
        recent_minutes: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> SynopticResult:
        params = self._observation_params()
        if start is None and end is None:
            recent = 120 if recent_minutes is None else recent_minutes
            if type(recent) is not int or not 1 <= recent <= 120:
                raise SynopticError("SYNOPTIC_WINDOW_INVALID")
            params["recent"] = recent
        elif start is not None and end is not None and recent_minutes is None:
            first, last = _utc(start), _utc(end)
            if not timedelta(0) < last - first <= timedelta(hours=6) or last > datetime.now(UTC):
                raise SynopticError("SYNOPTIC_WINDOW_INVALID")
            if any(at.second or at.microsecond for at in (first, last)):
                raise SynopticError("SYNOPTIC_MINUTE_BOUNDARY_REQUIRED")
            params.update(start=first.strftime("%Y%m%d%H%M"), end=last.strftime("%Y%m%d%H%M"))
        else:
            raise SynopticError("SYNOPTIC_WINDOW_INVALID")
        return self._get("timeseries", params)

    @staticmethod
    def _observation_params() -> dict[str, str | int]:
        return dict(obtimezone="UTC", units="temp|C", qc="on", qc_flags="on", qc_remove_data="off")

    @staticmethod
    def _parse(
        body: Any, service: str, original: SynopticOriginal, params: dict[str, str | int]
    ) -> SynopticResult:
        if not isinstance(body, dict) or not isinstance(body.get("SUMMARY"), dict):
            raise SynopticError("SYNOPTIC_SCHEMA_INVALID")
        summary = body["SUMMARY"]
        code = summary.get("RESPONSE_CODE")
        if type(code) is not int or code not in {1, 2}:
            raise SynopticError(
                "SYNOPTIC_AUTHENTICATION_FAILED" if code == 200 else "SYNOPTIC_PROVIDER_REJECTED"
            )
        stations = body.get("STATION", [])
        if (
            not isinstance(stations, list)
            or len(stations) > 1
            or summary.get("NUMBER_OF_OBJECTS") != len(stations)
        ):
            raise SynopticError("SYNOPTIC_STATION_COUNT_INVALID")
        if not stations:
            return SynopticResult(None, (), original, "NO_OBSERVATIONS", body.get("QC_SUMMARY"))
        station = stations[0]
        if not isinstance(station, dict) or station.get("STID") != "KNYC":
            raise SynopticError("SYNOPTIC_STATION_IDENTITY_INVALID")
        if service == "metadata":
            return SynopticResult(station, (), original, "METADATA_ONLY", body.get("QC_SUMMARY"))
        unit = body.get("UNITS", {}).get("air_temp")
        if unit not in {"Celsius", "C"}:
            raise SynopticError("SYNOPTIC_TEMPERATURE_UNIT_INVALID")
        observed = station.get("OBSERVATIONS", {})
        if not isinstance(observed, dict):
            raise SynopticError("SYNOPTIC_SCHEMA_INVALID")
        rows = []
        for sensor, values in observed.items():
            if sensor == "date_time":
                continue
            if not re.fullmatch(r"air_temp_(value|set)_\d+d?", sensor):
                raise SynopticError("SYNOPTIC_SENSOR_ID_INVALID")
            if service == "latest":
                if not isinstance(values, dict):
                    raise SynopticError("SYNOPTIC_SCHEMA_INVALID")
                samples = [(values.get("date_time"), values.get("value"), values.get("qc"))]
            else:
                clocks = observed.get("date_time")
                if (
                    not isinstance(clocks, list)
                    or not isinstance(values, list)
                    or len(clocks) != len(values)
                    or len(clocks) > 720
                ):
                    raise SynopticError("SYNOPTIC_TIME_SERIES_ALIGNMENT_INVALID")
                qc_values = station.get("QC", {}).get(sensor)
                if qc_values is not None and (
                    not isinstance(qc_values, list) or len(qc_values) != len(clocks)
                ):
                    raise SynopticError("SYNOPTIC_QC_ALIGNMENT_INVALID")
                samples = [
                    (at, value, None if qc_values is None else qc_values[i])
                    for i, (at, value) in enumerate(zip(clocks, values, strict=True))
                ]
            prior = None
            for clock, value, qc in samples:
                at = _utc(clock)
                if at > original.received_at or (prior is not None and at <= prior):
                    raise SynopticError("SYNOPTIC_OBSERVATION_CLOCK_INVALID")
                prior = at
                if "start" in params:
                    first = datetime.strptime(str(params["start"]), "%Y%m%d%H%M").replace(
                        tzinfo=UTC
                    )
                    last = datetime.strptime(str(params["end"]), "%Y%m%d%H%M").replace(tzinfo=UTC)
                    if not first <= at <= last:
                        raise SynopticError("SYNOPTIC_OBSERVATION_OUTSIDE_WINDOW")
                rows.append(AirTemperatureObservation(sensor, at, _temperature(value), unit, qc))
        if len(rows) > 1440:
            raise SynopticError("SYNOPTIC_OBSERVATION_COUNT_INVALID")
        return SynopticResult(
            station,
            tuple(rows),
            original,
            "OBSERVATIONS_RESEARCH_ONLY" if rows else "NO_OBSERVATIONS",
            body.get("QC_SUMMARY"),
        )
