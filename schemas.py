from datetime import datetime, timezone

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from enums import EquipmentStatus, TaskStatus, DeliveryStatus

# Convention: datetimes are stored as naive UTC. Aware inputs are converted.
# Priority: 1 to 5, 5 = most critical.


def _to_utc_naive(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _validate_task_payloads(value):
    if value is not None and any(
        not key.isdigit() or amount < 0 or amount > 10000 for key, amount in value.items()
    ):
        raise ValueError("Task payloads need numeric task IDs and weights from 0 to 10,000 kg")
    return value


# Roles
class RoleBase(BaseModel):
    name: str = Field(min_length=1, max_length=30)


class RoleCreate(RoleBase):
    pass


class RoleResponse(RoleBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# Users
class UserBase(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    email: EmailStr


class UserCreate(UserBase):
    # bcrypt only uses the first 72 bytes, so cap the length.
    password: str = Field(min_length=8, max_length=64)


class UserResponse(UserBase):
    id: int
    role_id: int
    role: RoleResponse

    model_config = ConfigDict(from_attributes=True)


class UserRoleUpdate(BaseModel):
    role_id: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# Sites
class SiteBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class SiteCreate(SiteBase):
    pass


class SiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class SiteResponse(SiteBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# Illumination windows (periods of sunlight at a site)
class IlluminationWindowBase(BaseModel):
    start_time: datetime
    end_time: datetime

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def to_utc(cls, v):
        return _to_utc_naive(v)

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class IlluminationWindowCreate(IlluminationWindowBase):
    site_id: int


class IlluminationWindowResponse(IlluminationWindowBase):
    id: int
    site_id: int

    model_config = ConfigDict(from_attributes=True)


# Habitats
class HabitatBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class HabitatCreate(HabitatBase):
    site_id: int


class HabitatUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)


class HabitatResponse(HabitatBase):
    id: int
    site_id: int

    model_config = ConfigDict(from_attributes=True)


# Power sources (all assumed solar)
class PowerSourceBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    max_output_kw: float = Field(gt=0)
    is_available: bool = True


class PowerSourceCreate(PowerSourceBase):
    site_id: int


class PowerSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    max_output_kw: float | None = Field(default=None, gt=0)
    is_available: bool | None = None


class PowerSourceResponse(PowerSourceBase):
    id: int
    site_id: int

    model_config = ConfigDict(from_attributes=True)


# Batteries
class BatteryBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    capacity_kwh: float = Field(gt=0)
    charge_kwh: float = Field(ge=0)

    @model_validator(mode="after")
    def charge_within_capacity(self):
        if self.charge_kwh > self.capacity_kwh:
            raise ValueError("charge_kwh cannot exceed capacity_kwh")
        return self


class BatteryCreate(BatteryBase):
    site_id: int


# BatteryUpdate can't check charge <= capacity on its own because the client
# may send only one of the two. Check the merged values in the endpoint.
class BatteryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    capacity_kwh: float | None = Field(default=None, gt=0)
    charge_kwh: float | None = Field(default=None, ge=0)


class BatteryResponse(BatteryBase):
    id: int
    site_id: int

    model_config = ConfigDict(from_attributes=True)


# Teams
class TeamBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    specialization: str | None = None


class TeamCreate(TeamBase):
    site_id: int


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    specialization: str | None = None


class TeamResponse(TeamBase):
    id: int
    site_id: int

    model_config = ConfigDict(from_attributes=True)


# Equipment
class EquipmentBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    power_kw: float = Field(ge=0)
    status: EquipmentStatus = EquipmentStatus.available
    priority: int = Field(default=1, ge=1, le=5)


class EquipmentCreate(EquipmentBase):
    site_id: int
    habitat_id: int | None = None


# In update endpoints use payload.model_dump(exclude_unset=True) so that
# "field omitted" and "field explicitly set to null" are told apart.
class EquipmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    power_kw: float | None = Field(default=None, ge=0)
    status: EquipmentStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    habitat_id: int | None = None


class EquipmentResponse(EquipmentBase):
    id: int
    site_id: int
    habitat_id: int | None

    model_config = ConfigDict(from_attributes=True)


# Tasks
class TaskBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    start_time: datetime
    duration_hours: float = Field(gt=0)
    priority: int = Field(default=1, ge=1, le=5)
    status: TaskStatus = TaskStatus.planned

    @field_validator("start_time", mode="after")
    @classmethod
    def to_utc(cls, v):
        return _to_utc_naive(v)


class TaskCreate(TaskBase):
    site_id: int
    team_id: int | None = None
    equipment_ids: list[int] = Field(default_factory=list)


# Same rule as EquipmentUpdate: use model_dump(exclude_unset=True), and check
# "equipment_ids" in payload.model_fields_set before replacing the equipment list.
class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    start_time: datetime | None = None
    duration_hours: float | None = Field(default=None, gt=0)
    priority: int | None = Field(default=None, ge=1, le=5)
    status: TaskStatus | None = None
    team_id: int | None = None
    equipment_ids: list[int] | None = None

    @field_validator("start_time", mode="after")
    @classmethod
    def to_utc(cls, v):
        return _to_utc_naive(v)


class TaskResponse(TaskBase):
    id: int
    site_id: int
    team_id: int | None
    equipment: list[EquipmentResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# Deliveries
class DeliveryBase(BaseModel):
    expected_arrival: datetime
    setup_hours: float = Field(default=0, ge=0)
    status: DeliveryStatus = DeliveryStatus.in_transit

    @field_validator("expected_arrival", mode="after")
    @classmethod
    def to_utc(cls, v):
        return _to_utc_naive(v)


class DeliveryCreate(DeliveryBase):
    site_id: int
    equipment_id: int


class DeliveryUpdate(BaseModel):
    expected_arrival: datetime | None = None
    setup_hours: float | None = Field(default=None, ge=0)
    status: DeliveryStatus | None = None

    @field_validator("expected_arrival", mode="after")
    @classmethod
    def to_utc(cls, v):
        return _to_utc_naive(v)


class DeliveryResponse(DeliveryBase):
    id: int
    site_id: int
    equipment_id: int

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Calculated results (computed by the backend, not stored)
# ---------------------------------------------------------------------------

# Battery forecast: will the batteries last through the dark period?
class ForecastPoint(BaseModel):
    time: datetime
    load_kw: float
    generation_kw: float
    predicted_charge_kwh: float


class BatteryForecastResponse(BaseModel):
    battery_id: int
    current_charge_kwh: float
    capacity_kwh: float
    points: list[ForecastPoint]
    shortage_expected: bool
    first_shortage_time: datetime | None = None
    min_charge_kwh: float
    sunlight_source: str = "saved illumination windows"


# Power budget: do overlapping tasks fit under the site's total solar output?
class PowerBudgetResponse(BaseModel):
    site_id: int
    window_start: datetime
    window_end: datetime
    peak_load_kw: float
    available_output_kw: float
    overloaded: bool
    conflicting_task_ids: list[int] = Field(default_factory=list)


# Delivery conflicts: tasks that start before their equipment is delivered and set up
class TaskConflict(BaseModel):
    task_id: int
    equipment_id: int
    available_from: datetime
    reason: str


class ConflictReportResponse(BaseModel):
    site_id: int
    conflicts: list[TaskConflict]


# JPL Sun position. Values are true coordinates; `sunlit` uses elevation > 0°.
class SiteSunlightPoint(BaseModel):
    time: datetime
    azimuth_deg: float | None = None
    elevation_deg: float | None = None
    sunlit: bool


class SiteSunlightResponse(BaseModel):
    site_id: int
    source: str
    current: SiteSunlightPoint | None = None
    points: list[SiteSunlightPoint]
    note: str | None = None


# Rover mission comparison. Results stay predictive until the user saves them.
class MissionSimulationRequest(BaseModel):
    origin_site_id: int
    destination_site_id: int
    waypoint_site_ids: list[int] = Field(default_factory=list, max_length=5)
    task_ids: list[int] = Field(default_factory=list, max_length=20)
    departure_time: datetime | None = None
    return_trip: bool = False
    optimize_order: bool = False
    sun_mode: str = Field(default="live", pattern="^(live|manual)$")
    rover_charges: dict[str, float] = Field(default_factory=dict)
    task_payload_kg: dict[str, float] = Field(default_factory=dict)
    plan_id: int | None = None

    @field_validator("task_payload_kg")
    @classmethod
    def valid_payloads(cls, value):
        return _validate_task_payloads(value)

    @field_validator("departure_time", mode="after")
    @classmethod
    def to_utc(cls, v):
        return _to_utc_naive(v)


class MissionTaskSummary(BaseModel):
    id: int
    name: str
    duration_hours: float
    power_kw: float
    payload_kg: float = 0


class MissionTimelinePoint(BaseModel):
    time: datetime
    battery_kwh: float
    phase: str


class RoverMissionResult(BaseModel):
    rover_id: str
    rover_name: str
    success: bool
    reason: str
    data_type: str
    source_url: str | None = None
    battery_capacity_kwh: float
    start_charge_kwh: float
    end_charge_kwh: float
    reserve_kwh: float
    drive_energy_kwh: float
    work_energy_kwh: float
    solar_energy_kwh: float
    travel_hours: float
    mission_hours: float
    distance_km: float
    payload_kg: float
    max_payload_kg: float
    timeline: list[MissionTimelinePoint]


class MissionSimulationResponse(BaseModel):
    origin_site: str
    destination_site: str
    route: list[str]
    return_trip: bool
    distance_km: float
    departure_time: datetime
    sunlight_source: str
    optimized_order: bool
    note: str | None = None
    tasks: list[MissionTaskSummary]
    results: list[RoverMissionResult]
    recommended_task_ids: list[int]


class MissionPlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    route_site_ids: list[int] = Field(min_length=2, max_length=7)
    task_ids: list[int] = Field(default_factory=list, max_length=20)
    return_trip: bool = False
    rover_charges: dict[str, float] = Field(default_factory=dict)
    task_payload_kg: dict[str, float] = Field(default_factory=dict)

    @field_validator("task_payload_kg")
    @classmethod
    def valid_payloads(cls, value):
        return _validate_task_payloads(value)


class MissionPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    route_site_ids: list[int] | None = Field(default=None, min_length=2, max_length=7)
    task_ids: list[int] | None = Field(default=None, max_length=20)
    return_trip: bool | None = None
    rover_charges: dict[str, float] | None = None
    task_payload_kg: dict[str, float] | None = None

    @field_validator("task_payload_kg")
    @classmethod
    def valid_payloads(cls, value):
        return _validate_task_payloads(value)


class MissionPlanShareRequest(BaseModel):
    email: EmailStr


class MissionPlanResponse(BaseModel):
    id: int
    owner_id: int
    name: str
    route_site_ids: list[int]
    task_ids: list[int]
    return_trip: bool
    rover_charges: dict[str, float]
    task_payload_kg: dict[str, float]
    shared_user_ids: list[int]

    model_config = ConfigDict(from_attributes=True)


class SimulationAdvanceRequest(BaseModel):
    hours: int = Field(gt=0, le=24)


class SimulationAdvanceResponse(BaseModel):
    site_id: int
    current_time: datetime
    advanced_hours: float
    batteries: list[BatteryResponse]


class SiteSimulationClockResponse(BaseModel):
    site_id: int
    current_time: datetime
