from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, PermissionDenied, ensure_role,
                     normalize_anomaly_type, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (ANOMALY_LABELS, AUDIT_ROLES, CREATE_ROLES,
                    DOSE_SOURCE_LABELS, ENTITY, INCIDENT_CREATE_ROLES,
                    INCIDENT_REMEASURE_ROLES, INCIDENT_ENTITY, RECORD_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers, escalation_required,
                    incident_blockers, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.render_item(item["id"], role)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def register_incident(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, INCIDENT_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("已结案周期不能登记剂量计事件")
        dosimeter_id = require_text(payload.get("dosimeter_id"), "dosimeter_id", 100)
        wearing_period = require_text(payload.get("wearing_period"), "wearing_period", 50)
        anomaly_type = normalize_anomaly_type(payload.get("anomaly_type"))
        original_dose = require_number(payload.get("original_dose", item["quantity"]),
                                       "original_dose")
        incident = self.repository.create_incident(
            item_id, dosimeter_id, wearing_period, anomaly_type, original_dose, actor)
        self.repository.append_audit("incident_register", INCIDENT_ENTITY, item_id, actor, {
            "incident_id": incident["id"],
            "dosimeter_id": dosimeter_id,
            "wearing_period": wearing_period,
            "anomaly_type": anomaly_type,
            "anomaly_label": ANOMALY_LABELS[anomaly_type],
            "original_dose": original_dose,
            "original_excluded": True,
            "version": incident["version"],
        })
        return self.render_item(item_id, role)

    def complete_remeasurement(self, item_id: int, incident_id: int,
                               payload: Dict[str, Any], actor: str,
                               role: str) -> Dict[str, Any]:
        ensure_role(role, INCIDENT_REMEASURE_ROLES)
        actor = require_text(actor, "actor", 100)
        incident = self.repository.get_incident(incident_id)
        if incident["item_id"] != item_id:
            raise ConflictError("补测事件不属于该周期")
        if incident["status"] != "open":
            raise ConflictError("剂量计事件已结案，不能重复补测")
        if actor == incident["created_by"]:
            raise PermissionDenied("补测复核必须由另一名监测员执行")
        replacement_dose = require_number(payload.get("replacement_dose"),
                                          "replacement_dose")
        backup_dosimeter_id = require_text(payload.get("backup_dosimeter_id"),
                                           "backup_dosimeter_id", 100)
        if backup_dosimeter_id == incident["dosimeter_id"]:
            raise ConflictError("备用剂量计不能与原剂量计编号相同")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        updated = self.repository.complete_incident(
            incident_id, replacement_dose, backup_dosimeter_id, actor, expected_version)
        self.repository.append_audit("incident_remeasure", INCIDENT_ENTITY, item_id, actor, {
            "incident_id": incident_id,
            "dosimeter_id": incident["dosimeter_id"],
            "wearing_period": incident["wearing_period"],
            "anomaly_type": incident["anomaly_type"],
            "original_dose": incident["original_dose"],
            "original_excluded": True,
            "replacement_dose": replacement_dose,
            "backup_dosimeter_id": backup_dosimeter_id,
            "verified_by": actor,
            "from_version": expected_version,
            "to_version": updated["version"],
        })
        return self.render_item(item_id, role)

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(
            target,
            self.repository.open_record_count(item_id),
            self.repository.open_incident_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.render_item(item_id, role)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.render_item(item_id, role)

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = self.repository.list_items(status)
        incidents_by_item = self._incidents_by_item()
        annual_total = self._annual_cumulative(
            self.repository.list_items(), incidents_by_item)
        return [self._render(item, incidents_by_item.get(item["id"], []),
                             annual_total) for item in items]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_incidents(self, item_id: int, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        incidents = self.repository.list_incidents(item_id)
        return [self._render_incident(incident) for incident in incidents]

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def render_item(self, item_id: int, role: str) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        incidents = self.repository.list_incidents(item_id)
        incidents_by_item = self._incidents_by_item()
        annual_total = self._annual_cumulative(
            self.repository.list_items(), incidents_by_item)
        return self._render(item, incidents, annual_total)

    def _incidents_by_item(self) -> Dict[int, List[Dict[str, Any]]]:
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for incident in self.repository.list_all_incidents():
            grouped.setdefault(incident["item_id"], []).append(incident)
        return grouped

    @staticmethod
    def _annual_cumulative(all_items: List[Dict[str, Any]],
                           incidents_by_item: Dict[int, List[Dict[str, Any]]]) -> float:
        total = 0.0
        for item in all_items:
            incidents = incidents_by_item.get(item["id"], [])
            if any(incident["status"] == "open" for incident in incidents):
                continue
            counted = item["quantity"]
            for incident in incidents:
                counted += float(incident["replacement_dose"]) - incident["original_dose"]
            total += counted
        return round(total, 6)

    def _render_incident(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(incident)
        result["anomaly_label"] = ANOMALY_LABELS.get(
            incident["anomaly_type"], incident["anomaly_type"])
        if incident["status"] == "open":
            result["dose_source"] = "pending_remeasurement"
        else:
            result["dose_source"] = "backup_dosimeter"
        result["dose_source_label"] = DOSE_SOURCE_LABELS[result["dose_source"]]
        result["blockers"] = incident_blockers([incident])
        return result

    def _render(self, item: Dict[str, Any], incidents: List[Dict[str, Any]],
                annual_cumulative_dose: float) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        rendered = [self._render_incident(incident) for incident in incidents]
        result["dosimeter_incidents"] = rendered
        open_incidents = [incident for incident in rendered if incident["status"] == "open"]
        closed_incidents = [incident for incident in rendered if incident["status"] == "closed"]
        if not incidents:
            dose_source = "original_reading"
            counted_dose: Optional[float] = item["quantity"]
        elif open_incidents:
            dose_source = "pending_remeasurement"
            counted_dose = None
        else:
            dose_source = "backup_dosimeter"
            counted_dose = item["quantity"]
            for incident in closed_incidents:
                counted_dose += float(incident["replacement_dose"]) - incident["original_dose"]
            counted_dose = round(counted_dose, 6)
        result["dose_source"] = dose_source
        result["dose_source_label"] = DOSE_SOURCE_LABELS[dose_source]
        result["original_dose_excluded"] = bool(incidents)
        result["counted_dose"] = counted_dose
        result["replacement_dose"] = (round(sum(float(incident["replacement_dose"])
                                                for incident in closed_incidents), 6)
                                      if closed_incidents and not open_incidents else None)
        result["open_incident_count"] = len(open_incidents)
        result["blockers"] = incident_blockers(incidents)
        result["annual_cumulative_dose"] = annual_cumulative_dose
        return result
