"""质量评估空态与看板归属测试。

覆盖修复点：
1. 空样本批次不再伪造 100% 一致率/完备率，也不会被记成“已完成”；
2. 旧重复簇不进入本轮重复率，杜绝 200% 一类异常率；
3. 区分“无数据”和“采集异常”，两者都不产生可放行质量率；
4. 历史已确认（真实测量）轮次的统计范围完整保留；
5. 样本补报后在同一轮次重试，生成真实测量指标；
6. 看板与质量详情共用同一评估归属视图，放行判断口径一致。
"""
from __future__ import annotations

import datetime
import os
import sys
from typing import Any

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.mdm_dashboard import MdmDashboardService
from app.services.mdm_quality import (
    EVAL_COLLECTION_FAILED,
    EVAL_MEASURED,
    EVAL_NO_DATA,
    NA_TEXT,
    MdmQualityService,
)
from app.store import store


def _unique_model(tag: str) -> str:
    return f"质量修复模型-{tag}-{datetime.datetime.utcnow().strftime('%H%M%S%f')}"


def _add_cluster(model: str, size: int, created_at: str,
                 status: str = "待确认") -> None:
    rows = store.rows("dup_cluster")
    rows.append({
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "status": status,
        "pending": status != "已合并",
        "abnormal": False,
        "簇编号": f"DUP-修复-{len(rows) + 1}",
        "实体模型": model,
        "簇大小": str(size),
        "相似度均值": "0.92",
        "匹配规则": "MATC-0001",
        "创建时间": created_at,
        "处置状态": status,
        "处置批次": "",
    })


def _add_dashboard(model: str) -> int:
    rows = store.rows("mdm_dashboard")
    dashboard = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "status": "已发布",
        "pending": False,
        "abnormal": False,
        "看板编号": f"DB-{datetime.datetime.utcnow().strftime('%H%M%S%f')}",
        "看板名称": f"{model}治理看板",
        "监控实体": model,
        "展示指标": "完备率,重复率,一致率",
        "刷新频率": "每日",
        "数据范围": "全量",
        "分享链接": "",
        "看板版本": "v1",
    }
    rows.append(dashboard)
    return dashboard["id"]


class TestQualityEmptyState:
    """空态评估：缺数与达标必须明确分开。"""

    def test_empty_batch_does_not_fabricate_rates_or_completion(self):
        model = _unique_model("空批次")
        svc = MdmQualityService()

        stats = svc.evaluate_quality(model, [], ["name", "phone"])

        # 没有任何质量率被“算”出来
        assert stats["evaluation_state"] == EVAL_NO_DATA
        assert stats["release_allowed"] is False
        assert stats["total_records"] == 0
        assert stats["completeness_pct"] is None
        assert stats["duplication_pct"] is None
        assert stats["consistency_pct"] is None
        assert stats["match_precision_pct"] is None

        entry = svc.latest_round_entry(model)
        assert entry is not None
        # 缺数轮次保持“计算中”，不能被记成已完成
        assert entry["status"] == "计算中"
        assert entry["pending"] is True
        assert entry["measured"] is False
        # 列表与看板统一占位，而不是 100.00%
        assert entry["完备率"] == NA_TEXT
        assert entry["一致率"] == NA_TEXT
        assert entry["缺数原因"]

    def test_collection_failure_distinguished_from_no_data(self):
        model = _unique_model("采集异常")
        svc = MdmQualityService()

        stats = svc.evaluate_quality(
            model, [], ["name"], collection_ok=False,
        )

        assert stats["evaluation_state"] == EVAL_COLLECTION_FAILED
        assert stats["release_allowed"] is False
        entry = svc.latest_round_entry(model)
        assert entry["abnormal"] is True
        assert "采集异常" in entry["缺数原因"]

    def test_missing_round_cannot_be_confirmed_completed(self):
        model = _unique_model("禁止确认")
        svc = MdmQualityService()
        svc.evaluate_quality(model, [], ["name"])
        entry = svc.latest_round_entry(model)

        result, message = svc.run_action(entry["id"], "确认完成")

        assert result is None
        assert "不能确认完成" in message
        assert svc.latest_round_entry(model)["status"] == "计算中"

    def test_empty_consistency_not_100_percent(self):
        """旧实现对空样本直接返回 100.0 的一致率，新实现必须返回 None。"""
        svc = MdmQualityService()
        assert svc._compute_consistency([], ["name"]) is None
        # 单条记录也无法形成分布，同样不可评估
        assert svc._compute_consistency([{"name": "张三"}], ["name"]) is None


