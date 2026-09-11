# Key Person Discovery

从公司画像出发，优先通过 AnySearch 发现公开网页并在无合格结果或失败时降级到 SearXNG，同时主动读取官网 Sitemap；使用 Crawl4AI 并发抓取 HTML、使用 pypdf 提取文本型 PDF，最后由当前 Hermes 主 Agent 输出带来源证据的联系人候选。

命令行只读取 JSON 公司快照；本地展示页可通过 PostgreSQL 只读事务查询 Twenty 公司并创建快照，但不会写 Twenty CRM，不会自动登录 LinkedIn，也不会探测 WhatsApp 账号是否注册。所有结果都需要人工复核。

项目边界固定为本 README 所在的仓库根目录（目录可以命名为 `key-person-discovery` 或 `key-search`）：代码保存在 `src/`、`web/`、`deploy/` 和 `tests/`，运行输入保存在 `inputs/`，结果保存在 `outputs/`，任务状态与日志保存在 `state/`。CLI、dashboard 和 batch 会拒绝把输出、输入或状态库指向项目目录之外。Twenty PostgreSQL、搜索服务和 Hermes 是外部依赖，但不会接收本项目的代码或结果文件。

## 当前版本（2026-09-11）

已实现官网迁移核验、来源质量筛选、具名渠道补搜、任职证据核验和按失败原因分配追加查询（P0–P3），并支持 macOS 休眠/断网恢复与 Dashboard 诊断。当前自动化测试 **212 项通过**；历史实验只代表所列样本，不表示联系人普遍增长。仓库包含源码、测试与部署模板；新的 `inputs/`、`outputs/`、`state/` 运行数据及环境文件仅保存在本地。历史已经跟踪的运行文件不会因 `.gitignore` 自动移除，本次提交不更新它们。

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

### 来源质量第一轮（2026-09-09）

默认搜索现在先进行结果分级，再使用每来源原有的 5 条名额（宽搜实验的 SearXNG 仍为 10 条）。SearXNG 保留同一次响应中完整的解析结果，因此排除前排异常后可以补回后面的合格页面；不增加翻页、单来源请求次数或 AnySearch 返回条数上限。AnySearch 与 Tavily 也保留实际响应中的完整解析结果，不额外申请更多结果。

- **来源可追溯**：`SearchResult.engines` 保存 SearXNG 实际引擎列表；没有该字段的旧数据保持兼容。`search-responses.json` 保存每次取得的结果池及 SearXNG 原始响应，`search-quality.json` 保存逐条分级、原因与来源。接口失败仍写入 `search-warnings.json`，不伪装成质量过滤。
- **明确异常拒绝**：无效 URL、登录/注册入口、完整回显带搜索语法的查询标题，以及违反单一正向 `site:` 域名或路径限定的结果。多 `site:`、否定语法不套用单一域名规则。
- **相关性分级后降级**：包含公司别名或属于输入官网路径范围的结果优先；其余标记 `pending`，不因陌生域名或摘要短就硬删。没有优先结果时继续下一来源；所有来源都没有优先结果时，按原顺序保留最多 5 条待核验线索。原有网页准入、任职和渠道归属验证继续生效，`eligible` 仅表示通过搜索层初筛。

AnySearch 基础、人员补搜和海关预算，以及抓取/模型预算配置均不变。无合格结果时可能比过去多执行已有降级链中的 SearXNG 请求，这不等于实际调用次数或费用必然不变。独立海关检索仍使用原有贸易语义筛选。

本地报告：`state/source-quality-20260909/report.md`。3 家预先固定公司、9 次 SearXNG 查询共享原始响应，人工复核“公司相关且符合查询范围”的已选结果 **17/45 → 26/30**；三个 LinkedIn 限定查询全部被过滤为空。还存在同名地理实体误入和招聘页占位问题，因此这是结果筛选改善，**没有验证新增可联系人员**。另行抓取的两个补回第三方页面及一个历史快照缺页均成功；这不代表它们全部通过生产抓取准入。

