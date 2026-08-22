"""MES 数据库模型（兼容 C++ 权威实现的 SQLite schema）。

所有时间字段使用 ISO-8601 UTC 文本（``YYYY-MM-DDTHH:MM:SSZ``）。
视图在 ``init_db`` 中以原生 SQL 创建。
"""

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    resource_code: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[str] = mapped_column(String(32))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    before_data: Mapped[str] = mapped_column(Text, nullable=True)
    after_data: Mapped[str] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(64))


class MasterPlant(Base):
    __tablename__ = "master_plants"

    plant_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    plant_name: Mapped[str] = mapped_column(String(128), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[int] = mapped_column(Integer, default=1)


class MasterMaterial(Base):
    __tablename__ = "master_materials"

    material_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_name: Mapped[str] = mapped_column(String(128), default="")
    unit_code: Mapped[str] = mapped_column(String(16), default="")
    lot_controlled: Mapped[int] = mapped_column(Integer, default=1)
    serial_controlled: Mapped[int] = mapped_column(Integer, default=0)


class MasterRouting(Base):
    __tablename__ = "master_routings"

    routing_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    approved_by: Mapped[str] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[str] = mapped_column(String(32), nullable=True)
    effective_from: Mapped[str] = mapped_column(String(32), nullable=True)
    effective_to: Mapped[str] = mapped_column(String(32), nullable=True)


class MasterRoutingOperation(Base):
    __tablename__ = "master_routing_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    routing_code: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16), index=True)
    sequence: Mapped[int] = mapped_column(Integer, index=True)
    operation_code: Mapped[str] = mapped_column(String(64))
    work_center_code: Mapped[str] = mapped_column(String(64), default="")
    quality_gate: Mapped[int] = mapped_column(Integer, default=0)
    allow_skip: Mapped[int] = mapped_column(Integer, default=0)
    standard_cycle_seconds: Mapped[int] = mapped_column(Integer, default=0)


class AssetEquipment(Base):
    __tablename__ = "asset_equipment"

    equipment_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    work_center_code: Mapped[str] = mapped_column(String(64), default="")
    equipment_name: Mapped[str] = mapped_column(String(128), default="")
    criticality: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    current_status: Mapped[str] = mapped_column(String(16), default="OFFLINE")
    last_heartbeat_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MasterTeam(Base):
    __tablename__ = "master_teams"

    team_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    team_name: Mapped[str] = mapped_column(String(128), default="")


class MasterWorker(Base):
    __tablename__ = "master_workers"

    worker_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    team_code: Mapped[str] = mapped_column(String(32), default="")
    display_name: Mapped[str] = mapped_column(String(128), default="")


class MasterWorkerQualification(Base):
    __tablename__ = "master_worker_qualifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(32), index=True)
    qualification: Mapped[str] = mapped_column(String(64), index=True)
    granted_by: Mapped[str] = mapped_column(String(32), default="")


class ProductionWorkOrder(Base):
    __tablename__ = "production_work_orders"

    work_order_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    material_code: Mapped[str] = mapped_column(String(64))
    routing_code: Mapped[str] = mapped_column(String(64), default="")
    routing_version: Mapped[str] = mapped_column(String(16), default="")
    bom_code: Mapped[str] = mapped_column(String(64), nullable=True)
    bom_version: Mapped[str] = mapped_column(String(16), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    planned_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    completed_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    rejected_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    planned_start_at: Mapped[str] = mapped_column(String(32), nullable=True)
    planned_end_at: Mapped[str] = mapped_column(String(32), nullable=True)
    actual_start_at: Mapped[str] = mapped_column(String(32), nullable=True)
    actual_end_at: Mapped[str] = mapped_column(String(32), nullable=True)
    cancelled_at: Mapped[str] = mapped_column(String(32), nullable=True)
    cancelled_by: Mapped[str] = mapped_column(String(64), nullable=True)


class ProductionWorkOrderOperation(Base):
    __tablename__ = "production_work_order_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_number: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer, index=True)
    operation_code: Mapped[str] = mapped_column(String(64))
    work_center_code: Mapped[str] = mapped_column(String(64), default="")
    quality_gate: Mapped[int] = mapped_column(Integer, default=0)
    allow_skip: Mapped[int] = mapped_column(Integer, default=0)
    standard_cycle_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")


class ProductionWorkOrderLineage(Base):
    __tablename__ = "production_work_order_lineage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_work_order_number: Mapped[str] = mapped_column(String(64), index=True)
    target_work_order_number: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(16), default="SPLIT")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    occurred_at: Mapped[str] = mapped_column(String(32))


class StationSession(Base):
    __tablename__ = "station_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    station_code: Mapped[str] = mapped_column(String(64))
    work_order_number: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    operator_id: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[str] = mapped_column(String(32))
    ended_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionExecutionEvent(Base):
    __tablename__ = "production_execution_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), default="")
    operation_sequence: Mapped[int] = mapped_column(Integer, default=0)
    operation_code: Mapped[str] = mapped_column(String(64), default="")
    station_code: Mapped[str] = mapped_column(String(64), nullable=True)
    equipment_code: Mapped[str] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(32), default="")
    event_type: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[str] = mapped_column(String(32))


