from copy import deepcopy
import unittest

from market_catalog.futures_research import require_individual_contract, rollover_session


class FuturesResearchTests(unittest.TestCase):
    def contract(self):
        return {"instrument_id": "SYNTH-2026-12", "provider_security_id": "synthetic-security",
                "first_notice_date": "2026-11-30", "last_trade_date": "2026-12-29",
                "broker_closeout_date": "2026-11-27", "exchange_calendar_verified": True, "is_continuous": False}

    def test_roll_is_before_earliest_cutoff_on_the_supplied_calendar(self):
        # Synthetic calendar intentionally omits a weekday: do not synthesize it.
        sessions = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24",
                    "2026-11-25", "2026-11-27", "2026-11-30"]
        self.assertEqual(rollover_session(self.contract(), sessions), "2026-11-19")

    def test_unknown_notices_provider_and_continuous_aliases_are_rejected(self):
        for field, value in (("first_notice_date", None), ("provider_security_id", None),
                             ("exchange_calendar_verified", False), ("is_continuous", True), ("instrument_id", "MGC=F")):
            contract = deepcopy(self.contract())
            contract[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                require_individual_contract(contract)

    def test_missing_short_or_unordered_calendars_are_rejected(self):
        for sessions in ([], ["2026-11-30", "2026-11-25"], ["2026-11-25", "2026-11-25", "2026-11-30"],
                         ["2026-11-25", "2026-11-30"], ["2026-11-18", "2026-11-19"]):
            with self.subTest(sessions=sessions), self.assertRaises(ValueError):
                rollover_session(self.contract(), sessions)
