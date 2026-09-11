"""One bounded two-GET attempt for each frozen SOL cohort; no retries or model reruns."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.crypto import research_shadow_evaluation as E
from kalshi_predictor.crypto.settlement_target import _json

TARGETS = tuple(f"2026-09-11T{h}:00:00+00:00" for h in (20, 21, 22, 23)) + (
    "2026-09-12T00:00:00+00:00",
)
MAX_BODY = 3_000_000


def public_get(url: str, timeout: float) -> tuple[int, bytes]:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout

    def body(response):
        chunks = []
        size = 0
        while size <= MAX_BODY:
            if response.fp is None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("PUBLIC_READ_DEADLINE")
            response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_BODY + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("PUBLIC_READ_DEADLINE")
        return b"".join(chunks)

    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, body(response)
    except HTTPError as exc:
        try:
            return exc.code, body(exc.fp)
        finally:
            exc.close()


def collect(
    journal: Path,
    capture: Path,
    output: Path,
    *,
    completion_sha256: str,
    transport=public_get,
    clock=S.now,
) -> dict:
    files = E.load_capture(capture)
    plan = _json(files["plan.original.json"])
    target = S.at(plan["target_at"])
    start, end = target + timedelta(minutes=5), target + timedelta(minutes=6)
    if target.isoformat() not in TARGETS or plan["symbol"] != "SOL":
        raise ValueError("EXACT_FIVE_SOL_TARGETS_REQUIRED")
    current = clock()
    previous = current

    def checked(value=None):
        nonlocal previous
        value = clock() if value is None else value
        if value < previous:
            raise ValueError("OUTCOME_CLOCK_ROLLBACK")
        previous = value
        return value

    if not start <= current <= end - timedelta(seconds=10):
        raise ValueError("OUTCOME_WINDOW_NOT_OPEN")
    validation = E.validate_capture(
        journal, files, completion_sha256=completion_sha256, as_of=current
    )
    selected = sorted({row["ticker"] for row in validation["rows"]})
    if len(selected) != 2:
        raise ValueError("EXACT_TWO_SELECTED_CONTRACTS_REQUIRED")
    if output.resolve() != capture.resolve().with_name(capture.name + "-official-outcome"):
        raise ValueError("FIXED_PER_CAPTURE_OUTCOME_PATH_REQUIRED")
    originals = {
        str(Path(__file__).resolve()): Path(__file__).read_bytes(),
        str(Path(E.__file__).resolve()): Path(E.__file__).read_bytes(),
    }
    root = Path(S.__file__).resolve().parents[1]
    for name, item in S.source_originals().items():
        originals[str(root / (name.replace(".", "/") + ".py"))] = bytes.fromhex(item["hex"])
    output.mkdir(parents=False, exist_ok=False)  # Exclusive attempt reservation, never overwritten.

    def save(name, raw):
        with (output / name).open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())

    reserved = checked()
    save(
        "reservation.json",
        S.encode(
            dict(
                target_at=target.isoformat(),
                selected=selected,
                reserved_at=reserved.isoformat(),
                window_start=start.isoformat(),
                window_end=end.isoformat(),
                max_gets=2,
                retries=0,
                completion_sha256=completion_sha256,
                sources={name: S.sha(raw) for name, raw in originals.items()},
            )
        ),
    )
    save("collector.original.py", Path(__file__).read_bytes())
    save("evaluator.original.py", Path(E.__file__).read_bytes())
    official = {}
    states = []
    for i, ticker in enumerate(selected):
        requested = checked()
        if not reserved <= requested or not start <= requested <= end - timedelta(seconds=10):
            states.append(dict(ticker=ticker, status="NO_REQUEST_WINDOW_EXPIRED"))
            continue
        if E.load_capture(capture) != files or any(
            Path(p).read_bytes() != raw for p, raw in originals.items()
        ):
            raise ValueError("FROZEN_INPUT_OR_CODE_CHANGED")
        if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in ticker):
            raise ValueError("EXACT_PUBLIC_TICKER_REQUIRED")
        url = f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}"
        save(
            f"{i}.request.json",
            S.encode(
                dict(method="GET", url=url, requested_at=requested.isoformat(), timeout_seconds=10)
            ),
        )
        # Recheck after durable reservation and immediately before the only request.
        requested = checked()
        if not start <= requested <= end - timedelta(seconds=10):
            states.append(dict(ticker=ticker, status="NO_REQUEST_WINDOW_EXPIRED"))
            continue
        status = None
        raw = b""
        error = None
        try:
            status, raw = transport(url, 10)
        except Exception as exc:
            error = type(exc).__name__
        received = clock()
        complete = error is None and type(raw) is bytes and len(raw) <= MAX_BODY
        if type(raw) is not bytes:
            raw = b""
        raw = raw[:MAX_BODY]
        save(f"{i}.original.json", raw)
        receipt = S.encode(
            dict(
                method="GET",
                url=url,
                http_status=status,
                source_sha256=S.sha(raw),
                original_complete=complete,
                requested_at=requested.isoformat(),
                received_at=received.isoformat(),
                error=error,
            )
        )
        save(f"{i}.receipt.json", receipt)
        checked(received)
        lifecycle = "UNAVAILABLE"
        if complete and status == 200 and requested <= received <= end:
            try:
                lifecycle = _json(raw)["market"]["status"]
                official[ticker] = (raw, receipt)
            except (ValueError, KeyError, TypeError):
                pass
        states.append(dict(ticker=ticker, status=lifecycle, original_sha256=S.sha(raw)))
    if (
        any(Path(p).read_bytes() != raw for p, raw in originals.items())
        or E.load_capture(capture) != files
    ):
        raise ValueError("SOURCE_CHANGED_BEFORE_EVALUATION")
    scored = False
    reason: str | None = "INCOMPLETE_OR_NONFINAL_OFFICIAL_RESULTS"
    if len(official) == 2:
        try:
            E.write_evaluation(
                output / "evaluation",
                journal,
                files,
                completion_sha256=completion_sha256,
                official=official,
            )
            scored = True
            reason = None
        except ValueError as exc:
            reason = str(exc)
    result = dict(
        status="SCORED" if scored else "PENDING_OR_UNAVAILABLE_NO_RETRY",
        target_at=target.isoformat(),
        states=states,
        reason=reason,
        execution_authority=False,
        paper_pnl=None,
    )
    save("result.json", S.encode(result))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--completion-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(
        json.dumps(
            collect(a.journal, a.capture, a.output, completion_sha256=a.completion_sha256),
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
