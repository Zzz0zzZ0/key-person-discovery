# Key Person Discovery

从公司画像出发，优先通过 AnySearch 发现公开网页并在无结果或失败时降级到 SearXNG，同时主动读取官网 Sitemap；使用 Crawl4AI 并发抓取 HTML、使用 pypdf 提取文本型 PDF，最后由当前 Hermes 主 Agent 输出带来源证据的联系人候选。

命令行只读取 JSON 公司快照；本地展示页可通过 PostgreSQL 只读事务查询 Twenty 公司并创建快照，但不会写 Twenty CRM，不会自动登录 LinkedIn，也不会探测 WhatsApp 账号是否注册。所有结果都需要人工复核。

项目边界固定为本 README 所在的 `key-person-discovery/`：代码保存在 `src/`、`web/`、`deploy/` 和 `tests/`，运行输入保存在 `inputs/`，结果保存在 `outputs/`，任务状态与日志保存在 `state/`。CLI、dashboard 和 batch 会拒绝把输出、输入或状态库指向项目目录之外。Twenty PostgreSQL、搜索服务和 Hermes 是外部依赖，但不会接收本项目的代码或结果文件。

## 运行条件

- Python 3.11+
- AnySearch（API Key 可选，匿名访问限额较低）
- 可返回 JSON 的 SearXNG 实例，作为 AnySearch 的降级来源；本项目已提供仅监听本机 `18080` 端口的配置
- 已配置的 Hermes 主 Agent；默认命令为 `/Users/acelerzbw/.local/bin/hermes`

```bash
cd key-person-discovery
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium

docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml up -d
export KEY_PERSON_PROXY_URL=http://127.0.0.1:12001
# 可选：export ANYSEARCH_API_KEY=...
.venv/bin/key-person-discovery \
  --company examples/aceler.json \
  --output outputs/aceler.json \
  --phone-region CN
```

CLI 默认每家公司最多调用 AnySearch 5 次；其余查询直接使用 `http://127.0.0.1:18080` 的 SearXNG。官网联系页和联系方式密集型 PDF 会优先占用现有抓取名额，因此默认流程不再运行第 6–7 次姓名任职验证；显式设置为 7 时仍保留人工对照能力。可通过 `KEY_PERSON_ANYSEARCH_MAX_QUERIES` 调整上限，设置为 `0` 可完全禁用 AnySearch；如需覆盖 SearXNG 地址可设置 `SEARXNG_URL`。`KEY_PERSON_PROXY_URL` 会显式传给 AnySearch、官网 Sitemap 发现和 Crawl4AI；未设置时回退到 `HTTPS_PROXY` 或 `HTTP_PROXY`。如果 Hermes 不在默认位置，设置 `HERMES_COMMAND`。

海关数据使用独立的 AnySearch 预算。只有公司快照明确包含 `customs_search_enabled=true` 时才会查询；未被公司匹配模块选中的普通公司保持 0 次。选中后默认最多 2 次，可通过 `KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES` 设置为 0–3。首次查询没有同时匹配目标公司和贸易语义的有效线索时立即停止；命中后才继续核实。结果保存在 `customs` 字段和 `customs-search-results.json`，只添加“海关采购证据”或“海关记录待核实”标签，不改变公司匹配分，也不进入联系人抽取。实际调用数写入 `run_summary.customs_anysearch_queries`。

每次运行前会验证本机代理、Crawl4AI出口和本地SearXNG代理配置一致，只保存12位出口哈希，不保存公网IP。运行产物位于输出JSON同级的 `<文件名>.artifacts/`，包括带 provider、rank、source type 和抓取时间的搜索结果、来源告警、HTML 抓取内容、`pdf-pages.json` 和 Hermes usage。

前三条查询先调用 AnySearch，并在无结果或失败时降级到 SearXNG；超过每家公司 AnySearch 上限后的查询只调用 SearXNG。实际 AnySearch 调用次数写入结果的 `run_summary.anysearch_queries`。SearXNG 默认使用 Bing、DuckDuckGo、Google CSE 和 Qwant；`--phone-region` 为 `CN`、`KR` 或俄语区国家代码时，分别在同一请求中追加 Baidu、Naver 或 Yandex，不增加 AnySearch 预算，也不改变候选保留逻辑。来源故障不会被记录成“没有联系人”，而是写入 `search-warnings.json`。只要官网或其他来源仍可用，任务可以继续。

