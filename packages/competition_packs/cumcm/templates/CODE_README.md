# 代码与计算脚本说明

> 项目：{{project_name}}（{{competition}} {{problem_code}} 题，ID `{{problem_id}}`）
> Pack 版本：{{pack_version}}　|　生成时间：{{generated_at}}

## 1. 运行环境

- Python / 其他语言版本：
- 依赖清单文件：`requirements.txt`
- 安装命令：
  ```bash
  pip install -r requirements.txt
  ```
- 随机种子：
- 计算资源需求（CPU/内存/预计耗时）：

## 2. 每问入口与输出

| 子问题 | 入口脚本 | 输入 | 输出结果文件 | 预计耗时 |
| --- | --- | --- | --- | --- |
| 1 | `code/q1_solve.py` |  | `output/result1.xlsx` |  |
| 2 | `code/q2_solve.py` |  | `output/result2.xlsx` |  |
| 3 | `code/q3_solve.py` |  | `output/result3.xlsx` |  |
| 4 | `code/q4_solve.py` |  | `output/result4.xlsx` |  |

## 3. 运行方式

```bash
# 逐问运行
python code/q1_solve.py

# 一键复现全部结果
python code/run_all.py
```

## 4. 复现性约定

- 固定随机种子并在脚本入口设置：
- 输入数据只从 `user_data/` 读取，禁止联网抓取：
- 结果文件写入 `output/`，图表写入 `figures/`：
- 不得写入或修改 `user_data/` 原始附件：

## 5. 每问能力校验

每个子问题脚本末尾应包含可证伪的能力断言（不满足即抛错中断，而不是静默降级）：

| 子问题 | 能力 ID | 断言内容 | 失败后的行为 |
| --- | --- | --- | --- |
| 1 |  |  | 抛错中断 |

## 6. 结果自检

| 子问题 | 自检项 | 通过判据 | 实际 |
| --- | --- | --- | --- |
|  |  |  |  |
