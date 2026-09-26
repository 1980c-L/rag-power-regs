# 来源、许可与自研边界（NOTICE）

> 本文与 `README.md` 分工：README 讲"项目做什么、怎么跑、边界在哪"，本文只讲
> **哪些内容来自哪里、许可是什么、哪些是自己写的**。
> （README 处于复审链冻结状态，本文单独成篇，避免改动已锚定的文件。）

## 1. 自研与第三方

| 部分 | 来源 | 说明 |
|---|---|---|
| 切分 / BM25 / 同节上下文补全 / 引用解析（`rag_core.py`） | **自研** | 刻意不引入 LangChain，"每行都能讲清" |
| 评测框架与判据（`04_eval.py`） | **自研** | 判据函数被 `06_eval_vector.py` 以 importlib 复用，保证同口径 |
| 向量检索对照（`06_eval_vector.py`、`vector_retriever.py`） | **自研封装** | 模型 `Xenova/bge-small-zh-v1.5`（ONNX），推理用 `onnxruntime` + `transformers` 分词器 |
| Streamlit 工作台（`app.py` / `app_v2.py`） | **自研** | |
| 前端第三版（`frontend_v3/src`） | **自研** | React + Vite；`node_modules` 与构建产物未入库（见 `.gitignore`） |
| 端口占用/打包辅助脚本（`tools/*.py`） | **自研** | 含本地桩服务，联调时不产生费用 |
| 第三方运行依赖 | `requirements*.txt` | 未修改上游代码 |

## 2. 语料来源（**重要：许可状态需使用者自行确认**）

`data/rag_learning/documents/*.txt` 与 `corpus_manifest.json` 由开源教程项目
**All-in-RAG**（GitHub: `datawhalechina/all-in-rag`，约 11.4k stars）的文档章节转换而来：

- 转换脚本：`tools/import_rag_corpus.py`（本项目自写）
- 语料指纹与逐篇清单：`data/rag_learning/corpus_manifest.json`（可用于校验语料版本一致）
- **上游许可状态：截至 2026-09-26，该仓库未标注 LICENSE（GitHub API 返回 `license = null`）。**
  因此本文不声称这些文档可自由再分发；若上游有许可要求，请以
  [All-in-RAG 仓库](https://github.com/datawhalechina/all-in-rag) 的声明为准。

**本仓库不分发这些文档的原文** —— 上游未标注许可，为避免再分发风险，`data/rag_learning/documents/`
下的 `.txt` 不随仓库发布，只保留**语料清单与指纹**（`corpus_manifest.json`）和**转换脚本**。

要复现语料（三步）：

1. 自行获取 All-in-RAG 仓库，按其声明的方式使用；
2. 用 `tools/import_rag_corpus.py` 指向你的本地副本执行转换；
3. 转换后核对 `data/rag_learning/corpus_manifest.json` 里的指纹 —— **指纹一致，才说明语料版本相同，
   评测数字才可比**（这也是本项目"先锚定、再对比"的一贯做法）。

`data/regs/示例语料-电力安全通用要点.txt` 是**本项目自编的示例资料，非正式规程**，仅用于演示检索链路。

`eval/*.json` 的题目为本项目自编。

## 3. 数据与隐私

- 仓库内**不含任何 API key**：`05_gen_eval.py` 里的 `sk-STUB-NOT-A-REAL-KEY` 是本地桩服务用的假值，只校验请求头存在、不校验内容。
- 真实模型调用次数很少，且**未经独立复现**；生成层的状态与边界见 `生成验证记录.md` 与 `实验说明-真实RAG学习库基线.md`。
- `user_library.py` 操作的是使用者本机的资料目录，仓库内不含任何个人资料。

## 4. 能力口径（避免被误读）

- 这里报告的是**检索命中率**（标准证据片段是否进入前 K 条），**不是回答准确率**；
- **没有**做微调、reranker、融合检索、多轮记忆；不应被描述为"做了向量库/混合检索"以外的东西；
- 生成层只有少量用户侧样本，**未通过独立复现**。
