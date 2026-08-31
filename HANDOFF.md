# Key Search 新线程交接文档

更新时间：2026-08-15（Asia/Shanghai）

## 新线程启动指令

把下面内容作为新线程的第一条任务说明：

```text
继续维护 Key Search 联系人挖掘项目。

项目根目录：
/Users/acelerzbw/Documents/Codex/2026-08-13/crm-twenty-postgresql/key-person-discovery

开始前请完整阅读项目根目录的 HANDOFF.md 和 README.md，然后只读检查当前进程、dashboard API、SQLite 批次状态及最新输出。不要仅根据历史交接判断运行状态，不要在已有 batch 运行时重复启动。

所有代码、输入、输出、状态和日志必须保存在 key-person-discovery 项目内；禁止把任何结果写入父目录或 twenty-hermes-poc。Twenty PostgreSQL、Hermes、AnySearch、SearXNG 和本机代理只是外部依赖。不得输出密码、API Key 或完整令牌。

CRM 只读，联系人发现与消息发送保持分离；未经人工确认不得写回 CRM，也不得触发 Hermes 发信。
```

## 目标和业务规则

本项目从 Twenty CRM 读取公司，联网挖掘具有采购决策权、采购影响力或技术影响力的联系人。

联系人范围：

- 采购、供应链、老板、高管、厂长、运营负责人。
- 保留耐火材料工程师、冶金工程师、技术经理、质量经理、铸造或陶瓷工程师等技术影响者。
- 主要联系方式为 LinkedIn、邮箱、电话；明确标注的 WhatsApp 或 `wa.me` 链接单独保留。
- 普通手机号不能推断为 WhatsApp。
- 公司公共邮箱、总机和无归属电话可以保留，但不能强行绑定到个人。

数量规则：

- CRM 有效联系人少于 4 条时，单次任务目标为至少 4 个可联系项。
- 其他公司目标为至少 1 个新增可联系项。
- 目标是尽量达到，不允许降低证据标准或编造联系人凑数。
- 去重后没有新增可联系项，但公开来源已正常检查时，任务以 `no_new_contact` 正常完成；公开来源整体不可用时才以 `source_limited` 失败并保持可重试。

公司画像和 Aceler 产品范围保存在 `examples/aceler.json`。

## 项目边界

唯一项目根目录：

```text
/Users/acelerzbw/Documents/Codex/2026-08-13/crm-twenty-postgresql/key-person-discovery
```

目录职责：

| 路径 | 用途 |
|---|---|
| `src/key_person_discovery/` | 后端、批处理、CRM、搜索、抓取和验证代码 |
| `web/` | 简单前端 |
| `deploy/searxng/` | 本项目的 SearXNG 配置 |
| `tests/` | 单元和回归测试 |
| `examples/` | Aceler 公司画像和示例输入 |
| `inputs/` | CRM 公司快照和每任务日志 |
| `outputs/` | 公司结果及证据 artifacts |
| `state/jobs.sqlite3` | 持久化任务和批次状态 |
| `state/` | 服务日志和本地状态 |

CLI、dashboard 和 batch 已加入路径校验，`--output`、`--outputs`、`--inputs` 或 `--db` 指向项目外时会直接拒绝。父目录旧的空 `outputs/`、`work/` 已删除。

项目当前不是 Git 仓库。不要使用 reset、clean 或依赖 Git 恢复文件。

## 当前架构

处理流程：

1. 通过 PostgreSQL 只读事务从 Twenty 获取公司和现有联系人。
2. 保存公司本地快照；数据库凭据不传给搜索子进程。
3. 每家公司最多使用 AnySearch 处理前 3 条采购、技术和 LinkedIn 定向查询；无结果、失败或超出调用上限时使用本地 SearXNG。
4. 主动读取官网 `robots.txt`、Sitemap、首页和相关站内链接；CRM 网址不能与公司名匹配时，不直接将 Sitemap 页面填入抓取队列。
5. Crawl4AI 抓取 HTML；pypdf 提取文本型 PDF。
6. 提取 LinkedIn、邮箱、电话和明确 WhatsApp 信号。
7. Hermes 主 Agent 仅根据本次证据生成候选。
8. 验证当前任职、来源 URL、联系方式归属和公司匹配。
9. 按姓名、LinkedIn、邮箱、电话与 CRM 现有联系人精确去重。
10. 写入 JSON 结果和 artifacts，由前端显示；不会自动写回 CRM。

主要文件：

