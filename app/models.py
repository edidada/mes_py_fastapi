"""MES 数据库模型（兼容 C++ 权威实现的 SQLite schema）。

所有时间字段使用 ISO-8601 UTC 文本（``YYYY-MM-DDTHH:MM:SSZ``）。
视图在 ``init_db`` 中以原生 SQL 创建。
"""

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    plant_name: Mapped[str] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=True)


class MasterMaterial(Base):
    __tablename__ = "master_materials"

    material_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_name: Mapped[str] = mapped_column(String(128), nullable=True)
    unit_code: Mapped[str] = mapped_column(String(16), nullable=True)
    lot_controlled: Mapped[int] = mapped_column(Integer, nullable=True)
    serial_controlled: Mapped[int] = mapped_column(Integer, nullable=True)


class MasterBom(Base):
    __tablename__ = "master_boms"

    bom_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    effective_from: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionPlan(Base):
    __tablename__ = "production_production_plans"
    __table_args__ = (
        UniqueConstraint(
            "source_system", "external_reference", "plant_code", name="uq_plan_ref"
        ),
    )

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64))
    external_reference: Mapped[str] = mapped_column(String(128))
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    material_code: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=True)
    due_at: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=True)


class MasterRouting(Base):
    __tablename__ = "master_routings"

    routing_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
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
    operation_code: Mapped[str] = mapped_column(String(64), nullable=True)
    work_center_code: Mapped[str] = mapped_column(String(64), nullable=True)
    quality_gate: Mapped[int] = mapped_column(Integer, nullable=True)
    allow_skip: Mapped[int] = mapped_column(Integer, nullable=True)
    standard_cycle_seconds: Mapped[int] = mapped_column(Integer, nullable=True)


class AssetEquipment(Base):
    __tablename__ = "asset_equipment"

    equipment_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    work_center_code: Mapped[str] = mapped_column(String(64), nullable=True)
    equipment_name: Mapped[str] = mapped_column(String(128), nullable=True)
    criticality: Mapped[str] = mapped_column(String(16), nullable=True)
    current_status: Mapped[str] = mapped_column(String(16), nullable=True)
    last_heartbeat_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MasterTeam(Base):
    __tablename__ = "master_teams"

    team_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    team_name: Mapped[str] = mapped_column(String(128), nullable=True)


class MasterWorker(Base):
    __tablename__ = "master_workers"

    worker_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    team_code: Mapped[str] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=True)


class MasterWorkerQualification(Base):
    __tablename__ = "master_worker_qualifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(32), index=True)
    qualification: Mapped[str] = mapped_column(String(64), index=True)
    granted_by: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionWorkOrder(Base):
    __tablename__ = "production_work_orders"

    work_order_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    material_code: Mapped[str] = mapped_column(String(64), nullable=True)
    routing_code: Mapped[str] = mapped_column(String(64), nullable=True)
    routing_version: Mapped[str] = mapped_column(String(16), nullable=True)
    bom_code: Mapped[str] = mapped_column(String(64), nullable=True)
    bom_version: Mapped[str] = mapped_column(String(16), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    planned_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    completed_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    rejected_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=True)
    planned_start_at: Mapped[str] = mapped_column(String(32), nullable=True)
    planned_end_at: Mapped[str] = mapped_column(String(32), nullable=True)
    actual_start_at: Mapped[str] = mapped_column(String(32), nullable=True)
    actual_end_at: Mapped[str] = mapped_column(String(32), nullable=True)
    cancelled_at: Mapped[str] = mapped_column(String(32), nullable=True)
    cancelled_by: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionWorkOrderOperation(Base):
    __tablename__ = "production_work_order_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_number: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer, index=True)
    operation_code: Mapped[str] = mapped_column(String(64), nullable=True)
    work_center_code: Mapped[str] = mapped_column(String(64), nullable=True)
    sop_id: Mapped[str] = mapped_column(String(64), nullable=True)
    quality_gate: Mapped[int] = mapped_column(Integer, nullable=True)
    allow_skip: Mapped[int] = mapped_column(Integer, nullable=True)
    standard_cycle_seconds: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)


class MasterParameterSpecification(Base):
    __tablename__ = "master_parameter_specifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    operation_code: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64))
    unit_code: Mapped[str] = mapped_column(String(16), nullable=True)
    lower_limit: Mapped[float] = mapped_column(Float, nullable=True)
    target_value: Mapped[float] = mapped_column(Float, nullable=True)
    upper_limit: Mapped[float] = mapped_column(Float, nullable=True)
    required: Mapped[int] = mapped_column(Integer, nullable=True)


