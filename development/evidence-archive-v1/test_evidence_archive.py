"""New fixed-name retained cloud fixtures, at most twelve files and one owned child."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import evidence_archive as A


def sync_parent(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArchiveTests(unittest.TestCase):
    def create(self, suffix="", **policy):
        root = Path(os.environ["EVIDENCE_TEST_ROOT"])
        self.assertTrue(root.is_dir())
        path = root / (self._testMethodName + suffix + ".archive")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        sync_parent(root)
        self.addCleanup(os.close, fd)
        return path, fd, A.Archive(fd, reservation="fixture", **policy)

    def committed(self, **policy):
        path, fd, writer = self.create(**policy)
        writer.append("INPUT", "original", b"retained original")
        writer.commit_inputs()
        return path, fd, writer

    def test_exact_fit_and_next_frame_overcap(self):
        _, fd, writer = self.committed(limit=9999, log_limit=0)
        measured = len(A.read_fd(fd))
        exact = measured - len(str(9999)) + len(str(measured))
        # Header limit's digit count is the only length change; hashes stay64chars.
        exact = measured - len(str(9999)) + len(str(exact))
        _, second, bounded = self.create("-exact", limit=exact, log_limit=0)
        bounded.append("INPUT", "original", b"retained original")
        bounded.commit_inputs()
        self.assertEqual(len(A.read_fd(second)), exact)
        with self.assertRaisesRegex(ValueError, "LOGICAL_BYTE_BOUND"):
            bounded.terminal("WORK_COMPLETE")
        self.assertEqual(A.recover(second, reservation="fixture")["status"], "INCOMPLETE_CHARGED")
        self.assertEqual(len(A.read_fd(fd)), measured)

    def test_terminal_requires_input_commit_and_returns_no_authority(self):
        _, fd, writer = self.create()
        with self.assertRaisesRegex(ValueError, "TERMINAL_BINDING"):
            writer.terminal("WORK_COMPLETE")
        writer.append("INPUT", "original", b"x")
        writer.commit_inputs()
        writer.terminal("WORK_COMPLETE")
        result = A.recover(fd, reservation="fixture")
        self.assertEqual(result["terminal"], "WORK_COMPLETE")
        self.assertFalse(result["launch_authorized"])
        self.assertFalse(result["refund_authorized"])
        self.assertEqual(result["reservation_bytes"], 536870912)

    def test_shared_stdout_stderr_budget(self):
        _, fd, writer = self.committed(log_limit=8)
        writer.append("STDOUT", "out", b"12345")
        writer.append("STDERR", "err", b"678")
        before = A.read_fd(fd)
        with self.assertRaisesRegex(ValueError, "SHARED_LOG_BOUND"):
            writer.append("STDERR", "extra", b"9")
        self.assertEqual(A.read_fd(fd), before)

    def test_torn_terminal_recovers_committed_inputs(self):
        _, fd, writer = self.committed()
        writer.terminal("WORK_COMPLETE")
        os.ftruncate(fd, os.fstat(fd).st_size - 12)
        result = A.recover(fd, reservation="fixture")
        self.assertEqual(result["status"], "INCOMPLETE_CHARGED")
        self.assertIsNone(result["terminal"])
        self.assertEqual(result["inputs"][0]["data"], b"retained original")

    def test_corrupt_complete_frame_and_uncommitted_refuse(self):
        _, fd, writer = self.create()
        writer.append("INPUT", "original", b"x")
        with self.assertRaisesRegex(ValueError, "NO_COMMITTED_INPUTS"):
            A.recover(fd, reservation="fixture")
        writer.commit_inputs()
        raw = A.read_fd(fd)
        offset = raw.index(b"eA==")
        os.pwrite(fd, b"eQ==", offset)
        with self.assertRaisesRegex(ValueError, "DATA_HASH"):
            A.recover(fd, reservation="fixture")

    def test_exclusive_root_creation_and_fd_offset_independence(self):
        path, fd, writer = self.committed()
        with self.assertRaises(FileExistsError):
            os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
        writer.terminal("WORK_FAILED")
        os.lseek(fd, 3, os.SEEK_SET)
        self.assertEqual(A.recover(fd, reservation="fixture")["terminal"], "WORK_FAILED")

    def test_partial_write_exception_consumes_writer(self):
        _, fd, writer = self.committed()
        original = os.write

        def broken(target, raw):
            original(target, raw[:8])
            raise OSError("injected write failure")

        with patch.object(A.os, "write", broken), self.assertRaises(OSError):
            writer.terminal("WORK_COMPLETE")
        with self.assertRaisesRegex(ValueError, "WRITER_CONSUMED_OR_FORKED"):
            writer.terminal("WORK_COMPLETE")
        result = A.recover(fd, reservation="fixture")
        self.assertTrue(result["incomplete_tail"])
        self.assertEqual(result["status"], "INCOMPLETE_CHARGED")

    def test_actual_owned_child_exit_after_input_fsync(self):
        root = Path(os.environ["EVIDENCE_TEST_ROOT"])
        path = root / (self._testMethodName + ".archive")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        sync_parent(root)
        self.addCleanup(os.close, fd)
        child = os.fork()
        if child == 0:
            try:
                writer = A.Archive(fd, reservation="fixture")
                writer.append("INPUT", "child-original", b"before abrupt exit")
                writer.commit_inputs()
                os._exit(73)
            except BaseException:
                os._exit(74)
        until = time.monotonic() + 3
        reaped = False
        try:
            while time.monotonic() < until:
                pid, status = os.waitpid(child, os.WNOHANG)
                if pid:
                    reaped = True
                    self.assertEqual(os.waitstatus_to_exitcode(status), 73)
                    break
                time.sleep(0.01)
            self.assertTrue(reaped, "child exceeded bounded wait")
        finally:
            if not reaped:
                os.kill(child, 9)
                os.waitpid(child, 0)
        result = A.recover(fd, reservation="fixture")
        self.assertEqual(result["status"], "INCOMPLETE_CHARGED")
        self.assertEqual(result["inputs"][0]["data"], b"before abrupt exit")
        self.assertFalse(result["retry_authorized"])

    def test_identity_frames_and_payload_caps(self):
        _, fd, writer = self.create(frames=2)
        writer.append("INPUT", "once", b"x")
        with self.assertRaisesRegex(ValueError, "INPUT_PHASE_OR_DUPLICATE"):
            writer.append("INPUT", "once", b"y")
        writer.commit_inputs()
        with self.assertRaisesRegex(ValueError, "FRAME_COUNT"):
            writer.terminal("WORK_COMPLETE")
        with self.assertRaisesRegex(ValueError, "PAYLOAD_BOUND"):
            writer.append("STDOUT", "too-big", b"x" * (A.MAX_BYTES // 2 + 1))
        with self.assertRaisesRegex(ValueError, "RESERVATION_BINDING"):
            A.recover(fd, reservation="other")

    def test_commit_bool_float_count_rejected(self):
        _, fd, _ = self.committed()
        lines = A.read_fd(fd).splitlines()
        for invalid in (True, 1.0):
            frame = json.loads(lines[-1])
            frame["body"]["records"] = invalid
            altered = b"\n".join(lines[:-1] + [A.encode(frame)]) + b"\n"
            os.ftruncate(fd, 0)
            os.pwrite(fd, altered, 0)
            with self.assertRaisesRegex(ValueError, "INTEGER_BOUND"):
                A.recover(fd, reservation="fixture")


if __name__ == "__main__":
    unittest.main(verbosity=2)
