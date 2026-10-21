/* CUDA execution presets. The initial ASR uses Nemotron; later edits need approval. */
(() => {
  const pipeline = $('#speech-pipeline'), batch = $('#speech-batch');
  let deviceId = null, consentKey = null;
  const descriptions = {
    'moss-batch': '약 3분 청크를 동시에 전사하고 Nemotron으로 화자를 구분합니다.',
    'qwen-batch': '한국어 전사 → 시간 정렬 → Nemotron. 메모리를 더 사용하며 시간이 겹치는 단어는 확인이 필요합니다.',
    'moss-original': '긴 문맥을 유지하며 순서대로 전사합니다. 화자는 완료 후 재분리할 수 있습니다.',
  };
  function available() { return executionDevice()?.backend === 'cuda' && executionDevice()?.model_key !== 'custom' && executionDevice()?.model_selected !== false; }
  function active() { return available() && pipeline.value !== 'moss-original'; }
  function preferenceKey() { return `cosmoss.speech.${executionDevice()?.id}`; }
  function sync() {
    const shown = available();
    $('#speech-options').classList.toggle('hidden', !shown);
    if (deviceId !== executionDevice()?.id) {
      deviceId = executionDevice()?.id;
      let saved;
      try { saved = JSON.parse(localStorage.getItem(preferenceKey())); } catch (_) {}

      batch.value = ['0','1','4','8','16','24'].includes(saved?.batch) ? saved.batch : '0';
      $('#allow-speech-memory-risk').checked = false;
    }
    pipeline.value = executionDevice()?.options?.speech_pipeline || 'moss-original';
    const fast = active();
    pipeline.disabled = true;
    $('#speech-chosen-model').textContent = pipeline.value === 'qwen-batch' ? 'Qwen3-ASR + Nemotron' : pipeline.value === 'moss-batch' ? 'MOSS + Nemotron' : 'MOSS · 긴 문맥';
    batch.disabled = !fast || state.uploading || !state.selectedGpu;
    $('#speech-batch-controls').classList.toggle('hidden', !fast);
    $('.upload-chunk-row').classList.toggle('hidden', fast);
    $('.upload-chunk-info').classList.toggle('hidden', fast);
    if (!fast) {
      $('#speech-memory-consent').classList.add('hidden');
      $('#allow-speech-memory-risk').required = false;
    }
    $('#speech-description').textContent = descriptions[pipeline.value] || '';
    if (fast && (!state.selectedGpu || !state.uploadPreparation?.duration)) {
      $('#speech-batch-plan').textContent = '파일을 올리고 GPU 번호를 선택하면 동시 청크 수에 따른 예상값을 확인할 수 있습니다.';
      $('#speech-batch-plan').classList.add('needs-input');
    }
  }
  function estimate() {
    const device = executionDevice(), gpu = state.selectedGpu, duration = state.uploadPreparation?.duration;
    if (!device || !gpu || !duration) return;
    const key = JSON.stringify(['speech',device.id,pipeline.value,batch.value,gpu.uuid,gpu.name,gpu.memory_total_mib,gpu.memory_free_mib,duration]);
    if (state.estimateKey === key) return;
    clearTimeout(state.estimateTimer);
    state.estimateController?.abort();
    state.estimateKey = key;
    const nextConsent = JSON.stringify([device.id,pipeline.value,batch.value,gpu.uuid,gpu.memory_total_mib,duration]);
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
      const query = new URLSearchParams({pipeline:pipeline.value,batch:batch.value,duration,gpu_uuid:gpu.uuid});
      try {
        const result = await api(`/api/devices/${encodeURIComponent(device.id)}/speech-estimate?${query}`, {signal:controller.signal});
        if (state.estimateKey !== key) return;
        $('#upload-estimate-memory').textContent = `약 ${result.memory_expected_gib.toFixed(1)} GiB`;
        $('#upload-estimate-time').textContent = result.time_seconds == null ? '업데이트 예정입니다.' : `약 ${clock(result.time_seconds)}`;
        $('#speech-batch-plan').textContent = `${result.chunk_count}개 청크 · 한 번에 ${result.batch}개${batch.value === '0' ? ' 자동 추천' : ''}${result.budget_gib ? ` · VRAM 목표 ${result.budget_gib.toFixed(1)} GiB 이내` : ''}`;
        $('#speech-batch-plan').classList.remove('needs-input');
        const risk = result.exceeds_budget || result.exceeds_capacity;
        box.classList.toggle('is-over-capacity', risk);
        const warning = $('#upload-memory-warning');
        warning.classList.toggle('hidden', !risk && !result.exceeds_free);
        warning.textContent = result.exceeds_capacity ? `필요한 약 ${result.memory_expected_gib.toFixed(1)} GiB가 전체 VRAM을 초과합니다. 동시 청크 수를 줄여 주세요.`
          : result.exceeds_budget ? `예상 Peak Memory가 전체 VRAM의 70%를 넘습니다. 메모리 부족으로 실패할 수 있습니다. 동시 청크 수를 줄이는 것을 권장합니다.`
          : result.exceeds_free ? `약 ${result.memory_expected_gib.toFixed(1)} GiB가 필요합니다. 현재 여유 VRAM보다 큽니다. 다른 작업의 사용량을 확인하세요.` : '';
        $('#speech-memory-consent').classList.toggle('hidden', !result.exceeds_budget || result.exceeds_capacity);
        $('#allow-speech-memory-risk').required = result.exceeds_budget && !result.exceeds_capacity;
        $('#speech-batch').setCustomValidity(result.exceeds_capacity ? '동시 청크 수를 줄여 주세요.' : '');
      } catch (error) {
        if (state.estimateKey === key && error.name !== 'AbortError') $('#upload-estimate-note').textContent = '예상값을 확인하지 못했습니다. GPU를 다시 선택해 주세요.';
      }
    }, 180);
  }
  function form(body) {
    if (!available()) { body.delete('speech_pipeline'); body.delete('speech_batch'); return; }
    body.set('speech_pipeline',pipeline.value);
    if (active()) { body.delete('chunk_count'); body.delete('chunk_minutes'); body.set('speech_batch',batch.value); }
    else body.delete('speech_batch');
  }
  for (const element of [batch]) element.addEventListener('change', () => {
    try { localStorage.setItem(preferenceKey(),JSON.stringify({batch:batch.value})); } catch (_) {}
    $('#speech-batch').setCustomValidity('');
    $('#allow-speech-memory-risk').required = false;
    $('#allow-speech-memory-risk').checked = false;
    clearUploadEstimate(); renderFilePreparation();
  });
  $('#upload-dialog').addEventListener('close', () => {
    deviceId=null;
    $('#allow-speech-memory-risk').required=false;
    $('#speech-batch').setCustomValidity('');
  });
  api('/api/speech/pipelines').then(data => {
    $('#speech-comparison-content').innerHTML = data.pipelines.map((item,index) => `<p><b>${index+1}. ${escapeHtml(item.name)}</b><span>${escapeHtml(item.reference)}</span></p>`).join('');
  }).catch(() => {});
  window.cosmossSpeech = {active,sync,estimate,form};
})();
