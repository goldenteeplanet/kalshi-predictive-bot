"""One configured SQLite file only; no singleton, authentication, launch or release."""

import hashlib
import json
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PAGE_PHASE_BYTES = 512 * 1024 * 1024
MAX_BYTES = (1 << 63) - 1
STATES = frozenset(
    {"RESERVED", "START_INTENT", "RUNNING", "COMPLETED_CHARGED", "FAILED_CHARGED", "AMBIGUOUS"}
)
CONFIG_FIELDS = {
    "authority", "filesystem", "source_sha256", "margin_bytes", "max_rows",
    "max_epochs", "max_database_bytes", "max_age_seconds",
}
REQUEST_FIELDS = {
    "reservation", "run", "epoch", "epoch_metadata_sha256", "logical_work_sha256",
    "source_manifest_sha256", "input_sha256", "expected_generation", "pages", "phases",
    "available_bytes", "observed_at", "decision_at", "filesystem",
}


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def integer(value, maximum=MAX_BYTES):
    need(type(value) is int and 0 <= value <= maximum, "INTEGER_BOUND")
    return value


def identity(value):
    need(
        type(value) is str and 0 < len(value) <= 80
        and value.isascii() and all(c.isalnum() or c in "-_" for c in value),
        "IDENTITY",
    )


def pin(value):
    need(
        type(value) is str and len(value) == 64
        and all(c in "0123456789abcdef" for c in value),
        "SHA256",
    )


def parse(raw):
    need(type(raw) is bytes and 0 < len(raw) <= 8192, "INPUT_BOUND")

    def unique(items):
        result = {}
        for key, value in items:
            need(key not in result, "DUPLICATE_JSON")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    need(type(value) is dict, "OBJECT")
    return value


def config(raw, expected):
    pin(expected)
    need(digest(raw) == expected, "CONFIG_PIN")
    value = parse(raw)
    need(set(value) == CONFIG_FIELDS, "CONFIG_FIELDS")
    for field in ("authority", "filesystem"):
        identity(value[field])
    pin(value["source_sha256"])
    source = Path(__file__).read_bytes()
    need(len(source) <= 32768 and digest(source) == value["source_sha256"], "SOURCE_PIN")
    integer(value["margin_bytes"])
    for field, maximum in (("max_rows", 64), ("max_epochs", 32), ("max_age_seconds", 60)):
        need(integer(value[field], maximum) > 0, "POSITIVE_BOUND")
    size = integer(value["max_database_bytes"], 1048576)
    need(size >= 65536 and size % 4096 == 0, "DATABASE_BOUND")
    return value


def checked_path(path):
    path = Path(path).absolute()
    need(not any(p.is_symlink() for p in (path, *path.parents)), "SYMLINK")
    return path


def make_receipt(policy, config_sha256, request, request_raw, generation, charged, amount):
    return encode(dict(
        schema="CROSS_EPOCH_ACCOUNTING_V1", authority=policy["authority"],
        filesystem=policy["filesystem"], config_sha256=config_sha256,
        request_sha256=digest(request_raw), logical_work_sha256=request["logical_work_sha256"],
        epoch=request["epoch"], reservation=request["reservation"], previous_generation=generation,
        generation=generation + 1, charged_before_bytes=charged, requested_bytes=amount,
        state="RESERVED", launch_authorized=False, retry_authorized=False,
        singleton_proven=False, input_authenticated=False, restore_safe=False,
    ))


