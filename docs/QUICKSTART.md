# 本地运行指南

[返回首页](../README.md) · [项目复盘](PROJECT.md) · [评测说明](EVALUATION.md)

这里介绍从源码在本机运行。GitHub 仓库不提供在线 Python 服务，也没有随源码附带前端构建产物、完整教程语料或嵌入模型。以下命令在仓库根目录执行；示例使用 Windows PowerShell、Python 3.10+。

## 1. 最短路径：先试一次无 Key 检索

```powershell
git clone https://github.com/1980c-L/rag-power-regs.git
cd rag-power-regs
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-api.txt
python 02_retrieve.py "作业前需要确认哪些安全措施？" --corpus power_demo --top-k 3
```

若 PowerShell 环境不允许激活虚拟环境，可直接使用 `.\.venv\Scripts\python.exe` 替代后续 `python`。`power_demo` 使用仓库自编的电力示例资料，非正式规程，只用于体验链路。此命令不调用模型；它的结果不对应首页的 471 片段 / 27 题指标。

也可以直接检查已发布的评测文件是否成套：

```powershell
python 06_eval_vector.py --corpus rag_learning --verify-published
```

这是文件一致性检查，不是重跑检索。

## 2. 导入用于正式评测的技术学习资料

先阅读 [来源与许可说明](../NOTICE.md)。转换脚本要求上游恰好处于固定版本，不会自动改用其他版本：

```powershell
git clone https://github.com/datawhalechina/all-in-rag.git ..\all-in-rag
git -C ..\all-in-rag checkout 583a61b09869bc3afc4552289171f6ec188f2c76
python tools/import_rag_corpus.py --repo ..\all-in-rag
python tools/import_rag_corpus.py --repo ..\all-in-rag --upstream-check
python 02_retrieve.py "RAG 的三个核心步骤是什么？" --corpus rag_learning --top-k 3
```

导入产生 `data/rag_learning/documents/` 下的资料。`--upstream-check` 重新从固定上游构造资料并对比；`--self-check` 只比较本地资料与本地清单，二者证明范围不同。

## 3. 运行 React 工作台

本机需要 Node.js 与 npm，版本应符合 [Vite 6 的环境要求](https://v6.vite.dev/guide/#scaffolding-your-first-vite-project)。第一次运行先安装前端依赖并构建 **API 模式**：

```powershell
cd frontend_v3
npm ci
$env:VITE_DATA_SOURCE = "api"
$env:VITE_API_BASE = "http://127.0.0.1:5280"
npm run build -- --outDir dist-api
cd ..
& .\启动-RAG助手.ps1
```

在浏览器访问 `http://127.0.0.1:5273`。API 监听 `127.0.0.1:5280`，页面服务监听 `127.0.0.1:5273`；都仅供本机使用。页面默认使用技术学习库，体验前先完成第 2 步。

停止：

```powershell
& .\停止-RAG助手.ps1
```

源码下载后直接双击启动脚本不能替代依赖安装、资料导入和前端构建。未设置 `VITE_DATA_SOURCE=api` 时，前端使用模拟数据；看见页面不等于运行了真实检索。

### 可选：使用 Streamlit 页面

完成资料导入后，在相同 Python 环境执行：

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

这是另一个本地入口，默认端口通常为 8501；不需要 Node.js。

## 4. 可选生成与资料导入

不设置模型 Key 时，工作台可以展示检索证据。需要生成时，由使用者在启动后端前通过服务端环境变量配置 `DEEPSEEK_API_KEY`；可通过 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 指定兼容接口与模型。相关读取位置见 [config.py](../config.py)。Key 不放在前端 `VITE_*` 变量中。

配置真实 Key 后，在页面选择生成会发出模型请求并可能产生费用；检索指标不因此变成回答准确率。批量生成需要页面上的明确确认；不要把 `tools/` 中的真实调用实验脚本当成常规启动入口。

用户资料库支持 `.txt` / `.md` 文件和粘贴文本，独立于固定学习库。批量问题可参考 [示例 CSV](../示例问题.csv)，导出读取已保存结果，不重新生成。

## 常见问题

| 现象 | 先检查什么 |
|---|---|
| 学习库资料为空或加载失败 | 是否成功导入固定版本的 All-in-RAG 文档 |
| 页面看起来正常，但不是实时结果 | 构建时是否设置 `VITE_DATA_SOURCE=api`，当前是否访问 `dist-api` 服务 |
| 启动提示 5273 / 5280 被占用 | 核对已有进程；启动脚本不会抢占端口 |
| 没有模型回答 | 未配置 Key 时属正常行为，先确认检索证据是否合理 |
| 想运行向量对照却缺依赖 | 主运行依赖不含 ONNX / FAISS 及模型文件，先看 [评测范围](EVALUATION.md) 与 [实现入口](../vector_retriever.py) |

Windows 可执行包另由打包脚本生成。仓库中的源码、历史打包验证记录和现成可下载的安装包是不同交付物；本指南不假设已经下载了 EXE。
