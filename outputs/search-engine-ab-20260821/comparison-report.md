# Key Search 搜索引擎扩展隔离测试（2026-08-21）

## 边界

- 未创建 SQLite 任务或批次，未写 CRM，未触发发信。
- B 组复用 2026-08-18 的三家公司宽搜结果作为历史 A 组，不重复消耗 A 组 AnySearch。
- B 组公司：Grinding Techniques、Samancor Chrome、Pace Industries。
- B 组 AnySearch 上限及实际调用均为每家公司 7 次，合计 21 次；海关搜索 0 次。
- 实验配置：宽搜融合开启，SearXNG 显式使用 Bing、DuckDuckGo、Google CSE、Qwant、Yandex；默认生产配置不启用额外引擎。

## 全流程结果

| 指标 | 历史 A 组 | 本次 B 组 |
|---|---:|---:|
| AnySearch 调用 | 21 | 21 |
| 搜索结果 | 225 | 226 |
| 抓取 URL | 45 | 56 |
| 抓取失败 | 2 | 1 |
| 联系方式信号 | 77 | 159 |
| 公司公共联系方式 | 16 | 20 |
| 旧口径“可联系项目”（不再作为主指标） | 26 | 27 |

B 组相对历史 A 组出现 7 个新名字：Thembinkosi Mabena、Nicolene Breitenbach、Farhana Rahaman、Aveer Chandraprakash、John Conquest、Tammy Johnson Onsum、Christopher Baumann。

- John Conquest（Operations Manager）和 Tammy Johnson Onsum（Purchasing Manager）来自本次 AnySearch 返回，不是 Yandex 独有增量。
- Christopher Baumann 的 LinkedIn 由 Yandex 直接召回，公开摘要显示 Pace Industries 的 Sr. Director of Sales。
- Aveer Chandraprakash 的 Yandex 摘要包含过去 secondment 描述，只能作为低置信待核验项。
- 新增姓名均没有获得直接绑定的个人邮箱或电话。

历史 A 组与本次 B 组相隔三天，搜索排名、官网页面和 Hermes 输出均有波动，因此不能把全部差异归因于 Yandex。

## 联系方式口径修正

业务目标是增加公开联系方式总量，不要求先绑定到具体人员。因此，后续主指标改为去重后的邮箱、电话、WhatsApp和LinkedIn数量；任职、人员归属和联系方式归属仅作为验证标签。

- 本次 B 组的联系方式信号由 77 增至 159，公司公共联系方式由 16 增至 20，说明扩展搜索对联系方式召回有效。
- 放开“PDF必须排除”的硬门槛后，使用本轮已保存数据离线重算，三家公司去重联系方式由 27 增至 35，增加 8 条（+29.6%）。
- 8 条增量均来自 Samancor 官网公开PDF，包括2个同域邮箱、1个外部机构邮箱和5个电话号码；全部标记为“公开文档、归属待核验”，不绑定给个人。
- Grinding Techniques和Pace Industries在本轮保存数据中没有额外PDF联系方式，因此增量为0。

## 同次返回的 Top-5 / Top-10 对照

为消除时间波动，使用同一次实时 SearXNG 返回，仅改变保留数量：宽搜模式从每查询前 5 条扩大到前 10 条，AnySearch 调用不增加，网页抓取仍受 `max_urls` 限制。

| 公司 | Top-5 候选 | Top-10 新增候选 |
|---|---|---|
| Grinding Techniques | DJC Richardson | Corina Radford |
| Samancor Chrome | Aveer Chandraprakash | Maretha Steyn、Sepadi Ngoasheng |
| Pace Industries | Bill Estep | Amisha Anand Patankar、Justin Hesse、Christopher Baumann |

Top-10 共新增 6 个 LinkedIn 候选。其中 Corina Radford、Sepadi Ngoasheng、Amisha Anand Patankar、Justin Hesse、Christopher Baumann具有合理的目标公司关联；Maretha Steyn 使用了与 Aveer 相同的 secondment 摘要，误匹配风险高。新增候选仍应标为 `unverified` 或 `probable_current`，不能直接视为个人联系方式已验证。

## 引擎健康结论

- Yandex：无需 Key，可返回 LinkedIn 个人页，是本轮唯一确认有联系人召回增量的新增引擎。
- Naver：英文测试 0 结果，仅适合有韩文公司名的韩国公司定向实验。
- Baidu：出现 CAPTCHA 或明显中文泛化噪声，仅适合中文公司定向实验。
- Mojeek：当前代理链路出现 HTTP connection error，未保留。
- Seznam：当前代理链路超时，未保留。

## 应用状态

- 默认 SearXNG 仍只启用 Bing、DuckDuckGo、Google CSE、Qwant。
- Baidu、Naver、Yandex 已加载但默认关闭。
- 只有同时设置以下实验变量才使用扩展引擎：

```dotenv
KEY_PERSON_EXPERIMENTAL_BROAD_DISCOVERY=1
KEY_PERSON_EXPERIMENTAL_SEARXNG_ENGINES=bing,duckduckgo,google cse,qwant,yandex
```

- 宽搜模式下 SearXNG 每查询保留量已从 5 调整为 10；默认模式仍为 5。
- 输出现按联系方式数量统计；公司来源公开联系方式直接保留，目标公司相关公开页面及公开PDF中的联系方式也可保留，但明确标注归属验证状态。
- 当前结论：扩展搜索与分层联系方式输出对增加联系方式数量有效；尚未启动批量生产。
