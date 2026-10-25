from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Table,
    CheckConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import relationship

from database import Base
from enums import EquipmentStatus, TaskStatus, DeliveryStatus

# Convention: every DateTime is stored as naive UTC. The schemas convert
# incoming timezone-aware values to UTC before they reach the DB.
# Always use datetime.now(timezone.utc) in code, never datetime.now().

# Priority convention: 1 to 5, 5 = most critical. Shed the lowest first.

task_equipment = Table(
    "task_equipment",
    Base.metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("equipment_id", Integer, ForeignKey("equipment.id"), primary_key=True),
)


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(30), unique=True, nullable=False)

    users = relationship("User", back_populates="role")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(120), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)

    role = relationship("Role", back_populates="users")


class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    habitats = relationship("Habitat", back_populates="site", cascade="all, delete-orphan")
    power_sources = relationship("PowerSource", back_populates="site", cascade="all, delete-orphan")
    batteries = relationship("Battery", back_populates="site", cascade="all, delete-orphan")
    equipment = relationship("Equipment", back_populates="site", cascade="all, delete-orphan")
    teams = relationship("Team", back_populates="site", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="site", cascade="all, delete-orphan")
    deliveries = relationship("Delivery", back_populates="site", cascade="all, delete-orphan")
    illumination_windows = relationship(
        "IlluminationWindow", back_populates="site", cascade="all, delete-orphan"
    )


class IlluminationWindow(Base):
    """A period when the site is in sunlight. Anything outside a window is dark."""

    __tablename__ = "illumination_windows"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="ck_illumination_end_after_start"),
    )

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False, index=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)

    site = relationship("Site", back_populates="illumination_windows")


class Habitat(Base):
    __tablename__ = "habitats"

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    name = Column(String(100), nullable=False)

    site = relationship("Site", back_populates="habitats")
    equipment = relationship("Equipment", back_populates="habitat")


class PowerSource(Base):
    """All sources are assumed solar: output is max_output_kw while lit, 0 in the dark."""

    __tablename__ = "power_sources"
    __table_args__ = (
        CheckConstraint("max_output_kw > 0", name="ck_power_source_output_positive"),
    )

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    name = Column(String(100), nullable=False)
    max_output_kw = Column(Float, nullable=False)
    is_available = Column(Boolean, default=True, nullable=False)

    site = relationship("Site", back_populates="power_sources")


class Battery(Base):
    __tablename__ = "batteries"
    __table_args__ = (
        CheckConstraint("capacity_kwh > 0", name="ck_battery_capacity_positive"),
        CheckConstraint("charge_kwh >= 0", name="ck_battery_charge_non_negative"),
        CheckConstraint("charge_kwh <= capacity_kwh", name="ck_battery_charge_lte_capacity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    name = Column(String(100), nullable=False)
    capacity_kwh = Column(Float, nullable=False)
    charge_kwh = Column(Float, nullable=False)

    site = relationship("Site", back_populates="batteries")


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    name = Column(String(100), nullable=False)
    specialization = Column(String(100), nullable=True)

    site = relationship("Site", back_populates="teams")
    tasks = relationship("Task", back_populates="team")


class Equipment(Base):
    __tablename__ = "equipment"

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    habitat_id = Column(Integer, ForeignKey("habitats.id"), nullable=True)
    name = Column(String(100), nullable=False)
    power_kw = Column(Float, nullable=False)
    status = Column(
        SAEnum(EquipmentStatus),
        default=EquipmentStatus.available,
        nullable=False,
    )
    priority = Column(Integer, default=1, nullable=False)

    site = relationship("Site", back_populates="equipment")
    habitat = relationship("Habitat", back_populates="equipment")
    tasks = relationship(
        "Task",
        secondary=task_equipment,
        back_populates="equipment",
    )
    delivery = relationship(
        "Delivery",
        back_populates="equipment",
        uselist=False,
        cascade="all, delete-orphan",
    )


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    name = Column(String(150), nullable=False)
    start_time = Column(DateTime, nullable=False)
    duration_hours = Column(Float, nullable=False)
    priority = Column(Integer, default=1, nullable=False)
    status = Column(
        SAEnum(TaskStatus),
        default=TaskStatus.planned,
        nullable=False,
    )

    site = relationship("Site", back_populates="tasks")
    team = relationship("Team", back_populates="tasks")
    equipment = relationship(
        "Equipment",
        secondary=task_equipment,
        back_populates="tasks",
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    equipment_id = Column(
        Integer,
        ForeignKey("equipment.id"),
        unique=True,
        nullable=False,
    )
    expected_arrival = Column(DateTime, nullable=False)
    setup_hours = Column(Float, default=0, nullable=False)
    status = Column(
        SAEnum(DeliveryStatus),
        default=DeliveryStatus.in_transit,
        nullable=False,
    )

    site = relationship("Site", back_populates="deliveries")
    equipment = relationship("Equipment", back_populates="delivery")