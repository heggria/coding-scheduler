# coding-scheduler 项目规则

- 先读 README.md、DESIGN.md、BACKLOG.md 和目标代码，再做最小改动。
- 禁止子 agent。只改任务指定 worktree，不改主目录，不写生产状态。
- 兼容 /usr/bin/python3 3.9，不使用 match 或 X | Y 类型注解。
- 测试和冒烟使用临时 CS_HOME；不启动真实模型 worker 做测试。
- 使用 /usr/bin/python3 -m unittest discover -s tests_cs 验证。

## 结论格式

写 conclusion：改动摘要、commit SHA、验证命令与结果、产物路径、遗留问题。
完成实现、测试、合并、部署分别说明，不能以其中一项代替另一项。

## PR 规则

只有任务明确授权时才 push / 创建 PR / merge；未授权时只本地提交。
若本任务要求 PR，在 conclusion 写完整 https://github.com/<owner>/<repo>/pull/<number> 链接。