**176 项测试通过**；P2 两条 LinkedIn、原 12 家中的 22 份可回放模型输出、EKW 的 21 名新增可联系人员与 12 个个人邮箱保持；3,139 个历史文件哈希未变。SCHWARZ 的新选页面不在原冻结抓取池中，保留该离线回放缺口，另存联网检查结果，不回填旧实验。单独请求 Qwant 复现 10/10 条查询回显，确认异常已存在于进入本项目之前的单引擎响应；直接访问上游的代理探针连接被拒，尚不能断言根因在上游网站、出口或 SearXNG 解析器。

### 来源质量第二轮：页面价值与引擎贡献（P1，2026-09-09）

在第一轮分级基础上，单词公司名仅命中名称时进入 `pending`，需要企业/岗位背景、输入官网范围或品牌域名联系页等额外信号才优先使用；这是相关性初筛，不代表已核验公司身份。陌生域名和简短摘要仍保留待核验机会，不加入特定公司黑名单。

每个响应内按稳定优先级排序并在截取前去重：官网团队/联系页 → LinkedIn 个人页 → 具名岗位证据 → 官网其他页 → 企业资料及公司专属行业文章 → 人员目录 → 招聘页 → 表单确认页。同级保持来源原顺序；全部只有招聘页时仍可保留背景材料。抓取队列保留官网首页、联系页的原有优先级，其余按页面价值排序，不增加抓取名额。

对满足公司关联的具名岗位页，及 URL 明确指向该公司且包含岗位/采购主题的新闻、访谈、案例文章，允许使用现有抓取预算；明确离职或冲突雇主线索不会通过这个新增入口。实际正文、当前任职、个人渠道及 CRM 去重仍由后续验证决定。普通行业文章只在正文/摘要偶然提到公司，不能因此获得这个新增抓取资格。

正常返回的结果新增 `source_quality`，同级产物新增 `engine-quality.json`。按实际来源统计观察结果、初筛状态、去重入选页面、独有入选页面，以及来源 URL 能对应到的已保留人员和渠道。多引擎共同返回一个页面会共享归属，不能相加当作新增联系人；去重后的结果可从本次审计记录恢复已观察到的多来源归属。没有引擎信息的旧数据标记未知；未执行渠道评估时为 `null`，不会冒充 0。该轮只记录贡献，不自动调整引擎权重；2026-09-11 另加入任务内重复失败暂停，见下文。

本地报告：`state/source-value-20260909/report.md`。固定的 9 条旧响应在 P0/P1 两侧各保留 30 条结果；人工审查中，同名百科误入 **4→0**，明确公司相关 **26→28**，另有 **2 条公司主体未确认**。这一响应池曾用于发现问题，不是新的盲审留出集。另对 Sibelco、Calderys、Imerys 执行 3 次预先固定查询，官网联系/站点页进入前列，体育、系统问答、照片冲印等无关结果被移开；没有新模型提取或联系人增量结论。

**182 项测试通过**；P2 的两条 LinkedIn、原 12 家中 22 份可回放输出及 EKW 的 21 名可联系人员、12 个个人邮箱保持；3,834 个历史文件未变。原 SCHWARZ 冻结缺页与 Paul Gläser 模型处理失败继续单列保留。两个上一轮已经成功抓取的 RHI 采购专题/案例页面现在通过生产抓取入口，但未用其重新调用模型。本轮 3 次联网搜索、0 次模型或付费搜索 API 调用，沿用 EKW 脚本执行一次 CRM 只读查询，无 CRM 写入。

### 来源质量完整流程配对测试（2026-09-09）

