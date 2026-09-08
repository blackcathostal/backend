from datetime import datetime

from pydantic import BaseModel, Field


class AiSourceBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=8, max_length=500)
    category: str = Field(default="Turismo", max_length=80)
    language: str = Field(default="es", max_length=12)
    priority: int = Field(default=0, ge=0, le=100)
    is_active: bool = True


class AiSourceCreate(AiSourceBase):
    pass


class AiSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = Field(default=None, min_length=8, max_length=500)
    category: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=12)
    priority: int | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


class AiSourceOut(AiSourceBase):
    id: int
    last_checked_at: datetime | None = None
    last_status: str
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AiUsageOut(BaseModel):
    id: int
    run_id: int | None = None
    provider: str
    model: str
    operation: str
    status: str
    api_requests: int = 1
    prompt_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    platform_cost_usd: float | None = None
    platform_balance_usd: float | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class AiUsageSummary(BaseModel):
    from_date: datetime | None = None
    to_date: datetime | None = None
    total_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    platform_balance_usd: float | None = None
    platform_synced_runs: int = 0
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    api_requests: int = 0
    cache_hit_ratio: float = 0.0
    entries: list[AiUsageOut] = Field(default_factory=list)


class AiGenerationOut(BaseModel):
    message: str
    post_id: int
    run_id: int
    usage: AiUsageOut | None = None
