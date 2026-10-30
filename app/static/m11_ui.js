// M11_INTEGRATION_V1: existing demo state remains the single source of identity.
(() => {
  const panel = node('section', undefined, 'panel');
  panel.id = 'm11Panel';
  panel.append(node('h2', '自然语言修改预览'));
  panel.append(node('p', '我 = A，朋友 / 他 / 她 = B。先预览，再确认；默认偏好只保存 A 本人。', 'muted'));
  const input = node('textarea'); input.maxLength = 400;
  input.placeholder = '他那边放宽到一小时，我的限制不变，这次别记成默认。';
  input.setAttribute('aria-label', '自然语言条件修改');
  const generate = node('button', '生成预览（调用模型）', 'primary');
  const rule = node('button', '受控句式预览');
  const confirm = node('button', '确认这些修改', 'primary');
  const cancel = node('button', '取消预览');
  const history = node('button', '查看修改记录');
  const output = node('div'); output.setAttribute('aria-live', 'polite');
  const rows = node('div', undefined, 'row'); rows.append(generate, rule, confirm, cancel, history);
  panel.append(input, rows, output);
  confirm.disabled = cancel.disabled = true;
  const anchor = $('messageForm')?.closest('section');
  if (!anchor) return;
  anchor.after(panel);
  let pending = null;
  const labels = {max_duration_seconds:'通勤上限',max_transfers:'换乘上限',max_walk_meters:'步行上限',origin_text:'出发点',preference:'排序'};
  function value(path, v) {
    if (v === null || v === undefined) return '未设置';
    if (path.endsWith('max_duration_seconds')) return (v / 60) + ' 分钟';
    if (path.endsWith('max_walk_meters')) return v + ' 米';
    if (path.endsWith('max_transfers')) return v + ' 次';
    return v === 'balanced' ? '均衡优先' : v === 'total_time' ? '总耗时优先' : String(v);
  }
  function base() {
    if (!S.memory) throw Error('请先创建或恢复会话。');
    if (S.job?.working) throw Error('请先结束当前规划。');
    return {user_id:user(),expected_version:S.memory.task.constraints_version,expected_preferences_revision:S.prefs.revision};
  }
  async function preview(parser) {
    const request = {...base(),text:input.value.trim(),parser};
    if (!request.text) throw Error('请输入要修改的条件。');
    pending = null; confirm.disabled = cancel.disabled = true;
    output.replaceChildren(node('p', '正在解析，请稍候。'));
    generate.disabled = rule.disabled = true;
    try {
      const sid = S.memory.task.session_id;
      const data = await api('/api/m11/tasks/' + sid + '/preview', 'POST', request);
      output.replaceChildren();
      if (data.status !== 'awaiting_confirmation') {
        output.append(node('p', data.message || data.question)); return;
      }
      pending = {data,request,sid,owner:S.owner};
      for (const item of data.diff) {
        const [role, field] = item.path.includes('.') ? item.path.split('.') : ['',item.path];
        const who = role === 'person_a' ? 'A · 我' : role === 'person_b' ? 'B · 朋友' : '';
        output.append(node('p', (item.scope === 'defaults' ? '以后默认 A' : '本次 ' + who) + ' · ' +
          labels[field] + '：' + value(item.path,item.before) + ' → ' + value(item.path,item.after)));
      }
      output.append(node('p', data.notice, 'muted'));
      if (data.model_usage) output.append(node('small', '模型尝试 ' + data.model_usage.attempts +
        ' 次 · Token ' + (data.model_usage.tokens?.total_tokens ?? '未知')));
      confirm.disabled = cancel.disabled = false;
    } finally { generate.disabled = rule.disabled = false; }
  }
  generate.onclick = () => act(() => preview('llm'));
  rule.onclick = () => act(() => preview('rule'));
  confirm.onclick = () => act(async () => {
    const p = pending; if (!p) return;
    const current = base();
    if (S.memory.task.session_id !== p.sid || S.owner !== p.owner ||
        current.expected_version !== p.request.expected_version ||
        current.expected_preferences_revision !== p.request.expected_preferences_revision) {
      pending = null; confirm.disabled = cancel.disabled = true;
      throw Error('人物、会话或版本已变化，请重新生成预览。');
    }
    confirm.disabled = true;
    const data = await api('/api/m11/tasks/' + p.sid + '/proposals/' + p.data.proposal_id + '/confirm',
      'POST', {user_id:current.user_id,expected_version:p.data.base_version,expected_preferences_revision:p.data.base_revision});
    pending = null; cancel.disabled = true;
    if (data.replan_required) clearResults();
    paintMemory(data);
    output.append(node('p', '修改已确认。' + (data.replan_required ? '请重新规划。' : '默认值用于之后新建的会话。')));
  });
  cancel.onclick = () => act(async () => {
    const p = pending; if (!p) return;
    await api('/api/m11/tasks/' + p.sid + '/proposals/' + p.data.proposal_id + '/cancel','POST',{user_id:user()});
    pending = null; confirm.disabled = cancel.disabled = true;
    output.replaceChildren(node('p', '已取消，条件没有修改。'));
  });
  history.onclick = () => act(async () => {
    base();
    const data = await api('/api/m11/tasks/' + S.memory.task.session_id + '/proposals?user_id=' + encodeURIComponent(user()));
    output.replaceChildren(node('pre', JSON.stringify(data,null,2)));
  });
  // Availability is not inferred from valid transit routes. Slack uses only
  // already-validated route values and the matching current constraint version.
  const originalPaint = paintM9Cards;
  paintM9Cards = function(data) {
    originalPaint(data);
    const view = data.result?.m9_presentation;
    if (!view?.evidence_valid) return;
    for (const [index, card] of [...$('cards').children].entries()) {
      const item = view.cards[index];
      if (!item) continue;
      const suspect = /装修|停业|暂停营业/.test(item.name || '');
      card.append(node('p', suspect ? '名称含可用性提示，营业状态尚未核验，请确认后再前往。' :
        '营业时间与是否正常营业：未知，当前未查询营业信息。', suspect ? 'notice warn' : 'muted'));
      if (S.memory?.task.constraints_version !== data.constraints_version) continue;
      for (const role of ['a','b']) {
        const limits = S.memory.task.requirements['person_' + role], route = item[role];
        const margin = limits.max_duration_seconds - route.duration_seconds;
        if (Number.isFinite(margin)) card.append(node('small', role.toUpperCase() +
          ' 距通勤上限还剩 ' + (margin / 60).toFixed(1) + ' 分钟。'));
      }
    }
  };
})();
