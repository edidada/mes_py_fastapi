"""``GET /api/v1/master/recipes/{recipe}/versions/{version}`` 配方版本读取。

不校验角色。主记录不存在或查询失败返回 ``404``。三个区域保留数据库
snake_case 列名（含 nullable 字段保持 JSON null）。组分按
``component_sequence`` 升序，参数按 ``step_sequence``、``parameter_code``
升序。子查询失败降级为空数组。
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.contract import correlation_id, ok
from app.deps import DbSession
from app.errors import MESError

router = APIRouter()


@router.get("/{recipe}/versions/{version}", status_code=200)
async def get_recipe_version(
    request: Request, recipe: str, version: str, db: DbSession
) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))

    try:
        row = (
            await db.execute(
                text(
                    "SELECT recipe_code, version, recipe_name, product_material_code, "
                    "       target_batch_size, unit_code, status, effective_from, "
                    "       effective_to, approved_by, approved_at "
                    "FROM master_recipes WHERE recipe_code = :rc AND version = :v"
                ),
                {"rc": recipe, "v": version},
            )
        ).first()
    except Exception:
        raise MESError(404, "NOT_FOUND", "recipe version not found") from None
    if row is None:
        raise MESError(404, "NOT_FOUND", "recipe version not found")

    recipe_data = {
        "recipe_code": row[0],
        "version": row[1],
        "recipe_name": row[2],
        "product_material_code": row[3],
        "target_batch_size": row[4],
        "unit_code": row[5],
        "status": row[6],
        "effective_from": row[7],
        "effective_to": row[8],
        "approved_by": row[9],
        "approved_at": row[10],
    }

    try:
        comp_rows = (
            await db.execute(
                text(
                    "SELECT component_sequence, material_code, phase_code, target_quantity, "
                    "       lower_limit, upper_limit, unit_code, required, hazardous "
                    "FROM master_recipe_components "
                    "WHERE recipe_code = :rc AND version = :v "
                    "ORDER BY component_sequence ASC"
                ),
                {"rc": recipe, "v": version},
            )
        ).fetchall()
    except Exception:
        comp_rows = []
    components = [
        {
            "component_sequence": c[0],
            "material_code": c[1],
            "phase_code": c[2],
            "target_quantity": c[3],
            "lower_limit": c[4],
            "upper_limit": c[5],
            "unit_code": c[6],
            "required": c[7],
            "hazardous": c[8],
        }
        for c in comp_rows
    ]

    try:
        param_rows = (
            await db.execute(
                text(
                    "SELECT step_sequence, parameter_code, parameter_name, target_value, "
                    "       lower_limit, upper_limit, unit_code, required "
                    "FROM master_recipe_parameters "
                    "WHERE recipe_code = :rc AND version = :v "
                    "ORDER BY step_sequence ASC, parameter_code ASC"
                ),
                {"rc": recipe, "v": version},
            )
        ).fetchall()
    except Exception:
        param_rows = []
    parameters = [
        {
            "step_sequence": p[0],
            "parameter_code": p[1],
            "parameter_name": p[2],
            "target_value": p[3],
            "lower_limit": p[4],
            "upper_limit": p[5],
            "unit_code": p[6],
            "required": p[7],
        }
        for p in param_rows
    ]

    return ok(
        {"recipe": recipe_data, "components": components, "parameters": parameters},
        corr,
    )
