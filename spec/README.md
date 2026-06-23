# Spec

本仓库所有设计与规划文档的入口。结构按"特性自包含 + 规范独立 + 单一索引"组织,
随特性增多平滑扩张。

## 目录结构

```
spec/
├── README.md                 # 本文件:特性索引 + 状态总览
├── conventions/              # 跨特性的长期约定(不随单个特性生灭)
│   └── working-norms.md      # 工作规范:Spec 先行 + 测试随产出积累
└── features/                 # 每个特性一个自包含目录
    └── <feature-slug>/
        ├── design.md         # 设计 Spec(为什么、做什么、决策记录)
        ├── plan.md           # 实现计划(可执行的分步 task)
        ├── summary.md        # 完成总结(做了什么、改动文件、缺陷修复)
        └── test-report.md    # 测试报告(测了什么、结果)
```

## 约定

- **特性目录名**用稳定的 kebab-case slug(如 `mcp-client`),**不带日期前缀**。
  日期写进各文档内部的 frontmatter / 抬头。
- 一个特性不一定四份文档齐全:小改动可能只有 `design.md`;研究性工作可能只有
  笔记。**至少有 `design.md` 作为起点。**
- 同一特性的后续大迭代,在该特性目录内追加(如 `design-v2.md`),不新开目录。
- 跨特性的长期规范放 `conventions/`,不要混进某个特性目录。

## 特性索引

| 特性 | 状态 | 文档 |
|------|------|------|
| [MCP 客户端接入](features/mcp-client/) | ✅ 已实现(`feat/mcp-client`,待合并) | [design](features/mcp-client/design.md) · [plan](features/mcp-client/plan.md) · [summary](features/mcp-client/summary.md) · [test-report](features/mcp-client/test-report.md) |
| [上下文压缩(history 裁剪 + 中间轮蒸馏)](features/context-compression/) | 🚧 设计已拍板,前置 M0/M1/M2 已实现 | [design](features/context-compression/design.md) |

> 状态图例:📝 草稿 / 🚧 进行中 / ✅ 已实现 / 🔀 已合并 / 🗄️ 已归档
