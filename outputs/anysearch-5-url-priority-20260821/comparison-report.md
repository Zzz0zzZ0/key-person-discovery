# AnySearch默认5次与联系方式来源优先测试（2026-08-21）

## 应用内容

- CLI默认AnySearch上限由7改为5，环境变量仍可人工覆盖。
- 官网主页、联系/管理/团队页面优先于普通新闻页面。
- PDF仍最多处理3份，不扩大资源预算；PAIA、POPI、manual、supplier、profile等联系方式密集文档优先。
- 默认5次流程不运行第6–7次姓名任职验证；显式7次仅保留人工对照兼容能力。

## 真实测试边界

- 公司：Grinding Techniques、Pace Industries、Samancor Chrome。
- 新组未设置 `KEY_PERSON_ANYSEARCH_MAX_QUERIES`，用于验证默认5次实际生效。
- 共使用15次AnySearch，海关调用0次。
- 未创建SQLite任务或批次，未修改CRM，未触发发信。

## 效果

| 指标 | 7次基线 | 优化前5次 | 优化后默认5次 |
|---|---:|---:|---:|
| AnySearch调用 | 21 | 15 | 15 |
| 去重联系方式 | 35 | 35 | 52 |
| 邮箱 | 14 | 12 | 19 |
| 电话 | 14 | 12 | 19 |
| LinkedIn | 7 | 11 | 14 |
| 原始联系方式信号 | 159 | 151 | 168 |

相对7次基线：AnySearch调用降低28.6%，总联系方式增加48.6%，邮箱和电话合计由28增至38（+35.7%）。

| 公司 | 7次基线 | 优化前5次 | 优化后5次 |
|---|---:|---:|---:|
| Grinding Techniques | 12 | 13 | 12 |
| Pace Industries | 5 | 11 | 11 |
| Samancor Chrome | 18 | 11 | 29 |

Samancor的增量来自优先处理两份PAIA Manual和一份Supplier Orientation文档；此前5次组前三份PDF被社会责任报告、养老基金漫画等低联系方式密度文档占用。

## 验证状态与噪声

- 优化后共有18条公开PDF联系方式标记为 `published_document_unverified`，全部保持“未绑定人员”。
- Samancor PDF中包含2个外部监管机构邮箱，其余新增邮箱使用Samancor域名；外部邮箱没有被描述为目标公司邮箱。
- 普通目录页脚联系方式过滤回归测试继续通过。
- 本轮2个网页抓取失败，3家公司PDF均成功提取；搜索源告警49条，主要来自免费SearXNG引擎波动。

## 结论

方案达到验收线，默认5次可以保留。联系方式增量来自高价值官网来源排序，而不是增加AnySearch调用或放宽人员绑定。