本地报告：`state/source-e2e-20260909/report.md`。固定 Refratechnik、Zircar Ceramics、Capital Refractories 三家新样本，比较提交 `1caebbc` 与当前 P0＋P1 来源质量版本。两侧采用相同预算和 CRM 快照，重合查询/网页共享响应；6 次完整流程、10 次 MiniMax-M2.7 真实提取全部完成，没有复用旧模型输出。

**尚未验证联系人净增量**：程序新增可联系人数 **3→3**；剔除只有 2020 年旧任职证据的一人后，人工保守复核为 **2→2**。改进版补回 Nigel Robson 的 LinkedIn，但 Tim Hall 从已确认集合退到待核实，因此不能说原人员全部保持。保留搜索结果 **340→215**，模型总 token **182,021→191,747（+5.3%）**；减少结果条数不等于联系人或成本改善。

冻结实验定位了四个问题，后续修复见下一节：长导航占满每页 20,000 字符，导致联系正文未进入模型；旧 PDF 日期仅从候选引用中检查，遗漏原文发布日期；一条非法 LinkedIn 来源连带剔除同一人的有效 PDF 邮箱/电话；显式历史创始人被标为当前已确认。原始模型输出、真实提示词、校验拒绝原因及人工复核均单独保留。

共享来源池实际执行 36 次 SearXNG 搜索、35 次浏览器 URL 尝试、7 次 PDF 获取、3 次官网发现调用；浏览器子请求与官网发现内部 HTTP 请求未逐项计数。付费搜索 API 调用和 CRM 写入均为 0；模型费用有 5 次未知，不能算作免费。**182 项测试通过，3,834 个历史实验文件未变**。本轮只新增实验产物和文档，没有根据这三家公司调参；只读重算命令为 `.venv/bin/python state/source-e2e-20260909/analyze.py`。

### 正文与任职证据修复（2026-09-09）

本地报告：`state/evidence-fix-20260909/report.md`。上述四项缺陷已修复：长导航前缀移至正文之后，contact cards 优先，剩余字符预算在文档间分配；旧 PDF 的明确出版/更新日期从实际正文检查；非法渠道来源只剔除该渠道并记录 `contact_validation_rejections`；显式历史/前任职位和只有历史前身公司证据的人转入待核实。原网页、每页 20,000/总计 100,000 字符边界及人物证据 URL 校验继续保留，没有增加依赖。

**191 项测试通过**。10 份原始模型输出重评中，Tim Hall 的 1 个邮箱、1 个电话恢复为待核实渠道；Alan Benson 和历史创始人退出当前已确认集合；其他个人渠道未减少。旧 22 份回放、P2 两条 LinkedIn、EKW 的 21 名可联系人员/12 个邮箱保持上一轮验收结果，3,834 个历史文件及 234 个原始配对实验文件未变。

另用冻结证据进行了两次真实 MiniMax-M2.7 首轮重提取：Refratechnik 的实际正文来源数 **5→10**，Capital 为 **5→19**；Capital 保留 Tim/Nigel 两条 probable LinkedIn，Refratechnik 没有新增可联系人员。两次总 token **80,023→62,663（−21.7%）**，费用均未知；这是固定证据的首轮重提取，**不代表新公司完整流程已取得净增量**。本轮无新搜索/网页抓取或 CRM 写入。可运行 `.venv/bin/python state/evidence-fix-20260909/revalidate.py` 只读重算原始输出校验；通用回归在 `tests/test_evidence_quality.py`。

### 新公司联系人增量验证（2026-09-09）

本地报告：`state/increment-holdout-20260909/report.md`。固定此前未用于修复的 Gouda、Trent、Resco、Allied，比较四项证据修复前后，完成 8 次完整流程和 8 次真实 MiniMax-M2.7 提取。预算、CRM 快照和重合来源响应一致，期间没有更换公司或调参。

**未验证出净增量**：程序计数 **1→1**；人工确认的 3 家有效目标对照仍为 **1→1**，唯一一条是两版共有的 Jose Martin probable LinkedIn，没有个人邮箱/电话增量。Resco 两版均把正确输入官网替换为同名动物保健企业，必须列作**定位失败**，不能当作正常零召回。全部调用总 token **114,466→108,352（−5.3%）**，不代表联系人增长。

