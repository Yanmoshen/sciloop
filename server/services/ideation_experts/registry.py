"""20 个固定专家的注册表、提示词元数据和依赖 DAG。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpertRole:
    id: str
    name: str
    phase: str
    description: str
    dependencies: tuple[str, ...] = ()
    critical: bool = False
    output_kind: str = "analysis"


SCORE_WEIGHTS: dict[str, int] = {
    "novelty": 15,
    "evidence_support": 15,
    "importance": 15,
    "method_feasibility": 15,
    "data_availability": 10,
    "resource_cost": 10,
    "verifiability": 10,
    "risk_reproducibility": 5,
    "impact": 5,
}


def _role(
    id: str,
    name: str,
    phase: str,
    description: str,
    dependencies: tuple[str, ...] = (),
    *,
    critical: bool = False,
    output_kind: str = "analysis",
) -> ExpertRole:
    return ExpertRole(id, name, phase, description, dependencies, critical, output_kind)


EXPERT_ROLES: tuple[ExpertRole, ...] = (
    _role("evidence_extractor", "论文证据提取专家", "context", "提取论文解析卡、总结和全文证据中的可信事实。", critical=True),
    _role("cross_paper_reviewer", "跨论文综述专家", "context", "比较选中论文的共同点、差异、方法演进和边界。", critical=True),
    _role("gap_finder", "研究空白发现专家", "context", "只基于证据发现尚未解决的研究问题。", ("evidence_extractor", "cross_paper_reviewer"), critical=True),
    _role("question_reframer", "研究问题重构专家", "context", "把研究空白改写为清晰、可验证的问题和约束。", ("gap_finder",), critical=True),
    _role("idea_seed_generator", "发散 Idea 生成专家", "generation", "为四个创新方向生成候选种子，不直接作最终决策。", ("question_reframer",), critical=True, output_kind="seeds"),
    _role("method_iteration", "方法迭代专家", "generation", "针对原论文缺陷生成 2 条方法迭代型 Idea。", ("idea_seed_generator",), output_kind="ideas"),
    _role("scenario_transfer", "场景迁移专家", "generation", "生成 2 条跨任务、领域或模态的场景迁移型 Idea。", ("idea_seed_generator",), output_kind="ideas"),
    _role("technology_fusion", "技术融合专家", "generation", "生成 2 条技术融合型 Idea。", ("idea_seed_generator",), output_kind="ideas"),
    _role("paradigm_extension", "范式拓展专家", "generation", "生成 2 条新研究框架或技术体系 Idea。", ("idea_seed_generator",), output_kind="ideas"),
    _role("novelty_checker", "新颖性与重复检查专家", "analysis", "检查候选是否重复、改写或缺少实质创新。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("method_feasibility", "方法可行性专家", "analysis", "分析技术路径、关键假设和实现难度。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("data_feasibility", "数据可行性专家", "analysis", "分析数据来源、规模、标注、质量和权限。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("resource_feasibility", "算力与资源专家", "analysis", "分析 GPU、软件、时间、人力和成本。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("experiment_designer", "实验设计专家", "analysis", "设计基线、对照、消融、变量和实验步骤。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("metric_falsifiability", "指标与可证伪性专家", "analysis", "判断是否能够客观验证、比较和证伪。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("risk_ethics_compliance", "风险伦理合规专家", "analysis", "检查安全、伦理、隐私、版权和合规风险。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("impact_value", "科学影响与应用价值专家", "analysis", "分析科学意义、应用价值和迁移潜力。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("adversarial_critic", "对抗式批评专家", "analysis", "站在审稿人和竞争者角度寻找致命缺陷。", ("method_iteration", "scenario_transfer", "technology_fusion", "paradigm_extension")),
    _role("independent_scorer", "独立评分与证据审计专家", "decision", "按固定权重评分并审计每个分数的证据。", ("novelty_checker", "method_feasibility", "data_feasibility", "resource_feasibility", "experiment_designer", "metric_falsifiability", "risk_ethics_compliance", "impact_value", "adversarial_critic"), critical=True, output_kind="scores"),
    _role("decision_ranker", "决策排序专家", "decision", "综合评分、分歧和硬门槛输出推荐排序。", ("independent_scorer",), critical=True, output_kind="ranking"),
)

ROLE_REGISTRY: dict[str, ExpertRole] = {role.id: role for role in EXPERT_ROLES}


def get_role(role_id: str) -> ExpertRole:
    try:
        return ROLE_REGISTRY[role_id]
    except KeyError as exc:
        raise KeyError(f"未知 Idea 专家角色: {role_id}") from exc


def topological_levels() -> tuple[tuple[str, ...], ...]:
    """返回稳定拓扑层；同层专家可并行但每个角色仍单独调用。"""
    remaining = set(ROLE_REGISTRY)
    levels: list[tuple[str, ...]] = []
    while remaining:
        ready = tuple(role.id for role in EXPERT_ROLES if role.id in remaining and not (set(role.dependencies) & remaining))
        if not ready:
            raise ValueError("Idea 专家依赖图存在环")
        levels.append(ready)
        remaining.difference_update(ready)
    return tuple(levels)


__all__ = ["ExpertRole", "EXPERT_ROLES", "ROLE_REGISTRY", "SCORE_WEIGHTS", "get_role", "topological_levels"]
