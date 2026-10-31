$ErrorActionPreference = "Stop"

Write-Host "店小秘采集复用监控台当前选中的采集电脑和已打开的 Chrome 窗口。"
Write-Host "请在已登录监控台、AliExpress，并已启用本项目扩展和店小秘助手的 Chrome 中打开 worker 模式监控台。"
Write-Host "NAS/LAN worker 地址：在当前监控台地址后添加 ?browser_collection=1；已有 ? 参数时使用 &browser_collection=1。"
Write-Host "本机 127.0.0.1/localhost 地址：使用 ?browser_collection=1&browser_collection_profile=isolated，或 ?dianxiaomi_worker=1&dianxiaomi_local=1。"
Write-Host "然后在监控台唯一的“采集电脑”选择器中选中本机，并手动开始或继续队列。"
Write-Host "此脚本不会启动、关闭或更改 Chrome 配置，也不会开始采集。"