def create(path, *, config_raw, config_sha256):
    policy = config(config_raw, config_sha256)
    path = checked_path(path)
    with path.open("xb"):
        pass
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA page_size=4096")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.execute(f"PRAGMA max_page_count={policy['max_database_bytes'] // 4096}")
        db.executescript(
            "BEGIN IMMEDIATE;"
            "CREATE TABLE config(raw BLOB NOT NULL,pin TEXT NOT NULL);"
            "CREATE TABLE head(generation INTEGER NOT NULL);"
            "CREATE TABLE epochs(id TEXT PRIMARY KEY,metadata TEXT NOT NULL);"
            "CREATE TABLE reservations(id TEXT PRIMARY KEY,work TEXT UNIQUE NOT NULL,"
            "epoch TEXT NOT NULL,request BLOB NOT NULL,receipt BLOB NOT NULL,"
            "bytes INTEGER NOT NULL,state TEXT NOT NULL);"
        )
        db.execute("INSERT INTO config VALUES(?,?)", (config_raw, config_sha256))
        db.execute("INSERT INTO head VALUES(0)")
        db.commit()
    finally:
        db.close()


@contextmanager
def connection(path, config_raw, config_sha256, write):
    policy = config(config_raw, config_sha256)
    path = checked_path(path)
    info = path.stat()
    need(stat.S_ISREG(info.st_mode), "DATABASE_FILE")
    need(info.st_size <= policy["max_database_bytes"], "DATABASE_BOUND")
    mode = "rw" if write else "ro"
    db = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=1,
                         isolation_level=None)
    started = time.monotonic()
    db.set_progress_handler(lambda: int(time.monotonic() - started > 3), 100)
    try:
        need(db.execute("PRAGMA page_size").fetchone()[0] == 4096, "PAGE_SIZE")
        need(db.execute("PRAGMA journal_mode").fetchone()[0] == "delete", "JOURNAL_MODE")
        if write:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA max_page_count={policy['max_database_bytes'] // 4096}")
        else:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        need(db.execute("SELECT raw,pin FROM config LIMIT 2").fetchall()
             == [(config_raw, config_sha256)], "CONFIG_IDENTITY")
        heads = db.execute("SELECT generation FROM head LIMIT 2").fetchall()
        need(len(heads) == 1, "HEAD")
        generation = integer(heads[0][0], policy["max_rows"])
        epochs = db.execute("SELECT id,metadata FROM epochs LIMIT 33").fetchall()
        rows = db.execute(
            "SELECT id,work,epoch,bytes,state,request,receipt FROM reservations LIMIT 65"
        ).fetchall()
        need(len(epochs) <= policy["max_epochs"], "EPOCH_CAP")
        need(len(rows) == generation <= policy["max_rows"], "ROW_HEAD")
        for epoch, metadata in epochs:
            identity(epoch)
            pin(metadata)
        known = dict(epochs)
        records = []
        for reservation, work, epoch, amount, state, original, receipt in rows:
            identity(reservation)
            pin(work)
            need(epoch in known and state in STATES, "UNKNOWN_ROW_CONTEXT")
            need(integer(amount) > 0, "POSITIVE_CHARGE")
            request, derived = validate_request(original)
            need(type(receipt) is bytes and len(receipt) <= 8192, "RECEIPT_BOUND")
            need(
                reservation == request["reservation"] and work == request["logical_work_sha256"]
                and epoch == request["epoch"] and known[epoch] == request["epoch_metadata_sha256"]
                and policy["filesystem"] == request["filesystem"] and amount == derived,
                "STORED_REQUEST_CONFLICT",
            )
            records.append((request["expected_generation"], request, original, receipt, amount))
        records.sort(key=lambda record: record[0])
        total = 0
        for index, (prior, request, original, receipt, amount) in enumerate(records):
            need(prior == index, "STORED_GENERATION")
            age = (utc(request["decision_at"]) - utc(request["observed_at"])).total_seconds()
            need(0 <= age <= policy["max_age_seconds"], "STORED_OBSERVATION")
            need(request["available_bytes"] - total - amount >= policy["margin_bytes"],
                 "STORED_CAPACITY")
            need(receipt == make_receipt(policy, config_sha256, request, original,
                                         index, total, amount), "STORED_RECEIPT_CONFLICT")
            total = integer(total + amount)
        yield db, policy, generation, known, total, started
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def utc(value):
    need(type(value) is str and len(value) <= 40, "TIME_BOUND")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(result.tzinfo is not None and result.utcoffset().total_seconds() == 0, "UTC")
    return result


def validate_request(request_raw):
    request = parse(request_raw)
    need(set(request) == REQUEST_FIELDS, "REQUEST_FIELDS")
    for field in ("reservation", "run", "epoch", "filesystem"):
        identity(request[field])
    for field in ("epoch_metadata_sha256", "logical_work_sha256",
                  "source_manifest_sha256", "input_sha256"):
        pin(request[field])
    integer(request["expected_generation"], 64)
    pages = integer(request["pages"], 120)
    phases = integer(request["phases"], 2)
    need(pages > 0 and phases > 0, "FULL_PHASE")
    amount = integer(pages * phases * PAGE_PHASE_BYTES)
    integer(request["available_bytes"])
    utc(request["observed_at"])
    utc(request["decision_at"])
    return request, amount


def reserve(path, *, config_raw, config_sha256, request_raw, _checkpoint=None):
    request, amount = validate_request(request_raw)
    available = request["available_bytes"]
    observed, decision = utc(request["observed_at"]), utc(request["decision_at"])
    with connection(path, config_raw, config_sha256, True) as state:
        db, policy, generation, epochs, charged, started = state
        need(request["filesystem"] == policy["filesystem"], "FILESYSTEM")
        need(0 <= (decision - observed).total_seconds() <= policy["max_age_seconds"], "STALE")
        need(generation == request["expected_generation"], "GENERATION")
        need(generation < policy["max_rows"], "ROW_CAP")
        need(not db.execute("SELECT 1 FROM reservations WHERE id=? OR work=?",
                            (request["reservation"], request["logical_work_sha256"])).fetchone(),
             "IDENTITY_CONSUMED")
        epoch = request["epoch"]
        metadata = request["epoch_metadata_sha256"]
        if epoch in epochs:
            need(epochs[epoch] == metadata, "EPOCH_METADATA_CONFLICT")
        else:
            need(len(epochs) < policy["max_epochs"], "EPOCH_CAP")
        need(available - charged - amount >= policy["margin_bytes"], "CAPACITY")
        receipt = make_receipt(policy, config_sha256, request, request_raw,
                               generation, charged, amount)
        need(len(receipt) <= 8192, "RECEIPT_BOUND")
        if epoch not in epochs:
            db.execute("INSERT INTO epochs VALUES(?,?)", (epoch, metadata))
        db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?)",
                   (request["reservation"], request["logical_work_sha256"], epoch,
                    request_raw, receipt, amount, "RESERVED"))
        need(db.execute("UPDATE head SET generation=? WHERE generation=?",
                        (generation + 1, generation)).rowcount == 1, "GENERATION_CAS")
        if _checkpoint:
            _checkpoint("before_commit")
        need(time.monotonic() - started <= 3, "COOPERATIVE_DEADLINE")
        db.commit()
        if _checkpoint:
            _checkpoint("after_commit_before_ack")
        return receipt


def reconcile(path, *, config_raw, config_sha256, logical_work_sha256):
    pin(logical_work_sha256)
    with connection(path, config_raw, config_sha256, False) as state:
        db, _, generation, _, charged, _ = state
        row = db.execute("SELECT receipt FROM reservations WHERE work=?",
                         (logical_work_sha256,)).fetchone()
        if row:
            need(type(row[0]) is bytes and len(row[0]) <= 8192, "RECEIPT_BOUND")
        return dict(status="EXISTING" if row else "ABSENT_NOT_RETRY_AUTHORITY",
                    receipt=row[0] if row else None, generation=generation,
                    charged_bytes=charged, launch_authorized=False, retry_authorized=False)
