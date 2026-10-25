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

New tables are created automatically. Existing tables and saved planning data
are preserved; no database reset is needed for this change.

Do NOT add `from __future__ import annotations` to this file: the CRUD factory
builds routes with annotations taken from variables, and FastAPI needs the
real classes, not strings.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from bisect import bisect_right
from itertools import pairwise
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import event
from sqlalchemy.orm import Session, selectinload

import models
from lunar_sun import MAX_HORIZON_HOURS, get_sun_positions
from mission import ROVER_PROFILES, lunar_distance_km, simulate_rover, suggest_task_order
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
from enums import DeliveryStatus, EquipmentStatus, TaskStatus

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


app = FastAPI(title="LunarGrid", lifespan=lifespan)

# Wide open for hackathon/frontend dev. Restrict origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/app", include_in_schema=False)
def serve_app():
    return FileResponse(Path(__file__).with_name("index (1).html"))


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
#   - live Sun elevation scales solar output; saved illumination windows are the offline fallback
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


def sunlight_for_site(site, start: datetime, hours: float, step_minutes: int):
    """Use JPL Sun positions when reachable; keep saved windows as an offline fallback."""
    stop = start + timedelta(hours=hours)
    if site.latitude is not None and site.longitude is not None and hours <= MAX_HORIZON_HOURS:
        try:
            points = get_sun_positions(site.longitude, site.latitude, start, stop, step_minutes)
            return points, "NASA/JPL Horizons", None
        except Exception as exc:
            note = f"JPL Sun forecast unavailable ({type(exc).__name__}); using saved sunlight windows."
    else:
        note = "Site coordinates are missing or the requested period exceeds 30 days; using saved sunlight windows."
    windows = load_windows_from_site(site)
    points = [
        {
            "time": start + timedelta(minutes=step_minutes * i),
            "azimuth_deg": None,
            "elevation_deg": None,
            "sunlit": is_lit(windows, start + timedelta(minutes=step_minutes * i)),
        }
        for i in range(int(hours * 60 // step_minutes) + 1)
    ]
    return points, "Saved sunlight windows (offline fallback)", note


def load_windows_from_site(site):
    return [(window.start_time, window.end_time) for window in site.illumination_windows]


def sunlight_factor(points, windows, at, times=None):
    sun_times = times if times is not None else [point["time"] for point in points]
    index = bisect_right(sun_times, at) - 1
    if index >= 0 and points[index].get("elevation_deg") is not None:
        from math import radians, sin
        return max(0.0, sin(radians(points[index]["elevation_deg"])))
    return 1.0 if is_lit(windows, at) else 0.0


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
    site = get_or_404(db, models.Site, site_id)
    windows = load_windows(db, site_id)
    sun_points, sunlight_source, _ = sunlight_for_site(
        site, start, horizon_hours, max(30, min(60, step_minutes))
    )
    sun_times = [point["time"] for point in sun_points]
    output = solar_output(db, site_id)
    total_capacity = sum(b.capacity_kwh for b in batteries)
    dt_hours = step_minutes / 60

    times = [start + timedelta(minutes=step_minutes * i) for i in range(n_steps + 1)]
    series = [
        (t, load_at(tasks, t), output * sunlight_factor(sun_points, windows, t, sun_times))
        for t in times
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
                sunlight_source=sunlight_source,
            )
        )
    return results


planner_router = APIRouter(tags=["Planner"], dependencies=[Depends(get_current_user)])


@planner_router.get("/mission-rovers")
def mission_rovers():
    """Historical Apollo reference plus clearly marked future concept estimates."""
    return ROVER_PROFILES


def accessible_plan(db: Session, plan_id: int, user: models.User, owner_only=False):
    plan = get_or_404(db, models.MissionPlan, plan_id)
    if plan.owner_id != user.id and (owner_only or user.id not in (plan.shared_user_ids or [])):
        raise HTTPException(404, "Mission plan not found")
    return plan


def validate_plan_route(db: Session, ids: list[int]):
    if len(ids) < 2 or len(ids) > 7 or len(set(ids)) != len(ids):
        raise HTTPException(422, "A route needs 2–7 different sites")
    sites = [get_or_404(db, models.Site, site_id) for site_id in ids]
    if any(site.latitude is None or site.longitude is None for site in sites):
        raise HTTPException(422, "Add coordinates to every route site first")
    return sites


def validate_plan_tasks(db: Session, task_ids: list[int], destination_id: int):
    if len(task_ids) > 20 or len(set(task_ids)) != len(task_ids):
        raise HTTPException(422, "Choose up to 20 different tasks")
    if not task_ids:
        return
    tasks = db.query(models.Task).filter(models.Task.id.in_(task_ids)).all()
    if len(tasks) != len(task_ids) or any(t.site_id != destination_id for t in tasks):
        raise HTTPException(422, "Every selected task must belong to the destination site")
    if any(t.status not in LIVE_TASK_STATES for t in tasks):
        raise HTTPException(422, "Only planned or in-progress tasks can be simulated")


def validate_rover_charges(charges: dict[str, float]):
    profiles = {rover["id"]: rover for rover in ROVER_PROFILES}
    if set(charges) - profiles.keys():
        raise HTTPException(422, "Choose a known rover profile for every saved charge")
    for rover_id, charge in charges.items():
        if charge < 0 or charge > profiles[rover_id]["battery_kwh"]:
            raise HTTPException(422, f"{profiles[rover_id]['name']} charge must be between 0 and {profiles[rover_id]['battery_kwh']} kWh")


@planner_router.get("/mission-plans", response_model=list[schemas.MissionPlanResponse])
def list_mission_plans(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return [plan for plan in db.query(models.MissionPlan).order_by(models.MissionPlan.id.desc()).all()
            if plan.owner_id == user.id or user.id in (plan.shared_user_ids or [])]


@planner_router.post("/mission-plans", response_model=schemas.MissionPlanResponse,
                     status_code=201, dependencies=[Depends(require_planner)])
def create_mission_plan(payload: schemas.MissionPlanCreate, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    sites = validate_plan_route(db, payload.route_site_ids)
    validate_plan_tasks(db, payload.task_ids, sites[-1].id)
    validate_rover_charges(payload.rover_charges)
    plan = models.MissionPlan(owner_id=user.id, **payload.model_dump())
    db.add(plan)
    commit(db)
    db.refresh(plan)
    return plan


@planner_router.patch("/mission-plans/{plan_id}", response_model=schemas.MissionPlanResponse,
                      dependencies=[Depends(require_planner)])
def update_mission_plan(plan_id: int, payload: schemas.MissionPlanUpdate,
                        db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    plan = accessible_plan(db, plan_id, user, owner_only=True)
    data = payload.model_dump(exclude_unset=True)
    if any(value is None for value in data.values()):
        raise HTTPException(422, "Saved plan fields cannot be null")
    route_ids = data.get("route_site_ids", plan.route_site_ids)
    sites = validate_plan_route(db, route_ids)
    validate_plan_tasks(db, data.get("task_ids", plan.task_ids), sites[-1].id)
    validate_rover_charges(data.get("rover_charges", plan.rover_charges))
    for key, value in data.items():
        setattr(plan, key, value)
    commit(db)
    db.refresh(plan)
    return plan


@planner_router.post("/mission-plans/{plan_id}/share", response_model=schemas.MissionPlanResponse,
                     dependencies=[Depends(require_planner)])
def share_mission_plan(plan_id: int, payload: schemas.MissionPlanShareRequest,
                       db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    plan = accessible_plan(db, plan_id, user, owner_only=True)
    recipient = db.query(models.User).filter(models.User.email == str(payload.email).lower()).first()
    if recipient is None:
        raise HTTPException(404, "That email does not belong to a registered user")
    if recipient.id != user.id and recipient.id not in (plan.shared_user_ids or []):
        plan.shared_user_ids = [*(plan.shared_user_ids or []), recipient.id]
        commit(db)
    return plan


@planner_router.delete("/mission-plans/{plan_id}", status_code=204,
                       dependencies=[Depends(require_planner)])
def delete_mission_plan(plan_id: int, db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    db.delete(accessible_plan(db, plan_id, user, owner_only=True))
    commit(db)


@planner_router.post("/mission-simulation", response_model=schemas.MissionSimulationResponse)
def mission_simulation(payload: schemas.MissionSimulationRequest, db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """Compare rover profiles over a waypoint route and selected destination work."""
    plan = accessible_plan(db, payload.plan_id, user) if payload.plan_id else None
    route_ids = plan.route_site_ids if plan else [
        payload.origin_site_id, *payload.waypoint_site_ids, payload.destination_site_id
    ]
    route_sites = validate_plan_route(db, route_ids)
    origin, destination = route_sites[0], route_sites[-1]
    task_ids = list(dict.fromkeys(plan.task_ids if plan else payload.task_ids))
    tasks_by_id = {}
    if task_ids:
        task_rows = (
            db.query(models.Task)
            .options(selectinload(models.Task.equipment))
            .filter(models.Task.id.in_(task_ids))
            .all()
        )
        tasks_by_id = {task.id: task for task in task_rows}
        missing = set(task_ids) - tasks_by_id.keys()
        if missing:
            raise HTTPException(422, f"Task(s) not found: {sorted(missing)}")
        if any(task.site_id != destination.id for task in task_rows):
            raise HTTPException(422, "Choose tasks assigned to the destination site")
        if any(task.status not in LIVE_TASK_STATES for task in task_rows):
            raise HTTPException(422, "Only planned or in-progress tasks can be simulated")

    selected = [tasks_by_id[task_id] for task_id in task_ids]
    charges = plan.rover_charges if plan else payload.rover_charges
    payloads = plan.task_payload_kg if plan else payload.task_payload_kg
    unknown_rovers = set(charges) - {rover["id"] for rover in ROVER_PROFILES}
    if unknown_rovers:
        raise HTTPException(422, f"Unknown rover profile(s): {sorted(unknown_rovers)}")
    for rover in ROVER_PROFILES:
        charge = charges.get(rover["id"], rover["battery_kwh"])
        if charge < 0 or charge > rover["battery_kwh"]:
            raise HTTPException(422, f"{rover['name']} starting charge must be between 0 and {rover['battery_kwh']} kWh")
    simulation_tasks = [{
        "id": task.id, "name": task.name, "duration_hours": task.duration_hours,
        "power_kw": sum(equipment.power_kw for equipment in task.equipment),
        "payload_kg": float(payloads.get(str(task.id), 0)),
    } for task in selected]
    route_legs = [{
        "from": a.name, "to": b.name,
        "distance_km": lunar_distance_km(a.latitude, a.longitude, b.latitude, b.longitude),
    } for a, b in pairwise(route_sites)]
    distance = sum(leg["distance_km"] for leg in route_legs)
    departure = to_utc_naive(payload.departure_time) or utcnow()
    return_trip = plan.return_trip if plan else payload.return_trip
    slowest_speed = min(rover["speed_kmh"] for rover in ROVER_PROFILES)
    estimated_hours = sum(leg["distance_km"] / slowest_speed for leg in route_legs)
    estimated_hours *= 2 if return_trip else 1
    estimated_hours += sum(task["duration_hours"] for task in simulation_tasks)
    if estimated_hours > MAX_HORIZON_HOURS:
        raise HTTPException(422, "Rover missions are limited to 30 simulated days")
    light_windows = load_windows(db, destination.id)
    note = None
    sun_points = []
    sunlight_source = "Saved illumination windows (manual)"
    if payload.sun_mode == "live":
        duration_hours = sum(leg["distance_km"] / slowest_speed for leg in route_legs)
        duration_hours += sum(task["duration_hours"] for task in simulation_tasks)
        if return_trip:
            duration_hours += sum(leg["distance_km"] / slowest_speed for leg in route_legs)
        duration_hours = max(24, duration_hours + 1)
        sun_points, sunlight_source, note = sunlight_for_site(
            destination, departure, duration_hours, 30
        )
    if plan:
        task_payloads = plan.task_payload_kg or {}
    else:
        task_payloads = payloads
    for task in simulation_tasks:
        task["payload_kg"] = float(task_payloads.get(str(task["id"]), 0))
    ordered_tasks = simulation_tasks
    if payload.optimize_order and len(simulation_tasks) > 1:
        if len(simulation_tasks) > 6:
            raise HTTPException(422, "Order suggestions support up to 6 tasks")
        ordered_tasks = suggest_task_order(
            ROVER_PROFILES, route_legs, simulation_tasks, departure, light_windows,
            sun_points, return_trip, charges,
        )
    return {
        "origin_site": origin.name,
        "destination_site": destination.name,
        "route": [site.name for site in route_sites],
        "return_trip": return_trip,
        "distance_km": round(distance * (2 if return_trip else 1), 2),
        "departure_time": departure,
        "sunlight_source": sunlight_source,
        "optimized_order": payload.optimize_order,
        "note": note,
        "tasks": [
            schemas.MissionTaskSummary(
                id=task.id,
                name=task.name,
                duration_hours=task.duration_hours,
                power_kw=round(sum(equipment.power_kw for equipment in task.equipment), 3),
                payload_kg=float(task_payloads.get(str(task.id), 0)),
            )
            for task in (tasks_by_id[t["id"]] for t in ordered_tasks)
        ],
        "results": [
            simulate_rover(
                rover, route_legs, ordered_tasks, departure, light_windows,
                sun_points, return_trip, charges.get(rover["id"]),
            )
            for rover in ROVER_PROFILES
        ],
        "recommended_task_ids": [task["id"] for task in ordered_tasks],
    }


@planner_router.get("/sites/{site_id}/sunlight", response_model=schemas.SiteSunlightResponse)
def site_sunlight(site_id: int, horizon_hours: int = Query(24, ge=1, le=720),
                  step_minutes: int = Query(60, ge=10, le=360),
                  db: Session = Depends(get_db)):
    site = get_or_404(db, models.Site, site_id)
    if site.latitude is None or site.longitude is None:
        raise HTTPException(422, "Add latitude and longitude to this site first")
    start = utcnow().replace(second=0, microsecond=0)
    points, source, note = sunlight_for_site(site, start, horizon_hours, step_minutes)
    return {"site_id": site_id, "source": source, "current": points[0], "points": points, "note": note}


@planner_router.get("/sites/{site_id}/simulation-clock", response_model=schemas.SiteSimulationClockResponse)
def get_simulation_clock(site_id: int, db: Session = Depends(get_db)):
    get_or_404(db, models.Site, site_id)
    clock = db.get(models.SiteSimulationClock, site_id)
    return {"site_id": site_id, "current_time": clock.current_time if clock else utcnow()}


@planner_router.post("/sites/{site_id}/simulation/advance", response_model=schemas.SimulationAdvanceResponse,
                     dependencies=[Depends(require_planner)])
def advance_site_simulation(site_id: int, payload: schemas.SimulationAdvanceRequest,
                            db: Session = Depends(get_db)):
    site = get_or_404(db, models.Site, site_id)
    clock = db.get(models.SiteSimulationClock, site_id)
    start = clock.current_time if clock else utcnow().replace(minute=0, second=0, microsecond=0)
    step = 60
    forecasts = forecast_site_batteries(db, site_id, start, payload.hours, step)
    for forecast in forecasts:
        battery = get_or_404(db, models.Battery, forecast.battery_id)
        battery.charge_kwh = forecast.points[-1].predicted_charge_kwh
    next_time = start + timedelta(hours=payload.hours)
    if clock is None:
        clock = models.SiteSimulationClock(site_id=site_id, current_time=next_time)
        db.add(clock)
    else:
        clock.current_time = next_time
    commit(db)
    return {"site_id": site.id, "current_time": next_time, "advanced_hours": payload.hours,
            "batteries": db.query(models.Battery).filter(models.Battery.site_id == site_id).all()}


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
def seed_demo(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Add a repeatable set of judge scenarios without deleting existing records."""
    now = utcnow().replace(minute=0, second=0, microsecond=0)

    def at(hours: float) -> datetime:
        return now + timedelta(hours=hours)

    def site_named(name, latitude, longitude):
        site = db.query(models.Site).filter(models.Site.name == name).first()
        if site is None:
            site = models.Site(name=name, latitude=latitude, longitude=longitude)
            db.add(site)
            db.flush()
        else:
            if site.latitude is None:
                site.latitude = latitude
            if site.longitude is None:
                site.longitude = longitude
        return site

    def named(model, site_id, name, **values):
        obj = db.query(model).filter(model.site_id == site_id, model.name == name).first()
        if obj is None:
            obj = model(site_id=site_id, name=name, **values)
            db.add(obj)
            db.flush()
        elif name.startswith("Judge Demo"):
            for key, value in values.items():
                setattr(obj, key, value)
        return obj

    def task_named(site, name, start, duration, status, team, equipment):
        task = db.query(models.Task).filter(
            models.Task.site_id == site.id, models.Task.name == name
        ).first()
        if task is None:
            task = models.Task(
                site_id=site.id, team_id=team.id if team else None, name=name,
                start_time=start, duration_hours=duration, priority=4, status=status,
            )
            task.equipment = equipment
            db.add(task)
            db.flush()
        else:
            task.start_time = start
            task.duration_hours = duration
            task.status = status
            task.team_id = team.id if team else None
            task.priority = 4
            task.equipment = equipment
        return task

    def delivery_if_missing(equipment, arrival, setup, status):
        if equipment.delivery is None:
            db.add(models.Delivery(
                site_id=equipment.site_id, equipment_id=equipment.id,
                expected_arrival=arrival, setup_hours=setup, status=status,
            ))
        else:
            equipment.delivery.expected_arrival = arrival
            equipment.delivery.setup_hours = setup
            equipment.delivery.status = status

    base = site_named(DEMO_SITE_NAME, -89.9, 0.0)
    ridge = site_named("Demo Ridge", -85.0, 15.0)
    relay = site_named("Demo Crater Relay", -88.1, 10.0)

    base_habitat = named(models.Habitat, base.id, "Judge Demo Habitat")
    ridge_habitat = named(models.Habitat, ridge.id, "Judge Demo Ridge Lab")
    relay_habitat = named(models.Habitat, relay.id, "Judge Demo Relay Shelter")
    base_team = named(models.Team, base.id, "Judge Demo Power Crew", specialization="Base power and maintenance")
    rover_team = named(models.Team, base.id, "Judge Demo Rover Crew", specialization="Surface delivery and survey")
    ridge_team = named(models.Team, ridge.id, "Judge Demo Ridge Crew", specialization="Drilling and sample pickup")
    relay_team = named(models.Team, relay.id, "Judge Demo Relay Crew", specialization="Communications and power recovery")

    for site in (base, ridge, relay):
        windows = db.query(models.IlluminationWindow).filter(
            models.IlluminationWindow.site_id == site.id
        ).all()
        if not windows:
            db.add_all([
                models.IlluminationWindow(site_id=site.id, start_time=at(-4), end_time=at(8)),
                models.IlluminationWindow(site_id=site.id, start_time=at(28), end_time=at(40)),
            ])
        elif not any(window.start_time <= now < window.end_time for window in windows):
            demo_start, demo_end = at(-1), at(8)
            if not any(window.start_time == demo_start and window.end_time == demo_end for window in windows):
                db.add(models.IlluminationWindow(
                    site_id=site.id, start_time=demo_start, end_time=demo_end
                ))

    # Base camp: overlapping loads create a power-budget warning; two deliveries arrive too late.
    named(models.PowerSource, base.id, "Solar array A", max_output_kw=6, is_available=True)
    named(models.PowerSource, base.id, "Solar array B", max_output_kw=4, is_available=True)
    named(models.PowerSource, base.id, "Judge Demo standby array (offline)", max_output_kw=2, is_available=False)
    named(models.Battery, base.id, "Judge Demo low reserve", capacity_kwh=18, charge_kwh=2)
    named(models.Battery, base.id, "Judge Demo reserve bank", capacity_kwh=30, charge_kwh=26)
    life_support = named(
        models.Equipment, base.id, "Judge Demo life support", habitat_id=base_habitat.id,
        power_kw=2.5, status=EquipmentStatus.in_use, priority=5,
    )
    base_comms = named(
        models.Equipment, base.id, "Judge Demo base comms", habitat_id=base_habitat.id,
        power_kw=0.5, status=EquipmentStatus.in_use, priority=4,
    )
    base_drill = named(
        models.Equipment, base.id, "Judge Demo regolith drill", habitat_id=None,
        power_kw=5.5, status=EquipmentStatus.available, priority=2,
    )
    base_rover = named(
        models.Equipment, base.id, "Judge Demo survey rover", habitat_id=None,
        power_kw=2.0, status=EquipmentStatus.in_use, priority=3,
    )
    base_camera = named(
        models.Equipment, base.id, "Judge Demo installed camera", habitat_id=base_habitat.id,
        power_kw=0.2, status=EquipmentStatus.available, priority=1,
    )
    base_sensor = named(
        models.Equipment, base.id, "Judge Demo late sensor", habitat_id=base_habitat.id,
        power_kw=0.8, status=EquipmentStatus.available, priority=2,
    )
    named(
        models.Equipment, base.id, "Judge Demo offline spare tool", habitat_id=None,
        power_kw=0.4, status=EquipmentStatus.offline, priority=1,
    )
    delivery_if_missing(base_drill, at(10), 2, DeliveryStatus.delayed)
    delivery_if_missing(base_rover, at(8), 1, DeliveryStatus.in_transit)
    delivery_if_missing(base_camera, at(-8), 0, DeliveryStatus.installed)
    delivery_if_missing(base_sensor, at(5), 2, DeliveryStatus.arrived)
    task_named(base, "Judge Demo — Base operations (in progress)", at(-2), 24,
               TaskStatus.in_progress, base_team, [life_support, base_comms])
    task_named(base, "Judge Demo — Drill before delivery", at(1), 8,
               TaskStatus.planned, base_team, [base_drill])
    task_named(base, "Judge Demo — Rover before delivery", at(2), 6,
               TaskStatus.planned, rover_team, [base_rover])
    task_named(base, "Judge Demo — Late sensor setup", at(3), 2,
               TaskStatus.planned, base_team, [base_sensor])
    task_named(base, "Judge Demo — Completed inspection", at(-30), 1,
               TaskStatus.done, base_team, [base_camera])
    task_named(base, "Judge Demo — Cancelled repair", at(4), 2,
               TaskStatus.cancelled, base_team, [base_camera])

    # Ridge: a healthy site with a quick success, energy-heavy work, and a cargo-limit case.
    named(models.PowerSource, ridge.id, "Judge Demo ridge array", max_output_kw=16, is_available=True)
    named(models.PowerSource, ridge.id, "Judge Demo ridge backup (offline)", max_output_kw=3, is_available=False)
    named(models.Battery, ridge.id, "Judge Demo healthy bank", capacity_kwh=120, charge_kwh=105)
    sample_carrier = named(
        models.Equipment, ridge.id, "Judge Demo sample carrier", habitat_id=ridge_habitat.id,
        power_kw=0.25, status=EquipmentStatus.available, priority=3,
    )
    ridge_drill = named(
        models.Equipment, ridge.id, "Judge Demo ridge drill", habitat_id=None,
        power_kw=2.4, status=EquipmentStatus.available, priority=2,
    )
    cargo_pod = named(
        models.Equipment, ridge.id, "Judge Demo cargo pod", habitat_id=None,
        power_kw=0.2, status=EquipmentStatus.available, priority=3,
    )
    night_instruments = named(
        models.Equipment, ridge.id, "Judge Demo night instruments", habitat_id=ridge_habitat.id,
        power_kw=4.0, status=EquipmentStatus.in_use, priority=2,
    )
    delivery_if_missing(sample_carrier, at(-8), 0, DeliveryStatus.installed)
    delivery_if_missing(ridge_drill, at(16), 2, DeliveryStatus.delayed)
    delivery_if_missing(cargo_pod, at(5), 2, DeliveryStatus.arrived)
    quick_task = task_named(ridge, "Judge Demo — Quick sample pickup", at(1), 1,
                            TaskStatus.planned, rover_team, [sample_carrier])
    drill_task = task_named(ridge, "Judge Demo — Ridge drill run", at(2), 4,
                           TaskStatus.planned, ridge_team, [ridge_drill])
    heavy_task = task_named(ridge, "Judge Demo — Heavy cargo delivery", at(1), 1,
                           TaskStatus.planned, rover_team, [cargo_pod])
    task_named(ridge, "Judge Demo — Night power test", at(8), 8,
               TaskStatus.planned, ridge_team, [night_instruments])
    task_named(ridge, "Judge Demo — Ongoing ridge survey", at(-1), 4,
               TaskStatus.in_progress, ridge_team, [sample_carrier])
    task_named(ridge, "Judge Demo — Completed ridge survey", at(-20), 2,
               TaskStatus.done, ridge_team, [sample_carrier])
    task_named(ridge, "Judge Demo — Cancelled cargo run", at(10), 2,
               TaskStatus.cancelled, rover_team, [cargo_pod])

    # Relay: a deliberate outage case, separate from the healthy ridge.
    named(models.PowerSource, relay.id, "Judge Demo relay array", max_output_kw=0.5, is_available=True)
    named(models.Battery, relay.id, "Judge Demo critical bank", capacity_kwh=10, charge_kwh=1)
    relay_radio = named(
        models.Equipment, relay.id, "Judge Demo relay transmitter", habitat_id=relay_habitat.id,
        power_kw=2.2, status=EquipmentStatus.in_use, priority=5,
    )
    task_named(relay, "Judge Demo — Relay outage drill", at(-1), 8,
               TaskStatus.in_progress, relay_team, [relay_radio])
    task_named(relay, "Judge Demo — Relay antenna alignment", at(12), 2,
               TaskStatus.planned, relay_team, [relay_radio])

    clock = db.get(models.SiteSimulationClock, base.id)
    if clock is None:
        db.add(models.SiteSimulationClock(site_id=base.id, current_time=now))
    else:
        clock.current_time = now
    plan_name = "Judge Demo — Quick ridge delivery"
    plan = db.query(models.MissionPlan).filter_by(owner_id=user.id, name=plan_name).first()
    if plan is None:
        plan = models.MissionPlan(
            owner_id=user.id, name=plan_name, route_site_ids=[base.id, ridge.id],
            task_ids=[quick_task.id], return_trip=False,
            rover_charges={"apollo_lrv": 8.71, "scout_concept": 105, "cargo_concept": 240},
            task_payload_kg={str(quick_task.id): 25}, shared_user_ids=[],
        )
        db.add(plan)
    else:
        plan.route_site_ids = [base.id, ridge.id]
        plan.task_ids = [quick_task.id]
        plan.return_trip = False
        plan.rover_charges = {"apollo_lrv": 8.71, "scout_concept": 105, "cargo_concept": 240}
        plan.task_payload_kg = {str(quick_task.id): 25}
    commit(db)
    db.flush()
    plan = db.query(models.MissionPlan).filter_by(owner_id=user.id, name=plan_name).first()
    return {
        "site_id": base.id,
        "destination_site_id": ridge.id,
        "waypoint_site_id": relay.id,
        "site_ids": [base.id, ridge.id, relay.id],
        "plan_id": plan.id,
        "demo_scenarios": [
            {"name": "Quick sample pickup", "task_id": quick_task.id},
            {"name": "Energy-heavy drill", "task_id": drill_task.id},
            {"name": "Heavy cargo (tests payload limits)", "task_id": heavy_task.id},
            {"name": "Relay power shortage", "site_id": relay.id},
            {"name": "Delivery conflicts and base overload", "site_id": base.id},
        ],
        "try": [
            f"/sites/{base.id}/forecast",
            f"/sites/{base.id}/power-budget",
            f"/sites/{base.id}/conflicts",
        ],
    }


@app.get("/", tags=["Meta"])
def root():
    return {"status": "ok", "docs": "/docs"}
