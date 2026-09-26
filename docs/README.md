# 文档与代码导航

[返回项目首页](../README.md)

## 看项目，先读这三篇

| 文档 | 适合了解什么 |
|---|---|
| [项目复盘与个人职责](PROJECT.md) | 问题、设计取舍、个人工作与 AI 辅助方式 |
| [评测结果与失败分析](EVALUATION.md) | 语料、指标分母、两种检索方法、失败案例与结论边界 |
| [本地运行指南](QUICKSTART.md) | 从源码体验检索与工作台 |
| [来源与许可](../NOTICE.md) | 教程资料、第三方工具和仓库发布范围 |

## 看实现，按功能找入口

| 功能 | 入口 | 阅读建议 |
|---|---|---|
| 核心链路 | [rag_core.py](../rag_core.py) | 先看资料加载、`retrieve`、`expand_and_assemble`，再看生成与引用 |
| 命令行体验 | [02_retrieve.py](../02_retrieve.py)、[03_qa.py](../03_qa.py) | 检索与生成入口分开；生成需要 Key |
| 检索评测 | [04_eval.py](../04_eval.py) | 看标准证据匹配与 Hit@K / MRR 的计算 |
| 向量对照 | [06_eval_vector.py](../06_eval_vector.py)、[vector_retriever.py](../vector_retriever.py) | 离线实验，复用基线判据；不是工作台默认检索 |
| 资料导入 | [tools/import_rag_corpus.py](../tools/import_rag_corpus.py)、[语料清单](../data/rag_learning/corpus_manifest.json) | 固定上游版本与资料身份 |
| 工作台 | [api_v3.py](../api_v3.py)、[frontend_v3/src](../frontend_v3/src) | 本机 API 与 React；前端有模拟/API 两种数据源 |
| 早期页面 | [app.py](../app.py)、[app_v2.py](../app_v2.py) | Streamlit 入口，保留用于学习与历史演示 |
| 用户资料 | [user_library.py](../user_library.py) | 独立的本机用户资料目录 |
| 批量与导出 | [batch_jobs.py](../batch_jobs.py)、[batch_export.py](../batch_export.py) | 保存结果、状态与取消、CSV / DOCX |
| Windows 交付 | [build_release.py](../build_release.py)、[launcher.py](../launcher.py)、[tray.py](../tray.py) | 打包、启停与托盘 |
| 检查与样本 | 根目录 `verify_*.py`、[eval](../eval)、[output](../output) | 具体功能的回归脚本和逐题证据，按需查看 |

## 为什么根目录还有不少脚本

仓库保留了从 CLI、Streamlit 到本地工作台的演进入口，以及独立功能的验证脚本。它们不都是日常运行所需文件。当前整理以首页和文档导航为主，保留运行路径，避免仅为减少文件数量就破坏脚本之间的引用。

根目录的 [实验说明](../实验说明-真实RAG学习库基线.md)、[生成验证记录](../生成验证记录.md) 和 [托盘改动记录](../0.5.1-无窗口托盘-改动与验收.md) 是历史材料，保留当时的检查范围。了解当前项目先从首页与这组文档开始，不需要按编号读完整个历史。
