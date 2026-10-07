from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 报警方向
HIGH = "high"   # 高报：达到上限报警，回差在下侧（开关点 = 上限 - 回差）
LOW = "low"     # 低报：达到下限报警，回差在上侧（开关点 = 下限 + 回差）
BOTH = "both"   # 双限：上限高报、下限低报
DIRECTIONS = (HIGH, LOW, BOTH)

# 投产逐项复算的问题码
CHANGE_NOT_IMPLEMENTED = "CHANGE_NOT_IMPLEMENTED"   # 变更单尚未实施
NO_SETPOINT = "NO_SETPOINT"                         # 变更单上没有该回路的定值条目
RANGE_MISSING = "RANGE_MISSING"                     # 回路未登记量程
RANGE_COVER = "RANGE_COVER"                         # 量程盖不住定值
ALARM_DIRECTION_INVALID = "ALARM_DIRECTION_INVALID"  # 报警方向非法/与限值不一致
HYSTERESIS_DIRECTION = "HYSTERESIS_DIRECTION"       # 回差方向不对（开关点越界）
NO_PASSED_TEST = "NO_PASSED_TEST"                   # 没有针对当前版本的通过试验
TEST_FAILED = "TEST_FAILED"                         # 最新有效试验结论不合格
TEST_STALE = "TEST_STALE"                           # 试验针对旧版本，已失效
ALREADY_ACTIVE = "ALREADY_ACTIVE"                   # 该回路已有同一笔生效账

ISSUE_TEXT = {
    CHANGE_NOT_IMPLEMENTED: "变更单未处于已实施状态，不能作为投产依据",
    NO_SETPOINT: "变更单上没有该回路的定值条目",
    RANGE_MISSING: "回路未登记仪表量程",
    RANGE_COVER: "量程盖不住报警限值",
    ALARM_DIRECTION_INVALID: "报警方向非法或与上/下限不一致",
    HYSTERESIS_DIRECTION: "回差方向不对，报警/复位开关点越过量程或限值",
    NO_PASSED_TEST: "没有针对当前定值/仪表版本的通过试验",
    TEST_FAILED: "最新有效试验结论为未通过",
    TEST_STALE: "试验依据的定值或仪表版本已变更，试验失效",
    ALREADY_ACTIVE: "该回路已由另一笔投产提交生效",
}


def issue(code, loop_id, **extra):
    data = {"loop_id": loop_id, "code": code, "message": ISSUE_TEXT[code]}
    data.update(extra)
    return data


def _num(data, field, label):
    value = data.get(field)
    if value is None or value == "":
        raise ValidationError("%s is required" % label)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % label)


def _setpoint_entry(data, field="setpoints"):
    """变更单可以带一条或多条定值条目；取与回路匹配的那条。"""
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, dict):
        entries = [value]
    elif isinstance(value, list):
        entries = value
    else:
        raise ValidationError("setpoints must be an object or a list")
    normalized = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValidationError("each setpoint entry must be an object")
        entry = dict(item)
        direction = entry.get("direction")
        if direction not in DIRECTIONS:
            raise ValidationError("setpoint direction must be one of %s" % (DIRECTIONS,))
        if "hysteresis" in entry and entry["hysteresis"] not in (None, ""):
            entry["hysteresis"] = _num(entry, "hysteresis", "hysteresis")
            if entry["hysteresis"] < 0:
                raise ValidationError("hysteresis must be >= 0")
        else:
            entry["hysteresis"] = 0.0
        upper = entry.get("upper_limit")
        lower = entry.get("lower_limit")
        if upper not in (None, ""):
            entry["upper_limit"] = float(upper)
        if lower not in (None, ""):
            entry["lower_limit"] = float(lower)
        if direction in (HIGH, BOTH) and entry.get("upper_limit") is None:
            raise ValidationError("direction %s requires upper_limit" % direction)
        if direction in (LOW, BOTH) and entry.get("lower_limit") is None:
            raise ValidationError("direction %s requires lower_limit" % direction)
        if (
            entry.get("upper_limit") is not None
            and entry.get("lower_limit") is not None
            and entry["lower_limit"] >= entry["upper_limit"]
        ):
            raise ValidationError("lower_limit must be below upper_limit")
        normalized.append(entry)
    return normalized


