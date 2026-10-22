/* Per-note CUDA presets. Device defaults are only read when the dialog opens. */
(() => {
  const pipeline = $('#speech-pipeline'), batch = $('#speech-batch');
  const keys = ['moss-sglang','qwen-transformers','moss-qwen-sequential'];
  let deviceId = null, consentKey = null;
  let setupKey = null, setupState = null, setupRevision = 0, setupTimer = null, setupController = null;
  const descriptions = {
    'moss-sglang': '전체 녹음의 문맥을 유지해 전사합니다. 최대 90분 · 화자 구분은 Nemotron.',
    'qwen-transformers': '약 3분씩 빠르게 전사한 뒤 단어 시간을 정렬합니다. 화자 구분은 Nemotron.',
    'moss-qwen-sequential': 'MOSS 전체 전사 후 Qwen을 순차 실행합니다. MOSS 본문과 비교용 Qwen 대본을 각각 보관합니다.',
  };
  function available() { return executionDevice()?.backend === 'cuda' && executionDevice()?.model_key !== 'custom' && executionDevice()?.model_selected !== false; }
  function active() { return available() && keys.includes(pipeline.value); }
  function blocked() { return active() && setupState?.phase !== 'installed'; }
  function preferenceKey() { return `cosmoss.speech.${executionDevice()?.id}`; }
  function resetSetup() {
    ++setupRevision;
    clearTimeout(setupTimer);
    setupController?.abort();
    setupKey = null; setupState = null;
  }
  function renderSetup() {
    const phase = setupState?.phase || 'checking';
    const busy = ['checking','connecting','installing','installing_uv','downloading','loading'].includes(phase);
    $('#upload-model-setup').classList.toggle('hidden', !active() || phase === 'installed');
    $('#upload-model-status').textContent = phase === 'checking' ? '모델 준비 상태 확인 중…'
      : busy ? '선택한 모델을 준비하고 있어요…'
      : phase === 'error' ? setupState.error || '모델을 준비하지 못했습니다.'
      : '선택한 모델을 다운로드한 뒤 시작할 수 있습니다.';
    const button = $('#prepare-upload-model');
    button.classList.toggle('hidden', busy);
    button.disabled = state.uploading || busy;
    button.textContent = setupState?.checkFailed ? '다시 확인' : phase === 'error' ? '다운로드 다시 시도' : '모델 다운로드';
  }
  async function checkSetup(id, chosen, identity) {
    const revision = ++setupRevision;
    setupController?.abort();
    const controller = new AbortController(); setupController = controller;
    clearTimeout(setupTimer);
    try {
      const result = await api(`/api/devices/${encodeURIComponent(id)}/speech-setup?pipeline=${encodeURIComponent(chosen)}`, {signal:controller.signal});
      if (revision !== setupRevision || setupKey !== identity || !$('#upload-dialog').open) return;
      setupState = result;
      renderFilePreparation();
      if (['connecting','installing','installing_uv','downloading','loading'].includes(result.phase))
        setupTimer = setTimeout(() => checkSetup(id,chosen,identity),1000);
    } catch (error) {
      if (revision !== setupRevision || setupKey !== identity || error.name === 'AbortError') return;
      setupState = {phase:'error',error:error.message,checkFailed:true};
      renderFilePreparation();
    }
  }
  function sync() {
    const device = executionDevice(), shown = available();
    $('#speech-options').classList.toggle('hidden', !shown);
    if ($('#upload-dialog').open && deviceId !== device?.id) {
      deviceId = device?.id;
      const chosen = device?.options?.speech_pipeline;
      pipeline.value = keys.includes(chosen) ? chosen : chosen === 'qwen-batch' ? 'qwen-transformers' : 'moss-sglang';
      let saved;
      try { saved = JSON.parse(localStorage.getItem(preferenceKey())); } catch (_) {}
      batch.value = ['0','1','4','8','16','24'].includes(saved?.batch) ? saved.batch : '0';
      $('#allow-speech-memory-risk').checked = false;
      resetSetup();
    }
    pipeline.disabled = !shown || state.uploading;
    const fast = active(), fixed = pipeline.value !== 'qwen-transformers';
    batch.disabled = fixed || !fast || state.uploading || !state.selectedGpu;
    $('#speech-batch-controls').classList.toggle('hidden', !fast);
    $('#speech-batch-controls').classList.toggle('is-fixed', fixed);
    batch.closest('label').classList.toggle('hidden', fixed);
    $('.upload-chunk-row').classList.toggle('hidden', fast);
    $('.upload-chunk-info').classList.toggle('hidden', fast);
    if (!fast) {
      $('#speech-memory-consent').classList.add('hidden');
      $('#allow-speech-memory-risk').required = false;
    }
    $('#speech-description').textContent = descriptions[pipeline.value] || '';
    if (fast && (!state.selectedGpu || !state.uploadPreparation?.duration)) {
      $('#speech-batch-plan').textContent = '파일을 올리고 GPU 번호를 선택하면 예상 시간과 메모리를 확인할 수 있습니다.';
      $('#speech-batch-plan').classList.add('needs-input');
    }
    if (fast && $('#upload-dialog').open) {
      const identity = JSON.stringify([device.id,pipeline.value]);
      if (setupKey !== identity) {
        resetSetup(); setupKey = identity; setupState = {phase:'checking'};
        checkSetup(device.id,pipeline.value,identity);
      }
    }
    renderSetup();
  }
  function estimate() {
    const device = executionDevice(), gpu = state.selectedGpu, duration = state.uploadPreparation?.duration, chosen = pipeline.value;
    if (!device || !gpu || !duration) return;
    const key = JSON.stringify(['speech',device.id,chosen,batch.value,gpu.uuid,gpu.name,gpu.memory_total_mib,gpu.memory_free_mib,duration]);
    if (state.estimateKey === key) return;
    clearTimeout(state.estimateTimer);
    state.estimateController?.abort();
    state.estimateKey = key;
    const nextConsent = JSON.stringify([device.id,chosen,batch.value,gpu.uuid,gpu.memory_total_mib,duration]);
    if (consentKey !== nextConsent) $('#allow-speech-memory-risk').checked = false;
    consentKey = nextConsent;
    $('#speech-memory-consent').classList.add('hidden');
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
        $('#upload-estimate-time').textContent = result.time_seconds == null ? '업데이트 예정입니다.' : `약 ${clock(result.time_seconds)}`;
        $('#speech-batch-plan').textContent = chosen === 'moss-sglang' ? '전체 1청크 · 문맥 유지'
          : chosen === 'moss-qwen-sequential' ? 'MOSS 전체 1청크 · Qwen 약20분 보조 대본'
          : `${result.chunk_count}개 청크 · 한 번에 ${result.batch}개${batch.value === '0' ? ' 자동 추천' : ''}`;
        $('#speech-batch-plan').classList.remove('needs-input');
        const risk = result.exceeds_budget || result.exceeds_capacity;
        box.classList.toggle('is-over-capacity', risk);
        const warning = $('#upload-memory-warning');
        warning.classList.toggle('hidden', !risk && !result.exceeds_free);
        const advice = fixed ? '더 큰 GPU 또는 다른 전사 방식을 선택해 주세요.' : '동시 청크 수를 줄여 주세요.';
        warning.textContent = result.exceeds_capacity ? `필요한 약 ${result.memory_expected_gib.toFixed(1)} GiB가 전체 VRAM을 초과합니다. ${advice}`
          : result.exceeds_budget ? `예상 Peak Memory가 전체 VRAM의 70%를 넘습니다. 메모리 부족으로 실패할 수 있습니다. ${advice}`
          : result.exceeds_free ? `약 ${result.memory_expected_gib.toFixed(1)} GiB가 필요합니다. 현재 여유 VRAM보다 큽니다. 다른 작업의 사용량을 확인하세요.` : '';
        $('#speech-memory-consent').classList.toggle('hidden', !result.exceeds_budget || result.exceeds_capacity);
        $('#allow-speech-memory-risk').required = result.exceeds_budget && !result.exceeds_capacity;
        batch.setCustomValidity(result.exceeds_capacity ? advice : '');
      } catch (error) {
        if (state.estimateKey === key && error.name !== 'AbortError') $('#upload-estimate-note').textContent = error.message || '예상값을 확인하지 못했습니다. GPU를 다시 선택해 주세요.';
      }
    }, 180);
  }
  function form(body) {
    if (!available()) { body.delete('speech_pipeline'); body.delete('speech_batch'); return; }
    body.set('speech_pipeline',pipeline.value);
    body.delete('chunk_count'); body.delete('chunk_minutes');
    body.set('speech_batch',pipeline.value === 'qwen-transformers' ? batch.value : '0');
  }
  function changed() {
    batch.setCustomValidity('');
    $('#allow-speech-memory-risk').required = false;
    $('#allow-speech-memory-risk').checked = false;
    clearUploadEstimate(); renderFilePreparation();
  }
  pipeline.addEventListener('change', () => {
    batch.value = '0'; consentKey = null; resetSetup(); changed();
  });
  batch.addEventListener('change', () => {
    try { localStorage.setItem(preferenceKey(),JSON.stringify({batch:batch.value})); } catch (_) {}
    changed();
  });
  $('#prepare-upload-model').addEventListener('click', async () => {
    const device = executionDevice(), chosen = pipeline.value, identity = setupKey;
    if (!active() || !device || !$('#upload-dialog').open) return;
    if (setupState?.checkFailed) {
      setupState = {phase:'checking'}; renderFilePreparation();
      return checkSetup(device.id,chosen,identity);
    }
    const revision = ++setupRevision;
    setupState = {phase:'connecting'}; renderFilePreparation();
    try {
      const result = await api(`/api/devices/${encodeURIComponent(device.id)}/speech-setup`, {
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pipeline:chosen})});
      if (revision !== setupRevision || setupKey !== identity) return;
      setupState = result; renderFilePreparation();
      checkSetup(device.id,chosen,identity);
    } catch (error) {
      if (revision !== setupRevision || setupKey !== identity) return;
      setupState = {phase:'error',error:error.message}; renderFilePreparation();
    }
  });
  $('#upload-dialog').addEventListener('close', () => {
    deviceId = null; consentKey = null; resetSetup();
    $('#allow-speech-memory-risk').required = false;
    batch.setCustomValidity('');
  });
  window.cosmossSpeech = {active,sync,estimate,form,blocked};
})();
