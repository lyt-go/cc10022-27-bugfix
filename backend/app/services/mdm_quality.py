"""质量统计领域服务：完备率、重复率、一致率与匹配精度计算。

评估归属（evaluation_state）分三种，运营据此区分“能否用于本轮放行”：

- 已测量：本轮收到原始样本，所有质量率均来自真实测量，可用于放行；
- 无数据：本轮未收到任何原始样本，不产生质量率，历史已确认数据仅作参考；
- 采集异常：采集链路报错导致缺数，同样不产生质量率，需修复后补报重试。

缺数轮次保持“计算中”而不是“已完成”，样本补报后通过
``retry_quality_evaluation`` 在同一轮次上重算，历史已测量轮次的
统计范围（样本量、统计区间）原样保留。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 评估归属：真实测量 / 没有数据 / 采集异常
EVAL_MEASURED = "已测量"
EVAL_NO_DATA = "无数据"
EVAL_COLLECTION_FAILED = "采集异常"

# 不可评估时在列表与看板统一展示的占位文案
NA_TEXT = "无数据"

NO_DATA_REASON = "本轮未收到原始样本，质量率不可评估，不能用于本次放行"
COLLECTION_FAILED_REASON = "采集异常：本轮原始样本采集失败，未取得可评估数据"


class MdmQualityService(BaseModuleService):
    MODULE = "mdm_quality"
    ENTITY = "质量统计"
    OPERATION_TERM = "计算"
    LIST_FIELDS = [
        "统计编号", "评估轮次", "实体模型", "评估状态", "样本量",
        "完备率", "重复率", "一致率", "匹配精度",
        "统计区间", "统计时间", "缺数原因",
    ]
    REQUIRED_FIELDS = ["统计编号", "实体模型", "完备率"]
    STATUS_ORDER = ["草稿", "计算中", "已完成", "已归档"]
    ACTION_RULES = {"发起统计": "计算中", "确认完成": "已完成", "归档统计": "已归档"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """状态流转防护：缺数轮次不允许确认完成，避免被当成放行结论。"""
        entry = self.get_entry(entry_id)
        if action == "确认完成" and entry is not None and not entry.get("measured"):
            return None, (
                f"轮次 {entry.get('评估轮次')} 未取得可评估样本"
                f"（{entry.get('评估状态')}），不能确认完成并用于本次放行"
            )
        return super().run_action(entry_id, action)

    # ── 对外主流程 ──────────────────────────────────────────────

    def evaluate_quality(
        self,
        entity_model: str,
        records: list[dict[str, Any]],
        required_fields: list[str],
        *,
        round_id: str | None = None,
        collection_ok: bool = True,
        cluster_window_start: str | None = None,
    ) -> dict[str, Any]:
        """评估一轮主数据质量。

        ``records`` 为本轮实际采集到的原始样本；为空时不再伪造百分率，
        而是按 ``collection_ok`` 区分“无数据”和“采集异常”。
        ``cluster_window_start`` 给出本轮统计区间起点，早于该时间的
        旧重复簇不计入本轮重复率，避免历史簇污染本轮指标。
        """
        now = datetime.datetime.utcnow()
        round_id = round_id or f"RND-{now.strftime('%Y%m%d%H%M%S')}"
        total = len(records)

        if total == 0:
            # 空态评估：缺数与达标必须明确分开，绝不产出可放行的质量率
            if collection_ok:
                evaluation_state, reason, abnormal = EVAL_NO_DATA, NO_DATA_REASON, False
            else:
                evaluation_state, reason, abnormal = (
                    EVAL_COLLECTION_FAILED, COLLECTION_FAILED_REASON, True,
                )
            stats = self._empty_stats(entity_model, round_id, now,
                                      evaluation_state, reason)
            self._store_quality_stats(
                entity_model, stats,
                status="计算中", pending=True, abnormal=abnormal,
                round_id=round_id, window_start=cluster_window_start,
            )
            return stats

        # 真实测量：只在拿到样本时才计算质量率
        completeness = self._compute_completeness(records, required_fields)
        duplication = self._compute_duplication(
            entity_model, total, cluster_window_start,
        )
        consistency = self._compute_consistency(records, required_fields)
        match_precision = self._compute_match_precision(entity_model, total)

        stats = {
            "entity_model": entity_model,
            "round_id": round_id,
            "evaluation_state": EVAL_MEASURED,
            "release_allowed": True,
            "total_records": total,
            "completeness_pct": completeness,
            "duplication_pct": duplication,
            "consistency_pct": consistency,
            "match_precision_pct": match_precision,
            "computed_at": now.isoformat(),
            "reason": "",
        }
        self._store_quality_stats(
            entity_model, stats,
            status="已完成", pending=False, abnormal=False,
            round_id=round_id, window_start=cluster_window_start,
        )
        return stats

    def compute_quality_metrics(
        self,
        entity_model: str,
        records: list[dict[str, Any]],
        required_fields: list[str],
    ) -> dict[str, Any]:
        """兼容旧调用：等价于不带轮次/区间参数的一轮质量评估。"""
        return self.evaluate_quality(entity_model, records, required_fields)

    def retry_quality_evaluation(
        self,
        entity_model: str,
        round_id: str,
        records: list[dict[str, Any]],
        required_fields: list[str],
        *,
        collection_ok: bool = True,
        cluster_window_start: str | None = None,
    ) -> dict[str, Any]:
        """样本补报后重试指定轮次的质量评估。

        只允许重试缺数轮次，并在原轮次记录上原地更新，不新增轮次；
        已经真实测量的轮次保持不变，保护已确认的统计范围。
        """
        existing = self._find_round_entry(entity_model, round_id)
        if existing is None:
            return {"ok": False, "reason": f"轮次 {round_id} 不存在，无法重试"}
        if existing.get("measured"):
            return {
                "ok": False,
                "reason": "该轮次已完成真实测量并确认，统计范围保留，不能重复评估",
                "entry": existing,
            }

        if not records:
            # 补报仍为空：刷新缺数归属（无数据/采集异常），继续等待样本
            stats = self.evaluate_quality(
                entity_model, [], required_fields,
                round_id=round_id, collection_ok=collection_ok,
                cluster_window_start=cluster_window_start,
            )
            return {
                "ok": False,
                "message": "补报样本仍为空，轮次保持缺数状态，暂不可放行",
                "stats": stats,
                "entry": self._find_round_entry(entity_model, round_id),
            }

        stats = self.evaluate_quality(
            entity_model, records, required_fields,
            round_id=round_id, collection_ok=collection_ok,
            cluster_window_start=cluster_window_start,
        )
        return {
            "ok": True,
            "message": "补报样本已参与评估，本轮质量率已生成，可用于放行",
            "stats": stats,
            "entry": self._find_round_entry(entity_model, round_id),
        }

    # ── 指标计算：分母为 0 时一律返回 None，而不是伪造 100% ──────

    def _compute_completeness(
        self, records: list[dict[str, Any]], required_fields: list[str],
    ) -> float:
        """完备率：必填字段全部非空的记录占比。调用方保证 records 非空。"""
        complete = sum(
            1 for r in records
            if all(str(r.get(f, "")).strip() for f in required_fields)
        )
        return round(complete / len(records) * 100, 2)

    def _compute_duplication(
        self, entity_model: str, total: int,
        cluster_window_start: str | None,
    ) -> float:
        """重复率：本轮统计区间内重复簇成员数 / 本轮样本量。

        旧簇（创建时间早于本轮区间起点）不参与计算，从根上消除
        “旧簇 ÷ 本轮小样本”造成的 200% 一类异常率。
        """
        clusters = [
            c for c in store.rows("dup_cluster")
            if c.get("实体模型") == entity_model
            and self._in_window(c.get("创建时间", ""), cluster_window_start)
        ]
        dup_count = sum(self._safe_int(c.get("簇大小")) for c in clusters)
        return round(dup_count / total * 100, 2)

    def _compute_consistency(
        self, records: list[dict[str, Any]], fields: list[str],
    ) -> float | None:
        """一致率：同字段取值的分布集中度。

        没有任何字段具备两个及以上有效取值时，一致性无法被测量，
        返回 None（旧实现会在空样本/单样本时伪造 100%）。
        """
        total_checks = 0
        consistent = 0
        for field in fields:
            values = [
                str(r.get(field, "")) for r in records
                if str(r.get(field, "")).strip()
            ]
            if len(values) < 2:
                continue
            most_common = max(set(values), key=values.count)
            total_checks += len(values)
            consistent += values.count(most_common)
        if total_checks == 0:
            return None
        return round(consistent / total_checks * 100, 2)

    def _compute_match_precision(self, entity_model: str, total: int) -> float:
        """匹配精度：基于本轮实体模型的匹配任务输出。"""
        model_jobs = [
            j for j in store.rows("auto_match_job")
            if j.get("实体模型") == entity_model
        ]
        total_clusters = sum(
            self._safe_int(j.get("输出簇数")) for j in model_jobs
        )
        return round(max(100 - total_clusters / total * 100, 0), 2)

    # ── 存储与查询 ──────────────────────────────────────────────

    def _empty_stats(
        self, entity_model: str, round_id: str,
        now: datetime.datetime,
        evaluation_state: str, reason: str,
    ) -> dict[str, Any]:
        return {
            "entity_model": entity_model,
            "round_id": round_id,
            "evaluation_state": evaluation_state,
            "release_allowed": False,
            "total_records": 0,
            "completeness_pct": None,
            "duplication_pct": None,
            "consistency_pct": None,
            "match_precision_pct": None,
            "computed_at": now.isoformat(),
            "reason": reason,
        }

    def _store_quality_stats(
        self,
        entity_model: str,
        stats: dict[str, Any],
        *,
        status: str,
        pending: bool,
        abnormal: bool,
        round_id: str,
        window_start: str | None,
    ) -> dict[str, Any]:
        """落盘质量统计；同一轮次重试时原地更新，保留轮次归属。"""
        rows = store.rows(self.MODULE)
        existing = self._find_round_entry(entity_model, round_id)
        window_label = f"{window_start} 至今" if window_start else "全量"

        def rate_text(value: float | None) -> str:
            return f"{value:.2f}%" if value is not None else NA_TEXT

        measured = stats["evaluation_state"] == EVAL_MEASURED
        entry_data = {
            "统计编号": f"QLTY-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            "评估轮次": round_id,
            "实体模型": entity_model,
            "评估状态": stats["evaluation_state"],
            "样本量": str(stats["total_records"]),
            "完备率": rate_text(stats["completeness_pct"]),
            "重复率": rate_text(stats["duplication_pct"]),
            "一致率": rate_text(stats["consistency_pct"]),
            "匹配精度": rate_text(stats["match_precision_pct"]),
            "统计时间": stats["computed_at"],
            "统计区间": window_label,
            "缺数原因": stats.get("reason", ""),
        }

        if existing is not None:
            # 重试：原轮次记录原地更新，已确认的历史轮次不会走到这里
            existing.update(entry_data)
            existing["status"] = status
            existing["pending"] = pending
            existing["abnormal"] = abnormal
            existing["measured"] = measured
            existing["release_allowed"] = stats["release_allowed"]
            existing["metrics"] = self._raw_metrics(stats)
            return existing

        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            **entry_data,
            "status": status,
            "pending": pending,
            "abnormal": abnormal,
            "measured": measured,
            "release_allowed": stats["release_allowed"],
            "metrics": self._raw_metrics(stats),
        }
        rows.append(entry)
        return entry

    @staticmethod
    def _raw_metrics(stats: dict[str, Any]) -> dict[str, Any]:
        return {
            "completeness_pct": stats["completeness_pct"],
            "duplication_pct": stats["duplication_pct"],
            "consistency_pct": stats["consistency_pct"],
            "match_precision_pct": stats["match_precision_pct"],
        }

    def _find_round_entry(
        self, entity_model: str, round_id: str,
    ) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                row.get("实体模型") == entity_model
                and row.get("评估轮次") == round_id
                and row.get("status") != "已归档"
            ):
                return row
        return None

    def latest_round_entry(self, entity_model: str) -> dict[str, Any] | None:
        """最新一轮评估（含缺数轮次），看板据此判断本轮能否放行。"""
        candidates = [
            r for r in store.rows(self.MODULE)
            if r.get("实体模型") == entity_model and r.get("评估轮次")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.get("统计时间", ""))

    def latest_measured_entry(self, entity_model: str) -> dict[str, Any] | None:
        """最近一次真实测量，缺数轮次不覆盖它，统计范围随记录保留。"""
        candidates = [
            r for r in store.rows(self.MODULE)
            if r.get("实体模型") == entity_model and r.get("measured")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.get("统计时间", ""))

    def quality_release_view(self, entity_model: str) -> dict[str, Any]:
        """看板与质量详情共用的评估归属视图，保证两边口径一致。"""
        current = self.latest_round_entry(entity_model)
        measured = self.latest_measured_entry(entity_model)
        if current is None:
            return {
                "entity_model": entity_model,
                "round_id": None,
                "evaluation_state": EVAL_NO_DATA,
                "release_allowed": False,
                "sample_size": 0,
                "reason": "该实体模型尚无任何质量评估轮次",
                "current_rates": None,
                "latest_measured": None,
            }

        history = None
        if measured is not None:
            history = {
                "round_id": measured.get("评估轮次"),
                "统计时间": measured.get("统计时间"),
                "样本量": measured.get("样本量"),
                "统计区间": measured.get("统计区间"),
                "rates": {
                    "完备率": measured.get("完备率"),
                    "重复率": measured.get("重复率"),
                    "一致率": measured.get("一致率"),
                    "匹配精度": measured.get("匹配精度"),
                },
            }

        return {
            "entity_model": entity_model,
            "round_id": current.get("评估轮次"),
            "evaluation_state": current.get("评估状态"),
            "release_allowed": bool(current.get("release_allowed")),
            "sample_size": self._safe_int(current.get("样本量")),
            "reason": current.get("缺数原因", ""),
            "current_rates": (
                {
                    "完备率": current.get("完备率"),
                    "重复率": current.get("重复率"),
                    "一致率": current.get("一致率"),
                    "匹配精度": current.get("匹配精度"),
                }
                if current.get("measured") else None
            ),
            "latest_measured": history,
        }

    def query_quality_trend(
        self, entity_model: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """查询质量趋势：只返回真实测量轮次，缺数轮次不进入趋势。"""
        rows = store.rows(self.MODULE)
        stats = [
            row for row in rows
            if row.get("实体模型") == entity_model
            and row.get("status") == "已完成"
            and row.get("measured") is True
        ]
        stats.sort(key=lambda r: r.get("统计时间", ""), reverse=True)
        return stats[:limit]

    def aggregate_global_quality(self) -> dict[str, Any]:
        """聚合全局质量指标：区分真实测量与采集缺数。"""
        rows = store.rows(self.MODULE)
        completed = [r for r in rows if r.get("status") == "已完成"]
        measured_rows = [r for r in rows if r.get("measured")]
        if not completed and not measured_rows:
            return {
                "total_quality_stats": 0,
                "measured_stats": 0,
                "missing_data_stats": 0,
                "collection_failed_stats": 0,
                "models": [],
            }
        models: dict[str, dict[str, Any]] = {}
        for stat in rows:
            model = stat.get("实体模型", "")
            bucket = models.setdefault(model, {
                "entity_model": model,
                "latest": None,
                "latest_measured": None,
                "stat_count": 0,
                "measured_count": 0,
                "missing_count": 0,
                "collection_failed_count": 0,
            })
            bucket["stat_count"] += 1
            state = stat.get("评估状态")
            if stat.get("measured"):
                bucket["measured_count"] += 1
            elif state == EVAL_COLLECTION_FAILED:
                bucket["collection_failed_count"] += 1
            elif state == EVAL_NO_DATA:
                bucket["missing_count"] += 1
            if not bucket["latest"] or stat.get("统计时间", "") > bucket["latest"].get("统计时间", ""):
                bucket["latest"] = stat
            if stat.get("measured") and (
                not bucket["latest_measured"]
                or stat.get("统计时间", "") > bucket["latest_measured"].get("统计时间", "")
            ):
                bucket["latest_measured"] = stat
        return {
            "total_quality_stats": len(completed),
            "measured_stats": len(measured_rows),
            "missing_data_stats": sum(
                1 for r in rows if r.get("评估状态") == EVAL_NO_DATA
            ),
            "collection_failed_stats": sum(
                1 for r in rows if r.get("评估状态") == EVAL_COLLECTION_FAILED
            ),
            "models": list(models.values()),
        }

    def archive_old_stats(self, before_date: str) -> int:
        """归档指定日期之前的旧质量统计记录。"""
        rows = store.rows(self.MODULE)
        archived = 0
        for row in rows:
            if row.get("status") == "已完成" and row.get("统计时间", "") < before_date:
                row["status"] = "已归档"
                archived += 1
        return archived

    # ── 工具 ────────────────────────────────────────────────────

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _in_window(created_at: str, window_start: str | None) -> bool:
        if not window_start:
            return True
        return str(created_at) >= window_start
