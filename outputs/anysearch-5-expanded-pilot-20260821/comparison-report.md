# 默认5次AnySearch扩展样本测试（2026-08-21）

## 范围

- 公司：Heatmasters、Sialon Ceramics、Ardakan Industrial Ceramics、Rubin Trading、Refractarios y Aislamientos de San Luis。
- 每家公司实际使用5次AnySearch，合计25次；海关调用0次。
- 使用宽搜融合、SearXNG Top-10和官网高价值页面/PDF优先级。
- CRM只读快照去重；未创建SQLite任务或批次，未触发发信。

## 结果

| 公司 | 去重联系方式 | 邮箱 | 电话 | LinkedIn | CRM去重 | 备注 |
|---|---:|---:|---:|---:|---:|---|
| Heatmasters | 9 | 2 | 4 | 3 | 6 | 官网保持 `heatmasters.net`；1个Heatmasters Mechanical误匹配 |
| Sialon Ceramics | 3 | 0 | 1 | 2 | 0 | 两个LinkedIn URL实际为同一Nico van Dongen |
| Ardakan Industrial Ceramics | 4 | 0 | 0 | 4 | 2 | 均为公司关联明确但当前任职未确认的LinkedIn |
| Rubin Trading | 21 | 5 | 14 | 2 | 9 | 官网员工页贡献较大；1个“Other similar profiles”误匹配 |
| Refractarios San Luis | 3 | 0 | 3 | 0 | 2 | 旧结果为0，本轮官网及公开PDF获得3个电话 |
| **合计** | **40** | **7** | **22** | **11** | **19** | 明显误匹配/重复3条，人工净值约37条 |

## 覆盖与来源

- 5/5家公司获得至少一种公开联系方式。
- 4/5家公司获得邮箱或电话；Ardakan仅获得LinkedIn。
- 5家公司共抓取64个网页，12个失败；成功提取9份PDF，5份PDF失败或不可提取。
- Rubin官网人员页面提供4名具名业务联系人及多个未绑定公司渠道。
- Heatmasters官网身份没有再次切换到Heatmasters Mechanical，但宽搜LinkedIn候选中仍混入该公司的Tim Dowd。
- 公开PDF联系方式继续标记 `published_document_unverified`，未强行绑定个人。

## 质量问题

1. 同名公司消歧还需覆盖LinkedIn候选正文中的完整雇主名，不能只校验目标短名称。
2. 同一LinkedIn稳定ID可能使用不同slug，Sialon出现同一人重复计数。
3. 搜索摘要中的“Other similar profiles”可能把摘要里提到的另一人职位错误赋给结果主体。

## 结论

默认5次在扩展样本上仍能获得较高联系方式数量：每家公司平均8条原始渠道、人工扣除明显噪声后约7.4条。官网联系方式和PDF优先策略有效；下一项优化应是三条确定性的人名消歧规则，而不是增加AnySearch调用。

