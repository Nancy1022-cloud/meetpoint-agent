M6 本机验收记录
记录日期：2026-10-26（北京时间）。

结论：M6 验收通过，第十六步完成，可以进入 M7。

本文件汇总用户提供的本机22项M6检查、159项M2～M6回归、一次CLI audit、L01～L05五份在线摘要、五组起点表截图及页面操作全部成功的明确确认。机器输出与用户手工确认分别注明依据。此前空白项已按这次补充材料完成验收记录。
1. 环境与记录范围
项目	已确认内容
项目路径	D:\AI PROJECTS\meetpoint-agent
在线记录起始时间范围	2026-10-26 13:38:38～13:44:50，UTC+08:00
代码版本	262d184907ee5e36（5份记录一致）
地图API版本	v5
配置模型 / 实际返回模型	qwen3.8-27b / qwen3.8-27b
Prompt SHA-256	bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464
参数	temperature=0，max_tokens=512，enable_thinking=false；SDK自动重试关闭，每次调用最多额外重试1次
全部记录的请求出发时间	2026-10-26 10:00
演示user_id / session_id	本轮未收集；使用5个run_id关联记录
五组实际起点	已按用户本轮提供的起点表截图登记，见第7节
限制与排序的选择方式	用户确认：沿用长期偏好或默认；不补写未单独导出的生效数值


摘要中的tested_at是运行记录时间，晚于本次请求的10:00出发时刻；本次按指定出发时刻请求路线，不表述成13点多“立即出发”的实时耗时。起点人工点选、卡片符合限制及其余页面操作均依据用户本轮“你说的现象都是成功”的明确确认记录为通过。
2. 本机测试
- [x] M6 新增22项检查通过。
Ran 22 tests in 7.273s
OK
- [x] 本轮M2～M6回归159项通过。
用户实际执行：
docker compose exec -T app python -m unittest app.test_m2 app.test_m3 app.test_m4 app.test_m5 app.test_m6 -q
实际输出：
Ran 159 tests in 19.579s
OK
本次回归范围为M2=33、M3=30、M4=31、M5=43、M6=22，合计159项。命令未包含app.test_m1，因而不记录成“本机188项通过”。M1是此前已完成阶段，本次按上述实际覆盖范围验收M6，无需为凑满188项重复运行整套回归。
3. 五次真实接口运行
以下保留用户的L01～L05标签。L02和L03的时间先后与标签顺序不同，记录按原标签保留。五组实际起点已由用户补充表格截图确认，见第7节。
编号	条件版本	查询轮数	已评估候选	可行候选	推荐数	地图HTTP	模型调用	总耗时s（含点选）	停止原因	结果
L01	1	3	7	2	1	19	2	27.726	搜索轮数到上限	返回可行方案
L02	4	2	6	3	3	16	1	22.753	可行候选达到3个	返回可行方案
L03	3	1	2	2	1	7	1	26.542	模型选择结束探索	返回可行方案
L04	5	1	1	1	1	5	1	20.489	模型选择结束探索	返回可行方案
L05	6	3	5	0	0	15	2	20.744	搜索轮数到上限	当前候选不满足约束


逐条判读：
- L01：完成方案交付，同时因搜索轮数达到3轮停止探索。 已得到2个可行候选、筛选后推荐1个；完整可行方案可以在预算结束时交付，不属于接口异常。
- L02：达到3个可行候选的共同停止条件。 第2轮结束，推荐3个。
- L03：模型选择提前结束。 已有2个可行候选，推荐1个。动作在协议内；是否值得继续搜索留给M7比较。
- L04：模型选择提前结束。 已有1个可行候选，推荐1个。仅凭本次结果不能判断其搜索质量优于基线。
- L05：正确结束探索，但没有完成方案交付。 3轮内评估5个候选，可行数0，outcome=constraints_unsatisfied，推荐数0。error_code=null；应保留这条未完成记录。它不能证明全城没有可行地点。
五份记录的status均为completed，表示运行执行结束；其中completed_plan为true的只有L01～L04。L05的“执行结束”和“未取得可行方案”需要同时记录。
运行标识
编号	tested_at（UTC+08:00）	run_id
L01	2026-10-26T13:38:38+08:00	202fe6bb13364103ba842ade4bc23b77
L02	2026-10-26T13:43:18+08:00	3e810f08331b40d49408820c102e8cc1
L03	2026-10-26T13:42:06+08:00	4285b9a11ca445729cb0066398f1d320
L04	2026-10-26T13:44:09+08:00	1f7e41fc484b46f19ed3a04f7961f3be
L05	2026-10-26T13:44:50+08:00	dcb0797600934cd591aeaadfcf9e1da6


