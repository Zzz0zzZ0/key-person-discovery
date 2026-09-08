# Key Person Discovery

从公司画像出发，优先通过 AnySearch 发现公开网页并在无结果或失败时降级到 SearXNG，同时主动读取官网 Sitemap；使用 Crawl4AI 并发抓取 HTML、使用 pypdf 提取文本型 PDF，最后由当前 Hermes 主 Agent 输出带来源证据的联系人候选。

命令行只读取 JSON 公司快照；本地展示页可通过 PostgreSQL 只读事务查询 Twenty 公司并创建快照，但不会写 Twenty CRM，不会自动登录 LinkedIn，也不会探测 WhatsApp 账号是否注册。所有结果都需要人工复核。

项目边界固定为本 README 所在的仓库根目录（目录可以命名为 `key-person-discovery` 或 `key-search`）：代码保存在 `src/`、`web/`、`deploy/` 和 `tests/`，运行输入保存在 `inputs/`，结果保存在 `outputs/`，任务状态与日志保存在 `state/`。CLI、dashboard 和 batch 会拒绝把输出、输入或状态库指向项目目录之外。Twenty PostgreSQL、搜索服务和 Hermes 是外部依赖，但不会接收本项目的代码或结果文件。

## 当前版本（2026-09-08）

已实现官网迁移核验、联系页优先发现、具名渠道补搜和按失败原因分配追加查询（P0–P3），并支持 macOS 休眠/断网恢复与 Dashboard 诊断。当前自动化测试 **169 项通过**；历史实验只代表所列样本，不表示联系人普遍增长。仓库包含源码、测试与部署模板；新的 `inputs/`、`outputs/`、`state/` 运行数据及环境文件仅保存在本地。历史已经跟踪的运行文件不会因 `.gitignore` 自动移除，本次提交不更新它们。

以下实验报告和冻结回放脚本的 `state/...` 路径是本机审计索引，不随本次源码提交发布；新检出的仓库使用“测试”一节中的自动化测试即可验证代码。

## 运行条件

- Python 3.11+
- AnySearch（API Key 可选，匿名访问限额较低）
- 可返回 JSON 的 SearXNG 实例，作为 AnySearch 的降级来源；本项目已提供仅监听本机 `18080` 端口的配置
- 已配置的 Hermes 主 Agent；默认命令为 `/Users/acelerzbw/.local/bin/hermes`

```bash
git clone https://github.com/Zzz0zzZ0/key-person-discovery.git
cd key-person-discovery
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium

docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml up -d
export KEY_PERSON_PROXY_URL=http://127.0.0.1:7897  # 按本机代理地址调整
export HERMES_COMMAND="$HOME/.local/bin/hermes"  # 指向已配置、可执行的 Hermes
# 可选：export ANYSEARCH_API_KEY=...
.venv/bin/key-person-discovery \
  --company examples/aceler.json \
  --output outputs/aceler.json \
  --phone-region CN
```

CLI 默认每家公司最多调用 AnySearch 5 次；其余查询直接使用 `http://127.0.0.1:18080` 的 SearXNG。官网联系页和联系方式密集型 PDF 会优先占用现有抓取名额，因此默认流程不再运行第 6–7 次姓名任职验证；显式设置为 7 时仍保留人工对照能力。可通过 `KEY_PERSON_ANYSEARCH_MAX_QUERIES` 调整上限，设置为 `0` 可完全禁用 AnySearch；如需覆盖 SearXNG 地址可设置 `SEARXNG_URL`。`KEY_PERSON_PROXY_URL` 会显式传给 AnySearch、官网 Sitemap 发现和 Crawl4AI；未设置时回退到 `HTTPS_PROXY` 或 `HTTP_PROXY`。如果 Hermes 不在默认位置，设置 `HERMES_COMMAND`。

### 联系人不足时补搜

CLI、Dashboard 和批处理默认在首次分析、任职证据校验及 CRM 去重后检查新增可联系人员数量。未达到公司快照的 `target_contact_count`（未指定时为 1）时，在来源仍可用时最多追加 3 条人员查询：按主要缺口选择具名渠道补全、当前任职核验或岗位发现。岗位查询使用现有公司名称别名及去法定后缀名称，覆盖采购、技术/生产和管理层。已知电话区域对应德、法、西、葡、中、日、韩语时追加当地职位词。不推测品牌名，不降低任职和联系方式归属要求。

