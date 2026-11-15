"""修订与复核入口：补报/撤回整版切换、版本链、按版本钉住的复核结论。"""
from __future__ import annotations

import datetime as dt
import os

from fastapi import APIRouter, File, Form, Request, UploadFile

from app import errors
from app.modules.m1_ingest.handler import MAX_UPLOAD, _sample_path
from app.modules.m7_review import sensitiv
from app.modules.m8_revision import service
from app.web import state

router = APIRouter()

REVIEW_VERDICTS = ("confirmed", "rejected", "needs_followup")


async def _read_input(settings, file, sample) -> tuple[bytes, str]:
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise errors.BadInput("文件超过 8MB 上限", code="E_FILE_TOO_LARGE")
        return data, os.path.basename(file.filename)
    name = sample or ""
    if not name:
        raise errors.BadInput("缺少修订文件或样例名", code="E_BAD_INPUT")
    path = _sample_path(settings, name)
    with open(path, "rb") as handle:
        return handle.read(), name


@router.post("/api/revisions")
async def create_revision(
    request: Request,
    file: UploadFile | None = File(default=None),
    sample: str | None = Form(default=None),
    mode: str = Form(default="partial"),
    parent_run_id: str | None = Form(default=None),
):
    app_state = state(request)
    data, name = await _read_input(app_state.settings, file, sample)
    return service.apply_revision(
        app_state.settings, app_state.store, data, name,
        mode=mode, parent_run_id=parent_run_id or None)


@router.get("/api/revisions")
def list_revisions(request: Request, root_run_id: str = ""):
    app_state = state(request)
    return service.revision_history(app_state.store, root_run_id or None)


@router.get("/api/revisions/{run_id}/lineage")
def get_lineage(request: Request, run_id: str):
    app_state = state(request)
    run = app_state.store.get_run(run_id)
    if run is None:
        raise errors.NotFound("批次不存在：" + run_id, code="E_BATCH_NOT_FOUND")
    chain = app_state.store.lineage_for_run(run_id)
    return {
        "run_id": run_id,
        "current_run_id": app_state.store.current_run_id(),
        "lineage": chain,
        "depth": len(chain),
    }


@router.post("/api/review/mark")
async def mark_reviewed(request: Request):
    app_state = state(request)
    body = await request.json() or {}
    event_id = (body.get("event_id") or "").strip()
    verdict = (body.get("verdict") or "").strip()
    run_id = (body.get("run_id") or "").strip()
    if not event_id:
        raise errors.BadInput("缺少 event_id", code="E_BAD_INPUT")
    if verdict not in REVIEW_VERDICTS:
        raise errors.BadInput(
            "verdict 只支持 " + "/".join(REVIEW_VERDICTS), code="E_BAD_AXIS")
    run = app_state.store.get_run(run_id) if run_id else app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    active_run_id = run["run_id"]

    # event_id 必须能在该版本的检测结果里复算出来，避免给不存在的事件上结论
    from app.modules.m5_attrib import service as batch_service
    from app.modules.m4_detect import service as detect_service
    points = batch_service.load_view(app_state.store, active_run_id)
    detection = detect_service.detect(points, app_state.settings)
    known = {sensitiv.event_id(event): event for event in detection["events"]}
    if event_id not in known:
        raise errors.NotFound("复盘事件不存在：" + event_id, code="E_CARD_NOT_FOUND")
    event = known[event_id]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mark = app_state.store.save_review_mark(
        active_run_id, event_id, event["series"], verdict,
        (body.get("note") or "").strip(), (body.get("reviewer") or "").strip(), stamp)
    return {"mark": mark, "pinned_run_id": active_run_id}


@router.get("/api/review/marks")
def list_marks(request: Request, run_id: str = ""):
    app_state = state(request)
    run = app_state.store.get_run(run_id) if run_id else app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    marks = app_state.store.load_review_marks(run["run_id"])
    return {"run_id": run["run_id"], "marks": marks, "count": len(marks)}