class MasterSopDocument(Base):
    __tablename__ = "master_sop_documents"

    sop_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_uri: Mapped[str] = mapped_column(String(256), nullable=True)
    version: Mapped[str] = mapped_column(String(16), nullable=True)


class MasterBomComponent(Base):
    __tablename__ = "master_bom_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bom_code: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16), index=True)
    material_code: Mapped[str] = mapped_column(String(64))
    quantity_per: Mapped[float] = mapped_column(Float, nullable=True)
    unit_code: Mapped[str] = mapped_column(String(16), nullable=True)


class ProductionWorkOrderLineage(Base):
    __tablename__ = "production_work_order_lineage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_work_order_number: Mapped[str] = mapped_column(String(64), index=True)
    target_work_order_number: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String(32))


class MasterStation(Base):
    __tablename__ = "master_stations"

    station_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    station_name: Mapped[str] = mapped_column(String(128), nullable=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    work_center_code: Mapped[str] = mapped_column(String(64), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=True)


class StationSession(Base):
    __tablename__ = "production_station_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    station_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("master_stations.station_code")
    )
    user_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    shift_code: Mapped[str] = mapped_column(String(32), nullable=True)
    started_at: Mapped[str] = mapped_column(String(32))
    logged_out_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionOperationTask(Base):
    __tablename__ = "production_operation_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_number: Mapped[str] = mapped_column(
        String(64), ForeignKey("production_work_orders.work_order_number"), index=True
    )
    operation_sequence: Mapped[int] = mapped_column(Integer, index=True)
    station_code: Mapped[str] = mapped_column(String(64), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionDispatchAssignment(Base):
    __tablename__ = "production_dispatch_assignments"

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_number: Mapped[str] = mapped_column(
        String(64), ForeignKey("production_work_orders.work_order_number")
    )
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    station_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("master_stations.station_code")
    )
    worker_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("master_workers.worker_id")
    )
    shift_code: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=True)
    scheduled_start_at: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionExecutionEvent(Base):
    __tablename__ = "production_execution_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    operation_code: Mapped[str] = mapped_column(String(64), nullable=True)
    station_code: Mapped[str] = mapped_column(String(64), nullable=True)
    equipment_code: Mapped[str] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[str] = mapped_column(String(32))
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=True)
    defect_code: Mapped[str] = mapped_column(String(64), nullable=True)


class ProductionParameterRecord(Base):
    __tablename__ = "production_parameter_records"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    code: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float, nullable=True)
    lower_limit: Mapped[float] = mapped_column(Float, nullable=True)
    upper_limit: Mapped[float] = mapped_column(Float, nullable=True)
    in_spec: Mapped[int] = mapped_column(Integer, nullable=True)
    operator_id: Mapped[str] = mapped_column(String(32), nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(32))


class ProductionMaterialConsumption(Base):
    __tablename__ = "production_material_consumptions"

    consumption_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    lot_number: Mapped[str] = mapped_column(String(64), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=True)
    unit_code: Mapped[str] = mapped_column(String(16), nullable=True)
    consumed_by: Mapped[str] = mapped_column(String(32), nullable=True)
    consumed_at: Mapped[str] = mapped_column(String(32))


class QualityInspectionResult(Base):
    __tablename__ = "quality_inspection_results"

    inspection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    plan_code: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    disposition: Mapped[str] = mapped_column(String(16), nullable=True)
    inspector_id: Mapped[str] = mapped_column(String(32), nullable=True)
    inspected_at: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text, nullable=True)


class ProductionLaborRecord(Base):
    __tablename__ = "production_labor_records"

    labor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[str] = mapped_column(String(32), nullable=True)
    station_code: Mapped[str] = mapped_column(String(64), nullable=True)
    start_at: Mapped[str] = mapped_column(String(32), nullable=True)
    end_at: Mapped[str] = mapped_column(String(32), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)


class TraceEvent(Base):
    __tablename__ = "trace_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=True)
    resource_code: Mapped[str] = mapped_column(String(64), nullable=True)
    related_work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    related_serial_number: Mapped[str] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(String(256), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[str] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionExecutionParameter(Base):
    __tablename__ = "production_execution_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    code: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float, nullable=True)
    lower_limit: Mapped[float] = mapped_column(Float, nullable=True)
    upper_limit: Mapped[float] = mapped_column(Float, nullable=True)
    in_spec: Mapped[int] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(32))


