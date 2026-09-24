"""让平台能读到 Agent 的**回答**：摘要以回答为主、原始输出留得更长。

现状问题：摘要形如 `codex exit=0 run=… | reply: 回答…`，回答被夹在中间且截断到 400 字，
界面上只能看到半句话；原始 JSONL 只留末尾 4000 字，看不到中间过程。
"""

from pathlib import Path

# 1) codex_executor：摘要改成"回答在前，诊断在后"
p = Path("apps/agent/codex_executor.py")
text = p.read_text(encoding="utf-8")
old = '''    summary = (
        f"codex exit={exit_code} run={run_id} events={parsed.get('event_count', 0)}"
        f" tools={parsed.get('tool_events', 0)} files={parsed.get('file_events', 0)}"
    )
    if parsed.get("final_message"):
        summary += f" | reply: {parsed['final_message'][:400]}"
    if parsed.get("error_events"):
        summary += f" | errors: {'; '.join(parsed['error_events'])[:200]}"
    if parsed.get("transient_events"):
        summary += f" | warnings: {'; '.join(parsed['transient_events'])[:200]}"
    if parsed.get("unparsed"):
        summary += f" | unparsed_lines={parsed['unparsed']}"
    success = exit_code == 0 and not parsed.get("error_events")
    return success, summary'''

new = '''    diagnostics = (
        f"codex exit={exit_code} run={run_id} events={parsed.get('event_count', 0)}"
        f" tools={parsed.get('tool_events', 0)} files={parsed.get('file_events', 0)}"
    )
    if parsed.get("error_events"):
        diagnostics += f" | errors: {'; '.join(parsed['error_events'])[:300]}"
    if parsed.get("transient_events"):
        diagnostics += f" | warnings: {'; '.join(parsed['transient_events'])[:300]}"
    if parsed.get("unparsed"):
        diagnostics += f" | unparsed_lines={parsed['unparsed']}"

    # 回答放在**最前面**：界面（列表、详情、复制按钮）都直接展示这段文本，
    # 夹在诊断信息中间时会被各种截断切掉半句话。原文保留，不做二次截断——
    # 截断由 `_complete_run` 的字段上限统一负责。
    final_message = str(parsed.get("final_message") or "").strip()
    if final_message:
        summary = f"{final_message}\\n\\n---\\n{diagnostics}"
    else:
        summary = f"（本次执行没有产出回复文本）\\n\\n---\\n{diagnostics}"

    success = exit_code == 0 and not parsed.get("error_events")
    return success, summary'''
assert old in text
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8")
print("codex summary: answer first")

# 2) agentd：结果上报的字段上限放宽（回答要能完整看到）
p = Path("apps/agent/agentd.py")
text = p.read_text(encoding="utf-8")
old = '''                    "success": success,
                    "summary": summary[:800],
                    "stdout": (stdout or "")[-4000:],
                    "stderr": (stderr or "")[-4000:],'''
new = '''                    "success": success,
                    # 上限放宽：回答要能完整读到，原始输出要能回溯过程。
                    # 平台侧只存不截（RunComplete 无长度限制），截断只发生在这里。
                    "summary": summary[:8000],
                    "stdout": (stdout or "")[-40000:],
                    "stderr": (stderr or "")[-8000:],'''
assert old in text
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8")
print("complete_run: limits raised")