import unittest

from src.domain import Actor, ConflictError, ValidationError
from tests.test_ledger_workflow import LedgerCase


class VersionInvalidationTest(LedgerCase):
    def _activated_loop(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-1", 0, 100)
        self.attach_setpoint(change, loop, upper=80)
        change = self.implement(change)
        self.record_test(loop, change)
        result, batch = self.submit(change, [loop])
        self.assertEqual(result["status"], "activated")
        return self.svc.get(loop["id"]), self.svc.get(change["id"]), batch

    def test_instrument_revision_invalidates_old_tests_and_supersedes_ledger(self):
        loop, change, batch = self._activated_loop()
        tests = [t for t in self.svc.list("test_record") if t["data"]["loop_id"] == loop["id"]]
        self.assertEqual(len(tests), 1)

        updated = self.svc.revise_loop(
            Actor("eng-A", "engineer"), loop, {"instrument_version": "inst-v2"},
            expected_version=loop["version"],
        )
        self.assertEqual(updated["version"], loop["version"] + 1)
        self.assertEqual(updated["invalidated_tests"], [tests[0]["id"]])
        self.assertEqual(self.svc.get(tests[0]["id"])["status"], "invalid")

        reviews = [r for r in self.svc.list("review_task")
                   if r["data"]["loop_id"] == loop["id"]]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "pending")
        ledger = self.svc.ledger_list(loop_id=loop["id"])
        self.assertEqual([row["status"] for row in ledger], ["superseded"])

        # 版本没升却改量程，拒绝（防止拿旧仪表版本号盖新参数）
        newer = self.svc.get(loop["id"])
        with self.assertRaises(ConflictError):
            self.svc.revise_loop(Actor("eng-A", "engineer"), newer,
                                 {"instrument_version": "inst-v2",
                                  "range_low": -10, "range_high": 120},
                                 expected_version=newer["version"])

        # 乐观锁：后到方基于旧版本提交冲突，拿到当前版本号
        with self.assertRaises(ConflictError) as ctx:
            self.svc.revise_loop(Actor("eng-A", "engineer"), loop,
                                 {"instrument_version": "inst-v3"},
                                 expected_version=loop["version"])
        self.assertEqual(ctx.exception.details["current_version"], updated["version"])

    def test_pending_review_auto_resolved_by_new_passing_test(self):
        loop, change, _ = self._activated_loop()
        self.svc.revise_loop(Actor("eng-A", "engineer"), loop,
                             {"instrument_version": "inst-v2"}, loop["version"])
        pending = self.svc.list("review_task", status="pending")
        self.assertEqual(len(pending), 1)

        # 失败试验不能关闭待复核
        self.record_test(self.svc.get(loop["id"]), change, passed=False,
                         instrument="inst-v2")
        pending = [r for r in self.svc.list("review_task") if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)

        # 通过试验关闭待复核
        self.record_test(self.svc.get(loop["id"]), change, passed=True,
                         instrument="inst-v2")
        pending = self.svc.list("review_task", status="pending")
        self.assertEqual(pending, [])

        # 再来一次仪表改版，同一张待复核单重新打开，不重复建账
        loop = self.svc.get(loop["id"])
        self.svc.revise_loop(Actor("eng-A", "engineer"), loop,
                             {"instrument_version": "inst-v3"}, loop["version"])
        reviews = [r for r in self.svc.list("review_task")
                   if r["data"]["loop_id"] == loop["id"]]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "pending")

    def test_setpoint_change_invalidates_fingerprint_mismatched_tests(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-9", 0, 100)
        self.attach_setpoint(change, loop, upper=80, hysteresis=2)
        change = self.implement(self.svc.get(change["id"]))
        test = self.record_test(loop, change)

        # 新变更单引用同一回路，定值从 80 改为 85：旧试验针对旧定值 -> 失效
        change2 = self.draft_change(unit, description="MOC-2")
        loop = self.svc.get(loop["id"])
        self.svc.transition(Actor("eng-A", "engineer"), change2["id"], "revise",
                            {"setpoints": [{"loop_id": loop["id"], "direction": "high",
                                            "upper_limit": 85, "hysteresis": 2}]})
        change2 = self.implement(self.svc.get(change2["id"]))
        batch = self.svc.create(Actor("eng-A", "engineer"), "activation_batch",
                                {"change_id": change2["id"], "loop_ids": [loop["id"]]})
        result = self.svc.submit_batch(Actor("eng-A", "engineer"),
                                       self.svc.get(batch["id"]))
        codes = [item["code"] for item in result["blocked"]]
        self.assertIn("TEST_STALE", codes)
        stale = next(item for item in result["blocked"] if item["code"] == "TEST_STALE")
        self.assertEqual(stale["stale_test_id"], test["id"])

        # 按新依据补试验后放行
        self.record_test(loop, change2)
        result = self.svc.submit_batch(Actor("eng-A", "engineer"),
                                       self.svc.get(batch["id"]))
        self.assertEqual(result["status"], "activated")

    def test_identical_revision_does_not_invalidate(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-8", 0, 100)
        self.attach_setpoint(change, loop, upper=80, hysteresis=2)
        change = self.implement(self.svc.get(change["id"]))
        test = self.record_test(loop, change)
        # 同版本同参数重报仪表（版本号未变），不失效
        updated = self.svc.revise_loop(Actor("eng-A", "engineer"),
                                       self.svc.get(loop["id"]),
                                       {"instrument_version": "inst-v1"},
                                       expected_version=loop["version"])
        self.assertEqual(updated["invalidated_tests"], [])
        self.assertEqual(self.svc.get(test["id"])["status"], "recorded")

    def test_review_task_is_system_managed(self):
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.svc.create(Actor("eng-A", "engineer"), "review_task", {"loop_id": "x"})


if __name__ == "__main__":
    unittest.main()
