"""HR administration: completion approvals, audit log, dataset snapshots and export."""

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.api.auth import Principal, current_account, require_hr
from backend.api.presenters import complete_activity_response
from backend.storage import DatasetStore

router = APIRouter()


class Decision(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


def store(request: Request) -> DatasetStore:
    return request.app.state.store


@router.get("/completion-requests")
def list_completion_requests(status: str | None = Query("pending", pattern="^(pending|approved|rejected|expired|all)$"),
                             employee_id: str | None = Query(None, max_length=64),
                             limit: int = Query(200, ge=1, le=1000),
                             account: Principal = Depends(current_account),
                             repository: DatasetStore = Depends(store)):
    """HR sees every request; an employee sees only their own."""
    if account.role != "hr":
        employee_id = account.employee_id
    return repository.list_requests(status=None if status == "all" else status, employee_id=employee_id, limit=limit)


def _decide(approve: bool):
    def handler(request_id: str, request: Request, body: Decision = Body(Decision()),
                account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
        today = datetime.now(timezone.utc).date()
        from uuid import uuid4
        return repository.decide_request(
            request_id, approve=approve, actor=account.actor(request), comment=body.comment,
            complete=lambda dataset, employee_id, event_id: complete_activity_response(
                dataset, employee_id, event_id, f"CMP_{uuid4().hex}", today))
    return handler


router.add_api_route("/completion-requests/{request_id}/approve", _decide(True), methods=["POST"],
                     summary="Approve a completion: skills and history are updated now")
router.add_api_route("/completion-requests/{request_id}/reject", _decide(False), methods=["POST"],
                     summary="Reject a completion request; nothing changes in the profile")


@router.get("/audit")
def audit_log(limit: int = Query(100, ge=1, le=500), before_id: int | None = Query(None, ge=1),
              action: str | None = Query(None, max_length=64), actor_id: str | None = Query(None, max_length=64),
              target: str | None = Query(None, max_length=200),
              account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
    return repository.list_audit(limit=limit, before_id=before_id, action=action, actor_id=actor_id, target=target)


@router.get("/dataset/snapshots")
def snapshots(account: Principal = Depends(require_hr), repository: DatasetStore = Depends(store)):
    return repository.list_snapshots()


@router.post("/dataset/snapshots", status_code=201)
def create_snapshot(request: Request, account: Principal = Depends(require_hr),
                    repository: DatasetStore = Depends(store)):
    return repository.create_snapshot("manual", account.actor(request))


@router.post("/dataset/snapshots/{snapshot_id}/restore")
def restore_snapshot(snapshot_id: int, request: Request, account: Principal = Depends(require_hr),
                     repository: DatasetStore = Depends(store)):
    """Replace the dataset with a snapshot. The current state is snapshotted first."""
    return repository.restore_snapshot(snapshot_id, account.actor(request))


@router.get("/dataset/export")
def export_dataset(request: Request, account: Principal = Depends(require_hr),
                   repository: DatasetStore = Depends(store)):
    dataset = repository.snapshot()
    repository.audit(account.actor(request), "dataset_exported", None,
                     {"employees": len(dataset.employees), "history_records": len(dataset.activity_history)})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return JSONResponse(dataset.model_dump(mode="json"), headers={
        "Content-Disposition": f'attachment; filename="career-quest-export-{stamp}.json"'})