class TestDuplicationWindow:
    """旧重复簇不得污染本轮重复率。"""

    def test_old_clusters_excluded_from_current_round(self):
        model = _unique_model("旧簇窗口")
        svc = MdmQualityService()
        # 上一轮遗留的旧簇（20 个成员）+ 本轮新簇（4 个成员）
        _add_cluster(model, 20, "2026-10-01T00:00:00")
        _add_cluster(model, 4, "2026-11-21T08:00:00")
        records = [{"name": f"用户{i}", "phone": f"1380000000{i}"} for i in range(100)]

        stats = svc.evaluate_quality(
            model, records, ["name", "phone"],
            round_id="RND-窗口-1",
            cluster_window_start="2026-11-21T00:00:00",
        )

        # 只统计窗口内的 4 个成员：4/100 = 4%，而不是 24%，更不会是 200%
        assert stats["evaluation_state"] == EVAL_MEASURED
        assert stats["duplication_pct"] == 4.0
        assert svc.latest_round_entry(model)["重复率"] == "4.00%"

    def test_empty_batch_with_old_clusters_shows_no_rate_not_200_percent(self):
        """题目核心场景：新一轮只有旧簇、没有原始样本，重复率不得显示 200%。"""
        model = _unique_model("旧簇空样本")
        svc = MdmQualityService()
        _add_cluster(model, 20, "2026-10-01T00:00:00")

        stats = svc.evaluate_quality(
            model, [], ["name"],
            cluster_window_start="2026-11-21T00:00:00",
        )

        assert stats["evaluation_state"] == EVAL_NO_DATA
        assert stats["duplication_pct"] is None
        entry = svc.latest_round_entry(model)
        assert entry["重复率"] == NA_TEXT
        assert entry["status"] == "计算中"


class TestRetryAndHistory:
    """补报重试与历史统计范围保留。"""

    def test_full_lifecycle_retry_then_measured(self):
        model = _unique_model("完整链路")
        svc = MdmQualityService()
        round_id = "RND-生命周期-1"

        # 第 1 轮（此前已确认的历史轮次）：真实测量，保留其统计范围
        history_records = [
            {"name": "张三", "phone": "13800138000"},
            {"name": "", "phone": "13900139000"},
        ]
        history = svc.evaluate_quality(
            model, history_records, ["name", "phone"],
            round_id="RND-生命周期-0",
        )
        assert history["evaluation_state"] == EVAL_MEASURED
        assert history["completeness_pct"] == 50.0

        # 新一轮当天只有旧簇、没有原始样本
        _add_cluster(model, 20, "2026-10-01T00:00:00")
        missing = svc.evaluate_quality(
            model, [], ["name", "phone"],
            round_id=round_id,
            cluster_window_start="2026-11-21T00:00:00",
        )
        assert missing["evaluation_state"] == EVAL_NO_DATA
        assert missing["release_allowed"] is False

        # 历史轮次统计范围仍在
        measured_entry = svc.latest_measured_entry(model)
        assert measured_entry["评估轮次"] == "RND-生命周期-0"
        assert measured_entry["样本量"] == "2"
        assert measured_entry["完备率"] == "50.00%"

        # 样本补报，重试同一轮次（窗口内新增一个 4 成员簇）
        _add_cluster(model, 4, "2026-11-21T08:00:00")
        new_records = [
            {"name": f"用户{i}", "phone": f"1380000000{i}"} for i in range(100)
        ]
        retry = svc.retry_quality_evaluation(
            model, round_id, new_records, ["name", "phone"],
            cluster_window_start="2026-11-21T00:00:00",
        )

        assert retry["ok"] is True
        entry = retry["entry"]
        # 同一轮次原地更新，没有产生重复轮次行
        model_rounds = [
            r for r in store.rows("mdm_quality")
            if r.get("评估轮次") == round_id
        ]
        assert len(model_rounds) == 1
        assert entry["status"] == "已完成"
        assert entry["measured"] is True
        assert entry["评估状态"] == EVAL_MEASURED
        assert entry["样本量"] == "100"
        assert entry["重复率"] == "4.00%"
        assert entry["完备率"] == "100.00%"
        # 缺数原因在补报后清空，避免与测量结论并存
        assert entry["缺数原因"] == ""

        # 趋势只包含真实测量轮次，且历史轮次仍在
        trend = svc.query_quality_trend(model)
        round_ids = {row["评估轮次"] for row in trend}
        assert round_ids == {"RND-生命周期-0", "RND-生命周期-1"}

    def test_retry_with_empty_reports_keeps_missing_state(self):
        model = _unique_model("空补报")
        svc = MdmQualityService()
        round_id = "RND-空补报-1"
        svc.evaluate_quality(model, [], ["name"], round_id=round_id)

        retry = svc.retry_quality_evaluation(model, round_id, [], ["name"])

        assert retry["ok"] is False
        entry = svc.latest_round_entry(model)
        assert entry["评估状态"] == EVAL_NO_DATA
        assert entry["status"] == "计算中"

    def test_measured_round_cannot_be_retried(self):
        model = _unique_model("防重算")
        svc = MdmQualityService()
        round_id = "RND-防重算-1"
        svc.evaluate_quality(
            model, [{"name": "张三"}], ["name"], round_id=round_id,
        )

        retry = svc.retry_quality_evaluation(
            model, round_id, [{"name": "李四"}], ["name"],
        )

        assert retry["ok"] is False
        # 已确认轮次的统计范围不被覆盖
        entry = svc.latest_round_entry(model)
        assert entry["样本量"] == "1"