官网发现会读取 `robots.txt` 中声明的 Sitemap 以及默认 `/sitemap.xml`，优先选择团队、管理层、公司介绍、新闻和联系页面。只有 CRM 网址本身能与公司名匹配时，Sitemap 页面才会直接进入抓取队列；否则先抓首页，再从首页发现同域联系页，避免错误的 CRM 网址占满抓取预算。单个网页抓取失败时会有界地重试 1 次。

同域 PDF 和 `filetype:pdf` 搜索结果会交给 pypdf，而不会再发送给浏览器。每家公司最多处理 3 个 PDF，单文件最多 15 MB、前 60 页；加密、损坏、超限或纯扫描件会记录错误但不终止任务。首版不做 OCR。

抓取前会先过滤搜索结果：同域页面需要 CRM 网址本身可识别，或搜索结果精确出现公司名；外部页面必须同时包含公司名称，并且 URL 本身包含公司名称或来自受信任的企业资料、工商、贸易或 LinkedIn 域名。公司名校验会忽略大小写、重音、商标符号和法定后缀（包括西班牙 `S.L.`），并接受输入名称中明确写出的括号别名，不接受任意局部匹配。Hermes 输出的候选必须有目标公司的当前任职证据。证据来源必须是本次成功抓取页面、成功提取的 PDF，或通过“LinkedIn 个人页 + 精确公司名 + Current/Present/at company”预筛的 AnySearch `search_excerpt`；后者的 LinkedIn 状态强制为 `probable`。母集团职位或其他未经预筛的 probable 证据会进入 `validation_rejections`。

## SearXNG 管理

当前配置只绑定 `127.0.0.1:18080`。默认配置通过宿主机 `7897` 代理访问搜索引擎；本机可在 `deploy/searxng/.env` 中设置 `SEARXNG_SETTINGS_PATH=./settings.local.yml`，让不同引擎使用各自的已验证出口。`settings.local.yml` 会被 Git 忽略，避免提交本机代理认证信息。

本机当前分流为：Bing 和 DuckDuckGo Web 使用全局 `7897`；Google CSE 和 Qwant 使用 Clash Verge 的独立认证 listener。旧 DuckDuckGo 引擎因所有已测出口均触发 CAPTCHA，已由 DuckDuckGo Web 替换。Clash 订阅更新后若固定节点名称失效，需要重新验证并更新 listener。

```bash
# 查看状态
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml ps

# 停止
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml down
```

## 输出约束

- LinkedIn：只收集公开可见的个人主页链接，不自动登录或互动；AnySearch 摘要来源保存在 `search-evidence.json`，不会伪装成直接页面抓取。
- 邮箱：公开出现的地址记为 `observed`；Hermes 推测的地址必须标记为 `guessed`。
- 电话：使用 E.164 保存，并分别记录号码格式状态和来源；CRM 国家字段中已观察到的中英文国名（日本、越南、韩国、西班牙、泰国）会转为对应电话区域；带 Fax、Telefax、Facsimile 或 Télécopie 标签的传真不会计为电话。
- WhatsApp：只有来源明确标注 WhatsApp，或出现对应的 `wa.me` / `api.whatsapp.com` 链接时才是 `verified`；普通手机号保持 `unknown`。
- 未绑定到个人的联系方式只从企业专属页面，或同时包含精确公司名和明确联系方式的紧凑搜索摘要生成，并按渠道和值去重；仅在大型目录页正文出现公司名，不足以把站点页脚电话或邮箱归给该公司。CRM 网站为目录页时，精确公司联系人摘要可作为 `probable` 公共联系方式。PDF 中未绑定到具体人员的孤立联系方式仍会被排除，避免把监管机构或文档签发方号码误认为公司号码。Hermes 不能自行补写无来源联系方式。
- 耐火材料工程师、冶金工程师、技术经理等技术影响者应保留，不仅限采购岗位。

## 测试

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
```

## 本地展示页

FastAPI 展示服务读取 `outputs/` 下的结果，并支持按名称查询 Twenty CRM、选择公司后创建本地快照并启动联网任务。CRM 查询通过本机 `psql` 客户端使用参数化 SQL、`BEGIN READ ONLY` 和 10 秒超时；Twenty 凭据不会传给浏览器或搜索子进程。页面不需要 Node.js 或前端构建工具，默认监听 `0.0.0.0:18181`，可由同一内网设备访问：

```bash
.venv/bin/key-person-dashboard \
  --env-file .env \
  --env-file /path/to/twenty-local.env