本轮还确认 Gouda 的 `Mr M. Schuchmann` 未匹配 CRM 已有 `M. Schuchmann`，导致重复候选占用补搜名额；具名人数变化不能直接算新联系人。原实验保留当时版本与结论；后续官网选择和称谓去重修复见下一节。

共享池执行 52 次 SearXNG 搜索、47 次浏览器 URL 尝试、1 次 PDF 获取和 4 次官网发现调用；7 次模型费用未知，无付费搜索 API 或 CRM 写入。**191 项测试通过，4,814 个历史文件未变**。只读重算：`.venv/bin/python state/increment-holdout-20260909/analyze.py`。

### 官网身份、称谓去重与图片链接修复（2026-09-09）

本地报告：`state/identity-fix-20260909/report.md`。官网选择支持连写品牌域名，优先保留有搜索证据支持的输入官网及其子公司路径；隐式跨域替换需保留原域名中的品牌词，显式 Website 引用和实际重定向继续走已有核验。CRM 去重、人员合并、人数计数与补搜共用称谓清理，保留首字母和姓名粒子，不把 `M. Green` 自动扩展为某个全名；旧人员的新渠道仍保留但不计为新增人员。网页链接解析保留嵌套图片外层的页面链接，排除图片、样式与脚本文件。

冻结四家公司回放：Resco 恢复正确官网；Gouda 剔除一名 CRM 重复人员，补搜名额转给其他人员；Allied 原有 LinkedIn 保留。另做 Resco 两次独立完整流程，记录仅身份修复和追加图片链接修复的结果，共 2 次真实 MiniMax-M2.7 提取；沿用原查询响应，新增来源另存，不改写原实验。

图片修复后，10 个抓取名额中的图片 **2→0**，有效文档 **6→7**，新读取的 Locations 页提供 **10 个公共电话**（含加拿大分支），另保留 1 个公共邮箱。**新增可联系人员仍为 0**，公共渠道不计作人员增量；CEO 页面未成功提取，3 条人员补搜仍受来源不足限制。两次模型总 token 为 **54,501**，费用均未知，无付费搜索 API 或 CRM 写入。本轮是问题样本的开发验证，不能作为独立留出集的增长证据。

**198 项测试通过**；旧 22 份可回放模型输出与上一轮验收一致，4,814 个历史文件及 278 个原增量实验文件哈希未变。离线审计：`.venv/bin/python state/identity-fix-20260909/analyze.py`；通用回归：`tests/test_identity_recovery.py`。

### 人员全名线索补全（2026-09-09）

本地报告：`state/person-identity-20260909/report.md`。新增 `B.V.` / `N.V.` 法律后缀别名；人员搜索仅在前导拉丁字母首字母形式下放宽为姓氏检索，保留姓名粒子，完整名字继续精确搜索。该搜索词不用于身份键、CRM 去重或 LinkedIn 自动绑定；没有通过猜测扩展姓名。另过滤包含完整多词公司名的职位描述，避免把它当作人员姓名。

Gouda、Trent 两家公司完成 4 次完整配对流程、4 次真实 MiniMax-M2.7 提取。自动新增可联系人员仍为 **0→0**。别名修复让冻结 Qwant 响应中的 **Michel Grootenboer、Edwin Aalbers、Michiel Smedes** 三条真实全名线索进入待核实集合；原配对另有一条职位描述误识别，后续通用过滤已通过冻结响应回放。Gouda 网页证据集合未增加，新的姓氏补搜没有产生可用新证据；Trent 两版均无新增人员。

