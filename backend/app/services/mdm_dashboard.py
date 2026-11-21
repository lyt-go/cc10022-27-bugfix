"""看板配置领域服务：看板统计、实时指标展示与治理趋势分析。

质量口径与 ``MdmQualityService.quality_release_view`` 完全一致：
看板不自行解释百分号字符串，缺数轮次只提示“无数据/采集异常”，
过去已确认轮次仅作历史参考展示，避免旧重复簇产生的异常率被当成
本轮可放行结论。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.mdm_quality import (
    EVAL_COLLECTION_FAILED,
    EVAL_MEASURED,
    EVAL_NO_DATA,
    MdmQualityService,
)
from app.store import store


class MdmDashboardService(BaseModuleService):
    MODULE = "mdm_dashboard"
    ENTITY = "看板配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["看板编号", "看板名称", "监控实体", "展示指标", "刷新频率", "数据范围", "分享链接", "看板版本"]
    REQUIRED_FIELDS = ["看板编号", "看板名称", "监控实体"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "停用看板": "已停用"}
    NEGATIVE_ACTIONS = ["停用看板"]

    def __init__(self) -> None:
        self.quality_svc = MdmQualityService()

    def generate_governance_dashboard(self, dashboard_id: int) -> dict[str, Any]:
        """生成治理看板：汇总主数据质量、合并和同步维度的实时指标。

        读取看板配置的监控实体和展示指标，聚合来源系统接入数、
        黄金记录总量、重复簇库存和质量完备率等治理关键指标。
        质量部分直接复用质量统计的评估归属视图，保证看板与
        质量详情对“本轮能否放行”的判断完全一致。
        """
        dashboard = self.get_entry(dashboard_id)
        if not dashboard:
            return {"ok": False, "reason": "看板配置不存在"}
        entity = dashboard.get("监控实体", "")
        metrics = [m.strip() for m in dashboard.get("展示指标", "").split(",") if m.strip()]

        panel_data: dict[str, Any] = {
            "dashboard_name": dashboard.get("看板名称"),
            "generated_at": datetime.datetime.utcnow().isoformat(),
        }

        # 聚合来源系统接入统计
        sources = store.rows("source_system")
        panel_data["total_source_systems"] = len(sources)
        panel_data["active_systems"] = sum(1 for s in sources if s.get("status") == "已接入")

        # 聚合黄金记录统计
        golden = store.rows("golden_record")
        panel_data["total_golden_records"] = len(golden)
        panel_data["active_golden"] = sum(1 for g in golden if g.get("status") == "已生效")

        # 聚合重复簇库存（仅为待处置工作量，不等同于本轮重复率）
        clusters = store.rows("dup_cluster")
        if entity:
            clusters = [c for c in clusters if c.get("实体模型") == entity]
        panel_data["total_clusters"] = len(clusters)
        panel_data["pending_clusters"] = sum(1 for c in clusters if c.get("status") == "待确认")
        panel_data["merged_clusters"] = sum(1 for c in clusters if c.get("status") == "已合并")
        panel_data["cluster_note"] = "重复簇为历史库存统计，本轮重复率以质量统计的真实测量为准"

        # 聚合质量统计：与质量详情共用同一评估归属，缺数不产生放行率
        quality_view = self.quality_svc.quality_release_view(entity) if entity else None
        panel_data["quality"] = quality_view
        if quality_view is not None:
            # 兼容旧字段：只有本轮真实测量才给出百分比，否则统一“无数据”
            current_rates = quality_view.get("current_rates")
            panel_data["completeness"] = current_rates["完备率"] if current_rates else "无数据"
            panel_data["duplication"] = current_rates["重复率"] if current_rates else "无数据"
            panel_data["consistency"] = current_rates["一致率"] if current_rates else "无数据"
            panel_data["evaluation_state"] = quality_view["evaluation_state"]
            panel_data["release_allowed"] = quality_view["release_allowed"]
            panel_data["release_guidance"] = self._build_release_guidance(quality_view)
            panel_data["latest_measured_quality"] = quality_view.get("latest_measured")

        # 聚合同步统计
        sync_cfgs = store.rows("mdm_sync")
        panel_data["total_sync_configs"] = len(sync_cfgs)
        panel_data["active_syncs"] = sum(1 for s in sync_cfgs if s.get("status") == "同步中")

        panel_data["metrics"] = metrics
        return {"ok": True, "dashboard": panel_data}

    @staticmethod
    def _build_release_guidance(quality_view: dict[str, Any]) -> str:
        """把评估归属翻译成运营可直接判断的放行提示。"""
        state = quality_view["evaluation_state"]
        if state == EVAL_MEASURED:
            return "本轮已有真实测量结论，可结合质量率判断放行"
        if state == EVAL_COLLECTION_FAILED:
            return "本轮采集异常、缺少原始样本，暂无质量率，禁止按本轮放行，待补报后重试"
        if state == EVAL_NO_DATA:
            return "本轮没有收到原始样本，暂无质量率，禁止按本轮放行，历史结论仅供参考"
        return "本轮质量数据不可评估"

    def query_published_dashboards(self, entity_model: str | None = None) -> list[dict[str, Any]]:
        """查询已发布的治理看板配置。

        遍历看板配置存储，检索已发布状态的看板，
        按监控实体筛选并返回看板名称、展示指标和刷新频率。
        """
        rows = store.rows(self.MODULE)
        dashboards = [row for row in rows if row.get("status") == "已发布"]
        if entity_model:
            dashboards = [d for d in dashboards if d.get("监控实体") == entity_model]
        return dashboards

    def refresh_dashboard_data(self, dashboard_id: int) -> dict[str, Any]:
        """刷新看板数据：重新计算最新指标并更新看板展示。

        读取看板配置的刷新频率，触发指标重新计算流程，
        更新治理看板的实时数据并返回快照时间戳。
        """
        dashboard = self.get_entry(dashboard_id)
        if not dashboard:
            return {"ok": False, "reason": "看板不存在"}
        refreshed = self.generate_governance_dashboard(dashboard_id)
        if refreshed["ok"]:
            dashboard["last_refreshed"] = datetime.datetime.utcnow().isoformat()
            dashboard["refresh_count"] = str(int(dashboard.get("refresh_count", "0")) + 1)
        return refreshed

    def aggregate_governance_summary(self) -> dict[str, Any]:
        """聚合全局治理摘要：汇总所有看板的关键指标。

        读取所有看板配置和底层业务数据，计算全局治理总览，
        包含来源系统数、黄金记录数、重复簇库存和质量评估归属统计。
        """
        all_sources = store.rows("source_system")
        all_golden = store.rows("golden_record")
        all_clusters = store.rows("dup_cluster")
        all_quality = store.rows("mdm_quality")
        measured_q = [q for q in all_quality if q.get("measured")]
        no_data_q = [q for q in all_quality if q.get("评估状态") == EVAL_NO_DATA]
        failed_q = [q for q in all_quality if q.get("评估状态") == EVAL_COLLECTION_FAILED]
        latest_measured = (
            max(measured_q, key=lambda q: q.get("统计时间", "")) if measured_q else None
        )
        return {
            "source_systems": {"total": len(all_sources), "active": sum(1 for s in all_sources if s.get("status") == "已接入")},
            "golden_records": {"total": len(all_golden), "active": sum(1 for g in all_golden if g.get("status") == "已生效")},
            "duplicate_clusters": {"total": len(all_clusters), "pending": sum(1 for c in all_clusters if c.get("status") == "待确认")},
            "quality_stats": {
                "total": len(all_quality),
                "measured": len(measured_q),
                "missing_data": len(no_data_q),
                "collection_failed": len(failed_q),
                "latest_at": latest_measured.get("统计时间") if latest_measured else "N/A",
            },
        }

    def export_dashboard_report(self, dashboard_id: int) -> dict[str, Any]:
        """导出治理看板报告：将看板数据输出为结构化报告。

        读取看板配置并生成完整治理报告，包含指标数据、
        生成时间和监控实体信息，供外部系统消费。
        """
        result = self.generate_governance_dashboard(dashboard_id)
        if not result["ok"]:
            return result
        dashboard = self.get_entry(dashboard_id)
        result["dashboard"]["report_id"] = f"RPT-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        result["dashboard"]["entity"] = dashboard.get("监控实体") if dashboard else "N/A"
        return result