class ProductionExecutionParameter(Base):
    __tablename__ = "production_execution_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, default=0)
    code: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float, default=0.0)
    lower_limit: Mapped[float] = mapped_column(Float, nullable=True)
    upper_limit: Mapped[float] = mapped_column(Float, nullable=True)
    in_spec: Mapped[int] = mapped_column(Integer, default=1)
    recorded_at: Mapped[str] = mapped_column(String(32))


class ProductionProductUnit(Base):
    __tablename__ = "production_product_units"

    serial_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_number: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="CREATED")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class ProductionMaterialConsumption(Base):
    __tablename__ = "production_material_consumptions"

    consumption_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    lot_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit_code: Mapped[str] = mapped_column(String(16), default="EA")
    consumed_by: Mapped[str] = mapped_column(String(32), default="system")
    consumed_at: Mapped[str] = mapped_column(String(32))


class QualityInspectionLot(Base):
    __tablename__ = "quality_inspection_lots"

    lot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), default="")
    work_order_number: Mapped[str] = mapped_column(String(64), default="")
    plan_code: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    operator_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[str] = mapped_column(String(32))


class QualityInspection(Base):
    __tablename__ = "quality_inspections"

    inspection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lot_id: Mapped[str] = mapped_column(String(64), default="")
    serial_number: Mapped[str] = mapped_column(String(64), default="")
    plan_code: Mapped[str] = mapped_column(String(64), default="")
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    disposition: Mapped[str] = mapped_column(String(16), default="PASS")
    defect_code: Mapped[str] = mapped_column(String(64), nullable=True)
    inspector_id: Mapped[str] = mapped_column(String(32), default="")
    inspected_at: Mapped[str] = mapped_column(String(32))


class QualityNonconformance(Base):
    __tablename__ = "quality_nonconformances"

    nonconformance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), default="")
    work_order_number: Mapped[str] = mapped_column(String(64), default="")
    defect_code: Mapped[str] = mapped_column(String(64), default="")
    severity: Mapped[str] = mapped_column(String(16), default="MINOR")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    reported_by: Mapped[str] = mapped_column(String(32), default="")
    reported_at: Mapped[str] = mapped_column(String(32))


class QualityCapa(Base):
    __tablename__ = "quality_capas"

    capa_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonconformance_id: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    owner_id: Mapped[str] = mapped_column(String(32), default="")
    due_at: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class QualitySpcMeasurement(Base):
    __tablename__ = "quality_spc_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    characteristic_code: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    measured_at: Mapped[str] = mapped_column(String(32))


class AssetEquipmentEvent(Base):
    __tablename__ = "asset_equipment_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64), default="")
    occurred_at: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(64), unique=True)


class AssetEquipmentCounter(Base):
    __tablename__ = "asset_equipment_counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    counter_code: Mapped[str] = mapped_column(String(64), default="OUT")
    value: Mapped[float] = mapped_column(Float, default=0.0)
    recorded_at: Mapped[str] = mapped_column(String(32))


class AssetMaintenanceWorkOrder(Base):
    __tablename__ = "asset_maintenance_work_orders"

    maintenance_work_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    equipment_code: Mapped[str] = mapped_column(String(64), default="")
    maintenance_type: Mapped[str] = mapped_column(String(16), default="CORRECTIVE")
    description: Mapped[str] = mapped_column(String(256), default="")
    assignee_id: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    due_at: Mapped[str] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=True)