独立人工网页核查为 Michel 补齐官网职位与公开 LinkedIn 交叉证据，另两人仍待核实，见本地 `manual-leads.json`。人工结果不混入配对指标，也未写入 CRM。本次新 SearXNG 查询观察到 Bing 返回明显无关结果，其他引擎存在解析错误、限流和 CAPTCHA；只证明当前检索链路的响应异常，尚未定位其根因。下一步优先恢复有效检索，并补充官网刊物、企业动态的任职证据覆盖。

模型总 token **68,355→64,268**，3 次费用未知；新增 SearXNG 调用 9、浏览器 URL 尝试 8、官网发现函数 1，独立人工核查另计。**203 项测试通过**，旧 22 份回放与上一轮验收一致，4,814 个历史文件及最近两轮 807 个冻结文件未变。离线重算：`.venv/bin/python state/person-identity-20260909/analyze.py`；回归用例：`tests/test_person_identity_queries.py`。

### 搜索故障与追加证据修复（2026-09-11）

固定两条 Gouda 查询，分别探测 4 个引擎，并直接请求 Bing 对照：Bing 在 SearXNG 和直接响应中均出现无关页面，不能归因于本项目解析；Qwant CAPTCHA、DuckDuckGo Web 解析失败。Google CSE 在探测及 Gouda、RATH 流程中返回相关内容，因此默认保留该来源，但随后 Calderys 触发限流，**免费来源的连续可用性仍有限**。任务内重复失败暂停已生效，CLI 与本机 Docker 使用同一默认配置。

Gouda 开发样本、RATH 和 Calderys 两个新冻结样本，使用相同预算与共享新来源池完成 6 次流程。两版均使用恢复后的来源，因此该配对只比较流程改动，不能用来计算来源恢复的增益。每版每家公司最多 10 个网页、3 个 PDF、3 条人员补搜和 2 次模型提取，AnySearch 为 0。

| 样本 | 基线可联系人数 | 首版修复人数 | 观察 |
| --- | ---: | ---: | --- |
| Gouda | 4 | 1 | 3 人保留在待核验集合；同样的 LinkedIn 摘要在模型中出现任职判定差异 |
| RATH | 8 | 8 | 相同 8 个个人邮箱可对应官网卡片；网页 10→7 |
| Calderys | 0 | 0 | Google CSE 限流，官网 Sitemap 证书校验失败；改进版仍完成官网追加读取 |

该配对**未通过联系人净增量验收**：原始程序计数 12→9，模型 token 123,882→147,791；不能宣称人数或费用改善。随后修正官网证据被第三方目录挤占的问题：Gouda 补充验证实际读到 Publications 页的具名任职证据，3 次新浏览器 URL 尝试、2 次模型调用。该次原始输出的 2 人是 M. Smedes / Michiel Smedes 对应同一 LinkedIn，最终去重回放为 **1 人**；不计作增长。Carla Vierhout 已有官网具名角色，但尚未附上可计数的个人渠道；Michel 的全名与缩写仍未自动合并。

最终版本另外修复已知重定向旧网址重复抓取，以及补挂 LinkedIn 后同一账号再次计数。最后两项通过固定失败用例和离线结果回放验证，未另跑完整联网模型流程。**212 项测试通过**，旧 22 份有效输出回放一致，4,814 个历史文件及最近三轮 1,382 个冻结文件未变。7 次完整流程共调用 MiniMax-M2.7 11 次、326,876 token，6 次费用未知；未使用付费搜索 API，未写入 CRM。

本地审查报告：`state/retrieval-repair-20260911/report.md`；原配对离线重算：`.venv/bin/python state/retrieval-repair-20260911/analyze.py`；补充验证：`state/retrieval-confirmation-20260911/`；最终去重复核：`state/retrieval-repair-20260911/final-postprocess-review.json`。原始对照、失败及重复计数产物均保留。

### 海关查询与来源预算

