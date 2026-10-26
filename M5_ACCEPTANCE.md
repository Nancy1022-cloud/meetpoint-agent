MeetPoint M5 本机验收记录

状态：M5阶段验收通过，第16步完成。Memory主要功能、M2–M5回归137项、offline演示、版本3条件的真实规划、404隔离、检查点与所示日志检查均已取得对应证据。

本记录依据用户提供的《粘贴的文本 (1)(3).txt》和本轮聊天中的测试、错误拦截、m5_live、独立audit、应用日志及m5 offline输出填写；未用提供方受控测试替代用户本机结果。仅保存用户条件、控制状态、汇总计数与必要运行元信息，不收录地图地点、坐标、候选标识或路线明细。

验收日期：2026-10-26（北京时间）。

Memory证据采集时间：2026-10-26T11:50:00.8014024+08:00。

实际模型：qwen3.8-27b；configured_model与returned_models一致。

user_id：m5_demo_20261026_113854973。

session_id：02dc613e714f4c01b4bdf016f1d534e3。

本次m5_live run_id：a158a259355843f6a7ce3c9ef0a59b94。

当前constraints_version：3。

实际出发日期与时间：2026-10-26 10:00，按本次输入参数记录。

真实运行tested_at：2026-10-26T11:43:34+08:00。

代码版本：0470d119fe5228b6；地图API版本：v5。

本机验收表

项目

预期

本机结果

/api/m5/health

ok，M5，新增Memory业务表

通过。status=ok，milestone=M5，memory_schema_version=1；四张业务表均存在。

M5检查

M5用例通过

已包含在本轮M2–M5的137项回归中通过；未另行提供单独43项执行记录，不重复计数。

原模块回归

当前实际测试模块全部通过

通过。M2–M5合计137项，12.222秒，OK。M1测试模块缺失，不将本机结果写为166项。

m5 offline

memory_checks_passed=true；真实请求均0

通过。用户实际运行m5 offline，memory_checks_passed=true；只改变person_a.max_duration_seconds，同一会话A3600秒、新会话A换乘0次、原会话仍1次；8个检查点，字段边界true；真实地图/模型请求均0。

只改A时间

A3600秒，B2700秒；其他保留

通过。changed_fields仅person_a.max_duration_seconds；版本1→2；双方换乘1次、步行1500米、排序balanced均保留。

本次条件不写长期

第7步偏好仍为空

通过。changed.preferences.revision=0、values={}。

明确保存长期

新任务A继承，旧任务保留

通过。remembered保存max_transfers=0、revision=1；原任务A仍为1，新会话A为0且来源long_term_explicit；B仍为1。

本轮覆盖长期

显式换乘1次覆盖长期0次

通过。session3中A换乘1次、来源current_explicit；偏好快照仍为0。A显式时间1800秒，排序total_time。

含糊表达

needs_clarification，条件不变

通过。ambiguous为needs_clarification，问题码explicit_change，版本仍2且条件未变；随后明确修改排序，版本升至3，问题码清空。

旧条件版本

409

通过。用户try/catch在409分支返回version_conflict，expected_version=1、actual_version=3。

非法结构化限制

422

通过。max_transfers=-1被拦截；greater_than_equal，ge=0，字段路径正确。

跨用户读取

404

通过。m5_other_user读取该session返回task_not_found；应用访问日志同时确认HTTP 404 Not Found。

同用户不同会话

各自条件不串

通过。session2与other_session的任务数据一致，仍为版本1、A2700秒和换乘0次；原任务已为版本3、A3600秒、排序total_time。

查看/修改/删除偏好

值正确，revision递增

通过已提供的接口与状态验收。日志中GET、PUT、DELETE均为200；偏好由revision=1、max_transfers=0变为revision=3、values={}。137项回归已通过。未单独提供revision=2的返回体，不额外推定该中间值。

重启后读取

原ID、版本、条件保留

通过。日志显示应用完成关闭、重新启动，之后同一任务GET为200；recovered_after_restart快照保留原session_id、版本3、A3600秒、B2700秒、双方换乘1次、步行1500米、排序total_time，偏好revision=3且为空。

新条件真实规划

m5_live，正确版本，新查询

通过。运行模式m5_live，任务与运行同为版本3，A3600秒/B2700秒、total_time；真实地图请求12次、真实模型1次。评估4个候选，3个可行，Pareto筛选后展示2个；error=null，outcome=feasible_found，停止原因为feasible_target_reached。

检查点

count>0，字段边界true

通过。live末尾与独立audit均为checkpoint_count=8、persistent_field_boundary_passed=true；最新控制状态关联本次run_id，action=expand、completed_rounds=2、stop_reason=feasible_target_reached。

业务事件及日志

无地图/路线/完整对话内容

在已提供证据范围内通过。6条本会话事件只含来源、变更、版本和问题码；检查点audit通过；所示应用日志为启动、关闭与HTTP访问记录，未见地图原始响应、候选列表、路线明细、完整用户消息或模型消息。此结论不等同于对全部历史数据库与日志的普遍证明。

已确认的最终任务条件

字段

A：用户本人

B：朋友

用户输入的起点

五道口地铁站

国贸地铁站

最长通勤时间

3600秒（60分钟）

2700秒（45分钟）

最大换乘次数

1

1

最大步行距离

1500米

1500米

城市：北京市。

场所类型：mall。

排序：total_time。

条件版本：3。

当前任务阶段：stopped；active_run_id=null。本次关联运行另有status=finished及成功结果，二者分别表示任务阶段和运行状态。

