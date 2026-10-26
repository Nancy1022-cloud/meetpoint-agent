# M7 实模Memory独立对照

同样2048输入Token上限，用服务usage事后核验；缺usage或超限的样本保留，passed=false。同模型、提示、工具、输出上限；只改变上下文表示。无重试，逐重复保留。

| 上下文表示 | 完整匹配且协议通过 | 输入Token上限已核验 | API或格式错误 |
| --- | --- | --- | ---: |
| structured | 16/16 | 16/16 | 0 |
| recent_events | 8/16 | 16/16 | 2 |

| 任务 | 重复 | 表示 | 匹配且协议通过 | 输入Token | 输出Token | 错误 |
| --- | ---: | --- | --- | ---: | ---: | --- |
| R01 | 1 | structured | True | 1147 | 151 | None |
| R01 | 1 | recent_events | False | 1069 | 151 | None |
| R01 | 2 | recent_events | False | 1069 | 151 | None |
| R01 | 2 | structured | True | 1147 | 151 | None |
| R02 | 1 | recent_events | False | 1068 | 151 | None |
| R02 | 1 | structured | True | 1147 | 151 | None |
| R02 | 2 | structured | True | 1147 | 151 | None |
| R02 | 2 | recent_events | False | 1068 | 151 | None |
| R03 | 1 | structured | True | 1154 | 158 | None |
| R03 | 1 | recent_events | True | 1086 | 158 | None |
| R03 | 2 | recent_events | True | 1086 | 158 | None |
| R03 | 2 | structured | True | 1154 | 158 | None |
| R04 | 1 | recent_events | False | 1099 | 160 | invalid_memory_output |
| R04 | 1 | structured | True | 1154 | 158 | None |
| R04 | 2 | structured | True | 1154 | 158 | None |
| R04 | 2 | recent_events | False | 1099 | 160 | invalid_memory_output |
| R05 | 1 | structured | True | 1157 | 161 | None |
| R05 | 1 | recent_events | True | 1082 | 161 | None |
| R05 | 2 | recent_events | True | 1082 | 161 | None |
| R05 | 2 | structured | True | 1157 | 161 | None |
| R06 | 1 | recent_events | True | 1069 | 151 | None |
| R06 | 1 | structured | True | 1147 | 151 | None |
| R06 | 2 | structured | True | 1147 | 151 | None |
| R06 | 2 | recent_events | True | 1069 | 151 | None |
| R07 | 1 | structured | True | 1147 | 151 | None |
| R07 | 1 | recent_events | True | 1098 | 151 | None |
| R07 | 2 | recent_events | True | 1098 | 151 | None |
| R07 | 2 | structured | True | 1147 | 151 | None |
| R08 | 1 | recent_events | False | 1069 | 151 | None |
| R08 | 1 | structured | True | 1147 | 151 | None |
| R08 | 2 | structured | True | 1147 | 151 | None |
| R08 | 2 | recent_events | False | 1069 | 151 | None |

这些任务是作者可见的小规模自建对话；不外推到任意对话、模型或Token窗口。
两个系统均不进行地图搜索；只检查条件、偏好及澄清状态。完整输入表示受同一输入上限约束。
有输入超限、缺usage或接口失败时，单独报告覆盖；不能把剩下的成功样本冒称全量结果。
