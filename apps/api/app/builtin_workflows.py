"""内置工作流包定义（W3.4/W3.5，实施计划第六期）。

数学建模（CUMCM）与长文写作用**同一套内核**表达——Task/Run/Artifact/Handoff/Review
一个不改，换包只是换 definition JSON（schema §1 规则 3 的活的证明）：
- CUMCM：facts → model → code（重试）→ review → paper → compile → bundle（两个真实交付适配器）；
- 长文写作：outline → draft → critique → finalize（同一验收语法 + 同一物化路径）。

诚实边界：CUMCM 的**四问分支**仍是老轨 pack（`competition_pack/apply` 的四问 DAG），
本定义的 questions 是输入参数（进模板插值），不做 per-question 动态节点生成——
schema v1 没有动态节点，需要时走 additive 演进。另一处词表收窄（W3.4 对照核验回执
48bba40 ②2 记录在案）：老轨 comp-prob-analysis 产 `problem_analysis`，本定义收窄为
`problem_facts`（语义覆盖：事实+数据画像），老轨消费者按此对应。本模块只产定义与
幂等种子，应用仍走 workflow_service.start_workflow_run（生成的永远是**任务骨架**）。
"""

from __future__ import annotations

from typing import Any

from . import workflow_service

__all__ = ["cumcm_definition", "longform_definition", "ensure_builtin_workflows"]

# 角色预设对照（W3.4 对照核验回执 48bba40）：六个角色全部挂 roles/mm-*.md（平台有角色
# 指纹漂移检测，不挂预设的角色享受不到）；coder/compiler 补 shell.run——可复现计算与
# LaTeX 编译没有执行能力就是三次重试撞同一堵墙（对照清单③）。
_CUMCM_ROLES = [
    {"id": "analyst", "role_name": "赛题分析", "description": "题面事实、数据画像与问题拆解",
     "capability_requirements": ["files.read"], "prompt_overrides": {"system": "角色预设见 deploy/cloud-agent/roles/mm-analysis.md"}},
    {"id": "modeler", "role_name": "建模", "capability_requirements": ["files.read", "files.write"],
     "prompt_overrides": {"system": "角色预设见 deploy/cloud-agent/roles/mm-modeling.md"}},
    {"id": "coder", "role_name": "编程与实验", "capability_requirements": ["files.read", "files.write", "shell.run"],
     "prompt_overrides": {"system": "角色预设见 deploy/cloud-agent/roles/mm-coding.md"}},
    {"id": "reviewer", "role_name": "独立复核", "capability_requirements": ["files.read"],
     "prompt_overrides": {"system": "角色预设见 deploy/cloud-agent/roles/mm-critique.md"}},
    {"id": "paper_writer", "role_name": "论文写作", "capability_requirements": ["files.read", "files.write"],
     "prompt_overrides": {"system": "角色预设见 deploy/cloud-agent/roles/mm-paper-zh.md"}},
    {"id": "compiler", "role_name": "编译与交付", "capability_requirements": ["files.read", "shell.run"],
     "prompt_overrides": {"system": "角色预设见 deploy/cloud-agent/roles/mm-compile.md"}},
]


