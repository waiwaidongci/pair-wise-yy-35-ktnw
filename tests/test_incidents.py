import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class IncidentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "2026Q1 个人剂量", "description": "佩戴周期剂量登记",
             "severity": "low", "quantity": 5.0, "threshold": 10,
             "external_ref": "DM-1"}, "monitor-a", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_follow_up(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_excludes_original_and_blocks_close(self):
        detail = self.service.register_incident(
            self.item["id"],
            {"dosimeter_id": "PD-001", "wearing_period": "2026-Q1",
             "anomaly_type": "lost", "original_dose": 5.0},
            "monitor-a", "dosimetrist")
        self.assertEqual(detail["dose_source"], "pending_remeasurement")
        self.assertIsNone(detail["counted_dose"])
        self.assertIsNone(detail["replacement_dose"])
        self.assertTrue(detail["original_dose_excluded"])
        self.assertEqual(detail["annual_cumulative_dose"], 0.0)
        self.assertEqual(detail["open_incident_count"], 1)
        self.assertTrue(detail["blockers"])
        self.assertIn("PD-001", detail["blockers"][0])
        incident = detail["dosimeter_incidents"][0]
        self.assertEqual(incident["status"], "open")
        self.assertTrue(incident["original_excluded"])
        self.assertEqual(incident["anomaly_label"], "遗失")

        current = self._to_follow_up()
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], STATES[-1], current["version"], "reviewer",
                TRANSITION_ROLES[STATES[-1]][0])

    def test_same_period_same_dosimeter_single_active(self):
        payload = {"dosimeter_id": "PD-002", "wearing_period": "2026-Q1",
                   "anomaly_type": "malfunction", "original_dose": 5.0}
        self.service.register_incident(self.item["id"], payload,
                                       "monitor-a", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.register_incident(self.item["id"], payload,
                                           "monitor-b", "dosimetrist")

    def test_remeasurement_requires_other_monitor_and_writes_back(self):
        detail = self.service.register_incident(
            self.item["id"],
            {"dosimeter_id": "PD-003", "wearing_period": "2026-Q1",
             "anomaly_type": "not_recovered", "original_dose": 5.0},
            "monitor-a", "dosimetrist")
        incident_id = detail["dosimeter_incidents"][0]["id"]
        version = detail["dosimeter_incidents"][0]["version"]

        with self.assertRaises(PermissionDenied):
            self.service.complete_remeasurement(
                self.item["id"], incident_id,
                {"replacement_dose": 4.2, "backup_dosimeter_id": "BK-9",
                 "expected_version": version},
                "monitor-a", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.complete_remeasurement(
                self.item["id"], incident_id,
                {"replacement_dose": 4.2, "backup_dosimeter_id": "PD-003",
                 "expected_version": version},
                "monitor-b", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.complete_remeasurement(
                self.item["id"], incident_id,
                {"replacement_dose": 4.2, "backup_dosimeter_id": "BK-9",
                 "expected_version": version + 1},
                "monitor-b", "dosimetrist")

        done = self.service.complete_remeasurement(
            self.item["id"], incident_id,
            {"replacement_dose": 4.2, "backup_dosimeter_id": "BK-9",
             "expected_version": version},
            "monitor-b", "dosimetrist")
        self.assertEqual(done["dose_source"], "backup_dosimeter")
        self.assertAlmostEqual(done["counted_dose"], 4.2)
        self.assertAlmostEqual(done["replacement_dose"], 4.2)
        self.assertAlmostEqual(done["annual_cumulative_dose"], 4.2)
        self.assertEqual(done["open_incident_count"], 0)
        self.assertEqual(done["blockers"], [])
        closed_incident = done["dosimeter_incidents"][0]
        self.assertEqual(closed_incident["status"], "closed")
        self.assertEqual(closed_incident["verified_by"], "monitor-b")
        self.assertEqual(closed_incident["backup_dosimeter_id"], "BK-9")
        self.assertAlmostEqual(closed_incident["replacement_dose"], 4.2)
        self.assertEqual(closed_incident["blockers"], [])

        # 同周期同编号在结案后可以重新登记（只保留一条有效处理）
        again = self.service.register_incident(
            self.item["id"],
            {"dosimeter_id": "PD-003", "wearing_period": "2026-Q1",
             "anomaly_type": "malfunction", "original_dose": 4.2},
            "monitor-a", "dosimetrist")
        self.assertEqual(again["open_incident_count"], 1)

    def test_close_succeeds_after_remeasurement(self):
        detail = self.service.register_incident(
            self.item["id"],
            {"dosimeter_id": "PD-004", "wearing_period": "2026-Q1",
             "anomaly_type": "lost", "original_dose": 5.0},
            "monitor-a", "dosimetrist")
        incident = detail["dosimeter_incidents"][0]
        self.service.complete_remeasurement(
            self.item["id"], incident["id"],
            {"replacement_dose": 6.5, "backup_dosimeter_id": "BK-1",
             "expected_version": incident["version"]},
            "monitor-b", "dosimetrist")
        current = self._to_follow_up()
        closed = self.service.transition(
            current["id"], STATES[-1], current["version"], "reviewer",
            TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], "closed")

        with self.assertRaises(ConflictError):
            self.service.register_incident(
                self.item["id"],
                {"dosimeter_id": "PD-004", "wearing_period": "2026-Q2",
                 "anomaly_type": "lost"},
                "monitor-a", "dosimetrist")

    def test_validation_permission_and_audit_chain(self):
        with self.assertRaises(ValidationError):
            self.service.register_incident(
                self.item["id"],
                {"dosimeter_id": "PD-005", "wearing_period": "2026-Q1",
                 "anomaly_type": "stolen"},
                "monitor-a", "dosimetrist")
        with self.assertRaises(PermissionDenied):
            self.service.register_incident(
                self.item["id"],
                {"dosimeter_id": "PD-005", "wearing_period": "2026-Q1",
                 "anomaly_type": "lost"},
                "viewer-a", "viewer")

        listing = self.service.list_items("viewer")
        self.assertEqual(listing[0]["annual_cumulative_dose"], 5.0)

        events = self.service.audit("viewer", self.item["id"])
        actions = [event["action"] for event in events]
        self.assertEqual(actions, ["create"])
        detail = self.service.register_incident(
            self.item["id"],
            {"dosimeter_id": "PD-005", "wearing_period": "2026-Q1",
             "anomaly_type": "malfunction", "original_dose": 5.0},
            "monitor-a", "dosimetrist")
        incident = detail["dosimeter_incidents"][0]
        self.service.complete_remeasurement(
            self.item["id"], incident["id"],
            {"replacement_dose": 3.8, "backup_dosimeter_id": "BK-5",
             "expected_version": incident["version"]},
            "monitor-b", "dosimetrist")
        events = self.service.audit("viewer", self.item["id"])
        actions = [event["action"] for event in events]
        self.assertEqual(actions, ["create", "incident_register",
                                   "incident_remeasure"])
        register_event = events[1]
        self.assertTrue(register_event["detail"]["original_excluded"])
        remeasure_event = events[2]
        self.assertEqual(remeasure_event["detail"]["replacement_dose"], 3.8)
        self.assertEqual(remeasure_event["detail"]["verified_by"], "monitor-b")
        self.assertTrue(self.repo.verify_audit_chain())

        # 补测事件列表接口
        incidents = self.service.list_incidents(self.item["id"], "viewer")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["dose_source"], "backup_dosimeter")


if __name__ == "__main__":
    unittest.main()