海关数据使用独立的 AnySearch 预算。只有公司快照明确包含 `customs_search_enabled=true` 时才会查询；未被公司匹配模块选中的普通公司保持 0 次。选中后默认最多 2 次，可通过 `KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES` 设置为 0–3。首次查询没有同时匹配目标公司和贸易语义的有效线索时立即停止；命中后才继续核实。结果保存在 `customs` 字段和 `customs-search-results.json`，只添加“海关采购证据”或“海关记录待核实”标签，不改变公司匹配分，也不进入联系人抽取。实际调用数写入 `run_summary.customs_anysearch_queries`。

每次运行前会验证本机代理、Crawl4AI出口和本地SearXNG代理配置一致，只保存12位出口哈希，不保存公网IP。运行产物位于输出JSON同级的 `<文件名>.artifacts/`，包括带 provider、rank、source type 和抓取时间的搜索结果、来源告警、HTML 抓取内容、`pdf-pages.json` 和 Hermes usage。

基础查询在 AnySearch 预算内优先调用 AnySearch，没有合格结果或失败时降级到 SearXNG；超过该预算后的查询只调用 SearXNG。实际 AnySearch 调用次数写入结果的 `run_summary.anysearch_queries`。2026-09-11 的单引擎与直接请求检查后，SearXNG 默认使用 Google CSE；Bing 返回无关结果、DuckDuckGo Web 解析错误、Qwant CAPTCHA，因此默认停用。`--phone-region` 为 `CN`、`KR` 或俄语区国家代码时，仍按既有规则追加 Baidu、Naver 或 Yandex；这些地区来源未在本轮重新验证。显式实验引擎设置继续保留。一个客户端内同一引擎连续两次报告失败后，本任务停止请求该引擎；全部暂停时直接记录来源故障，下个任务使用新客户端重新尝试。成功返回该引擎结果会清除连续失败计数。原始错误写入 `search-warnings.json`，官网或其他来源仍可用时继续处理。

官网发现会读取 `robots.txt` 中声明的 Sitemap 以及默认 `/sitemap.xml`，优先选择团队、管理层、公司介绍、新闻和联系页面。只有 CRM 网址本身能与公司名匹配时，Sitemap 页面才会直接进入抓取队列；否则先抓首页，再从首页发现同域联系页，避免错误的 CRM 网址占满抓取预算。单个网页抓取失败时会有界地重试 1 次。

启用人员补搜时，为补充证据保留 `min(3, max_urls // 3)` 个网页名额，例如总预算 10 页时首轮最多 7 页；禁用补搜保持原预算。首轮未达到目标时，用剩余名额优先抓取官网具名证据、官网刊物、访谈、媒体中心、新闻或活动页，其后才读取合格第三方人员页；搜索故障也可继续沿已知官网链接读取。确认页、隐私和 Cookie 页面降级，已抓取的原网址及重定向目标均去重。没有合格新来源时允许少用预算，已达到目标时不增加模型调用。发现的是首字母姓名、同时已有待核实全名时，先查询全名任职；身份与个人渠道仍按完整证据核验，最多追加一次模型提取。广泛发现模式补挂 LinkedIn 后再次按现有账号标识去重，避免缩写和全名重复计数。

同域 PDF 和 `filetype:pdf` 搜索结果会交给 pypdf，而不会再发送给浏览器。每家公司最多处理 3 个 PDF，单文件最多 15 MB、前 60 页；加密、损坏、超限或纯扫描件会记录错误但不终止任务。首版不做 OCR。

抓取前会先过滤搜索结果：同域页面需要 CRM 网址本身可识别，或搜索结果精确出现公司名；外部页面必须同时包含公司名称，并且 URL 本身包含公司名称或来自受信任的企业资料、工商、贸易或 LinkedIn 域名。公司名校验会忽略大小写、重音、商标符号和法定后缀（包括西班牙 `S.L.`），并接受输入名称中明确写出的括号别名，不接受任意局部匹配。Hermes 输出的候选必须有目标公司的当前任职证据。证据来源必须是本次成功抓取页面、成功提取的 PDF，或通过“LinkedIn 个人页 + 精确公司名 + Current/Present/at company”预筛的搜索引擎 `search_excerpt`；后者的 LinkedIn 状态强制为 `probable`。母集团职位或其他未经预筛的 probable 证据会进入 `validation_rejections`。

