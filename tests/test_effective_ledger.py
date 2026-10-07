import tempfile
import unittest
from pathlib import Path

from src.domain import (
    CommissionBlocked,
    ConcurrentEditConflict,
    Actor,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")

    def tearDown(self):
        self.tmp.cleanup()

    def _change(self, description="Reactor interlock change"):
        unit = self.service.create(
            self.admin, "unit", {"name": "R-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": description}
        )
        self.service.transition(self.admin, change["id"], "assess", {"risk_level": "medium", "analyst": "E-1"})
        self.service.transition(self.admin, change["id"], "approve", {"approvals": ["S-1", "S-2"], "permit_id": "P-1"})
        self.service.transition(self.admin, change["id"], "implement", {"procedure_version": "v2"})
        return change

    def _loop(self, change_id, tag="PT-101", **overrides):
        payload = {
            "tag": tag,
            "range_min": 0.0,
            "range_max": 100.0,
            "setpoint": 50.0,
            "alarm_limit": 60.0,
            "alarm_direction": "high",
            "hysteresis": 2.0,
            "change_id": change_id,
            "setpoint_version": 1,
            "instrument_version": 1,
        }
        payload.update(overrides)
        return self.service.create(self.admin, "loop", payload)

    def _pass_test(self, loop_id, evidence="loop-check-ok"):
        return self.service.record_test(
            self.admin, loop_id, {"result": "passed", "tested_by": "T-1", "evidence": evidence}
        )


class CommissionRecomputeTest(LedgerTestBase):
    def test_commission_passes_and_signs_each_loop(self):
        change = self._change()
        loop = self._loop(change["id"])
        self._pass_test(loop["id"])

        batch = self.service.commission(self.admin, change["id"])
        self.assertEqual(batch["status"], "completed")
        signoffs = self.repo.list_signoffs(batch["id"])
        self.assertEqual([s["loop_id"] for s in signoffs], [loop["id"]])

    def test_range_exceeded_blocks_and_names_loop(self):
        change = self._change()
        loop = self._loop(change["id"], tag="PT-202", setpoint=120.0)
        self._pass_test(loop["id"])

        with self.assertRaises(CommissionBlocked) as ctx:
            self.service.commission(self.admin, change["id"])
        self.assertEqual(ctx.exception.batch["status"], "blocked")
        codes = [f["code"] for f in ctx.exception.failures]
        self.assertIn("range_exceeded", codes)
        self.assertEqual(ctx.exception.failures[0]["tag"], "PT-202")

    def test_hysteresis_direction_blocks(self):
        change = self._change()
        loop = self._loop(change["id"], tag="PT-303", hysteresis=0.0)
        self._pass_test(loop["id"])

        with self.assertRaises(CommissionBlocked) as ctx:
            self.service.commission(self.admin, change["id"])
        codes = [f["code"] for f in ctx.exception.failures]
        self.assertIn("hysteresis_direction", codes)
        self.assertEqual(ctx.exception.failures[0]["tag"], "PT-303")

    def test_missing_test_blocks(self):
        change = self._change()
        self._loop(change["id"], tag="PT-404")

        with self.assertRaises(CommissionBlocked) as ctx:
            self.service.commission(self.admin, change["id"])
        codes = [f["code"] for f in ctx.exception.failures]
        self.assertIn("test_missing", codes)

    def test_change_commission_gated_on_loops(self):
        change = self._change()
        loop = self._loop(change["id"], tag="PT-505", setpoint=200.0)  # out of range
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, change["id"], "commission", {"tests_passed": True})
        # fix the range and add a valid test -> commission now passes
        self.service.update_loop(self.admin, loop["id"], {"setpoint": 50.0}, expected_version=loop["version"])
        self._pass_test(loop["id"])
        updated = self.service.transition(self.admin, change["id"], "commission", {"tests_passed": True})
        self.assertEqual(updated["status"], "commissioned")


