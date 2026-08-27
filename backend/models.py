"""Pydantic request/response models for API input validation."""
from pydantic import BaseModel, Field
from typing import Literal, Optional


class CollectRequest(BaseModel):
    title: Optional[str] = None
    max_jobs: int = Field(default=20, ge=1, le=500)
    source: Literal["linkedin", "indeed"] = "linkedin"
    search_url: Optional[str] = None
    filters: dict = Field(default_factory=dict, description="Plugin-specific filter key-value pairs")
    automation: bool = Field(default=False, exclude=True)


class ApplyRequest(BaseModel):
    mode: Literal["easy", "external", "all"] = "easy"
    limit: Optional[int] = Field(default=None, ge=1, le=500)
    workers: int = Field(default=1, ge=1, le=4)
    max_steps: Optional[int] = Field(default=None, ge=1, le=500)
    job_url: Optional[str] = None
    job_urls: Optional[list[str]] = Field(default=None, description="Specific job URLs to apply to (batch apply)")
    automation: bool = Field(default=False, exclude=True)
    linkedin_outreach_enabled: bool = Field(default=True, exclude=True)


class AutomationConfigRequest(BaseModel):
    sources: list[Literal["linkedin", "indeed"]] = Field(
        default_factory=lambda: ["linkedin", "indeed"],
        min_length=1,
    )
    interval_minutes: Literal[5, 10, 15, 30, 60] = 15
    daily_job_limit: int = Field(default=10, ge=1, le=100)
    daily_company_limit: int = Field(default=5, ge=1, le=20)
    max_jobs_per_source: int = Field(default=5, ge=1, le=500)
    max_applications_per_cycle: int = Field(default=2, ge=1, le=20)
    automation_min_score: int = Field(default=70, ge=0, le=100)
    linkedin_outreach_enabled: bool = True
    hours_old: int = Field(default=1, ge=1, le=720)
    title: str = ""
    location: str = ""
    job_type: str = ""
    is_remote: bool = False
    linkedin_search_url: str = ""


class ClassifyRequest(BaseModel):
    job_urls: Optional[list[str]] = None
    force: bool = False


class ScreenRequest(BaseModel):
    job_urls: Optional[list[str]] = None


class ScreeningOverrideRequest(BaseModel):
    url: str
    override: Optional[Literal["qualified", "rejected"]] = None


JobCategory = Literal[
    "review",
    "qualified",
    "unqualified",
    "applied",
    "online_assessment",
    "rejected",
    "interview",
    "offer",
    "accepted",
    "refused",
    "failed",
    "blocked",
]


class JobCategoryRequest(BaseModel):
    url: str
    category: JobCategory


class JobCategoriesRequest(BaseModel):
    urls: list[str]
    category: JobCategory


class ApplicationOutcomeResolutionRequest(BaseModel):
    url: str
    attempt_id: Optional[str] = None
    decision: Literal["not_submitted", "submitted"]


class GmailConnectRequest(BaseModel):
    client_id: str = ""
    client_secret: str = ""


class GmailReviewRequest(BaseModel):
    message_id: str


class CollectFilters(BaseModel):
    filters: dict = Field(default_factory=dict, description="Plugin-specific filter key-value pairs")


class DecayRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
    factor: float = Field(default=0.95, gt=0, le=1.0)


class CleanupRequest(BaseModel):
    threshold: float = Field(default=0.3, ge=0, le=1.0)


class CriticalMemorySettingsRequest(BaseModel):
    auto_rerank: bool = True
    max_count: int = Field(default=5, ge=1, le=20)
    max_tokens: int = Field(default=500, ge=100, le=4000)


class PluginToggleRequest(BaseModel):
    enabled: bool = Field(description="Whether the plugin should be enabled")
