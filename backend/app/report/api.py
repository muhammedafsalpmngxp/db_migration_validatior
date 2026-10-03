"""HTTP API of the report section.

    GET  /api/report/preflight              can a run start? servers, plan, other runs, AI
    GET  /api/report/status?since=n         the run: stage, progress, current table/mapping, log
    POST /api/report/start {resume}         start (or resume) a report run
    POST /api/report/cancel                 stop it; a partial report is still written
    GET  /api/report/list                   the saved reports
    POST /api/report/rebuild {run_id}       write a saved report's files again (no database read)
    GET  /api/report/download/{id}/{kind}   the Word (docx) or Excel (xlsx) file

Starting, stopping and rebuilding read the shared servers or change files, so they are only
allowed from this computer unless REPORT_ALLOW_REMOTE=yes; anyone may follow and download.
"This computer" means the app was opened as http://localhost:3000: the frontend forwards the
address the browser used (X-Forwarded-For is not sent by it), and only this computer can open
the app under localhost. A guard against starting a run by mistake, not a login.
"""
import socket

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import jobs, settings

router = APIRouter(prefix="/api/report", tags=["Report"])


def _local_addresses():
    found = {"127.0.0.1", "::1", "localhost"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0].split("%")[0])
    except OSError:
        pass
    return found


LOCAL = _local_addresses()


def _client(request: Request):
    """The browser's address: the first X-Forwarded-For entry (the frontend forwards the
    request), else the connection's address."""
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
    return ip.removeprefix("::ffff:")


LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _opened_as(request: Request):
    """The host name the browser used to open the app (the frontend forwards it)."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _allowed(request: Request):
    return settings.ALLOW_REMOTE or (_client(request) in LOCAL and _opened_as(request) in LOOPBACK)


def _require_local(request: Request):
    if not _allowed(request):
        raise HTTPException(status_code=403, detail="Reports can only be started or changed on the computer that runs "
                                                    "the app, opened as http://localhost:3000 (a run reads the shared "
                                                    "servers for a while). You can follow it and download the files here.")


@router.get("/preflight")
def preflight(request: Request):
    return {**jobs.preflight(), "allowed": _allowed(request)}


@router.get("/status")
def status(request: Request, since: int | None = Query(None, ge=0)):
    return {**jobs.status(since), "allowed": _allowed(request), "client": _client(request),
            "opened_as": _opened_as(request)}


class StartRequest(BaseModel):
    resume: bool = False


@router.post("/start")
def start(req: StartRequest, request: Request):
    _require_local(request)
    ok, message = jobs.start(resume=req.resume)
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


class RebuildRequest(BaseModel):
    run_id: str


@router.post("/rebuild")
def rebuild(req: RebuildRequest, request: Request):
    _require_local(request)
    if jobs.job["running"]:
        raise HTTPException(status_code=409, detail="A report is being made; rebuild when it has finished.")
    if not any(r["run_id"] == req.run_id for r in jobs.reports()):
        raise HTTPException(status_code=404, detail="No such report.")
    return jobs.rebuild(req.run_id)


@router.get("/download/{run_id}/{kind}")
def download(run_id: str, kind: str):
    path = jobs.file_of(run_id, kind)
    if path is None:
        raise HTTPException(status_code=404, detail="No such report file.")
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if kind == "docx"
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return FileResponse(path, media_type=media, filename=path.name)