class TraceAndonEvent(Base):
    __tablename__ = "trace_andon_events"

    andon_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    severity: Mapped[str] = mapped_column(String(16), default="WARNING")
    category: Mapped[str] = mapped_column(String(32), default="QUALITY")
    resource_type: Mapped[str] = mapped_column(String(64), default="")
    resource_code: Mapped[str] = mapped_column(String(64), default="")
    related_work_order_number: Mapped[str] = mapped_column(String(64), default="")
    related_serial_number: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(String(256), default="")
    raised_at: Mapped[str] = mapped_column(String(32))
    acknowledged_at: Mapped[str] = mapped_column(String(32), nullable=True)
    acknowledged_by: Mapped[str] = mapped_column(String(32), nullable=True)
    closed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    closed_by: Mapped[str] = mapped_column(String(32), nullable=True)


class MaterialInventoryBalance(Base):
    __tablename__ = "material_inventory_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    location_code: Mapped[str] = mapped_column(String(32), default="RAW-STORE")
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    lot_number: Mapped[str] = mapped_column(String(64), default="")
    batch_number: Mapped[str] = mapped_column(String(64), default="")
    on_hand_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    reserved_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="AVAILABLE")
    expiry_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MaterialInventoryReservation(Base):
    __tablename__ = "material_inventory_reservations"

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    lot_number: Mapped[str] = mapped_column(String(64), default="")
    work_order_number: Mapped[str] = mapped_column(String(64), default="")
    operation_sequence: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    expires_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MaterialTransaction(Base):
    __tablename__ = "material_material_transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    lot_number: Mapped[str] = mapped_column(String(64), default="")
    transaction_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    location_code: Mapped[str] = mapped_column(String(32), default="")
    reference_type: Mapped[str] = mapped_column(String(16), default="")
    reference_id: Mapped[str] = mapped_column(String(64), default="")
    transaction_at: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(32), default="system")


class IntegrationInboxMessage(Base):
    __tablename__ = "integration_inbox_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64))
    message_type: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="RECEIVED")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    received_at: Mapped[str] = mapped_column(String(32))
    processed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str] = mapped_column(String(256), nullable=True)


class IntegrationOutboxMessage(Base):
    __tablename__ = "integration_outbox_messages"

    outbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), default="")
    aggregate_id: Mapped[str] = mapped_column(String(64), default="")
    event_type: Mapped[str] = mapped_column(String(128), default="")
    target_system: Mapped[str] = mapped_column(String(64), default="ERP")
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32))
    published_at: Mapped[str] = mapped_column(String(32), nullable=True)


class IntegrationOutboxDeliveryAttempt(Base):
    __tablename__ = "integration_outbox_delivery_attempts"

    attempt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outbox_id: Mapped[str] = mapped_column(String(64), index=True)
    attempted_at: Mapped[str] = mapped_column(String(32))


class IntegrationOutboxDeadLetter(Base):
    __tablename__ = "integration_outbox_dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outbox_id: Mapped[str] = mapped_column(String(64), index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(256), nullable=True)
    failed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    replayed_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ReportingEquipmentOeeHourly(Base):
    __tablename__ = "reporting_equipment_oee_hourly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    hour_bucket: Mapped[str] = mapped_column(String(32), index=True)
    actual_output: Mapped[float] = mapped_column(Float, default=0.0)
    good_output: Mapped[float] = mapped_column(Float, default=0.0)
    running_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    planned_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    ideal_cycle_seconds: Mapped[float] = mapped_column(Float, default=0.0)


class ReportingAndonSummary(Base):
    __tablename__ = "reporting_andon_summary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_code: Mapped[str] = mapped_column(String(32), index=True)
    summary_date: Mapped[str] = mapped_column(String(16), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="WARNING")
    raised_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_count: Mapped[int] = mapped_column(Integer, default=0)