def setpoint_fingerprint(entries):
    """定值指纹：值或回差、方向任何一个变了，旧试验立即失效。"""
    result = []
    for entry in sorted(entries or [], key=lambda item: item.get("direction", "")):
        result.append((
            entry.get("direction"),
            entry.get("upper_limit"),
            entry.get("lower_limit"),
            float(entry.get("hysteresis") or 0.0),
        ))
    return tuple(sorted(result))


def _setpoint_for_loop(entries, loop_id):
    for entry in entries or []:
        if entry.get("loop_id") in (None, "", loop_id):
            return entry
    return None


# ---------------------------------------------------------------- 通用对象

def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")
    _setpoint_entry(data)  # 结构合法性，跨回路取值在投产复算时做


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
    return {"commissioned_by": actor.user_id}


def _validate_loop(actor, data, lookup):
    _num(data, "range_low", "range_low")
    _num(data, "range_high", "range_high")
    low = float(data["range_low"])
    high = float(data["range_high"])
    if low >= high:
        raise ValidationError("range_low must be below range_high")
    if not str(data.get("instrument_version") or "").strip():
        raise ValidationError("instrument_version is required")
    if not str(data.get("tag") or "").strip():
        raise ValidationError("tag is required")


def _validate_test(actor, data, lookup):
    loop = _find_one(lookup, "instrument_loop", "id", data.get("loop_id"))
    if not loop:
        raise ValidationError("loop does not exist")
    if not isinstance(data.get("passed"), bool):
        raise ValidationError("passed must be true or false")
    if not str(data.get("basis_change_version") or "").strip():
        raise ValidationError("basis_change_version is required")
    if not str(data.get("basis_instrument_version") or "").strip():
        raise ValidationError("basis_instrument_version is required")


