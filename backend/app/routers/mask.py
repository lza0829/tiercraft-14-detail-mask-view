from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import masking

router = APIRouter(tags=["脱敏导出"])


class RuleBody(BaseModel):
    name: str = Field(..., description="规则名称")
    keep_head: int = Field(1, description="保留首字符数")
    keep_tail: int = Field(1, description="保留尾字符数")
    mask_char: str = Field("*", description="掩码字符（单个）")


class SnapshotBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")


class ExportBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")
    batch_id: int = Field(..., description="快照批次号")


@router.get("/mask/rules")
def list_rules():
    return masking.list_rules()


@router.post("/mask/rules")
def create_rule(body: RuleBody):
    try:
        return masking.create_rule(body.name, body.keep_head, body.keep_tail, body.mask_char)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/mask/rules/{version}/enable")
def enable_rule(version: int):
    try:
        return masking.enable_rule(version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mask/view")
def masked_view(domain_id: int | None = Query(default=None), limit: int = Query(default=200)):
    return masking.masked_view(domain_id, limit)


@router.get("/mask/batches")
def list_batches(domain_id: int | None = Query(default=None)):
    return masking.list_batches(domain_id)


@router.get("/mask/batches/{batch_id}/rows")
def list_snapshot_rows(batch_id: int):
    return masking.list_snapshot_rows(batch_id)


@router.post("/mask/snapshots")
def run_snapshot(body: SnapshotBody):
    try:
        return masking.run_snapshot(body.domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mask/exports")
def list_exports(domain_id: int | None = Query(default=None)):
    return masking.list_exports(domain_id)


@router.post("/mask/exports")
def run_export(body: ExportBody):
    try:
        return masking.run_export(body.domain_id, body.batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mask/exports/{export_id}/file")
def download_export(export_id: int):
    record = masking.get_export(export_id)
    if not record or record["status"] != "成功" or not record["file_path"]:
        raise HTTPException(status_code=404, detail="导出文件不存在")
    path = masking.export_file_path(record)
    if not path.exists():
        raise HTTPException(status_code=404, detail="导出文件已被移除")
    return FileResponse(path, filename=path.name)
