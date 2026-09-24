"""多 Agent 文档撰写演示项目（W-8）：用**真实的 store 调用**串一个完整协作故事。

不伪造数据——每一步（领取、进度、运行、成果、复核、交接）都走 Agent 真实使用的同一条代码路径，
所以聊天流里的卡片、成果空间里的成果物、时间线上的事件全部是真事件。

故事：队长安排两个 Agent（写手 + 复核）协作写一份《synapforge 平台使用指南》。
幂等：项目名已存在即跳过，可重复执行。

用法：
  本地：  PYTHONPATH="apps/api;." python scripts/deploy/_demo_doc_writing.py
  服务器：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_demo_doc_writing.py
"""
import sys

if len(sys.argv) > 1 and sys.argv[1].endswith(".db"):
    DB = sys.argv[1]
else:
    DB = "apps/api/data/platform.db"

sys.path.insert(0, "apps/api")
from uuid import UUID, uuid4

from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    ArtifactCreate,
    EvidenceCreate,
    HandoffCreate,
    ProjectCreate,
    ReviewCreate,
    ReviewerKind,
    RunComplete,
    RunCreate,
    TaskClaimRequest,
    TaskCreate,
    TaskProgressRequest,
    TaskResultSubmit,
)
from app.store import Store

DEMO_NAME = "演示 · 多 Agent 文档撰写"

DRAFT_OUTLINE = """# synapforge 平台使用指南 · 提纲

- 一、项目工作区：群聊、成员概览、成果空间
- 二、任务：派单、认领、执行方式
- 三、Agent：接入、能力、自动领取
- 四、成果与复核：三层版本、门禁与交付
"""

DRAFT_DOC = """# synapforge 平台使用指南（初稿）

## 一、项目工作区
进入项目后，中间是群聊：人在群里说话，Agent 在同一个群里汇报进度与产出。
右侧常驻成员概览：谁在线、谁的 Agent 正在跑什么。

## 二、任务
任务默认由队长派单（「任务」Tab 里勾选后批量派给成员）；
也可以在聊天输入框直接敲 /task 任务标题 快速建一条。
每条任务要有"执行方式"（Codex 或一条命令），Agent 才会领走。

## 三、Agent
在「设备与接入」配对一台机器，它就会自动领取任务并回报结果。

## 四、成果与复核
产出进「成果空间」；复核通过才算交付，门禁与风险都记在那里。
"""

REVIEW_NOTES = """# 复核意见

1. 第二部分"任务默认由队长派单"——表述正确，可补一句：hybrid 模式下成员也能认领无人任务。
2. 聊天区 /task 建任务的入口位置描述需修正：在输入框左侧的提示里，不在任务 Tab。
3. 建议补一段"成果物在群聊里可以就地查看"，方便第一次用的人。
"""

FINAL_DOC = """# synapforge 平台使用指南（终稿）

## 一、项目工作区
中间是群聊：人在群里说话，Agent 在同一个群里汇报进度与产出，全程可追溯。
右侧常驻成员概览：谁在线、谁的 Agent 正在跑什么。下方是成果空间入口。

## 二、任务
任务默认由队长派单：「任务」Tab 里勾选后批量派给成员；hybrid 模式下成员也能认领还没有负责人的任务。
也可以直接在聊天输入框敲 /task 任务标题 快速建一条（回车即创建，不用切 Tab）。

## 三、Agent
在「设备与接入」配对一台机器，Agent 会自动领取任务、执行并回报；
产出会以卡片形式实时出现在群聊里，点「就地查看」就能读内容。

## 四、成果与复核
产出进「成果空间」：成果物、文档三层版本、交接单、门禁与复核都在那里。
复核通过才算交付；门禁未通过的项目会一直挂在概览上提醒。"""

store = Store(DB)

existing = store.db.execute("SELECT id FROM projects WHERE name = ?", (DEMO_NAME,)).fetchone()
if existing:
    print(f"演示项目已存在（{DEMO_NAME}），跳过。")
    store.close()
    raise SystemExit(0)

# 归属：找首个在职管理员（没有就退回首个在职成员；再没有就用 member-001）
owner = store.db.execute(
    "SELECT id, display_name FROM human_members WHERE status = 'active' ORDER BY is_admin DESC, created_at LIMIT 1"
).fetchone()
if not owner:
    owner = store.db.execute("SELECT id, display_name FROM human_members LIMIT 1").fetchone()
