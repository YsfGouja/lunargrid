"""
Moon base power planner API.

Assumes database.py exposes: Base, engine, SessionLocal
Assumes enums.py has TaskStatus and DeliveryStatus (same names as models.py uses).
Assumes security.py has hash_password, verify_password, create_access_token,
verify_access_token, and that the SECRET_KEY env var is set.

Permissions:
  viewer   read everything
  planner  viewer + create / edit / delete planning data
  admin    planner + manage users, delete sites, load demo data

Run:  uvicorn main:app --reload
Docs: http://127.0.0.1:8000/docs
Demo: POST /demo/seed, then GET /sites/{id}/forecast, /sites/{id}/conflicts

IMPORTANT: models changed (enums, new tables/columns). create_all() does NOT
migrate existing tables, so delete your old .db file once before first run.

Do NOT add `from __future__ import annotations` to this file: the CRUD factory
builds routes with annotations taken from variables, and FastAPI needs the
real classes, not strings.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import event
from sqlalchemy.orm import Session, selectinload

import models
import schemas
from auth_routes import auth_router, roles_router, seed_roles, users_router
from database import Base, engine
from deps import (
    commit,
    get_current_user,
    get_db,
    get_or_404,
    require_admin,
    require_planner,
)
from enums import DeliveryStatus, TaskStatus

MAX_FORECAST_POINTS = 5000
DEMO_SITE_NAME = "Demo Base"
# Tasks in these states are the ones that draw power / need their equipment.
LIVE_TASK_STATES = [TaskStatus.planned, TaskStatus.in_progress]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

# SQLite ignores foreign keys unless you turn them on per connection.
if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    seed_roles()
    yield


app = FastAPI(title="Moon Base Power Planner", lifespan=lifespan)

# Wide open for hackathon/frontend dev. Restrict origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    """Naive UTC, matching how everything is stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


# ---------------------------------------------------------------------------
# Generic CRUD router factory
#
# One function builds list / get / create / patch / delete routes for a model.
# Entity-specific rules are passed in as validate_create / validate_update hooks.
# Tasks are NOT built with this because they have the equipment many-to-many.
# ---------------------------------------------------------------------------

def crud_router(
    prefix,
    tag,
    model,
    create_schema,
    response_schema,
    update_schema=None,
    site_scoped=True,
    validate_create=None,
    validate_update=None,
    delete_dependency=require_planner,
):
    # Router-level dependency: every route needs a logged-in user (any role = read).
    # Create / update need planner or admin. Delete defaults to planner or admin.
    router = APIRouter(prefix=prefix, tags=[tag], dependencies=[Depends(get_current_user)])

    if site_scoped:

        @router.get("", response_model=list[response_schema])
        def list_items(site_id: int | None = None, db: Session = Depends(get_db)):
            q = db.query(model)
            if site_id is not None:
                q = q.filter(model.site_id == site_id)
            return q.order_by(model.id).all()

    else:

        @router.get("", response_model=list[response_schema])
        def list_items(db: Session = Depends(get_db)):
            return db.query(model).order_by(model.id).all()

    @router.get("/{item_id}", response_model=response_schema)
    def get_item(item_id: int, db: Session = Depends(get_db)):
        return get_or_404(db, model, item_id)

    @router.post(
        "", response_model=response_schema, status_code=201,
        dependencies=[Depends(require_planner)],
    )
    def create_item(payload: create_schema, db: Session = Depends(get_db)):  # type: ignore
        if site_scoped:
            get_or_404(db, models.Site, payload.site_id)
        if validate_create:
            validate_create(db, payload)
        obj = model(**payload.model_dump())
        db.add(obj)
        commit(db)
        db.refresh(obj)
        return obj

    if update_schema is not None:

        @router.patch(
            "/{item_id}", response_model=response_schema,
            dependencies=[Depends(require_planner)],
        )
        def update_item(item_id: int, payload: update_schema, db: Session = Depends(get_db)):  # type: ignore
            obj = get_or_404(db, model, item_id)
            # exclude_unset: only fields the client actually sent. This is what
            # lets {"habitat_id": null} clear a field while omitting it leaves it alone.
            data = payload.model_dump(exclude_unset=True)
            columns = model.__table__.columns
            for key, value in data.items():
                if value is None and key in columns and not columns[key].nullable:
                    raise HTTPException(422, f"{key} cannot be null")
            if validate_update:
                validate_update(db, obj, data)
            for key, value in data.items():
                setattr(obj, key, value)
            commit(db)
            db.refresh(obj)
            return obj

    @router.delete(
        "/{item_id}", status_code=204,
        dependencies=[Depends(delete_dependency)],
    )
    def delete_item(item_id: int, db: Session = Depends(get_db)):
        obj = get_or_404(db, model, item_id)
        db.delete(obj)
        commit(db)

    return router


