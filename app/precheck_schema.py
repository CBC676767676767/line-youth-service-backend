"""Versioned, bounded schemas for the deterministic administrative precheck."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


ShortText = Annotated[str, Field(max_length=200)]
DateText = Annotated[str, Field(max_length=10)]
RuleStatus = Literal["demo", "draft", "confirmed"]
BillingType = Literal["monthly", "annual", "credits", "other", "unsure"]
Channel = Literal["official", "marketplace", "agent", "app_store", "other", "unsure"]
Money = Annotated[Decimal, Field(ge=0, le=1_000_000_000, max_digits=18, decimal_places=6, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MonthlyTransaction(StrictModel):
    purchase_date: DateText = ""
    subscription_start: DateText = ""
    subscription_end: DateText = ""
    eligible_cost_twd: Money | None = None


class PrecheckInput(StrictModel):
    purchase_stage: Literal["planning", "purchased"] = "planning"
    tool_id: ShortText | None = None
    tool_name: ShortText = ""
    plan_id: ShortText | None = None
    plan_name: ShortText = ""
    billing_type: BillingType = "unsure"
    purchase_channel: Channel = "unsure"
    purchase_url: Annotated[str, Field(max_length=2048)] = ""
    seller: ShortText = ""
    purchase_date: DateText = ""
    subscription_start: DateText = ""
    subscription_end: DateText = ""
    residency: Literal["hsinchu", "hsinchu_county", "other", "unsure"] = "unsure"
    birth_date: DateText = ""
    application_type: ShortText = ""
    payer: Literal["self", "other", "unsure"] = "unsure"
    payer_relationship: ShortText = ""
    prepared_documents: Annotated[list[ShortText], Field(max_length=60)] = Field(default_factory=list)
    eligible_cost_twd: Money | None = None
    foreign_currency_only: bool = False
    qualification_categories: Annotated[list[ShortText], Field(max_length=10)] = Field(default_factory=list)
    payment_method: Literal["credit_card", "other", "unsure"] = "unsure"
    billing_component: Literal["subscription", "included_credits", "standalone_credits", "mixed", "unsure"] = "unsure"
    prior_subsidy: Literal["received", "applied", "draft", "withdrawn", "rejected", "none", "unsure"] = "unsure"
    multiple_tools: bool = False
    transactions: Annotated[list[MonthlyTransaction], Field(max_length=12)] = Field(default_factory=list)


class Source(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    url: Annotated[str, Field(max_length=2048)] | None = None
    kind: Literal["synthetic", "official", "unconfirmed"]

    @model_validator(mode="after")
    def valid_url(self):
        if self.url:
            parsed = urlsplit(self.url)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError("Source URL must be an HTTPS URL without credentials")
        return self


class DomainEntry(StrictModel):
    hostname: Annotated[str, Field(min_length=1, max_length=253)]
    include_subdomains: bool = False

    @model_validator(mode="after")
    def normalize_hostname(self):
        value = self.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        labels = value.split(".")
        if len(labels) < 2 or any(not label or len(label) > 63 or label.startswith("-")
                                  or label.endswith("-") or not all(c.isalnum() or c == "-" for c in label)
                                  for label in labels):
            raise ValueError("Domain entries must contain a hostname only")
        self.hostname = value
        return self


class PlanEntry(StrictModel):
    id: ShortText
    name: ShortText
    aliases: Annotated[list[ShortText], Field(max_length=20)] = Field(default_factory=list)
    billing_types: Annotated[list[BillingType], Field(min_length=1, max_length=5)]
    classification: Literal["subscription", "subscription_included_credits", "standalone_credits", "other"]


class ToolEntry(StrictModel):
    id: ShortText
    name: ShortText
    aliases: Annotated[list[ShortText], Field(max_length=20)] = Field(default_factory=list)
    status: RuleStatus
    plans: Annotated[list[PlanEntry], Field(max_length=30)]
    domains: Annotated[list[DomainEntry], Field(max_length=20)]
    channels: Annotated[list[Channel], Field(max_length=6)]
    restricted: bool = False
    restriction_reason: ShortText = ""
    source: Source
    updated_at: date
    valid_from: date
    valid_until: date
    catalog_status: Literal["listed_example", "excluded_by_notice", "verified_plan_catalog", "unverified"] = "verified_plan_catalog"
    checked_at: date | None = None
    category: ShortText = ""

    @model_validator(mode="after")
    def validate_entry(self):
        if self.valid_from > self.valid_until:
            raise ValueError("Invalid catalog validity interval")
        if len({p.id for p in self.plans}) != len(self.plans):
            raise ValueError("Duplicate plan IDs")
        if self.status == "confirmed" and self.source.kind != "official":
            raise ValueError("Confirmed catalog entries require official sources")
        if self.restricted and not self.restriction_reason:
            raise ValueError("Restricted entries require a stated reason")
        return self


class DocumentRequirement(StrictModel):
    doc_id: ShortText
    title: ShortText
    reason: Annotated[str, Field(max_length=500)]


class ApplicationType(StrictModel):
    id: ShortText
    name: ShortText
    documents: Annotated[list[DocumentRequirement], Field(max_length=10)] = Field(default_factory=list)


class QualificationCategory(StrictModel):
    id: ShortText
    name: ShortText
    kind: Literal["specific", "language"]


class PublicSnapshot(StrictModel):
    program_id: Literal["hsinchu_ai_2026"]
    snapshot_version: Annotated[str, Field(min_length=1, max_length=100)]
    source_checked_date: date
    timezone: Literal["Asia/Taipei"]
    source_type: Literal["official_public_page"]
    agency_approved: Literal[False]
    usage: Literal["advisory_precheck"]
    automatic_approval_enabled: Literal[False]


class PendingConfirmation(StrictModel):
    id: ShortText
    title: ShortText
    detail: Annotated[str, Field(max_length=1500)]
    rule_ids: Annotated[list[ShortText], Field(max_length=14)] = Field(default_factory=list)


class RuleParameters(StrictModel):
    purchase_date_from: date
    purchase_date_until: date
    birth_date_from: date
    birth_date_until: date
    residency: Literal["hsinchu"]
    restricted_channels: Annotated[list[Channel], Field(max_length=6)] = Field(default_factory=list)
    restrict_standalone_credits: bool
    subscription_min_months: Annotated[int, Field(ge=1, le=120)] | None = None
    subscription_max_months: Annotated[int, Field(ge=1, le=120)] | None = None
    subscription_month_semantics: Literal["calendar_clamped"] | None = None
    application_types: Annotated[list[ApplicationType], Field(min_length=1, max_length=20)]
    base_documents: Annotated[list[DocumentRequirement], Field(max_length=10)]
    purchased_documents: Annotated[list[DocumentRequirement], Field(max_length=10)]
    paid_by_other_documents: Annotated[list[DocumentRequirement], Field(max_length=10)]
    acceptance_date_from: date | None = None
    acceptance_date_until: date | None = None
    general_rate: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    general_cap_twd: Money | None = None
    enhanced_rate: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    enhanced_cap_twd: Money | None = None
    monthly_application_months: Annotated[int, Field(ge=1, le=12)] | None = None
    annual_application_months: Annotated[int, Field(ge=1, le=12)] | None = None
    qualification_categories: Annotated[list[QualificationCategory], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.purchase_date_from > self.purchase_date_until or self.birth_date_from > self.birth_date_until:
            raise ValueError("Invalid rule date interval")
        if bool(self.acceptance_date_from) != bool(self.acceptance_date_until):
            raise ValueError("Both acceptance period boundaries must be provided")
        if self.acceptance_date_from and self.acceptance_date_from > self.acceptance_date_until:
            raise ValueError("Invalid acceptance date interval")
        if self.subscription_min_months and self.subscription_max_months:
            if self.subscription_min_months > self.subscription_max_months:
                raise ValueError("Invalid subscription month interval")
        if len({a.id for a in self.application_types}) != len(self.application_types):
            raise ValueError("Duplicate application type IDs")
        if len({category.id for category in self.qualification_categories}) != len(self.qualification_categories):
            raise ValueError("Duplicate qualification category IDs")
        documents = self.base_documents + self.purchased_documents + self.paid_by_other_documents
        documents += [doc for app in self.application_types for doc in app.documents]
        if len({doc.doc_id for doc in documents}) != len(documents):
            raise ValueError("Duplicate document IDs")
        return self


class RuleSet(StrictModel):
    version: Annotated[str, Field(min_length=1, max_length=100)]
    status: RuleStatus
    valid_from: date
    valid_until: date
    source: Source
    confirmed_by: ShortText | None = None
    confirmed_at: date | None = None
    params: RuleParameters

    @model_validator(mode="after")
    def validate_confirmation(self):
        if self.valid_from > self.valid_until:
            raise ValueError("Invalid rules validity interval")
        if self.status == "confirmed" and (
            not self.confirmed_by or not self.confirmed_at or self.source.kind != "official"
        ):
            raise ValueError("Confirmed rules require confirmation metadata and official source")
        return self


class PrecheckBundle(StrictModel):
    input_version: Literal["1", "2"] = "1"
    catalog_version: Annotated[str, Field(min_length=1, max_length=100)]
    rules: RuleSet
    tools: Annotated[list[ToolEntry], Field(max_length=100)]
    official_application_url: Annotated[str, Field(max_length=2048)] | None = None
    snapshot: PublicSnapshot | None = None
    pending_confirmations: Annotated[list[PendingConfirmation], Field(max_length=30)] = Field(default_factory=list)
    sources: Annotated[list[Source], Field(max_length=10)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bundle(self):
        if len({t.id for t in self.tools}) != len(self.tools):
            raise ValueError("Duplicate tool IDs")
        if self.snapshot and (self.rules.status == "demo" or self.rules.source.kind != "official"
                              or not self.rules.source.url):
            raise ValueError("Public advisory snapshots require an official source URL and non-demo rules")
        if self.official_application_url:
            parsed = urlsplit(self.official_application_url)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError("Official application URL must be HTTPS without credentials")
        return self