调用与耗时汇总
- 有方案交付：4/5次。这是本次接入记录的观察值，不外推为通用成功率，也不称为准确率或可行方案召回率。
- 地图HTTP：合计62次，平均12.4次/运行；计数与real_map_http_attempts逐条一致。
- 模型调用：合计7次，平均1.4次/运行；与real_llm_attempts逐条一致。
- 搜索：合计10轮，评估21个候选。
- 地图、模型重试：均为0次；五份error_code均为null。
- 总耗时：合计118.254秒，平均23.651秒，中位数22.753秒，范围20.489～27.726秒。全部包含人工点选等待，不作为纯模型或自动规划延迟。
- usage报告：输入Token合计6581，输出Token合计349，共6930；7次模型调用均返回usage。未提供实际计价或免费额度扣减记录，因此不填写货币费用。
这些数据没有B0对照、冻结任务集与重复试验，不用于证明Agent策略改善程度。五组限制按各次生效的长期偏好或默认值处理，当前摘要未单独保存完整约束快照；M7冻结评测时再固定并留存这些输入。
4. 预算与计数一致性
- [x] 五次地图HTTP分别19、16、7、5、15，均≤40。
- [x] 五次模型调用分别2、1、1、1、2，均≤12。
- [x] 五次搜索轮数分别3、2、1、1、3，均≤3。
- [x] 当前轮候选计数均≤5，累计候选数均≤5×实际轮数。
- [x] 推荐数≤可行候选数≤已评估候选数，五份均一致。
- [x] 本次五条记录均符合“2次起点解析 + 实际搜索轮数 + 2×已评估候选数”的HTTP数量关系，且无重试。
这里验证已记录计数的一致性。起点点选、卡片符合当前限制等页面项，另按用户手工确认记录通过。
5. 持久化边界
- [x] 五次在线运行的persistent_field_boundary_passed全部为true。
- [x] 用户额外运行的CLI audit与L05运行ID和停止原因一致。
编号	checkpoint_count	边界检查	最新动作	completed_rounds
L01	11	true	toward_a	3
L02	8	true	toward_a	2
L03	6	true	middle	1
L04	6	true	middle	1
L05	11	true	toward_b	3