- `crm.py`：Twenty PostgreSQL 只读查询和联系人快照。
- `sources.py`：AnySearch、SearXNG、Crawl4AI、Sitemap。
- `pdf_source.py`：有边界的 PDF 下载和文本提取。
- `signals.py`：联系方式提取、E.164、WhatsApp 和传真排除。
- `hermes.py`：Hermes 提示词、输出验证和公共联系方式规则。
- `pipeline.py`：完整发现流水线、CRM 去重和统计。
- `jobs.py`：SQLite 任务状态、公司快照、项目路径边界。
- `job_runner.py`：独立任务 Runner、心跳和成功判定。
- `batch.py`：CRM 分页、累计有效时长、休眠恢复。
- `dashboard.py`、`web/index.html`：API 和前端。

## 数据源和配置

当前来源：

- Twenty CRM / PostgreSQL：公司和现有联系人，只读。
- AnySearch：前 3 条采购、技术和 LinkedIn 定向查询的优先来源，项目 `.env` 已保存 Key。
- SearXNG：AnySearch 失败及超过每公司调用上限后的搜索来源，启用 Bing、DuckDuckGo、Google CSE、Qwant。
- 官网、robots.txt、Sitemap 和公开页面。
- 公开 PDF；当前不支持扫描件 OCR。
- 搜索索引中的公开 LinkedIn 个人页和摘要；不登录 LinkedIn。
- 搜索发现的企业目录、工商、公司资料和贸易页面。

关键端口：

- Dashboard：`0.0.0.0:18181`。
- 本地 SearXNG：`127.0.0.1:18080`。
- 本机代理：由项目 `.env` 的 `KEY_PERSON_PROXY_URL` 配置。

配置文件：

- 项目 `.env`：AnySearch Key、代理、SearXNG、Hermes 等；权限应为 `600`，已被 `.gitignore` 忽略。
- Twenty 配置：`/Users/acelerzbw/Documents/p2/twenty-hermes-poc/config/local.env`，仅作为外部数据库连接配置读取。
- 启动时必须先加载 Twenty 配置，再加载项目 `.env`。不要反转顺序，因为当前 env loader 使用 `setdefault`，项目模板中的空 CRM 字段可能阻止后续覆盖。
- 不得在文档、终端输出或回复中展示任何实际 Key、密码或令牌。

只有处于公司内网时，`192.168.112.72:5432` 的 Twenty CRM 才可连接。离开公司内网时不要反复重试，也不要改连本机 `55432`；该端口是另一套 PostgreSQL，账号和数据用途不同。

## 已完成的重要修复

- 任务和批次状态持久化到 SQLite，刷新前端不会丢任务。
- 批次按累计有效运行时长计时；Mac 休眠时间不计入 48 小时。
- AnySearch Key 已复制到项目 `.env`，Runner 已验证能够加载。
- AnySearch 默认限制为每家公司最多 3 次调用，其余查询使用 SearXNG；可通过 `KEY_PERSON_ANYSEARCH_MAX_QUERIES` 调整，实际调用数写入 `run_summary.anysearch_queries`。
- 海关数据仅在公司快照包含 `customs_search_enabled=true` 时使用独立 AnySearch 预算，普通公司保持 0 次；选中后默认最多 2 次，首次无有效线索立即停止。配置项为 `KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES`（0–3），调用数写入 `run_summary.customs_anysearch_queries`，线索只形成标签和证据，不影响匹配分或联系人抽取。
- CRM 快照包含现有联系人，结果在计数前按姓名、LinkedIn、邮箱和电话去重。
- JSJ 真实案例中，已有联系人 Steffen Schulze 和已有电话能够被识别为重复。
- 大型企业目录仅出现公司名称时，不再把目录运营方页脚联系方式归给目标公司。
- Fax、Telefax、Facsimile、Télécopie 不再作为电话。
- 失败任务的 JSON 证据仍保留，但不会出现在前端正常结果列表。
- 无新增联系人与技术失败已分开：`no_new_contact` 是完成子状态且不展示空结果，`source_limited` 保持可重试。
- 已为 CRM 中已观察的日本、越南、韩国、西班牙和泰国中英文国名补充电话区域映射，并补充西班牙 `S.L.` 公司后缀。
- 抓取失败的 URL 会有界地重试 1 次；错误 CRM 网址不再用通用同域页面占满抓取预算。
- Dashboard 首屏已改为当前批次视图：独立进度条、当前批次统计和最多 20 条当前任务；历史任务不再混入。结果区默认精确按当前 batch ID 的输出文件过滤，也可从“结果范围”选择历史日期批次。单公司启动区和结果公司卡片默认折叠。
- 公共联系方式只接受企业专属页面或精确公司联系人摘要。
- 所有主动写入路径被限制在 Key Search 项目内。