## SearXNG 管理

当前配置只绑定 `127.0.0.1:18080`。默认配置通过宿主机 `7897` 代理访问搜索引擎；本机可在 `deploy/searxng/.env` 中设置 `SEARXNG_SETTINGS_PATH=./settings.local.yml`，让不同引擎使用各自的已验证出口。`settings.local.yml` 会被 Git 忽略，避免提交本机代理认证信息。

2026-09-11 已将本机服务重新指向当前项目的 `settings.yml`，Google CSE 使用宿主机 `7897`，并通过实际查询验证。旧 `settings.local.yml` 中的引擎分流配置保留在本地，当前不加载；再次切换出口或启用引擎前应运行单引擎相关性检查。HTTP 200 或结果数量非零不能证明搜索有效。

```bash
# 查看状态
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml ps

# 停止
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml down
```

### 免费引擎接入实验（2026-09-09）

新增独立的 `deploy/searxng/compose.experimental.yml` 和 `settings.experimental.yml`，复用固定版本的 SearXNG 镜像，在 `127.0.0.1:18082` 提供 Brave、Mojeek、Startpage。生产实例的 `18080` 端口、默认引擎和配置不变；实验实例没有自动重启策略。本机本轮测试后已停止实验实例。

固定 7 家公司、8 名已知人员的 16 条查询，每个引擎请求均检查原始结果的 `engines` 归属。现有组合完成 16 次查询，返回 314 个不同 URL，但包含明显无关及复述查询的异常结果，**不能把 URL 数量当作有效联系人数量**。三个新引擎各尝试 3 次后按连续故障规则停止，剩余各 13 条未执行；Brave 限流、Mojeek 连接错误、Startpage CAPTCHA，均未返回结果。部分请求直接命中 SearXNG 暂停状态，不是新的上游请求。**本轮新增有效联系人为 0，尚不能比较新引擎的召回质量，暂不加入默认流程。**

另有 3 次健康检查和 3 次现有 `SearxngClient` 联网检查；客户端将来源故障报告为 `RuntimeError`，不会当作正常空结果。未调用搜索付费 API 或模型，未写入 CRM；原有 **169 项测试通过**。冻结查询、原始响应、网络诊断和审查报告仅在本地：`state/search-engines-20260909/report.md`。本轮使用既有样本，属于开发对照，不是新的独立留出测试。

复现接入检查（需要现有 Docker、宿主机代理 `7897` 和包含 `SEARXNG_SECRET` 的本地环境文件）：

```bash
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.experimental.yml up -d

# 启动完成后检查配置和单引擎原始响应；engines 可换为 mojeek 或 startpage。
curl --fail --silent --show-error http://127.0.0.1:18082/config
curl --fail --silent --show-error --get http://127.0.0.1:18082/search \
  --data-urlencode 'q=refractories' \
  --data-urlencode 'engines=brave' \
  --data-urlencode 'format=json'

docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.experimental.yml down
```

HTTP 200 仅表示本地 SearXNG 接口正常，还需检查 `unresponsive_engines`、非空结果和实际 `engines` 归属。CAPTCHA、限流和连接失败时停止扩测；先恢复来源可用性，再比较经核验的联系人增量。

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

当前测试共 **212 项**，覆盖来源核验、渠道归属、补搜预算、引擎连续失败暂停、官网证据优先、重定向别名去重、补挂账号后的重复人员清理、失败诊断、CRM 去重、官网身份、嵌套图片链接、人员检索与身份边界、任务恢复和旧结果兼容。测试使用固定数据与替身，不启动真实搜索或模型任务。

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