CLI audit原样整理如下：
{
  "storage": {
    "schema_version": 1,
    "tables": [
      "memory_events",
      "run_metrics",
      "sqlite_sequence",
      "task_sessions",
      "user_preferences"
    ],
    "task_count": 11,
    "run_count": 17
  },
  "memory_schema_version": 1
}
{
  "checkpoint_count": 11,
  "persistent_field_boundary_passed": true,
  "latest_control_state": {
    "stop_reason": "budget_exhausted:search_rounds",
    "run_id": "dcb0797600934cd591aeaadfcf9e1da6",
    "action": "toward_b",
    "completed_rounds": 3
  },
  "resume_policy": "read_confirmed_conditions_then_requery"
}
schema_version=1、memory_schema_version=1及上述表名与M5兼容。LangChainPendingDeprecationWarning没有阻断本次测试或audit，不作为本次验收失败项。
6. 第十六步逐项状态
验收项	当前状态	依据
M6 22项检查	已通过	用户本机22项、7.273秒、OK
M2～M6回归	已通过	用户本机159项、19.579秒、OK；本次不含M1
真实地图与模型已联通	已通过	amap_live、真实qwen3.8-27b及调用数；4次完成方案交付
5组不同起点及条件来源留档	已完成	用户提供起点表，确认沿用长期偏好或默认
网页创建条件、双方显式点选	已通过	用户本轮确认相应操作均成功
页面显示可行卡片，双方指标符合当前限制	已通过	4次completed_plan=true，用户确认页面现象成功
页面显示动作、失败反馈、计数和停止原因	已通过	机器摘要中的计数和停止原因，及用户页面操作确认
只改A，其他字段保持，旧结果清除并重规划	已通过	用户本轮手工确认
含糊要求追问，明确回答后继续	已通过	用户本轮手工确认
长期偏好显式保存、新会话读取、本次覆盖、修改删除	已通过	用户本轮手工确认
刷新和重启后恢复条件并重新查询	已通过	用户本轮手工确认
运行中重启后结束中断运行并重开	已通过	用户本轮手工确认
人工停止	已通过	用户本轮手工确认；未新增虚构run_id
搜索轮数达到上限后正确结束	已通过	L01、L05为3轮预算停止且未超限
地图HTTP上限1的专门补测	已通过	用户本轮手工确认；与五次常规在线记录分开
真实运行检查点字段边界	已通过	五份true，CLI audit再次确认L05
保留未找到可行方案的记录	已完成	L05推荐数0、error_code=null，保留为未完成方案交付


“用户手工确认”指本轮回复“你说的现象都是成功”，对应之前明确列出的修改、澄清、偏好、恢复、停止和预算补测项目。这些项目按用户观察验收，不添加未提供的自动日志、截图或运行编号。
7. 补充确认与输入记录
7.1 五组实际起点
来源：用户本轮提供的起点表截图，已核对图片中的文字。
编号	A起点	B起点	限制/排序的选择方式
L01	五道口地铁站	国贸地铁站	沿用长期偏好或默认
L02	西直门地铁站	望京西地铁站	沿用长期偏好或默认
L03	北京南站	东直门地铁站	沿用长期偏好或默认
L04	中关村地铁站	双井地铁站	沿用长期偏好或默认
L05	清华大学	北京站	沿用长期偏好或默认


记录的是用户实际选择的条件来源。摘要没有完整生效约束，因此不把每组一律写成45分钟、1次换乘、1500米。既定Memory规则仍为：长期个人限制只作用于A，排序可以继承长期偏好，本轮明确要求优先。M7固定对照条件时再导出并冻结完整生效值。
7.2 页面操作确认
以下由用户确认全部成功：
- [x] 网页创建条件，分别点选A和B，看到候选卡片。
- [x] 候选卡片的双方耗时、换乘、步行符合当前限制。
- [x] 页面可查看搜索动作、双方失败反馈和调用计数。
- [x] 只改A的时间，其他条件保持，旧卡片清除并重新规划。
- [x] 含糊表达触发追问，明确回答后可以继续。
- [x] 长期偏好保存、新会话读取、本次覆盖、修改和删除正确。
- [x] 刷新页面后恢复条件。
- [x] 重启容器后恢复条件。
- [x] 运行中重启后结束中断运行并重新规划。
- [x] 人工停止成功。
- [x] 地图HTTP上限1时正确停止，未显示不完整方案。
7.3 本轮回归范围
M2～M6的159项已经通过，真实在线五组结果已经保存。本阶段无需重复这些测试和在线运行。
8. 阶段结论
M6已完成，第十六步验收通过，可以进入M7。

依据包括：本机22项M6检查及159项M2～M6回归通过；五组不同起点的真实地图和模型调用；4次可行方案交付；L05在预算内正确结束并保留未完成记录；五次Checkpointer字段边界通过；用户明确确认全部列出的页面与Memory操作成功。

本轮回归如实记录为159项，不写成188项。L05继续作为“已查候选不满足当前约束”的有效未完成记录保留，不改成成功交付，也不据此声称全城无解。

