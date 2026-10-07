from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    ValidationError,
)
from .rules import (
    ALREADY_ACTIVE,
    RuleEngine,
    _setpoint_entry,
    evaluate_loop,
    issue,
    setpoint_fingerprint,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field=None, value=None):
        kind = self.rules.normalize_kind(kind)
        if field is None:
            return self.repository.list_entities(kind=kind)
        return self.repository.find_entities(kind, field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------ 建账

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "test_record":
            return self._create_test(actor, payload, idempotency_key)
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if kind == "activation_batch":
            self.repository.init_batch_lines(entity_id, entity["data"]["loops"])
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_test(self, actor, data, idempotency_key=None):
        payload = self.rules.validate_create(actor, "test_record", data, self._lookup)
        loop = self.repository.get_entity(payload["loop_id"])
        # 依据变更单可显式指定（同回路被多张变更单引用时），缺省取回路登记的依据
        change_id = payload.get("basis_change_id") or loop["data"].get("basis_change_id")
        fingerprint = None
        if change_id:
            change = self.repository.get_entity(change_id)
            if change:
                entries = [e for e in (_setpoint_entry(change["data"]) or [])
                           if e.get("loop_id") in (None, "", loop["id"])]
                if entries:
                    fingerprint = list(setpoint_fingerprint([entries[0]]))
        payload["basis_change_id"] = change_id
        payload["basis_change_version"] = str(payload["basis_change_version"])
        payload["basis_instrument_version"] = str(payload["basis_instrument_version"])
        payload["basis_setpoint_fingerprint"] = fingerprint
        test_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(test_id):
            raise ConflictError("entity already exists: " + test_id)
        entity = self.repository.insert_test(test_id, payload, actor)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, test_id)
        return entity

    # ------------------------------------------------------------ 动作分发

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "change" and action == "revise":
            return self.revise_change(actor, entity, dict(data or {}), expected_version)
        if kind == "instrument_loop" and action == "revise_instrument":
            return self.revise_loop(actor, entity, dict(data or {}), expected_version)
        if kind == "activation_batch" and action == "submit":
            return self.submit_batch(actor, entity, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id, actor, action, entity["status"], updated["status"], {"patch": patch}
        )
        return updated

    def revise_change(self, actor, entity, data, expected_version):
        """变更单草稿阶段改定值：结构校验后升版；定值指纹一变，旧试验立即失效并挂待复核。"""
        next_status, patch = self.rules.validate_transition(
            actor, entity, "revise", data, self._lookup
        )
        entries = _setpoint_entry({"setpoints": patch["setpoints"]})
        merged = dict(entity["data"])
        merged["setpoints"] = entries
        expected = int(expected_version) if expected_version is not None else entity["version"]
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        per_loop = {}
        for entry in entries:
            loop_id = entry.get("loop_id")
            if loop_id:
                per_loop[loop_id] = setpoint_fingerprint([entry])
        invalidated = self.repository.invalidate_tests_for_change(
            entity["id"], per_loop, actor, "change setpoint revised"
        )
        self.audit.record(entity["id"], actor, "revise", entity["status"], next_status,
                          {"setpoints": entries, "invalidated": invalidated})
        updated["invalidated_tests"] = invalidated
        return updated

    def revise_loop(self, actor, entity, data, expected_version):
        """仪表/量程版本变更：原子地升版、旧试验失效、生成待复核、旧生效账让位。"""
        _, patch = self.rules.validate_transition(
            actor, entity, "revise_instrument", data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        # 允许随仪表改量程，但量程必须是数字且合法
        merged["range_low"] = float(merged["range_low"])
        merged["range_high"] = float(merged["range_high"])
        if merged["range_low"] >= merged["range_high"]:
            raise ValidationError("range_low must be below range_high")
        expected = int(expected_version) if expected_version is not None else entity["version"]
        updated = self.repository.apply_loop_revision(
            entity["id"], expected, merged, actor, "instrument revision"
        )
        return updated

    # ------------------------------------------------------------ 投产逐项复算

    def submit_batch(self, actor, batch, expected_version=None):
        """按当前变更与回路逐条复算；挡住不过项；写盘失败保留未完成回路，可重试。"""
        if batch["status"] not in ("open", "blocked"):
            raise InvalidTransition("cannot submit batch from status %s" % batch["status"])
        self.rules._ensure_role(actor, ("admin", "engineer"))
        if expected_version is not None and int(expected_version) != int(batch["version"]):
            lines = self.repository.list_lines(batch["id"])
            raise ConflictError(
                "batch version conflict: expected %s, found %s"
                % (expected_version, batch["version"]),
                details={"reason": "BATCH_VERSION_CONFLICT",
                         "current_version": batch["version"], "lines": lines},
            )

        change = self.repository.get_entity(batch["data"]["change_id"])
        if not change:
            raise NotFoundError("change not found: " + batch["data"]["change_id"])
        loop_ids = batch["data"]["loops"]
        loops = {item["id"]: item for item in self.repository.list_entities("instrument_loop")
                 if item["id"] in loop_ids}
        all_tests = [t for t in self.repository.list_entities("test_record")
                     if t["data"].get("loop_id") in loop_ids]
        tests_by_loop = {}
        for test in all_tests:
            tests_by_loop.setdefault(test["data"]["loop_id"], []).append(test)

        blocked, activated, skipped, write_failed = [], [], [], []
        for loop_id in loop_ids:
            line = self.repository.get_line(batch["id"], loop_id)
            if line and line["status"] == "done":
                skipped.append(loop_id)  # 重试不重复签认
                continue

            problem = evaluate_loop(
                loop_id, loops, {change["id"]: change},
                tests_by_loop.get(loop_id, []), None, change_id=change["id"],
            )
            if problem:
                self.repository.mark_line_blocked(
                    batch["id"], loop_id, problem,
                    {"evaluated_against": {"change_version": change["version"],
                                           "instrument_version": loops.get(loop_id, {}).get(
                                               "data", {}).get("instrument_version")}},
                )
                blocked.append(problem)
                continue

            loop = loops[loop_id]
            entries = [e for e in (_setpoint_entry(change["data"]) or [])
                       if e.get("loop_id") in (None, "", loop_id)]
            entry = entries[0]
            matching = [t for t in tests_by_loop.get(loop_id, []) if t["status"] != "invalid"]
            matching = [
                t for t in matching
                if str(t["data"].get("basis_change_version")) == str(change["version"])
                and str(t["data"].get("basis_instrument_version")) == str(
                    loop["data"]["instrument_version"])
            ]
            passed = [t for t in matching if t["data"].get("passed")]
            latest_test = sorted(passed or matching,
                                 key=lambda t: (t["created_at"], t["id"]))[-1]
            snapshot = {
                "tag": loop["data"].get("tag"),
                "range_low": loop["data"]["range_low"],
                "range_high": loop["data"]["range_high"],
                "instrument_version": loop["data"]["instrument_version"],
                "change_id": change["id"],
                "change_version": change["version"],
                "direction": entry["direction"],
                "upper_limit": entry.get("upper_limit"),
                "lower_limit": entry.get("lower_limit"),
                "hysteresis": entry.get("hysteresis", 0.0),
                "test_id": latest_test["id"],
            }
            try:
                _, created = self.repository.activate_line(
                    batch["id"], loop_id, snapshot, actor.user_id
                )
            except ConflictError as exc:
                # 并发：另一笔已抢先生效——后到方拿到当前值与冲突项
                fresh = self.repository.list_ledger(loop_id=loop_id, status="active")
                current = fresh[0] if fresh else None
                if current is None and getattr(exc, "details", None):
                    current = {"loop_id": loop_id,
                               "batch_id": exc.details.get("current_batch"),
                               "signed_by": exc.details.get("current_signed_by")}
                detail = issue(ALREADY_ACTIVE, loop_id, current=current)
                self.repository.mark_line_blocked(batch["id"], loop_id, detail,
                                                  {"signed_by": (current or {}).get("signed_by")})
                blocked.append(detail)
                continue
            except Exception:
                # 写盘失败：回路保持未完成，事务已回滚，之后重试接着做
                write_failed.append(loop_id)
                continue
            activated.append(loop_id)
            self.audit.record(
                batch["id"], actor, "activate_loop", None, "active",
                {"loop_id": loop_id, "change_version": change["version"], "created": created},
            )

        status = self.repository.refresh_batch_status(batch["id"])
        result = {
            "batch_id": batch["id"],
            "status": status,
            "activated": activated,
            "skipped": skipped,
            "write_failed": write_failed,
            "blocked": blocked,
            "lines": self.repository.list_lines(batch["id"]),
        }
        self.audit.record(batch["id"], actor, "submit", batch["status"], status,
                          {"activated": activated, "blocked_count": len(blocked),
                           "write_failed": write_failed, "skipped": skipped})
        return result

    # ------------------------------------------------------------ 查询

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

    def batch_detail(self, batch_id):
        batch = self.repository.get_entity(batch_id)
        if not batch:
            raise NotFoundError("batch not found: " + batch_id)
        change = self.repository.get_entity(batch["data"]["change_id"])
        return {
            "batch": batch,
            "change": {"id": change["id"], "version": change["version"],
                       "status": change["status"]} if change else None,
            "lines": self.repository.list_lines(batch_id),
        }

    def ledger_detail(self, loop_id):
        """一条回路的生效账详情：有效版本、来源、试验与未完成项。"""
        loop = self.repository.get_entity(loop_id)
        if not loop:
            raise NotFoundError("loop not found: " + loop_id)
        active = self.repository.list_ledger(loop_id=loop_id, status="active")
        history = self.repository.list_ledger(loop_id=loop_id)
        tests = self.repository.list_entities("test_record")
        tests = [t for t in tests if t["data"].get("loop_id") == loop_id]
        reviews = [r for r in self.repository.list_entities("review_task")
                   if r["data"].get("loop_id") == loop_id]
        change = None
        change_id = loop["data"].get("basis_change_id")
        if change_id:
            change_entity = self.repository.get_entity(change_id)
            if change_entity:
                entries = [e for e in (_setpoint_entry(change_entity["data"]) or [])
                           if e.get("loop_id") in (None, "", loop_id)]
                change = {"id": change_entity["id"], "version": change_entity["version"],
                          "status": change_entity["status"], "setpoint": entries[0] if entries else None}
        pending_lines = []
        for batch in self.repository.list_entities("activation_batch"):
            for line in self.repository.list_lines(batch["id"]):
                if line["loop_id"] == loop_id and line["status"] in ("pending", "blocked"):
                    pending_lines.append({"batch_id": batch["id"], **line})
        return {
            "loop": loop,
            "basis_change": change,
            "active": active[0] if active else None,
            "history": history,
            "tests": tests,
            "review_tasks": reviews,
            "unfinished": pending_lines,
        }

    def ledger_list(self, loop_id=None, status=None, change_id=None):
        return self.repository.list_ledger(loop_id=loop_id, status=status, change_id=change_id)