def _validate_batch(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    loop_ids = data.get("loop_ids") or []
    if not isinstance(loop_ids, list) or not loop_ids:
        raise ValidationError("loop_ids must be a non-empty list")
    loops = {item["id"]: item for item in (lookup("instrument_loop") or [])}
    missing = [loop_id for loop_id in loop_ids if loop_id not in loops]
    if missing:
        raise ValidationError("unknown loops: " + ", ".join(missing))
    return {"loops": loop_ids}


CUSTOM_CREATE = {
    'change': _validate_change,
    'instrument_loop': _validate_loop,
    'test_record': _validate_test,
    'activation_batch': _validate_batch,
}
CUSTOM_TRANSITIONS = {
    ('change', 'assess'): _validate_assess,
    ('change', 'approve'): _validate_approve,
    ('change', 'commission'): _validate_commission,
}


class RuleEngine:
    ALIASES = {
        'units': 'unit',
        'changes': 'change',
        'action_items': 'action_item',
        'loops': 'instrument_loop',
        'instrument_loops': 'instrument_loop',
        'tests': 'test_record',
        'test_records': 'test_record',
        'reviews': 'review_task',
        'review_tasks': 'review_task',
        'batches': 'activation_batch',
        'activation_batches': 'activation_batch',
    }
    INITIAL_STATUS = {
        'unit': 'operating',
        'change': 'draft',
        'action_item': 'open',
        'instrument_loop': 'in_service',
        'test_record': 'recorded',
        'review_task': 'pending',
        'activation_batch': 'open',
    }
    TRANSITIONS = {
        'unit': {
            'shutdown': (('operating',), 'shutdown'),
            'startup': (('shutdown',), 'operating'),
            'freeze': (('operating',), 'frozen'),
            'unfreeze': (('frozen',), 'operating'),
        },
        'change': {
            'assess': (('draft',), 'assessed'),
            'approve': (('assessed',), 'approved'),
            'implement': (('approved',), 'implemented'),
            'revise': (('draft',), 'draft'),
            'commission': (('implemented',), 'commissioned'),
            'rollback': (('implemented', 'commissioned'), 'rolled_back'),
            'close': (('rolled_back',), 'closed'),
        },
        'action_item': {
            'complete': (('open',), 'completed'),
            'verify': (('completed',), 'verified'),
            'reopen': (('verified',), 'open'),
        },
        'instrument_loop': {
            'revise_instrument': (('in_service',), 'in_service'),
            'replace': (('in_service', 'retired'), 'in_service'),
            'retire': (('in_service',), 'retired'),
        },
        'test_record': {
            'invalidate': (('recorded', 'passed', 'failed'), 'invalid'),
        },
        'review_task': {
            'resolve': (('pending',), 'resolved'),
        },
        'activation_batch': {
            'submit': (('open', 'blocked'), 'submitting'),
        },
    }
    CREATE_REQUIRED = {
        'unit': ('name', 'location'),
        'change': ('unit_id', 'description'),
        'action_item': ('change_id', 'description', 'owner'),
        'instrument_loop': ('tag', 'range_low', 'range_high', 'instrument_version'),
        'test_record': ('loop_id', 'passed', 'basis_change_version', 'basis_instrument_version'),
        'activation_batch': ('change_id', 'loop_ids'),
    }
    ACTION_REQUIRED = {
        ('unit', 'shutdown'): ('reason',),
        ('unit', 'freeze'): ('reason',),
        ('change', 'assess'): ('risk_level', 'analyst'),
        ('change', 'approve'): ('approvals', 'permit_id'),
        ('change', 'implement'): ('procedure_version',),
        ('change', 'revise'): ('setpoints',),
        ('change', 'commission'): ('tests_passed',),
        ('change', 'rollback'): ('reason',),
        ('change', 'close'): ('outcome',),
        ('action_item', 'complete'): ('completed_by', 'evidence'),
        ('action_item', 'verify'): ('verifier',),
        ('action_item', 'reopen'): ('reason',),
        ('instrument_loop', 'revise_instrument'): ('instrument_version',),
        ('test_record', 'invalidate'): ('reason',),
        ('review_task', 'resolve'): ('resolved_by',),
        ('activation_batch', 'submit'): (),
    }
    CREATE_ROLES = {
        'unit': ('admin', 'engineer'),
        'change': ('admin', 'engineer'),
        'action_item': ('admin', 'safety'),
        'instrument_loop': ('admin', 'engineer'),
        'test_record': ('admin', 'engineer', 'verifier'),
        'activation_batch': ('admin', 'engineer'),
    }
    ROLE_ACTIONS = {
        'shutdown': ('admin', 'operator'),
        'startup': ('admin', 'operator'),
        'freeze': ('admin', 'operator'),
        'unfreeze': ('admin', 'operator'),
        'assess': ('admin', 'engineer'),
        'approve': ('admin', 'safety'),
        'implement': ('admin', 'engineer'),
        'revise': ('admin', 'engineer'),
        'commission': ('admin', 'engineer'),
        'rollback': ('admin', 'engineer'),
        'close': ('admin', 'safety'),
        'complete': ('admin', 'engineer'),
        'verify': ('admin', 'verifier'),
        'reopen': ('admin', 'verifier'),
        'revise_instrument': ('admin', 'engineer'),
        'replace': ('admin', 'engineer'),
        'retire': ('admin', 'engineer'),
        'invalidate': ('admin', 'engineer', 'verifier'),
        'resolve': ('admin', 'engineer', 'verifier'),
        'submit': ('admin', 'engineer'),
    }

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
        if kind == 'review_task':
            # 待复核任务由系统在版本失效时生成，不允许手工建账
            raise PermissionDenied("review_task is system-managed")
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        extra = custom(actor, data, lookup) if custom else None
        payload = dict(data)
        if isinstance(extra, dict):
            payload.update(extra)
        return payload

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


# ---------------------------------------------------------------- 投产复算

def check_range_and_direction(loop, entry):
    """量程覆盖 + 回差方向检查；返回问题码或 None。"""
    range_low = loop["data"].get("range_low")
    range_high = loop["data"].get("range_high")
    if range_low is None or range_high is None:
        return RANGE_MISSING
    range_low, range_high = float(range_low), float(range_high)
    direction = entry.get("direction")
    if direction not in DIRECTIONS:
        return ALARM_DIRECTION_INVALID
    upper = entry.get("upper_limit")
    lower = entry.get("lower_limit")
    if direction in (HIGH, BOTH) and upper is None:
        return ALARM_DIRECTION_INVALID
    if direction in (LOW, BOTH) and lower is None:
        return ALARM_DIRECTION_INVALID
    if upper is not None and not (range_low <= float(upper) <= range_high):
        return RANGE_COVER
    if lower is not None and not (range_low <= float(lower) <= range_high):
        return RANGE_COVER
    hysteresis = float(entry.get("hysteresis") or 0.0)
    # 高报回差在报警点下侧，复位（开关）点 = 上限 - 回差；低报相反。
    # 开关点必须留在量程内，且不能越过对侧限值——否则回差方向装反。
    if direction in (HIGH, BOTH):
        reset = float(upper) - hysteresis
        if not (range_low <= reset <= range_high):
            return HYSTERESIS_DIRECTION
        if lower is not None and reset < float(lower):
            return HYSTERESIS_DIRECTION
    if direction in (LOW, BOTH):
        reset = float(lower) + hysteresis
        if not (range_low <= reset <= range_high):
            return HYSTERESIS_DIRECTION
        if upper is not None and reset > float(upper):
            return HYSTERESIS_DIRECTION
    return None


def test_matches(test, loop_id, change_fingerprint, change_version, instrument_version):
    """试验是否仍针对当前定值与仪表版本。"""
    data = test["data"]
    if data.get("loop_id") != loop_id:
        return False
    if str(data.get("basis_instrument_version")) != str(instrument_version):
        return False
    if str(data.get("basis_change_version")) != str(change_version):
        return False
    recorded = data.get("basis_setpoint_fingerprint")
    if recorded is not None and tuple(tuple(item) if isinstance(item, list) else item
                                      for item in recorded) != change_fingerprint:
        return False
    return True


def evaluate_loop(loop_id, loops_by_id, changes_by_id, tests, current_active=None,
                  change_id=None):
    """对单条回路按当前变更与回路数据逐项复算。

    tests: 该回路的全部试验记录；current_active: 已生效账（loop_id -> row）。
    change_id 优先取投产提交所挂的当前变更，缺省再看回路登记的依据。
    """
    loop = loops_by_id.get(loop_id)
    if not loop:
        return issue(RANGE_MISSING, loop_id)

    change_id = change_id or loop["data"].get("basis_change_id")
    change = changes_by_id.get(change_id)
    if not change or change["status"] != "implemented":
        return issue(CHANGE_NOT_IMPLEMENTED, loop_id)

    entries = _setpoint_entry(change["data"]) or []
    entry = _setpoint_for_loop(entries, loop_id)
    if entry is None:
        return issue(NO_SETPOINT, loop_id)

    code = check_range_and_direction(loop, entry)
    if code:
        return issue(
            code, loop_id,
            range=[loop["data"].get("range_low"), loop["data"].get("range_high")],
            upper_limit=entry.get("upper_limit"),
            lower_limit=entry.get("lower_limit"),
            direction=entry.get("direction"),
        )

    fingerprint = setpoint_fingerprint([entry])
    matching = [
        t for t in tests
        if t["status"] != "invalid"
        and test_matches(t, loop_id, fingerprint, change["version"],
                         loop["data"]["instrument_version"])
    ]
    active = current_active or {}
    active_row = active.get(loop_id)
    if not matching:
        # 有针对该回路的旧试验（已失效或版本对不上）-> 明确是“旧试验失效”，并点名试验单
        stale = [t for t in tests if t["data"].get("loop_id") == loop_id]
        if stale and active_row and active_row.get("change_id") == change_id \
                and str(active_row.get("change_version")) == str(change["version"]) \
                and str(active_row.get("instrument_version")) == str(loop["data"]["instrument_version"]):
            return None  # 同一依据已生效，幂等放行
        if stale:
            return issue(TEST_STALE, loop_id,
                         stale_test_id=sorted(t["id"] for t in stale)[-1])
        return issue(NO_PASSED_TEST, loop_id)

    latest = sorted(matching, key=lambda t: (t["created_at"], t["id"]))[-1]
    if not latest["data"].get("passed"):
        # 最新一次不合格，但只要存在同版本的通过试验，以最新通过为准
        passed = [t for t in matching if t["data"].get("passed")]
        if not passed:
            return issue(TEST_FAILED, loop_id, test_id=latest["id"])
    return None


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