当前实测汇总：5次运行，4次完成方案交付，地图HTTP合计62次，模型调用7次，平均总耗时23.651秒（包含人工点选等待）。这些是本次接入观察值。

下一阶段M7再进行冻结任务、B0/B1独立对照、Memory对照、质量和成本比较。M6接入数据与M7策略效果数据分别报告。
附录：五份在线摘要
以下按用户粘贴内容整理，保留数值与标签；不包含地图候选、坐标或路线。
L01
{
  "record_type": "m6_online_integration",
  "data_source": "amap_live",
  "decision_source": "real_llm_when_needed",
  "tested_at": "2026-10-26T13:38:38+08:00",
  "code_version": "262d184907ee5e36",
  "api_version": "v5",
  "run_id": "202fe6bb13364103ba842ade4bc23b77",
  "constraints_version": 1,
  "departure_date": "2026-10-26",
  "departure_time": "10:00",
  "status": "completed",
  "stop_reason": "budget_exhausted:search_rounds",
  "outcome": "feasible_found",
  "completed_plan": true,
  "recommendations_count": 1,
  "feasible_evaluated_count": 2,
  "evaluated_count": 7,
  "limits": {
    "max_map_http": 40,
    "max_llm_calls": 12,
    "max_search_rounds": 3,
    "max_candidates_per_round": 5
  },
  "counters": {
    "map_http": 19,
    "llm_calls": 2,
    "search_rounds": 3,
    "candidates_in_round": 2,
    "candidates_total": 7
  },
  "real_map_http_attempts": 19,
  "map_retries": 0,
  "model": {
    "configured_model": "qwen3.8-27b",
    "returned_models": [
      "qwen3.8-27b"
    ],
    "prompt_sha256": "bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464",
    "parameters": {
      "temperature": 0,
      "max_tokens": 512,
      "enable_thinking": false,
      "sdk_max_retries": 0,
      "extra_retries_per_call": 1
    },
    "real_llm_attempts": 2,
    "llm_retries": 0,
    "prompt_tokens_reported": 1882,
    "completion_tokens_reported": 98,
    "usage_coverage": {
      "attempts": 2,
      "responses_with_usage": 2
    }
  },
  "elapsed_seconds_including_confirmation": 27.726,
  "checkpoint_audit": {
    "checkpoint_count": 11,
    "persistent_field_boundary_passed": true,
    "latest_control_state": {
      "stop_reason": "budget_exhausted:search_rounds",
      "run_id": "202fe6bb13364103ba842ade4bc23b77",
      "action": "toward_a",
      "completed_rounds": 3
    },
    "resume_policy": "read_confirmed_conditions_then_requery"
  },
  "error_code": null,
  "note": "本次在线接入记录；不是策略评测，不包含地图候选、坐标或路线。"
}
L02
{
  "record_type": "m6_online_integration",
  "data_source": "amap_live",
  "decision_source": "real_llm_when_needed",
  "tested_at": "2026-10-26T13:43:18+08:00",
  "code_version": "262d184907ee5e36",
  "api_version": "v5",
  "run_id": "3e810f08331b40d49408820c102e8cc1",
  "constraints_version": 4,
  "departure_date": "2026-10-26",
  "departure_time": "10:00",
  "status": "completed",
  "stop_reason": "feasible_target_reached",
  "outcome": "feasible_found",
  "completed_plan": true,
  "recommendations_count": 3,
  "feasible_evaluated_count": 3,
  "evaluated_count": 6,
  "limits": {
    "max_map_http": 40,
    "max_llm_calls": 12,
    "max_search_rounds": 3,
    "max_candidates_per_round": 5
  },
  "counters": {
    "map_http": 16,
    "llm_calls": 1,
    "search_rounds": 2,
    "candidates_in_round": 1,
    "candidates_total": 6
  },
  "real_map_http_attempts": 16,
  "map_retries": 0,
  "model": {
    "configured_model": "qwen3.8-27b",
    "returned_models": [
      "qwen3.8-27b"
    ],
    "prompt_sha256": "bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464",
    "parameters": {
      "temperature": 0,
      "max_tokens": 512,
      "enable_thinking": false,
      "sdk_max_retries": 0,
      "extra_retries_per_call": 1
    },
    "real_llm_attempts": 1,
    "llm_retries": 0,
    "prompt_tokens_reported": 960,
    "completion_tokens_reported": 51,
    "usage_coverage": {
      "attempts": 1,
      "responses_with_usage": 1
    }
  },
  "elapsed_seconds_including_confirmation": 22.753,
  "checkpoint_audit": {
    "checkpoint_count": 8,
    "persistent_field_boundary_passed": true,
    "latest_control_state": {
      "stop_reason": "feasible_target_reached",
      "run_id": "3e810f08331b40d49408820c102e8cc1",
      "action": "toward_a",
      "completed_rounds": 2
    },
    "resume_policy": "read_confirmed_conditions_then_requery"
  },
  "error_code": null,
  "note": "本次在线接入记录；不是策略评测，不包含地图候选、坐标或路线。"
}
L03
{
  "record_type": "m6_online_integration",
  "data_source": "amap_live",
  "decision_source": "real_llm_when_needed",
  "tested_at": "2026-10-26T13:42:06+08:00",
  "code_version": "262d184907ee5e36",
  "api_version": "v5",
  "run_id": "4285b9a11ca445729cb0066398f1d320",
  "constraints_version": 3,
  "departure_date": "2026-10-26",
  "departure_time": "10:00",
  "status": "completed",
  "stop_reason": "agent_finished",
  "outcome": "feasible_found",
  "completed_plan": true,
  "recommendations_count": 1,
  "feasible_evaluated_count": 2,
  "evaluated_count": 2,
  "limits": {
    "max_map_http": 40,
    "max_llm_calls": 12,
    "max_search_rounds": 3,
    "max_candidates_per_round": 5
  },
  "counters": {
    "map_http": 7,
    "llm_calls": 1,
    "search_rounds": 1,
    "candidates_in_round": 2,
    "candidates_total": 2
  },
  "real_map_http_attempts": 7,
  "map_retries": 0,
  "model": {
    "configured_model": "qwen3.8-27b",
    "returned_models": [
      "qwen3.8-27b"
    ],
    "prompt_sha256": "bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464",
    "parameters": {
      "temperature": 0,
      "max_tokens": 512,
      "enable_thinking": false,
      "sdk_max_retries": 0,
      "extra_retries_per_call": 1
    },
    "real_llm_attempts": 1,
    "llm_retries": 0,
    "prompt_tokens_reported": 902,
    "completion_tokens_reported": 49,
    "usage_coverage": {
      "attempts": 1,
      "responses_with_usage": 1
    }
  },
  "elapsed_seconds_including_confirmation": 26.542,
  "checkpoint_audit": {
    "checkpoint_count": 6,
    "persistent_field_boundary_passed": true,
    "latest_control_state": {
      "stop_reason": "agent_finished",
      "run_id": "4285b9a11ca445729cb0066398f1d320",
      "action": "middle",
      "completed_rounds": 1
    },
    "resume_policy": "read_confirmed_conditions_then_requery"
  },
  "error_code": null,
  "note": "本次在线接入记录；不是策略评测，不包含地图候选、坐标或路线。"
}
L04
{
  "record_type": "m6_online_integration",
  "data_source": "amap_live",
  "decision_source": "real_llm_when_needed",
  "tested_at": "2026-10-26T13:44:09+08:00",
  "code_version": "262d184907ee5e36",
  "api_version": "v5",
  "run_id": "1f7e41fc484b46f19ed3a04f7961f3be",
  "constraints_version": 5,
  "departure_date": "2026-10-26",
  "departure_time": "10:00",
  "status": "completed",
  "stop_reason": "agent_finished",
  "outcome": "feasible_found",
  "completed_plan": true,
  "recommendations_count": 1,
  "feasible_evaluated_count": 1,
  "evaluated_count": 1,
  "limits": {
    "max_map_http": 40,
    "max_llm_calls": 12,
    "max_search_rounds": 3,
    "max_candidates_per_round": 5
  },
  "counters": {
    "map_http": 5,
    "llm_calls": 1,
    "search_rounds": 1,
    "candidates_in_round": 1,
    "candidates_total": 1
  },
  "real_map_http_attempts": 5,
  "map_retries": 0,
  "model": {
    "configured_model": "qwen3.8-27b",
    "returned_models": [
      "qwen3.8-27b"
    ],
    "prompt_sha256": "bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464",
    "parameters": {
      "temperature": 0,
      "max_tokens": 512,
      "enable_thinking": false,
      "sdk_max_retries": 0,
      "extra_retries_per_call": 1
    },
    "real_llm_attempts": 1,
    "llm_retries": 0,
    "prompt_tokens_reported": 922,
    "completion_tokens_reported": 49,
    "usage_coverage": {
      "attempts": 1,
      "responses_with_usage": 1
    }
  },
  "elapsed_seconds_including_confirmation": 20.489,
  "checkpoint_audit": {
    "checkpoint_count": 6,
    "persistent_field_boundary_passed": true,
    "latest_control_state": {
      "stop_reason": "agent_finished",
      "run_id": "1f7e41fc484b46f19ed3a04f7961f3be",
      "action": "middle",
      "completed_rounds": 1
    },
    "resume_policy": "read_confirmed_conditions_then_requery"
  },
  "error_code": null,
  "note": "本次在线接入记录；不是策略评测，不包含地图候选、坐标或路线。"
}
L05
{
  "record_type": "m6_online_integration",
  "data_source": "amap_live",
  "decision_source": "real_llm_when_needed",
  "tested_at": "2026-10-26T13:44:50+08:00",
  "code_version": "262d184907ee5e36",
  "api_version": "v5",
  "run_id": "dcb0797600934cd591aeaadfcf9e1da6",
  "constraints_version": 6,
  "departure_date": "2026-10-26",
  "departure_time": "10:00",
  "status": "completed",
  "stop_reason": "budget_exhausted:search_rounds",
  "outcome": "constraints_unsatisfied",
  "completed_plan": false,
  "recommendations_count": 0,
  "feasible_evaluated_count": 0,
  "evaluated_count": 5,
  "limits": {
    "max_map_http": 40,
    "max_llm_calls": 12,
    "max_search_rounds": 3,
    "max_candidates_per_round": 5
  },
  "counters": {
    "map_http": 15,
    "llm_calls": 2,
    "search_rounds": 3,
    "candidates_in_round": 1,
    "candidates_total": 5
  },
  "real_map_http_attempts": 15,
  "map_retries": 0,
  "model": {
    "configured_model": "qwen3.8-27b",
    "returned_models": [
      "qwen3.8-27b"
    ],
    "prompt_sha256": "bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464",
    "parameters": {
      "temperature": 0,
      "max_tokens": 512,
      "enable_thinking": false,
      "sdk_max_retries": 0,
      "extra_retries_per_call": 1
    },
    "real_llm_attempts": 2,
    "llm_retries": 0,
    "prompt_tokens_reported": 1915,
    "completion_tokens_reported": 102,
    "usage_coverage": {
      "attempts": 2,
      "responses_with_usage": 2
    }
  },
  "elapsed_seconds_including_confirmation": 20.744,
  "checkpoint_audit": {
    "checkpoint_count": 11,
    "persistent_field_boundary_passed": true,
    "latest_control_state": {
      "stop_reason": "budget_exhausted:search_rounds",
      "run_id": "dcb0797600934cd591aeaadfcf9e1da6",
      "action": "toward_b",
      "completed_rounds": 3
    },
    "resume_policy": "read_confirmed_conditions_then_requery"
  },
  "error_code": null,
  "note": "本次在线接入记录；不是策略评测，不包含地图候选、坐标或路线。"
}