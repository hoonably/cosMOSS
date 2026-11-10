/* Per-note model choices. Device defaults are only read when the dialog opens. */
(() => {
  const pipeline = $('#speech-pipeline'), localModel = $('#upload-local-model'), batch = $('#speech-batch');
  const keys = ['moss-sglang','qwen-transformers','moss-qwen-sequential'];
  let deviceId = null, localOptionsKey = null;
  let setupKey = null, setupState = null, setupRevision = 0, setupTimer = null, setupController = null;
  const descriptions = {
    'moss-sglang': '문맥을 유지해 전사합니다. GPU별 자동 구간(최대 90분)을 넘으면 확인 후 최소 개수로 균등 분할합니다. 화자 구분은 Nemotron 0.1B.',
    'qwen-transformers': '약 3분씩 빠르게 전사한 뒤 단어 시간을 정렬합니다. 화자 구분은 Nemotron 0.1B.',
    'moss-qwen-sequential': 'MOSS 전체 전사 후 Qwen3-ASR 1.7B를 순차 실행합니다. MOSS 본문과 비교용 Qwen 대본을 각각 보관합니다.',
  };
  function available() { return executionDevice()?.backend === 'cuda' && executionDevice()?.model_key !== 'custom' && executionDevice()?.model_selected !== false; }
  function localActive() { return executionDevice()?.kind === 'local' && executionDevice()?.backend === 'mlx' && executionDevice()?.model_selected !== false; }
  function active() { return available() && keys.includes(pipeline.value); }
  function managed() { return active() || localActive(); }
  function selectedModel() { return localActive() ? localModel.value : null; }
  function blocked() { return managed() && setupState?.phase !== 'installed'; }
  function selection() { return localActive() ? localModel.value : pipeline.value; }
  function setupUrl(id, chosen, query = true) {
    const local = id === 'local', path = local ? 'model-readiness' : 'speech-setup';
    return `/api/devices/${encodeURIComponent(id)}/${path}${query ? `?${local ? 'model_key' : 'pipeline'}=${encodeURIComponent(chosen)}` : ''}`;
  }
  function syncLocalOptions(device, entering) {
    const selected = entering || !localModel.value ? device.model_key || state.localModel?.model_key || 'mlx-8bit' : localModel.value;
    const optionsKey = JSON.stringify([device.id, state.catalog?.models, state.catalog?.recommendation?.model_key, device.model_key]);
    if (optionsKey === localOptionsKey && !entering) return;
    localOptionsKey = optionsKey;
    localModel.innerHTML = modelOptionsHtml('local', selected);
    if ([...localModel.options].some(option => option.value === selected)) localModel.value = selected;
  }
  function preferenceKey() { return `cosmoss.speech.${executionDevice()?.id}`; }
  function resetSetup() {
    ++setupRevision;
    clearTimeout(setupTimer);
    setupController?.abort();
    setupKey = null; setupState = null;
  }
  function authLink(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'huggingface.co' && !url.port && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }
  function renderSetup() {
    const phase = setupState?.phase || 'checking';
    const authRequired = phase === 'auth_required';
    const busy = phase === 'checking' || modelBusy(setupState);
    $('#upload-model-setup').classList.toggle('hidden', !managed() || phase === 'installed');
    $('#upload-model-status').textContent = phase === 'checking' ? '모델 준비 상태 확인 중…'
      : busy ? '선택한 모델을 준비하고 있어요…'
      : authRequired ? setupState.error || 'Hugging Face 로그인 또는 모델 이용 동의가 필요합니다.'
      : phase === 'error' ? setupState.error || '모델을 준비하지 못했습니다.'
      : localActive() && setupState?.asr_ready && !setupState?.diarization_ready ? '화자 구분에 필요한 Nemotron 0.1B를 준비해 주세요.'
      : '선택한 전사 모델과 화자 구분 모델을 준비한 뒤 시작할 수 있습니다.';
    const button = $('#prepare-upload-model');
    button.classList.toggle('hidden', busy);
    button.disabled = state.uploading || busy;
    button.textContent = setupState?.checkFailed ? '다시 확인' : authRequired ? '확인 후 다시 시도' : phase === 'error' ? '다운로드 다시 시도' : '모델 다운로드';
    $('#upload-model-auth').classList.toggle('hidden', !managed() || !authRequired);
    $('#upload-model-auth-help').textContent = authRequired
      ? `${localActive() ? '이 Mac' : '실행 서버'}에서 Hugging Face에 로그인하고, 모델 이용 동의가 필요한 경우 모델 페이지에서 동의한 뒤 다시 시도해 주세요.` : '';
    const link = $('#upload-model-auth-link'), href = authRequired ? authLink(setupState.auth_url) : null;
    link.classList.toggle('hidden', !href);
    if (href) link.href = href;
    else link.removeAttribute('href');
  }
  async function checkSetup(id, chosen, identity) {
    const revision = ++setupRevision;
    setupController?.abort();
    const controller = new AbortController(); setupController = controller;
    clearTimeout(setupTimer);
    try {
      const result = await api(setupUrl(id, chosen), {signal:controller.signal});
      if (revision !== setupRevision || setupKey !== identity || !$('#upload-dialog').open) return;
      setupState = result;
      renderFilePreparation();
      if (modelBusy(result))
        setupTimer = setTimeout(() => checkSetup(id,chosen,identity),1000);
    } catch (error) {
      if (revision !== setupRevision || setupKey !== identity || error.name === 'AbortError') return;
      setupState = {phase:'error',error:error.message,checkFailed:true};
      renderFilePreparation();
    }
  }
  function sync() {
    const device = executionDevice(), server = available(), local = localActive(), shown = server || local;
    $('#speech-options').classList.toggle('hidden', !shown);
    pipeline.closest('label').classList.toggle('hidden', !server);
    localModel.closest('label').classList.toggle('hidden', !local);
    const entering = $('#upload-dialog').open && deviceId !== device?.id;
    if (local && $('#upload-dialog').open) syncLocalOptions(device, entering);
    if (entering) {
      deviceId = device?.id;
      const chosen = device?.options?.speech_pipeline;
      pipeline.value = keys.includes(chosen) ? chosen : chosen === 'qwen-batch' ? 'qwen-transformers' : 'moss-sglang';
      let saved;
      try { saved = JSON.parse(localStorage.getItem(preferenceKey())); } catch (_) {}
      batch.value = ['0','1','4','8','16','24'].includes(saved?.batch) ? saved.batch : '0';
      resetSetup();
    }
    pipeline.disabled = !server || state.uploading;
    localModel.disabled = !local || state.uploading;
    const fast = active(), fixed = pipeline.value !== 'qwen-transformers';
    batch.disabled = fixed || !fast || state.uploading || !state.selectedGpu;
    $('#speech-batch-controls').classList.toggle('hidden', !fast);
    $('#speech-batch-controls').classList.toggle('is-fixed', fixed);
    batch.closest('label').classList.toggle('hidden', fixed);
    $('.upload-chunk-row').classList.toggle('hidden', fast);
    $('.upload-chunk-info').classList.toggle('hidden', fast);
    $('#speech-description').textContent = local
      ? 'MOSS MLX로 전사한 뒤 Nemotron 0.1B로 전체 녹음의 화자를 구분합니다. 두 모델은 순서대로 실행하고 메모리를 해제합니다.'
      : descriptions[pipeline.value] || '';
    $('#speech-length-notice').classList.toggle('hidden',
      !server || pipeline.value === 'qwen-transformers' || !(uploadDuration() >= 45 * 60));
    const caution = $('#local-model-caution');
    caution.classList.toggle('hidden', !local || localModel.value !== 'mlx-4bit');
    caution.textContent = local && localModel.value === 'mlx-4bit' ? '4bit는 실측에서 시간표시 오류와 긴 녹음의 출력 중단이 확인됐습니다. 8bit를 권장합니다.' : '';
    if (fast && (!state.selectedGpu || !uploadDuration())) {
      $('#speech-batch-plan').textContent = '파일을 올리고 GPU 번호를 선택하면 예상 시간과 메모리를 확인할 수 있습니다.';
      $('#speech-batch-plan').classList.add('needs-input');
    }
    if (managed() && $('#upload-dialog').open && selection()) {
      const chosen = selection(), identity = JSON.stringify([device.id,chosen]);
      if (setupKey !== identity) {
        resetSetup(); setupKey = identity; setupState = {phase:'checking'};
        checkSetup(device.id,chosen,identity);
      }
    }
    renderSetup();
  }
  function estimate() {
    const device = executionDevice(), gpu = state.selectedGpu, duration = uploadDuration(), chosen = pipeline.value;
    if (!device || !gpu || !duration) return;
    const key = JSON.stringify(['speech',device.id,chosen,batch.value,gpu.uuid,gpu.name,gpu.memory_total_mib,gpu.memory_free_mib,duration]);
    if (state.estimateKey === key) return;
    clearTimeout(state.estimateTimer);
    state.estimateController?.abort();
    state.estimateKey = key;
    const box = $('#upload-estimates');
    box.classList.remove('hidden','needs-gpu-selection','is-pending','is-over-capacity');
    $('#upload-estimate-memory').textContent = '—';
    $('#upload-estimate-time').textContent = '—';
    $('#upload-estimate-note').textContent = '';
    state.estimateTimer = setTimeout(async () => {
      const controller = new AbortController(); state.estimateController = controller;
      const fixed = chosen !== 'qwen-transformers';
      const query = new URLSearchParams({pipeline:chosen,batch:fixed ? '0' : batch.value,duration,gpu_uuid:gpu.uuid});
      try {
        const result = await api(`/api/devices/${encodeURIComponent(device.id)}/speech-estimate?${query}`, {signal:controller.signal});
        if (state.estimateKey !== key) return;
        $('#upload-estimate-memory').textContent = `약 ${result.memory_expected_gib.toFixed(1)} GiB`;
        $('#upload-estimate-time').textContent = result.time_seconds == null ? '추가 측정 필요' : `약 ${clock(result.time_seconds)}`;
        renderEstimateCalibration(result);
        const mossPlan = (result.moss_partition_count || 1)>1 ? `${result.moss_partition_count}개 균등 구간 · 구간당 ${clock(result.chunk_seconds)}` : '전체 1청크 · 문맥 유지';
        const context = result.moss_chunk_seconds ? ` · 자동 최대 ${result.moss_chunk_seconds/60}분` : '';
        $('#speech-batch-plan').textContent = chosen === 'moss-sglang' ? mossPlan+context
          : chosen === 'moss-qwen-sequential' ? `MOSS ${mossPlan}${context} · Qwen 보조 대본`
          : `${result.chunk_count}개 청크 · 한 번에 ${result.batch}개${batch.value === '0' ? ' 자동 추천' : ''}`;
        $('#speech-batch-plan').classList.remove('needs-input');
        const risk = result.exceeds_budget || result.exceeds_capacity;
        box.classList.toggle('is-over-capacity', risk);
        const warning = $('#upload-memory-warning');
        warning.classList.toggle('hidden', !risk && !result.exceeds_free);
        warning.textContent = result.exceeds_capacity ? `예상 메모리 약 ${result.memory_expected_gib.toFixed(1)} GiB가 전체 VRAM을 넘습니다. 실행할 수 있지만 실제 메모리가 부족하면 실패합니다.`
          : result.exceeds_budget ? `예상 메모리가 전체 VRAM의 70%를 넘습니다. 실행할 수 있지만 실제 메모리가 부족하면 실패합니다.`
          : result.exceeds_free ? `약 ${result.memory_expected_gib.toFixed(1)} GiB가 필요합니다. 현재 여유 VRAM보다 큽니다. 다른 작업의 사용량을 확인하세요.` : '';
      } catch (error) {
        if (state.estimateKey === key && error.name !== 'AbortError') $('#upload-estimate-note').textContent = error.message || '예상값을 확인하지 못했습니다. GPU를 다시 선택해 주세요.';
      }
    }, 180);
  }
  function form(body) {
    if (localActive()) body.set('local_model_key',localModel.value);
    else body.delete('local_model_key');
    if (!available()) { body.delete('speech_pipeline'); body.delete('speech_batch'); return; }
    body.set('speech_pipeline',pipeline.value);
    body.delete('chunk_count'); body.delete('chunk_minutes');
    body.set('speech_batch',pipeline.value === 'qwen-transformers' ? batch.value : '0');
  }
  function changed() {
    batch.setCustomValidity('');
    clearUploadEstimate(); renderFilePreparation();
  }
  pipeline.addEventListener('change', () => {
    batch.value = '0'; resetSetup(); changed();
  });
  localModel.addEventListener('change', () => { resetSetup(); changed(); });
  batch.addEventListener('change', () => {
    try { localStorage.setItem(preferenceKey(),JSON.stringify({batch:batch.value})); } catch (_) {}
    changed();
  });
  $('#prepare-upload-model').addEventListener('click', async () => {
    const device = executionDevice(), chosen = selection(), identity = setupKey;
    if (!managed() || !device || !$('#upload-dialog').open) return;
    if (setupState?.checkFailed) {
      setupState = {phase:'checking'}; renderFilePreparation();
      return checkSetup(device.id,chosen,identity);
    }
    const revision = ++setupRevision;
    setupState = {phase:'connecting'}; renderFilePreparation();
    try {
      const result = await api(setupUrl(device.id, chosen, false), {
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(localActive() ? {model_key:chosen} : {pipeline:chosen})});
      if (revision !== setupRevision || setupKey !== identity) return;
      setupState = result; renderFilePreparation();
      checkSetup(device.id,chosen,identity);
    } catch (error) {
      if (revision !== setupRevision || setupKey !== identity) return;
      setupState = {phase:'error',error:error.message}; renderFilePreparation();
    }
  });
  $('#upload-dialog').addEventListener('close', () => {
    deviceId = null; localOptionsKey = null; resetSetup();
    batch.setCustomValidity('');
  });
  window.cosmossSpeech = {active,sync,estimate,form,blocked,selectedModel};
})();
