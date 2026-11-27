# Ozon 反爬、飞书看板与店铺名说明

## 1. Ozon 到底怎么绕？——结论先行

**必须换出口 IP（俄罗斯住宅/移动代理）。** 代码层面的伪装（stealth、有头浏览器、Composer API）在本机全部实测失败：

| 尝试 | 结果 |
| --- | --- |
| requests 直连 composer-api | 307 重定向循环 / 403 |
| 无头 Chrome 直接打开搜索页 | 403「Похоже, нет соединения」 |
| Chrome 先开首页过验证，再在页面内 fetch API | 首页 403 Antibot Challenge Page，接口返回 `incidentId` + `challengeURL` |
| playwright-stealth + 有头 Chrome | 仍然 403，30 秒后进入挑战页 |
| 公共代理网关（AllOrigins / CORSProxy / Jina / translate.goog） | 网关不可达或需要付费密钥 |

原因：Ozon 用 **Variti**（不是普通 Cloudflare）做 antibot，核心判定是 IP 信誉 + TLS 指纹 + 会话行为。
当前出口 IP 是境外机房 IPv6，属于高危段，未过挑战前连首页 HTML 都拿不到，因此任何"页面内取 JSON"的技巧都无法启动。
反过来看，WB 的 JS 挑战能在本机 40 秒内自动通过，说明 WB 的防线比 Ozon 弱一档——这也是为什么 WB 能抓、Ozon 不能。

### 可落地的三条路（按推荐度）

**方案 A：俄罗斯住宅代理（最稳，几十美元/月）**
- 选莫斯科/圣彼得堡住宅或移动 IP，支持 sticky session 20–30 分钟。
- 配置：`python scraper.py --proxy http://user:pass@host:port`，或设置 `COSORI_PROXY` 环境变量。
- 关键细节：一个会话内先打开 `https://www.ozon.ru/` 直到能搜索，再访问搜索页；价格与配送城市绑定，代理地区选莫斯科才能对齐其他平台口径。
- 请求间隔 3–10 秒，403/验证码立即换 IP 并加倍延迟，成功 20–30 次后轮换。

**方案 B：第三方抓取 API（最省事，按量付费）**
- Oxylabs / Bright Data / ScrapingBee / Zyte 都提供"俄罗斯电商"目标解析，内置住宅 IP 池和验证码处理，返回结构化 JSON。
- 成本参考：约 $100–400/月（中低频价格监控），无需自己维护代理和指纹。
- 本工具的 `scrapers.py::scrape_ozon` 是独立适配器，可把请求替换为这些服务的 API，输出格式不变。

**方案 C：官方/合作伙伴数据（最合规，门槛高）**
- Ozon Seller API 只返回自己店铺数据，不能抓竞品价。大零售商可与 Ozon 签数据合作或获取授权采集许可。

### 不建议的做法
- 破解验证码（2Captcha/AntiCaptcha）：违反平台条款，且有法律风险；本工具不内置。
- 高频暴力重试：只会让 IP 段被永久封禁。
- 单纯改 User-Agent / 加 `-disable-blink-features`：对 Variti 无效。

## 2. 飞书看板怎么接

当前账号下没有可直接绑定的飞书 Connector，因此采用**飞书开放 API 自建接入**（`push_feishu.py` 已写好）。

### 2.1 群机器人卡片（5 分钟，最低门槛）
1. 飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 webhook。
2. 设置环境变量：
   ```bash
   export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
   # 若开启签名校验：
   export FEISHU_WEBHOOK_SECRET="你的密钥"
   ```
3. 测试：`python push_feishu.py --test`，群里应收到蓝色卡片。

### 2.2 多维表格看板（店铺名/型号/到手价/链接全字段）
1. 飞书开放平台 → 创建企业自建应用 → 开通 `bitable:app`、`im:message` 权限 → 发布版本。
2. 新建多维表格，字段名必须与脚本一致：`平台`、`型号`、`商品名称`、`店铺`、`到手价`、`原价`、`商品链接`、`采集时间`。
3. 从表格 URL 取 `app_token`（`bascn...`）和 `table_id`（`tbl...`），并把表格授权给应用。
4. 设置环境变量：
   ```bash
   export FEISHU_APP_ID="cli_xxx"
   export FEISHU_APP_SECRET="xxx"
   export FEISHU_APP_TOKEN="bascnxxx"
   export FEISHU_TABLE_ID="tblxxx"
   ```
5. 推送：`python push_feishu.py --mode bitable`（写表）或 `--mode both`（写表+发卡片）。

### 2.3 每日自动更新
已创建自动化任务「COSORI 三平台价格每日更新」，每天 **09:00** 执行：采集 → 补全店铺名 → 推送飞书 → 汇报结果。
凭证未配置时会自动跳过飞书推送，只更新本地 `index.html`，不会中断。

## 3. 为什么店铺页找不到商品

以你给的 `https://www.wildberries.ru/seller/250223727` 为例：

- 该店铺名是 **「DP Добрые покупки」**，在售的是汽车密封条、毛巾架、握力器、窗帘杆等杂货，**本来就不卖 COSORI 空气炸锅**。
- 这台 COSORI CAF-R901（nm=466255431）在商品详情页里的真实卖家链接是 `/seller/1385227`，店名 **「Smart Mall」**。
- 因此"点进链接发现店在卖，但店铺里找不到商品"的原因是：**链接指向的卖家 ID 与商品实际卖家不是同一个**，或该卖家曾挂过该商品但已下架/转移（WB 2026 年 11 月 9 日起开放商品链接转让，旧链接会指向新卖家）。

工具已加入 `enrich_shops.py`：从商品静态 card.json 取 `supplier_id`，再打开对应 seller 页读店名，写入 `shop` 字段和 `seller_url`。重新采集后跑一次即可补全。
Yandex 搜索页同样不公开卖家名，字段保留"见商品页"；Ozon 一旦接入代理，其 composer-api 的 `widgetStates` 里带 `shopName`，可自动填充。
