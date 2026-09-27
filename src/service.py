from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_dosimeter_no, require_number, require_text,
                     require_wear_period)
from .repository import Repository
from .rules import (ANOMALY_LABELS, AUDIT_ROLES, CREATE_ROLES,
                    DOSE_SOURCE_LABELS, INCIDENT_CREATE_ROLES,
                    INCIDENT_ENTITY, INCIDENT_PENDING_REASON,
                    INCIDENT_REMEASURE_ROLES, RECORD_ROLES, ENTITY, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    pending_block_reasons, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_anomaly_type, validate_transition)


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
        dosimeter_no = payload.get("dosimeter_no")
        if dosimeter_no is not None:
            dosimeter_no = require_dosimeter_no(dosimeter_no)
        wear_period = payload.get("wear_period")
        if wear_period is not None:
            wear_period = require_wear_period(wear_period)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor,
                                           dosimeter_no, wear_period)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

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
            self.repository.open_incident_count(item_id),
        )
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["effective_dose"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def register_incident(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, INCIDENT_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("剂量事件已结案，不能登记坏损剂量计事件")
        dosimeter_no = require_dosimeter_no(payload.get("dosimeter_no"))
        wear_period = require_wear_period(payload.get("wear_period"))
        anomaly_type = validate_anomaly_type(payload.get("anomaly_type"))
        if item["dosimeter_no"] and item["dosimeter_no"] != dosimeter_no:
            raise ConflictError("剂量计编号与该周期读数登记不一致")
        if item["wear_period"] and item["wear_period"] != wear_period:
            raise ConflictError("佩戴周期与该周期读数登记不一致")
        if item["dose_status"] != "original":
            raise ConflictError("该周期读数已存在坏损剂量计处理，不能重复登记")
        original_dose = require_number(
            payload.get("original_dose", item["quantity"]), "original_dose", 0.0)
        incident = self.repository.create_incident(
            item_id, dosimeter_no, wear_period, anomaly_type, original_dose, actor)
        self.repository.apply_excluded_dose(item_id, dosimeter_no, wear_period)
        self.repository.append_audit(
            "dosimeter_incident_register", INCIDENT_ENTITY, incident["id"], actor, {
                "item_id": item_id, "dosimeter_no": dosimeter_no,
                "wear_period": wear_period, "anomaly_type": anomaly_type,
                "anomaly_label": ANOMALY_LABELS[anomaly_type],
                "original_dose": original_dose,
                "dose_effect": "原读数退出年度累计，等待补测",
            })
        return self.incident_detail(self.repository.get_incident(incident["id"]))

    def remeasure_incident(self, incident_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, INCIDENT_REMEASURE_ROLES)
        actor = require_text(actor, "actor", 100)
        incident = self.repository.get_incident(incident_id)
        if incident["status"] != "open":
            raise ConflictError("该补测事件已结案")
        replacement_dose = require_number(
            payload.get("replacement_dose"), "replacement_dose", 0.0)
        spare_dosimeter_no = require_dosimeter_no(
            payload.get("spare_dosimeter_no"), "spare_dosimeter_no")
        if spare_dosimeter_no == incident["dosimeter_no"]:
            raise ConflictError("备用剂量计不能与原剂量计相同")
        if actor == incident["created_by"]:
            raise ConflictError("必须由另一名监测员进行备用剂量计复核")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        resolved = self.repository.resolve_incident(
            incident_id, replacement_dose, spare_dosimeter_no, actor,
            expected_version)
        self.repository.apply_replacement_dose(
            incident["item_id"], replacement_dose)
        self.repository.append_audit(
            "dosimeter_incident_remeasure", INCIDENT_ENTITY, incident_id, actor, {
                "item_id": incident["item_id"],
                "dosimeter_no": incident["dosimeter_no"],
                "wear_period": incident["wear_period"],
                "anomaly_type": incident["anomaly_type"],
                "original_dose": incident["original_dose"],
                "replacement_dose": replacement_dose,
                "spare_dosimeter_no": spare_dosimeter_no,
                "registered_by": incident["created_by"],
                "dose_effect": "替代剂量回写该周期并恢复年度累计",
            })
        return self.incident_detail(self.repository.get_incident(incident_id))

    def list_incidents(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ("open", "resolved"):
            raise ConflictError("status必须是open或resolved")
        return [self.incident_detail(i)
                for i in self.repository.list_incidents(status)]

    def get_incident(self, incident_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.incident_detail(self.repository.get_incident(incident_id))

    def list_incidents_for_item(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self.incident_detail(i)
                for i in self.repository.list_incidents_for_item(item_id)]

    def annual_totals(self, role: str, year: str) -> Dict[str, Any]:
        self._view(role)
        from .domain import ValidationError
        if not (isinstance(year, str) and len(year) == 4 and year.isdigit()):
            raise ValidationError("year格式必须为YYYY")
        entries: List[Dict[str, Any]] = []
        total = 0.0
        for item in self.repository.list_items():
            if not item["wear_period"] or item["wear_period"][:4] != year:
                continue
            entries.append({
                "item_id": item["id"],
                "dosimeter_no": item["dosimeter_no"],
                "wear_period": item["wear_period"],
                "dose_source": DOSE_SOURCE_LABELS[item["dose_status"]],
                "original_dose": item["quantity"],
                "replacement_dose": item["replacement_dose"],
                "effective_dose": item["effective_dose"],
            })
            total += float(item["effective_dose"])
        return {"year": year, "total": round(total, 6), "readings": entries}

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def incident_detail(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(incident)
        result["anomaly_label"] = ANOMALY_LABELS[incident["anomaly_type"]]
        item = self.repository.get_item(incident["item_id"])
        result["dose_source"] = DOSE_SOURCE_LABELS[item["dose_status"]]
        result["block_reason"] = (INCIDENT_PENDING_REASON
                                  if incident["status"] == "open" else None)
        return result

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["effective_dose"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["effective_dose"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["effective_dose"], item["threshold"])
        result["dose_source"] = DOSE_SOURCE_LABELS[item["dose_status"]]
        result["block_reasons"] = pending_block_reasons(
            self.repository.open_record_count(item["id"]),
            self.repository.list_open_incidents(item["id"]),
        )
        return result