class ProductionProductUnit(Base):
    __tablename__ = "production_product_units"

    serial_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    material_code: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    current_operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    current_station_code: Mapped[str] = mapped_column(String(64), nullable=True)
    started_at: Mapped[str] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ProductionReworkOrder(Base):
    __tablename__ = "production_rework_orders"

    rework_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    target_operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class ProductionScrapRecord(Base):
    __tablename__ = "production_scrap_records"

    scrap_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), index=True)
    scrap_code: Mapped[str] = mapped_column(String(64), nullable=True)
    disposition: Mapped[str] = mapped_column(String(16), nullable=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class QualityInspectionLot(Base):
    __tablename__ = "quality_inspection_lots"

    lot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    plan_code: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class QualityInspection(Base):
    __tablename__ = "quality_inspections"

    inspection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=True)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=True)
    plan_code: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    disposition: Mapped[str] = mapped_column(String(16), nullable=True)
    defect_code: Mapped[str] = mapped_column(String(64), nullable=True)
    inspector_id: Mapped[str] = mapped_column(String(32), nullable=True)
    inspected_at: Mapped[str] = mapped_column(String(32))


class QualityNonconformance(Base):
    __tablename__ = "quality_nonconformances"

    nonconformance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    defect_code: Mapped[str] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    reported_by: Mapped[str] = mapped_column(String(32), nullable=True)
    reported_at: Mapped[str] = mapped_column(String(32))


class QualityCapa(Base):
    __tablename__ = "quality_capas"

    capa_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonconformance_id: Mapped[str] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=True)
    due_at: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))


class QualitySpcMeasurement(Base):
    __tablename__ = "quality_spc_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    characteristic_code: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float, nullable=True)
    measured_at: Mapped[str] = mapped_column(String(32))


class AssetEquipmentEvent(Base):
    __tablename__ = "asset_equipment_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(64), unique=True)


class AssetEquipmentCounter(Base):
    __tablename__ = "asset_equipment_counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    counter_code: Mapped[str] = mapped_column(String(64), nullable=True)
    value: Mapped[float] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(32))


class AssetMaintenanceWorkOrder(Base):
    __tablename__ = "asset_maintenance_work_orders"

    maintenance_work_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    equipment_code: Mapped[str] = mapped_column(String(64), nullable=True)
    maintenance_type: Mapped[str] = mapped_column(String(16), nullable=True)
    description: Mapped[str] = mapped_column(String(256), nullable=True)
    assignee_id: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    due_at: Mapped[str] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=True)


class TraceAndonEvent(Base):
    __tablename__ = "trace_andon_events"

    andon_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=True)
    resource_code: Mapped[str] = mapped_column(String(64), nullable=True)
    related_work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    related_serial_number: Mapped[str] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(String(256), nullable=True)
    raised_at: Mapped[str] = mapped_column(String(32))
    acknowledged_at: Mapped[str] = mapped_column(String(32), nullable=True)
    acknowledged_by: Mapped[str] = mapped_column(String(32), nullable=True)
    closed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    closed_by: Mapped[str] = mapped_column(String(32), nullable=True)


class MaterialInventoryBalance(Base):
    __tablename__ = "material_inventory_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    location_code: Mapped[str] = mapped_column(String(32), nullable=True)
    material_code: Mapped[str] = mapped_column(String(64), index=True)
    lot_number: Mapped[str] = mapped_column(String(64), nullable=True)
    batch_number: Mapped[str] = mapped_column(String(64), nullable=True)
    on_hand_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    reserved_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    expiry_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MaterialInventoryReservation(Base):
    __tablename__ = "material_inventory_reservations"

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    lot_number: Mapped[str] = mapped_column(String(64), nullable=True)
    work_order_number: Mapped[str] = mapped_column(String(64), nullable=True)
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MaterialTransaction(Base):
    __tablename__ = "material_material_transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_code: Mapped[str] = mapped_column(String(64))
    lot_number: Mapped[str] = mapped_column(String(64), nullable=True)
    transaction_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float, nullable=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    location_code: Mapped[str] = mapped_column(String(32), nullable=True)
    reference_type: Mapped[str] = mapped_column(String(16), nullable=True)
    reference_id: Mapped[str] = mapped_column(String(64), nullable=True)
    transaction_at: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(32), nullable=True)


class IntegrationInboxMessage(Base):
    __tablename__ = "integration_inbox_messages"

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(64))
    message_type: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=True)
    received_at: Mapped[str] = mapped_column(String(32))
    processed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str] = mapped_column(String(256), nullable=True)


