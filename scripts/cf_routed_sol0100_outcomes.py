"""One separately registered 01:00 UTC routed SOL event; two GETs, no model reruns."""

from __future__ import annotations

import argparse

# Existing public transport is reused unchanged; its exact source is registered.
import importlib.util
import json
import os
from datetime import timedelta
from pathlib import Path

from kalshi_predictor.crypto import research_shadow as S
from kalshi_predictor.crypto import research_shadow_evaluation as E
from kalshi_predictor.crypto.settlement_target import _json
from kalshi_predictor.ui import research_journals as UI
from kalshi_predictor.ui import routed_single_event as U

TRANSPORT_PATH = Path(__file__).resolve().with_name("cf_average_shadow_outcomes.py")
_SPEC = importlib.util.spec_from_file_location("_fixed_public_outcome_transport", TRANSPORT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_O = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_O)
public_get = _O.public_get
MAX_BODY = 3_000_000


def sources() -> dict[str, bytes]:
    root = Path(S.__file__).resolve().parents[3]
    values = {
        "src/kalshi_predictor/" + name.replace(".", "/") + ".py": S.original(item)
        for name, item in S.source_originals().items()
    }
    from kalshi_predictor.forecasting.crypto_average_shadow_route import route_sources

    values.update(
        {
            "src/kalshi_predictor/forecasting/" + name + ".py": S.original(item)
            for name, item in route_sources().items()
        }
    )
    for relative, module in [
        ("src/kalshi_predictor/crypto/research_shadow_evaluation.py", E),
        ("src/kalshi_predictor/ui/routed_single_event.py", U),
        ("src/kalshi_predictor/ui/research_journals.py", UI),
        ("scripts/cf_average_shadow_outcomes.py", _O),
    ]:
        path = (root / relative).resolve()
        if module.__file__ is None or Path(module.__file__).resolve() != path:
            raise ValueError("EXACT_EXECUTED_SOURCE_ORIGIN_REQUIRED")
        values[relative] = UI.read(path, 1_000_000)
    own = Path(__file__).resolve()
    if own != root / "scripts/cf_routed_sol0100_outcomes.py":
        raise ValueError("EXACT_COLLECTOR_SOURCE_ORIGIN_REQUIRED")
    values["scripts/cf_routed_sol0100_outcomes.py"] = UI.read(own, 1_000_000)
    return values


def preflight(capture: Path, journal: Path, control: Path, current) -> tuple[str, dict[str, bytes]]:
    _, plan, method = U.registration(control, now=current)
    if capture.name != "slot-0" or journal.resolve() != (capture / "research.db").resolve():
        raise ValueError("FIXED_SINGLE_EVENT_PATH_REQUIRED")
    code = sources()
    if method["source_sha256"] != {k: S.sha(v) for k, v in code.items()}:
        raise ValueError("REGISTERED_OUTCOME_SOURCE_MISMATCH")
    pin = UI.decode(UI.read(control / "slot-0.completion-pin.json"))
    completion = UI.read(capture / "completion.json")
    terminal = UI.decode(completion)
    if (
        pin["schema"] != "cf-cohort-external-completion-pin-v1"
        or type(pin["slot"]) is not int
        or pin["slot"] != 0
        or pin["protocol_sha256"] != S.sha(UI.read(control / "slot-0.protocol.json"))
        or pin["completion_sha256"] != S.sha(completion)
        or S.at(pin["target_at"]) != U.TARGET
        or pin["event_ticker"] != U.EVENT
        or Path(pin["capture_path"]).resolve() != capture.resolve()
        or pin["execution_authority"] is not False
        or terminal["status"] != "COMPLETE"
        or not U.START
        <= S.at(terminal["recorded_after_result"])
        <= S.at(pin["observed_at"])
        < U.END
        or UI.read(capture / "plan.original.json") != UI.read(control / "slot-0.protocol.json")
    ):
        raise ValueError("EXACT_ROOT_CAPTURE_PIN_REQUIRED")
    return pin["completion_sha256"], code


