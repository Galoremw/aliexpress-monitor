# Chrome Extension Manual Collector

这是人工补采渠道，不是每日自动采集器。

1. 打开监控台商品详情页，点击“在 Chrome 打开”。
2. 在 Chrome 商品页手动完成 AliExpress 验证（如页面要求）。
3. 在 Chrome 加载 `extension/` 目录：打开 `chrome://extensions`，启用“开发者模式”，点击“加载已解压的扩展程序”。
4. 回到商品页，点击扩展图标，再点击“采集当前商品”。

店铺页也支持人工发现：在 Chrome 打开已加入监控的店铺页并通过验证，点击扩展图标，再点击“采集店铺前 20 个商品”。插件只读取当前页面公开可见的商品链接和销量文本，并将结果同步到已存在的监控店铺。

扩展只在用户主动点击时读取当前标签页的公开可见信息，并调用：

```text
POST http://127.0.0.1:8000/api/collection/browser-extension
```

它不会自动监听页面、读取 Cookie、保存密码、处理验证码或调用内部订单接口。