class ReportingShiftProduction(Base):
    __tablename__ = "reporting_shift_production"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_code: Mapped[str] = mapped_column(String(32), index=True)
    shift_date: Mapped[str] = mapped_column(String(16), index=True)
    shift_code: Mapped[str] = mapped_column(String(16), default="DAY")
    planned_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    completed_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    good_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    rejected_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    last_event_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MasterRecipe(Base):
    __tablename__ = "master_recipes"

    recipe_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    recipe_name: Mapped[str] = mapped_column(String(128), default="")
    product_material_code: Mapped[str] = mapped_column(String(64))
    target_batch_size: Mapped[float] = mapped_column(Float)
    unit_code: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    effective_from: Mapped[str] = mapped_column(String(32), nullable=True)
    effective_to: Mapped[str] = mapped_column(String(32), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MasterRecipeComponent(Base):
    __tablename__ = "master_recipe_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipe_code: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16), index=True)
    component_sequence: Mapped[int] = mapped_column(Integer, index=True)
    material_code: Mapped[str] = mapped_column(String(64))
    phase_code: Mapped[str] = mapped_column(String(64))
    target_quantity: Mapped[float] = mapped_column(Float)
    lower_limit: Mapped[float] = mapped_column(Float)
    upper_limit: Mapped[float] = mapped_column(Float)
    unit_code: Mapped[str] = mapped_column(String(16))
    required: Mapped[int] = mapped_column(Integer, default=1)
    hazardous: Mapped[int] = mapped_column(Integer, default=0)


class MasterRecipeParameter(Base):
    __tablename__ = "master_recipe_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipe_code: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16), index=True)
    step_sequence: Mapped[int] = mapped_column(Integer, index=True)
    parameter_code: Mapped[str] = mapped_column(String(64))
    parameter_name: Mapped[str] = mapped_column(String(128), default="")
    target_value: Mapped[float] = mapped_column(Float, default=0.0)
    lower_limit: Mapped[float] = mapped_column(Float, default=0.0)
    upper_limit: Mapped[float] = mapped_column(Float, default=0.0)
    unit_code: Mapped[str] = mapped_column(String(16))
    required: Mapped[int] = mapped_column(Integer, default=1)


class ProductionBatch(Base):
    __tablename__ = "production_batches"

    batch_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), default="PLANT-A")
    product_material_code: Mapped[str] = mapped_column(String(64), default="")
    recipe_code: Mapped[str] = mapped_column(String(64), default="")
    recipe_version: Mapped[str] = mapped_column(String(16), default="")
    equipment_code: Mapped[str] = mapped_column(String(64), default="")
    planned_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    actual_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit_code: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    quality_disposition: Mapped[str] = mapped_column(String(16), nullable=True)
    hold_reason: Mapped[str] = mapped_column(String(256), nullable=True)
    started_at: Mapped[str] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)


class ProductionBatchCharge(Base):
    __tablename__ = "production_batch_charges"

    charge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(64), index=True)
    component_sequence: Mapped[int] = mapped_column(Integer, index=True)
    material_code: Mapped[str] = mapped_column(String(64))
    lot_number: Mapped[str] = mapped_column(String(64), default="")
    quantity: Mapped[float] = mapped_column(Float)
    unit_code: Mapped[str] = mapped_column(String(16))
    charged_by: Mapped[str] = mapped_column(String(32), default="system")
    charged_at: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(64), nullable=True)


class ProductionBatchParameter(Base):
    __tablename__ = "production_batch_parameters"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(64), index=True)
    step_sequence: Mapped[int] = mapped_column(Integer, index=True)
    parameter_code: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    lower_limit: Mapped[float] = mapped_column(Float, nullable=True)
    upper_limit: Mapped[float] = mapped_column(Float, nullable=True)
    unit_code: Mapped[str] = mapped_column(String(16), default="")
    in_spec: Mapped[int] = mapped_column(Integer, default=1)
    recorded_by: Mapped[str] = mapped_column(String(32), default="system")
    recorded_at: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(64), nullable=True)


class QualityBatchDisposition(Base):
    __tablename__ = "quality_batch_dispositions"

    disposition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(64), index=True)
    disposition: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=True)
    deviation_reference: Mapped[str] = mapped_column(String(128), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(32), default="")
    electronic_signature: Mapped[str] = mapped_column(String(64), default="")
    reviewed_at: Mapped[str] = mapped_column(String(32))
