"""项目分群指标领域服务：按项目授权范围聚合可共享预测样本、计算高分率并防控披露。

授权模型（按样本版本记录项目、样本归属和授权版本）：
- 明细授权：允许逐行使用，样本取值对项目已知；
- 仅汇总授权：只能进入获准分组范围的分子/分母，项目不得反推出单条样本的高分状态；
- 私有样本：不能算入任何项目的分子或分母。

披露防护：每个项目的交付台账从首次交付起累计，新统计与任一已交付结果（或明细已知集）
组合后若能隔离出一条非明细样本，即构成披露冲突，拒绝交付。挖掘详情与对照操作两个入口
共用同一把锁与同一条核对流水线，并发到达时只有一项能生成洞察。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 同一项目的统计请求必须串行核对交付台账，并发到达时只有一项能交付。
_METRIC_LOCK = threading.Lock()

# 授权级别
GRANT_DETAIL = "明细"      # 逐行可读
GRANT_AGGREGATE = "仅汇总"  # 只能进入获准分组范围的分子分母
GRANT_PRIVATE = "私有"     # 不能算入分子或分母

# 一次交付中允许包含的非明细样本最小单元：恰为 1 条时其高分状态会被直接暴露
MIN_NON_DETAIL_CELL = 2


class ProjectMetricService(BaseModuleService):
    MODULE = "project_metric"
    ENTITY = "项目分群指标"
    OPERATION_TERM = "统计"
    LIST_FIELDS = ["指标编号", "项目名称", "统计范围", "分子", "分母", "高分率", "授权版本", "指标状态"]
    REQUIRED_FIELDS = ["指标编号", "项目名称", "统计范围"]
    STATUS_ORDER = ["草稿", "待复核", "已交付", "已拒绝", "已撤权"]
    ACTION_RULES = {"提交复核": "待复核", "确认交付": "已交付", "标记拒绝": "已拒绝", "标记撤权": "已撤权"}
    NEGATIVE_ACTIONS = ["标记拒绝", "标记撤权"]

    # ── 授权与样本读取 ──
    def _grants(self, project: str, *, valid_only: bool = True) -> list[dict[str, Any]]:
        rows = [g for g in store.internal_rows("sample_grant") if g.get("项目") == project]
        if valid_only:
            rows = [g for g in rows if g.get("状态") == "有效"]
        return rows

    def _sample_map(self) -> dict[str, dict[str, Any]]:
        return {s.get("样本编号"): s for s in store.internal_rows("predict_sample")}

    def project_samples(self, project: str) -> dict[str, Any]:
        """明细授权允许逐行使用：返回项目当前可逐行读取的预测样本。"""
        detail_ids = {
            g.get("样本编号") for g in self._grants(project)
            if g.get("授权级别") == GRANT_DETAIL
        }
        samples = self._sample_map()
        rows = [dict(samples[sid]) for sid in sorted(detail_ids) if sid in samples]
        return {"ok": True, "project": project, "samples": rows, "count": len(rows)}

    def _allowed_samples(self, project: str, scope: str) -> list[dict[str, Any]]:
        """聚合可共享预测样本：有效、非私有且分组范围获准的样本才计入分子分母。"""
        samples = self._sample_map()
        allowed: list[dict[str, Any]] = []
        for grant in self._grants(project):
            if grant.get("授权级别") == GRANT_PRIVATE:
                continue  # 私有样本不能算入分子或分母
            scopes = [s.strip() for s in str(grant.get("允许范围", "")).split(",") if s.strip()]
            if scope not in scopes:
                continue  # 仅汇总授权限定可用的分组范围
            sample = samples.get(grant.get("样本编号"))
            if sample is None:
                continue
            if scope != "合计" and sample.get("分群") != scope:
                continue
            allowed.append({
                **sample,
                "授权级别": grant.get("授权级别"),
                "授权版本": grant.get("授权版本"),
            })
        return allowed

    # ── 披露核对 ──
    def _detail_ids(self, project: str) -> set[str]:
        return {
            g.get("样本编号") for g in self._grants(project)
            if g.get("授权级别") == GRANT_DETAIL
        }

    def _deliveries(self, project: str) -> list[dict[str, Any]]:
        """项目从首次交付起实际收到的统计结果台账。"""
        return [d for d in store.internal_rows("metric_delivery") if d.get("项目") == project]

    def _disclosure_conflict(self, project: str, new_set: set[str]) -> tuple[bool, str]:
        """核对新项目实际收到的全部结果：组合后若能隔离出一条非明细样本即冲突。"""
        detail = self._detail_ids(project)
        known: list[tuple[str, set[str]]] = []
        if detail:
            known.append(("明细授权", set(detail)))
        for delivery in self._deliveries(project):
            known.append((str(delivery.get("指标编号", "?")), set(delivery.get("样本集", []))))
        for label, old_set in known:
            for diff in (new_set - old_set, old_set - new_set):
                if not diff:
                    continue
                unknown = diff - detail
                if len(unknown) == 1:
                    target = sorted(unknown)[0]
                    return True, (
                        f"与{label}组合后可反推出仅汇总样本{target}的高分状态，存在披露冲突"
                    )
        return False, ""

    # ── 统计提交（挖掘详情 / 对照操作共用） ──
    def submit_comparison_metric(self, project: str, scope: str) -> dict[str, Any]:
        """对照操作入口：与挖掘详情入口共用同一把锁与同一条披露核对流水线。"""
        return self.submit_metric_request(project, scope, entry="对照操作")

    def submit_metric_request(self, project: str, scope: str, entry: str = "对照操作") -> dict[str, Any]:
        """提交项目分群统计：核对授权范围与交付台账，交付高分率并生成待复核分群洞察。"""
        with _METRIC_LOCK:
            allowed = self._allowed_samples(project, scope)
            if not allowed:
                row = self._record_metric(
                    project, scope, 0, 0, "-", "已拒绝", entry, reason="授权范围内无可统计样本")
                return {
                    "ok": False, "reason": "无可统计样本", "metric_id": row["id"],
                    "message": f"{project}在{scope}范围内没有可计入分子分母的样本",
                }
            new_set = {s["样本编号"] for s in allowed}
            detail = self._detail_ids(project)
            non_detail = new_set - detail
            if 0 < len(non_detail) < MIN_NON_DETAIL_CELL:
                target = sorted(non_detail)[0]
                row = self._record_metric(
                    project, scope, 0, 0, "-", "已拒绝", entry, reason="披露冲突")
                return {
                    "ok": False, "reason": "披露冲突", "metric_id": row["id"],
                    "message": f"{scope}范围内仅汇总样本{target}会被单独暴露，存在披露冲突",
                }
            conflict, message = self._disclosure_conflict(project, new_set)
            if conflict:
                row = self._record_metric(
                    project, scope, 0, 0, "-", "已拒绝", entry, reason="披露冲突")
                return {"ok": False, "reason": "披露冲突", "metric_id": row["id"], "message": message}
            numerator = sum(1 for s in allowed if s.get("分数状态") == "高分")
            denominator = len(allowed)
            rate = f"{numerator / denominator:.4f}"
            version = self._auth_version(allowed)
            row = self._record_metric(
                project, scope, numerator, denominator, rate, "已交付", entry, version=version)
            self._record_delivery(project, row, new_set, version)
            insight = self._create_insight(
                project, row, scope, numerator, denominator, rate, version, new_set, entry)
            return {
                "ok": True, "metric_id": row["id"], "insight_id": insight["id"],
                "分子": numerator, "分母": denominator, "高分率": rate, "授权版本": version,
                "message": f"{project}的{scope}统计已交付，并生成待复核分群洞察{insight['洞察编号']}",
            }

    # ── 共享撤销与所有方核对 ──
    def revoke_sharing(self, project: str, group: str | None = None) -> dict[str, Any]:
        """撤销共享：授权置为已撤销，受影响的已交付洞察对该项目仅展示撤权状态。"""
        samples = self._sample_map()
        revoked: list[str] = []
        for grant in self._grants(project):
            if grant.get("授权级别") == GRANT_PRIVATE:
                continue  # 私有样本从未共享，无需撤销
            sample = samples.get(grant.get("样本编号"), {})
            if group and sample.get("分群") != group:
                continue
            grant["状态"] = "已撤销"
            revoked.append(str(grant.get("样本编号")))
        affected: list[int] = []
        for insight in store.internal_rows("group_insight"):
            if insight.get("交付项目") != project:
                continue
            if set(insight.get("样本清单", [])) & set(revoked):
                insight["撤权标记"] = True
                affected.append(int(insight["id"]))
        return {
            "ok": True, "project": project, "revoked_samples": revoked,
            "affected_insights": affected,
            "message": f"{project}的{len(revoked)}条样本共享已撤销",
        }

    def run_owner_audit(self, owner: str, project: str, scope: str = "合计") -> dict[str, Any]:
        """数据所有方核对：按项目当前有效授权重跑统计，确认被撤/私有样本已剔除。"""
        samples = self._sample_map()
        owned = {sid for sid, s in samples.items() if s.get("样本归属") == owner}
        if not owned:
            return {"ok": False, "message": f"{owner}名下没有预测样本"}
        allowed = self._allowed_samples(project, scope)
        allowed_ids = {s["样本编号"] for s in allowed}
        numerator = sum(1 for s in allowed if s.get("分数状态") == "高分")
        denominator = len(allowed)
        excluded = sorted(owned - allowed_ids)
        rate = f"{numerator / denominator:.4f}" if denominator else "-"
        row = self._record_metric(
            project, scope, numerator, denominator, rate, "已核对", "所有方核对",
            version=self._auth_version(allowed))
        return {
            "ok": True, "metric_id": row["id"], "分子": numerator, "分母": denominator,
            "高分率": rate, "excluded_samples": excluded,
            "message": f"所有方核对完成：{project}当前可统计{denominator}条样本，已剔除{len(excluded)}条",
        }

    # ── 内部落库 ──
    def _auth_version(self, allowed: list[dict[str, Any]]) -> str:
        versions = sorted({str(s.get("授权版本")) for s in allowed if s.get("授权版本")})
        if not versions:
            return "-"
        return versions[0] if len(versions) == 1 else ",".join(versions)

    def _record_metric(
        self, project: str, scope: str, numerator: Any, denominator: Any, rate: str,
        status: str, entry: str, version: str = "-", reason: str = "",
    ) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        row = {
            "id": next_id,
            "status": status,
            "pending": status in ("草稿", "待复核"),
            "abnormal": status == "已拒绝",
            "指标编号": f"MET-{next_id:04d}",
            "项目名称": project,
            "统计范围": scope,
            "分子": numerator,
            "分母": denominator,
            "高分率": rate,
            "授权版本": version,
            "指标状态": status,
            "入口": entry,
            "拒绝原因": reason,
        }
        rows.append(row)
        return row

    def _record_delivery(
        self, project: str, row: dict[str, Any], sample_set: set[str], version: str,
    ) -> None:
        deliveries = store.internal_rows("metric_delivery")
        deliveries.append({
            "id": len(deliveries) + 1,
            "项目": project,
            "指标ID": row["id"],
            "指标编号": row["指标编号"],
            "样本集": sorted(sample_set),
            "授权版本": version,
        })

    def _create_insight(
        self, project: str, row: dict[str, Any], scope: str, numerator: int,
        denominator: int, rate: str, version: str, sample_set: set[str], entry: str,
    ) -> dict[str, Any]:
        insights = store.internal_rows("group_insight")
        next_id = max((int(r.get("id", 0)) for r in insights), default=0) + 1
        samples = self._sample_map()
        owners = sorted({
            str(samples[sid].get("样本归属")) for sid in sample_set if sid in samples
        })
        insight = {
            "id": next_id,
            "洞察编号": f"INS-{next_id:04d}",
            "指标ID": row["id"],
            "交付项目": project,
            "数据所有方": owners[0] if owners else "数据所有方",
            "统计范围": scope,
            "分子": numerator,
            "分母": denominator,
            "高分率": rate,
            "授权版本": version,
            "样本清单": sorted(sample_set),
            "入口": entry,
            "status": "待复核",
            "撤权标记": False,
            "创建时间": "2026-11-22",
        }
        insights.append(insight)
        return insight
