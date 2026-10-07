import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    loop_id TEXT NOT NULL,
                    change_id TEXT NOT NULL,
                    change_version INTEGER NOT NULL,
                    instrument_version TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    signed_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_one_active
                    ON ledger(loop_id) WHERE status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_same_basis
                    ON ledger(loop_id, change_id, change_version, instrument_version)
                    WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_ledger_batch
                    ON ledger(batch_id);
                CREATE TABLE IF NOT EXISTS activation_line (
                    batch_id TEXT NOT NULL,
                    loop_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issue TEXT,
                    detail TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, loop_id)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ------------------------------------------------------------ 生效账

    @staticmethod
    def _line_from_row(row):
        return {
            "batch_id": row["batch_id"],
            "loop_id": row["loop_id"],
            "status": row["status"],
            "issue": json.loads(row["issue"]) if row["issue"] else None,
            "detail": json.loads(row["detail"]),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ledger_from_row(row):
        return {
            "id": row["id"],
            "loop_id": row["loop_id"],
            "change_id": row["change_id"],
            "change_version": int(row["change_version"]),
            "instrument_version": row["instrument_version"],
            "batch_id": row["batch_id"],
            "status": row["status"],
            "detail": json.loads(row["detail"]),
            "signed_by": row["signed_by"],
            "created_at": row["created_at"],
            "superseded_at": row["superseded_at"],
        }

    def init_batch_lines(self, batch_id, loop_ids):
        now = utcnow()
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO activation_line(batch_id, loop_id, status, issue, detail, updated_at) "
                "VALUES (?, ?, 'pending', NULL, '{}', ?) "
                "ON CONFLICT(batch_id, loop_id) DO NOTHING",
                [(batch_id, loop_id, now) for loop_id in loop_ids],
            )

    def list_lines(self, batch_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM activation_line WHERE batch_id = ? ORDER BY loop_id",
                (batch_id,),
            ).fetchall()
        return [self._line_from_row(row) for row in rows]

    def get_line(self, batch_id, loop_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM activation_line WHERE batch_id = ? AND loop_id = ?",
                (batch_id, loop_id),
            ).fetchone()
        return self._line_from_row(row) if row else None

    def mark_line_blocked(self, batch_id, loop_id, issue_obj, detail):
        now = utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO activation_line(batch_id, loop_id, status, issue, detail, updated_at) "
                "VALUES (?, ?, 'blocked', ?, ?, ?) "
                "ON CONFLICT(batch_id, loop_id) DO UPDATE SET "
                "status='blocked', issue=excluded.issue, detail=excluded.detail, "
                "updated_at=excluded.updated_at",
                (batch_id, loop_id, json.dumps(issue_obj, ensure_ascii=False),
                 json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
            )
            self._refresh_batch_status_locked(connection, batch_id)
            connection.commit()

    @staticmethod
    def _refresh_batch_status_locked(connection, batch_id):
        counts = dict(connection.execute(
            "SELECT status, COUNT(*) FROM activation_line WHERE batch_id = ? GROUP BY status",
            (batch_id,),
        ).fetchall())
        entity = connection.execute(
            "SELECT status FROM entities WHERE id = ?", (batch_id,)
        ).fetchone()
        if not entity:
            return
        if counts.get("done", 0) and not counts.get("pending") and not counts.get("blocked"):
            target = "activated"
        elif counts.get("blocked", 0) and not counts.get("pending"):
            target = "blocked"
        else:
            target = "open"
        if entity["status"] == "activated":
            target = "activated"  # 已整体生效不回退
        if target != entity["status"]:
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, updated_at = ? "
                "WHERE id = ?",
                (target, utcnow(), batch_id),
            )

    def activate_line(self, batch_id, loop_id, snapshot, signed_by):
        """一笔事务：同依据只让一笔生效；依据变了则旧账让位。已 done 直接返回，不重复签认。"""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            line = connection.execute(
                "SELECT * FROM activation_line WHERE batch_id = ? AND loop_id = ?",
                (batch_id, loop_id),
            ).fetchone()
            if line and line["status"] == "done":
                connection.commit()
                return self.get_line(batch_id, loop_id), False
            if line is None:
                connection.execute(
                    "INSERT INTO activation_line(batch_id, loop_id, status, issue, detail, updated_at) "
                    "VALUES (?, ?, 'pending', NULL, '{}', ?)",
                    (batch_id, loop_id, now),
                )
            detail_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            try:
                cursor = connection.execute(
                    "INSERT INTO ledger(loop_id, change_id, change_version, instrument_version, "
                    "batch_id, status, detail, signed_by, created_at, superseded_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)",
                    (
                        loop_id, snapshot["change_id"], int(snapshot["change_version"]),
                        str(snapshot["instrument_version"]), batch_id,
                        detail_json, signed_by, now,
                    ),
                )
            except sqlite3.IntegrityError:
                previous = connection.execute(
                    "SELECT * FROM ledger WHERE loop_id = ? AND status = 'active'",
                    (loop_id,),
                ).fetchone()
                same_basis = (
                    previous is not None
                    and previous["change_id"] == snapshot["change_id"]
                    and int(previous["change_version"]) == int(snapshot["change_version"])
                    and str(previous["instrument_version"]) == str(snapshot["instrument_version"])
                )
                if same_basis:
                    # 两名工程师并发同一回路同一依据：只让先到的一笔生效
                    raise ConflictError(
                        "loop %s already activated for the same basis" % loop_id,
                        details={"loop_id": loop_id, "reason": "ALREADY_ACTIVE",
                                 "current_batch": previous["batch_id"],
                                 "current_signed_by": previous["signed_by"]},
                    )
                # 依据版本变了：旧账让位后重插
                self.supersede_loop_ledger(connection, loop_id, now,
                                           "superseded by newer basis")
                cursor = connection.execute(
                    "INSERT INTO ledger(loop_id, change_id, change_version, instrument_version, "
                    "batch_id, status, detail, signed_by, created_at, superseded_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)",
                    (
                        loop_id, snapshot["change_id"], int(snapshot["change_version"]),
                        str(snapshot["instrument_version"]), batch_id,
                        detail_json, signed_by, now,
                    ),
                )
            ledger_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
                "to_status, detail, created_at) VALUES (?, ?, ?, 'activate', NULL, 'active', ?, ?)",
                (loop_id, signed_by, "engineer",
                 json.dumps({"ledger_id": ledger_id, "batch_id": batch_id,
                             "change_id": snapshot["change_id"],
                             "change_version": snapshot["change_version"],
                             "instrument_version": snapshot["instrument_version"]},
                            ensure_ascii=False, sort_keys=True), now),
            )
            connection.execute(
                "INSERT INTO activation_line(batch_id, loop_id, status, issue, detail, updated_at) "
                "VALUES (?, ?, 'done', NULL, ?, ?) "
                "ON CONFLICT(batch_id, loop_id) DO UPDATE SET status='done', issue=NULL, "
                "detail=excluded.detail, updated_at=excluded.updated_at",
                (batch_id, loop_id,
                 json.dumps({"ledger_id": ledger_id}, ensure_ascii=False, sort_keys=True), now),
            )
            # 控制系统侧回路依据绑定到本次生效的变更版本
            loop_row = connection.execute(
                "SELECT data FROM entities WHERE id = ?", (loop_id,)
            ).fetchone()
            if loop_row:
                loop_data = json.loads(loop_row["data"])
                loop_data["basis_change_id"] = snapshot["change_id"]
                connection.execute(
                    "UPDATE entities SET data = ?, version = version + 1, updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(loop_data, ensure_ascii=False, sort_keys=True), now, loop_id),
                )
            self._refresh_batch_status_locked(connection, batch_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_line(batch_id, loop_id), True

    def supersede_loop_ledger(self, connection, loop_id, now, reason):
        row = connection.execute(
            "SELECT * FROM ledger WHERE loop_id = ? AND status = 'active'", (loop_id,)
        ).fetchone()
        if not row:
            return None
        connection.execute(
            "UPDATE ledger SET status = 'superseded', superseded_at = ? "
            "WHERE id = ? AND status = 'active'",
            (now, row["id"]),
        )
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
            "to_status, detail, created_at) VALUES (?, 'system', 'system', 'supersede', "
            "'active', 'superseded', ?, ?)",
            (loop_id, json.dumps({"ledger_id": row["id"], "reason": reason},
                                 ensure_ascii=False, sort_keys=True), now),
        )
        return row["id"]

    def apply_loop_revision(self, loop_id, expected_version, new_data, actor, reason):
        """仪表/量程版本变更的原子操作：回路升版 + 旧试验失效 + 待复核生成 + 旧生效账让位。"""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (loop_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + loop_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version),
                    details={"current_version": current_version},
                )
            old_data = json.loads(row["data"])
            old_instrument = str(old_data.get("instrument_version"))
            new_instrument = str(new_data["instrument_version"])
            try:
                range_changed = (
                    float(old_data.get("range_low")) != float(new_data["range_low"])
                    or float(old_data.get("range_high")) != float(new_data["range_high"])
                )
            except (TypeError, ValueError):
                range_changed = old_data.get("range_low") != new_data["range_low"]
            if range_changed and old_instrument == new_instrument:
                raise ConflictError(
                    "range change on loop %s requires an instrument_version bump" % loop_id,
                    details={"loop_id": loop_id, "reason": "INSTRUMENT_VERSION_REQUIRED"},
                )
            if not range_changed and old_instrument == new_instrument:
                # 版本与量程都没动：幂等处理，不升版、不失效
                connection.commit()
                entity = self.get_entity(loop_id)
                entity["invalidated_tests"] = []
                return entity
            payload = json.dumps(new_data, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (payload, now, loop_id),
            )
            invalidated = []
            if old_instrument != new_instrument:
                tests = connection.execute(
                    "SELECT * FROM entities WHERE kind = 'test_record' AND status != 'invalid'"
                ).fetchall()
                for test in tests:
                    test_data = json.loads(test["data"])
                    if test_data.get("loop_id") != loop_id:
                        continue
                    if str(test_data.get("basis_instrument_version")) == new_instrument:
                        continue
                    connection.execute(
                        "UPDATE entities SET status = 'invalid', updated_at = ? WHERE id = ?",
                        (now, test["id"]),
                    )
                    detail = json.dumps(
                        {"reason": reason, "old_instrument_version": old_instrument,
                         "new_instrument_version": new_instrument},
                        ensure_ascii=False, sort_keys=True,
                    )
                    connection.execute(
                        "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                        "from_status, to_status, detail, created_at) VALUES (?, ?, ?, "
                        "'invalidate', ?, 'invalid', ?, ?)",
                        (test["id"], actor.user_id, actor.role, test["status"], detail, now),
                    )
                    invalidated.append(test["id"])
                # 每个回路一张待复核单（幂等：已待复核不重复建）
                review_id = "review-" + loop_id
                review = connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (review_id,)
                ).fetchone()
                if review is None:
                    connection.execute(
                        "INSERT INTO entities(id, kind, status, version, data, created_by, "
                        "created_at, updated_at) VALUES (?, 'review_task', 'pending', 1, ?, ?, ?, ?)",
                        (review_id, json.dumps(
                            {"loop_id": loop_id, "reason": reason,
                             "invalidated_tests": invalidated},
                            ensure_ascii=False, sort_keys=True),
                         "system", now, now),
                    )
                    connection.execute(
                        "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                        "from_status, to_status, detail, created_at) VALUES (?, 'system', "
                        "'system', 'create', NULL, 'pending', ?, ?)",
                        (review_id, json.dumps({"loop_id": loop_id}, ensure_ascii=False), now),
                    )
                elif review["status"] != "pending":
                    connection.execute(
                        "UPDATE entities SET status = 'pending', version = version + 1, "
                        "data = ?, updated_at = ? WHERE id = ?",
                        (json.dumps({"loop_id": loop_id, "reason": reason,
                                     "invalidated_tests": invalidated},
                                    ensure_ascii=False, sort_keys=True), now, review_id),
                    )
                    connection.execute(
                        "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                        "from_status, to_status, detail, created_at) VALUES (?, ?, ?, "
                        "'reopen', ?, 'pending', ?, ?)",
                        (review_id, actor.user_id, actor.role, review["status"],
                         json.dumps({"invalidated_tests": invalidated},
                                    ensure_ascii=False, sort_keys=True), now),
                    )
                self.supersede_loop_ledger(connection, loop_id, now, reason)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        entity = self.get_entity(loop_id)
        entity["invalidated_tests"] = invalidated
        return entity

    def insert_test(self, test_id, data, actor):
        """登记试验；通过的试验自动关闭该回路的待复核单。"""
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        review_id = "review-" + data["loop_id"]
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, "
                "created_at, updated_at) VALUES (?, 'test_record', 'recorded', 1, ?, ?, ?, ?)",
                (test_id, payload, actor.user_id, now, now),
            )
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
                "to_status, detail, created_at) VALUES (?, ?, ?, 'create', NULL, 'recorded', ?, ?)",
                (test_id, actor.user_id, actor.role,
                 json.dumps({"loop_id": data["loop_id"], "passed": data["passed"]},
                            ensure_ascii=False, sort_keys=True), now),
            )
            if data.get("passed"):
                review = connection.execute(
                    "SELECT * FROM entities WHERE id = ? AND status = 'pending'", (review_id,)
                ).fetchone()
                if review:
                    connection.execute(
                        "UPDATE entities SET status = 'resolved', version = version + 1, "
                        "updated_at = ? WHERE id = ?",
                        (now, review_id),
                    )
                    connection.execute(
                        "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                        "from_status, to_status, detail, created_at) VALUES (?, ?, ?, "
                        "'resolve', 'pending', 'resolved', ?, ?)",
                        (review_id, actor.user_id, actor.role,
                         json.dumps({"test_id": test_id}, ensure_ascii=False), now),
                    )
        return self.get_entity(test_id)

    def invalidate_tests_for_change(self, change_id, per_loop, actor, reason):
        """定值版本一改：该变更所涉回路上，指纹对不上（或针对旧变更）的试验立即失效。"""
        now = utcnow()
        invalidated = {}
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            tests = connection.execute(
                "SELECT * FROM entities WHERE kind = 'test_record' AND status != 'invalid'"
            ).fetchall()
            for test in tests:
                test_data = json.loads(test["data"])
                loop_id = test_data.get("loop_id")
                if loop_id not in per_loop:
                    continue
                recorded = test_data.get("basis_setpoint_fingerprint")
                recorded = tuple(tuple(x) if isinstance(x, list) else x for x in (recorded or ()))
                if str(test_data.get("basis_change_id")) == str(change_id) \
                        and recorded == per_loop[loop_id]:
                    continue
                connection.execute(
                    "UPDATE entities SET status = 'invalid', updated_at = ? WHERE id = ?",
                    (now, test["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                    "from_status, to_status, detail, created_at) VALUES (?, ?, ?, "
                    "'invalidate', ?, 'invalid', ?, ?)",
                    (test["id"], actor.user_id, actor.role, test["status"],
                     json.dumps({"reason": reason}, ensure_ascii=False), now),
                )
                invalidated.setdefault(loop_id, []).append(test["id"])
            for loop_id, test_ids in invalidated.items():
                review_id = "review-" + loop_id
                review = connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (review_id,)
                ).fetchone()
                if review is None:
                    connection.execute(
                        "INSERT INTO entities(id, kind, status, version, data, created_by, "
                        "created_at, updated_at) VALUES (?, 'review_task', 'pending', 1, ?, ?, ?, ?)",
                        (review_id, json.dumps(
                            {"loop_id": loop_id, "reason": reason,
                             "invalidated_tests": test_ids},
                            ensure_ascii=False, sort_keys=True),
                         "system", now, now),
                    )
                    connection.execute(
                        "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                        "from_status, to_status, detail, created_at) VALUES (?, 'system', "
                        "'system', 'create', NULL, 'pending', ?, ?)",
                        (review_id, json.dumps({"loop_id": loop_id}, ensure_ascii=False), now),
                    )
                elif review["status"] != "pending":
                    connection.execute(
                        "UPDATE entities SET status = 'pending', version = version + 1, "
                        "data = ?, updated_at = ? WHERE id = ?",
                        (json.dumps({"loop_id": loop_id, "reason": reason,
                                     "invalidated_tests": test_ids},
                                    ensure_ascii=False, sort_keys=True), now, review_id),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return invalidated

    def refresh_batch_status(self, batch_id):
        """按明细表重算批次状态：有未完成→open；全挡住→blocked；全 done→activated。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._refresh_batch_status_locked(connection, batch_id)
            row = connection.execute(
                "SELECT status FROM entities WHERE id = ?", (batch_id,)
            ).fetchone()
            target = row["status"] if row else None
            connection.commit()
        return target

    def list_ledger(self, loop_id=None, status=None, change_id=None):
        clauses, params = [], []
        if loop_id:
            clauses.append("loop_id = ?")
            params.append(loop_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if change_id:
            clauses.append("change_id = ?")
            params.append(change_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger" + where + " ORDER BY id", params
            ).fetchall()
        return [self._ledger_from_row(row) for row in rows]

    def active_ledger_map(self, loop_ids=None):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger WHERE status = 'active'"
            ).fetchall()
        result = {}
        for row in rows:
            if loop_ids is None or row["loop_id"] in loop_ids:
                result[row["loop_id"]] = self._ledger_from_row(row)
        return result

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
