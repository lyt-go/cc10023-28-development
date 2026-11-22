"""挖掘结果管理领域服务：分析结果、洞察发现与关键发现的管理。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MiningResultService(BaseModuleService):
    MODULE = "mining_result"
    ENTITY = "挖掘结果"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["结果编号", "分析主题", "算法类型", "洞察摘要", "关键分群", "置信度", "结果文件", "结果版本"]
    REQUIRED_FIELDS = ["结果编号", "分析主题", "算法类型"]
    STATUS_ORDER = ["草稿", "审核中", "已采纳", "已搁置"]
    ACTION_RULES = {"提交审核": "审核中", "确认采纳": "已采纳", "标记搁置": "已搁置"}
    NEGATIVE_ACTIONS = []

    # ── 链路4: 预测 → 挖掘结果 ──
    def export_insights_to_labels(self, result_id: int) -> dict[str, Any]:
        """将挖掘结果的关键发现导出到业务场景标注模块，形成可复用的场景规则。"""
        result = store.find(self.MODULE, result_id)
        if result is None:
            return {"ok": False}
        label_rows = store.rows("scenario_label")
        next_id = max((int(r.get("id", 0)) for r in label_rows), default=0) + 1
        result["status"] = "已采纳"
        result["pending"] = False
        label_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "标注编号": f"LABEL-{next_id:04d}",
            "场景名称": f"挖掘场景_{result.get('分析主题', 'unknown')}",
            "关联特征": result.get("关键分群", ""),
            "业务规则": result.get("洞察摘要", ""),
            "标注时间": "2026-11-18",
            "标注人": "system_auto",
            "标注状态": "草稿",
            "场景版本": "v1.0.0",
        })
        return {"ok": True, "result_id": result_id, "label_id": next_id}

    def aggregate_cluster_insights(self, result_id: int) -> dict[str, Any]:
        """聚合挖掘结果中关键分群的洞察数据并关联到聚类分析配置。"""
        result = store.find(self.MODULE, result_id)
        if result is None:
            return {"ok": False}
        cluster_rows = store.rows("cluster_config")
        related_clusters = [
            r.get("id") for r in cluster_rows
            if result.get("关键分群", "") and any(g.strip() in str(r.get("特征集", ""))
            for g in result.get("关键分群", "").split(","))
        ]
        return {
            "ok": True,
            "result_id": result_id,
            "clusters": result.get("关键分群", "").split(","),
            "related_cluster_configs": related_clusters,
        }

    def convert_insight_to_feature_source(self, result_id: int) -> dict[str, Any]:
        """将采纳的挖掘洞察转换为新的特征数据源，丰富下游模型的特征输入。"""
        result = store.find(self.MODULE, result_id)
        if result is None:
            return {"ok": False}
        source_rows = store.rows("feature_source")
        next_id = max((int(r.get("id", 0)) for r in source_rows), default=0) + 1
        source_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "源编号": f"SRC-R{result_id:04d}",
            "源名称": f"洞察衍生特征_{result.get('分析主题', '')}",
            "数据表名": f"insight_derived_{result_id}",
            "连接配置": "hive_metastore",
            "字段清单": "insight_score,cluster_label,confidence",
            "采样比例": "0.5",
            "缓存策略": "memory_1h",
            "源版本": "v1.0.0",
        })
        return {"ok": True, "source_id": next_id, "result_id": result_id}

    def deduplicate_mining_results(self, result_ids: list[int]) -> dict[str, Any]:
        """对挖掘结果集进行去重处理，识别相似洞察并合并重复发现。"""
        topics = {}
        duplicates = []
        for rid in result_ids:
            r = store.find(self.MODULE, rid)
            if r is None:
                continue
            topic = r.get("分析主题", "")
            if topic in topics:
                duplicates.append({"id": rid, "similar_to": topics[topic]})
                r["status"] = "已搁置"
            else:
                topics[topic] = rid
        return {"ok": True, "unique_count": len(topics), "duplicate_count": len(duplicates),
                "duplicates": duplicates}

    # ── 项目分群指标: 挖掘详情入口与分群洞察 ──
    def generate_group_insight(self, project: str, scope: str) -> dict[str, Any]:
        """挖掘详情入口：用项目分群指标允许的分子分母计算高分率，生成待复核的分群洞察。"""
        from app.services import service_for
        metric = service_for("project_metric")
        if metric is None:
            return {"ok": False, "message": "项目分群指标模块未注册"}
        return metric.submit_metric_request(project, scope, entry="挖掘详情")

    def open_group_insight(self, insight_id: int, viewer: str) -> dict[str, Any]:
        """按查看方打开分群洞察。

        数据所有方始终可核对当时的分母和授权版本；被撤权项目重新打开原洞察时
        只能看到撤权状态，看不到任何分子分母。
        """
        insight: dict[str, Any] | None = None
        for row in store.internal_rows("group_insight"):
            if int(row.get("id", 0)) == insight_id:
                insight = row
                break
        if insight is None:
            return {"ok": False, "message": f"分群洞察 {insight_id} 不存在或已归档"}
        delivered = str(insight.get("交付项目", ""))
        still_valid = {
            g.get("样本编号") for g in store.internal_rows("sample_grant")
            if g.get("项目") == delivered and g.get("状态") == "有效"
        }
        revoked = bool(insight.get("撤权标记")) or any(
            sid not in still_valid for sid in insight.get("样本清单", [])
        )
        if viewer == insight.get("数据所有方"):
            return {"ok": True, "viewer": viewer, **insight, "撤权标记": revoked}
        if viewer == delivered and revoked:
            return {
                "ok": True, "viewer": viewer, "洞察编号": insight.get("洞察编号"),
                "status": "已撤权",
                "message": "共享已撤销，被撤权项目仅可查看撤权状态",
            }
        return {"ok": True, "viewer": viewer, **insight}

    def list_group_insights(self, project: str | None = None) -> dict[str, Any]:
        """列出分群洞察；指定项目时返回它作为交付方或数据所有方能看到的洞察。"""
        rows = [
            dict(row) for row in store.internal_rows("group_insight")
            if project is None
            or row.get("交付项目") == project
            or row.get("数据所有方") == project
        ]
        return {"ok": True, "insights": rows, "count": len(rows)}