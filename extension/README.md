# Chrome Extension Browser Collector

这个扩展支持人工补采，以及专用 Chrome 配置中的每日顺序采集。所有渠道都只读取公开可见数据，并写入新的 Snapshot。

1. 打开监控台商品详情页，点击“在 Chrome 打开”。
2. 在 Chrome 商品页手动完成 AliExpress 验证（如页面要求）。
3. 在 Chrome 加载当前 `extension/` 目录：打开 `chrome://extensions`，启用“开发者模式”，点击“加载已解压的扩展程序”；如果已经安装，点击“重新加载”。
4. 在扩展详情的“网站访问权限”中允许访问 AliExpress，至少选择“在 aliexpress.com 上”。
5. 回到商品页并刷新。商品页右上角会自动出现“AliExpress 监控采集”浮层；点击浮层中的“采集当前商品”。重新加载扩展后，即使商品页已经打开，扩展也会尝试自动注入浮层。监控台生成的商品链接会携带一个仅供插件识别的监控商品标记，即使 AliExpress 将页面商品 ID 重定向，也能匹配回原监控商品。

说明：Chrome 不允许网页自动打开浏览器工具栏里的扩展 action 弹窗，因此这里使用页面右上角的浮层作为自动弹出界面。工具栏图标仍可用于手动打开扩展菜单。

店铺页也支持人工发现：在 Chrome 打开已加入监控的店铺页并通过验证，点击扩展图标，再点击“采集店铺前 20 个商品”。插件只读取当前页面公开可见的商品链接和销量文本，并将结果同步到已存在的监控店铺。

人工采集时，扩展只在用户主动点击后读取当前标签页的公开可见信息，并调用配置文件中的 Backend：

```text
POST <BACKEND_URL>/api/collection/browser-extension
```

## 每日自动采集

项目根目录的 `install-browser-collector.bat` 会创建每天 `02:00` 运行的 Windows 计划任务，并打开独立 Chrome 配置。扩展必须在这个专用配置中加载，AliExpress 账号也需由用户正常登录。扩展会先刷新店铺前 20 商品池，再采集当天缺少有效快照的商品。

店铺前 20 商品池更新完成后，扩展会把本轮第一个商品页显示到前台并发送通知。若 AliExpress 要求验证，请在这个页面正常完成；扩展检测到公开商品数据恢复后会自动保存第一个 Snapshot，再最小化专用窗口并顺序采集其余商品。

后续商品再次遇到 AliExpress 验证页时，扩展会保留当前标签、暂停整个队列并再次通知用户；不会自动处理验证码。验证完成并回到正常商品页后，扩展从当前商品继续，不重复已经成功的任务。任务完成时只关闭自己使用的专用窗口，不影响用户日常 Chrome。

扩展不会导出 Cookie、保存密码、自动处理验证码或调用内部订单接口。

## 托管 Backend 配置

编辑 `config.js`，把 `globalThis.ALIEXPRESS_MONITOR_API_BASE` 改成托管 FastAPI 的 HTTPS origin；同时在 `manifest.json` 的 `host_permissions` 中加入同一 origin 的精确匹配，例如 `https://monitor-api.example.com/*`。重新加载扩展后，人工补采和每日浏览器采集都会写入云端 Backend。

Render 部署时，Frontend 的 `VITE_API_BASE_URL` 也必须填写同一个 Backend HTTPS origin。扩展和 Frontend 必须指向同一个 Backend，否则会出现“商品尚未加入监控”或看不到历史快照。
