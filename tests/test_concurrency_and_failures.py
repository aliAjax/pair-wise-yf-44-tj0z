import threading
import unittest

from src.domain import Actor
from tests.test_ledger_workflow import LedgerCase


class ConcurrencyTest(LedgerCase):
    def _ready_loop(self):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loop = self.create_loop(change, "TI-C", 0, 100)
        self.attach_setpoint(change, loop, upper=80)
        change = self.implement(self.svc.get(change["id"]))
        self.record_test(loop, change)
        return self.svc.get(loop["id"]), self.svc.get(change["id"])

    def test_two_engineers_same_loop_only_one_takes_effect(self):
        loop, change = self._ready_loop()
        batch_a = self.svc.create(Actor("eng-A", "engineer"), "activation_batch",
                                  {"change_id": change["id"], "loop_ids": [loop["id"]]})
        batch_b = self.svc.create(Actor("eng-B", "engineer"), "activation_batch",
                                  {"change_id": change["id"], "loop_ids": [loop["id"]]})
        results = {}
        barrier = threading.Barrier(2)

        def submit(actor_id, batch_id):
            actor = Actor(actor_id, "engineer")
            barrier.wait()
            results[actor_id] = self.svc.submit_batch(actor, self.svc.get(batch_id))

        t1 = threading.Thread(target=submit, args=("eng-A", batch_a["id"]))
        t2 = threading.Thread(target=submit, args=("eng-B", batch_b["id"]))
        t1.start(); t2.start(); t1.join(); t2.join()

        active = self.svc.ledger_list(status="active")
        self.assertEqual(len(active), 1)  # 只让一笔生效
        winners = [name for name, result in results.items() if result["status"] == "activated"]
        losers = [name for name, result in results.items() if result["status"] != "activated"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        loser_result = results[losers[0]]
        self.assertEqual(loser_result["blocked"][0]["code"], "ALREADY_ACTIVE")
        # 后到方拿到当前值（谁签认的、哪个批次、哪个版本）
        current = loser_result["blocked"][0]["current"]
        self.assertEqual(current["signed_by"], winners[0])
        self.assertEqual(current["change_version"], change["version"])
        self.assertEqual(current["instrument_version"], "inst-v1")

    def test_concurrent_different_loops_both_activate(self):
        loop_a, change = self._ready_loop()
        # 第二张变更单把第二个回路纳入，两条回路各投各的
        unit2 = self.create_unit()
        change2 = self.draft_change(unit2, description="MOC-2")
        loop_b = self.create_loop(change2, "TI-D", 0, 100)
        self.svc.transition(Actor("eng-A", "engineer"), change2["id"], "revise",
                            {"setpoints": [
                                {"loop_id": loop_a["id"], "direction": "high",
                                 "upper_limit": 80, "hysteresis": 2},
                                {"loop_id": loop_b["id"], "direction": "high",
                                 "upper_limit": 70, "hysteresis": 2},
                            ]})
        change2 = self.implement(self.svc.get(change2["id"]))
        self.record_test(loop_b, change2)
        self.record_test(loop_a, change2)
        batch_a = self.svc.create(Actor("eng-A", "engineer"), "activation_batch",
                                  {"change_id": change2["id"], "loop_ids": [loop_a["id"]]})
        batch_b = self.svc.create(Actor("eng-B", "engineer"), "activation_batch",
                                  {"change_id": change2["id"], "loop_ids": [loop_b["id"]]})
        barrier = threading.Barrier(2)

        def submit(actor_id, batch_id):
            barrier.wait()
            self.svc.submit_batch(Actor(actor_id, "engineer"), self.svc.get(batch_id))

        t1 = threading.Thread(target=submit, args=("eng-A", batch_a["id"]))
        t2 = threading.Thread(target=submit, args=("eng-B", batch_b["id"]))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(self.svc.ledger_list(status="active")), 2)


class WriteFailureTest(LedgerCase):
    def _ready(self, count=3):
        unit = self.create_unit()
        change = self.draft_change(unit)
        loops = []
        entries = []
        for idx in range(count):
            loop = self.create_loop(change, "TI-%d" % idx, 0, 100)
            loops.append(loop)
            entries.append({"loop_id": loop["id"], "direction": "high",
                            "upper_limit": 70 + idx, "hysteresis": 1})
        self.svc.transition(Actor("eng-A", "engineer"), change["id"], "revise",
                            {"setpoints": entries})
        change = self.implement(self.svc.get(change["id"]))
        for loop in loops:
            self.record_test(loop, change)
        return loops, self.svc.get(change["id"])

    def test_write_failure_keeps_unfinished_loops_and_retry_continues(self):
        loops, change = self._ready(count=3)
        batch = self.svc.create(Actor("eng-A", "engineer"), "activation_batch",
                                {"change_id": change["id"],
                                 "loop_ids": [l["id"] for l in loops]})
        calls = {"n": 0}
        original_activate = self.repo.activate_line

        def flaky_activate(batch_id, loop_id, snapshot, signed_by):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return original_activate(batch_id, loop_id, snapshot, signed_by)

        self.repo.activate_line = flaky_activate
        first = self.svc.submit_batch(Actor("eng-A", "engineer"), self.svc.get(batch["id"]))
        self.repo.activate_line = original_activate

        # 写盘失败的回路保持未完成，其余不受影响，没有重复签认
        self.assertEqual(len(first["activated"]), 2)
        self.assertEqual(first["write_failed"], [loops[1]["id"]])
        statuses = {line["loop_id"]: line["status"] for line in first["lines"]}
        self.assertEqual(statuses[loops[1]["id"]], "pending")
        self.assertEqual(first["status"], "open")

        # 重试：只做未完成回路，已完成的不重复签认
        second = self.svc.submit_batch(Actor("eng-A", "engineer"), self.svc.get(batch["id"]))
        self.assertEqual(second["activated"], [loops[1]["id"]])
        self.assertEqual(second["skipped"], [loops[0]["id"], loops[2]["id"]])
        self.assertEqual(second["write_failed"], [])
        self.assertEqual(second["status"], "activated")
        ledger = self.svc.ledger_list(status="active")
        self.assertEqual(sorted(row["loop_id"] for row in ledger),
                         sorted(l["id"] for l in loops))
        # 每回路只有一笔账：重试未产生重复签认
        self.assertEqual(len(self.svc.ledger_list()), 3)

    def test_persistent_failure_is_retriable(self):
        loops, change = self._ready(count=1)
        batch = self.svc.create(Actor("eng-A", "engineer"), "activation_batch",
                                {"change_id": change["id"], "loop_ids": [loops[0]["id"]]})

        def always_fail(*args, **kwargs):
            raise OSError("disk offline")

        self.repo.activate_line = always_fail
        result = self.svc.submit_batch(Actor("eng-A", "engineer"), self.svc.get(batch["id"]))
        self.assertEqual(result["write_failed"], [loops[0]["id"]])
        self.assertEqual(result["status"], "open")
        self.assertEqual(self.svc.ledger_list(), [])

        del self.repo.activate_line
        retry = self.svc.submit_batch(Actor("eng-A", "engineer"), self.svc.get(batch["id"]))
        self.assertEqual(retry["status"], "activated")
        self.assertEqual(len(self.svc.ledger_list(status="active")), 1)


if __name__ == "__main__":
    unittest.main()
