# Key Search 宽进严标隔离试验（2026-08-18）

## 边界

- 公司：Grinding Techniques、Samancor Chrome、Pace Industries。
- AnySearch：每家公司最多 7 次；合计 21 次。
- 海关搜索：0 次。
- CRM、项目 SQLite、批次和运行中的 Dashboard：未写入、未重启。
- 实验开关：`KEY_PERSON_EXPERIMENTAL_BROAD_DISCOVERY=1`；默认关闭。

## 汇总

| 指标 | 原基线 | 宽搜原始实跑 | 最终确定性过滤后 |
|---|---:|---:|---:|
| AnySearch 调用 | 21 | 21 | 21 |
| 搜索结果 | 129 | 225 | 225 |
| 抓取 URL | 44 | 45 | 45 |
| 抓取失败 | 3 | 2 | 2 |
| 已验证候选 | 22 | 27 | 25 |
| 待核验候选 | 3 | 13 | 10 |
| `probable_current` | 未分层 | 1 | 5 |
| 未绑定公共联系方式 | 17 | 16 | 16 |
| 可联系项目 | 18 | 18 | 16 |
| 推测邮箱 | 0 | 0 | 0 |

“最终确定性过滤后”是在内存中使用最终版后处理函数重放原始 JSON 和搜索证据；没有重新调用搜索。原始 JSON 保留实跑原貌，便于审计。

## 有价值增量

- Grinding Techniques：Stefan Meyer，Managing Director；旧基线为已验证，本轮证据波动后保留为 `probable_current`。
- Samancor Chrome：Wilma Naude，Operations Manager；新增 `probable_current`。
- Samancor Chrome：Christo Swanepoel，General Manager；新增 `probable_current`。
- Pace Industries：Cathy Voskuil，Senior Buyer；新增 `probable_current`。
- Pace Industries：Jon Osborn，Purchasing Manager；新增 `probable_current`。

相对旧基线，真正新增且优先级较高的待核验人员为 4 名。Pace 还新增两名官网可验证的 Division President；Samancor 新增两名官网领导，但其岗位偏企业事务和人力，并非优先采购角色。

## 发现并修复的噪声

- Hermes 曾把 Grinding Techniques 官网的 5 个招聘职位输出为 `Unknown ...` 人员。最终版会确定性删除 Unknown/vacancy/job_posting 非人员记录。
- Samancor 的 2023 年 PAIA PDF 曾把两名法务联系人升级为当前已验证。最终版会把仅有过期 PDF 且无 current/present/since/appointed 证据的人员降为待核验。
- Hermes 先产出的 probable 候选最初没有统一 `discovery_tier`。最终版统一标为 `verified_current`、`probable_current` 或 `unverified`。

## 结论

有限来源融合在不增加 AnySearch 调用的情况下，将搜索结果从 129 提高到 225，并新增 4 名业务相关的当前任职待核验人员。但它没有新增个人邮箱或电话，可联系项目反而由 18 降到 16；更多结果也没有增加抓取总量，因为仍受 `max_urls` 限制。

因此当前版本适合保留为默认关闭的人工研究实验，不建议接入批量生产。下一轮最值得做的是改进“个人页/公开索引证据的定向抓取与排序”，而不是继续增加查询次数或放宽联系方式绑定。
