from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# ---------------------------------------------------------------------------
# 生效账 (effective ledger) — instrument loop recompute rules
# ---------------------------------------------------------------------------

# Alarm / trip direction. "high" trips on a rising value, "low" on a falling one.
VALID_DIRECTIONS = ("high", "low")

# Failure codes produced by the commissioning recompute.
RANGE_EXCEEDED = "range_exceeded"
HYSTERESIS_DIRECTION = "hysteresis_direction"
TEST_MISSING = "test_missing"


def _loop_data(loop):
    return loop.get("data", {}) if isinstance(loop, dict) else {}


def _failure(loop, code, message):
    data = _loop_data(loop)
    return {
        "loop_id": loop.get("id"),
        "tag": data.get("tag"),
        "code": code,
        "message": message,
    }


def check_range(loop):
    """量程盖不住：setpoint / alarm limit must sit inside the instrument range."""
    data = _loop_data(loop)
    rmin = data.get("range_min")
    rmax = data.get("range_max")
    if rmin is None or rmax is None:
        return [_failure(loop, RANGE_EXCEEDED, "loop has no instrument range registered")]
    failures = []
    setpoint = data.get("setpoint")
    if setpoint is not None and not (rmin <= setpoint <= rmax):
        failures.append(
            _failure(
                loop,
                RANGE_EXCEEDED,
                "setpoint %s is outside range [%s, %s]" % (setpoint, rmin, rmax),
            )
        )
    alarm_limit = data.get("alarm_limit")
    if alarm_limit is not None and not (rmin <= alarm_limit <= rmax):
        failures.append(
            _failure(
                loop,
                RANGE_EXCEEDED,
                "alarm limit %s is outside range [%s, %s]" % (alarm_limit, rmin, rmax),
            )
        )
    return failures


def check_hysteresis(loop):
    """回差方向不对：hysteresis must be positive and reset on the correct side.

    For a high-direction loop the value trips on rising and resets on falling
    below ``setpoint - hysteresis``; for a low-direction loop it trips on
    falling and resets on rising above ``setpoint + hysteresis``. The reset
    point must stay inside the instrument range.
    """
    data = _loop_data(loop)
    direction = data.get("alarm_direction", "high")
    hysteresis = data.get("hysteresis")
    rmin = data.get("range_min")
    rmax = data.get("range_max")
    setpoint = data.get("setpoint")
    if hysteresis is None or hysteresis <= 0:
        return [
            _failure(
                loop,
                HYSTERESIS_DIRECTION,
                "hysteresis must be a positive value for %s-direction loops" % direction,
            )
        ]
    if setpoint is None or rmin is None or rmax is None:
        return []
    if direction == "high":
        reset = setpoint - hysteresis
        if reset < rmin:
            return [
                _failure(
                    loop,
                    HYSTERESIS_DIRECTION,
                    "high-direction reset point %s is below range floor %s" % (reset, rmin),
                )
            ]
    else:
        reset = setpoint + hysteresis
        if reset > rmax:
            return [
                _failure(
                    loop,
                    HYSTERESIS_DIRECTION,
                    "low-direction reset point %s is above range ceiling %s" % (reset, rmax),
                )
            ]
    return []


def check_test(loop, tests):
    """试验未过：a valid, passed test for the current setpoint/instrument versions."""
    data = _loop_data(loop)
    sp_ver = data.get("setpoint_version", 1)
    inst_ver = data.get("instrument_version", 1)
    for test in tests or []:
        td = _loop_data(test)
        if (
            td.get("status") == "valid"
            and td.get("result") == "passed"
            and td.get("tested_setpoint_version") == sp_ver
            and td.get("tested_instrument_version") == inst_ver
        ):
            return []
    return [
        _failure(
            loop,
            TEST_MISSING,
            "no valid passed test for setpoint v%s / instrument v%s" % (sp_ver, inst_ver),
        )
    ]


def recompute_commission(loops, tests_by_loop):
    """Recompute commissioning readiness per loop against current config + tests.

    ``tests_by_loop`` maps a loop id to its list of test records. Returns
    ``(passed, failures)``; ``failures`` is ordered per loop so the caller can
    name the specific loop(s) that block the submit.
    """
    failures = []
    for loop in loops:
        tests = tests_by_loop.get(loop.get("id"), [])
        failures.extend(check_range(loop))
        failures.extend(check_hysteresis(loop))
        failures.extend(check_test(loop, tests))
    return (not failures), failures


def _validate_loop_create(actor, data, lookup):
    tag = data.get("tag")
    if not tag:
        raise ValidationError("loop tag is required")
    if data.get("alarm_direction") not in VALID_DIRECTIONS:
        raise ValidationError("alarm_direction must be one of %s" % (VALID_DIRECTIONS,))
    existing = lookup("loop", "tag", tag)
    if existing:
        raise ConflictError("loop tag already exists: " + tag)


def _validate_loop_test_create(actor, data, lookup):
    result = data.get("result")
    if result not in ("passed", "failed"):
        raise ValidationError("test result must be 'passed' or 'failed'")


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    # 生效账 gate: when a change touches instrument loops, every loop must
    # pass the per-loop recompute (range / hysteresis / valid test). A change
    # with no loops is unaffected for backward compatibility.
    if lookup:
        loops = lookup("loop", "change_id", entity["id"]) or []
        if loops:
            tests_by_loop = {
                loop["id"]: (lookup("loop_test", "loop_id", loop["id"]) or []) for loop in loops
            }
            passed, failures = recompute_commission(loops, tests_by_loop)
            if not passed:
                tags = ", ".join(str(f.get("tag") or f.get("loop_id")) for f in failures)
                raise ValidationError("commission blocked, loops not ready: " + tags)
    return {"commissioned_by": actor.user_id}


CUSTOM_CREATE = {
    'change': _validate_change,
    'loop': _validate_loop_create,
    'loop_test': _validate_loop_test_create,
}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'commission'): _validate_commission}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item', 'loops': 'loop', 'loop_tests': 'loop_test', 'review_items': 'review_item', 'commission_batches': 'commission_batch'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open', 'loop': 'active', 'loop_test': 'valid', 'review_item': 'open', 'commission_batch': 'in_progress'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner'), 'loop': ('tag', 'range_min', 'range_max', 'setpoint', 'alarm_direction', 'hysteresis', 'change_id'), 'loop_test': ('loop_id', 'result', 'tested_by', 'evidence'), 'review_item': ('loop_id', 'reason'), 'commission_batch': ('change_id',)}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety'), 'loop': ('admin', 'engineer'), 'loop_test': ('admin', 'engineer', 'verifier'), 'review_item': ('admin', 'engineer', 'safety'), 'commission_batch': ('admin', 'engineer')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
