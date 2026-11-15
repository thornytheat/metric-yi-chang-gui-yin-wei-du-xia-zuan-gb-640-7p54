"""修订编排：按业务日期与快照事实找影响面，整版重算，核对通过后一次性切换。

关键约束：
- 新版本是不可变的整版结果（独立 run_id + 完整 points/quarantine），旧版本原样保留。
- 切换在单事务内完成（落 runs/points/quarantine + 修订记录 + 当前版本指针）；
  任一对账不平则整体回滚，当前版本沿用旧版，并把失败修订登记备查。
- 幂等：同一修订文件对同一父版本重试，命中同一 idempotency_key，直接返回原记录，
  不新增版本、不重复落库。
"""
from __future__ import annotations

import datetime as dt

from app import config, manifest
from app.errors import DomainError, NotFound
from app.modules.m1_ingest import service as ingest_service
from app.modules.m2_calendar import service as coerce_service, tz
from app.modules.m3_quality import service as quality_service
from app.modules.m4_detect import service as detect_service
from app.modules.m5_attrib import batches, service as batch_service
from app.modules.m8_revision import facts, verify


class RevisionRejected(DomainError):
    """修订文件本身不可采信（空态/缺列等），沿用 4xx 语义。"""
    status = 400
    code = "E_REVISION_REJECTED"


def _stamp() -> str:
    return tz.local_stamp(dt.datetime.now(dt.timezone.utc))


def idempotency_key(input_sha256: str, parent_run_id: str, mode: str) -> str:
    return manifest.params_digest(
        {"rev": input_sha256, "parent": parent_run_id, "mode": mode})


def revision_run_id(input_sha256: str, parent_run_id: str, rules_version: str,
                    params_key: str) -> str:
    seed = f"rev|{input_sha256}|{parent_run_id}|{rules_version}|{params_key}"
    return "rev-" + manifest.sha256_bytes(seed.encode("utf-8"))[:12]


def revision_record_id(run_id: str) -> str:
    return "rec-" + manifest.sha256_bytes(run_id.encode("utf-8"))[:12]


