"""Cooperative single-FD logical archive bound; physical allocation is unknown."""

import base64
import hashlib
import json
import os
import stat

MAX_BYTES = 1048576
MAX_FRAMES = 64


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def number(value, maximum):
    need(type(value) is int and 0 <= value <= maximum, "INTEGER_BOUND")


def name(value):
    need(
        type(value) is str
        and 0 < len(value) <= 80
        and value.isascii()
        and all(c.isalnum() or c in "-_." for c in value),
        "IDENTITY",
    )


def parse(raw):
    def unique(items):
        result = {}
        for key, value in items:
            need(key not in result, "DUPLICATE_JSON")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    need(type(value) is dict and encode(value) == raw, "CANONICAL_JSON")
    return value


def read_fd(fd):
    before = os.fstat(fd)
    need(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_BYTES, "REGULAR_FILE_BOUND")
    raw = os.pread(fd, MAX_BYTES + 1, 0)
    after = os.fstat(fd)

    def signature(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    need(len(raw) == before.st_size and signature(before) == signature(after), "FD_CHANGED")
    return raw


def decode(raw, reservation):
    need(type(raw) is bytes and len(raw) <= MAX_BYTES, "ARCHIVE_BOUND")
    lines = raw.split(b"\n")
    need(len(lines) <= MAX_FRAMES + 3 and len(lines) >= 2, "FRAME_COUNT")
    trailing = lines.pop()
    header = parse(lines.pop(0))
    need(set(header) == {"schema", "reservation", "limit", "frames", "log_limit"}, "HEADER")
    need(
        header["schema"] == "EVIDENCE_ARCHIVE_V1" and header["reservation"] == reservation,
        "RESERVATION_BINDING",
    )
    name(reservation)
    number(header["limit"], MAX_BYTES)
    number(header["frames"], MAX_FRAMES)
    number(header["log_limit"], MAX_BYTES)
    need(0 < header["frames"] and 0 < len(raw) <= header["limit"], "LOGICAL_BYTE_BOUND")
    need(header["log_limit"] <= header["limit"], "LOG_BUDGET")
    need(len(lines) <= header["frames"], "FRAME_COUNT")
    previous = sha(encode(header))
    inputs = []
    names = set()
    committed = None
    terminal = None
    logs = 0
    for index, line in enumerate(lines, 1):
        frame = parse(line)
        need(set(frame) == {"seq", "previous", "kind", "body"}, "FRAME_FIELDS")
        need(
            type(frame["seq"]) is int
            and frame["seq"] == index
            and frame["previous"] == previous
            and terminal is None,
            "CHAIN_ORDER",
        )
        body, kind = frame["body"], frame["kind"]
        need(type(body) is dict, "BODY")
        if kind in ("INPUT", "STDOUT", "STDERR"):
            need(set(body) == {"name", "bytes", "sha256", "base64"}, "DATA_FIELDS")
            name(body["name"])
            number(body["bytes"], MAX_BYTES)
            need(type(body["base64"]) is str and len(body["base64"]) <= MAX_BYTES, "DATA_BOUND")
            data = base64.b64decode(body["base64"], validate=True)
            need(
                len(data) == body["bytes"]
                and sha(data) == body["sha256"]
                and base64.b64encode(data).decode() == body["base64"],
                "DATA_HASH",
            )
            if kind == "INPUT":
                need(committed is None and body["name"] not in names, "INPUT_PHASE_OR_DUPLICATE")
                names.add(body["name"])
                inputs.append(dict(name=body["name"], bytes=len(data), sha256=sha(data), data=data))
            else:
                need(committed is not None, "LOG_BEFORE_INPUT_COMMIT")
                logs += len(data)
                need(logs <= header["log_limit"], "SHARED_LOG_BOUND")
        elif kind == "INPUT_COMMIT":
            number(body.get("records"), MAX_FRAMES)
            manifest = [dict(name=x["name"], bytes=x["bytes"], sha256=x["sha256"]) for x in inputs]
            need(
                committed is None
                and inputs
                and body == {"manifest_sha256": sha(encode(manifest)), "records": len(inputs)},
                "INPUT_COMMIT",
            )
            committed = sha(line)
        elif kind == "TERMINAL":
            need(
                committed is not None
                and set(body) == {"input_commit", "outcome"}
                and body["input_commit"] == committed
                and body["outcome"] in ("WORK_COMPLETE", "WORK_FAILED"),
                "TERMINAL_BINDING",
            )
            terminal = body["outcome"]
        else:
            raise ValueError("FRAME_KIND")
        previous = sha(line)
    need(not trailing or terminal is None, "TAIL_AFTER_TERMINAL")
    return dict(
        header=header,
        inputs=inputs,
        input_commit=committed,
        terminal=terminal,
        log_bytes=logs,
        frames=len(lines),
        previous=previous,
        incomplete_tail=bool(trailing),
    )


class Archive:
    """Caller owns an exclusive O_RDWR regular FD; aliases/external writers are not fenced."""

    def __init__(self, fd, *, reservation, limit=MAX_BYTES, frames=MAX_FRAMES, log_limit=65536):
        name(reservation)
        number(limit, MAX_BYTES)
        number(frames, MAX_FRAMES)
        number(log_limit, MAX_BYTES)
        need(read_fd(fd) == b"", "EXCLUSIVE_EMPTY_FD_REQUIRED")
        self.fd, self.reservation, self.owner = fd, reservation, os.getpid()
        self.identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        self.failed = False
        self.raw = b""
        header = (
            encode(
                dict(
                    schema="EVIDENCE_ARCHIVE_V1",
                    reservation=reservation,
                    limit=limit,
                    frames=frames,
                    log_limit=log_limit,
                )
            )
            + b"\n"
        )
        decode(header, reservation)
        self._write(header, sync=True)

    def _write(self, suffix, *, sync):
        need(not self.failed and os.getpid() == self.owner, "WRITER_CONSUMED_OR_FORKED")
        info = os.fstat(self.fd)
        need(
            (info.st_dev, info.st_ino) == self.identity and read_fd(self.fd) == self.raw,
            "FD_OR_PREFIX_CHANGED",
        )
        self.failed = True
        os.lseek(self.fd, len(self.raw), os.SEEK_SET)
        offset = 0
        while offset < len(suffix):
            written = os.write(self.fd, suffix[offset:])
            need(written > 0, "ZERO_WRITE")
            offset += written
        if sync:
            os.fsync(self.fd)
        self.raw += suffix
        need(read_fd(self.fd) == self.raw, "WRITE_READBACK")
        self.failed = False

    def _append(self, kind, body, *, sync=False):
        state = decode(self.raw, self.reservation)
        need(not state["incomplete_tail"] and state["terminal"] is None, "ARCHIVE_CLOSED")
        frame = (
            encode(dict(seq=state["frames"] + 1, previous=state["previous"], kind=kind, body=body))
            + b"\n"
        )
        decode(self.raw + frame, self.reservation)
        self._write(frame, sync=sync)

    def append(self, kind, name_value, data):
        need(kind in ("INPUT", "STDOUT", "STDERR"), "DATA_KIND")
        name(name_value)
        need(type(data) is bytes and len(data) <= MAX_BYTES // 2, "PAYLOAD_BOUND")
        self._append(
            kind,
            dict(
                name=name_value,
                bytes=len(data),
                sha256=sha(data),
                base64=base64.b64encode(data).decode(),
            ),
        )

    def commit_inputs(self):
        state = decode(self.raw, self.reservation)
        manifest = [
            dict(name=x["name"], bytes=x["bytes"], sha256=x["sha256"]) for x in state["inputs"]
        ]
        self._append(
            "INPUT_COMMIT",
            dict(manifest_sha256=sha(encode(manifest)), records=len(manifest)),
            sync=True,
        )

    def terminal(self, outcome):
        state = decode(self.raw, self.reservation)
        self._append(
            "TERMINAL", dict(input_commit=state["input_commit"], outcome=outcome), sync=True
        )


def recover(fd, *, reservation):
    state = decode(read_fd(fd), reservation)
    need(state["input_commit"] is not None, "NO_COMMITTED_INPUTS")
    return dict(
        status="TERMINAL_RECORDED" if state["terminal"] else "INCOMPLETE_CHARGED",
        inputs=state["inputs"],
        terminal=state["terminal"],
        incomplete_tail=state["incomplete_tail"],
        input_commit_sha256=state["input_commit"],
        launch_authorized=False,
        retry_authorized=False,
        refund_authorized=False,
        reservation_bytes=512 * 1024 * 1024,
        logical_bound_only=True,
        physical_allocation_unknown=True,
    )