## 当前运行状态快照

以下为 2026-08-15 应用本次修复后的快照，接手时仍必须重新检查：

- Dashboard、batch、Runner 和 CLI 均已停止，`18181` 端口未监听。
- 最新 batch：`a51af78f3156`，状态 `failed/failed`，原因 `Paused by user`。
- 该批次已累计有效运行 `1069.1` 秒，剩余 `2530.9` 秒。
- 该批次历史结果为 4 个完成、16 个失败；本次修复未改写这些历史状态或输出。下次新任务才使用新分类。

不要依赖上述状态声称服务仍在运行。新线程必须执行下面的只读检查。

## 新线程首先执行的检查

```bash
cd /Users/acelerzbw/Documents/Codex/2026-08-13/crm-twenty-postgresql/key-person-discovery

ps -axo pid,ppid,state,etime,command \
  | rg 'key-person-(dashboard|batch)|key_person_discovery.(job_runner|cli)' \
  | rg -v 'rg '

curl -fsS http://127.0.0.1:18181/api/batches
curl -fsS 'http://127.0.0.1:18181/api/jobs?limit=20'
```

判断标准：

- 只有 dashboard 端口可访问，不代表 batch 正常。
- 必须同时检查 batch 进程、API 中的新鲜 heartbeat、活跃 job 和 Runner/CLI 进程。
- 如果已有 batch 正常运行，不要重复启动。
- 如果进程不存在但 SQLite 仍显示 `running`，先根据 PID、heartbeat 和 active job 确认它确实失效，再标记旧批次失败并用剩余秒数恢复。
- 新线程无法依赖旧线程的 PTY session ID，因此不要把 session ID 当成服务状态证据。

## 启动命令

仅在公司内网、CRM 只读连接成功且确认没有重复进程时使用。

Dashboard：

```bash
.venv/bin/key-person-dashboard \
  --env-file /Users/acelerzbw/Documents/p2/twenty-hermes-poc/config/local.env \
  --env-file .env
```

Batch：

```bash
caffeinate -i .venv/bin/key-person-batch \
  --env-file /Users/acelerzbw/Documents/p2/twenty-hermes-poc/config/local.env \
  --env-file .env \
  --duration-hours <剩余秒数除以3600> \
  --workers 1
```

不要重新写死 48 小时，否则会重复累计。恢复前从最新有效批次读取 `remaining_seconds`。

## 验证命令

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
```

交接时最近一次结果：54/54 测试通过，compileall 通过，pip check 无损坏依赖，前端 JavaScript 语法检查通过。

项目边界验证：

```bash
.venv/bin/key-person-discovery \
  --company examples/aceler.json \
  --output /tmp/key-search-boundary-test.json
```

应在联网前直接报错：`--output must be inside the Key Search project`，且不得生成 `/tmp/key-search-boundary-test.json`。

## 已知限制和风险

- Dashboard 监听内网且没有登录认证；不要暴露到不可信网络。
- 搜索引擎可能部分失败；部分失败记录 warning，全部活跃引擎失败必须中止，不能伪装成零联系人。
- LinkedIn 只使用公开索引证据，不保证页面可直接抓取。
- 电话格式合法不代表号码真实可用；WhatsApp 只有明确证据才标记 verified。
- CRM 去重以标准化后的精确字段匹配为主，不做高风险模糊合并。
- 国家名到电话区域的映射仅覆盖已观察到的 CRM 标签；长期应由 CRM 统一提供 ISO 国家代码。
- pypdf 不处理纯扫描件 OCR。
- 结果全部要求人工复核。
- 联系人挖掘与 Hermes 消息发送严格分离。
- Mac 合盖仍可能休眠和断网；`caffeinate -i` 主要避免空闲休眠，批次会在恢复后继续累计有效时长。

## 接手原则

1. 先查实时状态，再相信交接快照。
2. 保留用户已有文件，不做相邻重构。
3. 所有写入严格限制在 Key Search 项目根目录内。
4. CRM 只读；互联网证据不能自动覆盖 CRM。
5. 不为了数量降低证据质量。
6. 修改后运行针对性测试和完整测试，并报告剩余风险。