`KEY_PERSON_PEOPLE_SEARCH_MAX_QUERIES` 控制补搜上限（0–6，默认 3；0 恢复原流程）。补搜有独立的 AnySearch 预算，因此默认总上限是基础 5 次 + 补搜 3 次，不含独立海关预算。`KEY_PERSON_ANYSEARCH_MAX_QUERIES=0` 时补搜也只使用 SearXNG。直接调用 Python `discover()` 的既有调用者保持原行为，需要显式传入 `people_search_limit=3` 启用。

补搜最多抓取 6 个新 HTML 页面，且仍受本次 `max_urls` 总额度约束；仅出现新增有效证据时再调用一次 Hermes。新证据优先进入提示词，第二次分析失败保留第一轮结果，成功则合并两轮经过校验的人员及联系方式。原始人员不因第二轮模型遗漏而丢失。公共邮箱、总机、待核验人员、仅有推测邮箱的人员以及 CRM 已有人员的新渠道均不计入“新增可联系人员”，原有联系方式统计继续保留。

`run_summary` 新增 `new_contactable_people`、`people_before_topup`、`people_topup_gain`、`people_search_queries` 和 `people_anysearch_queries`。页面单独展示新增可联系人员及补搜增益。`people-baseline.json` 保存触发补搜前结果，`people-search.json` 保存补搜查询和前后人数；两次模型用量分别保存在 `usage.json` 和 `usage-people-topup.json`，核算时应相加。

针对性回归命令：`.venv/bin/python -m unittest discover -s tests -p test_people_recall.py`。真实公司试验产物位于 `state/people-recall-20260907/`，属于本地运行数据。

2026-09-07 免费来源审计见 本地实验报告（仅本地：`state/free-sources-20260907/report.md`）：5 家低产出公司 + EKW 对照，人工辅助提取在 EKW 补回 17 名经当前 CRM 去重的相关业务联系人，全部姓名已在历史抓取文本中；其余 5 家本轮没有新增可直接联系人员。该报告记录修复前的实验，后续生产修复与配对验证见下文；不能将技术销售联系人数量视为采购决策人增益。

### 官网联系人归属与邮箱解析

Crawl4AI 现在从已获取的 HTML 中保留独立的联系人段落及 `mailto:`、`tel:` 证据，使用现有 BeautifulSoup 依赖，不追加网页请求。当前支持带 H2–H6 标题和联系链接、长度不超过 2,500 字符、最多向上 4 层的单标题容器；其他布局继续走原有正文分析。只将同站已成功抓取的卡片优先送入 Hermes，总正文预算保持 100,000 字符。卡片保留相邻章节标题，人物、任职和目标公司关系仍由分析与原有校验流程确认。

明确的 `(at)`、`[at]` 邮箱写法会在信号、提示词和证据归属校验中统一解析。已有卡片结构时，不能仅凭同一网页上的两个值就将其他人的电话或邮箱附到当前人员。显示地址与链接地址不一致的渠道标为 `conflicting`，从具名联系人、未归属渠道及后续补搜合并结果中隔离；不会作为推测邮箱重新补入。传真继续排除，普通手机号不标为已验证 WhatsApp。

`pages.json` 增加 `contact_blocks`、`contact_conflicts`；每次分析保存 `contact-conflicts.json`。`run_summary` 增加 `contact_card_blocks`、`contact_channel_conflicts`、`excluded_role_candidates`。目标人数是最低覆盖要求，不限制有证据支持的人员数量；技术销售作为业务转介联系人保留。明确的人事或网站维护岗位进入 `excluded_candidates` 供复核，不计目标人员；同时兼任采购、技术或经营职责的岗位保留。无姓名部门不作为人员。

验证命令：`.venv/bin/python -m unittest discover -s tests -q`（140 项通过）。EKW 原始公司配置与同页对照中，去重后的个人邮箱从 0 增至 12 个；统一剔除人事、已确认 CRM 重复人员及冲突渠道后，可联系人员从 20 增至 21 名。修复后的结果也覆盖此前人工审计的 17 名历史遗漏人员，但没有新增同页姓名，不能把 17 当作本次 A/B 增益。证据与限制见 修复验证报告（仅本地：`state/contact-attribution-fix-20260907/review.md`）。本地验证数据不写入 CRM，旧任务结果不会被自动重写，重新运行任务即可使用修复后的流程。

