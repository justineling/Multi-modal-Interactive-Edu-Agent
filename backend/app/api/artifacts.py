"""画布产物接口：列表 / 详情 / 版本 / patch / 删除。

patch 是这里唯一有写逻辑的端点，三条不可妥协的规则：
1. **落库用 `PatchOutcome.spec`**（保持原键序的 dict），不能用 `.model` 重新 dump ——
   `model_dump` 会按字段声明顺序重排键，P0-10② 的「其余节点字节级不变」当场失效。
2. **`base_version` 乐观锁**：不匹配返回 409 `PATCH_BASE_VERSION_CONFLICT`，
   防止两个标签页互相覆盖（PRD Q3 的「版本自增可回看」也依赖它）。
3. **历史版本只读**：详情接口在 `?version=` 分支只从 `artifact_versions` 取快照，
   patch 分支只往 versions 表 append，没有任何 UPDATE/DELETE 历史行的路径（P0-11③）。
"""

from __future__ import annotations

import copy
import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import repository
from app.db.session import get_session
from app.errors import AppError, ErrorCode
from app.logging_conf import log_event
from app.schemas.api import (
    ArtifactOut,
    ArtifactVersionOut,
    DeletedOut,
    artifact_out,
    artifact_version_out,
)
from app.schemas.common import ApiResponse
from app.schemas.patch import PatchRequest
from app.services.patch_engine import apply_patch_request

router = APIRouter(tags=["artifacts"])


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


@router.get(
    "/api/workspaces/{workspace_id}/artifacts",
    response_model=ApiResponse[list[ArtifactOut]],
    tags=["artifacts"],
)
async def list_artifacts(
    workspace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ArtifactOut]]:
    await repository.require_workspace(session, workspace_id)
    artifacts = await repository.list_artifacts(session, workspace_id)
    return ApiResponse[list[ArtifactOut]].ok(
        [artifact_out(artifact) for artifact in artifacts], request_id=_request_id(request)
    )


@router.get(
    "/api/artifacts/{artifact_id}", response_model=ApiResponse[ArtifactOut], tags=["artifacts"]
)
async def get_artifact(
    artifact_id: str,
    request: Request,
    version: int | None = Query(default=None, ge=1, description="不传取最新版本"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ArtifactOut]:
    """详情。传 `version` 时返回该历史版本的 spec（只读预览，P0-11②）。"""
    artifact = await repository.require_artifact(session, artifact_id)
    if version is None:
        return ApiResponse[ArtifactOut].ok(
            artifact_out(artifact), request_id=_request_id(request)
        )

    row = await repository.get_artifact_version(session, artifact_id, version)
    if row is None:
        raise AppError(ErrorCode.ARTIFACT_NOT_FOUND, f"该产物没有 v{version} 版本")
    return ApiResponse[ArtifactOut].ok(
        artifact_out(artifact, spec=row.spec_json or {}, version=row.version),
        request_id=_request_id(request),
    )


@router.get(
    "/api/artifacts/{artifact_id}/versions",
    response_model=ApiResponse[list[ArtifactVersionOut]],
    tags=["artifacts"],
)
async def list_artifact_versions(
    artifact_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ArtifactVersionOut]]:
    """版本列表（升序）。`patchable=false` 的旧版本只能看，不能再基于它 patch。"""
    artifact = await repository.require_artifact(session, artifact_id)
    rows = await repository.list_artifact_versions(session, artifact_id)
    keep = get_settings().ARTIFACT_VERSION_KEEP
    return ApiResponse[list[ArtifactVersionOut]].ok(
        [
            artifact_version_out(row, current_version=artifact.version, keep=keep)
            for row in rows
        ],
        request_id=_request_id(request),
    )


@router.post(
    "/api/artifacts/{artifact_id}/patch",
    response_model=ApiResponse[ArtifactOut],
    tags=["artifacts"],
)
async def patch_artifact(
    artifact_id: str,
    payload: PatchRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ArtifactOut]:
    """应用一批 PatchOp：深拷贝定点写入 → 强校验 → append 版本 → 快照前移。"""
    if payload.artifact_id != artifact_id:
        raise AppError(
            ErrorCode.REQUEST_INVALID, "路径中的 artifact_id 与请求体不一致"
        )

    artifact = await repository.require_artifact(session, artifact_id)
    # 必须 deepcopy：`dict()` 只复制最外层，嵌套的 slides/nodes 仍与 ORM 对象（以及
    # 同一 session 里已加载的历史版本行）共享引用。虽然 patch_engine 内部还会再拷一次，
    # 但入口处就把引用切断，能保证「读到的基线」与「库里/内存里的对象」永不互相牵连。
    spec_dict = copy.deepcopy(artifact.spec_json or {})

    outcome = apply_patch_request(spec_dict, payload, current_version=artifact.version)
    try:
        await repository.apply_artifact_patch(
            session,
            artifact=artifact,
            spec_json=outcome.spec,
            patch_ops=[op.model_dump(mode="json") for op in payload.ops],
        )
        await session.flush()
    except IntegrityError as exc:
        # 「读版本 → 比版本 → 写版本」三步之间没有原子性：两个标签页同时基于 v1 提交时，
        # 两个请求都能通过 base_version 比较，第二个写版本行会撞 (artifact_id, version)
        # 唯一约束。数据没坏（约束兜住了），但用户会看到 500 —— 必须翻译成 409，前端才能
        # 提示「画布已被更新，请刷新」（P0-10 / PRD Q3）。
        await session.rollback()
        log_event(
            "patch_version_race",
            level=logging.WARNING,
            artifact_id=artifact_id,
            base_version=payload.base_version,
        )
        raise AppError(
            ErrorCode.PATCH_BASE_VERSION_CONFLICT,
            "画布已被另一次修改更新，请刷新后重试",
        ) from exc

    log_event(
        "patch_applied",
        artifact_id=artifact_id,
        version=outcome.new_version,
        changed_paths=list(outcome.changed_paths),
        focus_preset=outcome.focus_preset,
    )
    return ApiResponse[ArtifactOut].ok(artifact_out(artifact), request_id=_request_id(request))


@router.delete(
    "/api/artifacts/{artifact_id}", response_model=ApiResponse[DeletedOut], tags=["artifacts"]
)
async def delete_artifact(
    artifact_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DeletedOut]:
    """删除整个产物（历史版本随之级联删除）。这是唯一允许历史版本消失的路径。"""
    await repository.delete_artifact(session, artifact_id)
    return ApiResponse[DeletedOut].ok(DeletedOut(deleted=True), request_id=_request_id(request))


__all__ = ["router"]
