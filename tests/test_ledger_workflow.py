import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ENG = Actor("eng-A", "engineer")
ENG_B = Actor("eng-B", "engineer")
SAFETY = Actor("safe-1", "safety")
VERIFIER = Actor("ver-1", "verifier")


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.svc = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def create_unit(self):
        return self.svc.create(ENG, "unit", {"name": "R-1", "location": "Plant-A"})

    def draft_change(self, unit, description="MOC", setpoints=None):
        change = self.svc.create(ENG, "change",
                                 {"unit_id": unit["id"], "description": description})
        if setpoints is not None:
            change = self.svc.transition(
                ENG, change["id"], "revise", {"setpoints": setpoints})
        return change

    def create_loop(self, change, tag, low, high, instrument="inst-v1"):
        return self.svc.create(ENG, "instrument_loop", {
            "tag": tag, "range_low": low, "range_high": high,
            "instrument_version": instrument, "basis_change_id": change["id"],
        })

    def implement(self, change):
        self.svc.transition(ENG, change["id"], "assess",
                            {"risk_level": "low", "analyst": "eng-A"})
        self.svc.transition(SAFETY, change["id"], "approve",
                            {"approvals": ["safe-1"], "permit_id": "P-1"})
        self.svc.transition(ENG, change["id"], "implement", {"procedure_version": "pr-1"})
        return self.svc.get(change["id"])

    def attach_setpoint(self, change, loop, direction="high", upper=80, lower=None,
                        hysteresis=2):
        entry = {"loop_id": loop["id"], "direction": direction, "hysteresis": hysteresis}
        if upper is not None:
            entry["upper_limit"] = upper
        if lower is not None:
            entry["lower_limit"] = lower
        return self.svc.transition(ENG, change["id"], "revise", {"setpoints": [entry]})

    def record_test(self, loop, change, passed=True, instrument=None):
        payload = {
            "loop_id": loop["id"], "passed": passed,
            "basis_change_id": change["id"],
            "basis_change_version": change["version"],
            "basis_instrument_version": instrument or loop["data"]["instrument_version"],
        }
        return self.svc.create(VERIFIER, "test_record", payload)

    def submit(self, change, loops, actor=ENG):
        batch = self.svc.create(actor, "activation_batch", {
            "change_id": change["id"],
            "loop_ids": [loop["id"] for loop in loops],
        })
        return self.svc.submit_batch(actor, self.svc.get(batch["id"])), batch