后续 12 家公司扩大验证（仅本地：`state/expanded-contact-test-20260907/report.md`） **未通过扩大验收**：11 家完成同页对照、1 家官网失败，新增有效入口集中在 Schmiedeberger 的 2 个具名部门邮箱；另有 4 人共享公共邮箱转介，独立个人邮箱人数未增加。140 项现有测试通过，但 3 项补充边界检查复现了部分卡片误挡正文、人事职称漏过滤和具名 WhatsApp 归属丢失。冻结证据、逐家结果及复现脚本位于 `state/expanded-contact-test-20260907/`；本轮只扩大测试，生产代码未变。

上述问题已在 边界修复与冻结重放（仅本地：`state/contact-boundary-fix-20260907/report.md`） 中修复：卡片只约束其覆盖的人员或渠道，未覆盖人员仍可使用正文直接引用；德语人员专员、人员事务职称及纯人事副总裁被排除，兼采购/生产职责仍保留；抓取页面上的具名 WhatsApp 链接保留姓名与号码的实际关联，后续补入也使用同一规则，号码不会再同时计为未归属 WhatsApp。目标公司只出现在页面链接中时，相关人员转入待核验，不能仅凭集团导航确认任职。

该轮验证为 **145 项测试及 3 项原冻结失败用例全部通过**。12 家样本保留原页面、CRM 快照和模型输出，11 家各重放前后两份输出（22 次），1 家原抓取失败仍单列；恢复 2 名待核验人员的 WhatsApp，旧模型输出中的 2 名人事人员被排除，其余已保留渠道无变化。上一轮修复后样本的已确认新增人数保持 7，不能把恢复的 2 个号码当作已确认目标公司增益。EKW 回归保持 21 名新增可联系人员、12 个个人邮箱及 3 处冲突隔离。本轮没有重新抓取或调用模型；冻结重放命令：`.venv/bin/python state/contact-boundary-fix-20260907/replay.py`。

### 官网定位与联系页覆盖（P0＋P1）

抓取结果保存请求地址、最终地址、页面标题及链接文字。官网入口每次新抓取一次，避免 Crawl4AI 当前磁盘缓存丢失重定向地址；其他页面继续使用缓存。跨域迁移需要页面标题/主标题中的企业证据，不能只凭跳转或集团导航确认。跳到错误企业时，仅尝试具名目标企业链接，实际抓取并核验后再采用；错误页面单独存入 `website-routing-pages.json`，不供联系人提取使用。集团 `/companies/`、`/empresas/`、`/subsidiaries/` 企业页限制在对应子路径。迁移决策见 `website-resolution.json`，Dashboard 显示核实状态。

联系页发现结合 sitemap、URL 和实际链接文字，补充德语、葡语、意大利语、芬兰语等常见词；人员、采购、联系及法律声明页面优先于普通新闻，保持原抓取上限与 PDF 上限。联系方式放进姓名字段的模型输出会被拒绝；有证据的邮箱仍保留为未归属公共渠道。

20 家新公司对照报告（仅本地：`state/official-coverage-20260907/report.md`）：每侧每家公司最多 8 个网页、3 个 PDF，0 次搜索查询；共享 URL 的采集结果和 CRM 快照一致。16 家完成配对，3 家无可用抓取证据，1 家基线处理失败；共 34 次 MiniMax-M2.7 调用。成功网页 **54 → 73**，完整配对中的具名渠道 **2 → 2**、新增可联系人员 **1 → 1**，**未达到至少 3 家获益及渠道增长 20% 的目标**。新增姓名缺少渠道或命中 CRM；不能把覆盖增长解释为联系人增长。最终 **154 项测试通过**，33 份模型输出通过修复后重放；原 12 家与 EKW 回归保持。该实验未测试 P2；后续结果见下节。

### 已知人员渠道补搜（P2）

优先为已确认任职、非 CRM、缺少渠道的人员执行姓名＋公司、LinkedIn 个人页、PDF 查询，仍受 `people_search_limit` 限制。摘要同时精确提及目标姓名和公司时可以补抓外部页面，但该页面的公共页脚不归属于目标公司；补抓仍最多 6 个网页、总计 3 个 PDF。实际源段落约束姓名与邮箱/电话的绑定，防止模型将法定代表人和单独的公司联系段拼接；唯一、完整姓名且明确当前雇主的 LinkedIn 个人页可以在模型遗漏或可选补搜失败时保留，状态为 `probable`，排除 CRM 已有链接和含糊匹配。

