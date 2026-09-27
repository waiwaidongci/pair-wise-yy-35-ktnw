from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; ROLES=['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
@dataclass(frozen=True)
class DosimeterIncident:
    id:int; item_id:int; dosimeter_no:str; wear_period:str; anomaly_type:str; original_dose:float; status:str; version:int; replacement_dose:Optional[float]; spare_dosimeter_no:Optional[str]; verified_by:Optional[str]; verified_at:Optional[str]; created_by:str; created_at:str; updated_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
PERIOD_RE=re.compile(r'^\d{4}-(?:0[1-9]|1[0-2])$')
DOSIMETER_NO_RE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
def require_wear_period(value):
    value=require_text(value,"wear_period",20)
    if not PERIOD_RE.match(value): raise ValidationError("wear_period格式必须为YYYY-MM")
    return value
def require_dosimeter_no(value,field="dosimeter_no"):
    value=require_text(value,field,64)
    if not DOSIMETER_NO_RE.match(value): raise ValidationError(f"{field}只能包含字母、数字、下划线或连字符")
    return value