class IntegrationOutboxMessage(Base):
    __tablename__ = "integration_outbox_messages"

    outbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=True)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=True)
    target_system: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=True)
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
    retry_count: Mapped[int] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str] = mapped_column(String(256), nullable=True)
    failed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    replayed_at: Mapped[str] = mapped_column(String(32), nullable=True)


class ReportingEquipmentOeeHourly(Base):
    __tablename__ = "reporting_equipment_oee_hourly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    hour_bucket: Mapped[str] = mapped_column(String(32), index=True)
    actual_output: Mapped[float] = mapped_column(Float, nullable=True)
    good_output: Mapped[float] = mapped_column(Float, nullable=True)
    running_seconds: Mapped[float] = mapped_column(Float, nullable=True)
    planned_seconds: Mapped[float] = mapped_column(Float, nullable=True)
    ideal_cycle_seconds: Mapped[float] = mapped_column(Float, nullable=True)


class ReportingAndonSummary(Base):
    __tablename__ = "reporting_andon_summary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_code: Mapped[str] = mapped_column(String(32), index=True)
    summary_date: Mapped[str] = mapped_column(String(16), index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=True)
    raised_count: Mapped[int] = mapped_column(Integer, nullable=True)
    closed_count: Mapped[int] = mapped_column(Integer, nullable=True)


class ReportingShiftProduction(Base):
    __tablename__ = "reporting_production_shift_summary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_code: Mapped[str] = mapped_column(String(32), index=True)
    shift_date: Mapped[str] = mapped_column(String(16), index=True)
    shift_code: Mapped[str] = mapped_column(String(16), nullable=True)
    planned_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    completed_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    good_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    rejected_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    last_event_at: Mapped[str] = mapped_column(String(32), nullable=True)


class MasterRecipe(Base):
    __tablename__ = "master_recipes"

    recipe_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    recipe_name: Mapped[str] = mapped_column(String(128), nullable=True)
    product_material_code: Mapped[str] = mapped_column(String(64))
    target_batch_size: Mapped[float] = mapped_column(Float)
    unit_code: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=True)
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
    required: Mapped[int] = mapped_column(Integer, nullable=True)
    hazardous: Mapped[int] = mapped_column(Integer, nullable=True)


class MasterRecipeParameter(Base):
    __tablename__ = "master_recipe_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipe_code: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16), index=True)
    step_sequence: Mapped[int] = mapped_column(Integer, index=True)
    parameter_code: Mapped[str] = mapped_column(String(64))
    parameter_name: Mapped[str] = mapped_column(String(128), nullable=True)
    target_value: Mapped[float] = mapped_column(Float, nullable=True)
    lower_limit: Mapped[float] = mapped_column(Float, nullable=True)
    upper_limit: Mapped[float] = mapped_column(Float, nullable=True)
    unit_code: Mapped[str] = mapped_column(String(16))
    required: Mapped[int] = mapped_column(Integer, nullable=True)


class ProductionBatch(Base):
    __tablename__ = "production_batches"

    batch_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(32), nullable=True)
    product_material_code: Mapped[str] = mapped_column(String(64), nullable=True)
    recipe_code: Mapped[str] = mapped_column(String(64), nullable=True)
    recipe_version: Mapped[str] = mapped_column(String(16), nullable=True)
    equipment_code: Mapped[str] = mapped_column(String(64), nullable=True)
    planned_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    actual_quantity: Mapped[float] = mapped_column(Float, nullable=True)
    unit_code: Mapped[str] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=True)
    quality_disposition: Mapped[str] = mapped_column(String(16), nullable=True)
    hold_reason: Mapped[str] = mapped_column(String(256), nullable=True)
    started_at: Mapped[str] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=True)


class ProductionBatchCharge(Base):
    __tablename__ = "production_batch_charges"

    charge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(64), index=True)
    component_sequence: Mapped[int] = mapped_column(Integer, index=True)
    material_code: Mapped[str] = mapped_column(String(64))
    lot_number: Mapped[str] = mapped_column(String(64), nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    unit_code: Mapped[str] = mapped_column(String(16))
    charged_by: Mapped[str] = mapped_column(String(32), nullable=True)
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
    unit_code: Mapped[str] = mapped_column(String(16), nullable=True)
    in_spec: Mapped[int] = mapped_column(Integer, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(32), nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(64), nullable=True)


class QualityBatchDisposition(Base):
    __tablename__ = "quality_batch_dispositions"

    disposition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(64), index=True)
    disposition: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=True)
    deviation_reference: Mapped[str] = mapped_column(String(128), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(32), nullable=True)
    electronic_signature: Mapped[str] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[str] = mapped_column(String(32))