7 家、8 人固定实验报告（仅本地：`state/person-channel-20260907/report.md`）：各 21 次免费 SearXNG 查询，最终共同规则下两侧均补到 **2 名人员的 LinkedIn 线索，个人邮箱/电话净增 0**。旧模型曾漏掉两侧都检索到的 EWW 链接，不能将修复前的 1→2 解释为查询召回提升。成功网页 31→55，模型 tokens 80,141→130,592，**尚未证明来源扩展提高可联系人数**。12 次模型调用中 11 次费用未知；新侧 1 次补搜处理失败保留原结果。另有 2 条待核验个人页（其中 1 条旧查询也找到），单列且不计增量。最终 **162 项测试通过**，历史 12 家与 EKW 回归保持；不扩大查询预算。

### 按失败阶段分配补搜（P3）

补搜前根据实际结果选择下一步：已确认人员缺渠道时使用 P2 具名查询；任职不明时只查姓名和当前雇主；全部人员与 CRM 重复时查其他岗位并排除已知姓名；未提取到目标岗位人员时使用岗位查询。已达目标、已识别的官网归属未确认或没有任何可分析证据时不追加人员搜索。部分抓取失败但仍有可用证据时可以继续补全。初始采集无可分析证据时跳过模型；已有搜索中的合格具名 LinkedIn 会在决定追加预算之前保留。

结果与 `.artifacts/diagnostics.json` 同时保存缺口、文档/人员计数、追加原因、执行查询和阶段错误码。Dashboard 的结果卡片与终态任务展示“本次结果诊断”和下一步，首次分析失败也能显示原因；补搜失败保留原结果。诊断不会复制任意模型输出或凭据。旧任务没有诊断时保持原显示，不自动重写历史输出。网络预检或首次采集在取得诊断输入前退出，仍走现有任务错误与网络恢复机制。

P3 实现与验证报告（仅本地：`state/failure-routing-20260908/report.md`）：**169 项测试通过**，P2 的 2 条 LinkedIn、原 12 家和 EKW 回归保持。20 家冻结证据的计划追加查询 **57→46（减少 19.3%）**；这是离线计划对比，不是实测费用或联系人增长。本轮没有新联网搜索或模型调用，未增加原有查询预算。Dashboard 已完成桌面/手机布局与错误展示检查。

### 海关查询与来源预算

海关数据使用独立的 AnySearch 预算。只有公司快照明确包含 `customs_search_enabled=true` 时才会查询；未被公司匹配模块选中的普通公司保持 0 次。选中后默认最多 2 次，可通过 `KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES` 设置为 0–3。首次查询没有同时匹配目标公司和贸易语义的有效线索时立即停止；命中后才继续核实。结果保存在 `customs` 字段和 `customs-search-results.json`，只添加“海关采购证据”或“海关记录待核实”标签，不改变公司匹配分，也不进入联系人抽取。实际调用数写入 `run_summary.customs_anysearch_queries`。

每次运行前会验证本机代理、Crawl4AI出口和本地SearXNG代理配置一致，只保存12位出口哈希，不保存公网IP。运行产物位于输出JSON同级的 `<文件名>.artifacts/`，包括带 provider、rank、source type 和抓取时间的搜索结果、来源告警、HTML 抓取内容、`pdf-pages.json` 和 Hermes usage。

基础查询在 AnySearch 预算内优先调用 AnySearch，无结果或失败时降级到 SearXNG；超过该预算后的查询只调用 SearXNG。实际 AnySearch 调用次数写入结果的 `run_summary.anysearch_queries`。SearXNG 默认使用 Bing、DuckDuckGo、Google CSE 和 Qwant；`--phone-region` 为 `CN`、`KR` 或俄语区国家代码时，分别在同一请求中追加 Baidu、Naver 或 Yandex，不增加 AnySearch 预算，也不改变候选保留逻辑。来源故障不会被记录成“没有联系人”，而是写入 `search-warnings.json`。只要官网或其他来源仍可用，任务可以继续。

官网发现会读取 `robots.txt` 中声明的 Sitemap 以及默认 `/sitemap.xml`，优先选择团队、管理层、公司介绍、新闻和联系页面。只有 CRM 网址本身能与公司名匹配时，Sitemap 页面才会直接进入抓取队列；否则先抓首页，再从首页发现同域联系页，避免错误的 CRM 网址占满抓取预算。单个网页抓取失败时会有界地重试 1 次。

