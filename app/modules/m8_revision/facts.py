"""修订事实合并：把修订文件与父版本快照事实按业务键合并成一份新事实集。

只做「事实层」的合并，不做判定：归一、筛选、重复归因、迟到标记、网格与三账
仍由 M1→M3 原链路在合并后的整版事实上重跑，保证 28 天基线与下钻份额整版重算，
不会出现只重算一半。

合并规则（业务键 = 门店 × 渠道 × 业务日）：
- 常规补报：同键多快照交给 M3 的 dupes，按 (快照瞬时, 行号) 取最晚，冲突整键隔离。
- 撤回（op_flag = WITHDRAW 等）：该键父版本事实被移除，整键不再采信，记 W_WITHDRAWN。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app import config, reasons
from app.modules.m2_calendar import calendar_rules, tz
from app.modules.m2_calendar.service import CoercedRow


def point_to_row(point: dict) -> CoercedRow:
    """把父版本已采信点还原成事实行，使合并后能整体重跑 M2/M3。"""
    snapshot_utc = dt.datetime.strptime(point["updated_utc"],
                                        "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    biz_date = dt.date.fromisoformat(point["biz_date"])
    snapshot_local = tz.local_date(snapshot_utc)
    return CoercedRow(
        hotel_code=point["hotel_code"],
        hotel_name=point["hotel_name"],
        channel=point["channel"],
        biz_date=biz_date,
        stratum=point["stratum"] or calendar_rules.day_stratum(biz_date),
        rooms_available=int(point["rooms_available"]),
        rooms_sold=int(point["rooms_sold"]),
        adr=Decimal(str(point["adr"])),
        revenue_net=Decimal(str(point["revenue_net"])),
        snapshot_utc=snapshot_utc,
        snapshot_local=snapshot_local,
        late_days=int(point["late_days"]),
        line_no=int(point["source_line"]),
        late_flag=bool(point["late_flag"]),
        out_of_window=bool(point["out_of_window"]),
    )


def is_withdraw(raw_row: dict) -> bool:
    flag = (raw_row.get("op_flag") or "").strip().lower()
    return flag in config.WITHDRAW_OPS


def withdraw_entry(raw_row: dict, line_no: int) -> dict | None:
    hotel = (raw_row.get("hotel_code") or "").strip()
    channel = (raw_row.get("channel") or "").strip().lower()
    day_text = (raw_row.get("biz_date") or "").strip()
    if not hotel or not channel or not day_text:
        return None
    return {
        "line_no": line_no,
        "code": "W_WITHDRAWN",
        "label": reasons.label("W_WITHDRAWN"),
        "field": "op_flag",
        "raw": f"{hotel}|{channel}|{day_text}",
        "detail": "门店渠道撤回该业务日事实，整键不再采信",
        "hotel_code": hotel,
        "channel": channel,
        "biz_date": day_text,
    }


def parent_fact_map(points: list[dict], line_offset: int = 100000) -> dict[tuple, CoercedRow]:
    """父版本每个业务键对应一条采信事实（父版本键已唯一）。

    承接行的行号加固定偏移并标负号前缀语义，避免与修订文件物理行号撞号；
    隔离 CSV 里仍能看出它来自父版本承接。键统一为字符串三元组，与修订行对齐。
    """
    out: dict[tuple, CoercedRow] = {}
    for point in points:
        row = point_to_row(point)
        row.line_no = line_offset + int(point["source_line"])
        out[(row.hotel_code, row.channel, row.biz_date.isoformat())] = row
    return out


def merge_facts(parent_points: list[dict], revision_rows: list[dict],
                settings: config.Settings) -> dict:
    """返回合并后的事实行、撤回条目与本次修订的键级影响面。

    revision_rows 是 M1 采信后的原始行（含 _line_no 与可选 op_flag）；
    数值归一并入 M2，撤回行不参与归一。
    """
    parent_map = parent_fact_map(parent_points)
    parent_before = {key: _fact_signature(row) for key, row in parent_map.items()}

    withdrawn_keys: set[tuple] = set()
    withdraw_entries: list[dict] = []
    normal_rows: list[dict] = []
    revision_keys: set[tuple] = set()

    for raw in revision_rows:
        line_no = int(raw.get("_line_no", 0))
        if is_withdraw(raw):
            entry = withdraw_entry(raw, line_no)
            if entry is None:
                continue
            key = (entry["hotel_code"], entry["channel"], entry["biz_date"])
            withdrawn_keys.add(key)
            revision_keys.add(key)
            withdraw_entries.append(entry)
            continue
        normal_rows.append(raw)
        hotel = (raw.get("hotel_code") or "").strip()
        channel = (raw.get("channel") or "").strip().lower()
        day_text = (raw.get("biz_date") or "").strip()
        if hotel and channel and day_text:
            revision_keys.add((hotel, channel, day_text))

    # 撤回优先：被撤回键的父事实直接剔除
    for key in withdrawn_keys:
        parent_map.pop(key, None)

    # 父事实（去掉撤回键）作为基础行，修订常规行追加后整体重跑 M2/M3
    merged_rows: list[CoercedRow] = list(parent_map.values())
    return {
        "parent_map": parent_map,
        "parent_before": parent_before,
        "merged_seed_rows": merged_rows,
        "normal_rows": normal_rows,
        "withdraw_entries": withdraw_entries,
        "withdrawn_keys": withdrawn_keys,
        "revision_keys": revision_keys,
    }


def _fact_signature(row: CoercedRow) -> dict:
    return {
        "rooms_available": row.rooms_available,
        "rooms_sold": row.rooms_sold,
        "adr": str(row.adr),
        "revenue_net": str(row.revenue_net),
        "snapshot_utc": row.snapshot_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def classify_impact(parent_before: dict[tuple, dict], final_points: list[dict],
                    withdrawn_keys: set[tuple], revision_keys: set[tuple]) -> dict:
    """按业务键比对父版本事实与新版本采信点，给出受影响范围与变更分类。"""
    after_map: dict[tuple, dict] = {}
    for point in final_points:
        key = (point["hotel_code"], point["channel"], point["biz_date"])
        after_map[key] = {
            "rooms_available": int(point["rooms_available"]),
            "rooms_sold": int(point["rooms_sold"]),
            "adr": str(point["adr"]),
            "revenue_net": str(point["revenue_net"]),
            "snapshot_utc": point["updated_utc"],
        }

    added, changed, unchanged, withdrawn = [], [], [], []
    for key in sorted(revision_keys | set(parent_before) | set(after_map)):
        before = parent_before.get(key)
        after = after_map.get(key)
        label = {"hotel_code": key[0], "channel": key[1], "biz_date": key[2]}
        if key in withdrawn_keys and after is None:
            withdrawn.append(label)
        elif before is None and after is not None:
            added.append(label)
        elif before is not None and after is None:
            # 修订把该键变成冲突/隔离而消失，也算受影响（按撤回之外的移除处理）
            withdrawn.append(label)
        elif before is not None and after is not None:
            if before == after:
                unchanged.append(label)
            else:
                changed.append({**label, "before": before, "after": after})

    affected_keys = sorted({(d["hotel_code"], d["channel"], d["biz_date"])
                            for d in added + changed + withdrawn})
    affected_dates = sorted({key[2] for key in affected_keys})
    affected_combos = sorted({f"{key[0]}|{key[1]}" for key in affected_keys})
    return {
        "keys_total_touched": len(revision_keys),
        "keys_added": added,
        "keys_changed": changed,
        "keys_withdrawn": withdrawn,
        "keys_unchanged": unchanged,
        "affected_keys": [{"hotel_code": k[0], "channel": k[1], "biz_date": k[2]}
                          for k in affected_keys],
        "affected_dates": affected_dates,
        "affected_combos": affected_combos,
        "counts": {
            "added": len(added),
            "changed": len(changed),
            "withdrawn": len(withdrawn),
            "unchanged": len(unchanged),
            "affected": len(affected_keys),
        },
    }
