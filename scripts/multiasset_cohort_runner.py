"""Fixed GET-only entrypoint for a separately registered prospective cohort."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from kalshi_predictor.crypto.multiasset_capture import BASE, at, capture, digest, encode, persist
from kalshi_predictor.crypto.multiasset_outcomes import collect, verify_capture


def trusted(path: Path) -> bytes:
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ValueError("ROOT_OWNED_UNWRITABLE_CONTROL_REQUIRED")
    if not path.is_file() or not 0 < path.stat().st_size <= 4000000:
        raise ValueError("BOUNDED_CONTROL_REQUIRED")
    return path.read_bytes()


def registered(control: Path, slot: int):
    if control.is_symlink() or control.stat().st_uid != 0 or control.stat().st_mode & 0o022:
        raise ValueError("TRUSTED_CONTROL_DIRECTORY_REQUIRED")
    registration = json.loads(trusted(control / "registration.json"))
    if type(slot) is not int or not 0 <= slot < len(registration["slots"]):
        raise ValueError("REGISTERED_SLOT_REQUIRED")
    item = registration["slots"][slot]
    raw = trusted(control / f"slot-{slot}.json")
    if item["slot"] != slot or digest(raw) != item["plan_sha256"]:
        raise ValueError("REGISTERED_PLAN_HASH_MISMATCH")
    plan = json.loads(raw)
    if not at(registration["registered_at"]) < at(plan["not_before"]):
        raise ValueError("PROSPECTIVE_REGISTRATION_REQUIRED")
    for name, expected in registration["documents"].items():
        if Path(name).name != name or digest(trusted(control / name)) != expected:
            raise ValueError("REGISTERED_DOCUMENT_CHANGED")
    return plan, item, registration


def allowed_url(url: str, plan: dict, mode: str) -> bool:
    if mode not in {"capture", "outcome"}:
        return False
    exact = {
        BASE + "/markets?event_ticker=" + plan["event"] + "&limit=1000",
        BASE + "/cfbenchmarks/values?id=" + plan["benchmark"],
    }
    if mode == "capture" and url in exact:
        return True
    parts = urlsplit(url)
    suffix = "/orderbook" if mode == "capture" else ""
    expression = (
        r"/trade-api/v2/markets/" + re.escape(plan["event"]) + r"-[A-Z0-9.-]{1,80}" + suffix
    )
    return (
        parts.scheme == "https"
        and parts.netloc == "external-api.kalshi.com"
        and re.fullmatch(expression, parts.path) is not None
        and not parts.fragment
        and parts.query == ("depth=10" if mode == "capture" else "")
    )


def transport_for(plan: dict, mode: str):
    import httpx
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key_id = os.environ["KALSHI_API_KEY_ID"]
    key = serialization.load_pem_private_key(
        Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]).read_bytes(), password=None
    )
    if not key_id or not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("REGISTERED_RSA_SIGNING_CONFIGURATION_REQUIRED")

    def get(url, timeout):
        if not allowed_url(url, plan, mode) or not 0 < timeout <= 12:
            raise ValueError("FIXED_GET_SURFACE_REQUIRED")
        stamp = str(int(time.time() * 1000))
        sig = key.sign(
            (stamp + "GET" + urlsplit(url).path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        headers = {
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }
        expires = time.monotonic() + timeout
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", url, headers=headers) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 6000000 or time.monotonic() > expires:
                        raise ValueError("BOUNDED_RESPONSE_LIMIT")
                    raw.extend(chunk)
                if key_id.encode() in raw or headers["KALSHI-ACCESS-SIGNATURE"].encode() in raw:
                    raise ValueError("CREDENTIAL_ECHO_REFUSED")
                return response.status_code, bytes(raw)

    return get


def pin_capture(control: Path, slot: int, plan: dict, item: dict):
    if os.geteuid() != 0:
        raise ValueError("ROOT_PIN_PROCESS_REQUIRED")
    root = Path(item["capture_path"])
    complete = (root / "completion.json").read_bytes()
    current = datetime.now(UTC)
    pin = encode(
        {
            "event": plan["event"],
            "target_at": plan["target_at"],
            "at": current.isoformat(),
            "protocol_sha256": item["plan_sha256"],
            "completion_sha256": digest(complete),
        }
    )
    provisional_receipt = encode({"pin_sha256": digest(pin), "at": current.isoformat()})
    verify_capture(root, pin, provisional_receipt, item["plan_sha256"])
    persist(control / f"slot-{slot}.pin.json", pin)
    receipt = encode({"pin_sha256": digest(pin), "at": datetime.now(UTC).isoformat()})
    persist(control / f"slot-{slot}.pin-receipt.json", receipt)
    verify_capture(root, pin, receipt, item["plan_sha256"])
    if datetime.now(UTC) >= at(plan["target_at"]):
        raise ValueError("POST_FSYNC_PIN_DEADLINE")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("capture", "pin", "outcome"))
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--slot", type=int, required=True)
    args = parser.parse_args()
    plan, item, registration = registered(args.control, args.slot)
    root = Path(item["capture_path"])
    if root != Path(registration["data_root"]) / f"slot-{args.slot}":
        raise ValueError("REGISTERED_OUTPUT_PATH_REQUIRED")
    if args.mode == "pin":
        if (args.control / f"slot-{args.slot}.pin.json").exists() or (
            args.control / f"slot-{args.slot}.pin-receipt.json"
        ).exists():
            raise FileExistsError("TERMINAL_PIN_ALREADY_EXISTS")
        try:
            pin_capture(args.control, args.slot, plan, item)
        except Exception:
            marker = args.control / f"slot-{args.slot}.pin-failure.json"
            if not marker.exists():
                persist(marker, encode({"at": datetime.now(UTC).isoformat(), "status": "FAILED"}))
            raise
    elif args.mode == "capture":
        capture(root, plan, transport_for(plan, args.mode))
    else:
        if (args.control / f"slot-{args.slot}.pin-failure.json").exists():
            raise ValueError("ROOT_PIN_FAILED")
        collect(
            root.with_name(root.name + "-outcome"),
            root,
            trusted(args.control / f"slot-{args.slot}.pin.json"),
            trusted(args.control / f"slot-{args.slot}.pin-receipt.json"),
            item["plan_sha256"],
            transport_for(plan, args.mode),
        )


if __name__ == "__main__":
    main()
