# COSORI 空气炸锅 · 三平台价格采集工具

采集 Wildberries、Yandex Market、Ozon 上 COSORI 空气炸锅的**到手价、型号、店铺名、商品链接**，生成可离线打开的对比页面。

## 运行

```bash
python -m pip install requests beautifulsoup4 lxml playwright
python scraper.py
```

打开生成的 `index.html` 即可查看。结构化数据在 `data/products.json`。

常用参数：

```bash
python scraper.py --pages 3                          # 每个平台多抓几页
python scraper.py --skip-wildberries --skip-ozon     # 只抓 Yandex
python scraper.py --proxy http://user:pass@host:port # 用住宅代理绕过反爬
python ozon_seller.py --merge              # 用 Ozon Seller 官方 API 补 Ozon 价格
```

代理也可通过环境变量 `COSORI_PROXY` 提供。

## Ozon Seller API（推荐，无需代理）

`ozon_seller.py` 用店铺后台 API 直接读数据，绕开前台反爬：

- **本店商品实时售价**：从商品报告读 DC601 / DC112 / CP158 的 Ozon 售价与划线价
- **竞品卖家名单**：`pricing-strategy/competitors/list`，含 market.yandex.ru、wildberries.ru、ozon.ru、citilink.ru、dns-shop.ru 等 31 家
- **定价策略状态**：系统策略「跟随最优外部价」与自定义策略「Ozon 最低价」的启用状态

凭证放在 `cosori_price_tool/.env`（模板见 `.env.example`）或环境变量 `OZON_CLIENT_ID` / `OZON_API_KEY`。

**重要边界**：官方 API 的竞品价接口 `pricing-strategy/product/info` 只对「已加入定价策略」的商品返回价格。
目前这 3 件 COSORI 商品尚未加入策略，所以竞品价为空。`--setup` 会创建策略并加商品，
但现有策略是自动改价策略，可能触发跟价，未经确认不要执行。只读采集用默认参数即可。

## 每日自动更新与看板

- 已创建自动化任务「COSORI 三平台价格每日更新」，每天 09:00 执行采集 → 补全店铺名 → 推送飞书 → 汇报。
- `python enrich_shops.py`：从 WB 静态 card.json 取 supplier_id，再读 seller 页店名，写入看板「店铺」列（点店名可直达店铺）。
- `python push_feishu.py`：把结果推送到飞书群卡片和/或多维表格，**从零配置见 `FEISHU_SETUP_GUIDE.md`（逐步操作 + 报错速查）**。
- `PRICE_DISPUTE_PLAYBOOK.md`：零售商投诉乱价时的应对话术与证据清单。
- `OZON_AND_FEISHU.md`：Ozon 反爬实测结论、三种可行绕过方案、飞书配置步骤、店铺页找不到商品的原因。

## 各平台策略

| 平台 | 主要方式 | 失败回退 |
| --- | --- | --- |
| Yandex Market | 搜索页 HTML 内嵌 JSON-LD + SSR 卡片解析 | 重试流式下载 |
| Wildberries | 内部接口 `search.wb.ru/exactmatch/.../v9/search` | Chrome 浏览器渲染 |
| Ozon | `composer-api.bx/page/json/v2` | Chrome 浏览器渲染 + 网络响应捕获 |

WB / Ozon 对数据中心 IP 有强反爬。WB 的 JS 挑战需真实 Chrome 等待约 40 秒自动求解；
Ozon 在当前网络下返回 403 重定向循环，需在 `--proxy` 中提供俄罗斯住宅/移动代理。
工具会把被拦截的平台标记为 `blocked` 并在页面上提示，不影响其他平台数据展示。

## 字段说明

- `price`：到手价（搜索页展示的当前成交价，含平台折扣）
- `original_price`：划线价，未展示时为 `null`
- `model`：从标题中提取的型号（如 `DC112`、`CP158`、`DC601`、`P585S`）
- `shop`：店铺/卖家名称；三家平台的搜索结果页都不公开卖家名，统一标记为“见商品页”。
  若需要精确店铺名，可在生成的页面点击商品链接进入商品页查看，或接入带卖家字段的付费数据源
- `status`：`ok` / `blocked` / `error`

平台接口随时可能调整；若解析失败，优先检查 `data/products.json` 中对应平台的 `note` 字段。
