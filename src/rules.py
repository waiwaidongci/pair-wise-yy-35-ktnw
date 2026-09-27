from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'; INCIDENT_ENTITY='剂量计事件'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
INCIDENT_CREATE_ROLES=set(['dosimetrist']); INCIDENT_REMEASURE_ROLES=set(['dosimetrist'])
ANOMALY_LABELS={'lost':'遗失', 'not_recovered':'未回收', 'malfunction':'故障'}
DOSE_SOURCE_LABELS={'original_reading':'原始读数', 'pending_remeasurement':'待补测（原读数已退出累计）', 'backup_dosimeter':'备用剂量计复核'}
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records,open_incidents=0):
    blockers=[]
    if target in TERMINAL_STATES:
        if open_records>0: blockers.append("仍有未关闭事项")
        if open_incidents>0: blockers.append("坏损剂量计补测未完成")
    return blockers
def incident_blockers(incidents):
    blockers=[]
    for incident in incidents:
        if incident["status"]=="open":
            label=ANOMALY_LABELS.get(incident["anomaly_type"],incident["anomaly_type"])
            blockers.append(f"剂量计{incident['dosimeter_id']}（{incident['wearing_period']}，{label}）补测未完成，原读数已退出累计且尚无替代剂量")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
