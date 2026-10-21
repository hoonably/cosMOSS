/* Dedicated diarizers propose speaker labels; only explicit selection writes. */
(() => {
  const dialog = $('#diarization-dialog');
  const busyPhases = new Set(['connecting', 'installing', 'downloading', 'preparing', 'loading', 'running']);
  let noteId, models = [], setup = {}, job = {}, gpus = [], gpu = null, timer, generation = 0, saving = false, authOpened = false;
  const host = () => $('#diarization-host').value;
  const fixedCount = () => $('#diarization-model').value === 'pyannote-community-1-fixed';
  const model = () => fixedCount() ? 'pyannote-community-1' : $('#diarization-model').value;
  const local = () => host() === '@local';
  const selected = () => [...dialog.querySelectorAll('[data-diarization-change]:checked')].map(el => Number(el.value));
  function status(message, error = false) {
    $('#diarization-status').textContent = message;
    $('#diarization-status').classList.toggle('error', error);
  }
  function controls() {
    const busy = busyPhases.has(job.phase), installing = busyPhases.has(setup.phase);
    const info = models.find(item => item.key === model());
    for (const id of ['#diarization-model', '#diarization-host']) $(id).disabled = busy || saving;
    for (const option of $('#diarization-model').options) option.disabled = local() && option.value !== 'nemotron-3';
    $('#diarization-count').disabled = busy || saving || !fixedCount();
    $('#diarization-count').placeholder = fixedCount() ? '녹음 전체의 인원수' : '자동 판단';
    $('#diarization-model-help').textContent = (info?.description || '') + (local() ? ' · 이 Mac에서 분석 · Apple GPU 또는 CPU 자동 선택' : '');
    $('#prepare-diarization').disabled = busy || installing || !host();
    $('#prepare-diarization').textContent = installing ? '모델 준비 중…' : '모델 준비하기';
    $('#diarization-setup-status').textContent = setup.error || ({installed:'모델 준비 완료', idle:'처음 한 번 모델을 준비해 주세요.', connecting:'서버 연결 중…', installing:'실행 환경 설치 중…', downloading:'모델 다운로드 중…'}[setup.phase] || '');
    $('#diarization-auth-help').classList.toggle('hidden', model() !== 'pyannote-community-1');
    if (setup.phase === 'auth_required') { $('#diarization-auth-help').open = true; authOpened = true; }
    else if (setup.phase === 'installed' && authOpened) { $('#diarization-auth-help').open = false; authOpened = false; }
    $('#run-diarization').disabled = busy || installing || setup.phase !== 'installed' || !gpu || saving;
    $('#cancel-diarization').classList.toggle('hidden', !busy);
    $('#refresh-diarization-gpus').disabled = busy || !host();
    $('#diarization-gpu-head').classList.toggle('hidden', local());
    $('#diarization-selection').textContent = `선택한 변경 ${selected().length}개`;
    $('#approve-diarization').disabled = busy || saving || !selected().length;
  }
  async function refreshSetup() {
    const current = generation, url = new URLSearchParams({host:host(), model_key:model()});
    if (!host()) return;
    const result = await api('/api/diarization/setup?' + url);
    if (current !== generation || !dialog.open) return;
    setup = result;
    controls();
  }
  async function refreshGpus() {
    const current = generation;
    gpu = null;
    $('#diarization-gpus').textContent = 'GPU 상태 확인 중…';
    controls();
    if (!host()) return;
    if (local()) {
      gpus = [{uuid:'local', idle_candidate:true}]; gpu = gpus[0];
      $('#diarization-gpus').textContent = '이 Mac에서 Nemotron으로 목소리를 분석합니다.';
      controls(); return;
    }
    try {
      const result = await api('/api/ssh/probe', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({host:host()})});
      if (current !== generation || !dialog.open) return;
      gpus = result.gpus || [];
      $('#diarization-gpus').innerHTML = gpus.map(item => `<label class="diarization-gpu-choice"><input type="radio" name="diarization-gpu" value="${escapeHtml(item.uuid)}"><span><strong>GPU #${item.index}</strong> ${escapeHtml(item.name)}<small>${escapeHtml(mib(item.memory_used_mib))} / ${escapeHtml(mib(item.memory_total_mib))} · ${item.idle_candidate ? '여유' : '사용 중 · 실행 전 확인'}</small></span></label>`).join('') || '사용 가능한 NVIDIA GPU를 찾을 수 없습니다.';
    } catch (error) { if (current === generation) $('#diarization-gpus').textContent = error.message; }
    controls();
  }
  function renderResult() {
    const result = job.result;
    $('#diarization-review').classList.toggle('hidden', !result);
    if (!result) return;
    const name = models.find(item => item.key === result.model_key)?.name || '화자 분석';
    const memory = result.peak_process_mib ? `Peak VRAM 약 ${(result.peak_process_mib / 1024).toFixed(2)} GiB`
      : result.peak_reserved_gib != null ? `Peak 예약 VRAM ${Number(result.peak_reserved_gib).toFixed(2)} GiB`
      : result.peak_host_rss_gib != null ? `프로세스 Peak RAM ${Number(result.peak_host_rss_gib).toFixed(2)} GiB` : '';
    $('#diarization-metrics').textContent = `${name} 결과 ${result.speaker_count}명 · 추론 ${clock(result.inference_seconds)} · 전체 ${clock(result.wall_seconds || result.total_seconds)} · ${memory}`;
    $('#diarization-voices').innerHTML = (result.voices || []).map(voice => `<article><strong>${escapeHtml(voice.speaker)}</strong><small>총 발화 ${clock(voice.seconds)}</small><div>${voice.samples.map((sample, i) => `<button class="inline-link" type="button" data-diarization-play="${sample.start}" data-diarization-end="${sample.end}">샘플 ${i + 1} · ${clock(sample.start)}</button>`).join('')}</div></article>`).join('');
    const rows = result.rows.filter(row => row.changed);
    $('#diarization-rows').innerHTML = rows.map(row => `<label class="diarization-row"><input type="checkbox" data-diarization-change value="${row.segment_id}"><span><small>${clock(row.start)} · ${escapeHtml(state.current?.speaker_names?.[row.before] || row.before)} → ${escapeHtml(state.current?.speaker_names?.[row.after] || row.after)}${row.mixed ? ' · 여러 목소리 확인 필요' : ''}</small><span>${escapeHtml(row.text)}</span><button class="inline-link" type="button" data-diarization-play="${row.start}">이 구간 듣기</button></span></label>`).join('');
    $('#diarization-empty').textContent = rows.length ? `${rows.length}개 발화의 화자가 달라졌습니다.${result.unmatched_segments ? ` 음성이 대응되지 않은 ${result.unmatched_segments}개 발화는 유지했습니다.` : ''}` : '기존 화자와 다른 변경 후보가 없습니다.';
    controls();
  }
  async function poll() {
    clearTimeout(timer);
    if (!dialog.open || !noteId) return;
    const current = generation;
    try {
      if (busyPhases.has(setup.phase)) await refreshSetup();
      const next = await api(`/api/notes/${encodeURIComponent(noteId)}/diarization`);
      if (current !== generation || !dialog.open) return;
      const changed = next.phase === 'done' && next.result?.proposal_id !== job.result?.proposal_id;
      job = next;
      status(job.error || ({preparing:'음성을 준비하고 있습니다…', loading:'화자 분리 모델을 불러오고 있습니다…', running:'목소리를 분석하고 있습니다. 창을 닫아도 앱 실행 중에는 계속됩니다.', done:'분석 완료 · 승인할 발화를 선택해 주세요.', cancelled:'분석을 취소했습니다.'}[job.phase] || ''), job.phase === 'error');
      if (changed) renderResult();
      controls();
    } catch (error) { if (current === generation) status(error.message, true); }
    if (dialog.open && current === generation) timer = setTimeout(poll, 1200);
  }
  $('#open-diarization').addEventListener('click', async () => {
    if (state.current?.status !== 'done' || state.current.source_type === 'text_file') return toast('원본 녹음이 보관된 노트를 선택해 주세요.', true);
    try {
      await flushSave();
      generation++;
      noteId = state.current.id;
      job = {}; setup = {}; gpu = null;
      $('#diarization-review').classList.add('hidden');
      $('#diarization-rows').replaceChildren();
      $('#diarization-count').value = '';
      $('#diarization-auth-help').open = false;
      authOpened = false;
      const data = await api('/api/diarization/models');
      models = data.models;
      $('#diarization-model').value = data.default_model_key || 'nemotron-3';
      $('#diarization-host').innerHTML = data.hosts.map(alias => `<option value="${escapeHtml(alias)}">${escapeHtml(alias === '@local' ? '이 Mac' : alias)}</option>`).join('');
      const device = state.devices.find(item => item.id === state.current.device_id);
      if (data.hosts.includes(device?.host)) $('#diarization-host').value = device.host;
      else if (data.hosts.includes('@local')) $('#diarization-host').value = '@local';
      job = await api(`/api/notes/${encodeURIComponent(noteId)}/diarization`);
      if (busyPhases.has(job.phase)) {
        if (data.hosts.includes(job.host)) $('#diarization-host').value = job.host;
        $('#diarization-model').value = job.model_key === 'pyannote-community-1' && job.num_speakers ? 'pyannote-community-1-fixed' : job.model_key || 'nemotron-3';
        $('#diarization-count').value = job.num_speakers || '';
      }
      dialog.showModal();
      $('#diarization-title').focus({preventScroll:true});
      $('#diarization-player').src = `/api/notes/${encodeURIComponent(noteId)}/media`;
      controls();
      await Promise.all([refreshSetup(), refreshGpus()]);
      await poll();
      if (job.result) renderResult();
    } catch (error) { status(error.message, true); toast(error.message, true); }
  });
  for (const id of ['#diarization-host', '#diarization-model']) $(id).addEventListener('change', async () => {
    generation++; setup = {};
    if (local()) $('#diarization-model').value = 'nemotron-3';
    try { await Promise.all([refreshSetup(), refreshGpus()]); await poll(); }
    catch (error) { status(error.message, true); }
  });
  $('#refresh-diarization-gpus').addEventListener('click', refreshGpus);
  $('#diarization-gpus').addEventListener('change', event => { gpu = gpus.find(item => item.uuid === event.target.value); controls(); });
  $('#prepare-diarization').addEventListener('click', async () => {
    try {
      setup = await api('/api/diarization/setup', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({host:host(), model_key:model()})});
      controls(); await poll();
    } catch (error) { status(error.message, true); }
  });
  $('#run-diarization').addEventListener('click', async () => {
    if (!gpu) return;
    try {
      if (!gpu.idle_candidate && !await confirmBusyGpu(gpu)) return;
      const count = fixedCount() ? $('#diarization-count').value : '';
      if (fixedCount() && !count) throw new Error('녹음 전체의 화자 인원수를 입력해 주세요. 모르면 자동 판단을 선택하세요.');
      if (count && (!Number.isInteger(Number(count)) || Number(count) < 1 || Number(count) > 32)) throw new Error('화자 인원수는 1~32명으로 입력해 주세요.');
      job = await api(`/api/notes/${encodeURIComponent(noteId)}/diarization`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({host:host(), model_key:model(), gpu_uuid:gpu.uuid, busy_confirmation:gpu.busy_confirmation, num_speakers:count ? Number(count) : null})});
      $('#diarization-rows').replaceChildren();
      $('#diarization-review').classList.add('hidden');
      controls(); await poll();
    } catch (error) { status(error.message, true); }
  });
  $('#cancel-diarization').addEventListener('click', async () => {
    try { job = await api(`/api/notes/${encodeURIComponent(noteId)}/diarization/cancel`, {method:'POST'}); await poll(); }
    catch (error) { status(error.message, true); }
  });
  $('#diarization-rows').addEventListener('change', controls);
  dialog.addEventListener('click', event => {
    const button = event.target.closest('[data-diarization-play]');
    if (!button) return;
    event.preventDefault();
    const player = $('#diarization-player');
    const end = Number(button.dataset.diarizationEnd || '0');
    player.ontimeupdate = end ? () => { if (player.currentTime >= end) { player.pause(); player.ontimeupdate = null; } } : null;
    player.currentTime = Number(button.dataset.diarizationPlay);
    player.play().catch(error => status(error.message, true));
  });
  $('#approve-diarization').addEventListener('click', async () => {
    if (saving || !selected().length) return;
    saving = true; controls();
    try {
      const note = await api(`/api/notes/${encodeURIComponent(noteId)}/diarization/approve`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({proposal_id:job.result.proposal_id, segment_ids:selected(), directory:state.saveDirectory})});
      if (state.current?.id === noteId) { state.current = note; renderEditor(); renderTextFileActions(); renderSummary(); }
      dialog.close();
      toast('승인한 발화의 화자를 저장했습니다.');
    } catch (error) { status(error.message, true); }
    finally { saving = false; controls(); }
  });
  $('#close-diarization').addEventListener('click', () => { if (!saving) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  dialog.addEventListener('close', () => { generation++; clearTimeout(timer); const player = $('#diarization-player'); player.pause(); player.ontimeupdate = null; player.removeAttribute('src'); player.load(); });
})();
