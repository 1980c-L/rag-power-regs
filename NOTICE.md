# 来源、许可与项目工作边界

[项目首页](README.md) · [运行指南](docs/QUICKSTART.md)

## 教程资料来自哪里

技术学习库使用 Datawhale 的 [All-in-RAG](https://github.com/datawhalechina/all-in-rag) 教程，固定版本为 `583a61b09869bc3afc4552289171f6ec188f2c76`。

该固定版本的 [README 许可声明](https://github.com/datawhalechina/all-in-rag/blob/583a61b09869bc3afc4552289171f6ec188f2c76/README.md#许可证) 指明 **CC BY-NC-SA 4.0（署名、非商业性使用、相同方式共享）**。使用或再分发相关教程材料应遵循上游声明；GitHub API 的 `license = null` 不等于上游没有许可声明。

本仓库没有附带转换后的完整教程 `.txt` 文件，只发布 [导入脚本](tools/import_rag_corpus.py)、[资料清单](data/rag_learning/corpus_manifest.json) 和评测资产。报告与界面截图中包含部分来源标题和片段，资料出处仍为 All-in-RAG。导入过程与版本核对见 [运行指南](docs/QUICKSTART.md)。

`data/regs/示例语料-电力安全通用要点.txt` 是项目自编演示材料，非正式规程，不用于实际作业指导。`eval/` 题目由本项目设计。

## 第三方方法和工具

| 部分 | 使用的已有方法或工具 |
|---|---|
| 关键词检索 | BM25 方法、jieba 分词 |
| 本地向量实验 | BAAI/bge-small-zh-v1.5 的 Xenova ONNX 版本，ONNX Runtime、Transformers 分词器、FAISS |
| 页面 | React、Vite、Streamlit |
| 请求与导出 | requests、python-docx |
| Windows 打包 | PyInstaller 与相关打包依赖 |

上述工具、模型和资料各自保留其上游许可。主运行依赖见 `requirements*.txt`，前端依赖见 `frontend_v3/package.json`；向量实验还有单独的推理依赖，见 `vector_retriever.py`。

## 本项目完成了什么

项目组织了资料转换、可追溯切分、检索与上下文组装、评测判据、本地工作台、用户资料库、批量任务、导出和运行检查。代码由 CodeBuddy / Codex 辅助实现；项目负责人主导需求、范围、评测与验收，并整理可解释的复盘。

这不表示自行发明 BM25、训练嵌入模型或从零实现 React 等框架。个人职责与当前能力边界见 [项目复盘](docs/PROJECT.md)。

## 发布范围

本仓库没有为整仓另行添加统一开源许可；可见源码与获得所有内容的使用授权是不同事项。第三方资料与依赖以各自上游许可为准。

运行时模型 Key 应通过服务端环境变量配置，用户自己的资料与运行产物不应提交到公开仓库。项目说明不声称已对整仓完成秘密扫描，也不把检索命中率当作回答准确率。
