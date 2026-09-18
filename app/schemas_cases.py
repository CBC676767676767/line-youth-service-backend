"""Validated request contracts for the case workflow.

The scheme's published schema is validated separately when saving/submitting a
form. These contracts never accept state, ownership, or audit fields from clients.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


Identifier = Annotated[str, Field(min_length=1, max_length=100)]
ReasonText = Annotated[str, Field(min_length=1, max_length=1000)]
ReviewResult = Literal["PENDING", "PASS", "FAIL", "QUESTION"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CaseCreate(StrictRequest):
    scheme_id: Identifier


class DraftPatch(StrictRequest):
    form_data: dict[str, Any]


class SubmitCase(StrictRequest):
    file_version_ids: list[Identifier] = Field(default_factory=list, max_length=10)

    @field_validator("file_version_ids")
    @classmethod
    def unique_files(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("同一份文件版本不可重複提交。")
        return value


class TaskSubmission(SubmitCase):
    task_revision: int = Field(ge=1, strict=True)
    statement: str | None = Field(default=None, max_length=2000)


class Reason(StrictRequest):
    reason: ReasonText


class TaskCreate(StrictRequest):
    title: str = Field(min_length=1, max_length=200)
    requirement: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: str = Field(min_length=1, max_length=2000)
    due_at: AwareDatetime
    example_ref: str | None = Field(default=None, max_length=500)

    @field_validator("due_at")
    @classmethod
    def normalize_due_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class TaskRevise(Reason):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    requirement: str | None = Field(default=None, min_length=1, max_length=2000)
    acceptance_criteria: str | None = Field(default=None, min_length=1, max_length=2000)
    due_at: AwareDatetime | None = None
    example_ref: str | None = Field(default=None, max_length=500)

    @field_validator("due_at")
    @classmethod
    def normalize_due_at(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value else None


class TaskAccept(StrictRequest):
    submission_id: Identifier
    review_note: str = Field(min_length=1, max_length=2000)


class TaskReopen(StrictRequest):
    submission_id: Identifier
    public_reason: str = Field(min_length=1, max_length=2000)
    due_at: AwareDatetime

    @field_validator("due_at")
    @classmethod
    def normalize_due_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class EvidenceReference(StrictRequest):
    case_revision_id: Identifier | None = None
    file_version_id: Identifier | None = None
    replaces_file_version_id: Identifier | None = None
    page_no: int | None = Field(default=None, ge=1, le=100000, strict=True)
    field_path: str | None = Field(default=None, min_length=1, max_length=200)
    rule_version_id: Identifier
    note: str | None = Field(default=None, max_length=1000)


class ReviewUpdate(StrictRequest):
    result: ReviewResult
    internal_note: str | None = Field(default=None, max_length=2000)
    public_reason: str | None = Field(default=None, max_length=2000)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=50)


class DecisionCreate(Reason):
    outcome: Literal["APPROVED", "REJECTED"]
    rule_version_id: Identifier
    evidence_refs: list[EvidenceReference] = Field(min_length=1, max_length=50)


class CaseClose(StrictRequest):
    completion_note: str = Field(min_length=1, max_length=2000)


class AssignmentItem(StrictRequest):
    case_id: Identifier
    case_version: int = Field(ge=1, strict=True)


class BatchAssignment(Reason):
    cases: list[AssignmentItem] = Field(min_length=1, max_length=50)
    assignee_id: Identifier

    @field_validator("cases")
    @classmethod
    def unique_cases(cls, value: list[AssignmentItem]) -> list[AssignmentItem]:
        if len({item.case_id for item in value}) != len(value):
            raise ValueError("分派清單不可包含重複案件。")
        return value


class OperationLookup(StrictRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    method: Literal["POST", "PATCH", "DELETE"]
    route_scope: str = Field(min_length=1, max_length=500)
