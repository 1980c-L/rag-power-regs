# -*- coding: utf-8 -*-
"""配置：从环境变量读取 API 信息。

不要把 API key 写进代码或提交到 git，用环境变量注入。
Windows PowerShell 里设置方式：
    $env:DEEPSEEK_API_KEY = "sk-xxxxxxxx"

换用智谱时，改下面三个环境变量即可（OpenAI 兼容接口）：
    $env:DEEPSEEK_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    $env:DEEPSEEK_MODEL   = "glm-4-flash"
"""
import os

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
