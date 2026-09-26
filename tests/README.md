# 当前功能的检查脚本

[项目首页](../README.md) · [运行准备](../docs/QUICKSTART.md)

这里保留当前本地 API、用户资料库、批量任务、页面与 Windows 交付的检查。它们用于开发回归，不是日常运行入口。

## 常用的三项

先按运行指南安装 Python 依赖，并导入固定版本的技术学习资料。随后从仓库根目录执行：

```powershell
$env:NO_PROXY = "127.0.0.1,localhost,::1"
python tests/verify_api_v3.py
python tests/verify_user_library.py
python tests/verify_batch_jobs.py
```

这三项检查分别覆盖接口、用户资料导入删除、批量取消与导出。`NO_PROXY` 让本机请求直连，避免系统代理影响大请求的结果。生成相关用例指向本机桩服务，不调用真实模型；测试资料与批量状态使用隔离目录。检查会暂时使用本机端口，运行前应停止自己的工作台服务。

## 其他检查按功能运行

| 脚本 | 检查内容 | 额外准备 |
|---|---|---|
| `verify_frontend_v3.py` | React 页面与 API / 模拟数据模式 | Node.js、前端依赖和浏览器检查环境 |
| `verify_userlib_page.py` | 用户资料库页面操作 | 已构建前端与浏览器检查环境 |
| `verify_batch_page.py` | 批量提问页面 | 前端与浏览器检查环境 |
| `verify_portable_package.py` | 源码便携运行路径 | 本地资料与前端构建产物 |
| `verify_exe_package.py` | Windows 可执行包 | 实际打包产物 |
| `verify_tray.py` | Windows 托盘与启停 | Windows 及实际打包产物 |

例如查看参数：`python tests/verify_frontend_v3.py --help`。部分脚本会自行构建、启动本地服务或读取指定交付包，请先查看其参数与说明。

检索评测入口仍在根目录的 `04_eval.py`、`06_eval_vector.py`。历史 Streamlit 与早期实验检查不再放在当前源码树，原版本可从 Git 历史查看。