def cumcm_definition() -> dict[str, Any]:
    """CUMCM 主线（与 cumcm_importer 的骨架同阶段词表；老轨入口保留不动）。"""

    return {
        "schema_version": 1,
        "workflow": {
            "key": "cumcm-main",
            "name": "数学建模（CUMCM 主线）",
            "description": "题面事实 → 建模 → 编程实验 → 独立复核 → 论文 → 编译 → 提交包",
            "inputs": [
                {"name": "problem_code", "required": False, "hint": "赛题代码（如 C）"},
                {"name": "questions", "required": False, "hint": "要作答的问题（如 1,2,3,4）"},
            ],
        },
        "stages": [
            {"id": "problem_analysis", "title": "题面分析", "goal": "事实与数据画像"},
            {"id": "modeling", "title": "建模", "goal": "变量、假设、约束与目标"},
            {"id": "coding", "title": "编程实验", "goal": "可复现计算与结果表"},
            {"id": "review", "title": "独立复核", "goal": "假设、代码与结果的独立检查"},
            {"id": "paper", "title": "论文", "goal": "按模板成文"},
            {"id": "delivery", "title": "交付", "goal": "编译与提交包"},
        ],
        "role_bindings": _CUMCM_ROLES,
        "gate_policies": [
            {"id": "cumcm-model-gate", "spec": ["artifact:model_spec approved"], "on_block": "escalate_human"},
            {"id": "cumcm-review-gate", "spec": ["artifact:audit_report approved"], "on_block": "escalate_human"},
            {"id": "cumcm-paper-gate", "spec": ["artifact:paper_source approved"], "on_block": "escalate_human"},
        ],
        "delivery_adapters": [
            {"id": "cumcm-compile", "kind": "paper_compile", "config": {}},
            {"id": "cumcm-bundle", "kind": "submission_bundle", "config": {}},
        ],
        "nodes": [
            {
                "id": "problem_facts", "stage_id": "problem_analysis", "title": "整理题面与数据画像",
                "goal": "登记题面事实、附件清单与信息边界", "depends_on": [], "mode": "auto",
                "role_binding": "analyst",
                "prompt": {"task_template": "赛题 {{input.problem_code}}（问题 {{input.questions}}）：整理题面事实与数据画像"},
                "outputs": [{"name": "problem_facts", "artifact_type": "problem_facts"}],
            },
            {
                "id": "model", "stage_id": "modeling", "title": "问题拆解与模型建立",
                "goal": "转化为变量、假设、约束与目标", "depends_on": ["problem_facts"], "mode": "auto",
                "role_binding": "modeler",
                "inputs": [{"from_output": "problem_facts"}],
                "outputs": [{"name": "model_spec", "artifact_type": "model_spec"}],
                "gate_policy": "cumcm-model-gate",
            },
            {
                "id": "code", "stage_id": "coding", "title": "代码实现与实验",
                "goal": "可复现计算，登记参数、种子与结果", "depends_on": ["model"], "mode": "auto",
                "role_binding": "coder",
                "inputs": [{"from_output": "model_spec"}],
                "outputs": [{"name": "code", "artifact_type": "code"},
                            {"name": "result_table", "artifact_type": "result_table"}],
                "budget": {"max_attempts": 3},
                "on_fail": "retry",
                "retry_policy": {"backoff_seconds": 30},
            },
            {
                "id": "review", "stage_id": "review", "title": "独立复核与信息边界审计",
                "goal": "重复计量、未来信息与跨问一致性检查", "depends_on": ["code"], "mode": "auto",
                "role_binding": "reviewer",
                "inputs": [{"from_output": "result_table"}, {"from_output": "code"}],
                "outputs": [{"name": "audit_report", "artifact_type": "audit_report"}],
                "gate_policy": "cumcm-review-gate",
            },
            {
                "id": "paper", "stage_id": "paper", "title": "论文成文",
                "goal": "按模板装配论文源文件", "depends_on": ["review"], "mode": "auto",
                "role_binding": "paper_writer",
                "inputs": [{"from_output": "audit_report"}],
                "outputs": [{"name": "paper_source", "artifact_type": "paper_source"}],
                "gate_policy": "cumcm-paper-gate",
            },
            {
                "id": "compile", "stage_id": "delivery", "title": "论文编译",
                "goal": "编译 PDF 并登记为成果物", "depends_on": ["paper"], "mode": "auto",
                "role_binding": "compiler",
                "inputs": [{"from_output": "paper_source"}],
                "outputs": [{"name": "compiled_pdf", "artifact_type": "compiled_pdf"}],
                "delivery_adapter": "cumcm-compile",
                "human_intervention": "after",
            },
            {
                "id": "bundle", "stage_id": "delivery", "title": "提交包与交付清单",
                "goal": "装配 + 检查 + 校验清单（人工确认后交付）", "depends_on": ["compile"], "mode": "hybrid",
                "role_binding": "compiler",
                "inputs": [{"from_output": "compiled_pdf"}],
                "outputs": [{"name": "submission_bundle", "artifact_type": "submission_bundle"}],
                "delivery_adapter": "cumcm-bundle",
                "human_intervention": "approval_gate",
            },
        ],
    }