# Entity-specific validation hooks -------------------------------------------

def check_equipment_create(db: Session, p: schemas.EquipmentCreate):
    if p.habitat_id is not None:
        habitat = get_or_404(db, models.Habitat, p.habitat_id)
        if habitat.site_id != p.site_id:
            raise HTTPException(422, "Habitat belongs to a different site")


def check_equipment_update(db: Session, obj: models.Equipment, data: dict):
    habitat_id = data.get("habitat_id")
    if habitat_id is not None:
        habitat = get_or_404(db, models.Habitat, habitat_id)
        if habitat.site_id != obj.site_id:
            raise HTTPException(422, "Habitat belongs to a different site")


def check_battery_update(db: Session, obj: models.Battery, data: dict):
    # BatteryUpdate can't validate this itself: the client may send only one field.
    capacity = data.get("capacity_kwh", obj.capacity_kwh)
    charge = data.get("charge_kwh", obj.charge_kwh)
    if charge > capacity:
        raise HTTPException(422, "charge_kwh cannot exceed capacity_kwh")


def check_delivery_create(db: Session, p: schemas.DeliveryCreate):
    equipment = get_or_404(db, models.Equipment, p.equipment_id)
    if equipment.site_id != p.site_id:
        raise HTTPException(422, "Equipment belongs to a different site")
    if equipment.delivery is not None:
        raise HTTPException(409, "This equipment already has a delivery")


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(roles_router)
app.include_router(
    crud_router(
        "/sites", "Sites", models.Site,
        schemas.SiteCreate, schemas.SiteResponse, schemas.SiteUpdate,
        site_scoped=False,
        delete_dependency=require_admin,  # cascades to everything at the site
    )
)
app.include_router(
    crud_router(
        "/illumination-windows", "Illumination", models.IlluminationWindow,
        schemas.IlluminationWindowCreate, schemas.IlluminationWindowResponse,
    )
)
app.include_router(
    crud_router(
        "/habitats", "Habitats", models.Habitat,
        schemas.HabitatCreate, schemas.HabitatResponse, schemas.HabitatUpdate,
    )
)
app.include_router(
    crud_router(
        "/power-sources", "Power sources", models.PowerSource,
        schemas.PowerSourceCreate, schemas.PowerSourceResponse, schemas.PowerSourceUpdate,
    )
)
app.include_router(
    crud_router(
        "/batteries", "Batteries", models.Battery,
        schemas.BatteryCreate, schemas.BatteryResponse, schemas.BatteryUpdate,
        validate_update=check_battery_update,
    )
)
app.include_router(
    crud_router(
        "/teams", "Teams", models.Team,
        schemas.TeamCreate, schemas.TeamResponse, schemas.TeamUpdate,
    )
)
app.include_router(
    crud_router(
        "/equipment", "Equipment", models.Equipment,
        schemas.EquipmentCreate, schemas.EquipmentResponse, schemas.EquipmentUpdate,
        validate_create=check_equipment_create,
        validate_update=check_equipment_update,
    )
)
app.include_router(
    crud_router(
        "/deliveries", "Deliveries", models.Delivery,
        schemas.DeliveryCreate, schemas.DeliveryResponse, schemas.DeliveryUpdate,
        validate_create=check_delivery_create,
    )
)


