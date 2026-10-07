import unittest

from src.rules import (
    ALARM_DIRECTION_INVALID,
    HYSTERESIS_DIRECTION,
    RANGE_COVER,
    check_range_and_direction,
    required_approval_level,
    setpoint_fingerprint,
    test_matches,
)
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


def _loop(low, high, instrument="v1"):
    return {"id": "L1", "status": "in_service", "kind": "instrument_loop",
            "data": {"range_low": low, "range_high": high,
                     "instrument_version": instrument}}


class RangeRulesTest(unittest.TestCase):
    def test_range_must_cover_limits(self):
        entry = {"direction": "high", "upper_limit": 90, "hysteresis": 0}
        self.assertEqual(check_range_and_direction(_loop(0, 100), entry), None)
        self.assertEqual(check_range_and_direction(_loop(0, 50), entry), RANGE_COVER)

    def test_high_alarm_hysteresis_lives_below(self):
        good = {"direction": "high", "upper_limit": 80, "hysteresis": 5}
        bad = {"direction": "high", "upper_limit": 80, "hysteresis": 90}
        self.assertEqual(check_range_and_direction(_loop(0, 100), good), None)
        self.assertEqual(check_range_and_direction(_loop(0, 100), bad),
                         HYSTERESIS_DIRECTION)

    def test_low_alarm_hysteresis_lives_above(self):
        good = {"direction": "low", "lower_limit": 20, "hysteresis": 5}
        bad = {"direction": "low", "lower_limit": 20, "hysteresis": 95}
        self.assertEqual(check_range_and_direction(_loop(0, 100), good), None)
        self.assertEqual(check_range_and_direction(_loop(0, 100), bad),
                         HYSTERESIS_DIRECTION)

    def test_both_direction_and_crossing(self):
        ok = {"direction": "both", "upper_limit": 80, "lower_limit": 20,
              "hysteresis": 5}
        crossed = {"direction": "both", "upper_limit": 30, "lower_limit": 20,
                   "hysteresis": 15}
        self.assertEqual(check_range_and_direction(_loop(0, 100), ok), None)
        self.assertEqual(check_range_and_direction(_loop(0, 100), crossed),
                         HYSTERESIS_DIRECTION)
        self.assertEqual(
            check_range_and_direction(_loop(0, 100),
                                      {"direction": "sideways", "upper_limit": 1}),
            ALARM_DIRECTION_INVALID)

    def test_fingerprint_changes_invalidate_match(self):
        fp = setpoint_fingerprint([{"direction": "high", "upper_limit": 80.0,
                                    "hysteresis": 2.0}])
        test = {"id": "T1", "status": "recorded",
                "data": {"loop_id": "L1", "basis_change_version": 4,
                         "basis_instrument_version": "v1",
                         "basis_setpoint_fingerprint": list(fp)}}
        self.assertTrue(test_matches(test, "L1", fp, 4, "v1"))
        self.assertFalse(test_matches(test, "L1", fp, 5, "v1"))
        self.assertFalse(test_matches(test, "L1", fp, 4, "v2"))
        changed = setpoint_fingerprint([{"direction": "high", "upper_limit": 81.0,
                                         "hysteresis": 2.0}])
        self.assertFalse(test_matches(test, "L1", changed, 4, "v1"))


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(required_approval_level("low"), 1)
        self.assertEqual(required_approval_level("high"), 3)
        self.assertEqual(required_approval_level("unknown"), 4)
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "change", "status": "assessed", "id": "c1", "data": {"required_approvals": 2}}, "approve", {"approvals": ["one"], "permit_id": "p"})


if __name__ == "__main__":
    unittest.main()