def _collect(
    journal: Path,
    capture: Path,
    output: Path,
    *,
    control: Path,
    transport=public_get,
    clock=S.now,
) -> dict:
    completion_sha256, registered_sources = preflight(capture, journal, control, clock())
    files = E.load_capture(capture)
    dependencies = E.evaluation_dependencies(files)
    plan = _json(files["plan.original.json"])
    target = S.at(plan["target_at"])
    start, end = target + timedelta(minutes=5), target + timedelta(minutes=6)
    if (
        target != U.TARGET
        or plan["event_ticker"] != U.EVENT
        or plan["schema"] != "cf-average-prospective-slot-v2"
    ):
        raise ValueError("EXACT_NEW_ROUTED_EVENT_REQUIRED")
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
    if any(Path(path).read_bytes() != raw for path, raw in dependencies.items()):
        raise ValueError("VALIDATOR_CHANGED_DURING_PREFLIGHT")
    selected = sorted({row["ticker"] for row in validation["rows"]})
    if len(selected) != 2:
        raise ValueError("EXACT_TWO_SELECTED_CONTRACTS_REQUIRED")
    if output.resolve() != capture.resolve().with_name(capture.name + "-official-outcome"):
        raise ValueError("FIXED_PER_CAPTURE_OUTCOME_PATH_REQUIRED")
    originals = {
        str(Path(__file__).resolve()): Path(__file__).read_bytes(),
        str(Path(E.__file__).resolve()): Path(E.__file__).read_bytes(),
    }
    originals.update(dependencies)
    code_root = Path(S.__file__).resolve().parents[3]
    originals.update({str(code_root / k): v for k, v in registered_sources.items()})
    control_originals = {
        str(control / name): UI.read(control / name)
        for name in (
            "registration.json",
            "registration.receipt.json",
            "outcome-source-pins.json",
            "slot-0.protocol.json",
            "slot-0.completion-pin.json",
        )
    }
    originals.update(control_originals)
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
    save(
        "registered-sources.originals.json",
        S.encode({k: {"hex": v.hex(), "sha256": S.sha(v)} for k, v in registered_sources.items()}),
    )
    save("collector.original.py", Path(__file__).read_bytes())
    save("evaluator.original.py", Path(E.__file__).read_bytes())
    for raw in dependencies.values():
        save("route-validator.original.py", raw)
    for path, raw in control_originals.items():
        save("control-" + Path(path).name, raw)
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

    def verify_publication():
        if (
            any(Path(p).read_bytes() != raw for p, raw in originals.items())
            or E.load_capture(capture) != files
            or (output / "failure.json").exists()
        ):
            raise ValueError("PUBLICATION_SOURCE_OR_CAPTURE_CHANGED")

    verify_publication()
    result = dict(
        status="SCORED" if scored else "PENDING_OR_UNAVAILABLE_NO_RETRY",
        target_at=target.isoformat(),
        states=states,
        reason=reason,
        execution_authority=False,
        paper_pnl=None,
    )
    save("result.json", S.encode(result))
    completed = checked()
    if not start <= completed < end:
        raise ValueError("OUTCOME_COMPLETION_DEADLINE")
    save(
        "completion.json",
        S.encode(
            dict(
                status="COMPLETE",
                recorded_after_result=completed.isoformat(),
                result_sha256=S.sha(S.encode(result)),
            )
        ),
    )
    verify_publication()
    if not completed <= checked() < end:
        raise ValueError("OUTCOME_COMPLETION_DEADLINE")
    return result


def collect(journal, capture, output, *, control, transport=public_get, clock=S.now):
    if output.exists():
        raise FileExistsError("EXCLUSIVE_OUTCOME_ALREADY_EXISTS")
    try:
        return _collect(journal, capture, output, control=control, transport=transport, clock=clock)
    except Exception as exc:
        if output.exists() and not (output / "failure.json").exists():
            with (output / "failure.json").open("xb") as stream:
                stream.write(
                    S.encode(
                        dict(status="FAILED", reason=str(exc), recorded_at=clock().isoformat())
                    )
                )
                stream.flush()
                os.fsync(stream.fileno())
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", type=Path, required=True)
    p.add_argument("--control", type=Path, required=True)
    a = p.parse_args()
    result = collect(
        a.capture / "research.db",
        a.capture,
        a.capture.with_name(a.capture.name + "-official-outcome"),
        control=a.control,
    )
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
