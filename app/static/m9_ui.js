// M9_INTEGRATION_V1: injected into the existing page by apply_m9_changes.py.
function paintM9Cards(d) {
  const out = $('cards');
  out.replaceChildren();
  const view = d.result?.m9_presentation;
  if (!d.result) return;
  if (!view) {
    $('outcome').textContent = '证据模块尚未就绪，请重启服务后重新规划。';
    return;
  }
  $('outcome').textContent = view.headline;
  if (d.error) $('outcome').append(node('p', d.error.code + '：' + d.error.message));
  if (d.result.pending_user) $('outcome').append(node('p', d.result.pending_user));
  if (d.confirmation_timed_out) $('outcome').append(node('p', '起点确认已超时，可重新规划后确认。'));
  $('outcome').append(node('p', view.scope_note, 'muted'));
  if (!view.evidence_valid) return;
  for (const [index, item] of view.cards.entries()) {
    const card = node('article', undefined, 'candidate');
    card.append(node('span', '方案 ' + (index + 1), 'badge'), node('h3', item.name));
    card.append(node('p', '地点编号：' + item.candidate_id, 'muted'));
    const row = node('div', undefined, 'grid2');
    for (const role of ['a', 'b']) {
      const route = item[role], box = node('div', undefined, 'route');
      box.append(node('small', role === 'a' ? 'A · 我' : 'B · 朋友'),
        node('strong', seconds(route.duration_seconds)),
        node('p', '换乘 ' + route.transfers + ' 次 · 步行 ' + route.walk_meters + ' 米'));
      row.append(box);
    }
    card.append(row, node('p', '总耗时 ' + seconds(item.total_duration_seconds) +
      ' · 时间差 ' + seconds(item.gap_seconds)));
    card.append(node('small', '总耗时与时间差由双方已引用的耗时计算。', 'muted'));
    const details = node('details');
    details.append(node('summary', '查看本方案的 8 项引用依据'));
    const labels = {poi_id:'地点编号', name:'地点名称', duration_seconds:'通勤耗时',
      walk_meters:'步行距离', transfers:'换乘次数'};
    for (const claim of item.claims) {
      const proof = item.evidence.find(e => e.evidence_id === claim.evidence_ids[0]);
      const part = node('div', undefined, 'logline');
      part.append(node('strong', (claim.role ? claim.role.toUpperCase() + ' · ' : '') +
        labels[claim.field] + '：' + claim.value + ' ' + claim.unit));
      part.append(node('p', '引用编号：' + claim.evidence_ids.join('、'), 'muted'));
      if (proof) part.append(node('small', '本次运行 ' + proof.run_id + ' · 条件 v' +
        proof.constraints_version + (proof.route_id ? ' · 路线 ' + proof.route_id : '')));
      details.append(part);
    }
    card.append(details);
    out.append(card);
  }
}
