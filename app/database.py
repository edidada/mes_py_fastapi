"""SQLAlchemy 异步引擎、连接池管理与数据库初始化。"""

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base

# 兼容 C++ 权威实现的只读视图
_VIEWS: list[tuple[str, str]] = [
    (
        "v_available_inventory",
        """
        CREATE VIEW IF NOT EXISTS v_available_inventory AS
        SELECT plant_code, location_code, material_code, lot_number, batch_number,
               on_hand_quantity, reserved_quantity,
               (on_hand_quantity - reserved_quantity) AS available_quantity,
               expiry_at, status
        FROM material_inventory_balances
        WHERE on_hand_quantity > reserved_quantity AND status = 'AVAILABLE'
        """,
    ),
    (
        "v_active_andons",
        """
        CREATE VIEW IF NOT EXISTS v_active_andons AS
        SELECT andon_id, plant_code, severity, category, resource_type, resource_code,
               message, raised_at, acknowledged_at, acknowledged_by,
               related_work_order_number, related_serial_number
        FROM trace_andon_events
        WHERE closed_at IS NULL
        ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                 raised_at ASC
        """,
    ),
    (
        "v_equipment_current_state",
        """
        CREATE VIEW IF NOT EXISTS v_equipment_current_state AS
        SELECT e.equipment_code, e.plant_code, e.current_status AS status,
               e.last_heartbeat_at,
               CASE
                 WHEN e.last_heartbeat_at IS NULL OR e.last_heartbeat_at = ''
                   THEN 0
                 ELSE CAST(strftime('%s','now') - strftime('%s', e.last_heartbeat_at) AS INTEGER)
               END AS heartbeat_age_seconds,
               (SELECT COUNT(*) FROM asset_maintenance_work_orders m
                 WHERE m.equipment_code = e.equipment_code
                   AND m.status NOT IN ('COMPLETED','CANCELLED')) AS open_maintenance_count
        FROM asset_equipment e
        """,
    ),
    (
        "v_wip_by_operation",
        """
        CREATE VIEW IF NOT EXISTS v_wip_by_operation AS
        SELECT u.work_order_number, u.status,
               o.sequence AS operation_sequence, o.operation_code
        FROM production_product_units u
        JOIN production_work_order_operations o
          ON o.work_order_number = u.work_order_number
        """,
    ),
    (
        "v_work_order_progress",
        """
        CREATE VIEW IF NOT EXISTS v_work_order_progress AS
        SELECT w.work_order_number, w.plant_code, w.material_code, w.status, w.priority,
               w.planned_quantity, w.completed_quantity, w.rejected_quantity,
               (SELECT COUNT(*) FROM production_product_units u
                 WHERE u.work_order_number = w.work_order_number
                   AND u.status IN ('CREATED','IN_PROCESS','REWORK','HOLD','WAITING_INSPECTION'))
                 AS wip_quantity
        FROM production_work_orders w
        """,
    ),
    (
        "v_batch_progress",
        """
        CREATE VIEW IF NOT EXISTS v_batch_progress AS
        SELECT b.batch_number, b.plant_code, b.product_material_code, b.recipe_code,
               b.recipe_version, b.equipment_code, b.planned_quantity, b.actual_quantity,
               b.unit_code, b.status, b.quality_disposition, b.hold_reason,
               b.started_at, b.completed_at,
               (SELECT COUNT(DISTINCT c.charge_id) FROM production_batch_charges c
                 WHERE c.batch_number = b.batch_number) AS charge_count,
               (SELECT COUNT(DISTINCT p.record_id) FROM production_batch_parameters p
                 WHERE p.batch_number = b.batch_number) AS parameter_record_count,
               b.updated_at AS last_activity_at
        FROM production_batches b
        """,
    ),
    (
        "v_dashboard_current",
        """
        CREATE VIEW IF NOT EXISTS v_dashboard_current AS
        SELECT plant_code,
               SUM(COALESCE(planned_quantity, 0)) AS planned_quantity,
               SUM(COALESCE(completed_quantity, 0)) AS completed_quantity,
               SUM(COALESCE(good_quantity, 0)) AS good_quantity,
               SUM(COALESCE(rejected_quantity, 0)) AS rejected_quantity,
               MAX(last_event_at) AS last_event_at
        FROM reporting_production_shift_summary
        GROUP BY plant_code
        """,
    ),
]


async def create_db_engine(database_url: str) -> AsyncEngine:
    """创建异步 SQLite 连接池并验证初始连接。

    若无法建立数据库连接则抛出异常，应用启动失败、HTTP 服务不开始监听，
    与 C++ 服务启动时数据库打开失败即退出的行为一致。
    """
    engine = create_async_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):  # noqa: ARG001
        # 与 C++ 权威实现一致，SQLite 连接开启外键约束
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return engine


async def init_db(engine: AsyncEngine, reseed_on_start: bool = True) -> None:
    """创建兼容表、视图与种子数据。

    默认 ``reseed_on_start=true``（项目配置项），装入与 C++ 种子一致的 ``PLANT-A``。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for _, ddl in _VIEWS:
            await conn.execute(text(ddl))
        if reseed_on_start:
            existing = (
                await conn.execute(text("SELECT 1 FROM master_plants WHERE plant_code = 'PLANT-A'"))
            ).first()
            if existing is None:
                await conn.execute(
                    text(
                        "INSERT INTO master_plants (plant_code, plant_name, timezone, active) "
                        "VALUES ('PLANT-A', 'Plant A', 'Asia/Shanghai', 1)"
                    )
                )


async def close_db_engine(engine: AsyncEngine) -> None:
    """关闭连接池。"""
    await engine.dispose()
