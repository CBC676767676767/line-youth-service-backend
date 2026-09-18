from sqlalchemy import and_, false, or_, select, true

from app.common import ApiError
from app.db import utcnow
from app.models import Case, CaseAccess


def scope_filter(principal, role):
    parts = []
    for grant in principal.grants:
        if grant.role != role or grant.revoked_at is not None:
            continue
        if grant.expires_at is not None and grant.expires_at <= utcnow():
            continue
        if grant.scope_type == "GLOBAL":
            return true()
        if grant.scope_type == "SCHEME":
            parts.append(Case.scheme_id == grant.scope_id)
        if grant.scope_type == "CASE":
            parts.append(Case.id == grant.scope_id)
    return or_(*parts) if parts else false()


def case_filter(principal, write=False):
    access = select(CaseAccess.case_id).where(
        CaseAccess.account_id == principal.account.id,
        CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > utcnow()),
    )
    if write:
        access = access.where(CaseAccess.permission.in_(["OWNER", "WRITE"]))
    granted = Case.id.in_(access)
    predicates = []
    if "applicant" in principal.roles:
        predicates.append(granted)
    if "reviewer" in principal.roles:
        predicates.append(and_(scope_filter(principal, "reviewer"),
                               or_(Case.assigned_to == principal.account.id, granted)))
    if "supervisor" in principal.roles:
        predicates.append(scope_filter(principal, "supervisor"))
    if "auditor" in principal.roles and not write:
        predicates.append(and_(scope_filter(principal, "auditor"), granted))
    return or_(*predicates) if predicates else false()


def case_for(db, principal, case_id, write=False):
    query = select(Case).where(Case.id == case_id, case_filter(principal, write=write))
    if write:
        query = query.with_for_update()
    case = db.scalar(query)
    if case is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可存取的案件。")
    return case