长期偏好：values={}，revision=3。

当前待确认问题：无。

会话隔离证据：

session2：052915c48ddb4f1fbe228a73b0153c48，版本1、A2700秒、A换乘0次、排序balanced。

session3：1b31d86de1514e2fa6aad13aa159a983，版本1、A1800秒、A换乘1次、排序total_time。

真实运行摘要

execution_mode：m5_live；搜索策略B1。

run_id：a158a259355843f6a7ce3c9ef0a59b94。

session_id：02dc613e714f4c01b4bdf016f1d534e3。

constraints_version：任务、运行、最终结果均为3。

departure_date / departure_time：2026-10-26 / 10:00。

status：finished。

stop_reason / termination_reason：feasible_target_reached。

outcome：feasible_found。

是否取得至少一个完整且满足当前约束的方案：是。运行报告3个可行候选；用户展示的2个推荐均满足本次双方的时间、换乘和步行限制。

候选汇总：本轮搜索接受5个，实际评估4个，其中1个不满足约束、3个可行；达到可行目标后停止。Pareto筛选后展示2个，数量与可行候选总数含义不同。

地图调用：12/40；real_map_http_attempts=12，与预算计数一致。

模型调用：1/12；real_llm_attempts=1，与预算计数一致；模型重试0次。

搜索轮数：2/3；每轮最多5个新候选；未越界。

模型：qwen3.8-27b，配置值与服务返回值相同。

模型usage：prompt_tokens=906，completion_tokens=47，合计953；1次尝试对应1份usage，覆盖完整。

模型参数：temperature=0，max_tokens=512，enable_thinking=false，SDK自动重试0次，单调用最多额外重试1次。

Prompt SHA-256：bdc8eb3d0e5385049e30d903d52bfab25151f6d53d21941b4267e02b014a2464。

总耗时：elapsed_seconds=16.689。此为单次完整CLI规划耗时，包含起点人工确认等待，不代表纯接口延迟或多次平均性能。12.222秒另属于回归检查耗时。

错误字段：error=null，未报告本次运行的接口错误。

checkpoint_count：8。

persistent_field_boundary_passed：true，live与独立audit一致。

resume_policy：read_confirmed_conditions_then_requery。

运行控制摘要：首轮middle，半径1500米，无候选；真实模型选择合法动作expand；第二轮半径3000米，重新查询并评估后达到可行目标。模型没有修改已确认硬约束；最终预算均在上限内。

已观察到的非阻断问题：首轮反馈是no_candidates，模型却给出reason_code=no_route并使用“无路线”理由文案。二者含义不同，应记录为解释准确性问题。本次expand动作合法，第二轮经过真实工具重新验证并取得可行结果；该理由误标不作为本次Memory、约束或预算验收失败，也不能据此把首轮统计为无路线。后续可完善理由码与观察状态的一致性校验。

日期格式说明

Get-Date -Format "yyyy-MM-dd"会把当天日期格式化为带连字符的日期；Get-Date -Format "yyyyMMdd_HHmmssfff"用于生成带时间的标识。它们是格式字符串，无需手动替换为年月日。

本次m5_live已直接给出departure_date=2026-10-26，出发时间为10:00，日期与本次验收日一致；tested_at同为2026-10-26。即使用户当时把格式字符串替换成了该日的固定文字，也没有因此使本次运行的日期错误，无需仅为这一原因重跑。以后应恢复格式字符串，避免跨日期使用时仍查询固定旧日期。

如果实际传给--date的是字面量YYYYMMDD，则与程序要求的%Y-%m-%d不符；应先核对原始命令或运行开头参数，而不是直接重新运行所有验收。

离线演示记录

以下来自用户实际执行docker compose exec -T app python -m app.m5 offline的输出，与上述真实规划分开记录：

data_source：self_authored_synthetic。

decision_source：scripted_test_double。

memory_checks_passed：true。

changed_fields：仅person_a.max_duration_seconds。

same_session_A_seconds：3600。

new_session_A_transfers：0。

current_session_A_transfers：1。

checkpoint_count：8。

persistent_field_boundary_passed：true。

completed_rounds：2；action=expand；stop_reason=feasible_target_reached。

resume_policy：read_confirmed_conditions_then_requery。

simulated_counters：map_http=10、llm_calls=1、search_rounds=2、candidates_in_round=3、candidates_total=3。

real_map_http_attempts：0。

real_llm_attempts：0。

模拟计数表示自建工具与脚本决策器的执行次数，不是实际API调用量。该演示使用临时数据并在退出后清理，不将其临时run_id当作真实规划ID，也不把该演示统计作为M7独立效果评测。

最终结论

已确认：M2–M5回归137项通过；offline演示通过且真实外部请求均0；Memory字段更新、长期默认、显式覆盖、澄清、会话独立、来源与版本、重启后条件恢复、409/422/404拦截、偏好管理接口与最终状态、新条件真实规划、预算以及所提供范围内的检查点/日志边界符合本次要求。

最终结论：M5阶段已完成，可进入M6最小页面与联调。无需为完成本阶段重复执行已通过的检查或重新调用真实地图/模型。

保留的证据范围说明：本机回归为137项，未包含缺失的M1单独测试模块；偏好revision=2的具体中间值未单独截取，但管理接口、最终状态与相关回归均已通过；日志与持久状态结论限于已提供证据。首轮理由将“无候选”误写为“无路线”的非阻断问题已在真实运行摘要中如实保留。这些说明不改变本次M5的阶段通过结论，也不将其夸大为生产级可靠性或M7效果结论。