```

环境文件需要提供 `TWENTY_DB_HOST`、`TWENTY_DB_PORT`、`TWENTY_DB_NAME`、`TWENTY_DB_USER`、`TWENTY_DB_PASSWORD` 和 `TWENTY_WORKSPACE_SCHEMA`；可同时在另一个环境文件中提供代理、AnySearch 和 Hermes 配置。公司输入至少 2 个字符，点击匹配项后才会开始联网搜索；CRM 中没有的公司可手动输入。快照保存在 `inputs/web-YYYYMMDD/`，结果保存在 `outputs/web-YYYYMMDD/`，搜索失败日志位于同批次 `inputs` 目录。

任务状态保存在 `state/jobs.sqlite3`，任务 Runner 独立于展示服务运行。页面每 2 秒读取队列状态，首屏只汇总当前批次，展示进度、排队、网络预检、搜索、抓取、Hermes 分析、有新结果、无新增联系人、公开来源不可用和执行失败。结果区默认只显示当前批次的输出，也可用“结果范围”下拉框查看历史日期批次。单家公司启动区和结果公司卡片默认折叠。“无新增联系人”是正常完成，不进入结果列表；“公开来源不可用”保持可重试。刷新页面或重启 dashboard 都不会丢失任务。Runner 心跳超过 5 分钟未更新时会标记失败，避免永久显示“运行中”。

## CRM 批量挖掘

批量脚本默认读取 CRM 中未删除、名称和官网均非空的公司，使用 UUID 游标稳定分页；已经存在于任务库的公司会跳过。默认单并发累计有效运行 48 小时，停止时间到达后不再创建新任务，已经启动的任务仍会完成。Mac 休眠期间进程和网络都会暂停，这段时间不计入有效运行时长；唤醒后先进入 10 秒恢复宽限期，避免把刚恢复的子任务误判为心跳失联。页面每 2 秒显示累计有效时长、剩余时长和恢复状态：

```bash
nohup .venv/bin/key-person-batch \
  --env-file /path/to/twenty-local.env \
  --env-file .env \
  --duration-hours 48 \
  --workers 1 \
  > state/batch.log 2>&1 &
```

启动前可只读预览前三家公司，不创建任务：

```bash
.venv/bin/key-person-batch \
  --env-file /path/to/twenty-local.env \
  --env-file .env \
  --dry-run --max-companies 3
```

`--workers` 支持 1–4；建议先使用 1，确认搜索源、VPN 和 Hermes 连续稳定后再提高。`--max-companies N` 可设置公司数量上限，默认 0 表示只受 48 小时时间限制。批量脚本和 dashboard 默认共享同一个 `state/jobs.sqlite3`，所以内网页面可以实时查看运行情况。

CRM 有效联系人少于 4 条时，快照会记录 `target_contact_count=4`，该任务增加管理层、工程师和官网公共联系方式查询，并把最大抓取页面数从 20 提高到 30；其他公司每次也至少要求 1 个新的可联系渠道。快照同时保存 CRM 现有联系人的姓名、LinkedIn、邮箱和电话，仅用于本地精确去重；新结果会先剔除与 CRM 重复的人员和公共渠道，再计算目标数量。最终目标按“至少有一个 LinkedIn/邮箱/电话的人员 + 未归属公共邮箱/电话/WhatsApp”去重计数；公共号码的电话和 WhatsApp 不重复计数。目标是尽量达到 4 个，不会为了凑数编造联系人、降低当前任职证据或把公共联系方式强行绑定到个人。去重后零可联系项但公开来源已正常检查时，标记为“无新增联系人”并正常完成；只有搜索与抓取来源不可用时才保持失败和可重试。

本机可打开 `http://127.0.0.1:18181`，其他内网设备使用本机局域网 IP，例如 `http://192.168.x.x:18181`。端口冲突时可使用 `--port 18182`，只允许本机访问时使用 `--host 127.0.0.1`。当前页面没有登录认证，局域网内能够访问该端口的设备都可以查询 CRM 公司并启动任务。当前直连 PostgreSQL 是本机已有凭据下的可验证实现；取得稳定的 Twenty API Token 后，可将 CRM 查询替换为 REST/GraphQL，后续快照与搜索流程无需改变。
