from datetime import datetime

from pydantic import BaseModel, Field


class AnalyticsEventIn(BaseModel):
    type: str = Field(min_length=1, max_length=20)
    path: str = ""
    title: str = ""
    label: str = ""
    href: str = ""
    element: str = ""
    x_pct: float = 0
    y_pct: float = 0
    duration_seconds: int = Field(default=0, ge=0, le=86_400)


class AnalyticsCollectIn(BaseModel):
    visitor_id: str = Field(min_length=8, max_length=36)
    session_id: str = Field(min_length=8, max_length=36)
    timezone: str = ""
    locale: str = ""
    referrer: str = ""
    screen_w: int = 0
    screen_h: int = 0
    events: list[AnalyticsEventIn] = Field(default_factory=list, max_length=80)


class AnalyticsCollectOut(BaseModel):
    ok: bool = True
    skipped: bool = False


class CountryStat(BaseModel):
    country_code: str
    country_name: str
    visits: int
    unique_visitors: int
    avg_duration: int
    clicks: int


class PageStat(BaseModel):
    path: str
    views: int
    unique_visitors: int
    clicks: int
    avg_duration: int


class DeviceStat(BaseModel):
    device: str
    visits: int


class ReferrerStat(BaseModel):
    referrer: str
    visits: int


class AnalyticsSummaryOut(BaseModel):
    live_now: int
    visits_today: int
    unique_today: int
    visits_period: int
    unique_period: int
    avg_duration: int
    clicks_period: int
    countries: list[CountryStat]
    pages: list[PageStat]
    devices: list[DeviceStat]
    referrers: list[ReferrerStat]


class SessionListItem(BaseModel):
    id: int
    session_id: str
    visitor_id: str
    country_code: str
    country_name: str
    city: str
    region: str
    device: str
    browser: str
    landing_path: str
    current_path: str
    page_count: int
    click_count: int
    duration_seconds: int
    started_at: datetime | None = None
    last_seen_at: datetime | None = None
    live: bool = False


class SessionListOut(BaseModel):
    items: list[SessionListItem]
    total: int


class EventOut(BaseModel):
    id: int
    event_type: str
    path: str
    title: str
    label: str
    href: str
    element: str
    x_pct: float
    y_pct: float
    duration_seconds: int = 0
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class PageStayOut(BaseModel):
    path: str
    title: str = ""
    duration_seconds: int = 0
    entered_at: datetime | None = None
    left_at: datetime | None = None


class SessionDetailOut(BaseModel):
    session: SessionListItem
    events: list[EventOut]
    page_stays: list[PageStayOut] = []


class ClickPoint(BaseModel):
    path: str
    label: str
    href: str
    element: str
    x_pct: float
    y_pct: float
    country_name: str
    session_id: str
    created_at: datetime | None = None


class ClicksOut(BaseModel):
    path: str
    total: int
    points: list[ClickPoint]
    top_targets: list[dict]