# ---------------------------------------------------------------------------
# Tasks (written by hand because of the equipment many-to-many)
# ---------------------------------------------------------------------------

tasks_router = APIRouter(
    prefix="/tasks", tags=["Tasks"], dependencies=[Depends(get_current_user)]
)


def resolve_task_refs(
    db: Session, site_id: int, team_id: int | None, equipment_ids: list[int]
) -> list[models.Equipment]:
    """Make sure the team and equipment exist and belong to the task's site."""
    if team_id is not None:
        team = get_or_404(db, models.Team, team_id)
        if team.site_id != site_id:
            raise HTTPException(422, "Team belongs to a different site")

    ids = list(dict.fromkeys(equipment_ids))  # dedupe, keep order
    if not ids:
        return []
    found = db.query(models.Equipment).filter(models.Equipment.id.in_(ids)).all()
    missing = set(ids) - {e.id for e in found}
    if missing:
        raise HTTPException(422, f"Equipment not found: {sorted(missing)}")
    if any(e.site_id != site_id for e in found):
        raise HTTPException(422, "Some equipment belongs to a different site")
    return found


@tasks_router.get("", response_model=list[schemas.TaskResponse])
def list_tasks(site_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Task).options(selectinload(models.Task.equipment))
    if site_id is not None:
        q = q.filter(models.Task.site_id == site_id)
    return q.order_by(models.Task.start_time).all()


@tasks_router.get("/{task_id}", response_model=schemas.TaskResponse)
def get_task(task_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, models.Task, task_id)


@tasks_router.post(
    "", response_model=schemas.TaskResponse, status_code=201,
    dependencies=[Depends(require_planner)],
)
def create_task(payload: schemas.TaskCreate, db: Session = Depends(get_db)):
    get_or_404(db, models.Site, payload.site_id)
    equipment = resolve_task_refs(db, payload.site_id, payload.team_id, payload.equipment_ids)
    task = models.Task(**payload.model_dump(exclude={"equipment_ids"}))
    task.equipment = equipment
    db.add(task)
    commit(db)
    db.refresh(task)
    return task


@tasks_router.patch(
    "/{task_id}", response_model=schemas.TaskResponse,
    dependencies=[Depends(require_planner)],
)
def update_task(task_id: int, payload: schemas.TaskUpdate, db: Session = Depends(get_db)):
    task = get_or_404(db, models.Task, task_id)
    data = payload.model_dump(exclude_unset=True)

    equipment_ids = data.pop("equipment_ids", None)
    if "equipment_ids" in payload.model_fields_set and equipment_ids is None:
        raise HTTPException(422, "equipment_ids cannot be null (send [] to clear)")

    for key in ("name", "start_time", "duration_hours", "priority", "status"):
        if key in data and data[key] is None:
            raise HTTPException(422, f"{key} cannot be null")

    if "team_id" in data and data["team_id"] is not None:
        resolve_task_refs(db, task.site_id, data["team_id"], [])
    if equipment_ids is not None:
        task.equipment = resolve_task_refs(db, task.site_id, None, equipment_ids)

    for key, value in data.items():
        setattr(task, key, value)
    commit(db)
    db.refresh(task)
    return task


@tasks_router.delete(
    "/{task_id}", status_code=204,
    dependencies=[Depends(require_planner)],
)
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = get_or_404(db, models.Task, task_id)
    db.delete(task)
    commit(db)


app.include_router(tasks_router)


# ---------------------------------------------------------------------------
# Planner logic
#
# Model (deliberately simple):
#   - all power sources are solar: output = sum of available max_output_kw
#     while the site is inside an illumination window, 0 otherwise
#   - load = power_kw of every piece of equipment used by a planned or
#     in-progress task that is active at that moment (each unit counted once,
#     even if two tasks list it). Equipment not attached to a task draws nothing.
#   - all batteries at a site share the net power in proportion to capacity
#   - no charge/discharge losses, no rate limits, no terrain
# ---------------------------------------------------------------------------