def apply_revision(settings: config.Settings, store, data: bytes, input_name: str,
                   mode: str = "partial", parent_run_id: str | None = None) -> dict:
    if mode not in ("partial", "full"):
        raise RevisionRejected("修订模式只支持 partial 或 full", code="E_BAD_REVISION_MODE")

    digest = manifest.sha256_bytes(data)
    params_key = manifest.params_digest(settings.params_digest_payload())

    # ---- 父版本与版本链定位 ------------------------------------------
    parent = store.get_run(parent_run_id) if parent_run_id else store.latest_run()
    if parent is None:
        raise NotFound("还没有可修订的基线版本，请先导入一份完整快照", code="E_NO_RUN")
    parent_id = parent["run_id"]

    idem_key = idempotency_key(digest, parent_id, mode)
    # 幂等查找两层：
    # 1) 同一父版本 + 同一修订文件 + 同一模式 → 完全相同的修订请求重试，
    #    无论上次成功还是失败，都回放原记录、不新增版本；
    # 2) 父版本指针已前进到别的版本后，重放同一文件只复用「成功切换」记录，
    #    且该记录不能挂在当前父版本上（否则应走第 1 层或重新修订）；
    #    失败记录不拦截在新父版本上的重新修订。
    existing = _find_revision(store, idem_key)
    if existing is None:
        existing = _find_switched_by_content(store, digest, mode, parent_id)
    if existing is not None:
        # 同一修订重试：不新增版本，直接回放原结论（成功带 reused，失败带原因）
        return _idempotent_payload(existing, store)

    # 根版本：父版本本身是修订（成功或失败）则沿用其 root，否则父版本即根。
    prior_chain = store.lineage_for_run(parent_id)
    if prior_chain:
        root_run_id = prior_chain[0]["root_run_id"]
    else:
        parent_revision = next(
            (item for item in store.list_revisions() if item["run_id"] == parent_id), None)
        root_run_id = parent_revision["root_run_id"] if parent_revision else parent_id
    # 版本号只对成功切换的版本递增，按整条根链的全局最大成功序号计算，
    # 失败修订（可能挂在任意父版本上）不占用链上序号。
    switched_nos = [item["revision_no"] for item in store.list_revisions(root_run_id)
                    if item["switched"]]
    revision_no = (max(switched_nos) + 1) if switched_nos else 1
    run_id = revision_run_id(digest, parent_id, settings.rules_version, params_key)
    revision_id = revision_record_id(run_id)
    created_at = _stamp()

    # ---- 采信修订文件（撤回行与常规行分开） ---------------------------
    ingest = ingest_service.ingest(data, input_name, settings)
    parent_points = store.load_points(parent_id)
    merged = facts.merge_facts(parent_points, ingest.rows, settings)

    if mode == "full":
        # 整版替换：只采信修订文件里的事实，父事实不并入；撤回语义仍生效
        seed_rows: list = []
    else:
        seed_rows = merged["merged_seed_rows"]

    coerced, coerce_rejects = coerce_service.coerce_rows(merged["normal_rows"], settings)
    all_coerced = seed_rows + coerced
    carried = len(seed_rows)
    quality = quality_service.evaluate(
        all_coerced,
        list(ingest.rejects) + coerce_rejects + merged["withdraw_entries"],
        settings,
        records_read=ingest.total_records,
        carried_points=carried,
    )

    impact = facts.classify_impact(
        merged["parent_before"], quality.points,
        merged["withdrawn_keys"], merged["revision_keys"])

    points_view = batch_service._points_view(quality.points)
    detection = detect_service.detect(points_view, settings)

    run_manifest = manifest.RunManifest(
        run_id=run_id,
        input_name=input_name,
        input_sha256=digest,
        input_bytes=len(data),
        rules_version=settings.rules_version,
        params_key=params_key,
        created_at=created_at,
        duration_ms=0,
        counts={
            "rows_read": quality.accounts["rows_read"],
            "rows_accepted": len(quality.points),
            "rows_quarantine": quality.accounts["rows_quarantine"],
            "rows_superseded": quality.accounts["rows_superseded"],
            "points_grid": quality.accounts["points_grid"],
            "points_missing": quality.accounts["points_missing"],
        },
        encoding=ingest.encoding,
        empty_state="",
    )
    summary = batch_service._summary(run_manifest, quality, ingest, detection, settings)
    summary["run_key"] = batches.run_key(digest, settings.rules_version, params_key)
    summary["reused"] = False
    summary["revision"] = {
        "is_revision": True,
        "parent_run_id": parent_id,
        "root_run_id": root_run_id,
        "revision_no": revision_no,
        "mode": mode,
        "impact": impact,
    }

    # ---- 先落库到候选 run，再做整版核对（不切换指针） ------------------
    run_key_value = "rev|" + idem_key
    clock = manifest.RunClock()
    conn = store.transaction()
    closed = False
    try:
        store._save_run(conn, run_manifest.as_dict(), run_key_value, summary)
        store._replace_points(conn, run_id, quality.points)
        store._replace_quarantine(conn, run_id, quality.quarantine)
        verification = verify.verify_run(settings, store, run_id,
                                         quality.accounts, conn=conn)
        if not verification["ok"]:
            # 显式回滚：候选版本不留痕，当前版本不动，失败修订另登记
            conn.rollback()
            conn.close()
            closed = True
            run_manifest.duration_ms = clock.elapsed_ms()
            return _fail_and_record(
                store, revision_id, run_id, parent_id, root_run_id, revision_no,
                input_name, digest, idem_key, mode, created_at, impact,
                verification, "整版核对未通过：" + _failed_names(verification))
        record = {
            "revision_id": revision_id,
            "run_id": run_id,
            "parent_run_id": parent_id,
            "root_run_id": root_run_id,
            "revision_no": revision_no,
            "input_name": input_name,
            "input_sha256": digest,
            "idempotency_key": idem_key,
            "mode": mode,
            "status": "switched",
            "switched": True,
            "affected": impact,
            "verification": verification,
            "created_at": created_at,
            "fail_reason": "",
        }
        store._save_revision(conn, record)
        store._set_current_version(conn, run_id, created_at)
        conn.commit()
        elapsed_ms = clock.elapsed_ms()
        run_manifest.duration_ms = elapsed_ms
        summary["duration_ms"] = elapsed_ms
        # 提交后回写耗时（不触碰事实数据，单独短事务）
        store.save_run(run_manifest.as_dict(), run_key_value, summary)
    except Exception as exc:  # 任何意外都保证回滚、沿用旧版
        if not closed:
            conn.rollback()
        conn.close()
        closed = True
        return _fail_and_record(
            store, revision_id, run_id, parent_id, root_run_id, revision_no,
            input_name, digest, idem_key, mode, created_at, impact,
            {"ok": False, "checks": [], "error": str(exc)},
            "切换事务异常，已回滚沿用旧版：" + str(exc))
    finally:
        if not closed:
            conn.close()

    payload = dict(summary)
    payload["revision_result"] = {
        "status": "switched",
        "switched": True,
        "reused": False,
        "revision_id": revision_id,
        "parent_run_id": parent_id,
        "root_run_id": root_run_id,
        "revision_no": revision_no,
        "mode": mode,
        "affected": impact,
        "verification": verification,
        "current_run_id": store.current_run_id(),
    }
    return payload


