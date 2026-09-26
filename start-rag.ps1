# start-rag.ps1 - ASCII-named wrapper so the .bat entry never depends on the console codepage.
$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot '启动-RAG助手.ps1'
& $target
exit $LASTEXITCODE
