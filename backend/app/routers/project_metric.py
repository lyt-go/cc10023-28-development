"""项目分群指标接口：授权统计提交、共享撤销、洞察查看与所有方核对。

注意：本路由必须先于通用资源路由注册，否则 /api/project_metric/samples 等
固定路径会被 /api/{module}/{entry_id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services import service_for

router = APIRouter(prefix="/api/project_metric", tags=["项目分群指标"])


class MetricRequestPayload(BaseModel):
    project: str
    scope: str
    entry: str = "对照操作"


class RevokePayload(BaseModel):
    project: str
    group: str | None = None


class OwnerAuditPayload(BaseModel):
    owner: str
    project: str
    scope: str = "合计"


@router.post("/requests", response_model=dict)
def submit_metric_request(payload: MetricRequestPayload) -> dict[str, Any]:
    """提交项目分群统计（对照操作入口）；披露冲突时拒绝交付。"""
    service = service_for("project_metric")
    return service.submit_metric_request(payload.project, payload.scope, entry=payload.entry)


@router.post("/revoke", response_model=dict)
def revoke_sharing(payload: RevokePayload) -> dict[str, Any]:
    """撤销项目在某分群（或全部）的样本共享。"""
    service = service_for("project_metric")
    return service.revoke_sharing(payload.project, group=payload.group)


@router.post("/owner_audit", response_model=dict)
def owner_audit(payload: OwnerAuditPayload) -> dict[str, Any]:
    """数据所有方按项目当前有效授权重跑统计，核对被撤样本已剔除。"""
    service = service_for("project_metric")
    return service.run_owner_audit(payload.owner, payload.project, scope=payload.scope)


@router.get("/samples", response_model=dict)
def project_samples(project: str = Query(...)) -> dict[str, Any]:
    """明细授权逐行使用：返回项目可逐行读取的预测样本。"""
    service = service_for("project_metric")
    return service.project_samples(project)


@router.get("/insights", response_model=dict)
def list_insights(project: str | None = Query(default=None)) -> dict[str, Any]:
    """列出分群洞察（可按交付项目或数据所有方过滤）。"""
    service = service_for("mining_result")
    return service.list_group_insights(project)


@router.get("/insights/{insight_id}", response_model=dict)
def open_insight(insight_id: int, project: str = Query(...)) -> dict[str, Any]:
    """按查看方打开分群洞察：所有方看当时的分母与授权版本，被撤权项目只看撤权状态。"""
    service = service_for("mining_result")
    return service.open_group_insight(insight_id, project)