leader_id = owner["id"]
print("演示归属:", owner["display_name"])

key = lambda tag: f"demo-{tag}-{uuid4().hex[:8]}"


# ---- 1. 建项目与 Agent ----
project = store.create_project(
    ProjectCreate(
        name=DEMO_NAME,
        competition_pack="cumcm-2026",
        description="演示两个 Agent 怎么协作写一份文档：写手出稿、复核把关、队长验收。",
        goal="产出一份《synapforge 平台使用指南》，全程在群聊里可追溯",
        target_member_count=3,
        task_mode="manual",
        created_by=leader_id,
    )
)
print("项目:", project.id)

for agent_id, display_name in [("agent-demo-writer", "写手 Agent"), ("agent-demo-checker", "复核 Agent")]:
    store.register_agent(
        AgentRegister(
            agent_id=agent_id,
            display_name=display_name,
            owner_member_id=leader_id,
            model_provider="openai",
            model_name="demo",
            supported_tools=["write", "review", "markdown"],
        )
    )
    store.grant_agent_project(
        AgentProjectGrant(
            agent_id=agent_id,
            project_id=project.id,
            capabilities=["task.claim", "task.progress", "task.result", "run.create", "run.complete", "artifact.write"],
            granted_by=leader_id,
        )
    )
print("Agent: 写手 / 复核 已登记并授权")

# ---- 2. 任务（带依赖的四步流程） ----
t_outline = store.create_task(project.id, TaskCreate(title="收集资料并拟定指南提纲", description="整理平台功能清单与读者关心的十件事，产出提纲。", stage="problem_analysis"))
t_draft = store.create_task(project.id, TaskCreate(title="撰写指南初稿", description="按提纲写初稿：工作区/任务/Agent/成果四部分。", stage="paper"))
t_check = store.create_task(project.id, TaskCreate(title="复核初稿事实与表述", description="逐条核对功能描述是否准确，标记需修改处。", stage="review"))
t_final = store.create_task(project.id, TaskCreate(title="修订定稿", description="按复核意见修订，产出终稿。", stage="paper"))
print("任务: 4 条已创建（提纲→初稿→复核→定稿）")


def agent_flow(agent_id: str, task_id: UUID, *, progress_note: str, run_summary: str, artifact_name: str, artifact_type: str, result_summary: str, body: str = "") -> str:
    """一条任务的完整执行链：领取 → 进度 → 运行 → 产出成果 → 提交结果。返回成果物 id。"""
    task, lease = store.claim_task(task_id, TaskClaimRequest(agent_id=agent_id, lease_seconds=3600, idempotency_key=key("claim")))
    store.update_task_progress(
        task_id,
        TaskProgressRequest(agent_id=agent_id, lease_token=lease.lease_token, status="RUNNING", message=progress_note, idempotency_key=key("prog")),
    )
    run = store.create_run(
        project.id,
        RunCreate(task_id=task_id, agent_id=agent_id, parameters={"demo": True}, idempotency_key=key("run-create")),
    )
    artifact = store.create_artifact(
        project.id,
        ArtifactCreate(name=artifact_name, artifact_type=artifact_type, description=f"由 {agent_id} 产出"),
        created_by=agent_id,
        created_by_kind="agent",
    )
    # 写正文：这样在群里点「就地查看」能直接读到文档（真 Agent 产出也是这么走的）
    if body:
        store.store_artifact_content(artifact.id, body.encode("utf-8"), "text/markdown")
    evidence = store.create_evidence(
        project.id,
        EvidenceCreate(claim=f"{artifact_name} 的产出依据", evidence_type="artifact", artifact_id=artifact.id, created_by=leader_id),
    )
    store.submit_artifact_for_review(artifact.id, actor=agent_id, actor_kind="agent", evidence_ids=[evidence.id])
    store.complete_run(run.id, RunComplete(success=True, summary=run_summary, output_artifact_ids=[artifact.id], idempotency_key=key("run")))
    store.submit_task_result(
        task_id,
        TaskResultSubmit(
            agent_id=agent_id,
            lease_token=lease.lease_token,
            success=True,
            summary=result_summary,
            output_artifact_ids=[artifact.id],
            idempotency_key=key("result"),
        ),
    )
    return str(artifact.id)