class LedgerWorkflowTest(LedgerCase):
    def test_loop_must_register_range_and_direction(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        with self.assertRaises(ValidationError):
            self.svc.create(ENG, "instrument_loop",
                            {"tag": "TI-1", "range_low": 0, "range_high": 100})
        with self.assertRaises(ValidationError):
            self.svc.create(ENG, "instrument_loop",
                            {"tag": "TI-1", "range_low": 100, "range_high": 0,
                             "instrument_version": "v1"})
        loop = self.create_loop(change, "TI-1", 0, 100)
        self.assertEqual(loop["status"], "in_service")
        with self.assertRaises(ValidationError):
            self.attach_setpoint(change, loop, direction="sideways", upper=80)

    def test_happy_path_activates_one_ledger_per_loop(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-1", 0, 100)
        self.attach_setpoint(change, loop, upper=80, hysteresis=2)
        change = self.implement(change)
        test = self.record_test(loop, change)
        self.assertIsNotNone(test["data"]["basis_setpoint_fingerprint"])

        result, batch = self.submit(change, [loop])
        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["activated"], [loop["id"]])
        self.assertEqual(result["blocked"], [])
        active = self.svc.ledger_list(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["change_id"], change["id"])
        self.assertEqual(active[0]["change_version"], change["version"])
        self.assertEqual(active[0]["instrument_version"], "inst-v1")
        self.assertEqual(active[0]["signed_by"], "eng-A")
        self.assertEqual(active[0]["detail"]["direction"], "high")

        detail = self.svc.ledger_detail(loop["id"])
        self.assertEqual(detail["active"]["id"], active[0]["id"])
        self.assertEqual(detail["basis_change"]["setpoint"]["upper_limit"], 80.0)
        self.assertEqual(detail["unfinished"], [])

    def test_range_mismatch_blocks_and_names_loop(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-2", 0, 50)  # 量程到 50
        self.attach_setpoint(change, loop, upper=90)   # 限值 90 盖不住
        change = self.implement(change)
        self.record_test(loop, change)

        result, batch = self.submit(change, [loop])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["activated"], [])
        self.assertEqual(result["blocked"][0]["loop_id"], loop["id"])
        self.assertEqual(result["blocked"][0]["code"], "RANGE_COVER")
        self.assertEqual(self.svc.ledger_list(status="active"), [])

        detail = self.svc.batch_detail(batch["id"])
        self.assertEqual(detail["lines"][0]["status"], "blocked")
        self.assertEqual(detail["lines"][0]["issue"]["code"], "RANGE_COVER")

    def test_hysteresis_direction_blocks(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-3", 0, 100)
        # 高报回差 90 -> 复位点 -10，越过量程，方向装反
        self.attach_setpoint(change, loop, upper=80, hysteresis=90)
        change = self.implement(change)
        self.record_test(loop, change)
        result, _ = self.submit(change, [loop])
        self.assertEqual(result["blocked"][0]["code"], "HYSTERESIS_DIRECTION")

    def test_low_alarm_hysteresis_on_wrong_side_blocks(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-4", 0, 100)
        # 低报回差应在上侧；给 90 -> 复位点 110 越界
        self.attach_setpoint(change, loop, direction="low", upper=None,
                             lower=20, hysteresis=90)
        change = self.implement(change)
        self.record_test(loop, change)
        result, _ = self.submit(change, [loop])
        self.assertEqual(result["blocked"][0]["code"], "HYSTERESIS_DIRECTION")

    def test_failed_test_blocks_until_passing_test_recorded(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-5", 0, 100)
        self.attach_setpoint(change, loop)
        change = self.implement(change)
        self.record_test(loop, change, passed=False)

        result, batch = self.submit(change, [loop])
        self.assertEqual(result["blocked"][0]["code"], "TEST_FAILED")

        self.record_test(loop, change, passed=True)
        result = self.svc.submit_batch(ENG, self.svc.get(batch["id"]))
        self.assertEqual(result["status"], "activated")

    def test_partial_batch_blocks_only_failing_loops(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        good = self.create_loop(change, "TI-G", 0, 100)
        bad = self.create_loop(change, "TI-B", 0, 50)
        self.svc.transition(ENG, change["id"], "revise", {"setpoints": [
            {"loop_id": good["id"], "direction": "high", "upper_limit": 80, "hysteresis": 2},
            {"loop_id": bad["id"], "direction": "high", "upper_limit": 90},
        ]})
        change = self.implement(change)
        self.record_test(good, change)
        self.record_test(bad, change)

        result, batch = self.submit(change, [good, bad])
        self.assertEqual(result["activated"], [good["id"]])
        self.assertEqual([b["loop_id"] for b in result["blocked"]], [bad["id"]])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self.svc.ledger_list(status="active")), 1)

    def test_change_must_be_implemented(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-6", 0, 100)
        # 批次可以先建，但对未实施变更提交投产必须挡住
        batch = self.svc.create(ENG, "activation_batch",
                                {"change_id": change["id"], "loop_ids": [loop["id"]]})
        result = self.svc.submit_batch(ENG, self.svc.get(batch["id"]))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked"][0]["code"], "CHANGE_NOT_IMPLEMENTED")
        self.assertEqual(self.svc.ledger_list(status="active"), [])


if __name__ == "__main__":
    unittest.main()
