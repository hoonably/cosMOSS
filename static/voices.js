/* Optional local voice enrollment. Recording only starts after an explicit click. */
(() => {
  const ui = selector => document.querySelector(selector);
  const dialog = ui('#voice-profiles-dialog');
  const model = { profiles: [], engine: {}, busy: false, recording: false, requestingMic: false,
    source: null, objectURL: null, stream: null, recorder: null, timer: null, countdown: null,
    poll: null, token: 0, recorded: false, optionsDirty: false };
  window.cosmossVoiceProfiles = { get busy() { return model.busy || model.recording || model.requestingMic || !!model.source; } };

  function message(text = '', error = false) {
    ui('#voice-profile-message').textContent = text;
    ui('#voice-profile-message').classList.toggle('hidden', !text);
    ui('#voice-profile-message').classList.toggle('is-error', error);
  }
  function render() {
    const running = model.busy || model.recording || model.requestingMic;
    ui('#voice-profile-count').textContent = model.profiles.length;
    if (running || !(document.activeElement?.matches('input') && ui('#voice-profile-list').contains(document.activeElement))) ui('#voice-profile-list').innerHTML = model.profiles.map(profile => `
      <article class="voice-profile-row" data-voice-id="${escapeHtml(profile.id)}">
        <div class="voice-profile-description"><input value="${escapeHtml(profile.name)}" maxlength="60" aria-label="${escapeHtml(profile.name)} 프로필 이름" ${running ? 'disabled' : ''}><small>${profile.duration.toFixed(1)}초 목소리 · .mossvoice</small></div>
        <div class="voice-profile-actions"><button class="ghost-button" data-voice-action="rename" type="button" ${running ? 'disabled' : ''}>이름 저장</button><button class="ghost-button" data-voice-action="listen" type="button" ${running ? 'disabled' : ''}>듣기</button><button class="ghost-button" data-voice-action="export" type="button" ${running ? 'disabled' : ''}>다운로드</button><button class="icon-button danger" data-voice-action="delete" type="button" aria-label="${escapeHtml(profile.name)} 프로필 삭제" ${running ? 'disabled' : ''}>×</button></div>
      </article>`).join('') || '<p class="voice-profiles-empty">자주 만나는 사람의 목소리를 등록해 보세요.</p>';
    const engine = model.engine;
    ui('#voice-engine-status').textContent = engine.error || (engine.busy ? `${engine.message || '비교 도구를 준비하고 있습니다…'}${engine.percent == null ? '' : ' ' + engine.percent + '%'}` : engine.ready ? '목소리 비교 도구 준비 완료 · 이 Mac에서 처리' : '처음 한 번 비교 도구와 28MB 모델을 준비하세요.');
    ui('#prepare-voice-engine').textContent = engine.busy ? '준비 중…' : engine.ready ? '도구 확인·복구' : '비교 도구 준비';
    ui('#prepare-voice-engine').disabled = engine.busy || running;
    ui('#record-voice-profile').disabled = model.busy || model.recording || model.requestingMic;
    ui('#voice-enroll-file').disabled = running;
    ui('#import-voice-profile').disabled = running;
    ui('#save-voice-profile').disabled = !model.source || running || !engine.ready;
    ui('#save-voice-profile').textContent = model.busy ? '목소리 등록 중…' : '목소리 등록';
    ui('#save-voice-options').disabled = model.busy;
    ui('#voice-profile-name').disabled = model.busy;
    ui('#voice-sample-start').disabled = running;
  }
  async function refresh() {
    const result = await api('/api/voice-profiles');
    model.profiles = result.profiles;
    model.engine = result.engine;
    if (!model.optionsDirty) {
      ui('#voice-match-threshold').value = result.settings.threshold;
      ui('#voice-auto-enabled').checked = result.settings.enabled;
    }
    render();
    clearTimeout(model.poll);
    if (dialog.open && result.engine.busy) model.poll = setTimeout(() => refresh().catch(error => message(error.message, true)), 1000);
  }
  function stopTracks() {
    model.stream?.getTracks().forEach(track => track.stop());
    model.stream = null;
    clearTimeout(model.timer);
    clearInterval(model.countdown);
  }
  function clearSource() {
    ui('#voice-enroll-preview').pause();
    ui('#voice-enroll-preview').removeAttribute('src');
    ui('#voice-enroll-preview').classList.add('hidden');
    if (model.objectURL) URL.revokeObjectURL(model.objectURL);
    model.objectURL = null;
    model.source = null;
    ui('#voice-sample-start-field').classList.add('hidden');
    ui('#voice-source-status').textContent = '직접 녹음하거나 기존 녹음의 5초 구간을 선택하세요.';
  }
  function selectSource(file, recorded) {
    if (!file || !file.size) return;
    if (file.size > 128 * 1024 * 1024) return message('128MB 이하의 녹음 파일을 선택해 주세요.', true);
    clearSource();
    model.source = file;
    model.recorded = recorded;
    model.objectURL = URL.createObjectURL(file);
    ui('#voice-enroll-preview').src = model.objectURL;
    ui('#voice-enroll-preview').classList.remove('hidden');
    ui('#voice-sample-start').value = '0';
    ui('#voice-sample-start-field').classList.toggle('hidden', recorded);
    ui('#voice-source-status').textContent = recorded ? '5초 녹음 완료 · 듣고 이름을 등록하세요.' : `${file.name} · 시작 위치부터 5초를 사용합니다.`;
    render();
  }
  async function open() {
    if (!dialog.open) dialog.showModal();
    ui('#voice-profiles-title').focus({preventScroll: true});
    message();
    try { await refresh(); }
    catch (error) { message(error.message, true); }
  }
  document.addEventListener('click', event => { if (event.target.closest('[data-open-voice-profiles]')) open(); });
  ui('#close-voice-profiles').addEventListener('click', () => { if (!model.busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (model.busy) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    model.token++;
    if (model.recorder?.state === 'recording') model.recorder.stop();
    stopTracks();
    model.recording = model.requestingMic = false;
    clearTimeout(model.poll);
    clearSource();
    ui('#voice-enroll-file').value = '';
    ui('#voice-profile-player').pause();
    ui('#voice-profile-player').removeAttribute('src');
    ui('#voice-profile-player').classList.add('hidden');
  });
  ui('#prepare-voice-engine').addEventListener('click', async () => {
    try { await api('/api/voice-profiles/setup', {method: 'POST'}); await refresh(); }
    catch (error) { message(error.message, true); }
  });
  for (const selector of ['#voice-match-threshold', '#voice-auto-enabled']) ui(selector).addEventListener('input', () => { model.optionsDirty = true; });
  ui('#save-voice-options').addEventListener('click', async () => {
    try {
      const threshold = ui('#voice-match-threshold');
      if (!threshold.value || !threshold.reportValidity()) return;
      await api('/api/voice-profiles/settings', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: ui('#voice-auto-enabled').checked, threshold: Number(ui('#voice-match-threshold').value)})});
      model.optionsDirty = false;
      message('목소리 추천 설정을 저장했습니다.');
    } catch (error) { message(error.message, true); }
  });
  ui('#voice-enroll-file').addEventListener('change', event => { message(); selectSource(event.target.files[0], false); });
  ui('#voice-enroll-preview').addEventListener('play', () => {
    const start = model.recorded ? 0 : Number(ui('#voice-sample-start').value) || 0;
    if (ui('#voice-enroll-preview').currentTime < start || ui('#voice-enroll-preview').currentTime >= start + 5) ui('#voice-enroll-preview').currentTime = start;
  });
  ui('#voice-enroll-preview').addEventListener('timeupdate', () => {
    const start = model.recorded ? 0 : Number(ui('#voice-sample-start').value) || 0;
    if (ui('#voice-enroll-preview').currentTime >= start + 5) ui('#voice-enroll-preview').pause();
  });
  ui('#record-voice-profile').addEventListener('click', async () => {
    if (model.busy || model.recording || model.requestingMic) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return message('이 환경에서는 녹음할 수 없습니다. 기존 녹음 파일을 선택해 주세요.', true);
    const token = ++model.token;
    model.requestingMic = true;
    message('마이크 접근 요청을 허용한 뒤 자연스럽게 말씀해 주세요.');
    render();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true, video: false});
      if (!dialog.open || token !== model.token) { stream.getTracks().forEach(track => track.stop()); return; }
      model.stream = stream;
      const mimeType = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm'].find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? {mimeType} : {});
      model.recorder = recorder;
      const chunks = [];
      let failed = false;
      recorder.addEventListener('dataavailable', event => { if (event.data.size) chunks.push(event.data); });
      recorder.addEventListener('stop', () => {
        stopTracks();
        model.recording = false;
        if (!failed && dialog.open && token === model.token) {
          const blob = new Blob(chunks, {type: recorder.mimeType});
          selectSource(new File([blob], '목소리.' + (recorder.mimeType.includes('mp4') ? 'm4a' : 'webm'), {type: recorder.mimeType}), true);
          message('녹음이 끝났습니다. 이름을 입력하고 ‘목소리 등록’을 누르세요.');
        }
        render();
      }, {once: true});
      recorder.addEventListener('error', () => { failed = true; stopTracks(); model.recording = false; message('녹음을 완료하지 못했습니다. 다시 시도해 주세요.', true); render(); });
      recorder.start();
      model.requestingMic = false;
      model.recording = true;
      let left = 5;
      message(`녹음 중 · ${left}초 남았습니다.`);
      model.countdown = setInterval(() => { left--; message(`녹음 중 · ${Math.max(0, left)}초 남았습니다.`); }, 1000);
      model.timer = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, 5000);
    } catch (error) {
      stopTracks();
      message(error.name === 'NotAllowedError' ? '마이크 접근이 허용되지 않았습니다. macOS 개인정보 보호 및 보안의 마이크 설정을 확인하거나 녹음 파일을 선택하세요.' : error.message, true);
    } finally { model.requestingMic = false; render(); }
  });
  ui('#voice-enroll-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!model.source || model.busy || model.recording || !model.engine.ready) return;
    const data = new FormData();
    data.set('file', model.source);
    data.set('name', ui('#voice-profile-name').value.trim());
    data.set('start', model.recorded ? '0' : ui('#voice-sample-start').value || '0');
    data.set('seconds', '5');
    model.busy = true;
    message('목소리 특징을 추출하고 있습니다…');
    render();
    try {
      await api('/api/voice-profiles/enroll', {method: 'POST', body: data});
      clearSource();
      ui('#voice-enroll-file').value = '';
      ui('#voice-profile-name').value = '';
      ui('#voice-source-status').textContent = '직접 녹음하거나 기존 녹음의 5초 구간을 선택하세요.';
      message('목소리를 등록했습니다. 다음 전사부터 이름 추천에 사용합니다.');
      await refresh();
    } catch (error) { message(error.message, true); }
    finally { model.busy = false; render(); }
  });
  ui('#import-voice-profile').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file || model.busy) return;
    model.busy = true;
    render();
    try {
      const data = new FormData();
      data.set('file', file);
      await api('/api/voice-profiles/import', {method: 'POST', body: data});
      await refresh();
      message('목소리 프로필을 불러왔습니다.');
    } catch (error) { message(error.message, true); }
    finally { model.busy = false; event.target.value = ''; render(); }
  });
  ui('#voice-profile-list').addEventListener('click', async event => {
    const button = event.target.closest('[data-voice-action]');
    if (!button || model.busy) return;
    const row = button.closest('[data-voice-id]');
    const path = '/api/voice-profiles/' + encodeURIComponent(row.dataset.voiceId);
    try {
      if (button.dataset.voiceAction === 'export') {
        const link = document.createElement('a');
        link.href = path + '/export'; link.download = ''; document.body.append(link); link.click(); link.remove();
      } else if (button.dataset.voiceAction === 'listen') {
        const player = ui('#voice-profile-player');
        player.src = path + '/sample'; player.classList.remove('hidden'); await player.play();
      } else if (button.dataset.voiceAction === 'rename') {
        await api(path, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: row.querySelector('input').value.trim()})});
        await refresh(); message('프로필 이름을 저장했습니다.');
      } else if (button.dataset.voiceAction === 'delete') {
        if (button.dataset.confirm !== 'yes') { button.dataset.confirm = 'yes'; button.textContent = '삭제'; button.title = '한 번 더 누르면 이 프로필을 삭제합니다.'; return; }
        await api(path, {method: 'DELETE'}); await refresh(); message('목소리 프로필을 삭제했습니다.');
      }
    } catch (error) { message(error.message, true); }
  });
})();
