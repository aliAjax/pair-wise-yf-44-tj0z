from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    CommissionBlocked,
    ConcurrentEditConflict,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .rules import RuleEngine, recompute_commission


class DomainService:
    def __init__(self, repository, rules=None, fault=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # Optional callable(batch_id, loop) invoked before each loop sign-off;
        # raising from it simulates a mid-batch write failure so retry/resume
        # can be exercised in tests.
        self.fault = fault

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 生效账: instrument loops, tests, commissioning
    # ------------------------------------------------------------------

    SETPOINT_FIELDS = (
        "setpoint",
        "alarm_limit",
        "alarm_direction",
        "hysteresis",
        "range_min",
        "range_max",
    )

    def update_loop(self, actor, loop_id, data, expected_version=None):
        """Edit a loop's config with optimistic concurrency.

        The first writer wins. A concurrent writer gets a
        ``ConcurrentEditConflict`` carrying the current record and the list
        of fields that conflicted, so the later submitter sees live values.
        Bumping setpoint values or the instrument tag invalidates every
        existing valid test and opens a review for the loop.
        """
        loop = self.repository.get_entity(loop_id)
        if not loop or loop["kind"] != "loop":
            raise NotFoundError("loop not found: " + loop_id)
        expected = int(expected_version) if expected_version is not None else loop["version"]
        if expected != loop["version"]:
            raise self._edit_conflict(loop, data)
        self.rules._ensure_role(actor, ("admin", "engineer"))

        merged = dict(loop["data"])
        merged.update(data)
        setpoint_changed = any(
            k in data and data[k] is not None and data[k] != loop["data"].get(k)
            for k in self.SETPOINT_FIELDS
        )
        instrument_changed = (
            "instrument_tag" in data
            and data["instrument_tag"] != loop["data"].get("instrument_tag")
        )
        if setpoint_changed:
            merged["setpoint_version"] = int(loop["data"].get("setpoint_version", 1)) + 1
        if instrument_changed:
            merged["instrument_version"] = int(loop["data"].get("instrument_version", 1)) + 1

        try:
            updated = self.repository.update_entity(loop_id, expected, loop["status"], merged)
        except ConflictError:
            raise self._edit_conflict(self.repository.get_entity(loop_id), data)

        if setpoint_changed or instrument_changed:
            reason = "instrument_changed" if instrument_changed and not setpoint_changed else "setpoint_changed"
            self._invalidate_tests(loop_id, actor, reason)
        self.audit.record(
            loop_id,
            actor,
            "update",
            loop["status"],
            updated["status"],
            {
                "changed": sorted(data.keys()),
                "setpoint_changed": setpoint_changed,
                "instrument_changed": instrument_changed,
            },
        )
        return updated

    @staticmethod
    def _edit_conflict(current, data):
        conflicts = [
            k for k in data if k in current["data"] and data[k] != current["data"][k]
        ]
        return ConcurrentEditConflict(
            "loop was modified by another engineer; current values returned",
            current=current,
            conflicts=conflicts,
        )

    def _invalidate_tests(self, loop_id, actor, reason):
        tests = self.repository.find_entities("loop_test", "loop_id", loop_id)
        for test in tests:
            if test["data"].get("status") == "valid":
                merged = dict(test["data"])
                merged["status"] = "invalid"
                merged["invalidated_by"] = actor.user_id
                merged["invalidated_reason"] = reason
                self.repository.update_entity(test["id"], test["version"], "invalid", merged)
                self.audit.record(
                    test["id"], actor, "invalidate", test["status"], "invalid", {"reason": reason}
                )
        self.create(
            actor,
            "review_item",
            {"loop_id": loop_id, "reason": reason, "status": "open", "raised_by": actor.user_id},
        )

    def record_test(self, actor, loop_id, data):
        """Record a test against the loop's *current* setpoint/instrument versions."""
        loop = self.repository.get_entity(loop_id)
        if not loop or loop["kind"] != "loop":
            raise NotFoundError("loop not found: " + loop_id)
        self.rules._ensure_role(actor, ("admin", "engineer", "verifier"))
        result = data.get("result")
        if result not in ("passed", "failed"):
            raise ValidationError("result must be 'passed' or 'failed'")
        payload = {
            "loop_id": loop_id,
            "result": result,
            "tested_by": data.get("tested_by", actor.user_id),
            "evidence": data.get("evidence", ""),
            "tested_setpoint_version": loop["data"].get("setpoint_version", 1),
            "tested_instrument_version": loop["data"].get("instrument_version", 1),
            "status": "valid",
        }
        test = self.create(actor, "loop_test", payload)
        if result == "passed":
            self._resolve_reviews(loop_id, actor)
        else:
            self._ensure_open_review(loop_id, actor, "test_failed")
        self.audit.record(
            loop_id, actor, "test", None, test["status"], {"test_id": test["id"], "result": result}
        )
        return test

    def _resolve_reviews(self, loop_id, actor):
        for review in self.repository.find_entities("review_item", "loop_id", loop_id):
            if review["data"].get("status") == "open":
                merged = dict(review["data"])
                merged["status"] = "resolved"
                merged["resolved_by"] = actor.user_id
                self.repository.update_entity(review["id"], review["version"], "resolved", merged)
                self.audit.record(review["id"], actor, "resolve", "open", "resolved", {"loop_id": loop_id})

    def _ensure_open_review(self, loop_id, actor, reason):
        reviews = self.repository.find_entities("review_item", "loop_id", loop_id)
        if not any(r["data"].get("status") == "open" for r in reviews):
            self.create(
                actor,
                "review_item",
                {"loop_id": loop_id, "reason": reason, "status": "open", "raised_by": actor.user_id},
            )

    def commission(self, actor, change_id, loop_ids=None, idempotency_key=None):
        """Submit a change for commissioning: recompute every loop, then sign.

        Recompute is read-only and runs against the current change + loops. If
        any loop fails range / hysteresis / valid-test checks the batch is
        persisted as ``blocked`` and a ``CommissionBlocked`` naming the loops is
        raised. Otherwise each loop is signed in its own transaction so a
        mid-batch write failure leaves completed sign-offs intact.
        """
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                batch = self.repository.get_entity(existing)
                if batch:
                    return batch
        change = self.repository.get_entity(change_id)
        if not change or change["kind"] != "change":
            raise NotFoundError("change not found: " + change_id)
        self.rules._ensure_role(actor, ("admin", "engineer"))

        loops = self._loops_for(change_id, loop_ids)
        if not loops:
            raise ValidationError("no loops registered for change: " + change_id)
        tests_by_loop = {
            loop["id"]: self.repository.find_entities("loop_test", "loop_id", loop["id"])
            for loop in loops
        }
        passed, failures = recompute_commission(loops, tests_by_loop)

        batch_id = "batch-" + uuid4().hex[:8]
        batch_data = {
            "change_id": change_id,
            "loop_ids": [loop["id"] for loop in loops],
            "failures": failures,
        }
        initial = "in_progress" if passed else "blocked"
        batch = self.repository.create_entity(batch_id, "commission_batch", initial, batch_data, actor.user_id)
        self.audit.record(
            batch_id,
            actor,
            "commission_submit",
            None,
            initial,
            {"change_id": change_id, "failures": failures},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, batch_id)

        if not passed:
            raise CommissionBlocked(
                "commission blocked: %d loop(s) not ready" % len(failures),
                failures=failures,
                batch=batch,
            )

        self._sign_loops(batch_id, loops, actor)
        batch = self.repository.update_entity(
            batch_id, batch["version"], "completed", dict(batch_data, status="completed")
        )
        self.audit.record(batch_id, actor, "commission_complete", "in_progress", "completed", {})
        return batch

    def retry_commission(self, actor, batch_id):
        """Resume a batch after a write failure: re-validate, sign only unsigned loops."""
        batch = self.repository.get_entity(batch_id)
        if not batch or batch["kind"] != "commission_batch":
            raise NotFoundError("commission batch not found: " + batch_id)
        self.rules._ensure_role(actor, ("admin", "engineer"))
        loops = self._loops_for(batch["data"].get("change_id"), batch["data"].get("loop_ids"))
        tests_by_loop = {
            loop["id"]: self.repository.find_entities("loop_test", "loop_id", loop["id"])
            for loop in loops
        }
        passed, failures = recompute_commission(loops, tests_by_loop)
        if not passed:
            merged = dict(batch["data"])
            merged["status"] = "blocked"
            merged["failures"] = failures
            batch = self.repository.update_entity(batch_id, batch["version"], "blocked", merged)
            raise CommissionBlocked(
                "commission blocked: %d loop(s) not ready" % len(failures),
                failures=failures,
                batch=batch,
            )

        signed_ids = {s["loop_id"] for s in self.repository.list_signoffs(batch_id)}
        todo = [loop for loop in loops if loop["id"] not in signed_ids]
        self._sign_loops(batch_id, todo, actor)

        signed_ids = {s["loop_id"] for s in self.repository.list_signoffs(batch_id)}
        merged = dict(batch["data"])
        merged["failures"] = []
        if len(signed_ids) >= len(loops):
            merged["status"] = "completed"
            batch = self.repository.update_entity(batch_id, batch["version"], "completed", merged)
            self.audit.record(batch_id, actor, "retry", "in_progress", "completed", {"signed": len(signed_ids)})
        else:
            merged["status"] = "in_progress"
            batch = self.repository.update_entity(batch_id, batch["version"], "in_progress", merged)
        return batch

    def _loops_for(self, change_id, loop_ids):
        if loop_ids:
            loops = [self.repository.get_entity(lid) for lid in loop_ids]
            loops = [loop for loop in loops if loop and loop["kind"] == "loop"]
        else:
            loops = self.repository.find_entities("loop", "change_id", change_id)
        return sorted(loops, key=lambda loop: (loop["data"].get("tag") or loop["id"]))

    def _sign_loops(self, batch_id, loops, actor):
        for loop in loops:
            if self.fault is not None:
                self.fault(batch_id, loop)
            inserted = self.repository.add_signoff(batch_id, loop["id"], actor.user_id)
            if inserted:
                self.audit.record(
                    batch_id,
                    actor,
                    "sign",
                    None,
                    "signed",
                    {"loop_id": loop["id"], "tag": loop["data"].get("tag")},
                )

    def effective(self, change_id=None, loop_id=None):
        """Effective ledger view: current config + source + open items per loop."""
        if loop_id:
            loop = self.repository.get_entity(loop_id)
            loops = [loop] if loop and loop["kind"] == "loop" else []
        elif change_id:
            loops = self.repository.find_entities("loop", "change_id", change_id)
        else:
            loops = self.repository.list_entities(kind="loop")
        result = []
        for loop in loops:
            tests = self.repository.find_entities("loop_test", "loop_id", loop["id"])
            valid_tests = [
                t for t in tests
                if t["data"].get("status") == "valid" and t["data"].get("result") == "passed"
            ]
            open_reviews = [
                r for r in self.repository.find_entities("review_item", "loop_id", loop["id"])
                if r["data"].get("status") == "open"
            ]
            change = None
            if loop["data"].get("change_id"):
                change = self.repository.get_entity(loop["data"]["change_id"])
            result.append(
                {
                    "loop": loop,
                    "effective": {
                        "setpoint": loop["data"].get("setpoint"),
                        "alarm_limit": loop["data"].get("alarm_limit"),
                        "alarm_direction": loop["data"].get("alarm_direction"),
                        "hysteresis": loop["data"].get("hysteresis"),
                        "range_min": loop["data"].get("range_min"),
                        "range_max": loop["data"].get("range_max"),
                        "setpoint_version": loop["data"].get("setpoint_version", 1),
                        "instrument_version": loop["data"].get("instrument_version", 1),
                        "basis_version": loop["data"].get("basis_version"),
                    },
                    "source": {
                        "change_id": change["id"] if change else None,
                        "description": change["data"].get("description") if change else None,
                        "status": change["status"] if change else None,
                    },
                    "last_valid_test": valid_tests[-1] if valid_tests else None,
                    "open_reviews": open_reviews,
                    "ready": not open_reviews and bool(valid_tests),
                }
            )
        return result
