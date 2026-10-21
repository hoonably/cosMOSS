/* Voice matches remain proposals until selected, approved, and explicitly saved. */
const voiceReview = (() => {
  const dialog = $('#voice-review-dialog');
  const player = $('#voice-review-player');
  let review = null;
  let playback = null;
  let playbackRequest = 0;

  const label = (draft, speaker) => {
    const name = draft.names[speaker]?.trim();
    return name && name !== speaker ? `${speaker} · ${name}` : speaker;
  };
  const score = value => `${(Math.max(0, Math.min(1, Number(value) || 0)) * 100).toFixed(1)}점`;

  function proposals(draft, result) {
    const names = (result.matches || []).filter(item => draft.speakers.includes(item.speaker)
      && item.name && item.name !== (draft.names[item.speaker]?.trim() || item.speaker))
      .map(item => ({...item, kind: 'name'}));
    const merges = (result.merge_candidates || []).filter(item => item.source !== item.target
      && draft.speakers.includes(item.source) && draft.speakers.includes(item.target))
      .map(item => ({...item, kind: 'merge'}));
    return [...names, ...merges];
  }

  function plan() {
    const draft = review.draft;
    const next = structuredClone({names: draft.names, merges: draft.merges,
      segments: draft.segments, speakers: draft.speakers});
    const resolve = speaker => {
      while (next.merges[speaker]) speaker = next.merges[speaker];
      return speaker;
    };
    for (const index of review.selected) {
      const row = review.rows[index];
      if (row.kind === 'name') next.names[row.speaker] = row.name;
    }
    for (const index of review.selected) {
      const row = review.rows[index];
      if (row.kind !== 'merge') continue;
      const source = resolve(row.source), target = resolve(row.target);
      if (source === target) continue;
      if ((!next.names[target] || next.names[target] === target) && next.names[source] && next.names[source] !== source) next.names[target] = next.names[source];
      next.merges[source] = target;
      next.names[source] = next.names[target] || target;
      next.speakers = next.speakers.filter(speaker => speaker !== source);
    }
    for (const source of Object.keys(next.merges)) next.merges[source] = resolve(source);
    next.segments = next.segments.map(segment => ({...segment, speaker: resolve(segment.speaker)}));
    return next;
  }

  function renderPreview() {
    const next = plan();
    $('#voice-review-before-count').textContent = `${review.draft.speakers.length}명`;
    $('#voice-review-before').textContent = review.draft.speakers.map(speaker => label(review.draft, speaker)).join(' / ');
    $('#voice-review-after-count').textContent = `${next.speakers.length}명`;
    $('#voice-review-after').textContent = next.speakers.map(speaker => label(next, speaker)).join(' / ');
    const button = $('#approve-voice-review');
    button.disabled = !review.selected.size;
    button.textContent = review.selected.size ? `선택한 ${review.selected.size}건 승인` : '선택한 추천 승인';
  }

  function sampleButtons(ranges, speaker) {
    return (ranges || []).filter(range => Number.isFinite(range.start) && Number.isFinite(range.end)
      && range.start >= 0 && range.end > range.start).map((range, index) => {
      const id = review.samples.push({url: `/api/notes/${encodeURIComponent(review.draft.id)}/media`,
        start: range.start, end: range.end, label: `${speaker} 샘플 ${index + 1} · ${clock(range.start)}–${clock(range.end)}`}) - 1;
      return `<button class="ghost-button" type="button" data-voice-sample="${id}">${escapeHtml(speaker)} 샘플 ${index + 1} 듣기</button>`;
    }).join('');
  }

  function open(result) {
    const draft = state.speakerDraft;
    if (!draft || state.speakerSaving || state.voiceIdentifying) return;
    review = {draft, rows: proposals(draft, result), selected: new Set(), samples: []};
    $('#voice-review-results').innerHTML = review.rows.map((row, index) => {
      const isName = row.kind === 'name';
      const before = isName ? label(draft, row.speaker) : `${label(draft, row.target)} / ${label(draft, row.source)}`;
      const after = isName ? `${row.speaker} · ${row.name}` : `${label(draft, row.target)} · 한 화자로 병합`;
      let samples = isName ? sampleButtons(row.samples, row.speaker)
        : sampleButtons(row.target_samples, row.target) + sampleButtons(row.source_samples, row.source);
      if (isName && row.profile_id) {
        const id = review.samples.push({url: `/api/voice-profiles/${encodeURIComponent(row.profile_id)}/sample`,
          start: 0, label: `${row.name} · 등록한 목소리`}) - 1;
        samples += `<button class="ghost-button" type="button" data-voice-sample="${id}">등록한 목소리 듣기</button>`;
      }
      const recommended = row.recommended ?? row.accepted ?? false;
      return `<article class="voice-review-card" data-voice-card="${index}" data-voice-kind="${row.kind}">
        <div class="voice-review-card-head"><label><input type="checkbox" data-voice-proposal="${index}" aria-label="${escapeHtml(before)} ${isName ? '이름' : '병합'} 추천 선택">${isName ? '이름 추천' : '같은 화자 병합 추천'}</label><span class="voice-review-score">일치도 ${score(row.similarity)}</span><span class="voice-review-confidence ${recommended ? '' : 'needs-review'}">${recommended ? '추천 기준 충족' : '추가 확인 필요'}</span></div>
        <div class="voice-review-change"><span><small>현재</small><b>${escapeHtml(before)}</b></span><span aria-hidden="true">→</span><span><small>추천</small><b>${escapeHtml(after)}</b></span></div>
        <div class="voice-review-samples">${samples}</div></article>`;
    }).join('') || '<p class="voice-review-empty">추천할 변경이 없습니다. 등록한 목소리와 비교 가능한 발언 구간을 확인해 주세요.</p>';
    renderPreview();
    $('#media-player').pause();
    if (!dialog.open) dialog.showModal();
    $('#voice-review-title').focus({preventScroll: true});
  }

  function close() {
    if (dialog.open) dialog.close();
    ++playbackRequest;
    player.pause();
    player.removeAttribute('src');
    player.onloadedmetadata = null;
    player.onerror = null;
    player.load();
    playback = null;
    $('#voice-review-audio').classList.add('hidden');
    review = null;
  }

  $('#voice-review-results').addEventListener('change', event => {
    const input = event.target.closest('[data-voice-proposal]');
    if (!input || !review) return;
    const index = Number(input.dataset.voiceProposal);
    if (input.checked) review.selected.add(index); else review.selected.delete(index);
    input.closest('.voice-review-card').classList.toggle('is-selected', input.checked);
    renderPreview();
  });
  $('#voice-review-results').addEventListener('click', event => {
    const button = event.target.closest('[data-voice-sample]');
    if (!button || !review) return;
    playback = review.samples[Number(button.dataset.voiceSample)];
    if (!playback) return;
    const request = ++playbackRequest;
    player.pause();
    $('#voice-review-audio').classList.remove('hidden');
    $('#voice-review-audio-label').textContent = playback.label;
    player.onloadedmetadata = () => {
      if (request !== playbackRequest || !review) return;
      player.currentTime = playback.start;
      player.play().catch(() => { if (request === playbackRequest && review) toast('샘플을 재생하지 못했습니다.', true); });
    };
    player.onerror = () => { if (request === playbackRequest && review) toast('샘플을 불러오지 못했습니다.', true); };
    player.src = playback.url;
    player.load();
  });
  player.addEventListener('timeupdate', () => {
    if (playback?.end != null && player.currentTime >= playback.end) player.pause();
  });
  player.addEventListener('play', () => {
    if (playback && (player.currentTime < playback.start || (playback.end != null && player.currentTime >= playback.end))) player.currentTime = playback.start;
  });
  for (const id of ['#close-voice-review', '#cancel-voice-review']) $(id).addEventListener('click', close);
  bindConfirmationDialog('#voice-review-dialog', close);
  $('#refresh-voice-review').addEventListener('click', () => { close(); compareSpeakerVoices(true); });
  $('#approve-voice-review').addEventListener('click', () => {
    if (!review || !review.selected.size || state.speakerDraft !== review.draft || state.speakerSaving) return;
    const count = review.selected.size;
    const next = plan();
    const draft = review.draft;
    Object.assign(draft, next, {voiceResult: null});
    if (!draft.speakers.includes(draft.selected)) draft.selected = draft.speakers[0];
    close();
    renderSpeakerDialog();
    goToSpeakerUtterance(0, true);
    $('#identify-voice-speakers').textContent = '목소리 비교·추천';
    $('#speaker-voice-status').textContent = `${count}건을 승인했습니다. 저장하면 노트와 파일에 반영됩니다.`;
    $('#speaker-voice-status').classList.remove('hidden');
  });
  return {open, close};
})();