class InvalidationTest(LedgerTestBase):
    def test_setpoint_change_invalidates_test_and_opens_review(self):
        change = self._change()
        loop = self._loop(change["id"])
        test = self._pass_test(loop["id"])

        updated = self.service.update_loop(
            self.admin, loop["id"], {"setpoint": 55.0}, expected_version=loop["version"]
        )
        self.assertEqual(updated["data"]["setpoint_version"], 2)

        # old test is now invalid
        stale = self.service.get(test["id"])
        self.assertEqual(stale["data"]["status"], "invalid")

        # a pending review was raised
        reviews = self.service.list("review_item", status="open")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["data"]["loop_id"], loop["id"])
        self.assertEqual(reviews[0]["data"]["reason"], "setpoint_changed")

        # commissioning is blocked until retested
        with self.assertRaises(CommissionBlocked):
            self.service.commission(self.admin, change["id"])

        # retest against the new version resolves the review and passes
        self._pass_test(loop["id"], evidence="retest-ok")
        reviews = self.service.list("review_item", status="open")
        self.assertEqual(len(reviews), 0)
        batch = self.service.commission(self.admin, change["id"])
        self.assertEqual(batch["status"], "completed")

    def test_instrument_change_invalidates_and_reviews(self):
        change = self._change()
        loop = self._loop(change["id"])
        self._pass_test(loop["id"])

        updated = self.service.update_loop(
            self.admin, loop["id"], {"instrument_tag": "PT-101-B"}, expected_version=loop["version"]
        )
        self.assertEqual(updated["data"]["instrument_version"], 2)
        reviews = self.service.list("review_item", status="open")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["data"]["reason"], "instrument_changed")

    def test_failed_test_keeps_review_open(self):
        change = self._change()
        loop = self._loop(change["id"])
        self.service.update_loop(
            self.admin, loop["id"], {"setpoint": 60.0}, expected_version=loop["version"]
        )
        self.service.record_test(
            self.admin, loop["id"], {"result": "failed", "tested_by": "T-1", "evidence": "fail-1"}
        )
        reviews = self.service.list("review_item", status="open")
        self.assertEqual(len(reviews), 1)
        # a failed test does not satisfy commissioning
        with self.assertRaises(CommissionBlocked):
            self.service.commission(self.admin, change["id"])


class ConcurrencyTest(LedgerTestBase):
    def test_losing_engineer_gets_current_values_and_conflicts(self):
        change = self._change()
        loop = self._loop(change["id"])

        # engineer A commits first
        self.service.update_loop(
            self.engineer, loop["id"], {"setpoint": 55.0}, expected_version=loop["version"]
        )

        # engineer B works from the stale version
        with self.assertRaises(ConcurrentEditConflict) as ctx:
            self.service.update_loop(
                self.engineer, loop["id"], {"setpoint": 60.0}, expected_version=loop["version"]
            )
        exc = ctx.exception
        self.assertEqual(exc.current["version"], 2)
        self.assertEqual(exc.current["data"]["setpoint"], 55.0)
        self.assertIn("setpoint", exc.conflicts)


class RetryResumeTest(LedgerTestBase):
    def test_write_failure_keeps_unfinished_loops_and_retry_skips_signed(self):
        change = self._change()
        loop1 = self._loop(change["id"], tag="PT-101")
        loop2 = self._loop(change["id"], tag="PT-102")
        self._pass_test(loop1["id"])
        self._pass_test(loop2["id"])

        # fail on the second sign-off
        def fail_on_second(batch_id, loop):
            fail_on_second.calls += 1
            if fail_on_second.calls == 2:
                raise RuntimeError("disk full")

        fail_on_second.calls = 0
        self.service.fault = fail_on_second

        with self.assertRaises(RuntimeError):
            self.service.commission(self.admin, change["id"])

        batch = self.service.list("commission_batch")[0]
        self.assertEqual(batch["status"], "in_progress")
        signed = self.repo.list_signoffs(batch["id"])
        self.assertEqual([s["loop_id"] for s in signed], [loop1["id"]])

        # retry: loop1 must not be re-signed, loop2 gets signed
        self.service.fault = None
        batch = self.service.retry_commission(self.admin, batch["id"])
        self.assertEqual(batch["status"], "completed")
        signed = self.repo.list_signoffs(batch["id"])
        self.assertEqual(len(signed), 2)
        # no duplicate sign-offs for loop1
        self.assertEqual([s["loop_id"] for s in signed].count(loop1["id"]), 1)


class EffectiveLedgerTest(LedgerTestBase):
    def test_effective_view_shows_version_source_and_open_items(self):
        change = self._change()
        loop = self._loop(change["id"])
        self._pass_test(loop["id"])

        view = self.service.effective(change_id=change["id"])
        self.assertEqual(len(view), 1)
        entry = view[0]
        self.assertEqual(entry["loop"]["id"], loop["id"])
        self.assertEqual(entry["effective"]["setpoint"], 50.0)
        self.assertEqual(entry["effective"]["setpoint_version"], 1)
        self.assertEqual(entry["effective"]["instrument_version"], 1)
        self.assertEqual(entry["source"]["change_id"], change["id"])
        self.assertIn("Reactor interlock change", entry["source"]["description"])
        self.assertTrue(entry["ready"])
        self.assertEqual(entry["open_reviews"], [])

        # after a setpoint change, the view shows the open review and not-ready
        self.service.update_loop(
            self.admin, loop["id"], {"setpoint": 55.0}, expected_version=loop["version"]
        )
        entry = self.service.effective(change_id=change["id"])[0]
        self.assertEqual(entry["effective"]["setpoint_version"], 2)
        self.assertFalse(entry["ready"])
        self.assertEqual(len(entry["open_reviews"]), 1)


if __name__ == "__main__":
    unittest.main()
