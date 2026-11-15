"""M8 修订与版本化：补报/撤回整版切换、影响面、四账+分解核对、原子切换与幂等。

核心不变量：
- 新版本是不可变整版结果，旧版本的点、异常、导出原样保留；
- 按业务日期与快照事实算出受影响键，28 天基线与下钻份额随整版重算；
- 四账与分解全部平才切换，任一不平回滚并沿用旧版；
- 同一修订重试不新增版本（成功与失败都幂等）。
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.m4_detect import service as detect_service
from app.modules.m5_attrib import service as batch_service
from app.modules.m8_revision import facts, service as revision_service, verify
from app.store import Store

HEADER = ("hotel_code,hotel_name,channel,biz_date,rooms_available,rooms_sold,"
          "adr,revenue_net,updated_at,op_flag\n")


def _amend_bytes() -> bytes:
    """补报改写 H02/ota 11-17、11-18（消除确认异常）+ 撤回 + 补齐缺桶。"""
    rows = [
        "H02,蓝湾·湖畔,ota,2026-11-17,180,98,612.00,59976.00,2026-11-22T03:00:00Z,AMEND",
        "H02,蓝湾·湖畔,ota,2026-11-18,180,97,615.00,59655.00,2026-11-22T03:05:00Z,AMEND",
        "H01,蓝湾·滨海,direct,2026-11-10,0,0,,,2026-11-22T03:10:00Z,WITHDRAW",
        "H04,蓝湾·山居,member,2026-10-05,160,88,540.00,47520.00,2026-11-22T03:20:00Z,AMEND",
    ]
    return (HEADER + "\n".join(rows) + "\n").encode("utf-8")


def _only_withdraw_bytes() -> bytes:
    row = ("H01,蓝湾·滨海,direct,2026-11-10,0,0,,,2026-11-22T03:10:00Z,WITHDRAW\n")
    return (HEADER + row).encode("utf-8")


# ---- 影响面：按业务键与快照事实分类 ----------------------------------

def test_impact_scope_classified_by_biz_key(settings, store, run):
    from app.modules.m2_calendar import service as coerce_service
    from app.modules.m3_quality import service as quality_service

    parent_points = store.load_points(run["run_id"])
    ingest_rows = _ingest_rows(settings, _amend_bytes())
    merged = facts.merge_facts(parent_points, ingest_rows, settings)
    coerced, crejects = coerce_service.coerce_rows(merged["normal_rows"], settings)
    quality = quality_service.evaluate(
        merged["merged_seed_rows"] + coerced,
        crejects + merged["withdraw_entries"], settings,
        records_read=4, carried_points=len(merged["merged_seed_rows"]))
    impact = facts.classify_impact(merged["parent_before"], quality.points,
                                   merged["withdrawn_keys"], merged["revision_keys"])
    assert impact["counts"]["added"] == 1       # H04/member 2026-10-05 补齐
    assert impact["counts"]["changed"] == 2     # H02/ota 两天改写
    assert impact["counts"]["withdrawn"] == 1   # H01/direct 撤回
    assert impact["counts"]["affected"] == 4
    assert set(impact["affected_dates"]) == {
        "2026-10-05", "2026-11-10", "2026-11-17", "2026-11-18"}
    combos = set(impact["affected_combos"])
    assert {"H02|ota", "H01|direct", "H04|member"} <= combos


def test_withdraw_flag_recognized_in_variants(settings):
    base = {"hotel_code": "H01", "channel": "direct", "biz_date": "2026-11-10",
            "op_flag": "withdraw"}
    assert facts.is_withdraw(dict(base)) is True
    base["op_flag"] = "撤销"
    assert facts.is_withdraw(dict(base)) is True
    base["op_flag"] = "AMEND"
    assert facts.is_withdraw(dict(base)) is False
    base["op_flag"] = ""
    assert facts.is_withdraw(dict(base)) is False


# ---- 整版切换：新版本生成、旧版本不可变、基线整版重算 ------------------

def test_revision_switches_to_new_immutable_version(settings, store, run):
    before_id = run["run_id"]
    before_detect = detect_service.detect(
        batch_service.load_view(store, before_id), settings)
    assert before_detect["summary"]["events"] == 2

    out = revision_service.apply_revision(
        settings, store, _amend_bytes(), "amend.csv", mode="partial")
    result = out["revision_result"]
    new_id = out["run_id"]

    assert result["status"] == "switched" and result["switched"] is True
    assert new_id != before_id
    assert store.current_run_id() == new_id
    # 新版本四账与分解全部核对通过
    assert result["verification"]["ok"] is True
    assert {c["name"] for c in result["verification"]["checks"]} == {
        "行账", "点账", "金额账", "分解账"}

    # 旧版本点与异常结论原样保留（不可变）
    assert store.point_count(before_id) == run["accounts"]["points_valid"]
    old_detect = detect_service.detect(
        batch_service.load_view(store, before_id), settings)
    assert old_detect["summary"]["events"] == before_detect["summary"]["events"]

    # 新版本整版重算：H02/ota 确认异常被改写消除，只剩 H01/member 一段
    new_detect = detect_service.detect(
        batch_service.load_view(store, new_id), settings)
    assert new_detect["summary"]["events"] == 1


def test_revision_is_full_recompute_not_half(settings, store, run):
    """受影响门店之外的序列点也在同一整版结果里，基线窗口完整不缺半截。"""
    out = revision_service.apply_revision(
        settings, store, _amend_bytes(), "amend.csv", mode="partial")
    new_points = batch_service.load_view(store, out["run_id"])
    # 新版本点数 = 父 1001 - 撤回 1 + 新增 1（改写不改变点数）
    assert len(new_points) == run["accounts"]["points_valid"]
    h01_member = [p for p in new_points
                  if p["hotel_code"] == "H01" and p["channel"] == "member"]
    assert len(h01_member) == 84  # 未触及的序列完整保留，基线可整窗重算


def test_withdrawn_key_removed_and_accounted(settings, store, run):
    out = revision_service.apply_revision(
        settings, store, _only_withdraw_bytes(), "w.csv", mode="partial")
    assert out["revision_result"]["status"] == "switched"
    new_accounts = store.get_run(out["run_id"])["summary"]["accounts"]
    assert new_accounts["rows_withdrawn"] == 1
    # 撤回键不再是有效点
    points = store.load_points(out["run_id"])
    assert not any(p["hotel_code"] == "H01" and p["channel"] == "direct"
                   and p["biz_date"] == "2026-11-10" for p in points)
    # 撤回条目带稳定原因码，可在隔离/证据导出里复核
    codes = {e["code"] for e in store.load_quarantine(out["run_id"])}
    assert "W_WITHDRAWN" in codes
    # 行账恒等式（含承接与撤回项）
    checks = [c for c in out["revision_result"]["verification"]["checks"]
              if c["name"] == "行账"]
    assert checks and checks[0]["ok"]


# ---- 幂等：同一修订重试不新增版本（成功与失败都幂等） ------------------

def test_same_revision_retry_is_idempotent(settings, store, run):
    data = _amend_bytes()
    first = revision_service.apply_revision(settings, store, data, "a.csv", "partial")
    second = revision_service.apply_revision(settings, store, data, "a.csv", "partial")
    assert second["reused"] is True
    assert second["run_id"] == first["run_id"]
    assert len(store.list_revisions()) == 1
    # 当前指针不被重试改动
    assert store.current_run_id() == first["run_id"]


def test_distinct_revision_creates_next_version(settings, store, run):
    first = revision_service.apply_revision(
        settings, store, _only_withdraw_bytes(), "w.csv", "partial")
    second = revision_service.apply_revision(
        settings, store, _amend_bytes(), "a.csv", "partial")
    assert second["run_id"] != first["run_id"]
    assert second["revision_result"]["revision_no"] == 2
    chain = store.lineage_for_run(second["run_id"])
    assert [c["revision_no"] for c in chain] == [1, 2]
    assert chain[0]["parent_run_id"] == run["run_id"]


# ---- 切换失败：核对不平整体回滚，沿用旧版，失败可幂等重试 --------------

def test_failed_switch_rolls_back_and_keeps_old_version(settings, store, run):
    old_current = store.current_run_id()
    real_verify = verify.verify_run

    def forced_fail(s, st, rid, accounts, conn=None):
        res = real_verify(s, st, rid, accounts, conn=conn)
        res["ok"] = False
        res["checks"][2]["ok"] = False
        return res

    with mock.patch.object(revision_service.verify, "verify_run", forced_fail):
        out = revision_service.apply_revision(
            settings, store, _amend_bytes(), "a.csv", "partial")

    result = out["revision_result"]
    assert result["status"] == "failed" and result["switched"] is False
    # 当前版本沿用旧版
    assert store.current_run_id() == old_current == run["run_id"]
    # 候选版本已回滚，不留任何 run/points
    assert store.get_run(out["run_id"]) is None
    # 失败修订已登记备查
    records = store.list_revisions()
    assert len(records) == 1 and records[0]["status"] == "failed"

    # 同一失败修订重试：回放失败记录，不新增
    retry = revision_service.apply_revision(
        settings, store, _amend_bytes(), "a.csv", "partial")
    assert retry["revision_result"]["reused"] is True
    assert retry["revision_result"]["status"] == "failed"
    assert len(store.list_revisions()) == 1


def test_transaction_exception_keeps_old_version(settings, store, run):
    old_current = store.current_run_id()

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    # 在切换指针这一步炸掉：事实与修订记录写入已在同事务内，整体回滚
    with mock.patch.object(store, "_set_current_version", boom):
        out = revision_service.apply_revision(
            settings, store, _amend_bytes(), "a.csv", "partial")
    assert out["revision_result"]["status"] == "failed"
    assert store.current_run_id() == old_current
    # 候选 run 随事务回滚
    assert store.get_run(out["run_id"]) is None
    # 失败修订已用独立短事务登记
    assert store.list_revisions()[0]["status"] == "failed"


# ---- 版本链与基线定位 -------------------------------------------------

def test_revision_requires_existing_base(settings, store):
    with pytest.raises(revision_service.NotFound) as exc:
        revision_service.apply_revision(
            settings, store, _amend_bytes(), "a.csv", "partial")
    assert exc.value.code == "E_NO_RUN"


def test_full_mode_replaces_entire_fact_set(settings, store, run):
    # full 模式直接用主样例整版替换：影响面按全键比对，承接为 0
    import os
    data = open(os.path.join(settings.sample_dir, "daily_metrics_dirty.csv"), "rb").read()
    out = revision_service.apply_revision(
        settings, store, data, "full.csv", mode="full")
    assert out["revision_result"]["status"] == "switched"
    accounts = store.get_run(out["run_id"])["summary"]["accounts"]
    assert accounts["rows_carried"] == 0
    assert accounts["rows_accepted"] == run["accounts"]["points_valid"]


def test_failed_revision_keeps_chain_number_free(settings, store, run):
    """失败修订不占用版本链序号；修正后的不同文件拿到下一个成功序号。"""
    amend = _amend_bytes()
    first = revision_service.apply_revision(
        settings, store, amend, "a.csv", "partial")
    assert first["revision_result"]["revision_no"] == 1

    real_verify = verify.verify_run

    def forced_fail(s, st, rid, accounts, conn=None):
        res = real_verify(s, st, rid, accounts, conn=conn)
        res["ok"] = False
        res["checks"][0]["ok"] = False
        return res

    bad = ("hotel_code,hotel_name,channel,biz_date,rooms_available,rooms_sold,"
           "adr,revenue_net,updated_at,op_flag\n"
           "H02,蓝湾·湖畔,member,2026-11-09,150,80,600.00,48000.00,"
           "2026-11-22T05:00:00Z,AMEND\n").encode("utf-8")
    with mock.patch.object(revision_service.verify, "verify_run", forced_fail):
        failed = revision_service.apply_revision(
            settings, store, bad, "bad.csv", "partial")
    assert failed["revision_result"]["status"] == "failed"
    assert failed["revision_result"]["revision_no"] == 0

    # 修正后的不同文件（sha 变化）正常切换，成功序号仍是 2，不被失败记录占用
    fixed = bad.replace(b"48000.00", b"48001.00")
    success = revision_service.apply_revision(
        settings, store, fixed, "fixed.csv", "partial")
    assert success["revision_result"]["status"] == "switched"
    assert success["revision_result"]["revision_no"] == 2
    chain = store.lineage_for_run(success["run_id"])
    assert [c["revision_no"] for c in chain] == [1, 2]
    # 失败记录仍可在修订历史里追溯
    assert any(r["status"] == "failed" for r in store.list_revisions())


# ---- HTTP 接口 --------------------------------------------------------

def test_revision_api_and_pinned_exports(settings, run):
    app = create_app(settings)
    client = TestClient(app)
    base_id = run["run_id"]
    response = client.post(
        "/api/revisions",
        files={"file": ("amend.csv", _amend_bytes(), "text/csv")},
        data={"mode": "partial"})
    assert response.status_code == 200
    body = response.json()
    new_id = body["run_id"]
    assert body["revision_result"]["switched"] is True

    # 批次接口的 latest 已指向新版本
    assert client.get("/api/batches").json()["latest"] == new_id
    # 旧版本导出仍按 run_id 钉住，内容不变
    old_manifest = client.get(f"/api/export/manifest.json?run_id={base_id}").json()
    assert old_manifest["manifest"]["run_id"] == base_id
    # 异常接口支持按版本回看，新旧结论不同
    old_events = client.get(f"/api/anomalies?run_id={base_id}").json()["summary"]["events"]
    new_events = client.get(f"/api/anomalies?run_id={new_id}").json()["summary"]["events"]
    assert old_events == 2 and new_events == 1
    # 版本链接口
    lineage = client.get(f"/api/revisions/{new_id}/lineage").json()
    assert lineage["depth"] == 1 and lineage["current_run_id"] == new_id


def test_review_mark_is_pinned_to_version_and_not_copied(settings, run):
    app = create_app(settings)
    client = TestClient(app)
    base_id = run["run_id"]
    revised = revision_service.apply_revision(
        settings, app.state.anomalyattr.store, _amend_bytes(), "a.csv", "partial")
    new_id = revised["run_id"]

    review = client.get(f"/api/review?run_id={base_id}").json()
    event_id = review["cards"][0]["event_id"]
    mark = client.post("/api/review/mark", json={
        "run_id": base_id, "event_id": event_id, "verdict": "confirmed",
        "note": "经营会已确认", "reviewer": "analyst-1"})
    assert mark.status_code == 200
    assert mark.json()["pinned_run_id"] == base_id

    # 复核结论只钉在旧版本，新版本不带
    assert client.get(f"/api/review/marks?run_id={base_id}").json()["count"] == 1
    assert client.get(f"/api/review/marks?run_id={new_id}").json()["count"] == 0
    # 非法结论 400，不存在事件 404
    assert client.post("/api/review/mark", json={
        "run_id": base_id, "event_id": event_id, "verdict": "x"}).status_code == 400
    assert client.post("/api/review/mark", json={
        "run_id": new_id, "event_id": "EV-nope", "verdict": "confirmed"}).status_code == 404


def test_revision_api_idempotent_over_http(settings, run):
    app = create_app(settings)
    client = TestClient(app)
    payload = {"files": {"file": ("a.csv", _amend_bytes(), "text/csv")},
               "data": {"mode": "partial"}}
    first = client.post("/api/revisions", **payload).json()
    second = client.post("/api/revisions", **payload).json()
    assert second["revision_result"]["reused"] is True
    assert second["run_id"] == first["run_id"]


# ---- 辅助 -------------------------------------------------------------

def _ingest_rows(settings, data: bytes) -> list[dict]:
    from app.modules.m1_ingest import service as ingest_service
    return ingest_service.ingest(data, "rev.csv", settings).rows


# ---- 迟到与顺序无关 ---------------------------------------------------

def test_late_revision_kept_and_marked_out_of_window(settings, store, run):
    late = ("hotel_code,hotel_name,channel,biz_date,rooms_available,rooms_sold,"
            "adr,revenue_net,updated_at\n"
            "H01,蓝湾·滨海,ota,2026-11-05,180,90,700.00,63000.00,"
            "2026-11-22T06:00:00Z\n").encode("utf-8")
    out = revision_service.apply_revision(
        settings, store, late, "late.csv", mode="partial")
    assert out["revision_result"]["status"] == "switched"
    point = next(p for p in store.load_points(out["run_id"])
                 if p["hotel_code"] == "H01" and p["channel"] == "ota"
                 and p["biz_date"] == "2026-11-05")
    assert point["late_flag"] is True and point["out_of_window"] is True
    assert point["late_days"] == 17


def test_revision_order_independent(settings, store, run, tmp_path):
    data = _amend_bytes()
    lines = data.decode().splitlines()
    shuffled = (lines[0] + "\n" + "\n".join(reversed(lines[1:]))).encode("utf-8")

    import os
    from app import config as cfg
    base_bytes = open(os.path.join(settings.sample_dir,
                                   "daily_metrics_dirty.csv"), "rb").read()

    def fresh_store(tag):
        s = cfg.Settings(db_path=str(tmp_path / f"{tag}.sqlite"),
                         sample_dir=settings.sample_dir, seed_on_start=False)
        st = Store(s.db_path)
        batch_service.run_batch(s, st, base_bytes, "base.csv")
        return s, st

    s1, st1 = fresh_store("a")
    s2, st2 = fresh_store("b")
    r1 = revision_service.apply_revision(s1, st1, data, "r.csv", "partial")
    r2 = revision_service.apply_revision(s2, st2, shuffled, "r.csv", "partial")
    assert (st1.get_run(r1["run_id"])["summary"]["accounts"]
            == st2.get_run(r2["run_id"])["summary"]["accounts"])
    assert (r1["revision_result"]["affected"]["counts"]
            == r2["revision_result"]["affected"]["counts"])