def longform_definition() -> dict[str, Any]:
    """长文写作最小包（C 的 WORKFLOW_SCHEMA §2 示例形态，落到同一内核）。"""

    return {
        "schema_version": 1,
        "workflow": {
            "key": "longform-writing",
            "name": "长文写作",
            "description": "大纲 → 初稿 → 复核 → 定稿",
            "inputs": [{"name": "topic", "required": True, "hint": "写作主题与受众"}],
        },
        "stages": [
            {"id": "outline", "title": "大纲", "goal": "确定结构与论点"},
            {"id": "draft", "title": "初稿", "goal": "按大纲成文"},
            {"id": "revise", "title": "复核", "goal": "独立复核结构与事实"},
            {"id": "final", "title": "定稿", "goal": "定稿与交付"},
        ],
        "role_bindings": [
            {"id": "writer", "role_name": "写作", "capability_requirements": ["files.read", "files.write"]},
            {"id": "critic", "role_name": "复核", "capability_requirements": ["files.read"]},
        ],
        "gate_policies": [
            {"id": "final-gate", "spec": ["artifact:document approved"], "on_block": "escalate_human"},
        ],
        "nodes": [
            {
                "id": "write_outline", "stage_id": "outline", "title": "写大纲",
                "goal": "产出结构化大纲", "depends_on": [], "mode": "auto", "role_binding": "writer",
                "prompt": {"task_template": "围绕 {{input.topic}} 写结构化大纲"},
                "outputs": [{"name": "outline_doc", "artifact_type": "document", "path": "outputs/outline.md"}],
            },
            {
                "id": "write_draft", "stage_id": "draft", "title": "写初稿",
                "goal": "按大纲成文", "depends_on": ["write_outline"], "mode": "auto", "role_binding": "writer",
                "inputs": [{"from_output": "outline_doc"}],
                "outputs": [{"name": "draft_doc", "artifact_type": "document", "path": "outputs/draft.md"}],
                "gate_policy": "final-gate",
            },
            {
                "id": "critique", "stage_id": "revise", "title": "独立复核",
                "goal": "复核结构与事实，给出报告", "depends_on": ["write_draft"], "mode": "auto", "role_binding": "critic",
                "inputs": [{"from_output": "draft_doc"}],
                "outputs": [{"name": "review_report", "artifact_type": "review_report"}],
            },
            {
                "id": "finalize", "stage_id": "final", "title": "定稿交付",
                "goal": "按复核意见定稿", "depends_on": ["critique"], "mode": "hybrid", "role_binding": "writer",
                "inputs": [{"from_output": "review_report"}],
                "outputs": [{"name": "final_doc", "artifact_type": "document", "path": "outputs/final.md"}],
                "human_intervention": "approval_gate",
            },
        ],
    }


def ensure_builtin_workflows(store: Any, organization_id: str, actor: str) -> dict[str, Any]:
    """幂等种子：两个内置包按 key 各建一次（已存在跳过）。返回 created/skipped 清单。"""

    workflow_service.ensure_schema(store)
    created: list[str] = []
    skipped: list[str] = []
    for builder in (cumcm_definition, longform_definition):
        definition = builder()
        key = str(definition["workflow"]["key"])
        existing = store.db.execute("SELECT 1 FROM workflows WHERE key = ?", (key,)).fetchone()
        if existing:
            skipped.append(key)
            continue
        workflow_service.create_workflow(store, organization_id, actor, definition)
        created.append(key)
    return {"created": created, "skipped": skipped}