def _failed_names(verification: dict) -> str:
    return "、".join(item["name"] for item in verification["checks"] if not item["ok"])


def _fail_and_record(store, revision_id, run_id, parent_id, root_run_id, revision_no,
                     input_name, digest, idem_key, mode, created_at, impact,
                     verification, reason) -> dict:
    """切换失败：登记失败修订（不切换指针），当前版本沿用旧版。

    失败记录 revision_no 固定 0：它不属于成功版本链，不占用链上序号。
    """
    record = {
        "revision_id": revision_id,
        "run_id": run_id,
        "parent_run_id": parent_id,
        "root_run_id": root_run_id,
        "revision_no": 0,
        "input_name": input_name,
        "input_sha256": digest,
        "idempotency_key": idem_key,
        "mode": mode,
        "status": "failed",
        "switched": False,
        "affected": impact,
        "verification": verification,
        "created_at": created_at,
        "fail_reason": reason,
    }
    store.save_revision(record)
    current_id = store.current_run_id() or parent_id
    return {
        "run_id": run_id,
        "reused": False,
        "revision_result": {
            "status": "failed",
            "switched": False,
            "reused": False,
            "revision_id": revision_id,
            "parent_run_id": parent_id,
            "root_run_id": root_run_id,
            "revision_no": 0,
            "mode": mode,
            "affected": impact,
            "verification": verification,
            "fail_reason": reason,
            "current_run_id": current_id,
        },
    }


def _idempotent_payload(existing: dict, store) -> dict:
    status = existing["status"]
    current_id = store.current_run_id()
    result = {
        "status": status,
        "switched": existing["switched"],
        "reused": True,
        "revision_id": existing["revision_id"],
        "parent_run_id": existing["parent_run_id"],
        "root_run_id": existing["root_run_id"],
        "revision_no": existing["revision_no"],
        "mode": existing["mode"],
        "affected": existing["affected"],
        "verification": existing["verification"],
        "fail_reason": existing["fail_reason"],
        "current_run_id": current_id,
    }
    return {"run_id": existing["run_id"], "reused": True, "revision_result": result}


def _find_revision(store, idem_key: str) -> dict | None:
    for record in store.list_revisions():
        if record["idempotency_key"] == idem_key:
            return record
    return None


def _find_switched_by_content(store, input_sha256: str, mode: str,
                              parent_run_id: str) -> dict | None:
    """同一修订文件（sha + 模式）曾成功切换，则视为重试。

    只匹配 switched 且父版本不同于当前父版本的记录：在同一父版本上的请求由
    精确幂等键处理；失败修订不阻止在新父版本上重新提交同一文件。
    """
    matches = [record for record in store.list_revisions()
               if record["input_sha256"] == input_sha256
               and record["mode"] == mode and record["switched"]
               and record["parent_run_id"] != parent_run_id]
    return matches[-1] if matches else None


def revision_history(store, root_run_id: str | None = None) -> dict:
    records = store.list_revisions(root_run_id)
    current_id = store.current_run_id()
    return {
        "current_run_id": current_id,
        "root_run_id": root_run_id or (records[0]["root_run_id"] if records else ""),
        "revisions": records,
        "total": len(records),
    }