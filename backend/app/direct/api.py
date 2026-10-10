"""HTTP API of the direct report (client ATNM databases → AlTasnimBI).

    GET  /api/direct-report/preflight           servers reachable, plan readable, other runs
    GET  /api/direct-report/status?since=N      the run, its stages and the log after entry N
    POST /api/direct-report/start               start a run (on the computer that runs the app)
    POST /api/direct-report/cancel              stop it
    GET  /api/direct-report/list                the saved reports, newest first
    GET  /api/direct-report/download/{run_id}   the Excel workbook of one report

Starting and stopping follow the same rule as the migration report (REPORT_ALLOW_REMOTE).
"""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..report.api import _allowed, _require_local
from . import jobs

router = APIRouter(prefix="/api/direct-report", tags=["Direct report"])


@router.get("/preflight")
def preflight(request: Request):
    return {**jobs.preflight(), "allowed": _allowed(request)}


@router.get("/status")
def status(request: Request, since: int | None = Query(None, ge=0)):
    return {**jobs.status(since), "allowed": _allowed(request)}


@router.post("/start")
def start(request: Request):
    _require_local(request)
    ok, message = jobs.start()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return jobs.status(0)


@router.post("/cancel")
def cancel(request: Request):
    _require_local(request)
    return {"cancelled": jobs.cancel(), **jobs.status()}


@router.get("/list")
def reports():
    return {"reports": jobs.reports()}


@router.get("/download/{run_id}")
def download(run_id: str):
    path = jobs.file_of(run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No such report file.")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename=path.name)
