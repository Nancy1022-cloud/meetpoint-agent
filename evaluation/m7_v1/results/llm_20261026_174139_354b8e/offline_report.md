# M7 离线受控评测报告

数据：自建有限候选全集；真实地图请求为0。作者已见这些题，不称独立盲测集。
决策来源：真实LLM（仅地图工具为合成）。
冻结时间：2026-10-26T17:40:10.833047+08:00；配置模型：qwen3.8-27b。

| 策略 | 有解任务运行中找到方案 | 推荐违规项 | 预算越界 | 模拟HTTP总数 | 真实模型调用 |
| --- | --- | ---: | ---: | ---: | ---: |
| B0 | 7/9 | 0 | 0 | 111 | 0 |
| B1 | 14/18 | 0 | 0 | 232 | 34 |

B0每任务一次；实模B1每任务2或3次。总调用数的运行次数不同，不直接据总数比较成本。
逐任务、逐重复的调用差值和质量差值在metrics.json的paired_deltas中，完整运行记录在planning.jsonl中。
成功率分母只包括自建全集中已知有解的任务运行；无解、未知及无路线任务仍保留在明细。

| 任务 | 重复 | B0交付 | B1交付 | 模拟HTTP差B1-B0 | 最长耗时差/秒 | 时间差之差/秒 | 总耗时差/秒 |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| S01 | 1 | True | True | 0 | 0 | 0 | 0 |
| S01 | 2 | True | True | 0 | 0 | 0 | 0 |
| S02 | 1 | False | True | 5 | 不比较 | 不比较 | 不比较 |
| S02 | 2 | False | True | 5 | 不比较 | 不比较 | 不比较 |
| S03 | 1 | False | True | 5 | 不比较 | 不比较 | 不比较 |
| S03 | 2 | False | True | 5 | 不比较 | 不比较 | 不比较 |
| S04 | 1 | True | False | -5 | 不比较 | 不比较 | 不比较 |
| S04 | 2 | True | False | -5 | 不比较 | 不比较 | 不比较 |
| S05 | 1 | True | False | -5 | 不比较 | 不比较 | 不比较 |
| S05 | 2 | True | False | -5 | 不比较 | 不比较 | 不比较 |
| S06 | 1 | True | True | 2 | 0 | 0 | 0 |
| S06 | 2 | True | True | 2 | 0 | 0 | 0 |
| S07 | 1 | True | True | 3 | -540 | 120 | -1200 |
| S07 | 2 | True | True | 3 | -540 | 120 | -1200 |
| S08 | 1 | False | False | 0 | 不比较 | 不比较 | 不比较 |
| S08 | 2 | False | False | 0 | 不比较 | 不比较 | 不比较 |
| S09 | 1 | False | False | 0 | 不比较 | 不比较 | 不比较 |
| S09 | 2 | False | False | 0 | 不比较 | 不比较 | 不比较 |
| S10 | 1 | False | False | 0 | 不比较 | 不比较 | 不比较 |
| S10 | 2 | False | False | 0 | 不比较 | 不比较 | 不比较 |
| S11 | 1 | True | True | 0 | 0 | 0 | 0 |
| S11 | 2 | True | True | 0 | 0 | 0 | 0 |
| S12 | 1 | True | True | 0 | 0 | 0 | 0 |
| S12 | 2 | True | True | 0 | 0 | 0 | 0 |

双方都交付的质量配对覆盖：10/24。负值表示B1指标更小。
不会将基线失败的任务塞入质量均值，也不会丢掉它们：所有成功/失败配对均在上表。

## Memory 状态保留对照

两个系统复用同一受控解析器和存储规则；窗口基线每次从默认任务重放最近3条用户事件。

- structured：8/8 个任务的完整条件、偏好和待澄清状态全部匹配。
- recent_events_replay：5/8 个任务的完整条件、偏好和待澄清状态全部匹配。

窗口基线从默认任务重放最近N条自建用户事件；不代表所有聊天模型。这不是原大纲的等Token、实模Memory消融实验。

## 成本及适用范围

planning.jsonl逐条保留模型返回名、Token、usage覆盖及费用估算；未提供单价时费用为null。
合成工具的执行时间不代表真实地图端到端延迟。LLM模式耗时包含模型网络请求，地图部分仍为合成。
模拟HTTP只是冻结计费规则下的请求次数，不包含真实QPS、超时、重试分布或地图价格。
全集最优值仅针对当前自建有限候选与确定性排序，不能外推为全城最优。
实模每次重复单独保留；温度0也不能保证结果一致。小样本结果不外推。

## 未交付与错误明细

- S02 B0 第1次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S03 B0 第1次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S04 B1 第1次：no_route；budget_exhausted:search_rounds；error_code=None。
- S04 B1 第2次：no_route；budget_exhausted:search_rounds；error_code=None。
- S05 B1 第1次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S05 B1 第2次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S08 B0 第1次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S08 B1 第1次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S08 B1 第2次：constraints_unsatisfied；budget_exhausted:search_rounds；error_code=None。
- S09 B0 第1次：data_missing；budget_exhausted:search_rounds；error_code=None。
- S09 B1 第1次：data_missing；budget_exhausted:search_rounds；error_code=None。
- S09 B1 第2次：data_missing；budget_exhausted:search_rounds；error_code=None。
- S10 B0 第1次：no_route；budget_exhausted:search_rounds；error_code=None。
- S10 B1 第1次：no_route；budget_exhausted:search_rounds；error_code=None。
- S10 B1 第2次：no_route；budget_exhausted:search_rounds；error_code=None。