@dataclass
class ActiveTask:
    id: int
    start: datetime
    end: datetime
    equipment: dict[int, float]  # equipment_id -> power_kw


def load_active_tasks(db: Session, site_id: int) -> list[ActiveTask]:
    tasks = (
        db.query(models.Task)
        .options(selectinload(models.Task.equipment))
        .filter(models.Task.site_id == site_id, models.Task.status.in_(LIVE_TASK_STATES))
        .all()
    )
    return [
        ActiveTask(
            id=t.id,
            start=t.start_time,
            end=t.start_time + timedelta(hours=t.duration_hours),
            equipment={e.id: e.power_kw for e in t.equipment},
        )
        for t in tasks
    ]


def load_at(tasks: list[ActiveTask], t: datetime) -> float:
    used: dict[int, float] = {}
    for task in tasks:
        if task.start <= t < task.end:
            used.update(task.equipment)
    return sum(used.values())


def solar_output(db: Session, site_id: int) -> float:
    sources = (
        db.query(models.PowerSource)
        .filter(models.PowerSource.site_id == site_id, models.PowerSource.is_available.is_(True))
        .all()
    )
    return sum(s.max_output_kw for s in sources)


def load_windows(db: Session, site_id: int) -> list[tuple[datetime, datetime]]:
    rows = (
        db.query(models.IlluminationWindow)
        .filter(models.IlluminationWindow.site_id == site_id)
        .all()
    )
    return [(w.start_time, w.end_time) for w in rows]


def is_lit(windows: list[tuple[datetime, datetime]], t: datetime) -> bool:
    return any(start <= t < end for start, end in windows)


