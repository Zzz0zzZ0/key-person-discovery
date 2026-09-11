# Key Person Discovery / Key Search

从公司名称、官网和业务画像出发，发现公开网页中的关键人员，输出姓名、任职、LinkedIn、邮箱、电话和可追溯证据。主要服务于采购、技术、生产和经营管理联系人发现，也保留有价值的业务转介人员。

**交接基准：2026-09-11，应用版本 `0.4.0`，功能基线提交 `df32059`，212 项自动化测试通过。** 目前已修复来源筛选、官网定位、补搜预算和重复计数，但最近的三家公司配对**没有证明联系人净增量**。免费搜索限流、任职判定不一致以及姓名有证据但缺个人渠道，仍是主要限制。

## 阅读导航

- [项目边界与运行结构](#项目边界与运行结构)
- [接手现有机器](#接手现有机器)
- [新机器安装](#新机器安装)
- [配置文件与优先级](#配置文件与优先级)
- [启动与使用](#启动与使用)
- [macOS 自动启动与停止](#macos-自动启动与停止)
- [结果、证据与计数](#结果证据与计数)
- [日常维护](#日常维护)
- [备份、恢复与迁移](#备份恢复与迁移)
- [故障排查](#故障排查)
- [开发、测试与发布](#开发测试与发布)
- [已知限制与后续工作](#已知限制与后续工作)
- [交接验收清单](#交接验收清单)

详细规则、历次实验和指标保留在[发现规则与验证记录](docs/discovery-and-validation.md)。其中 `state/...` 报告、CRM 快照和冻结回放脚本仅在原机器本地，**不随 Git 克隆交付**；新机器应先运行仓库内的 `tests/`。

## 项目边界与运行结构

```text
CLI 公司 JSON ───────────────────────────────┐
Dashboard / Batch → Twenty 只读查询 → 本地快照 ├→ 独立任务 / discovery
                                             ↓
代理预检 → AnySearch / SearXNG → 来源筛选与官网身份核验
         → Crawl4AI HTML / pypdf PDF → Hermes 提取
         → 任职、渠道归属、CRM 去重 → 必要时补搜 → JSON + 证据
```

- **不写入 Twenty CRM，不自动发邮件或消息，不登录 LinkedIn，不探测 WhatsApp 注册状态。** 所有人员结果要求人工复核。
- CLI 直接读取公司 JSON，不需要连接 CRM；Dashboard 的 CRM 选公司功能和 Batch 需要可访问的 Twenty PostgreSQL。
- 搜索请求会携带公司名、岗位或人员名；Hermes 会接收公司画像和公开来源文本，并使用其自身配置的模型服务。输入快照中的 CRM 联系资料用于本地去重。不要把运行目录、模型日志或完整快照当作可公开数据。
- Dashboard 默认监听 `0.0.0.0:18181`，**没有登录认证**。同网段可访问者可查询 CRM 并启动消耗搜索/模型额度的任务；仅本机使用时显式设置 `--host 127.0.0.1`。不要直接暴露公网。
- Python 包通过 editable 安装使用此仓库。CLI 输出目录，以及 Dashboard/Batch 的输入、输出、任务库路径必须位于仓库内；CLI 的 `--company` 是只读输入，不受输出路径限制。
- 不需要 Node.js 或前端构建。Twenty、Docker/SearXNG、宿主机代理和 Hermes 均为独立依赖，不由 Dashboard 自动启动。

| 路径 | 用途 |
| --- | --- |
| `src/key_person_discovery/cli.py` | 单家公司入口、预算及代理预检 |
| `pipeline.py`、`sources.py`、`search_quality.py` | 发现流程、采集、相关性和来源贡献 |
| `hermes.py`、`contact_evidence.py`、`signals.py` | 模型适配、证据归属、渠道提取和校验 |
| `models.py`、`diagnostics.py` | 公司/来源模型、失败分类和补搜计划 |
| `dashboard.py`、`jobs.py`、`job_runner.py`、`batch.py` | 展示/API、SQLite 队列、独立 Runner、批处理 |
| `crm.py`、`preflight.py` | Twenty 只读 SQL、代理和出口检查 |
| `web/index.html` | Dashboard 页面 |
| `examples/aceler.json` | CLI 示例公司；Dashboard/Batch 的行业和产品画像模板 |
| `deploy/searxng/`、`deploy/macos/` | Docker 配置与 LaunchAgent 模板 |
| `inputs/`、`outputs/`、`state/` | 私有快照、结果及证据、任务库和日志 |
| `tests/`、`docs/` | 固定数据测试与详细规则/历史验证 |

表中省略目录的 Python 文件均位于 `src/key_person_discovery/`。

## 接手现有机器

先检查现有服务，不重复启动。下面是 2026-09-11 本机核实的部署信息，不是其他机器必须使用的绝对路径。

| 项目 | 当前值 |
| --- | --- |
| 仓库 | `/Users/acelershen/Projects/key-search`，分支 `main` |
| 远端 | [Zzz0zzZ0/key-person-discovery](https://github.com/Zzz0zzZ0/key-person-discovery) |
| Dashboard | `http://127.0.0.1:18181`，现有服务监听所有网卡 |
| LaunchAgent | `~/Library/LaunchAgents/com.aceler.key-person-dashboard.plist` |
| CRM / 应用配置 | 仓库下 `state/twenty.env` / `.env`，依次加载 |
| SearXNG | 容器 `key-person-searxng`，`127.0.0.1:18080` |
| SearXNG 当前挂载 | 仓库下 `deploy/searxng/settings.yml` |
| 代理 | 宿主机 `127.0.0.1:7897`；Docker 内为 `host.docker.internal:7897` |
| 实验引擎 | 独立模板使用 `18082`，不属于默认生产流程 |

旧 `Documents/ChatGPT/key-search` 是迁移前路径；不要从旧路径重新创建服务。现存 `settings.local.yml` 是旧本机分流配置，当前不加载，不能因文件存在就认定它生效。

在当前仓库根目录检查：

```bash
pwd
git status --short
.venv/bin/python -c 'from key_person_discovery.jobs import PROJECT_DIR; print(PROJECT_DIR)'
launchctl print "gui/$(id -u)/com.aceler.key-person-dashboard"
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml ps
curl --fail --silent --show-error -o /dev/null -w 'Dashboard HTTP %{http_code}\n' http://127.0.0.1:18181/
```

仓库历史上已经跟踪过部分运行数据，因此 `state/dashboard.log`、`state/jobs.sqlite3` 出现本地修改不一定是异常。`.gitignore` 不会自动取消跟踪；不要用 `git restore .`、`git reset --hard` 或 `git clean` 清理工作区。

## 新机器安装

以下命令默认在 macOS 的仓库根目录执行。Python 要求 3.11+；示例使用 3.11。Docker Desktop/兼容 Docker Engine 需已启动，Compose v2 可用。Linux 上容器访问宿主机代理可能需要额外的 host-gateway 配置；仓库的 macOS 模板不能直接视为已验证的 Linux 部署方案。

### 1. 安装 Python 环境与浏览器

```bash
git clone https://github.com/Zzz0zzZ0/key-person-discovery.git
cd key-person-discovery
python3.11 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/playwright install chromium
mkdir -p inputs outputs state
chmod 700 inputs outputs state
```

仅运行项目可使用 `pip install -e .`；执行测试需要 `.[test]`。依赖版本范围见 `pyproject.toml`，目前没有完整依赖锁文件，重新安装可能得到不同的间接依赖版本。

Dashboard/Batch 访问 CRM 还需要本机 `psql`。macOS Homebrew 可执行 `brew install libpq`，并设置 `PSQL_BIN=/opt/homebrew/opt/libpq/bin/psql`；Intel Mac 应使用 `brew --prefix libpq` 下的实际路径。只安装 PostgreSQL 客户端即可，不需新建本地数据库。

### 2. 配置代理、Hermes 与环境文件

```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

编辑 `.env`，逐项确认[配置表](#配置文件与优先级)。**`.env.example` 中仍有历史机器的 Hermes 路径和 `12001` 代理示例；必须替换，不能直接照搬。** 本仓库 SearXNG 模板使用 `7897`，应用与容器代理必须一致。

Hermes 不包含在本仓库中。先在接手机器安装、配置模型凭据，再将 `HERMES_COMMAND` 设置为实际可执行文件的**绝对路径**。适配器需要该命令支持 `--toolsets clarify --usage-file PATH --oneshot PROMPT`；只有同名命令还不够。路径中不要附加命令行参数；需要固定 profile 时可指向现有包装脚本。最近的实验使用 MiniMax-M2.7，但生产代码不锁定模型，实际模型由 Hermes 配置决定，应以每次 `usage*.json` 为准。

### 3. 创建 SearXNG 私有配置并启动

首次安装执行下列脚本；若文件已存在则停止，避免覆盖现有秘密配置：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import secrets
p = Path('deploy/searxng/.env')
with p.open('x') as f:
    f.write('SEARXNG_SECRET=' + secrets.token_hex(32) + '\n')
    f.write('SEARXNG_SETTINGS_PATH=./settings.yml\n')
p.chmod(0o600)
PY

docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml up -d
```

Compose 固定了镜像摘要，首次启动需要拉取镜像；不要为排障直接换成 `latest`。`SEARXNG_SECRET` 由 Compose 传给容器，不能省略。容器设置 `restart: unless-stopped`，但 Docker 本身仍需启动。

默认引擎是 Google CSE；Bing、Qwant、DuckDuckGo Web 因最近实测异常默认关闭。验证接口和实际相关性：

```bash
curl --fail --silent --show-error http://127.0.0.1:18080/config
curl --fail --silent --show-error --get http://127.0.0.1:18080/search \
  --data-urlencode 'q="Gouda Refractories"' \
  --data-urlencode 'engines=google cse' \
  --data-urlencode 'format=json'
```

应同时检查 `results` 的公司相关性、`engines` 归属和 `unresponsive_engines`。HTTP 200、空结果、结果很多都不能单独证明搜索源健康。遇到 CAPTCHA/限流就停止扩大测试。

### 4. 配置 CRM（CLI 单独使用可跳过）

向维护者取得相应 Twenty 实例的只读连接参数，写入 `state/twenty.env`，权限设为 `600`。所需键见下表。不要把真实主机、账号、密码或工作区标识写入 README。

代码使用 `BEGIN READ ONLY`，查询有 10 秒 statement timeout、15 秒子进程超时；数据库账号仍应由管理员配置相应读取权限。`TWENTY_WORKSPACE_SCHEMA` 必须符合 `workspace_[a-z0-9]+`，且 schema 中存在当前代码使用的 `company`、`person` 字段。Twenty 版本或自定义字段变化后须重新验证 SQL。

## 配置文件与优先级

**Dashboard/Batch：已有进程环境优先，其次是先加载的环境文件。** 加载器使用 `os.environ.setdefault`，空字符串也会占位，后加载文件不会覆盖。正确顺序通常是 `--env-file state/twenty.env --env-file .env`。文件不存在会被跳过，并不会立即报错。

**CLI 不自动读取 `.env`，也没有 `--env-file` 参数。** 可通过下一节的 Python 启动示例显式加载。文件只写 `KEY=VALUE`，含空格的值用引号；不要使用 `export KEY=...`，也不要期待 `$HOME`、`~` 或 `$(...)` 被展开。

| 变量 | 默认/范围 | 注意事项 |
| --- | --- | --- |
| `KEY_PERSON_PROXY_URL` | CLI 必须有有效代理 | 未设时回退 `HTTPS_PROXY`、`HTTP_PROXY`；优先使用完整 HTTP 代理地址及端口 |
| `SEARXNG_URL` | `http://127.0.0.1:18080` | JSON 搜索端点所在实例 |
| `HERMES_COMMAND` | 代码有旧机器路径 | 必须显式改为本机可执行文件绝对路径 |
| `KEY_PERSON_TIMEOUT_SECONDS` | `600`，30–3600 | 单次 Hermes 超时，不是整家公司总时限 |
| `ANYSEARCH_API_KEY` | 空 | 匿名访问额度较低；API 与模型费用分别计算 |
| `KEY_PERSON_ANYSEARCH_MAX_QUERIES` | `5`，0–100 | 基础预算；0 禁用基础及人员补搜中的 AnySearch |
| `KEY_PERSON_PEOPLE_SEARCH_MAX_QUERIES` | `3`，0–6 | 额外人员查询预算；0 关闭补搜 |
| `KEY_PERSON_CUSTOMS_ANYSEARCH_MAX_QUERIES` | `2`，0–3 | 独立海关预算；完全免费搜索实验应另设为 0 |
| `KEY_PERSON_EXPERIMENTAL_BROAD_DISCOVERY` | 未设为关闭 | `true` 开启两源融合、较宽人员保留及额外账号去重；不是保证增量的开关 |
| `KEY_PERSON_EXPERIMENTAL_SEARXNG_ENGINES` | 未设 | 逗号分隔；只有开启 broad discovery 才使用显式覆盖 |
| `SEARXNG_SETTINGS_PATH`（应用环境） | 仓库 `deploy/searxng/settings.yml` | 本地代理预检读取的文件；覆盖时建议用绝对路径 |
| `SEARXNG_SETTINGS_PATH`（Compose `.env`） | `./settings.yml` | 决定实际挂载文件，相对 `deploy/searxng/`；与应用预检文件必须指向同一配置 |
| `SEARXNG_SECRET`（仅 Compose `.env`） | 无 | 本机生成，禁止提交 |
| `TWENTY_DB_HOST/NAME/USER/PASSWORD` | 无业务默认值 | 连接目标 Twenty PostgreSQL，密码按数据库认证方式提供 |
| `TWENTY_DB_PORT` | `5432` | 目标数据库端口 |
| `TWENTY_DB_SSLMODE` | `prefer` | 按数据库管理员要求设置，不为消除报错关闭 TLS 校验 |
| `TWENTY_DB_CONNECT_TIMEOUT` | `5` | PostgreSQL 连接超时秒数 |
| `TWENTY_WORKSPACE_SCHEMA` | 必填 | 目标工作区 schema |
| `PSQL_BIN` | PATH，其次 Apple Silicon Homebrew 路径 | 新机器和 LaunchAgent 建议显式设置绝对路径 |

预算补充：默认基础 AnySearch 5 次，人员补搜另有最多 3 次；海关只有公司快照明确启用时才运行，且预算独立。免费搜索模式仍可能产生 Hermes 模型费用。CLI 对部分区域还会追加 Baidu、Naver 或 Yandex，这些来源未在最近一轮重新验收。

改配置后，已启动的 Dashboard、Batch、Runner 不会自动重新加载。需要重启入口进程；已经启动的独立任务仍使用旧环境，正在调用的模型也不会切换配置。

## 启动与使用

### 单家公司 CLI

在仓库内准备 JSON，`name` 必填，建议提供精确官网；官网为子公司目录时保留完整子路径。`industries`、`products` 为字符串数组；可指定 `target_contact_count`。提供 `crm_contacts` 才能按该快照去重；未提供时不能声称“相对 CRM 新增”。完整字段见 `CompanyProfile.from_dict` 与 `examples/aceler.json`。

下面使用示例公司启动一次真实搜索及模型任务，输出目录按时间区分。已准备自己的公司输入时替换 `--company`：

```bash
.venv/bin/python -c 'from pathlib import Path; from key_person_discovery.crm import load_env_file; load_env_file(Path(".env")); from key_person_discovery.cli import main; main()' \
  --company examples/aceler.json \
  --output "outputs/manual-$(date +%Y%m%d-%H%M%S)/aceler.json" \
  --phone-region CN \
  --max-urls 10
```

`--max-urls` 默认 20，允许 1–100。启用补搜时保留 `min(3, max_urls // 3)` 页，例如 10 页分为首轮最多 7 页、补充最多 3 页；最多 3 个 PDF，单份 15 MB、前 60 页，无 OCR。只有新有效证据才追加模型，单任务最多两轮提取。

可选 `--topeasy-export` 接受 TopEasy 决策人 CSV 导出，包括扩展名为 `.xls` 的 CSV；不是通用 Excel 二进制文件导入器。CLI 写同一个输出路径会覆盖既有结果及部分同级产物，因此复测请使用新路径。

### Dashboard 前台启动

```bash
.venv/bin/key-person-dashboard \
  --host 127.0.0.1 --port 18181 \
  --env-file state/twenty.env \
  --env-file .env
```

打开 `http://127.0.0.1:18181/`。输入至少两个字符查询 CRM，选择公司后创建快照并开始任务；也可手动输入公司。仅查看已有结果或手动输入不需要 CRM 查询成功。`--profile` 默认 `examples/aceler.json`，Dashboard/Batch 只取其中行业和产品画像，目标公司来自用户选择/CRM。

前台服务用 Ctrl-C 停止。若已有 LaunchAgent 占用端口，不要再运行第二个 Dashboard；可用 `--port 18182` 临时验证，但注意两个页面默认共用任务库。

### Batch 先预览，再小批量运行

只读预览前三家公司，不创建任务、搜索或模型调用；输出含公司资料，仅用于本地检查：

```bash
.venv/bin/key-person-batch \
  --env-file state/twenty.env --env-file .env \
  --dry-run --max-companies 3
```

首次运行建议先 3 家、单并发；以下命令会真实创建任务：

```bash
.venv/bin/key-person-batch \
  --env-file state/twenty.env --env-file .env \
  --duration-hours 1 --workers 1 --max-companies 3
```

长期运行可在确认来源及模型稳定后使用：

```bash
mkdir -p state
nohup .venv/bin/key-person-batch \
  --env-file state/twenty.env --env-file .env \
  --duration-hours 48 --workers 1 \
  > "state/batch-$(date +%Y%m%d-%H%M%S).log" 2>&1 &
```

默认 48 小时有效运行、1 个 worker；时长允许大于 0 至 168 小时，workers 1–4，`--page-size` 默认 100（1–500），`--max-companies 0` 不设公司数量上限。按 UUID 游标读取名称/官网非空且未删除的公司；任务库中出现过的公司会被 Batch 跳过，**失败的历史任务也不会由新批次自动重跑**。需要复查时从 Dashboard 明确重新发起；同公司仍有活跃任务时会复用该任务。

CRM 联系人少于 4 条的快照目标设为 4，Runner 使用 30 个网页名额；其他情况目标通常为 1、网页 20。目标是尽量达到的新增人员数，不是结果上限，也不保证一定满足。

### 任务状态与停止边界

SQLite 中任务主状态为 `queued/running/completed/failed`，`stage` 区分预检、搜索、抓取、分析、`waiting_network`、`recovering`、`no_new_contact` 和 `source_limited` 等阶段。

- Runner 独立于 Dashboard 和 Batch。页面刷新、Dashboard 重启不会取消在途任务；Batch 到达时限/公司数后停止派发，已启动任务继续完成。批次显示完成不等于所有子任务都完成。
- 代理网络不可用时每 30 秒检测，恢复后等 10 秒，再重试同一个任务。网络正常但搜索源限流，不一定进入等待网络，可能以来源受限结束，需要来源恢复后再手动重试。
- 心跳超过 5 分钟且 Runner PID 不存在，才会清理为失败。状态保存在 SQLite 不代表进程被系统终止后一定自动续跑。
- Mac 运行任务时使用 `caffeinate -i -s` 阻止空闲睡眠，不能保证合盖仍运行；睡眠间隔不计 Batch 有效时长，所有活跃任务等待网络时也暂停计时。
- 当前没有统一的暂停/取消 API。正常维护应停止新派发并等待任务完成。紧急停止前用 `ps` 核对 Batch、Runner、CLI、Hermes 的 PID/父子关系，再只停止本项目对应进程；仅停止 Dashboard 或 Batch 不会终止所有子进程。不要宽泛执行 `pkill python` 或删除数据库“清队列”。

## macOS 自动启动与停止

模板：[com.aceler.key-person-dashboard.plist.example](deploy/macos/com.aceler.key-person-dashboard.plist.example)。使用登录用户的 LaunchAgent，不用 sudo。先确保 `.venv`、两个环境文件和 `state/` 存在；Docker、代理和 Hermes 也要独立配置。

首次安装生成本机 plist，已存在的配置不会被覆盖。此示例将监听地址改为仅本机：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from xml.sax.saxutils import escape
import plistlib
root = Path.cwd().resolve()
text = (root / 'deploy/macos/com.aceler.key-person-dashboard.plist.example').read_text()
text = text.replace('__PROJECT_DIR__', escape(str(root)))
text = text.replace('__CRM_ENV_FILE__', escape(str(root / 'state/twenty.env')))
data = plistlib.loads(text.encode())
args = data['ProgramArguments']
args[args.index('--host') + 1] = '127.0.0.1'
target = Path.home() / 'Library/LaunchAgents/com.aceler.key-person-dashboard.plist'
target.parent.mkdir(parents=True, exist_ok=True)
with target.open('xb') as f:
    plistlib.dump(data, f)
PY
plutil -lint "$HOME/Library/LaunchAgents/com.aceler.key-person-dashboard.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.aceler.key-person-dashboard.plist"
launchctl enable "gui/$(id -u)/com.aceler.key-person-dashboard"
launchctl kickstart -k "gui/$(id -u)/com.aceler.key-person-dashboard"
```

常用维护命令：

```bash
# 状态与日志
launchctl print "gui/$(id -u)/com.aceler.key-person-dashboard"
tail -n 100 state/dashboard.log

# 已加载的 Dashboard 重启：用于更新应用 .env 或代码；不取消独立任务
launchctl kickstart -k "gui/$(id -u)/com.aceler.key-person-dashboard"

# 卸载本次登录会话中的服务
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.aceler.key-person-dashboard.plist"

# 如需跨登录保持停用，再执行；重新启用用 enable，随后 bootstrap
launchctl disable "gui/$(id -u)/com.aceler.key-person-dashboard"
```

修改 plist 的参数或路径后，需要 bootout 后重新 bootstrap，不能只 kickstart。不要给 Batch 配置无条件 `KeepAlive`，否则结束后会再次启动批次。

macOS 后台服务可能无权读取 `Documents` / `Desktop`。出现 `Operation not permitted` 时，优先将仓库放在 `~/Projects/`，或按组织要求授权具体解释器；不要关闭系统保护。迁移还需重新创建虚拟环境及 plist，见迁移章节。

## 结果、证据与计数

| 产物 | 位置/用途 |
| --- | --- |
| 手工 CLI 结果 | `--output` 指定的 JSON |
| Dashboard / Batch 输入 | `inputs/web-YYYYMMDD/` 或 `inputs/batch-YYYYMMDD/` |
| 任务日志 | 输入 JSON 同名 `.log`；日期按 UTC 生成 |
| 任务结果 | `outputs/web-YYYYMMDD/` 或 `outputs/batch-YYYYMMDD/` |
| 证据目录 | 结果文件名后追加 `.artifacts/`，例如 `company.json.artifacts/` |
| 状态 | `state/jobs.sqlite3`；SQLite 使用 WAL，运行时可能有 `-wal/-shm` |
| Dashboard 日志 | 当前 LaunchAgent 配置为 `state/dashboard.log` |

重要结果字段：

- `candidates`：保留的目标人员；`unverified_candidates`：任职/身份仍待核验；`excluded_candidates`：岗位等规则排除人员。
- `unassigned_contacts`：未归属具体人的公共渠道；`crm_duplicates`：相对本次 CRM 快照识别的重复；`validation_rejections`：任职/输出校验原因。
- `run_summary.new_contactable_people`：已确认目标公司关联、非 CRM 已有、至少有一条非 guessed 个人渠道的人数。公共邮箱/总机、待核验人员、只含推测邮箱者不补足该目标。
- `contactable_items` / `contact_methods` 是兼容的渠道指标，口径不同；任务 `completed` 也可能仅有待核验人员或公共渠道，不能据此宣布新增目标人员。
- `diagnostics`：当前缺口、前后诊断、补搜原因、查询与失败阶段。搜索 `eligible` 只代表初筛相关，不等于当前任职或渠道归属已确认。

证据目录重点文件：`search-responses.json`（成功取得的原始响应）、`search-quality.json`、`engine-quality.json`、`search-warnings.json`、`website-resolution.json`、`website-routing-pages.json`、`pages.json`、`pdf-pages.json`、`search-evidence.json`、`contact-signals.json`、`contact-conflicts.json`、`people-baseline.json`、`people-search.json`、`diagnostics.json`。按执行阶段不同，并非每个文件都会存在。

费用同时检查 `usage.json` 和 `usage-people-topup.json`；`cost_status=unknown` 不能当作免费。模型、provider、token 以产物为准，不从 README 的历史实验推断。

人工复核应同时核对：目标公司/子公司、当前角色、姓名身份、渠道与该人的直接绑定、证据日期、当前 CRM 重复情况。LinkedIn 搜索摘要链接保持 `probable`；推测邮箱必须 `guessed`；普通手机号不是已验证 WhatsApp；只有明确标注或对应链接才可标为 verified。不要把同姓或首字母姓名自动展开为特定全名，也不要把一条公共渠道重复算为多人。

## 日常维护

| 周期/场景 | 操作 |
| --- | --- |
| 每次开始 | 核对代理、Docker、Dashboard 状态；小范围验证搜索相关性；确认 Hermes 模型和预算 |
| 每批结束 | 核对 jobs 和 batches，抽查成功、零结果与失败样本；分别统计新增人员、公共渠道及待核验人员 |
| 每日有运行时 | 查看任务日志、限流/验证码、模型失败及 usage；观察 `du -sh inputs outputs state` 的容量 |
| 改配置后 | 核对应用读取的配置和容器实际挂载，重启需要加载新环境的入口；先单家公司验证 |
| 升级/迁移前 | 停止新派发，等待在途任务结束，备份数据库、输入输出、配置与证据 |
| 清理前 | 先归档，保留实验 manifest、原始失败、代码版本及哈希；不要删除仍被任务库引用的输出 |

SearXNG 运维：

```bash
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml ps
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml logs --tail 100 searxng
docker inspect key-person-searxng --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'

# 修改 settings 或挂载路径后，在无在途采集时重新创建
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml up -d --force-recreate

# 停止该项目的搜索服务
docker compose --env-file deploy/searxng/.env -f deploy/searxng/compose.yml down
```

切换宿主机代理端口时同时修改应用代理和实际挂载的 SearXNG outgoing proxy，并保证预检读取同一个文件。不要将 `docker compose config` 的完整渲染结果发到公共日志，它可能含展开后的秘密值。

当前没有自动日志轮转和数据保留策略。`dashboard.log` 轮转宜在 Dashboard 停止后归档再启动；任务日志要与输入/输出一起保存。可归档旧结果后移出活跃 `outputs/` 以减少加载量，但页面将不再展示这些文件，恢复方式和存放位置应一并记录。

## 备份、恢复与迁移

Git 推送不是业务数据备份。应保留 `inputs/`、`outputs/`（含 `.artifacts`）、`state/`、应用 `.env`、Compose `.env`、正在使用的 `settings.local.yml`（若有）、LaunchAgent，以及 Hermes 模型/profile 配置的安全交接方式。Hermes 和 Twenty 的服务端数据不在本项目备份范围内。

为保证文件之间一致，先停新任务并等待在途任务完成，再卸载 Dashboard。SQLite 不要在运行中仅复制主 `.sqlite3` 文件；可使用标准库 backup API。以下示例按默认 `--db/--inputs/--outputs` 路径备份；使用自定义路径时需对应调整，所有任务数据库都应采用一致性备份。备份位于用户私有目录，包含运行数据和秘密文件，不应上传仓库：

```bash
.venv/bin/python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import shutil, sqlite3, subprocess, os
os.umask(0o077)
root = Path.cwd().resolve()
backup = Path.home() / 'Backups/key-search' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
backup.mkdir(parents=True, exist_ok=False)
for name in ('inputs', 'outputs', 'state'):
    if (root / name).exists():
        shutil.copytree(root / name, backup / name, ignore=shutil.ignore_patterns('jobs.sqlite3*'))
if (root / 'state/jobs.sqlite3').is_file():
    with sqlite3.connect((root / 'state/jobs.sqlite3').as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(backup / 'state/jobs.sqlite3') as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
for name in ('.env', 'deploy/searxng/.env', 'deploy/searxng/settings.yml', 'deploy/searxng/settings.local.yml'):
    source = root / name
    if source.is_file():
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
plist = Path.home() / 'Library/LaunchAgents/com.aceler.key-person-dashboard.plist'
if plist.is_file():
    shutil.copy2(plist, backup / plist.name)
(backup / 'commit.txt').write_text(subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True))
(backup / 'requirements-snapshot.txt').write_text(subprocess.check_output(['.venv/bin/pip', 'freeze'], text=True))
print(backup)
PY
```

该快照记录已提交代码版本；如果有未提交源码修改，另保存受控补丁，不把包含 CRM 数据的全仓库 diff 当作可公开补丁。检查备份容量和完整性，并按组织要求加密保管。

恢复/迁移顺序：

1. 在目标机器取得备份对应的代码版本，重新创建 `.venv` 并 `pip install -e '.[test]'`、安装 Chromium。**不要直接复用移动前的虚拟环境**，其脚本包含绝对路径。
2. 停止所有读取旧任务库的进程后，恢复输入、输出、证据、SQLite 和私有配置；不要将旧 WAL 文件随意混入备份恢复的数据库。
3. 修改 Hermes、psql、代理、Compose 挂载和 LaunchAgent 的绝对路径。任务库的 `input_path/output_path/log_path` 也保存绝对路径：跨目录移动须备份后做受控路径迁移；不能只改 plist。当前没有一键历史任务路径迁移命令。
4. 不要在新旧目录同时运行同一批次。若不能迁移旧任务库，保留它作只读历史，另用仓库内的新 `--db`；这会丢失旧库的已处理公司去重信息，Batch 可能重跑历史公司。
5. 启动 Docker/代理，核实挂载和模型；验证 CLI 导入路径、CRM dry-run、单家公司，再加载 Dashboard。验证前保留旧备份，不覆盖冻结失败样本。

## 故障排查

| 现象 | 先查什么 | 处理原则 |
| --- | --- | --- |
| `KEY_PERSON_PROXY_URL is required` | CLI 是否实际加载 `.env`，进程环境是否有空值 | 使用显式加载示例，纠正加载顺序 |
| `Local SearXNG proxy does not match` | 应用代理、预检 YAML、Docker 实际挂载 | 同步三者；预检只检查本地配置与 HTTP/浏览器出口，不保证每个搜索引擎可用 |
| `Unable to verify proxy egress` / 出口不一致 | 代理服务、Crawl4AI、出口检查站点、Docker 网络 | 排查出口，不跳过校验来掩盖路由不一致 |
| SearXNG 200 但没有有效结果 | 原始响应、`unresponsive_engines`、结果是否对应目标公司 | 区分正常空结果、相关性筛掉、限流和验证码；不要反复扩大请求 |
| `All ... engines suspended` | 同一客户端内连续失败记录 | 客户端两次失败后暂停该引擎；新任务可再试，但不代表上游限流已解除 |
| `Hermes command is not executable` | 路径、权限、解释器、LaunchAgent 环境 | 使用可执行绝对路径；不要沿用旧机器路径 |
| Hermes 超时/JSON 校验失败 | 任务日志、`usage*.json`、`diagnostics.json` | 确认实际模型/额度/适配参数；超时仅允许 30–3600 秒，补搜失败会保留首轮结果 |
| Chromium 缺失或抓取启动失败 | 当前 `.venv` 与 Playwright 浏览器安装 | 用当前虚拟环境重新安装 Chromium；旧虚拟环境移动后需重建 |
| `psql client is unavailable` | `PSQL_BIN` 或进程 PATH | 安装本机 libpq，给后台进程显式绝对路径 |
| CRM 查询 503 / `CRM query failed (psql)` | 网络、数据库账号/schema、字段、环境加载顺序 | 用 batch dry-run 做只读验证；终端能连不证明 LaunchAgent 的 VPN 路由也正常 |
| Sitemap `CERTIFICATE_VERIFY_FAILED` | 系统/解释器信任链、站点证书、代理证书 | 正确配置可信证书，不关闭 TLS 验证 |
| `Operation not permitted` | 项目是否在 Documents、解释器权限 | 使用 Projects 目录或精确授权，再处理迁移路径 |
| `Address already in use` | `lsof -nP -iTCP:18181 -sTCP:LISTEN` 和 launchctl | 识别已有服务，不重复启动或误杀其他项目 |
| 页面有任务但结果为空 | 当前批次筛选、job stage、JSON/证据路径 | `no_new_contact` 是正常终态；旧任务可能引用迁移前路径 |
| 人名很多但可联系人数少 | 任职、个人渠道、CRM 重复、公共渠道口径 | 查看 pending/rejections/diagnostics，不降低核验标准凑数 |
| 重启后没有恢复任务 | Runner PID、心跳、`queued/running` 状态 | Dashboard 重启不等于 Runner 复活；先确认失活，再手动重试 |

不含个人数据的接口状态检查：

```bash
curl --fail --silent --show-error -o /dev/null -w 'jobs HTTP %{http_code}\n' http://127.0.0.1:18181/api/jobs
curl --fail --silent --show-error -o /dev/null -w 'results HTTP %{http_code}\n' http://127.0.0.1:18181/api/results
curl --fail --silent --show-error -o /dev/null -w 'batches HTTP %{http_code}\n' http://127.0.0.1:18181/api/batches
```

这些检查不验证 CRM 或模型是否可用。`/api/crm/companies?q=...` 是只读查询；`POST /api/discover` 会真实创建任务，不应当作无副作用的健康检查。

## 开发、测试与发布

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -q
git diff --check
```

截至交接，212 项固定数据测试通过，不启动真实搜索或模型任务。可定向运行 `-p test_retrieval_repair.py`（来源暂停、预算、官网优先、重定向、晚附账号去重）或 `-p test_people_recall.py`。单元测试通过不等于在线召回增长。

更新现有部署：先停止新派发、等待在途任务、完成备份；检查 `git status --short`，解决相关本地代码修改后再 `git pull --ff-only`。如果被已跟踪的运行文件阻挡，不要丢弃数据或把 CRM 文件混入提交，应先单独备份并处理冲突。安装依赖、运行测试；根据改动重启 Dashboard 或重新创建 SearXNG。模型和数据库版本变更另做小样本验证。

发布前：核对 diff、同步 README、保存实验输入/代码版本/原始失败，明确哪些是单元测试、离线回放和真实联网结果。对照使用同预算和冻结公司，报告 CRM 去重后的独立人员及渠道；来源失败、模型随机差异和重复账号不能当作增量。

只暂存明确的源码、测试、部署模板和文档路径，再检查 `git diff --cached --name-only`、`git diff --cached --check` 后提交并 `git push origin main`。不要默认 `git add -A`；新运行数据被忽略并不意味着历史跟踪文件安全。禁止提交 `.env`、密钥、任务库、CRM 快照和私人模型日志。

回退代码应保留已推送历史，使用经过审查的 `git revert` 或单独恢复分支，不强推覆盖共享 `main`。数据库及输出回退需用对应备份，代码回退不会自动恢复任务状态；目前没有数据库降级脚本。

## 已知限制与后续工作

1. **来源连续可用性不足。** 最近 Google CSE 能返回相关结果但也触发限流；Bing 无关结果、Qwant CAPTCHA、DuckDuckGo Web 解析失败。Brave/Mojeek/Startpage 的独立实验也未验证增益，保持非默认。详见[历史实验](docs/discovery-and-validation.md)。
2. **联系人净增量仍未验收。** 最近配对原始计数为 Gouda 4→1、RATH 8→8、Calderys 0→0。Gouda 后续 2 条记录实际共用一个账号，去重为 1。官网证据覆盖改善不能写成新增人员增长。
3. **任职判断与渠道归属仍要人工复核。** 相同 LinkedIn 摘要可能被模型标成 verified 或 probable；旧 HTML 新闻时效、集团/子公司、缩写/全名关系仍可能含糊。不要靠更宽的自动合并提高数字。
4. **运行管理不是完整调度平台。** 没有认证、一键取消、日志轮转、数据保留策略或跨目录迁移器；免费来源不足时提高并发往往只会增加限流。
5. **依赖和部署需要维护。** 没有全依赖锁文件，Hermes 为外部适配，macOS 模板含绝对路径占位符；Linux/新 Twenty schema 需要单独验证。PDF 无 OCR，反爬/robots 阻止的页面不保证获取。

下一轮建议先解决来源限流下的稳定采集和任职判定一致性，再用新冻结样本验证“新增可联系人员”；优先使用现有来源与证据，不先扩大请求数量或引入新框架。

## 交接验收清单

- [ ] 接手人已确认仓库、分支、代码版本和实际运行路径。
- [ ] 已通过安全渠道取得应用/CRM/Compose 配置及 Hermes profile；未将秘密写入 Git。
- [ ] Python editable 安装指向新仓库，Chromium、psql、代理、Docker 和 Hermes 均可用。
- [ ] CLI、Dashboard、Batch 的环境优先级及启动命令已理解，未重复启动服务。
- [ ] CRM dry-run、搜索相关性和一家公司完整流程分别验证；预算和模型费用已核对。
- [ ] 知道日志、SQLite、原始证据及冻结实验的位置，已验证备份和恢复路径。
- [ ] 理解独立 Runner 的停止/重试边界、Dashboard 无认证和联系人计数口径。
- [ ] 已阅读最新限制；没有把历史实验、公共渠道或重复账号称为联系人净增量。