# ---- 3. 开场白与流程叙述（队长的话 + Agent 的真实动作卡片） ----
store.post_project_message(project.id, leader_id, "这个项目演示两个 Agent 协作写文档：写手负责初稿，复核负责把关，我验收。都看群聊就知道进展。")

outline_artifact = agent_flow(
    "agent-demo-writer",
    t_outline.id,
    progress_note="整理平台功能清单：工作区、任务派单、Agent 接入、成果空间四大块",
    run_summary="完成资料整理与提纲拟定",
    artifact_name="指南提纲.md",
    artifact_type="problem_analysis",
    result_summary="提纲完成：四大部分，先给队长确认",
    body=DRAFT_OUTLINE,
)
print("① 提纲完成")

store.post_project_message(project.id, leader_id, "提纲没问题，按这个写初稿。")

draft_artifact = agent_flow(
    "agent-demo-writer",
    t_draft.id,
    progress_note="按提纲撰写初稿：工作区 / 任务 / Agent / 成果四个章节",
    run_summary="初稿完成，约 2400 字",
    artifact_name="指南初稿.md",
    artifact_type="paper_source",
    result_summary="初稿完成，交给复核 Agent 把关",
    body=DRAFT_DOC,
)
print("② 初稿完成")

# 交接：写手 → 复核（真实交接单，群聊会出现卡片）
store.create_handoff(
    project.id,
    HandoffCreate(
        task_id=t_draft.id,
        sender_agent_id="agent-demo-writer",
        receiver={"type": "member", "id": leader_id},
        objective="把初稿交给复核 Agent 逐条把关",
        completed=["提纲已确认", "初稿已产出"],
        input_artifacts=[draft_artifact],
        output_artifacts=[draft_artifact],
        key_conclusions=["初稿覆盖四大功能块", "示例代码缺一段，需要复核确认"],
        open_questions=["示例代码是否必须？"],
        next_actions=["复核 Agent 核对事实与表述"],
    ),
)
print("③ 交接单已发出（写手 → 复核）")

check_artifact = agent_flow(
    "agent-demo-checker",
    t_check.id,
    progress_note="逐条核对初稿：功能描述 12 处，发现 2 处表述不准",
    run_summary="复核完成：2 处需修改（任务派单的默认模式、成果空间入口位置）",
    artifact_name="复核意见.md",
    artifact_type="review_report",
    result_summary="两处需修改，已写明修改建议",
    body=REVIEW_NOTES,
)
print("④ 复核完成")

store.post_project_message(project.id, leader_id, "按复核意见改，出终稿。")

final_artifact = agent_flow(
    "agent-demo-writer",
    t_final.id,
    progress_note="按复核意见修订两处，补充示例代码",
    run_summary="终稿完成，含示例",
    artifact_name="指南终稿.md",
    artifact_type="paper_source",
    result_summary="终稿完成，申请验收",
    body=FINAL_DOC,
)
print("⑤ 终稿完成")

# ---- 4. 队长验收（真实复核：批准提纲与终稿） ----
for artifact_id, summary in [
    (outline_artifact, "提纲结构清晰，按此执行"),
    (draft_artifact, "初稿通过到复核环节"),
    (check_artifact, "复核意见明确，按建议修订"),
    (final_artifact, "终稿验收通过，文档交付"),
]:
    store.create_review(
        project.id,
        ReviewCreate(
            target_type="artifact",
            target_id=UUID(artifact_id),
            verdict="APPROVED",
            summary=summary,
            reviewer=leader_id,
            reviewer_kind=ReviewerKind.MEMBER,
        ),
    )
print("⑥ 四次人工复核通过（提纲/初稿/复核意见/终稿）")

store.post_project_message(project.id, leader_id, "验收通过。这份演示到此结束——新队友可以点任务 Tab 看每条任务、成果空间看四份文档、时间线看全部事件。")

# ---- 5. 汇总 ----
tasks = store.list_tasks(project.id)
artifacts = store.list_artifacts(project.id)
messages = store.list_project_messages(project.id, limit=200)
cards = [m for m in messages if m.message_type == "card"]
humans = [m for m in messages if m.sender_kind == "human"]
print(f"\n演示就绪：任务 {len(tasks)} 条 | 成果物 {len(artifacts)} 份 | 群聊 {len(messages)} 条（卡片 {len(cards)} / 人话 {len(humans)}）")
print("打开工作区切换到本项目即可看到完整故事。")
store.close()