def forecast_site_batteries(
    db: Session,
    site_id: int,
    start: datetime,
    horizon_hours: float,
    step_minutes: int,
    only_battery_id: int | None = None,
) -> list[schemas.BatteryForecastResponse]:
    batteries = (
        db.query(models.Battery)
        .filter(models.Battery.site_id == site_id)
        .order_by(models.Battery.id)
        .all()
    )
    if not batteries:
        return []

    n_steps = int(horizon_hours * 60 // step_minutes)
    if n_steps < 1:
        raise HTTPException(422, "Horizon is shorter than one step")
    if n_steps > MAX_FORECAST_POINTS:
        raise HTTPException(
            422,
            f"Too many points ({n_steps}). Raise step_minutes or lower horizon_hours "
            f"(limit {MAX_FORECAST_POINTS}).",
        )

    tasks = load_active_tasks(db, site_id)
    windows = load_windows(db, site_id)
    output = solar_output(db, site_id)
    total_capacity = sum(b.capacity_kwh for b in batteries)
    dt_hours = step_minutes / 60

    times = [start + timedelta(minutes=step_minutes * i) for i in range(n_steps + 1)]
    series = [
        (t, load_at(tasks, t), output if is_lit(windows, t) else 0.0) for t in times
    ]

    results = []
    for battery in batteries:
        if only_battery_id is not None and battery.id != only_battery_id:
            continue
        share = battery.capacity_kwh / total_capacity
        charge = battery.charge_kwh
        first_shortage = None
        points = []

        for i, (t, load, gen) in enumerate(series):
            # load_kw / generation_kw are THIS battery's share of the site totals,
            # so charge change per step is exactly (generation_kw - load_kw) * hours.
            points.append(
                schemas.ForecastPoint(
                    time=t,
                    load_kw=round(load * share, 3),
                    generation_kw=round(gen * share, 3),
                    predicted_charge_kwh=round(charge, 3),
                )
            )
            if i == n_steps:
                break

            net_kw = (gen - load) * share
            new_charge = charge + net_kw * dt_hours
            if new_charge < 0:
                # Ran dry inside this step: shortage starts when charge hits zero.
                if first_shortage is None:
                    hours_until_empty = charge / -net_kw
                    first_shortage = t + timedelta(hours=hours_until_empty)
                new_charge = 0.0
            elif new_charge > battery.capacity_kwh:
                new_charge = battery.capacity_kwh
            charge = new_charge

        results.append(
            schemas.BatteryForecastResponse(
                battery_id=battery.id,
                current_charge_kwh=battery.charge_kwh,
                capacity_kwh=battery.capacity_kwh,
                points=points,
                shortage_expected=first_shortage is not None,
                first_shortage_time=first_shortage,
                min_charge_kwh=min(p.predicted_charge_kwh for p in points),
            )
        )
    return results


planner_router = APIRouter(tags=["Planner"], dependencies=[Depends(get_current_user)])


@planner_router.get("/sites/{site_id}/forecast", response_model=list[schemas.BatteryForecastResponse])
def site_forecast(
    site_id: int,
    start: datetime | None = None,
    horizon_hours: float = Query(72, gt=0, le=1500),
    step_minutes: int = Query(60, ge=1, le=1440),
    db: Session = Depends(get_db),
):
    """Will the batteries last? One forecast per battery at the site."""
    get_or_404(db, models.Site, site_id)
    start = to_utc_naive(start) or utcnow()
    return forecast_site_batteries(db, site_id, start, horizon_hours, step_minutes)


@planner_router.get("/batteries/{battery_id}/forecast", response_model=schemas.BatteryForecastResponse)
def battery_forecast(
    battery_id: int,
    start: datetime | None = None,
    horizon_hours: float = Query(72, gt=0, le=1500),
    step_minutes: int = Query(60, ge=1, le=1440),
    db: Session = Depends(get_db),
):
    battery = get_or_404(db, models.Battery, battery_id)
    start = to_utc_naive(start) or utcnow()
    results = forecast_site_batteries(
        db, battery.site_id, start, horizon_hours, step_minutes, only_battery_id=battery_id
    )
    return results[0]


@planner_router.get("/sites/{site_id}/power-budget", response_model=schemas.PowerBudgetResponse)
def power_budget(
    site_id: int,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    db: Session = Depends(get_db),
):
    """
    Do overlapping tasks fit under the site's total solar capacity?
    available_output_kw is the NAMEPLATE total of available sources (what the
    panels deliver in sunlight), ignoring the dark. Dark periods are what the
    battery forecast is for.
    """
    get_or_404(db, models.Site, site_id)
    window_start = to_utc_naive(window_start) or utcnow()
    window_end = to_utc_naive(window_end) or window_start + timedelta(hours=24)
    if window_end <= window_start:
        raise HTTPException(422, "window_end must be after window_start")

    tasks = load_active_tasks(db, site_id)
    output = solar_output(db, site_id)

    # Load only changes when a task starts, so checking the window start and
    # every task start inside the window finds the true peak.
    check_times = {window_start} | {
        t.start for t in tasks if window_start <= t.start < window_end
    }
    peak = 0.0
    conflicting: set[int] = set()
    for t in check_times:
        load = load_at(tasks, t)
        peak = max(peak, load)
        if load > output:
            conflicting |= {task.id for task in tasks if task.start <= t < task.end}

    return schemas.PowerBudgetResponse(
        site_id=site_id,
        window_start=window_start,
        window_end=window_end,
        peak_load_kw=round(peak, 3),
        available_output_kw=round(output, 3),
        overloaded=peak > output,
        conflicting_task_ids=sorted(conflicting),
    )


@planner_router.get("/sites/{site_id}/conflicts", response_model=schemas.ConflictReportResponse)
def delivery_conflicts(site_id: int, db: Session = Depends(get_db)):
    """Tasks that start before their equipment has been delivered and set up."""
    get_or_404(db, models.Site, site_id)
    tasks = (
        db.query(models.Task)
        .options(selectinload(models.Task.equipment).selectinload(models.Equipment.delivery))
        .filter(models.Task.site_id == site_id, models.Task.status.in_(LIVE_TASK_STATES))
        .order_by(models.Task.start_time)
        .all()
    )
    conflicts = []
    for task in tasks:
        for equipment in task.equipment:
            delivery = equipment.delivery
            if delivery is None or delivery.status == DeliveryStatus.installed:
                continue
            ready_at = delivery.expected_arrival + timedelta(hours=delivery.setup_hours)
            if task.start_time < ready_at:
                conflicts.append(
                    schemas.TaskConflict(
                        task_id=task.id,
                        equipment_id=equipment.id,
                        available_from=ready_at,
                        reason=(
                            f"'{task.name}' needs '{equipment.name}', which is not "
                            f"delivered and set up until {ready_at:%Y-%m-%d %H:%M} UTC"
                        ),
                    )
                )
    return schemas.ConflictReportResponse(site_id=site_id, conflicts=conflicts)


app.include_router(planner_router)


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

@app.post("/demo/seed", tags=["Demo"], dependencies=[Depends(require_admin)])
def seed_demo(db: Session = Depends(get_db)):
    """
    Creates a site where, relative to now: the base is lit for 20h, then dark
    for 24h, and a drilling task plus a rover task in the dark drain the
    battery (shortage), while the rover task also starts before the rover is
    delivered and set up (conflict). Timings are compressed on purpose: a real
    lunar day/night is roughly two weeks each.
    """
    if db.query(models.Site).filter(models.Site.name == DEMO_SITE_NAME).first():
        raise HTTPException(409, "Demo data already exists")

    now = utcnow().replace(minute=0, second=0, microsecond=0)

    def at(hours: float) -> datetime:
        return now + timedelta(hours=hours)

    site = models.Site(name=DEMO_SITE_NAME, latitude=-89.9, longitude=0.0)
    db.add(site)
    db.flush()
    sid = site.id

    habitat = models.Habitat(site_id=sid, name="Hab Alpha")
    drill_team = models.Team(site_id=sid, name="Drilling crew", specialization="Regolith drilling")
    rover_team = models.Team(site_id=sid, name="Rover crew", specialization="Surface survey")
    db.add_all(
        [
            habitat,
            drill_team,
            rover_team,
            models.IlluminationWindow(site_id=sid, start_time=at(-10), end_time=at(10)),
            models.IlluminationWindow(site_id=sid, start_time=at(34), end_time=at(54)),
            models.PowerSource(site_id=sid, name="Solar array A", max_output_kw=6),
            models.PowerSource(site_id=sid, name="Solar array B", max_output_kw=4),
            models.Battery(site_id=sid, name="Main battery bank", capacity_kwh=60, charge_kwh=50),
        ]
    )
    db.flush()

    life_support = models.Equipment(
        site_id=sid, habitat_id=habitat.id, name="Life support", power_kw=1.5, priority=5
    )
    comms = models.Equipment(
        site_id=sid, habitat_id=habitat.id, name="Comms relay", power_kw=0.5, priority=4
    )
    drill = models.Equipment(site_id=sid, name="Regolith drill", power_kw=4, priority=2)
    rover = models.Equipment(site_id=sid, name="Survey rover", power_kw=2.5, priority=2)
    db.add_all([life_support, comms, drill, rover])
    db.flush()

    db.add(
        models.Delivery(
            site_id=sid, equipment_id=rover.id, expected_arrival=at(16), setup_hours=4
        )
    )

    baseline = models.Task(
        site_id=sid, name="Baseline habitat operations", start_time=at(-1),
        duration_hours=100, priority=5,
    )
    baseline.equipment = [life_support, comms]
    drilling = models.Task(
        site_id=sid, team_id=drill_team.id, name="Regolith drilling", start_time=at(14),
        duration_hours=6, priority=2,
    )
    drilling.equipment = [drill]
    survey = models.Task(
        site_id=sid, team_id=rover_team.id, name="Rover survey", start_time=at(12),
        duration_hours=4, priority=2,
    )
    survey.equipment = [rover]
    db.add_all([baseline, drilling, survey])
    commit(db)
    return {
        "site_id": sid,
        "try": [
            f"/sites/{sid}/forecast",
            f"/sites/{sid}/conflicts",
            f"/sites/{sid}/power-budget",
        ],
    }


@app.get("/", tags=["Meta"])
def root():
    return {"status": "ok", "docs": "/docs"}
