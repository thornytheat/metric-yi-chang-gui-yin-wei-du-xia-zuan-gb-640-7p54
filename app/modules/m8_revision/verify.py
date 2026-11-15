"""整版核对：切换前必须全部通过，任一不平即判定切换失败、沿用旧版。

四类核对：
  行账：读入记录 = 采信 + 被覆盖 + 隔离
  点账：网格点 = 有效 + 隔离 + 缺失
  金额账：采信行净收入合计 = 有效点净收入合计（Decimal，2 位）
  分解账：净收入差 = 三因子之和 + 残差（归因 identity_ok，份额守恒）

conn 传入时读取事务内尚未提交的候选版本数据，保证核对的就是将要切换的整版结果。
"""
from __future__ import annotations

from decimal import Decimal

from app import config
from app.modules.m3_quality import service as quality_service
from app.modules.m5_attrib import service as attrib_service


def _load_points(store, run_id: str, conn) -> list[dict]:
    if conn is not None:
        return store._load_points(conn, run_id)
    return attrib_service.load_view(store, run_id)


def verify_run(settings: config.Settings, store, run_id: str,
               accounts: dict, conn=None) -> dict:
    checks: list[dict] = []

    # 行账、点账复用 M3 的同一份等式
    base_checks = quality_service.account_checks(accounts)
    checks.extend(base_checks)

    # 金额账：采信行合计与有效点合计必须分毫不差
    points = _load_points(store, run_id, conn)
    points_view = attrib_service._points_view(
        store._load_points(conn, run_id)) if conn is not None else points
    points_revenue = sum((point["revenue_net"] for point in points_view),
                         Decimal("0")).quantize(settings.money_quant)
    accepted_revenue = Decimal(accounts["revenue_accepted"]).quantize(settings.money_quant)
    revenue_ok = points_revenue == accepted_revenue
    checks.append({
        "name": "金额账",
        "formula": "采信行净收入合计 = 有效点净收入合计",
        "left": str(accepted_revenue),
        "right": str(points_revenue),
        "ok": revenue_ok,
    })

    # 分解账：默认 7/7 窗口的总分解与逐行分解都守恒，份额合计 1.0000
    decomposition_ok, total = _decomposition(settings, store, run_id, conn)
    checks.append({
        "name": "分解账",
        "formula": "净收入差 = 可售房晚差 + 出租率差 + 均价差 + 残差",
        "left": total.get("delta", "-"),
        "right": _factor_right(total),
        "ok": decomposition_ok,
    })

    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "checks": checks,
        "decomposition_identity_ok": decomposition_ok,
        "revenue_identity_ok": revenue_ok,
    }


def _decomposition(settings, store, run_id: str, conn) -> tuple[bool, dict]:
    if conn is None:
        attribution = attrib_service.attribution(settings, store, run_id,
                                                 "hotel_code", 7, 7)
    else:
        attribution = _attribution_in_tx(settings, store, run_id, conn)
    if not attribution:
        return False, {}
    ok = attribution.get("identity_ok") is True
    if not attribution.get("zero_total"):
        ok = ok and attribution.get("share_sum") == "1.0000"
    return ok, attribution.get("total", {})


def _attribution_in_tx(settings, store, run_id: str, conn) -> dict:
    """事务内归因：直接读连接里的候选点，绕开会另开连接的 load_view。"""
    from app.modules.m5_attrib import batches, dimension, lmdi as lmdi_mod

    points = attrib_service._points_view(store._load_points(conn, run_id))
    watermark = batches.watermark_of(points)
    if not watermark:
        return {"run_id": run_id, "empty": True}
    window = attrib_service.windows(watermark, 7, 7)
    rows = dimension.group_rows(points, window["base"], window["cur"],
                                "hotel_code", settings)
    share_info = dimension.shares(rows, settings)
    base_all = dimension.aggregate(points, window["base"][0], window["base"][1], "hotel_code")
    cur_all = dimension.aggregate(points, window["cur"][0], window["cur"][1], "hotel_code")
    base_factors = attrib_service._total_factors(base_all)
    cur_factors = attrib_service._total_factors(cur_all)
    total_part = lmdi_mod.decompose(base_factors["rev"], cur_factors["rev"],
                                    base_factors, cur_factors, settings.money_quant)
    parts = [lmdi_mod.decompose(row["decomposition"]["rev_base"],
                                row["decomposition"]["rev_cur"],
                                attrib_service._row_factors(row, "base"),
                                attrib_service._row_factors(row, "cur"))
             for row in rows]
    return {
        "run_id": run_id,
        "watermark": watermark,
        "total": total_part.as_dict(settings.money_quant),
        "rows": share_info["rows"],
        "other": share_info["other"],
        "share_sum": share_info["share_sum"],
        "zero_total": share_info["zero_total"],
        "identity_ok": lmdi_mod.identity_holds(parts + [total_part], settings.money_quant),
    }


def _factor_right(total: dict) -> str:
    if not total:
        return "-"
    factor_sum = sum((Decimal(item.get("value", "0")) for item in total.get("factors", [])),
                     Decimal("0"))
    residual = Decimal(total.get("residual", "0"))
    return str((factor_sum + residual).quantize(Decimal("0.01")))
