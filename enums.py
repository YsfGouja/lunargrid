import enum

class EquipmentStatus(str, enum.Enum):
    available = "available"
    in_use = "in_use"
    offline = "offline"

class TaskStatus(str, enum.Enum):
    planned = "planned"
    in_progress = "in_progress"
    done = "done"
    cancelled = "cancelled"

class DeliveryStatus(str, enum.Enum):
    in_transit = "in_transit"
    arrived = "arrived"
    installed = "installed"
    delayed = "delayed"