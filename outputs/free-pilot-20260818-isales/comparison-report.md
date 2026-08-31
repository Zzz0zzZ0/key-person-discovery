# iSales vs Key Search 免费链路三公司对照测试

测试日期：2026-08-18（Asia/Shanghai）

## 测试边界

- 公司：Grinding Techniques、Samancor Chrome、Pace Industries。
- iSales：只读既有结果，没有新增挖掘、添加联系人、导出或发信。
- Key Search：独立 CLI 隔离输出；每家公司 AnySearch 自适应上限 7 次，海关预算 0，最多抓取 20 个 URL。
- CRM 只读；没有创建 SQLite 任务或批次，没有重启服务。

## 原始对照

| 公司 | iSales 总数 | iSales 人员记录 | 姓名去重后 | 公共邮箱 | 公共电话 | 人员邮箱候选值 | Key Search 已验证 | 待验证 | 带个人联系方式的已验证人员 | 未绑定公共联系方式 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Grinding Techniques | 72 | 61 | 61 | 6 | 5 | 261 | 1 | 0 | 1（仅 LinkedIn） | 11 |
| Samancor Chrome | 28 | 22 | 21 | 5 | 1 | 37 | 15 | 3 | 0 | 6 |
| Pace Industries | 39 | 38 | 37 | 0 | 1 | 61 | 6 | 0 | 0 | 0 |
| **合计** | **139** | **121** | **119** | **11** | **7** | **359** | **22** | **3** | **1** | **17** |

## 搜索成本

| 公司 | AnySearch | 搜索结果 | 抓取 URL | 抓取失败 | 联系方式信号 |
|---|---:|---:|---:|---:|---:|
| Grinding Techniques | 7 | 44 | 6 | 0 | 34 |
| Samancor Chrome | 7 | 40 | 18 | 3 | 14 |
| Pace Industries | 7 | 45 | 20 | 0 | 1 |
| **合计** | **21** | **129** | **44** | **3** | **49** |

## 关键发现

1. iSales 的总数不是纯人员数。三家公司共 139 条，其中 18 条是公共邮箱或电话；页面“免费获取”数量与公共联系方式数量逐家公司完全一致。
2. iSales 的个人邮箱更接近候选模式集合，而非每人一个已验证邮箱。Grinding Techniques 的 61 人对应 261 个邮箱候选值，单人可出现多个同域名组合和其他域名/免费邮箱。
3. Key Search 的官网公共联系方式覆盖达到 17/18；唯一遗漏是 Pace 的 vanity phone `1-888-DIE-CAST`。公共渠道提取已接近 iSales。
4. Key Search 当前更擅长官网领导团队：Samancor 找到 15 名现任高管、CTO、矿山/冶炼厂 GM；Pace 找到 6 名官网现任高管。
5. iSales 更擅长采购、工厂和生产中层：Samancor 的 21 个唯一人员以采购岗位为主，Pace 的 37 个唯一人员以 Plant/Purchasing/Production/Quality 为主。
6. 两套结果精确姓名重合只有 Grinding Techniques 的 Stefan Meyer 一人。Samancor 和 Pace 均为 0 重合，说明免费链路不是只少一点，而是人员发现入口偏向了不同组织层级。
7. 三家公司都用满 7 次 AnySearch。继续单纯增加查询次数不太可能弥补 iSales 的人员数据库覆盖。

## 结论

当前版本还不能替代 iSales 的人员发现功能，但已经基本替代其公司公共联系方式提取，并在官网当前任职验证上更可靠。真正缺口是：从标准 LinkedIn 公司身份或公开索引批量发现采购、工厂、生产、质量和技术中层，再进行当前任职验证。

优先改进顺序：

1. 将搜索源从顺序兜底改为有限结果融合，专门服务人员发现。
2. 围绕已确认 LinkedIn 公司页和官网域名执行职位族查询，重点覆盖 procurement、purchasing、supply chain、plant、production、operations、quality、technical、engineering。
3. 建立标准化姓名去重和跨来源证据图，不要求单页同时包含姓名、职位和联系方式。
4. 只有在已确认人员和公司邮箱格式后生成邮箱候选，并明确标注 `inferred`；MX/SMTP 只验证技术状态，不证明人员归属。
5. 继续保持公共邮箱、总机与人员记录分离。

## 产物

- `grinding-techniques.json` 及其 `.artifacts/`
- `samancor-chrome.json` 及其 `.artifacts/`
- `pace-industries.json` 及其 `.artifacts/`

运行前后 SQLite 任务状态均为 completed 135、failed 64；批次状态均为 completed 3、failed 10，没有 queued/running。

附带发现：Crawl4AI 代理预检会复用旧 IP 检查缓存，造成 `Crawl4AI egress does not match KEY_PERSON_PROXY_URL` 假失败。本次通过隔离临时缓存目录完成测试，没有清理全局缓存或修改项目代码。Crawl4AI 0.9.2 自身在运行时创建了其全局数据库备份并执行迁移；项目 SQLite 和 CRM 未受影响。
