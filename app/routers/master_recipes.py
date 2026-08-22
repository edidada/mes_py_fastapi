"""``POST /api/v1/master/recipes`` 创建配方版本。

权限 ``MES_ADMIN`` 或 ``PROCESS_ENGINEER``，先于幂等。主字段校验后依次校验
组分/参数，事务内写配方、所有组分、所有参数；任一业务插入失败回滚 ``409``。
EFFECTIVE 配方写 ``approved_by``/``approved_at``。组分布尔字段按 C++ helper
接受非零数字及 ``true``/``TRUE``。
"""

import json

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.audit import write_audit
from app.auth import get_actor, require_role
from app.contract import correlation_id, ok, utc_now
from app.deps import DbSession
from app.errors import MESError
from app.idempotency import find_idempotent, require_idempotency_key, save_idempotent

router = APIRouter()

_ALLOWED_ROLES = {"MES_ADMIN", "PROCESS_ENGINEER"}


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _as_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_bool(value, default: bool = True) -> int:
    """C++ helper：接受 bool、非零数字、'true'/'TRUE'。"""
    if value is None:
        return 1 if default else 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value != 0 else 0
    if isinstance(value, str):
        return 1 if value.lower() == "true" else 0
    return 1 if default else 0


async def _parse_body(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        raise MESError(422, "VALIDATION_ERROR", "invalid json") from None
    return data if isinstance(data, dict) else {}


@router.post("", status_code=200)
async def create_recipe(request: Request, db: DbSession) -> dict:
    corr = correlation_id(request.headers.get("X-Correlation-Id"))
    require_role(request, _ALLOWED_ROLES, "PROCESS_ENGINEER or MES_ADMIN required")
    idem_key = await require_idempotency_key(request)
    try:
        cached = await find_idempotent(db, idem_key)
    except Exception:
        raise MESError(500, "DATABASE_ERROR", "database error") from None
    if cached is not None:
        return ok({"recipeId": cached["resource_code"], "cached": True}, corr)

    body = await _parse_body(request)
    operator = get_actor(request)

    # 主字段校验
    recipe_code = _as_str(body.get("recipeCode"))
    version = _as_str(body.get("version"))
    recipe_name = _as_str(body.get("recipeName"))
    product_material_code = _as_str(body.get("productMaterialCode"))
    target_batch_size = _as_number(body.get("targetBatchSize"))
    unit_code = _as_str(body.get("unitCode"))
    status = _as_str(body.get("status")) or "DRAFT"
    if status not in ("DRAFT", "EFFECTIVE"):
        raise MESError(422, "VALIDATION_ERROR", "invalid status")
    components = body.get("components")
    parameters = body.get("parameters")

    if not recipe_code or not version or not product_material_code:
        raise MESError(422, "VALIDATION_ERROR", "recipeCode, version, productMaterialCode required")
    if target_batch_size is None or target_batch_size <= 0:
        raise MESError(422, "VALIDATION_ERROR", "targetBatchSize must be positive number")
    if not unit_code:
        raise MESError(422, "VALIDATION_ERROR", "unitCode required")
    if not isinstance(components, list) or len(components) == 0:
        raise MESError(422, "VALIDATION_ERROR", "components must be non-empty array")
    if parameters is not None and not isinstance(parameters, list):
        raise MESError(422, "VALIDATION_ERROR", "parameters must be array")

    # 组分校验
    validated_components = []
    for comp in components:
        if not isinstance(comp, dict):
            raise MESError(422, "VALIDATION_ERROR", "invalid component")
        sequence = _as_int(comp.get("sequence"))
        material_code = _as_str(comp.get("materialCode"))
        phase_code = _as_str(comp.get("phaseCode"))
        target_quantity = _as_number(comp.get("targetQuantity"))
        lower_limit = _as_number(comp.get("lowerLimit"))
        upper_limit = _as_number(comp.get("upperLimit"))
        comp_unit = _as_str(comp.get("unitCode")) or unit_code
        required = _as_bool(comp.get("required"), True)
        hazardous = _as_bool(comp.get("hazardous"), False)
        if sequence is None or sequence <= 0:
            raise MESError(422, "VALIDATION_ERROR", "component sequence must be positive integer")
        if not material_code or not phase_code:
            raise MESError(422, "VALIDATION_ERROR", "component materialCode/phaseCode required")
        if target_quantity is None or target_quantity <= 0:
            raise MESError(422, "VALIDATION_ERROR", "component targetQuantity must be positive")
        if lower_limit is None:
            lower_limit = target_quantity
        if upper_limit is None:
            upper_limit = target_quantity
        if not (0 <= lower_limit <= target_quantity <= upper_limit):
            raise MESError(422, "VALIDATION_ERROR", "component limits out of range")
        validated_components.append(
            (sequence, material_code, phase_code, target_quantity, lower_limit, upper_limit, comp_unit, required, hazardous)
        )

    # 参数校验
    validated_parameters = []
    if parameters is not None:
        for param in parameters:
            if not isinstance(param, dict):
                raise MESError(422, "VALIDATION_ERROR", "invalid parameter")
            step_sequence = _as_int(param.get("stepSequence"))
            parameter_code = _as_str(param.get("parameterCode"))
            parameter_name = _as_str(param.get("parameterName"))
            target_value = _as_number(param.get("targetValue"))
            lower_limit = _as_number(param.get("lowerLimit"))
            upper_limit = _as_number(param.get("upperLimit"))
            param_unit = _as_str(param.get("unitCode"))
            required = _as_bool(param.get("required"), True)
            if step_sequence is None or step_sequence <= 0:
                raise MESError(422, "VALIDATION_ERROR", "parameter stepSequence must be positive integer")
            if not parameter_code or not param_unit:
                raise MESError(422, "VALIDATION_ERROR", "parameter parameterCode/unitCode required")
            if target_value is None:
                target_value = 0.0
            if lower_limit is None:
                lower_limit = 0.0
            if upper_limit is None:
                upper_limit = 0.0
            if not (lower_limit <= target_value <= upper_limit):
                raise MESError(422, "VALIDATION_ERROR", "parameter range invalid")
            validated_parameters.append(
                (step_sequence, parameter_code, parameter_name, target_value, lower_limit, upper_limit, param_unit, required)
            )

    effective_from = _as_str(body.get("effectiveFrom")) or utc_now()
    effective_to = _as_str(body.get("effectiveTo")) or None
    approved_by = None
    approved_at = None
    if status == "EFFECTIVE":
        approved_by = _as_str(body.get("approvedBy")) or operator
        approved_at = utc_now()

    try:
        # 写配方
        await db.execute(
            text(
                "INSERT INTO master_recipes "
                "(recipe_code, version, recipe_name, product_material_code, target_batch_size, "
                " unit_code, status, effective_from, effective_to, approved_by, approved_at) "
                "VALUES (:rc, :v, :rn, :pmc, :tbs, :uc, :s, :ef, :et, :ab, :aa)"
            ),
            {
                "rc": recipe_code,
                "v": version,
                "rn": recipe_name,
                "pmc": product_material_code,
                "tbs": target_batch_size,
                "uc": unit_code,
                "s": status,
                "ef": effective_from,
                "et": effective_to,
                "ab": approved_by,
                "aa": approved_at,
            },
        )
        # 写组分
        for c in validated_components:
            await db.execute(
                text(
                    "INSERT INTO master_recipe_components "
                    "(recipe_code, version, component_sequence, material_code, phase_code, "
                    " target_quantity, lower_limit, upper_limit, unit_code, required, hazardous) "
                    "VALUES (:rc, :v, :seq, :mc, :pc, :tq, :ll, :ul, :uc, :req, :haz)"
                ),
                {
                    "rc": recipe_code,
                    "v": version,
                    "seq": c[0],
                    "mc": c[1],
                    "pc": c[2],
                    "tq": c[3],
                    "ll": c[4],
                    "ul": c[5],
                    "uc": c[6],
                    "req": c[7],
                    "haz": c[8],
                },
            )
        # 写参数
        for p in validated_parameters:
            await db.execute(
                text(
                    "INSERT INTO master_recipe_parameters "
                    "(recipe_code, version, step_sequence, parameter_code, parameter_name, "
                    " target_value, lower_limit, upper_limit, unit_code, required) "
                    "VALUES (:rc, :v, :seq, :pc, :pn, :tv, :ll, :ul, :uc, :req)"
                ),
                {
                    "rc": recipe_code,
                    "v": version,
                    "seq": p[0],
                    "pc": p[1],
                    "pn": p[2],
                    "tv": p[3],
                    "ll": p[4],
                    "ul": p[5],
                    "uc": p[6],
                    "req": p[7],
                },
            )
        # 审计 + 幂等
        try:
            await write_audit(
                db,
                action="RECIPE_CREATE",
                resource_type="RECIPE",
                resource_id=f"{recipe_code}:{version}",
                before={},
                after=body,
                actor_id=operator,
            )
        except Exception:
            pass
        try:
            await save_idempotent(db, idem_key, "200", f"{recipe_code}:{version}", "recipe created")
        except Exception:
            pass
        await db.commit()
    except MESError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise MESError(409, "CONFLICT", "recipe creation failed") from None

    return ok(
        {
            "recipeCode": recipe_code,
            "version": version,
            "status": status,
            "componentCount": len(validated_components),
            "parameterCount": len(validated_parameters),
        },
        corr,
    )
