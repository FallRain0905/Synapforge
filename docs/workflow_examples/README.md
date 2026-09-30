# 工作流定义 fixture 库

| 文件 | 地位 | 用途 |
|---|---|---|
| `longform-writing.json` | **规范示例**（WORKFLOW_SCHEMA.md §2 的机器可读版） | B 编辑器骨架的渲染/回填种子；线性四节点最小形状 |
| `cumcm-four-questions.json` | **参考预案（非规范）** | 复杂形状测试：四问并行建模分支 + 手工收口节点（互斥校验的 manual 例）+ 交付适配器 + 多 handoff；权威 CUMCM 定义以 `cumcm_importer`（W3.4）生成物为准 |

两个 fixture 都通过 `app.workflow_service.validate_definition` 实测（0 错误，2026-09-30）。
改任何一个 fixture 后必须重跑：

```bash
cd apps/api && python -X utf8 -c "import json,sys;sys.path.insert(0,'.');from app.workflow_service import validate_definition;import pathlib;spec=json.loads(pathlib.Path('../docs/workflow_examples/<文件名>.json').read_text(encoding='utf-8'));errs=validate_definition(spec);print(len(errs));[print(' -',e) for e in errs]"
```

fixture 由工作包 C 维护；校验器（A）语义变更导致 fixture 失配时，先改 schema 文档再改 fixture。
