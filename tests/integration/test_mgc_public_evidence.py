"""The public research sample is reproducible offline and stays in quarantine."""

import unittest

from scripts.mgc_public_data import verify


class PublicMgcEvidenceTests(unittest.TestCase):
    def test_public_source_evidence_and_unresolved_requirements(self):
        report = verify()
        self.assertFalse(report["execution_eligible"])
        self.assertEqual(len(report["contracts"]), 3)
        self.assertEqual(len(report["common_observed_labels"]), 201)
        for item in report["contracts"].values():
            self.assertEqual(item["rows"], 201)
            self.assertEqual(item["first_label"], "2026-01-02")
            self.assertEqual(item["last_label"], "2026-10-20")
            self.assertFalse(item["expected_sessions_verified"])
        self.assertEqual({v["http_status"] for v in report["availability_probes"].values()}, {404})