class TestDashboardAlignment:
    """看板与质量详情必须展示相同的评估归属。"""

    def test_dashboard_blocks_release_on_missing_round(self):
        model = _unique_model("看板缺数")
        svc = MdmQualityService()
        dashboard_svc = MdmDashboardService()
        dashboard_id = _add_dashboard(model)
        _add_cluster(model, 20, "2026-10-01T00:00:00")

        # 先有一轮已确认的历史测量
        svc.evaluate_quality(
            model, [{"name": "张三", "phone": "13800138000"}],
            ["name", "phone"], round_id="RND-看板-历史",
        )
        # 新一轮无样本、只有旧簇
        svc.evaluate_quality(
            model, [], ["name", "phone"],
            round_id="RND-看板-当天",
            cluster_window_start="2026-11-21T00:00:00",
        )

        result = dashboard_svc.generate_governance_dashboard(dashboard_id)
        assert result["ok"] is True
        panel = result["dashboard"]
        quality = panel["quality"]

        # 看板归属与质量详情视图完全一致
        detail_view = svc.quality_release_view(model)
        assert quality == detail_view
        assert quality["evaluation_state"] == EVAL_NO_DATA
        assert quality["release_allowed"] is False
        assert quality["round_id"] == "RND-看板-当天"

        # 看板上不出现伪造的百分比，也没有 200% 异常率
        assert panel["completeness"] == NA_TEXT
        assert panel["duplication"] == NA_TEXT
        assert panel["consistency"] == NA_TEXT
        assert panel["release_allowed"] is False
        assert "禁止按本轮放行" in panel["release_guidance"]

        # 历史已确认数据保留统计范围，仅供参考
        history = quality["latest_measured"]
        assert history["round_id"] == "RND-看板-历史"
        assert history["样本量"] == "1"
        assert history["rates"]["完备率"] == "100.00%"

    def test_dashboard_switches_to_measured_after_retry(self):
        model = _unique_model("看板补报")
        svc = MdmQualityService()
        dashboard_svc = MdmDashboardService()
        dashboard_id = _add_dashboard(model)
        round_id = "RND-看板补报-1"

        svc.evaluate_quality(
            model, [], ["name"], round_id=round_id,
            cluster_window_start="2026-11-21T00:00:00",
        )
        _add_cluster(model, 2, "2026-11-21T09:00:00")
        svc.retry_quality_evaluation(
            model, round_id,
            [{"name": f"用户{i}"} for i in range(50)],
            ["name"],
            cluster_window_start="2026-11-21T00:00:00",
        )

        result = dashboard_svc.generate_governance_dashboard(dashboard_id)
        panel = result["dashboard"]
        assert panel["quality"]["evaluation_state"] == EVAL_MEASURED
        assert panel["quality"]["release_allowed"] is True
        assert panel["duplication"] == "4.00%"
        assert panel["completeness"] == "100.00%"
        assert panel["release_allowed"] is True
        # 与质量详情同口径
        assert panel["quality"] == svc.quality_release_view(model)

    def test_global_summary_separates_missing_from_measured(self):
        model = _unique_model("全局摘要")
        svc = MdmQualityService()
        dashboard_svc = MdmDashboardService()

        svc.evaluate_quality(
            model, [{"name": "张三"}], ["name"], round_id="RND-摘要-1",
        )
        svc.evaluate_quality(model, [], ["name"], round_id="RND-摘要-2")
        svc.evaluate_quality(
            model, [], ["name"], round_id="RND-摘要-3", collection_ok=False,
        )

        global_quality = svc.aggregate_global_quality()
        assert global_quality["measured_stats"] >= 1
        assert global_quality["missing_data_stats"] >= 1
        assert global_quality["collection_failed_stats"] >= 1

        summary = dashboard_svc.aggregate_governance_summary()
        qs = summary["quality_stats"]
        assert "measured" in qs and "missing_data" in qs and "collection_failed" in qs
        assert qs["missing_data"] >= 1
        assert qs["collection_failed"] >= 1


def _run_all() -> None:
    """无 pytest 环境时的直跑入口。"""
    import inspect
    import traceback

    cases: list[tuple[str, Any]] = []
    for class_name, cls in sorted(globals().items()):
        if not class_name.startswith("Test"):
            continue
        instance = cls()
        for name, fn in inspect.getmembers(instance, predicate=inspect.ismethod):
            if name.startswith("test_"):
                cases.append((f"{class_name}::{name}", fn))

    passed = failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception:  # noqa: BLE001 - 直跑时打印完整失败堆栈
            print(f"  FAIL: {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"结果: {passed}/{len(cases)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 个质量评估测试失败"


if __name__ == "__main__":
    _run_all()