同域 PDF 和 `filetype:pdf` 搜索结果会交给 pypdf，而不会再发送给浏览器。每家公司最多处理 3 个 PDF，单文件最多 15 MB、前 60 页；加密、损坏、超限或纯扫描件会记录错误但不终止任务。首版不做 OCR。

抓取前会先过滤搜索结果：同域页面需要 CRM 网址本身可识别，或搜索结果精确出现公司名；外部页面必须同时包含公司名称，并且 URL 本身包含公司名称或来自受信任的企业资料、工商、贸易或 LinkedIn 域名。公司名校验会忽略大小写、重音、商标符号和法定后缀（包括西班牙 `S.L.`），并接受输入名称中明确写出的括号别名，不接受任意局部匹配。Hermes 输出的候选必须有目标公司的当前任职证据。证据来源必须是本次成功抓取页面、成功提取的 PDF，或通过“LinkedIn 个人页 + 精确公司名 + Current/Present/at company”预筛的搜索引擎 `search_excerpt`；后者的 LinkedIn 状态强制为 `probable`。母集团职位或其他未经预筛的 probable 证据会进入 `validation_rejections`。

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

- LinkedIn：只收集公开可见的个人主页链接，不自动登录或互动；搜索引擎摘要来源保存在 `search-evidence.json`，不会伪装成直接页面抓取。
- 邮箱：公开出现的地址记为 `observed`；Hermes 推测的地址必须标记为 `guessed`。
- 电话：使用 E.164 保存，并分别记录号码格式状态和来源；CRM 国家字段中已观察到的中英文国名（日本、越南、韩国、西班牙、泰国）会转为对应电话区域；带 Fax、Telefax、Facsimile 或 Télécopie 标签的传真不会计为电话。
- WhatsApp：只有来源明确标注 WhatsApp，或出现对应的 `wa.me` / `api.whatsapp.com` 链接时才是 `verified`；普通手机号保持 `unknown`。
- 未绑定到个人的联系方式只从企业专属页面，或同时包含精确公司名和明确联系方式的紧凑搜索摘要生成，并按渠道和值去重；仅在大型目录页正文出现公司名，不足以把站点页脚电话或邮箱归给该公司。CRM 网站为目录页时，精确公司联系人摘要可作为 `probable` 公共联系方式。PDF 中未绑定到具体人员的孤立联系方式仍会被排除，避免把监管机构或文档签发方号码误认为公司号码。Hermes 不能自行补写无来源联系方式。
- 耐火材料工程师、冶金工程师、技术经理等技术影响者应保留，不仅限采购岗位。

## 测试

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -q
```

当前测试共 **169 项**，覆盖来源核验、渠道归属、补搜预算、失败诊断、CRM 去重、任务恢复和旧结果兼容。测试使用固定数据与替身，不启动真实搜索或模型任务。

## 本地展示页

FastAPI 展示服务读取 `outputs/` 下的结果，并支持按名称查询 Twenty CRM、选择公司后创建本地快照并启动联网任务。CRM 查询通过本机 `psql` 客户端使用参数化 SQL、`BEGIN READ ONLY` 和 10 秒超时；Twenty 凭据不会传给浏览器或搜索子进程。页面不需要 Node.js 或前端构建工具，默认监听 `0.0.0.0:18181`，可由同一内网设备访问：

```bash
.venv/bin/key-person-dashboard \
  --env-file /path/to/twenty-local.env \
  --env-file .env
