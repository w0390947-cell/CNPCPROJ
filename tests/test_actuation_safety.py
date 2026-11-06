import unittest

from oilfield_energy.actuation_arbiter import ActuationArbiter
from oilfield_energy.control_contracts import CapIntentAction, StationCapIntent
from oilfield_energy.hard_protection import HardCapReleaseGuard


class ActuationArbiterTests(unittest.TestCase):
    def test_absolute_caps_compose_without_double_subtraction(self) -> None:
        arbiter = ActuationArbiter()
        available = {"PV-1": 10.0}
        result = arbiter.apply(
            available,
            (
                StationCapIntent("dispatch", "PV-1", CapIntentAction.SET_CAP, 8.0),
                StationCapIntent("hard", "PV-1", CapIntentAction.SET_CAP, 6.0),
            ),
        )
        self.assertEqual(result[0].effective_cap_mw, 6.0)
        self.assertEqual(dict(result[0].owner_caps), {"dispatch": 8.0, "hard": 6.0})

        after_no_change = arbiter.apply(
            available,
            (
                StationCapIntent("dispatch", "PV-1", CapIntentAction.NO_CHANGE),
                StationCapIntent("hard", "PV-1", CapIntentAction.RELEASE_CAP),
            ),
        )
        self.assertEqual(after_no_change[0].effective_cap_mw, 8.0)

        released = arbiter.apply(
            available,
            (StationCapIntent("dispatch", "PV-1", CapIntentAction.RELEASE_CAP),),
        )
        self.assertEqual(released[0].effective_cap_mw, 10.0)

    def test_counterfactual_resolution_does_not_release_latched_owner(self) -> None:
        arbiter = ActuationArbiter()
        available = {"PV-1": 10.0}
        arbiter.apply(
            available,
            (
                StationCapIntent("dispatch", "PV-1", CapIntentAction.SET_CAP, 8.0),
                StationCapIntent("hard", "PV-1", CapIntentAction.SET_CAP, 6.0),
            ),
        )

        preview = arbiter.resolve(available, excluded_owner_ids=("hard",))
        self.assertEqual(preview[0].effective_cap_mw, 8.0)
        self.assertEqual(arbiter.owner_cap("hard", "PV-1"), 6.0)
        actual = arbiter.resolve(available)
        self.assertEqual(actual[0].effective_cap_mw, 6.0)


class HardCapReleaseGuardTests(unittest.TestCase):
    def test_release_requires_consecutive_counterfactual_safe_cycles(self) -> None:
        guard = HardCapReleaseGuard(hysteresis_mw=0.05, confirmation_cycles=2)
        self.assertFalse(guard.observe(0.19, protection_margin_mw=0.15))
        self.assertEqual(guard.confirmation_count, 0)
        self.assertFalse(guard.observe(0.20, protection_margin_mw=0.15))
        self.assertTrue(guard.observe(0.21, protection_margin_mw=0.15))

    def test_any_unsafe_counterfactual_resets_release_evidence(self) -> None:
        guard = HardCapReleaseGuard(hysteresis_mw=0.05, confirmation_cycles=2)
        self.assertFalse(guard.observe(0.21, protection_margin_mw=0.15))
        self.assertFalse(guard.observe(0.19, protection_margin_mw=0.15))
        self.assertEqual(guard.confirmation_count, 0)
