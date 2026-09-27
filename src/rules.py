from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
ANOMALY_TYPES=['lost','not_returned','malfunction']
ANOMALY_LABELS={'lost':'遗失','not_returned':'未回收','malfunction':'故障'}
DOSE_STATUSES=['original','excluded','replacement']
DOSE_SOURCE_LABELS={'original':'原读数','excluded':'原读数已退出累计','replacement':'备用剂量计补测'}
INCIDENT_CREATE_ROLES=set(['dosimetrist','radiation_officer'])
INCIDENT_REMEASURE_ROLES=set(['dosimetrist','radiation_officer'])
INCIDENT_ENTITY='坏损剂量计事件'
INCIDENT_PENDING_REASON='等待另一名监测员使用备用剂量计复核并回写替代剂量'
def validate_anomaly_type(value):
    if value not in ANOMALY_TYPES: raise ValidationError("anomaly_type必须是lost、not_returned或malfunction")
    return value
def incident_blocker(incident):
    return f"坏损剂量计补测未完成（剂量计{incident['dosimeter_no']}/周期{incident['wear_period']}/{ANOMALY_LABELS[incident['anomaly_type']]}）"
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
    if target in TERMINAL_STATES and open_records>0: blockers.append("仍有未关闭事项")
    if target in TERMINAL_STATES and open_incidents>0: blockers.append(f"有{open_incidents}起坏损剂量计补测未完成")
    return blockers
def pending_block_reasons(open_records,open_incidents):
    reasons=[]
    if open_records>0: reasons.append("仍有未关闭事项")
    if open_incidents: reasons.extend(incident_blocker(i) for i in open_incidents)
    return reasons
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
