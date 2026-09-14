"""New cloud-only fixtures; current working directory must be writable /output."""

import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path

import registry_accounting as R


def contender(path, config, pin, raw, queue, barrier):
    barrier.wait(timeout=5)
    try:
        R.reserve(path, config_raw=config, config_sha256=pin, request_raw=raw)
        queue.put("OK")
    except ValueError:
        queue.put("REFUSED")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "registry.db"
        self.policy = dict(
            authority="external-fixture", filesystem="fixture-domain",
            source_sha256=R.digest(Path(R.__file__).read_bytes()), margin_bytes=0,
            max_rows=8, max_epochs=8, max_database_bytes=1048576, max_age_seconds=2,
        )
        self.config = R.encode(self.policy)
        self.pin = R.digest(self.config)
        R.create(self.path, config_raw=self.config, config_sha256=self.pin)

    def request(self, **changes):
        value = dict(
            reservation="r0", run="run0", epoch="epoch0", epoch_metadata_sha256="a" * 64,
            logical_work_sha256="b" * 64, source_manifest_sha256="c" * 64,
            input_sha256="d" * 64, expected_generation=0, pages=1, phases=1,
            available_bytes=R.PAGE_PHASE_BYTES, filesystem="fixture-domain",
            observed_at="2026-09-14T15:00:00Z", decision_at="2026-09-14T15:00:01Z",
        )
        value.update(changes)
        return R.encode(value)

    def reserve(self, raw=None, **kwargs):
        return R.reserve(self.path, config_raw=self.config, config_sha256=self.pin,
                         request_raw=raw if raw is not None else self.request(), **kwargs)

    def read(self, work="b" * 64):
        return R.reconcile(self.path, config_raw=self.config, config_sha256=self.pin,
                           logical_work_sha256=work)

    def other(self, **changes):
        defaults = dict(reservation="r1", run="run1", epoch="epoch1",
                        logical_work_sha256="e" * 64, expected_generation=1)
        defaults.update(changes)
        return self.request(**defaults)

    def test_exact_fit_receipt_reopen_and_absent_no_launch(self):
        receipt = self.reserve()
        self.assertEqual(self.read()["receipt"], receipt)
        self.assertEqual(self.read()["charged_bytes"], R.PAGE_PHASE_BYTES)
        self.assertFalse(json.loads(receipt)["launch_authorized"])
        absent = self.read("f" * 64)
        self.assertEqual(absent["status"], "ABSENT_NOT_RETRY_AUTHORITY")
        self.assertFalse(absent["retry_authorized"])

    def test_two_epochs_share_full_capacity(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "CAPACITY"):
            self.reserve(self.other())
        receipt = json.loads(self.reserve(self.other(available_bytes=2 * R.PAGE_PHASE_BYTES)))
        self.assertEqual(receipt["charged_before_bytes"], R.PAGE_PHASE_BYTES)
        self.assertEqual(self.read()["generation"], 2)

    def test_all_retained_states_charged(self):
        self.reserve()
        for state in sorted(R.STATES):
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE reservations SET state=?", (state,))
            with self.assertRaisesRegex(ValueError, "CAPACITY"):
                self.reserve(self.other())
            self.assertEqual(self.read()["charged_bytes"], R.PAGE_PHASE_BYTES)
        self.assertFalse(hasattr(R, "release"))

    def test_attempt_and_epoch_label_evasion_refused(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "IDENTITY_CONSUMED"):
            self.reserve(self.other(logical_work_sha256="b" * 64))
        with self.assertRaisesRegex(ValueError, "IDENTITY_CONSUMED"):
            self.reserve(self.other(reservation="r0"))
        self.assertEqual(self.read()["generation"], 1)

    def test_epoch_metadata_immutable(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "EPOCH_METADATA_CONFLICT"):
            self.reserve(self.other(epoch="epoch0", epoch_metadata_sha256="f" * 64))
        self.assertEqual(self.read()["generation"], 1)

    def test_stale_generation_refused(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "GENERATION"):
            self.reserve(self.other(expected_generation=0))
        self.assertEqual(self.read()["generation"], 1)

    def test_stale_future_naive_and_filesystem_refused(self):
        for change in (
            {"decision_at": "2026-09-14T15:00:03Z"},
            {"observed_at": "2026-09-14T15:00:02Z"},
            {"observed_at": "2026-09-14T15:00:00"},
            {"filesystem": "wrong-domain"},
        ):
            with self.assertRaises(ValueError):
                self.reserve(self.request(**change))
        self.assertEqual(self.read()["generation"], 0)

    def test_strict_values_duplicate_json_and_input_cap(self):
        for change in ({"pages": True}, {"phases": 0}, {"pages": 121},
                       {"available_bytes": R.MAX_BYTES + 1}, {"input_sha256": "bad"}):
            with self.assertRaises(ValueError):
                self.reserve(self.request(**change))
        for raw in (b'{"x":1,"x":2}', b"x" * 8193):
            with self.assertRaises(ValueError):
                self.reserve(raw)
        self.assertEqual(self.read()["generation"], 0)

    def test_policy_pin_wrong_source_and_exclusive_creation(self):
        with self.assertRaisesRegex(ValueError, "CONFIG_PIN"):
            R.reconcile(self.path, config_raw=self.config, config_sha256="0" * 64,
                        logical_work_sha256="b" * 64)
        wrong = R.encode(dict(self.policy, source_sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "SOURCE_PIN"):
            R.create(self.path.with_name("other.db"), config_raw=wrong,
                     config_sha256=R.digest(wrong))
        with self.assertRaises(FileExistsError):
            R.create(self.path, config_raw=self.config, config_sha256=self.pin)

    def test_ack_loss_remains_charged_and_before_commit_rolls_back(self):
        def before(phase):
            if phase == "before_commit":
                raise RuntimeError("INJECTED_BEFORE")

        with self.assertRaisesRegex(RuntimeError, "INJECTED_BEFORE"):
            self.reserve(_checkpoint=before)
        self.assertEqual(self.read()["generation"], 0)

        def after(phase):
            if phase == "after_commit_before_ack":
                raise RuntimeError("INJECTED_ACK_LOSS")

        with self.assertRaisesRegex(RuntimeError, "INJECTED_ACK_LOSS"):
            self.reserve(_checkpoint=after)
        self.assertEqual(self.read()["generation"], 1)
        self.assertFalse(self.read()["retry_authorized"])
        with self.assertRaisesRegex(ValueError, "IDENTITY_CONSUMED"):
            self.reserve(self.other(logical_work_sha256="b" * 64))

    def test_database_bound_and_unknown_state_fail_closed(self):
        self.reserve()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE reservations SET state='RELEASED'")
        with self.assertRaisesRegex(ValueError, "UNKNOWN_ROW_CONTEXT"):
            self.read()
        with self.path.open("ab") as stream:
            stream.truncate(self.policy["max_database_bytes"] + 1)
        with self.assertRaisesRegex(ValueError, "DATABASE_BOUND"):
            self.read()

    def test_epoch_and_row_caps_preserve_commits(self):
        for index in range(8):
            self.reserve(self.request(
                reservation=f"r{index}", epoch=f"epoch{index}", expected_generation=index,
                logical_work_sha256=f"{index:064x}", available_bytes=9 * R.PAGE_PHASE_BYTES,
            ))
        with self.assertRaisesRegex(ValueError, "ROW_CAP"):
            self.reserve(self.request(expected_generation=8, reservation="extra",
                                      logical_work_sha256="f" * 64))
        self.assertEqual(self.read()["charged_bytes"], 8 * R.PAGE_PHASE_BYTES)

    def test_actual_two_process_contention_across_epochs(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        barrier = context.Barrier(2)
        requests = (self.request(), self.other(expected_generation=0))
        children = [
            context.Process(
                target=contender,
                args=(str(self.path), self.config, self.pin, raw, queue, barrier),
            )
            for raw in requests
        ]
        try:
            for child in children:
                child.start()
            for child in children:
                child.join(8)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(sorted(queue.get(timeout=2) for _ in children), ["OK", "REFUSED"])
            self.assertEqual(self.read()["generation"], 1)
            self.assertEqual(self.read()["charged_bytes"], R.PAGE_PHASE_BYTES)
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join(2)
            queue.close()
            queue.join_thread()

    def test_epoch_cap_independent_of_row_cap(self):
        path = self.path.with_name("epoch-cap.db")
        raw = R.encode(dict(self.policy, max_epochs=1))
        pin = R.digest(raw)
        R.create(path, config_raw=raw, config_sha256=pin)
        R.reserve(path, config_raw=raw, config_sha256=pin, request_raw=self.request())
        with self.assertRaisesRegex(ValueError, "EPOCH_CAP"):
            R.reserve(path, config_raw=raw, config_sha256=pin,
                      request_raw=self.other(available_bytes=2 * R.PAGE_PHASE_BYTES))
        result = R.reconcile(path, config_raw=raw, config_sha256=pin,
                             logical_work_sha256="b" * 64)
        self.assertEqual(result["generation"], 1)

    def test_reduced_charge_and_receipt_tampering_refuse(self):
        receipt = self.reserve()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE reservations SET bytes=1")
        with self.assertRaisesRegex(ValueError, "STORED_REQUEST_CONFLICT"):
            self.reserve(self.other())
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE reservations SET bytes=?", (R.PAGE_PHASE_BYTES,))
            changed = json.loads(receipt)
            changed["charged_before_bytes"] = 1
            db.execute("UPDATE reservations SET receipt=?", (R.encode(changed),))
        with self.assertRaisesRegex(ValueError, "STORED_RECEIPT_CONFLICT"):
            self.read()


if __name__ == "__main__":
    unittest.main(verbosity=2)