```

环境文件需要提供 `TWENTY_DB_HOST`、`TWENTY_DB_PORT`、`TWENTY_DB_NAME`、`TWENTY_DB_USER`、`TWENTY_DB_PASSWORD` 和 `TWENTY_WORKSPACE_SCHEMA`；可同时在另一个环境文件中提供代理、AnySearch 和 Hermes 配置。公司输入至少 2 个字符，点击匹配项后才会开始联网搜索；CRM 中没有的公司可手动输入。快照保存在 `inputs/web-YYYYMMDD/`，结果保存在 `outputs/web-YYYYMMDD/`，搜索失败日志位于同批次 `inputs` 目录。

任务状态保存在 `state/jobs.sqlite3`，任务 Runner 独立于展示服务运行。页面每 2 秒读取队列状态，首屏只汇总当前批次，展示进度、排队、网络预检、等待网络恢复、搜索、抓取、Hermes 分析、有新结果、无新增联系人、公开来源不可用和执行失败。结果区默认只显示当前批次的输出，也可用“结果范围”下拉框查看历史日期批次。单家公司启动区和结果公司卡片默认折叠。“无新增联系人”是正常完成，不进入结果列表；“公开来源不可用”保持可重试。刷新页面或重启 dashboard 都不会丢失任务。Runner 心跳超过 5 分钟且对应进程已经不存在时才会标记失败，避免合盖唤醒时误判仍存活的任务。

## CRM 批量挖掘

批量脚本默认读取 CRM 中未删除、名称和官网均非空的公司，使用 UUID 游标稳定分页；已经存在于任务库的公司会跳过。默认单并发累计有效运行 48 小时，停止时间到达后不再创建新任务，已经启动的任务仍会完成。Mac 休眠期间进程和网络都会暂停，这段时间不计入有效运行时长；唤醒后先进入 10 秒恢复宽限期。若网络尚未恢复，任务保持 `waiting_network` 并每 30 秒检查一次出口，网络恢复后再宽限 10 秒并重试同一任务，不创建重复任务；所有活跃任务都在等待网络时，批次有效时长暂停累计。Runner 在 macOS 上会自动通过系统 `/usr/bin/caffeinate -i -s` 阻止任务执行期间的空闲睡眠，但合盖强制睡眠仍由 macOS 控制。页面每 2 秒显示累计有效时长、剩余时长和恢复状态：

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

CRM 有效联系人少于 4 条时，快照会记录 `target_contact_count=4`，该任务增加管理层、工程师和官网公共联系方式查询，并把最大抓取页面数从 20 提高到 30；其他公司每次也至少要求 1 个新的可联系渠道。快照同时保存 CRM 现有联系人的姓名、LinkedIn、邮箱和电话，仅用于本地精确去重；新结果会先剔除与 CRM 重复的人员和公共渠道，再计算目标数量。人员补搜目标按经任职核验、非 CRM 已有且至少具备一个非推测 LinkedIn/邮箱/电话的人员计数。旧的联系方式指标另行保留，按“具名可联系人员 + 未归属公共邮箱/电话/WhatsApp”统计；公共渠道不会补足新增人员目标，公共号码的电话和 WhatsApp 不重复计数。目标是尽量达到 4 个，不会为了凑数编造联系人、降低当前任职证据或把公共联系方式强行绑定到个人。去重后零可联系项但公开来源已正常检查时，标记为“无新增联系人”并正常完成；只有搜索与抓取来源不可用时才保持失败和可重试。

本机可打开 `http://127.0.0.1:18181`，其他内网设备使用本机局域网 IP，例如 `http://192.168.x.x:18181`。端口冲突时可使用 `--port 18182`，只允许本机访问时使用 `--host 127.0.0.1`。当前页面没有登录认证，局域网内能够访问该端口的设备都可以查询 CRM 公司并启动任务。当前直连 PostgreSQL 是本机已有凭据下的可验证实现；取得稳定的 Twenty API Token 后，可将 CRM 查询替换为 REST/GraphQL，后续快照与搜索流程无需改变。

### macOS 登录后自动运行

`deploy/macos/com.aceler.key-person-dashboard.plist.example` 是 Dashboard 的 LaunchAgent 模板。将其中所有 `__PROJECT_DIR__` 替换为项目绝对路径，并把 `__CRM_ENV_FILE__` 替换为只读 Twenty 环境文件的绝对路径，保存到 `~/Library/LaunchAgents/com.aceler.key-person-dashboard.plist` 后先确保项目的 `state/` 日志目录存在，再加载：

```bash
mkdir -p state  # 在仓库根目录执行
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.aceler.key-person-dashboard.plist"
launchctl enable "gui/$(id -u)/com.aceler.key-person-dashboard"
launchctl kickstart -k "gui/$(id -u)/com.aceler.key-person-dashboard"
```

LaunchAgent 会在登录后启动 Dashboard，并在它异常退出时重新拉起；系统真正睡眠期间不会执行 Python，开盖后由原进程或 LaunchAgent 恢复。不要为批处理配置无条件 `KeepAlive`，否则正常完成的批次也会被重新启动。

macOS 会限制后台 LaunchAgent 访问 `Documents`、`Desktop` 等受保护目录。如果项目位于 `Documents`，直接加载模板可能得到 `Operation not permitted`；应先把仓库迁移到例如 `~/Projects/key-search`，或在“系统设置 → 隐私与安全性 → 完全磁盘访问权限”中明确允许项目使用的 Python 解释器。不要通过关闭系统保护来绕过该限制。
