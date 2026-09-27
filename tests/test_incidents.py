import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import ANOMALY_LABELS, STATES, TRANSITION_ROLES


class DosimeterIncidentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "2026-01 监测周期", "description": "月度个人剂量",
            "severity": "low", "quantity": 1.5, "threshold": 10,
            "external_ref": "DM-1", "dosimeter_no": "D-1001",
            "wear_period": "2026-01",
        }, "monitor_a", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, **overrides):
        payload = {"dosimeter_no": "D-1001", "wear_period": "2026-01",
                   "anomaly_type": "lost"}
        payload.update(overrides)
        return self.service.register_incident(
            self.item["id"], payload, "monitor_a", "dosimetrist")

    def _to_follow_up(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_excludes_original_reading_from_annual_total(self):
        detail = self._register()
        self.assertEqual(detail["status"], "open")
        self.assertEqual(detail["anomaly_label"], ANOMALY_LABELS["lost"])
        self.assertIsNotNone(detail["block_reason"])
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["dose_source"], "原读数已退出累计")
        self.assertEqual(item["effective_dose"], 0.0)
        self.assertIn("坏损剂量计补测未完成", item["block_reasons"][0])
        totals = self.service.annual_totals("viewer", "2026")
        self.assertEqual(totals["total"], 0.0)
        self.assertEqual(totals["readings"][0]["original_dose"], 1.5)

    def test_remeasure_by_another_monitor_writes_back_replacement(self):
        incident = self._register(anomaly_type="malfunction")
        with self.assertRaises(ConflictError):
            self.service.remeasure_incident(incident["id"], {
                "replacement_dose": 1.8, "spare_dosimeter_no": "S-200",
                "expected_version": incident["version"],
            }, "monitor_a", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.remeasure_incident(incident["id"], {
                "replacement_dose": 1.8, "spare_dosimeter_no": "D-1001",
                "expected_version": incident["version"],
            }, "monitor_b", "dosimetrist")
        resolved = self.service.remeasure_incident(incident["id"], {
            "replacement_dose": 1.8, "spare_dosimeter_no": "S-200",
            "expected_version": incident["version"],
        }, "monitor_b", "dosimetrist")
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNone(resolved["block_reason"])
        self.assertEqual(resolved["verified_by"], "monitor_b")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["dose_source"], "备用剂量计补测")
        self.assertEqual(item["replacement_dose"], 1.8)
        self.assertEqual(item["effective_dose"], 1.8)
        self.assertEqual(item["quantity"], 1.5)
        self.assertEqual(item["block_reasons"], [])
        self.assertEqual(self.service.annual_totals("viewer", "2026")["total"], 1.8)

    def test_only_one_open_incident_per_period_and_dosimeter(self):
        self._register()
        with self.assertRaises(ConflictError):
            self._register()
        another = self.service.create_item({
            "title": "同编号另一周期", "description": "x", "severity": "low",
            "quantity": 0.9, "threshold": 10, "external_ref": "DM-2",
            "dosimeter_no": "D-1001", "wear_period": "2026-02",
        }, "monitor_a", "dosimetrist")
        second = self.service.register_incident(another["id"], {
            "dosimeter_no": "D-1001", "wear_period": "2026-02",
            "anomaly_type": "not_returned",
        }, "monitor_a", "dosimetrist")
        self.assertEqual(second["status"], "open")
        incidents = self.service.list_incidents("viewer", "open")
        self.assertEqual(len(incidents), 2)

    def test_unfinished_incident_blocks_close(self):
        self._register()
        current = self._to_follow_up()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                current["id"], STATES[-1], current["version"], "reviewer",
                TRANSITION_ROLES[STATES[-1]][0])
        self.assertIn("坏损剂量计补测未完成", str(ctx.exception))

    def test_case_can_close_after_remeasure(self):
        incident = self._register()
        self.service.remeasure_incident(incident["id"], {
            "replacement_dose": 1.8, "spare_dosimeter_no": "S-200",
            "expected_version": 1,
        }, "monitor_b", "radiation_officer")
        current = self._to_follow_up()
        closed = self.service.transition(
            current["id"], STATES[-1], current["version"], "reviewer",
            TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], "closed")

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_incident(self.item["id"], {
                "dosimeter_no": "D-1001", "wear_period": "2026-01",
                "anomaly_type": "lost",
            }, "viewer_a", "viewer")
        incident = self._register()
        with self.assertRaises(PermissionDenied):
            self.service.remeasure_incident(incident["id"], {
                "replacement_dose": 1.8, "spare_dosimeter_no": "S-200",
                "expected_version": 1,
            }, "viewer_a", "viewer")
        with self.assertRaises(ValidationError):
            self._register(anomaly_type="broken")
        with self.assertRaises(ValidationError):
            self._register(wear_period="2026/01")
        with self.assertRaises(ConflictError):
            self._register(dosimeter_no="D-9999")

    def test_version_conflict_when_remeasuring(self):
        first = self._register()
        with self.assertRaises(ConflictError):
            self.service.remeasure_incident(first["id"], {
                "replacement_dose": 1.8, "spare_dosimeter_no": "S-200",
                "expected_version": 99,
            }, "monitor_b", "dosimetrist")

    def test_audit_chain_preserved_and_views_available(self):
        incident = self._register()
        self.service.remeasure_incident(incident["id"], {
            "replacement_dose": 1.8, "spare_dosimeter_no": "S-200",
            "expected_version": 1,
        }, "monitor_b", "dosimetrist")
        self.assertTrue(self.repo.verify_audit_chain())
        actions = [e["action"] for e in self.service.audit("health_physicist")]
        self.assertIn("dosimeter_incident_register", actions)
        self.assertIn("dosimeter_incident_remeasure", actions)
        item_incidents = self.service.list_incidents_for_item(
            self.item["id"], "viewer")
        self.assertEqual(len(item_incidents), 1)
        detail = self.service.get_incident(incident["id"], "viewer")
        self.assertEqual(detail["replacement_dose"], 1.8)
        self.assertEqual(detail["spare_dosimeter_no"], "S-200")


if __name__ == "__main__":
    unittest.main()
