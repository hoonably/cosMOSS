const $ = (selector) => document.querySelector(selector);
const state = {
  appSetup: null, appSetupRequest: 0, appSetupChecking: false, appSetupTimer: null,
  libraryRequest: 0, libraryLoading: false, executionDeviceId: "local",
  textSaveAttempts: new Set(),
  fileActionId: null, noteMenuId: null,
  environmentDeviceId: null, environments: {}, environmentRequests: new Set(), environmentSubmitting: false,
  saveDirectory: "", summaryProviders: [], summaryStates: {}, summaryDrafts: {}, summaryTemplate: {prompt:"", useDefault:true}, summaryDefaultPrompt: "", summaryNoteId: null, summaryTimer: null, summaryRequest: 0, summarySubmitting: false, speakerDraft: null, speakerSaving: false, voiceIdentifying: false,
  notes: [], current: null, saveTimer: null, pollTimer: null,
  pendingChanges: null, savePromise: null, noteRequest: 0, editRevision: 0, openingNote: null,
  transcriptView: "edited", maxUploadBytes: null, defaultChunkMinutes: 30, uploading: false, deletingId: null,
  elapsedAnchor: null, elapsedTimer: null,
  uploadPreparation: null, uploadChunkCount: null, cancellingId: null, restartingId: null, cancellationNoteId: null, deletionNoteId: null,
  estimateKey: null, estimateTimer: null, estimateController: null, chunkDefaultSaving: false,
  voiceMemos: [], voiceMemoSelected: null, voiceMemoRequest: 0, voiceMemoLoading: false, voiceMemoController: null,
  devices: [], deviceStatus: {}, sshProbeHost: null, sshRequest: 0, healthChecking: false,
  deviceModelRevisions: {}, modelSelectingId: null,
  gpuTimer: null, gpuAbort: null, gpuInventory: [], selectedGpu: null, gpuApproval: null, gpuWarningResolve: null, workerStates: {}, workerTimers: {},
  addingDevice: false, gpuDeviceId: null, workerPolling: new Set(),
  sshHosts: [], panelRefreshing: false, localModel: null, localModelTimer: null, localModelSubmitting: false, modelDeviceId: "local", modelLoading: false, modelError: null, modelRequest: 0, catalog: null, modelMetadata: {},
};
const colors = ["#17ad6d", "#e49a3d", "#6878df", "#db667a", "#2ca5b7", "#9c67c5"];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `요청 실패 (${response.status})`;
    let detail;
    try {
      detail = (await response.json()).detail;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = "입력 내용을 확인해 주세요.";
      else if (detail?.message) message = detail.message;
    } catch (_) {}
    throw Object.assign(new Error(message), { detail, status: response.status });
  }
  if (response.status === 204) return null;
  const payload = await response.json();
  const receivedAt = performance.now();
  for (const note of Array.isArray(payload) ? payload : [payload]) {
    if (note?.id && note.status) {
      Object.defineProperty(note, "_elapsedReceivedAt", { value: receivedAt, configurable: true });
    }
  }
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.className = "toast", 4000);
}

function renderSaveLocation(location) {
  if (!location?.path) return;
  state.saveDirectory = location.path;
  const name = location.name || location.path.split("/").filter(Boolean).pop() || location.path;
  $("#save-location-name").textContent = name;
  $("#save-location").title = `${location.path}\n파일을 저장하고, 불러올 기본 경로를 지정하세요.`;
  for (const caption of document.querySelectorAll(".stage-path-note")) {
    caption.title = `${location.path}\n${caption.textContent}`;
  }
}

async function loadSaveLocation() {
  const bridge = window.webkit?.messageHandlers?.getSaveLocation;
  if (!bridge) return;
  try { renderSaveLocation(await bridge.postMessage({})); }
  catch (error) { toast(error.message || "저장 경로를 불러오지 못했습니다.", true); }
}

$("#save-location").addEventListener("click", async () => {
  const bridge = window.webkit?.messageHandlers?.chooseSaveLocation;
  if (!bridge) return toast("Mac 앱에서 기본 경로를 선택할 수 있습니다.");
  const button = $("#save-location");
  button.disabled = true;
  try {
    await flushSave();
    const location = await bridge.postMessage({});
    if (location) {
      renderSaveLocation(location);
      await loadNotes();
      if (state.current?.source_type === "text_file" && !state.notes.some(note => note.id === state.current.id)) await openHome();
      toast("기본 경로를 변경했습니다.");
    }
  } catch (error) { toast(error.message || "저장 폴더를 변경하지 못했습니다.", true); }
  finally { button.disabled = false; }
});

function formatDate(value) {
  return new Intl.DateTimeFormat("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

async function loadAppInfo() {
  const bridge = window.webkit?.messageHandlers?.getAppInfo;
  if (!bridge) return;
  try {
    const info = await bridge.postMessage({});
    $("#app-version").textContent = `v${info.version}`;
    $("#app-update-info").classList.remove("hidden");
    $("#check-app-update").classList.toggle("hidden", !info.updatesAvailable);
    $("#check-app-update").disabled = !info.canCheckForUpdates;
  } catch (_) {}
}

$("#check-app-update").addEventListener("click", async () => {
  const bridge = window.webkit?.messageHandlers?.checkForUpdates;
  if (!bridge) return;
  try {
    await flushSave();
    await bridge.postMessage({});
    $("#developer-dialog").close();
  } catch (error) { toast(error.message || "업데이트를 확인하지 못했습니다.", true); }
});

window.prepareForAppUpdate = async () => {
  if (window.cosmossVoiceProfiles?.busy || state.voiceIdentifying) return false;
  if (state.uploading || state.uploadPreparation || $("#voice-memo-dialog").open || state.speakerDraft || state.speakerSaving || state.summarySubmitting || state.addingDevice) return false;
  try { await flushSave(); }
  catch (_) { return false; }
  return !state.pendingChanges && !state.savePromise;
};

// Keep the coffee surprise inside the same modal so focus returns to its opener.
(() => {
  const dialog = $("#developer-dialog");
  $("#open-developer-info").addEventListener("click", () => {
    loadAppInfo();
    $("#developer-info-view").classList.remove("hidden");
    $("#developer-coffee-view").classList.add("hidden");
    dialog.setAttribute("aria-labelledby", "developer-dialog-title");
    dialog.removeAttribute("aria-describedby");
    if (!dialog.open) dialog.showModal();
    $("#developer-dialog-title").focus({ preventScroll: true });
  });
  $("#offer-developer-coffee").addEventListener("click", () => {
    $("#developer-info-view").classList.add("hidden");
    $("#developer-coffee-view").classList.remove("hidden");
    dialog.setAttribute("aria-labelledby", "developer-coffee-title");
    dialog.setAttribute("aria-describedby", "developer-coffee-message");
    $("#developer-coffee-title").focus({ preventScroll: true });
  });
  for (const selector of ["#close-developer-info", "#confirm-developer-coffee"]) {
    $(selector).addEventListener("click", () => dialog.close());
  }
  dialog.addEventListener("close", () => $("#open-developer-info").focus({ preventScroll: true }));
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
})();

function clock(seconds = 0) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [ ...(h ? [h] : []), m, s ].map(part => String(part).padStart(2, "0")).join(":");
}

function stopElapsedClock() {
  clearTimeout(state.elapsedTimer);
  state.elapsedTimer = null;
  state.elapsedAnchor = null;
}

function elapsedSeconds(anchor, now = performance.now()) {
  return anchor.seconds + (anchor.running ? Math.max(0, now - anchor.at) / 1000 : 0);
}

function renderElapsedClock() {
  clearTimeout(state.elapsedTimer);
  state.elapsedTimer = null;
  const anchor = state.elapsedAnchor;
  if (!anchor) return;
  const seconds = elapsedSeconds(anchor);
  $("#progress-elapsed").textContent = `경과 ${clock(seconds)}`;
  renderProgressValues(anchor.note, seconds);
  if (anchor.running) {
    // Schedule the next displayed second, independently of HTTP polling.
    const delay = Math.max(16, Math.ceil((Math.floor(seconds) + 1 - seconds) * 1000));
    state.elapsedTimer = setTimeout(renderElapsedClock, delay);
  }
}

function syncElapsedClock(note) {
  const sample = note.inference_elapsed_seconds;
  const element = $("#progress-elapsed");
  if (sample == null || note.status === "queued") {
    stopElapsedClock();
    element.classList.add("hidden");
    element.textContent = "";
    return;
  }
  element.classList.remove("hidden");
  const previous = state.elapsedAnchor;
  if (previous?.note !== note) {
    const now = performance.now();
    const key = `${note.id}:${note.inference_started_at}`;
    const running = note.status === "processing" && !note.inference_finished_at
      && ["transcribing", "linking"].includes(note.progress_phase);
    const at = note._elapsedReceivedAt ?? now;
    const seconds = running && previous?.key === key
      ? Math.max(Number(sample), elapsedSeconds(previous, at)) : Number(sample);
    state.elapsedAnchor = {note, key, seconds, at, running};
  }
  renderElapsedClock();
}

function renderCompletionTime(note) {
  const element = $("#transcription-time");
  const known = note.status === "done" && note.processing_seconds != null;
  element.classList.toggle("hidden", !known);
  element.textContent = known
    ? `전사 소요 ${note.processing_seconds < 1 ? "1초 미만" : clock(Math.round(note.processing_seconds))}` : "";
}

function statusText(status) {
  return { queued: "대기 중", processing: "전사 중", done: "완료", error: "오류", cancelled: "취소됨" }[status] || status;
}

function transcriptionProgress(note, elapsed) {
  const phase = note.progress_phase || (note.status === "processing" ? "normalizing" : note.status);
  const waiting = ["queued", "preparing_model", "normalizing"].includes(phase);
  const stopped = ["error", "cancelled"].includes(note.status);
  const complete = note.status === "done" || (!stopped && !!note.inference_finished_at);
  const estimate = Number(note.estimated_inference_seconds);
  const hasEstimate = Number.isFinite(estimate) && estimate > 0;
  const anchor = state.elapsedAnchor;
  const seconds = elapsed ?? (anchor?.note === note ? elapsedSeconds(anchor) : note.inference_elapsed_seconds ?? 0);
  const percent = complete ? 100 : Math.max(0, Math.min(99, Math.floor((hasEstimate ? seconds / estimate : 0) * 100)));
  return {phase, waiting, stopped, complete, estimate: hasEstimate ? estimate : null,
    indeterminate: waiting || (!hasEstimate && !complete), percent};
}

function noteStatus(note) {
  const progress = transcriptionProgress(note);
  if (note.status !== "processing") return statusText(note.status);
  if (progress.complete) return "전사 완료";
  return !progress.indeterminate ? `전사 중 약 ${progress.percent}%` : "전사 중";
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function renderList() {
  const list = $("#note-list");
  const focused = document.activeElement.closest?.(".note-item");
  const focusedId = focused?.dataset.id;
  const focusedControl = document.activeElement.matches(".note-edit") ? ".note-edit" : ".note-select";
  const scrollTop = list.scrollTop;
  const query = $("#note-search").value.trim().toLowerCase();
  const notes = state.notes.filter(note => note.title.toLowerCase().includes(query));
  $("#note-count").textContent = state.notes.length;
  list.innerHTML = notes.length ? notes.map(note => {
    const selected = state.current?.id === note.id;
    const action = note.status === "done" ? "편집" : ["queued", "processing"].includes(note.status) ? "진행 보기" : "상태 보기";
    return `
    <div class="note-item ${selected ? "active" : ""}" data-id="${escapeHtml(note.id)}">
      <button class="note-select" type="button" title="${escapeHtml(note.title)} 선택" aria-label="${escapeHtml(note.title)} 선택" aria-pressed="${selected}" aria-describedby="note-selection-hint">
        <span class="note-item-icon" aria-hidden="true"><svg viewBox="0 0 24 24">${selected ? '<path d="m6 12 4 4 8-8"/>' : '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Zm0 0v6h6M8 13h8M8 17h5"/>'}</svg></span>
        <span class="note-item-copy"><b>${escapeHtml(note.title)}</b><small><span>${formatDate(note.created_at)}</span><span class="note-status ${note.status}">${note.text_save_error ? "저장 실패" : note.status === "done" ? (note.output_format || "txt").toUpperCase() : noteStatus(note)}</span></small></span>
      </button>
      <button class="note-edit" type="button" aria-label="${escapeHtml(note.title)} ${action}" aria-haspopup="dialog" aria-controls="note-dialog">
        <svg viewBox="0 0 24 24" aria-hidden="true">${note.status === "done" ? '<path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15Zm0 0 4-1"/>' : '<path d="m9 6 6 6-6 6"/>'}</svg><span>${action}</span>
      </button>
    </div>`;
  }).join("") : (query ? '<div class="empty-list">검색 결과가 없습니다</div>' : "");
  list.querySelectorAll(".note-select").forEach(button => button.addEventListener("click", () => selectNote(button.closest(".note-item").dataset.id)));
  list.querySelectorAll(".note-edit").forEach(button => button.addEventListener("click", () => openNote(button.closest(".note-item").dataset.id)));
  list.scrollTop = scrollTop;
  if (focusedId) list.querySelector(`.note-item[data-id="${CSS.escape(focusedId)}"] ${focusedControl}`)?.focus({preventScroll:true});
}

async function loadNotes() {
  const request = ++state.libraryRequest;
  state.libraryLoading = true;
  try {
    await saveLocationReady;
    const directory = state.saveDirectory;
    const [notes, documents] = await Promise.allSettled([api("/api/notes"), api(`/api/text-library?directory=${encodeURIComponent(directory)}`)]);
    if (request !== state.libraryRequest) return;
    if (notes.status === "rejected") throw notes.reason;
    // Reconnect old manual exports once; new jobs save on the backend even when
    // this window is closed. Failed saves remain visible for an explicit retry.
    for (let index = 0; index < notes.value.length; index++) {
      const note = notes.value[index];
      if (note.status !== "done" || note.text_path || note.text_save_error || state.textSaveAttempts.has(note.id)) continue;
      state.textSaveAttempts.add(note.id);
      try { notes.value[index] = await api(`/api/notes/${encodeURIComponent(note.id)}/text-file`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ directory }) }); }
      catch (error) { note.text_save_error = error.message; }
    }
    if (request !== state.libraryRequest) return;
    const imported = documents.status === "fulfilled" ? documents.value : [];
    const filePaths = new Set(imported.map(note => note.text_path));
    const indexed = new Map(imported.map(file => [file.text_path,file]));
    const seen = new Set();
    const visible = notes.value.filter(note => {
      if (note.status !== 'done' || !note.text_path) return true;
      if (documents.status === 'fulfilled' && !filePaths.has(note.text_path)) return false;
      const file = indexed.get(note.text_path);
      if (file && 'managed_note_id' in file && file.managed_note_id !== note.id) return false;
      if (seen.has(note.text_path)) return false;
      seen.add(note.text_path);
      if (file) Object.assign(note,{file_mtime_ns:file.file_mtime_ns,file_size:file.file_size,file_version:file.file_version});
      return true;
    });
    const managed = new Set(visible.map(note => note.text_path).filter(Boolean));
    state.notes = [...visible, ...imported.filter(note => !managed.has(note.text_path))].sort((a, b) => b.created_at.localeCompare(a.created_at));
    $("#text-library-status").textContent = documents.status === "rejected" ? documents.reason.message : "";
    $("#text-library-status").classList.toggle("hidden", documents.status !== "rejected");
    renderList();
    if (state.noteMenuId && !state.notes.some(n=>n.id===state.noteMenuId)) closeNoteMenu();
    const current = state.current;
    const saved = notes.value.find(note => note.id === current?.id);
    if (saved && (saved.text_path !== current.text_path || saved.text_save_error !== current.text_save_error)) {
      state.current = { ...current, text_path: saved.text_path, text_hash: saved.text_hash, text_save_error: saved.text_save_error };
      renderTextFileActions();
    }
    const latest = imported.find(note => note.id === current?.id);
    if (latest && current?.source_type === "text_file" && latest.file_mtime_ns !== current.file_mtime_ns && !state.pendingChanges && !state.savePromise && !state.openingNote && !state.speakerDraft && document.activeElement !== $("#text-document-body")) openNote(current.id, true);
    if (!state.pollTimer && state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  } catch (error) { toast(error.message, true); }
  finally { if (request === state.libraryRequest) state.libraryLoading = false; }
}

function closeNoteMenu(restoreFocus=false) {
  const id=state.noteMenuId;
  state.noteMenuId=null;
  $('#note-context-menu').classList.add('hidden');
  if (restoreFocus && id) document.querySelector(`.note-item[data-id="${CSS.escape(id)}"] .note-select`)?.focus({preventScroll:true});
}

function openNoteMenu(button,x,y) {
  const note=state.notes.find(n=>n.id===button.dataset.id);
  if (!note) return;
  state.noteMenuId=note.id;
  const menu=$('#note-context-menu'), format=(note.output_format||'txt').toLowerCase();
  const busy=note.status!=='done' || !note.text_path || Boolean(state.fileActionId);
  $('#note-context-title').textContent=note.title;
  for (const item of menu.querySelectorAll('[data-convert-format]')) {
    item.classList.toggle('hidden',item.dataset.convertFormat===format);
    item.disabled=busy;
  }
  $('#trash-note-file').disabled=busy;
  menu.classList.remove('hidden');
  menu.style.left=`${Math.max(8,Math.min(x,innerWidth-menu.offsetWidth-8))}px`;
  menu.style.top=`${Math.max(8,Math.min(y,innerHeight-menu.offsetHeight-8))}px`;
  menu.querySelector('button:not(.hidden):not(:disabled)')?.focus({preventScroll:true});
}

$('#note-list').addEventListener('contextmenu',event=>{
  event.preventDefault();
  const button=event.target.closest('.note-item');
  if (button) openNoteMenu(button,event.clientX,event.clientY);
  else closeNoteMenu();
});
$('#note-list').addEventListener('keydown',event=>{
  if (!(event.key==='ContextMenu' || (event.shiftKey && event.key==='F10'))) return;
  const button=event.target.closest('.note-item');
  if (!button) return;
  event.preventDefault();
  const rect=button.getBoundingClientRect();
  openNoteMenu(button,rect.x+rect.width/2,rect.y+rect.height/2);
});
$('#note-context-menu').addEventListener('keydown',event=>{
  const items=[...event.currentTarget.querySelectorAll('button:not(.hidden):not(:disabled)')];
  if (event.key==='Escape') {event.preventDefault();closeNoteMenu(true);return;}
  if (event.key==='Tab') {closeNoteMenu();return;}
  if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key) || !items.length) return;
  event.preventDefault();
  const index=items.indexOf(document.activeElement);
  items[event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
});
document.addEventListener('pointerdown',event=>{
  if (!event.target.closest('#note-context-menu')) closeNoteMenu();
});
document.addEventListener('scroll',event=>{
  if (!event.target.closest?.('#note-context-menu')) closeNoteMenu();
},true);
window.addEventListener('resize',()=>closeNoteMenu());

async function actOnNoteFile(action,outputFormat) {
  const id=state.noteMenuId;
  let note=state.notes.find(n=>n.id===id);
  closeNoteMenu();
  if (!note || state.fileActionId) return;
  state.fileActionId=id;
  try {
    if (state.current?.id===id && (state.pendingChanges || state.savePromise)) {
      await flushSave();await loadNotes();note=state.notes.find(n=>n.id===id);
      if (!note) throw Error('파일을 찾을 수 없습니다. 목록을 다시 확인해 주세요.');
    }
    const version={text_hash:note.text_hash,file_version:note.file_version,file_size:note.file_size};
    await api(`/api/notes/${encodeURIComponent(id)}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...version,...(outputFormat?{output_format:outputFormat}:{})})});
    if (action==='trash' && state.current?.id===id) {
      state.current=null;clearTimeout(state.saveTimer);
      $('#media-player').pause();$('#media-player').removeAttribute('src');
      $('#workspace').classList.add('hidden');$('#note-dialog').close();selectSummaryNote(null);
    }
    await loadNotes();
    toast(action==='trash'?'파일을 휴지통으로 옮겼습니다.':`${outputFormat.toUpperCase()} 변환본을 저장했습니다.`);
  } catch(error) {toast(error.message,true);}
  finally {state.fileActionId=null;}
}
document.querySelectorAll('[data-convert-format]').forEach(button=>button.addEventListener('click',()=>actOnNoteFile('convert',button.dataset.convertFormat)));
$('#trash-note-file').addEventListener('click',()=>actOnNoteFile('trash'));

function selectNote(id) {
  return loadNote(id);
}

function openNote(id, quiet = false) {
  return loadNote(id, {openEditor: !quiet, quiet});
}

async function loadNote(id, {openEditor = false, quiet = false} = {}) {
  if (state.deletingId) return;
  const request = ++state.noteRequest;
  state.openingNote = request;
  renderSummary();
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  try {
    await flushSave();
    if (request !== state.noteRequest) return;
    let note;
    let revision;
    do {
      revision = state.editRevision;
      note = await api(`/api/notes/${encodeURIComponent(id)}`);
      await flushSave();
      if (request !== state.noteRequest) return;
    } while (state.current?.id === id && state.editRevision !== revision);
    if (state.current?.id !== id) {
      $("#media-player").pause();
      state.transcriptView = "edited";
      $("#transcript-search").value = "";
    }
    state.current = note;
    $("#workspace").classList.remove("hidden");
    const dialog = $("#note-dialog");
    const opening = openEditor && !dialog.open;
    if (opening) {
      $("#note-close-error").classList.add("hidden");
      dialog.showModal();
    }
    renderWorkspace();
    if (opening) dialog.focus({ preventScroll: true });
    selectSummaryNote(note);
    renderList();
    if (["queued", "processing"].includes(note.status) || state.notes.some(item => ["queued", "processing"].includes(item.status))) schedulePoll();
  } catch (error) {
    if (!quiet) toast(error.message, true);
    if (["queued", "processing"].includes(state.current?.status) || state.notes.some(item => ["queued", "processing"].includes(item.status))) schedulePoll();
  } finally {
    if (state.openingNote === request) {
      state.openingNote = null;
      renderSummary();
    }
  }
}

let closingNote = false;
async function closeNoteWindow() {
  if (closingNote || state.deletingId || state.speakerSaving) return;
  closingNote = true;
  $("#close-note").disabled = true;
  $("#note-close-error").classList.add("hidden");
  try {
    await flushSave();
    $("#media-player").pause();
    $("#note-dialog").close();
    const selected = document.querySelector(`.note-item[data-id="${CSS.escape(state.current?.id || "")}"] .note-edit`);
    (selected || $("#note-search")).focus({ preventScroll: true });
  } catch (_) {
    $("#note-close-error").textContent = "변경 내용을 저장하지 못했습니다. 저장 상태를 확인한 뒤 다시 닫아 주세요.";
    $("#note-close-error").classList.remove("hidden");
  } finally {
    closingNote = false;
    $("#close-note").disabled = false;
  }
}
$("#close-note").addEventListener("click", closeNoteWindow);
$("#note-dialog").addEventListener("cancel", event => {
  event.preventDefault();
  closeNoteWindow();
});

function renderTranscriptionControls(note) {
  const button = $("#cancel-transcription");
  const cancelling = state.cancellingId === note.id;
  const restarting = state.restartingId === note.id;
  const busy = Boolean(state.cancellingId || state.restartingId);
  button.classList.toggle("hidden", !["queued", "processing"].includes(note.status) || !!note.inference_finished_at);
  button.disabled = busy;
  button.textContent = cancelling ? "취소 중…" : "전사 취소";
  const restart = $("#restart-transcription");
  restart.classList.toggle("hidden", !["cancelled", "error"].includes(note.status));
  restart.disabled = busy;
  restart.textContent = restarting ? "다시 시작 중…" : "전사 다시 시작";
  $("#note-title").disabled = cancelling || restarting;
  $("#delete-note").disabled = busy || ["queued", "processing"].includes(note.status);
}

function renderWorkspace() {
  const note = state.current;
  const file = note.source_type === "text_file";
  $("#note-title").readOnly = file || note.status === "done";
  $("#note-title").title = note.title;
  $("#delete-note").classList.toggle("hidden", file || note.status === "done");
  $("#note-title").value = state.pendingChanges?.id === note.id
    ? state.pendingChanges.changes.title ?? note.title : note.title;
  const chunkLength = note.chunk_seconds ? ` · 청크 ${Number.isInteger(note.chunk_seconds / 60) ? `${note.chunk_seconds / 60}분` : clock(note.chunk_seconds)}` : "";
  const chunkMeta = `${chunkLength}${note.chunk_count > 1 ? ` · ${note.chunk_count}개 구간으로 나누어 전사` : ""}`;
  $("#note-meta").textContent = file || note.status === "done" ? `${formatDate(note.updated_at || note.created_at)} · ${note.text_path?.split('/').pop() || note.title + '.' + (note.output_format || 'txt')}` : `${formatDate(note.created_at)} · ${note.original_filename}${chunkMeta} · ${note.device_name || "이 Mac"}`;
  renderTextFileActions();
  const pending = ["queued", "processing"].includes(note.status);
  $("#processing-card").classList.toggle("hidden", !pending && !["error", "cancelled"].includes(note.status));
  $("#processing-card").classList.toggle("failed", note.status === "error");
  $("#processing-card").classList.toggle("cancelled", note.status === "cancelled");
  $("#editor").classList.toggle("hidden", note.status !== "done");
  renderTranscriptionControls(note);
  renderProgress(note);
  if (pending) {
    const phase = transcriptionProgress(note).phase;
    $("#processing-title").textContent = {
      queued: "전사 순서를 기다리고 있어요",
      normalizing: "모델 전사를 준비하고 있어요",
      preparing_model: "전사 모델을 준비하고 있어요",
      transcribing: "음성을 전사하고 있어요",
      linking: "음성을 전사하고 있어요",
      finalizing: "모델 전사가 끝났어요",
      identifying_speakers: "모델 전사가 끝났어요",
    }[phase] || "음성을 읽고 있어요";
    if (phase === 'transcribing') {
      const modelPhase = note.model_progress?.pipeline_phase;
      if (modelPhase === 'aligning') $("#processing-title").textContent = '단어의 시간을 맞추고 있어요';
      if (modelPhase === 'diarizing') $("#processing-title").textContent = 'Nemotron이 목소리를 구분하고 있어요';
    }
    $("#processing-copy").textContent = phase === "queued" ? "앞선 작업이 끝나면 자동으로 시작됩니다."
      : ["finalizing", "identifying_speakers"].includes(phase) ? "전사 결과를 불러오고 있습니다."
      : "모델이 녹음을 읽고 전사문을 생성합니다.";
    if (note.model_progress?.pipeline_phase === 'aligning') $("#processing-copy").textContent = '전사된 단어를 원본 음성 시간에 맞춥니다.';
    if (note.model_progress?.pipeline_phase === 'diarizing') $("#processing-copy").textContent = '원본 녹음에서 서로 다른 목소리를 찾습니다.';
  } else if (note.status === "error") {
    $("#processing-title").textContent = "전사하지 못했습니다";
    $("#processing-copy").textContent = note.error || "처리 장치와 녹음 파일을 확인한 뒤 ‘전사 다시 시작’을 눌러 주세요.";
  } else if (note.status === "cancelled") {
    $("#processing-title").textContent = "전사를 취소했습니다";
    $("#processing-copy").textContent = "녹음 파일은 보관되어 있습니다. ‘전사 다시 시작’을 누르면 기존 파일로 처음부터 다시 처리합니다.";
  } else if (note.status === "done") {
    renderEditor();
  }
}

function renderProgressValues(note, elapsed) {
  const progress = transcriptionProgress(note, elapsed);
  const {phase, waiting, stopped, complete, indeterminate, percent, estimate} = progress;
  $("#processing-card").classList.toggle("waiting", waiting);
  $("#processing-progress").classList.toggle("hidden", stopped && !note.inference_started_at);
  $("#progress-stage").textContent = complete ? "모델 전사 완료" : "예상 진행률";
  $("#progress-percent").textContent = waiting ? (phase === "queued" ? "대기 중" : "준비 중")
    : indeterminate ? "전사 중" : `${percent}%`;
  const track = $("#progress-track");
  track.classList.toggle("indeterminate", indeterminate && !stopped);
  if (indeterminate && !stopped) {
    track.removeAttribute("aria-valuenow");
    track.setAttribute("aria-valuetext", waiting ? "모델 전사 시작을 기다리는 중" : "모델 전사 중");
  } else {
    track.setAttribute("aria-valuenow", String(percent));
    track.setAttribute("aria-valuetext", complete ? "모델 전사 완료" : `예상 ${percent}%${stopped ? ", 작업 중단" : " 진행"}`);
  }
  $("#progress-fill").style.width = indeterminate && !stopped ? "" : `${percent}%`;
  $("#progress-estimate").textContent = estimate ? `예상 소요 ${clock(estimate)}` : "";
  $("#progress-detail").textContent = waiting ? "모델 전사가 시작되면 진행률을 표시합니다."
    : stopped ? "전사가 중단되었습니다."
    : complete ? "모델의 전사 응답을 모두 받았습니다."
    : percent >= 99 ? "모델의 전사 완료를 기다리고 있어요."
    : "예상 소요 시간 기준으로 표시하며, 모델 응답이 끝나면 100%가 됩니다.";
}

function renderProgress(note) {
  syncElapsedClock(note);
  renderProgressValues(note);
}

function displayedSegments() {
  const note = state.current;
  const segments = (state.transcriptView === "raw" ? note?.raw_segments : note?.segments) || [];
  return state.transcriptView === "raw" ? segments.map(segment => ({ ...segment, speaker: note.speaker_merges?.[segment.speaker] || segment.speaker })) : segments;
}

function fitText(element) {
  element.style.height = "auto";
  element.style.height = `${element.scrollHeight}px`;
}

function renderEditor() {
  const note = state.current;
  const file = note.source_type === "text_file";
  $("#text-document-editor").classList.toggle("hidden", !file);
  for (const selector of [".player-card", ".transcript-views", ".editor-toolbar", "#segments"]) $(selector).classList.toggle("hidden", file);
  const format = (note.output_format || "txt").toUpperCase();
  $("#text-document-format").textContent = format;
  $("#text-document-body").setAttribute("aria-label", `${format} 내용 편집`);
  $(".player-card").classList.add("hidden");
  const player = $("#media-player");
  player.pause();
  player.removeAttribute("src");
  if (file) {
    $('#auxiliary-transcript').classList.add('hidden');
    $('#speech-result').classList.add('hidden');
    const media = $("#media-player");
    media.pause();
    media.removeAttribute("src");
    for (const selector of ["#speaker-panel", "#chunk-notice", "#transcript-empty"]) $(selector).classList.add("hidden");
    $("#text-document-body").value = note.text_body || "";
    updateSaveState();
    return;
  }
  const segments = displayedSegments();
  const editable = state.transcriptView === "edited";
  const speakers = [...new Set(segments.map(segment => segment.speaker))];
  const allSpeakers = [...new Set([...(note.raw_segments || []), ...note.segments].map(segment => note.speaker_merges?.[segment.speaker] || segment.speaker))];
  const speakerIndex = Object.fromEntries(allSpeakers.map((speaker, index) => [speaker, index]));
  $("#duration-label").textContent = note.duration ? `녹음 ${clock(note.duration)}` : "";
  renderCompletionTime(note);
  $("#editing-hint").textContent = editable ? "본문을 눌러 수정하고, 화자 설정에서 이름을 지정하세요." : "처음 전사한 본문입니다. 원본은 그대로 보존됩니다.";
  document.querySelectorAll("[data-transcript-view]").forEach(button => {
    const active = button.dataset.transcriptView === state.transcriptView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $("#chunk-notice").classList.toggle("hidden", note.chunk_count <= 1);
  $("#chunk-notice").textContent = `긴 녹음을 ${note.chunk_count}개 구간으로 나누어 전사했습니다. 경계에서 확인되지 않은 화자는 여러 이름으로 표시될 수 있습니다. 화자 이름을 수정해 정리할 수 있습니다.`;
  const speechResult = note.pipeline_result;
  $("#speech-result").classList.toggle('hidden', !speechResult?.pipeline);
  if (speechResult?.pipeline) {
    $("#chunk-notice").classList.add('hidden');
    const peak = speechResult.peak_process_mib ? ` · Peak ${(speechResult.peak_process_mib / 1024).toFixed(1)} GiB` : '';
    const uncertain = note.segments.filter(segment => segment.speaker === '미확인' || segment.speaker_uncertain).length;
    $("#speech-result").textContent = `${{'moss-sglang':'MOSS · SGLang Omni · 전체','qwen-transformers':'Qwen · Transformers · 3분 + 정렬','moss-qwen-sequential':'MOSS + Qwen · 순차 실행','qwen-batch':'Qwen3-ASR'}[speechResult.pipeline] || 'MOSS'} + Nemotron · 모델 처리 ${clock(speechResult.inference_seconds)}${peak} · ${['moss-sglang','moss-qwen-sequential'].includes(speechResult.pipeline) ? 'MOSS 전체 1청크' : `${speechResult.chunk_count}청크 / 동시 ${speechResult.batch}개`}${uncertain ? ` · 화자 미확인 ${uncertain}개 발화` : ''}`;
  }
  const auxiliary = $('#auxiliary-transcript');
  auxiliary.classList.toggle('hidden', !speechResult?.has_auxiliary_transcript);
  if (auxiliary.dataset.noteId !== note.id) {
    auxiliary.open = false;
    auxiliary.dataset.noteId = note.id;
    $('#auxiliary-transcript-text').textContent = '';
  }
  $('#download-auxiliary-transcript').href = `/api/notes/${encodeURIComponent(note.id)}/auxiliary-transcript?download=true`;
  $("#speaker-panel").classList.toggle("hidden", !speakers.length);
  $("#speaker-panel").innerHTML = speakers.map(speaker => `
    <button type="button" class="speaker-chip" data-open-speaker="${escapeHtml(speaker)}" style="--speaker-color:${colors[speakerIndex[speaker] % colors.length]}">
      <i></i><small>${escapeHtml(speaker)}</small><span>${escapeHtml(note.speaker_names[speaker] || speaker)}</span>
    </button>`).join("");
  $("#segments").innerHTML = segments.map((segment, index) => `
    <article class="segment" data-index="${index}" data-start="${segment.start}" data-end="${segment.end}">
      <button class="time-button" data-seek="${segment.start}" aria-label="${clock(segment.start)}부터 재생">${clock(segment.start)}</button>
      <button type="button" class="speaker-label" data-open-speaker="${escapeHtml(segment.speaker)}"><i class="speaker-dot" style="--speaker-color:${colors[speakerIndex[segment.speaker] % colors.length]}"></i><span>${escapeHtml(note.speaker_names[segment.speaker] || segment.speaker)}</span></button>
      <div class="segment-text-wrap"><textarea class="segment-text" rows="1" ${editable ? "" : "readonly"} aria-label="${clock(segment.start)} ${escapeHtml(note.speaker_names[segment.speaker] || segment.speaker)} ${editable ? "전사 내용 편집" : "전사 원본"}">${escapeHtml(segment.text)}</textarea></div>
    </article>`).join("");
  bindEditorEvents();
  filterTranscript();
  document.querySelectorAll(".segment-text").forEach(fitText);
  updateSaveState();
}

function bindEditorEvents() {
  document.querySelectorAll("[data-seek]").forEach(button => button.addEventListener("click", async () => {
    const media = $("#media-player");
    media.currentTime = Number(button.dataset.seek);
    try { await media.play(); } catch (_) { toast("녹음 파일을 재생할 수 없습니다.", true); }
  }));
  document.querySelectorAll(".segment-text:not([readonly])").forEach(element => element.addEventListener("input", () => {
    state.current.segments[Number(element.closest(".segment").dataset.index)].text = element.value;
    fitText(element);
    queueSave({ segments: state.current.segments });
    filterTranscript();
  }));
  document.querySelectorAll("[data-open-speaker]").forEach(button => button.addEventListener("click", () => openSpeakerDialog(button.dataset.openSpeaker)));
}

function updateSaveState(failed = false) {
  const pending = Boolean(state.pendingChanges || state.savePromise);
  $("#save-state").textContent = failed ? "저장 실패" : pending ? "저장 중…" : "저장됨";
  $("#save-state").className = `save-state${failed ? " failed" : pending ? " saving" : ""}`;
  $("#retry-save").classList.toggle("hidden", !failed);
  $("#text-document-save-state").textContent = $("#save-state").textContent;
  $("#text-document-save-state").className = $("#save-state").className;
  $("#retry-text-save").classList.toggle("hidden", !failed);
}

function queueSave(changes) {
  ++state.editRevision;
  state.pendingChanges = {
    id: state.current.id,
    changes: { ...(state.pendingChanges?.changes || {}), ...structuredClone(changes) },
  };
  updateSaveState();
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => flushSave().catch(() => {}), 650);
}

async function flushSave() {
  clearTimeout(state.saveTimer);
  if (state.savePromise) {
    await state.savePromise;
    return flushSave();
  }
  const pending = state.pendingChanges;
  if (!pending) return;
  state.pendingChanges = null;
  const changes = pending.changes;
  if ("title" in changes) {
    changes.title = changes.title.trim();
    if (!changes.title) {
      if (state.current?.id === pending.id) $("#note-title").value = state.current.title;
      delete changes.title;
    }
  }
  if (!Object.keys(changes).length) return updateSaveState();
  const payload = { ...changes };
  if (state.current?.id === pending.id && state.current.source_type === "text_file") payload.text_hash = state.current.text_hash;
  state.savePromise = api(`/api/notes/${encodeURIComponent(pending.id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  }).then(async note => {
    if (state.current?.id === pending.id) state.current = state.pendingChanges ? { ...state.current, text_path: note.text_path, text_hash: note.text_hash } : note;
    renderTextFileActions();
    renderSummary();
    await loadNotes();
  }).catch(error => {
    state.pendingChanges = {
      id: pending.id,
      changes: { ...changes, ...(state.pendingChanges?.changes || {}) },
    };
    updateSaveState(true);
    toast(`저장하지 못했습니다. ${error.message}`, true);
    throw error;
  }).finally(() => { state.savePromise = null; });
  await state.savePromise;
  if (state.pendingChanges) return flushSave();
  updateSaveState();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    state.pollTimer = null;
    const id = state.current?.id;
    const previous = state.current?.status;
    if (id && state.cancellingId !== id && state.restartingId !== id && !state.openingNote && ["queued", "processing"].includes(previous)) await openNote(id, true);
    await loadNotes();
    if (id && state.current?.id === id && previous !== "done" && state.current.status === "done") toast("전사가 완료되었습니다.");
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }, 1000);
}

async function openHome() {
  if (state.deletingId) return;
  const request = ++state.noteRequest;
  state.openingNote = request;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  try {
    await flushSave();
    if (request !== state.noteRequest) return;
    state.current = null;
    stopElapsedClock();
    state.transcriptView = "edited";
    const media = $("#media-player");
    media.pause();
    media.removeAttribute("src");
    media.load();
    $("#transcript-search").value = "";
    $("#workspace").classList.add("hidden");
    $("#note-dialog").close();
    selectSummaryNote(null);
    renderList();
    window.scrollTo({ top: 0, left: 0 });
  } catch (_) {
    // flushSave keeps the draft and reports the error before navigation.
  } finally {
    if (state.openingNote === request) state.openingNote = null;
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }
}

$("#go-home").addEventListener("click", event => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openHome();
});

function openUpload(deviceId) {
  if (state.deletingId || $("#upload-dialog").open) return;
  state.executionDeviceId = typeof deviceId === "string" ? deviceId : "local";
  if (!executionDevice()) return toast("장치 목록에서 처리 장치를 다시 선택해 주세요.", true);
  state.uploadChunkCount = null;
  renderUploadChunks();
  try { $("#output-format").value = localStorage.getItem("cosmoss.outputFormat") || "txt"; } catch (_) {}
  if (!$("#output-format").value) $("#output-format").value = "txt";
  $("#upload-dialog").showModal();
  syncExecutionGpu();
  loadDevices().then(syncExecutionGpu).catch(error => toast(error.message, true));
}
$("#close-upload").addEventListener("click", () => $("#upload-dialog").close());
$("#upload-dialog").addEventListener("cancel", event => { if (state.uploading) event.preventDefault(); });
$("#upload-dialog").addEventListener("close", () => {
  clearUploadEstimate();
  closeGpuWarning(false);
  if ($("#voice-memo-dialog").open) $("#voice-memo-dialog").close();
  stopGpuMonitor();
  state.selectedGpu = null;
  state.gpuApproval = null;
  state.gpuDeviceId = null;
  $("#execution-gpu").classList.add("hidden");
  if (state.uploading) return;
  cancelFilePreparation();
  $("#upload-form").reset();
  resetDropZone();
});
$("#upload-dialog").addEventListener("click", event => {
  if (event.target !== event.currentTarget || state.uploading) return;
  const rect = event.currentTarget.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.currentTarget.close();
});
$("#note-search").addEventListener("input", renderList);
$("#note-title").addEventListener("input", event => { if (state.current) queueSave({ title: event.target.value }); });
$("#retry-save").addEventListener("click", () => flushSave().catch(() => {}));
$("#retry-text-save").addEventListener("click", () => flushSave().catch(() => {}));
$("#text-document-body").addEventListener("input", event => {
  if (state.current?.source_type !== "text_file") return;
  state.current.text_body = event.target.value;
  queueSave({ text_body: event.target.value });
});
document.querySelectorAll("[data-transcript-view]").forEach(button => button.addEventListener("click", async () => {
  if (!state.current || button.dataset.transcriptView === state.transcriptView) return;
  const id = state.current.id;
  try {
    await flushSave();
    if (state.current?.id !== id) return;
    state.transcriptView = button.dataset.transcriptView;
    renderEditor();
  } catch (_) {}
}));

const dropZone = $("#drop-zone");
const fileInput = $("#audio-file");
$("#choose-voice-memo").addEventListener("click", () => {
  if (state.uploading) return;
  state.voiceMemos = [];
  state.voiceMemoSelected = null;
  $("#voice-memo-access").classList.add('hidden');
  $("#voice-memo-privacy").classList.toggle('hidden', !window.webkit?.messageHandlers?.openVoiceMemoPrivacy);
  $("#voice-memo-search").value = "";
  $("#open-voice-memos").classList.toggle("hidden", !window.webkit?.messageHandlers?.openVoiceMemos);
  $("#voice-memo-dialog").showModal();
  $("#voice-memo-title").focus({ preventScroll: true });
  loadVoiceMemos();
});
function renderVoiceMemos() {
  const search = $("#voice-memo-search").value.trim().toLocaleLowerCase();
  const records = state.voiceMemos.filter(item => item.title.toLocaleLowerCase().includes(search));
  $("#voice-memo-list").innerHTML = records.map(item => {
    const date = item.created_at ? formatDate(item.created_at) : "";
    const detail = [date, item.duration ? clock(item.duration) : "", item.size ? `${(item.size / 1024 / 1024).toFixed(1)} MB` : "", item.available ? "" : "이 Mac에 다운로드 필요"].filter(Boolean).join(" · ");
    return `<label class="voice-memo-item"><input type="radio" name="voice-memo" value="${escapeHtml(item.id)}" ${item.id === state.voiceMemoSelected ? "checked" : ""} ${item.available ? "" : "disabled"}><span class="voice-memo-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 10v4m4-8v12m4-16v20m4-16v12m4-8v4"/></svg></span><span class="voice-memo-info"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(detail)}</small></span><span class="voice-memo-check" aria-hidden="true"></span></label>`;
  }).join("") || (state.voiceMemoLoading || !$("#voice-memo-access").classList.contains('hidden') ? "" : `<p class="voice-memo-empty">${search ? "검색 결과가 없습니다." : "불러올 녹음이 없습니다."}</p>`);
  $("#import-voice-memo").disabled = state.voiceMemoLoading || !records.some(item => item.id === state.voiceMemoSelected && item.available);
  $("#refresh-voice-memos").disabled = state.voiceMemoLoading;
}
async function loadVoiceMemos() {
  state.voiceMemoController?.abort();
  const request = ++state.voiceMemoRequest;
  const controller = new AbortController();
  state.voiceMemoController = controller;
  state.voiceMemoLoading = true;
  $("#voice-memo-status").textContent = "녹음을 불러오고 있습니다…";
  renderVoiceMemos();
  try {
    // Attribute the explicit library access to the foreground app before the
    // Python backend scans it. macOS controls whether a denied app is listed.
    try { await window.webkit?.messageHandlers?.requestVoiceMemosAccess?.postMessage({}); } catch (_) {}
    if (request !== state.voiceMemoRequest || !$('#voice-memo-dialog').open) return;
    const result = await api('/api/voice-memos', { signal: controller.signal });
    if (request !== state.voiceMemoRequest || !$("#voice-memo-dialog").open) return;
    state.voiceMemos = result.recordings;
    $("#voice-memo-access").classList.toggle('hidden', !result.permission_required);
    if (!state.voiceMemos.some(item => item.id === state.voiceMemoSelected && item.available)) state.voiceMemoSelected = null;
    $("#voice-memo-status").textContent = result.permission_required
      ? "macOS에서 음성 메모 데이터 접근을 차단했습니다."
      : result.metadata_unavailable ? "녹음 제목을 읽지 못해 파일명으로 표시합니다."
      : state.voiceMemos.length ? `녹음 ${state.voiceMemos.length}개 · 가져올 녹음을 선택하세요.`
      : "음성 메모 앱에서 녹음을 이 Mac에 다운로드한 뒤 새로고침해 주세요.";
  } catch (error) {
    if (request !== state.voiceMemoRequest || error.name === 'AbortError') return;
    $("#voice-memo-status").textContent = error.message;
  } finally {
    if (request === state.voiceMemoRequest) { state.voiceMemoLoading = false; renderVoiceMemos(); }
  }
}
$("#voice-memo-search").addEventListener('input', renderVoiceMemos);
$("#voice-memo-list").addEventListener('change', event => { state.voiceMemoSelected = event.target.value; $("#import-voice-memo").disabled = false; });
$("#refresh-voice-memos").addEventListener('click', loadVoiceMemos);
$("#close-voice-memo").addEventListener('click', () => $("#voice-memo-dialog").close());
$("#voice-memo-dialog").addEventListener('close', () => { state.voiceMemoRequest++; state.voiceMemoController?.abort(); state.voiceMemoLoading = false; });
$("#open-voice-memos").addEventListener('click', async () => {
  try { await window.webkit?.messageHandlers?.openVoiceMemos?.postMessage({}); }
  catch (error) { toast(error.message || "음성 메모를 열지 못했습니다.", true); }
});
$("#voice-memo-privacy").addEventListener('click', async () => {
  try {
    const opened = await window.webkit?.messageHandlers?.openVoiceMemoPrivacy?.postMessage({});
    if (!opened) toast('시스템 설정에서 개인정보 보호 및 보안 → 전체 디스크 접근 권한을 열어 주세요.', true);
  }
  catch (error) { toast(error.message || '권한 설정을 열지 못했습니다.', true); }
});
window.addEventListener('cosmoss-voice-memo-permission-return', () => {
  if ($('#voice-memo-dialog').open && !state.voiceMemoLoading) loadVoiceMemos();
});
$("#import-voice-memo").addEventListener('click', () => {
  const item = state.voiceMemos.find(item => item.id === state.voiceMemoSelected && item.available);
  if (!item || state.voiceMemoLoading) return;
  fileInput.value = "";
  $("#voice-memo-dialog").close();
  showFile({ name: `${item.title}.m4a`, size: item.size }, item);
});
function resetDropZone() {
  fileInput.required = true;
  $("#drop-title").textContent = "파일 선택 또는 드래그";
  const limit = state.maxUploadBytes ? ` · 최대 ${+(state.maxUploadBytes / 1024 ** 3).toFixed(2)} GB` : "";
  $("#drop-copy").textContent = "MP3, WAV, M4A, MP4 등";
  $("#upload-limit").textContent = `긴 녹음은 자동 분할${limit}`;
  renderFilePreparation();
}
function validateFile(file) {
  if (!file) throw new Error("전사할 음성 또는 영상 파일을 선택해 주세요.");
  if (state.maxUploadBytes && file.size > state.maxUploadBytes) throw new Error(`파일이 업로드 제한(${+(state.maxUploadBytes / 1024 ** 3).toFixed(2)} GB)을 초과합니다.`);
  if (file.size === 0) throw new Error("빈 파일은 전사할 수 없습니다.");
}
function releaseFileMetadata(preparation) {
  if (preparation?.audio) {
    preparation.audio.removeAttribute("src");
    preparation.audio.load();
    preparation.audio = null;
  }
  if (preparation?.objectUrl) {
    URL.revokeObjectURL(preparation.objectUrl);
    preparation.objectUrl = null;
  }
}
function deletePreparedFile(id) {
  if (id) return api(`/api/uploads/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
}
function cancelFilePreparation() {
  const preparation = state.uploadPreparation;
  state.uploadPreparation = null;
  if (!preparation) return;
  clearTimeout(preparation.timer);
  preparation.controller.abort();
  releaseFileMetadata(preparation);
  deletePreparedFile(preparation.id);
}
function uploadChunkDefaultMinutes() {
  const device = executionDevice();
  const value = device?.options?.chunk_minutes ?? state.modelMetadata[device?.id]?.options?.chunk_minutes ?? 30;
  return Number.isFinite(value) && value >= 1 && value <= 90 ? value : 30;
}
function uploadChunkPlan() {
  const duration = state.uploadPreparation?.duration;
  if (!Number.isFinite(duration) || duration <= 0) return null;
  const count = state.uploadChunkCount === null ? Math.ceil(duration / (uploadChunkDefaultMinutes() * 60)) : Number(state.uploadChunkCount);
  if (!Number.isInteger(count) || count < 1 || count > 1000) return null;
  return { count, seconds: duration / count };
}
function renderUploadChunks() {
  window.cosmossSpeech?.sync();
  const fastSpeech = !!window.cosmossSpeech?.active();
  const input = $("#upload-chunk-count");
  const duration = state.uploadPreparation?.duration;
  const hasDuration = Number.isFinite(duration) && duration > 0;
  const plan = uploadChunkPlan();
  const value = state.uploadChunkCount ?? (hasDuration ? String(Math.ceil(duration / (uploadChunkDefaultMinutes() * 60))) : "");
  if (input.value !== value) input.value = value;
  const needsGpu = executionDevice()?.backend === 'cuda' && !state.selectedGpu;
  input.disabled = state.uploading || !hasDuration || needsGpu || fastSpeech;
  input.setCustomValidity("");
  if (plan && !fastSpeech) {
    if (plan.seconds > 90 * 60) input.setCustomValidity("구간당 90분 이하가 되도록 청크 분할 개수를 늘려 주세요.");
    else if (plan.count > Math.max(1, Math.floor(duration))) input.setCustomValidity("구간당 1초 이상이 되도록 청크 분할 개수를 줄여 주세요.");
  }
  const total = plan ? Math.floor(plan.seconds) : 0;
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  $("#upload-chunk-preview").textContent = !plan ? "—" : total === 0 ? "1초 미만" :
    [hours ? `${hours}시간` : "", minutes ? `${minutes}분` : "", seconds ? `${seconds}초` : ""].filter(Boolean).join(" ");
  $("#increase-upload-chunks").disabled = input.disabled || input.valueAsNumber >= 1000;
  $("#decrease-upload-chunks").disabled = input.disabled || input.valueAsNumber <= 1;
  const needsInput = executionDevice()?.backend === 'cuda' && (needsGpu || !hasDuration);
  const help = $("#upload-chunk-default");
  help.textContent = needsInput ? '파일을 올리고 GPU 번호를 선택하면 청크 분할 개수에 따른 예상값을 확인할 수 있습니다.' : `이 장치 기본: ${uploadChunkDefaultMinutes()}분 이하 · 같은 길이로 나눕니다.`;
  help.classList.toggle('needs-input', needsInput);
  scheduleUploadEstimate(plan);
}

function clearUploadEstimate() {
  clearTimeout(state.estimateTimer);
  state.estimateController?.abort();
  state.estimateKey = null;
  $("#upload-estimates").classList.add('hidden');
  $("#upload-estimates").classList.remove('needs-gpu-selection', 'is-pending');
}
function scheduleUploadEstimate(plan) {
  if (!$('#upload-dialog').open || !executionDevice()) return clearUploadEstimate();
  const device = executionDevice(), gpu = state.selectedGpu;
  if (device.backend === 'mlx') {
    clearUploadEstimate();
    const box = $('#upload-estimates');
    box.classList.remove('hidden', 'is-over-capacity');
    box.classList.add('is-pending');
    $('#upload-estimate-memory').textContent = '업데이트 예정입니다.';
    $('#upload-estimate-time').textContent = '업데이트 예정입니다.';
    $('#upload-estimate-note').textContent = '';
    $('#upload-memory-warning').classList.add('hidden');
    return;
  }
  if (device.backend === 'cuda' && !gpu) {
    clearUploadEstimate();
    const box = $('#upload-estimates');
    box.classList.remove('hidden', 'is-over-capacity');
    box.classList.add('needs-gpu-selection');
    $('#upload-estimate-memory').textContent = '-';
    $('#upload-estimate-time').textContent = '-';
    $('#upload-estimate-note').textContent = '';
    $('#upload-memory-warning').classList.add('hidden');
    return;
  }
  if (!plan) return clearUploadEstimate();
  if (window.cosmossSpeech?.active()) return window.cosmossSpeech.estimate();
  const duration = state.uploadPreparation.duration;
  const hardware = state.gpuDeviceId === device.id ? state.gpuInventory.map(item => [item.name, item.memory_total_mib]) : [];
  const key = JSON.stringify([device.id, device.model_key, device.options, device.hardware, hardware, duration, plan.count, gpu?.uuid, gpu?.name, gpu?.memory_total_mib, gpu?.memory_used_mib, gpu?.memory_free_mib]);
  if (state.estimateKey === key) return;
  clearTimeout(state.estimateTimer);
  state.estimateController?.abort();
  state.estimateKey = key;
  const box = $('#upload-estimates');
  box.classList.remove('hidden', 'is-over-capacity', 'needs-gpu-selection', 'is-pending');
  $('#upload-memory-warning').classList.add('hidden');
  $('#upload-estimate-memory').textContent = '—';
  $('#upload-estimate-time').textContent = '—';
  $('#upload-estimate-note').textContent = '예상값 계산 중…';
  state.estimateTimer = setTimeout(async () => {
    const controller = new AbortController();
    state.estimateController = controller;
    const query = new URLSearchParams({duration, count:plan.count});
    if (gpu?.uuid && device.backend === 'cuda') query.set('gpu_uuid', gpu.uuid);
    try {
      const estimate = await api(`/api/devices/${encodeURIComponent(device.id)}/estimate?${query}`, {signal:controller.signal});
      if (state.estimateKey !== key) return;
      const memory = estimate.memory_expected_gib != null ? `약 ${estimate.memory_expected_gib.toFixed(1)} GiB` : '추정 불가';
      $('#upload-estimate-memory').textContent = memory;
      $('#upload-estimate-time').textContent = estimate.time_seconds != null ? `약 ${clock(estimate.time_seconds)}` : '추정 불가';
      $('#upload-estimate-note').textContent = '';
      box.classList.toggle('is-over-capacity', !!estimate.exceeds_capacity);
      const warning = $('#upload-memory-warning');
      warning.classList.toggle('hidden', !estimate.exceeds_capacity && !estimate.exceeds_free);
      warning.textContent = estimate.exceeds_capacity
        ? `여유를 포함한 ${estimate.memory_high_gib.toFixed(1)} GiB가 전체 ${estimate.capacity_kind} ${estimate.capacity_gib.toFixed(1)} GiB를 초과합니다. 청크 분할 개수를 늘려 주세요.`
        : estimate.exceeds_free ? `현재 여유 VRAM ${estimate.free_gib.toFixed(1)} GiB보다 클 수 있습니다. 다른 프로세스의 사용량을 확인하세요.` : '';
    } catch (error) {
      if (state.estimateKey === key && error.name !== 'AbortError') $('#upload-estimate-note').textContent = '예측값을 불러오지 못했습니다.';
    }
  }, 180);
}
$("#upload-chunk-count").addEventListener("input", event => {
  state.uploadChunkCount = event.target.value;
  renderUploadChunks();
});
for (const [selector, step] of [["#increase-upload-chunks", 1], ["#decrease-upload-chunks", -1]]) {
  $(selector).addEventListener("click", () => {
    const input = $("#upload-chunk-count");
    if (input.disabled) return;
    const current = Number.isInteger(input.valueAsNumber) ? input.valueAsNumber : Math.ceil(state.uploadPreparation.duration / (uploadChunkDefaultMinutes() * 60));
    state.uploadChunkCount = String(Math.max(1, Math.min(1000, current + step)));
    renderUploadChunks();
  });
}
function renderFilePreparation() {
  renderUploadChunks();
  const preparation = state.uploadPreparation;
  $("#upload-preparation").classList.toggle("hidden", !preparation);
  $("#upload-preparation").classList.toggle("failed", preparation?.status === "error");
  $("#upload-form").querySelector("button[type=submit]").disabled = state.uploading || !executionDevice() || preparation?.status !== "ready" || (executionDevice()?.backend === "cuda" && !state.selectedGpu) || !!window.cosmossSpeech?.blocked();
  $("#retry-preparation").classList.toggle("hidden", preparation?.status !== "error");
  if (!preparation) return;
  $("#upload-duration").textContent = Number.isFinite(preparation.duration) && preparation.duration > 0
    ? `음성 길이 ${clock(preparation.duration)}` : "음성 길이 확인 중…";
  $("#upload-preparation-status").textContent = {
    uploading: "파일 업로드 중…", preparing: "전처리 중…",
    ready: "전처리 완료 · 전사를 시작할 수 있어요", error: preparation.error,
  }[preparation.status];
}
async function pollFilePreparation(preparation) {
  if (state.uploadPreparation !== preparation) return;
  try {
    const upload = await api(`/api/uploads/${encodeURIComponent(preparation.id)}`, { signal: preparation.controller.signal });
    if (state.uploadPreparation !== preparation) return;
    preparation.status = upload.status;
    if (Number.isFinite(upload.duration) && upload.duration > 0) preparation.duration = upload.duration;
    preparation.error = upload.error;
    renderFilePreparation();
    if (upload.status === "preparing") preparation.timer = setTimeout(() => pollFilePreparation(preparation), 1000);
  } catch (error) {
    if (state.uploadPreparation !== preparation) return;
    preparation.status = "error";
    preparation.error = error.message;
    renderFilePreparation();
  }
}
async function showFile(file, voiceMemo = null) {
  cancelFilePreparation();
  state.uploadChunkCount = null;
  if (!file) return resetDropZone();
  try { validateFile(file); } catch (error) {
    fileInput.value = "";
    resetDropZone();
    return toast(error.message, true);
  }
  $("#drop-title").textContent = file.name;
  fileInput.required = !voiceMemo;
  $("#drop-copy").textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB`;
  const preparation = { file, voiceMemo, id: null, status: "uploading", duration: voiceMemo?.duration ?? null, controller: new AbortController() };
  state.uploadPreparation = preparation;
  renderFilePreparation();
  // Browser metadata can show duration before the upload completes. ffprobe
  // supplies the authoritative duration for formats the browser cannot read.
  if (!voiceMemo) {
  const audio = new Audio();
  preparation.audio = audio;
  preparation.objectUrl = URL.createObjectURL(file);
  audio.preload = "metadata";
  audio.addEventListener("loadedmetadata", () => {
    if (state.uploadPreparation === preparation && Number.isFinite(audio.duration) && audio.duration > 0) {
      preparation.duration = audio.duration;
      renderFilePreparation();
    }
    releaseFileMetadata(preparation);
  }, { once: true });
  audio.addEventListener("error", () => releaseFileMetadata(preparation), { once: true });
  audio.src = preparation.objectUrl;
  }
  try {
    await refreshAppSetup(true);
    if (state.uploadPreparation !== preparation) return;
    const body = new FormData();
    if (!voiceMemo) body.set("file", file);
    const upload = await api(voiceMemo ? '/api/voice-memos/import' : '/api/uploads', {
      method: 'POST', signal: preparation.controller.signal,
      ...(voiceMemo ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: voiceMemo.id, version: voiceMemo.version }) } : { body }),
    });
    if (state.uploadPreparation !== preparation) {
      deletePreparedFile(upload.id);
      return;
    }
    preparation.id = upload.id;
    preparation.status = upload.status;
    if (Number.isFinite(upload.duration) && upload.duration > 0) preparation.duration = upload.duration;
    preparation.error = upload.error;
    renderFilePreparation();
    if (upload.status === "preparing") await pollFilePreparation(preparation);
  } catch (error) {
    if (state.uploadPreparation !== preparation) return;
    preparation.status = "error";
    preparation.error = error.message;
    renderFilePreparation();
  }
}
$("#retry-preparation").addEventListener("click", () => {
  const preparation = state.uploadPreparation;
  showFile(preparation?.voiceMemo ? preparation.file : fileInput.files[0], preparation?.voiceMemo);
});
fileInput.addEventListener("change", () => showFile(fileInput.files[0]));
["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => { event.preventDefault(); if (!state.uploading) dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", event => {
  if (state.uploading || !event.dataTransfer.files.length) return;
  const files = new DataTransfer();
  files.items.add(event.dataTransfer.files[0]);
  fileInput.files = files.files;
  showFile(fileInput.files[0]);
});

$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.uploading) return;
  try { validateFile(state.uploadPreparation?.file); } catch (error) { return toast(error.message, true); }
  const preparation = state.uploadPreparation;
  if (preparation?.status !== "ready") return toast("전처리가 완료된 후 전사를 시작해 주세요.", true);
  const form = event.target;
  if (window.cosmossSpeech?.blocked()) return toast('선택한 모델의 준비가 끝난 뒤 전사를 시작해 주세요.', true);
  const body = new FormData(form);
  body.delete("selected-gpu");
  if (!executionDevice()) return toast("처리 장치를 다시 선택해 주세요.", true);
  body.set("device_id", executionDevice().id);
  window.cosmossSpeech?.form(body);
  body.set("output_directory", state.saveDirectory || "~/Downloads");
  if (executionDevice()?.backend === "cuda") {
    if (!state.selectedGpu || state.gpuDeviceId !== executionDevice().id) return toast("이번 전사에 사용할 GPU를 선택해 주세요.", true);
    body.set("gpu_uuid", state.selectedGpu.uuid);
    if (state.gpuApproval?.uuid === state.selectedGpu.uuid && state.gpuApproval.token === state.selectedGpu.busy_confirmation)
      body.set("gpu_confirmation", state.gpuApproval.token);
  }
  body.delete("file");
  body.set("upload_id", preparation.id);
  state.uploading = true;
  stopGpuMonitor();
  const button = form.querySelector("button[type=submit]");
  const fields = [...form.elements];
  const disabledBeforeUpload = new Map(fields.map(field => [field, field.disabled]));
  fields.forEach(field => field.disabled = true);
  button.firstChild.textContent = "전사 시작 중… ";
  try {
    await flushSave();
    let note;
    while (!note) {
      try { note = await api("/api/notes", { method: "POST", body }); }
      catch (error) {
        if (error.status !== 409 || error.detail?.code !== "gpu_confirmation_required") throw error;
        const gpu = error.detail.gpu;
        if (!(await confirmBusyGpu(gpu))) return;
        body.set("gpu_confirmation", gpu.busy_confirmation);
      }
    }
    releaseFileMetadata(preparation);
    state.uploadPreparation = null;
    $("#upload-dialog").close();
    form.reset();
    resetDropZone();
    $("#note-search").value = "";
    await loadNotes();
    await openNote(note.id);
    toast("전사 대기열에 추가했습니다.");
  } catch (error) { toast(error.message, true); }
  finally {
    state.uploading = false;
    fields.forEach(field => field.disabled = disabledBeforeUpload.get(field));
    button.firstChild.textContent = "전사 시작하기 ";
    renderFilePreparation();
    if ($("#upload-dialog").open) syncExecutionGpu();
  }
});

function filterTranscript() {
  if (!state.current) return;
  const query = $("#transcript-search").value.trim().toLowerCase();
  const segments = displayedSegments();
  let visible = 0;
  document.querySelectorAll(".segment").forEach((row, index) => {
    const segment = segments[index];
    const speaker = state.current.speaker_names[segment.speaker] || segment.speaker;
    const match = !query || `${speaker} ${segment.text}`.toLowerCase().includes(query);
    row.classList.toggle("filtered", !match);
    if (match) { visible++; fitText(row.querySelector(".segment-text")); }
  });
  const speakers = new Set(segments.map(segment => segment.speaker)).size;
  $("#segment-count").textContent = query ? `${visible}/${segments.length}개 구간` : `${segments.length}개 구간 · 화자 ${speakers}명`;
  $("#transcript-empty").classList.toggle("hidden", visible > 0);
  $("#transcript-empty").textContent = segments.length ? "검색 결과가 없습니다. 다른 단어로 검색해 보세요." : "인식된 발화가 없습니다. 녹음에 음성이 있는지 확인해 주세요.";
}
$("#transcript-search").addEventListener("input", filterTranscript);
window.addEventListener("resize", () => document.querySelectorAll(".segment:not(.filtered) .segment-text").forEach(fitText));
$("#media-player").addEventListener("timeupdate", event => {
  const time = event.target.currentTime;
  document.querySelectorAll(".segment").forEach(row => row.classList.toggle("active", time >= Number(row.dataset.start) && time < Number(row.dataset.end)));
});

function bindConfirmationDialog(selector, close) {
  const dialog = $(selector);
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener("close", () => {
    if (!dialog.open) close();
  });
  dialog.addEventListener("click", event => {
    if (event.target !== event.currentTarget) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
}

function openCancellationDialog() {
  if (!state.current || state.cancellingId || state.restartingId || state.deletingId) return;
  if (!["queued", "processing"].includes(state.current.status)) return;
  const dialog = $("#cancel-dialog");
  if (dialog.open) return;
  state.cancellationNoteId = state.current.id;
  $("#cancel-dialog-note-title").textContent = state.current.title;
  dialog.showModal();
}

function closeCancellationDialog() {
  state.cancellationNoteId = null;
  const dialog = $("#cancel-dialog");
  if (dialog.open) dialog.close();
}

async function cancelTranscription(noteId) {
  if (!state.current || state.cancellingId || state.restartingId || state.deletingId) return;
  if (state.current.id !== noteId) return;
  if (!["queued", "processing"].includes(state.current.status)) return;
  const id = state.current.id;
  state.cancellingId = id;
  ++state.noteRequest;
  renderTranscriptionControls(state.current);
  try {
    await flushSave();
    const note = await api(`/api/notes/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    if (state.current?.id === id) state.current = note;
    await loadNotes();
    toast("전사를 취소했습니다.");
  } catch (error) {
    toast(`전사를 취소하지 못했습니다. ${error.message}`, true);
  } finally {
    state.cancellingId = null;
    if (state.current?.id === id) renderWorkspace();
    else if (state.current) renderTranscriptionControls(state.current);
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }
}
$("#cancel-transcription").addEventListener("click", openCancellationDialog);
$("#keep-transcribing").addEventListener("click", closeCancellationDialog);
$("#close-cancel-dialog").addEventListener("click", closeCancellationDialog);
$("#confirm-cancel-transcription").addEventListener("click", async () => {
  const noteId = state.cancellationNoteId;
  closeCancellationDialog();
  if (noteId) await cancelTranscription(noteId);
});
bindConfirmationDialog("#cancel-dialog", closeCancellationDialog);

async function restartTranscription() {
  if (!state.current || state.restartingId || state.cancellingId || state.deletingId) return;
  if (!["cancelled", "error"].includes(state.current.status)) return;
  const id = state.current.id;
  state.restartingId = id;
  ++state.noteRequest;
  renderTranscriptionControls(state.current);
  try {
    await flushSave();
    const note = await api(`/api/notes/${encodeURIComponent(id)}/restart`, { method: "POST" });
    if (state.current?.id === id) state.current = note;
    await loadNotes();
    toast("전사 대기열에 다시 추가했습니다.");
  } catch (error) {
    toast(`다시 시작하지 못했습니다. ${error.message}`, true);
  } finally {
    state.restartingId = null;
    if (state.current?.id === id) renderWorkspace();
    else if (state.current) renderTranscriptionControls(state.current);
    if (state.notes.some(note => ["queued", "processing"].includes(note.status))) schedulePoll();
  }
}
$("#restart-transcription").addEventListener("click", restartTranscription);

function openDeletionDialog() {
  if (!state.current || state.deletingId || state.cancellingId || state.restartingId) return;
  if (["queued", "processing"].includes(state.current.status)) return;
  const dialog = $("#delete-dialog");
  if (dialog.open) return;
  state.deletionNoteId = state.current.id;
  $("#delete-dialog-note-title").textContent = state.pendingChanges?.id === state.current.id
    ? state.pendingChanges.changes.title ?? state.current.title : state.current.title;
  dialog.showModal();
}

function closeDeletionDialog() {
  state.deletionNoteId = null;
  const dialog = $("#delete-dialog");
  if (dialog.open) dialog.close();
}

async function deleteNote(noteId) {
  if (!state.current || state.deletingId || state.cancellingId || state.restartingId) return;
  if (state.current.id !== noteId || ["queued", "processing"].includes(state.current.status)) return;
  const note = state.current;
  state.deletingId = note.id;
  ++state.noteRequest;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  const controls = [...document.querySelectorAll(".app-shell button, #workspace button, #workspace input, #workspace textarea")];
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => control.disabled = true);
  try {
    await flushSave();
    await api(`/api/notes/${encodeURIComponent(note.id)}`, { method: "DELETE" });
    if (state.pendingChanges?.id === note.id) state.pendingChanges = null;
    clearTimeout(state.saveTimer);
    if (state.current?.id === note.id) {
      state.current = null;
      $("#media-player").pause();
      $("#media-player").removeAttribute("src");
      $("#workspace").classList.add("hidden");
      $("#note-dialog").close();
      selectSummaryNote(null);
    }
    await loadNotes();
    toast("노트를 삭제했습니다.");
  } catch (error) { toast(error.message, true); }
  finally {
    state.deletingId = null;
    controls.forEach((control, index) => control.disabled = disabled[index]);
    if (["queued", "processing"].includes(state.current?.status) || state.notes.some(item => ["queued", "processing"].includes(item.status))) schedulePoll();
  }
}
$("#delete-note").addEventListener("click", openDeletionDialog);
$("#keep-note").addEventListener("click", closeDeletionDialog);
$("#close-delete-dialog").addEventListener("click", closeDeletionDialog);
$("#confirm-delete-note").addEventListener("click", async () => {
  const noteId = state.deletionNoteId;
  closeDeletionDialog();
  if (noteId) await deleteNote(noteId);
});
bindConfirmationDialog("#delete-dialog", closeDeletionDialog);

function executionDevice() {
  return state.devices.find(device => device.id === state.executionDeviceId);
}

function stopGpuMonitor() {
  clearTimeout(state.gpuTimer);
  state.gpuAbort?.abort();
  state.sshRequest++;
}

function syncExecutionGpu() {
  const device = executionDevice();
  const managed = device?.backend === "cuda";
  $('#upload-dialog').classList.toggle('has-execution-gpu', managed);
  $("#execution-gpu").classList.toggle("hidden", !managed);
  if (state.gpuDeviceId !== device?.id) {
    stopGpuMonitor();
    state.selectedGpu = null;
    state.gpuApproval = null;
    state.gpuInventory = [];
    state.gpuDeviceId = device?.id;
    $("#execution-gpu-meta").textContent = "";
    $("#gpu-probe-result").textContent = "";
  }
  if (managed && $("#upload-dialog").open && !$("#device-dialog").open) refreshGpu();
  else stopGpuMonitor();
  renderFilePreparation();
}

async function loadDevices() {
  const devices = await api("/api/devices");
  state.devices = devices;
  for (const device of state.devices) {
    if (device.kind === "local" && device.setup) {
      state.localModel = device.setup;
      renderLocalModel();
      scheduleLocalModelPoll();
    }
    if (device.backend !== "cuda") continue;
    if (device.setup) state.workerStates[device.id] = { ...state.workerStates[device.id], ...device.setup };
    if (!state.workerTimers[device.id] && !state.workerPolling.has(device.id)) pollWorker(device.id);
  }
  renderDevices();
  renderHostCandidates();
  for (const device of state.devices) {
    if (device.kind !== "local" && device.backend !== "cuda") continue;
    const setup = device.id === "local" ? state.localModel : state.workerStates[device.id];
    if (device.model_selected === false || device.options?.speech_pipeline && device.options.speech_pipeline !== 'moss-original') continue;
    if (!runtimeState(device.id) && !state.environments[device.id] && !modelBusy(setup) && setup?.phase !== "unknown") checkEnvironment(device.id);
  }
  if ($('#upload-dialog').open) renderFilePreparation();
}

function runtimeState(id) {
  return (id === "local" ? state.localModel : state.workerStates[id])?.runtime || state.environments[id];
}

function renderAppSetup() {
  const setup = state.appSetup;
  const busy = !!setup?.busy || state.appSetupChecking;
  $("#open-app-settings").textContent = setup?.busy ? "설정 · 준비 중" : state.appSetupChecking ? "설정 · 확인 중" : "설정";
  $("#open-app-settings").classList.toggle("is-busy", busy);
  $("#app-settings-components").innerHTML = [["ffmpeg", "FFmpeg · 음성 변환"], ["ffprobe", "FFprobe · 파일 정보 확인"]].map(([key, title]) => {
    const item = setup?.components?.[key];
    const detail = item?.ready ? `${item.version} · ${item.source === "app" ? "앱에서 준비됨" : "이미 설치된 도구 사용"}` : "준비하기를 누르면 자동으로 설치합니다.";
    return `<div class="environment-component"><div><strong>${title}</strong><span class="environment-component-status${item?.ready ? " is-ready" : ""}">${item ? item.ready ? "준비됨" : "준비 필요" : "확인 중"}</span></div><p>${escapeHtml(detail)}</p></div>`;
  }).join("");
  const phases = { checking: "설치된 도구를 확인하고 있습니다…", downloading: "파일 처리 도구를 다운로드하고 있습니다…", installing: "다운로드한 도구를 확인하고 있습니다…" };
  $("#app-settings-status").textContent = setup?.error || (busy ? phases[setup?.phase] || "준비 상태를 확인하고 있습니다…" : setup?.ready ? "파일 처리 도구가 준비됐습니다." : "처음 한 번 준비하면 다음 실행부터 다시 사용할 수 있습니다.");
  $("#app-settings-status").classList.toggle("is-ready", !!setup?.ready && !busy);
  $("#app-settings-progress").innerHTML = downloadProgressHtml(setup, "파일 처리 도구 다운로드 진행률");
  $("#check-app-settings").disabled = busy;
  $("#prepare-app-settings").disabled = busy;
  $("#prepare-app-settings").textContent = busy ? "준비 중…" : setup?.ready ? "다시 확인·복구" : "준비하기";
}

async function refreshAppSetup(start = false) {
  const request = ++state.appSetupRequest;
  clearTimeout(state.appSetupTimer);
  state.appSetupChecking = true;
  renderAppSetup();
  try {
    const setup = await api("/api/setup", start ? { method: "POST" } : {});
    if (request === state.appSetupRequest) state.appSetup = start ? { components: state.appSetup?.components, ...setup } : setup;
  } catch (error) {
    if (request === state.appSetupRequest) state.appSetup = { ...state.appSetup, busy: false, phase: "error", error: error.message };
    if (start) throw error;
  } finally {
    if (request === state.appSetupRequest) {
      state.appSetupChecking = false;
      renderAppSetup();
      if (state.appSetup?.busy) state.appSetupTimer = setTimeout(refreshAppSetup, 800);
    }
  }
}

$("#open-app-settings").addEventListener("click", () => {
  $("#app-settings-dialog").showModal();
  refreshAppSetup();
});
$("#close-app-settings").addEventListener("click", () => $("#app-settings-dialog").close());
$("#check-app-settings").addEventListener("click", () => refreshAppSetup());
$("#prepare-app-settings").addEventListener("click", () => refreshAppSetup(true).catch(error => toast(error.message, true)));

async function checkEnvironment(id) {
  if (state.environmentRequests.has(id)) return;
  state.environmentRequests.add(id);
  renderEnvironment();
  try {
    const runtime = await api(`/api/devices/${encodeURIComponent(id)}/environment`);
    state.environments[id] = runtime;
    if (id === "local") state.localModel = { ...state.localModel, runtime };
    else state.workerStates[id] = { ...state.workerStates[id], runtime };
  } catch (error) {
    const runtime = { ...runtimeState(id), error: error.message };
    state.environments[id] = runtime;
    if (id === "local") state.localModel = { ...state.localModel, runtime };
    else state.workerStates[id] = { ...state.workerStates[id], runtime };
  } finally {
    state.environmentRequests.delete(id);
    renderDevices();
    renderEnvironment();
  }
}

function renderEnvironment() {
  const id = state.environmentDeviceId;
  if (!id || !$("#environment-dialog").open) return;
  const runtime = runtimeState(id);
  const setup = id === "local" ? state.localModel : state.workerStates[id];
  const busy = state.environmentSubmitting || state.environmentRequests.has(id) || modelBusy(setup);
  $("#environment-device-name").textContent = state.devices.find(device => device.id === id)?.name || "실행 환경";
  $("#environment-components").innerHTML = [["uv", "uv"], ["venv", "Python venv"], ["packages", "필수 패키지"]].map(([key, name]) => {
    const item = runtime?.[key];
    const detail = key === "packages" ? item?.missing?.length ? `누락: ${item.missing.join(", ")}` : Object.entries(item?.versions || {}).map(([name, version]) => `${name} ${version}`).join(" · ") : [item?.version, item?.path].filter(Boolean).join("\n");
    return `<div class="environment-component"><div><strong>${name}</strong><span class="environment-component-status${item?.ready ? " is-ready" : ""}">${item ? item.ready ? "준비됨" : "설치 필요" : "미확인"}</span></div>${detail ? `<p>${escapeHtml(detail)}</p>` : ""}</div>`;
  }).join("");
  $("#environment-status").textContent = state.environmentRequests.has(id) ? "확인 중…" : modelBusy(setup) ? (localModelPhases[setup.phase]?.[0] || "준비 중…") : runtime?.error || setup?.error || "";
  $("#environment-status").classList.toggle("hidden", !$("#environment-status").textContent);
  $("#check-environment").disabled = busy;
  $("#install-environment").disabled = busy;
  $("#install-environment").textContent = busy ? "준비 중…" : runtime?.ready ? "환경 다시 확인·복구" : "실행 환경 설치";
}

async function openEnvironment(id) {
  state.environmentDeviceId = id;
  if (!$("#environment-dialog").open) $("#environment-dialog").showModal();
  renderEnvironment();
  if (!modelBusy(id === "local" ? state.localModel : state.workerStates[id])) await checkEnvironment(id);
}

$("#close-environment").addEventListener("click", () => $("#environment-dialog").close());
$("#check-environment").addEventListener("click", () => checkEnvironment(state.environmentDeviceId));
$("#install-environment").addEventListener("click", async () => {
  const id = state.environmentDeviceId;
  if (!id || state.environmentSubmitting) return;
  state.environmentSubmitting = true;
  renderEnvironment();
  try {
    await refreshAppSetup(true);
    const result = await api(`/api/devices/${encodeURIComponent(id)}/environment`, { method: "POST" });
    if (id === "local") { state.localModel = result; scheduleLocalModelPoll(); }
    else { state.workerStates[id] = { ...state.workerStates[id], ...result }; pollWorker(id); }
    renderDevices();
  } catch (error) { toast(error.message, true); }
  finally { state.environmentSubmitting = false; renderEnvironment(); }
});

function devicePresentation(device) {
  const worker = state.workerStates[device.id];
  const status = state.deviceStatus[device.id];
  if (device.model_selected === false) return {label:'모델을 선택해 주세요',tone:'',ready:false};
  if (device.kind === 'local' && state.localModel?.phase === 'ready') return {label:'모델 준비됨',tone:'ready',ready:true};
  if (device.kind === "local" && localModelBusy()) return {
    label: "모델 준비 중", tone: "busy", ready: false,
  };
  if (device.kind === 'local') return {label:'모델 다운로드 필요',tone:'',ready:false};
  if (device.backend !== "cuda") return {
    label: status?.ok ? "사용 가능" : status ? "모델 준비 필요" : "확인 중",
    tone: status?.ok ? "ready" : "",
    ready: !!status?.ok,
  };
  const phases = {
    connecting: "연결 중", checking_environment: "기존 환경 확인 중", waiting_environment: "다른 장치의 환경 준비 대기 중",
    installing_uv: "uv 설치 중", installing: "환경 설치 중", unselected:'모델을 선택해 주세요',
    checking_model: "모델 확인 중", downloading: "다운로드 중",
    idle: "모델 준비 필요", installed: "모델 준비됨", stopped: "모델 준비됨",
    loading: "GPU 준비 중", ready: "GPU 사용 중", stopping: "정리 중",
    error: "확인 필요", interrupted: "확인 필요", unknown: "확인 중",
  };
  const phase = worker?.phase || "unknown";
  const ready = ["installed", "stopped"].includes(phase) && !worker?.active_gpu_uuid;
  const busy = ["connecting", "checking_environment", "waiting_environment", "installing_uv", "installing", "checking_model", "downloading", "loading", "ready", "stopping"].includes(phase);
  return { label: worker?.active_gpu_uuid ? "이전 GPU 작업 확인" : (phases[phase] || phases.unknown),
    tone: ready ? "ready" : busy ? "busy" : "", ready };
}

function managedSpeechPipeline(key) { return ['moss-batch','qwen-batch','moss-sglang','qwen-transformers','moss-qwen-sequential'].includes(key); }

function cardModelOptions(device) {
  const selected = device.model_selected !== false;
  const placeholder = `<option value="" disabled ${selected ? '' : 'selected'}>모델 선택…</option>`;
  if (device.backend === 'cuda') {
    const current = device.options?.speech_pipeline || 'moss-sglang';
    const presets = state.catalog?.speech_presets || [{key:'moss-sglang',name:'MOSS · SGLang Omni · 전체',badge:'추천 · 서버 기본값'}, {key:'qwen-transformers',name:'Qwen · Transformers · 3분 + 정렬'}, {key:'moss-qwen-sequential',name:'MOSS + Qwen · 순차 실행'}];
    const legacy = selected && !presets.some(item=>item.key===current) ? `<option value="speech:${escapeHtml(current)}" selected>이전 전사 설정 · 새 모델을 선택해 주세요</option>` : '';
    return placeholder + presets.map(item => `<option value="speech:${item.key}" ${selected && device.model_key !== 'custom' && item.key === current ? 'selected' : ''}>${escapeHtml(item.name + (item.badge ? ` · ${item.badge}` : ''))}</option>`).join('') + legacy;
  }
  return placeholder + modelOptionsHtml(device.id, selected ? device.model_key || state.localModel?.model_key || 'mlx-8bit' : '', false);
}

function renderDevices() {
  $("#device-count").textContent = state.devices.length;
  $("#device-panel-summary").textContent = "";
  $("#device-panel-summary").classList.add("hidden");
  $("#registered-devices").innerHTML = state.devices.map(device => {
    const worker = state.workerStates[device.id];
    const presentation = devicePresentation(device);
    const managed = device.backend === "cuda";
    const fast = managed && managedSpeechPipeline(device.options?.speech_pipeline);
    const gpuActive = !!worker?.active_gpu_uuid || ["loading", "ready"].includes(worker?.phase);
    const cleanupPending = gpuActive || device.worker_cleanup_pending || worker?.worker_cleanup_pending || (["prepare", "stop"].includes(worker?.action) && ["error", "interrupted"].includes(worker?.phase));
    const busy = modelBusy(device.kind === 'local' ? state.localModel : worker) || state.modelSelectingId === device.id;
    const detail = device.kind === "local" ? "이 기기 · Apple Silicon" : device.host;
    const icon = device.kind === "local" ? '<rect x="3" y="3" width="18" height="13" rx="2"/><path d="M8 21h8m-4-5v5"/>' : '<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M12 6.5h4M12 17.5h4"/>';
    return `<article class="registered-device ${device.kind === "local" ? "is-local" : ""}" data-device-id="${escapeHtml(device.id)}" aria-label="${escapeHtml(device.name)}">
      <div class="device-card-head"><span class="device-card-icon" aria-hidden="true"><svg viewBox="0 0 24 24">${icon}</svg></span>
        <div class="device-card-name"><div class="device-card-title"><b title="${escapeHtml(device.name)}">${escapeHtml(device.name)}</b>${device.kind === "local" ? '<span class="default-device">기본</span>' : ""}</div><small>${escapeHtml(detail)}</small></div>
        ${device.kind === "ssh" ? `<button class="icon-button danger" data-device-action="remove" type="button" aria-label="${escapeHtml(device.name)} 제거" title="장치 제거" ${busy ? "disabled" : ""}><svg class="button-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5m4-5v5"/></svg></button>` : ""}
      </div>
      <div class="device-card-state ${presentation.tone}"><i aria-hidden="true"></i><span>${escapeHtml(presentation.label)}</span>${(device.kind === "local" || managed) && !fast && device.model_selected !== false ? `<button class="device-environment-link${runtimeState(device.id)?.ready ? " is-ready" : ""}" data-device-action="environment" type="button" aria-haspopup="dialog" title="uv · Python venv · 필수 패키지 확인 및 설치">${runtimeState(device.id)?.ready ? "uv · venv ✓" : "실행 환경"}</button>` : ""}</div>
      ${device.kind === "local" || managed ? `<label class="field device-model-field"><span>전사 모델 <small class="device-chunk-size">${device.model_selected === false ? '선택 후 다운로드' : fast ? '화자 구분 · Nemotron' : escapeHtml(deviceKnobSummary(device))}</small></span><select data-device-model="${escapeHtml(device.id)}" ${busy ? "disabled" : ""}>${cardModelOptions(device)}</select></label>
      <button class="device-model-download" data-device-action="download" type="button" ${busy || device.model_selected === false || worker?.phase === 'unknown' || presentation.ready ? 'disabled' : ''}>${busy ? '모델 준비 중…' : presentation.ready ? '모델 준비 완료' : '선택한 모델 다운로드'}</button>` : ""}
      ${downloadProgressHtml(device.kind === "local" ? state.localModel : worker)}
      ${worker?.error ? `<details class="device-diagnostics"><summary>준비 상세 보기</summary><p>${escapeHtml(worker.error)}</p></details>` : ""}
      <div class="device-actions">
        <button class="device-create-note use-device" data-device-action="use" type="button" title="${escapeHtml(device.name)}에서 노트 만들기" ${device.model_selected === false || ((managed || device.kind === 'local') && !presentation.ready) ? 'disabled' : ''}>
          <span class="create-note-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></span>
          <span>노트 만들기</span>
          <svg class="create-note-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>
        </button>
        <div class="device-secondary-actions">${managed ? `${!fast ? '<button class="ghost-button" data-device-action="install" type="button">고급 설정</button>' : ''}${cleanupPending && !(worker?.phase === "stopped" && worker?.cleaned) ? `<button class="ghost-button" data-device-action="stop" type="button">${gpuActive ? "GPU 해제" : "작업 정리"}</button>` : ""}` : device.kind === "local" ? '<button class="ghost-button" data-device-action="local-model" type="button">고급 설정</button>' : '<button class="ghost-button" data-device-action="status" type="button">상태 확인</button>'}</div>
      </div></article>`;
  }).join("");
  renderEnvironment();
}

function modelOptionsHtml(id, selected, all = true) {
  const backend = id === "local" ? "mlx" : "cuda";
  const metadata = state.modelMetadata[id] || (id === "local" ? state.catalog : null);
  const recommended = metadata?.recommendation?.model_key || (backend === "mlx" ? "mlx-8bit" : "cuda-original");
  const entries = state.catalog?.models || [];
  const models = entries.filter(model => all || model.backend === backend).map(model => {
    const supported = model.backend === backend;
    return `<option value="${escapeHtml(model.key)}" ${model.key === selected ? "selected" : ""} ${supported ? "" : "disabled"}>${escapeHtml(model.name)}${supported && model.key === recommended ? " · 추천" : ""}${supported ? "" : " · 다른 실행 방식"}</option>`;
  }).join("");
  const unavailable = all ? (metadata?.unavailable || state.catalog?.unavailable || []).map(item => `<option disabled>${escapeHtml(item.name)} · ${escapeHtml(item.reason)}</option>`).join("") : "";
  return models + `<option value="custom" ${selected === "custom" ? "selected" : ""}>다른 MOSS 저장소…</option>` + unavailable;
}
function deviceKnobSummary(device) {
  const minutes = device.options?.chunk_minutes ?? (device.kind === "local" ? state.localModel?.options?.chunk_minutes : state.modelMetadata[device.id]?.options?.chunk_minutes);
  return minutes != null ? `청크 ${minutes}분` : device.backend === "cuda" ? "청크 자동 · GPU 권장값" : "청크 30분";
}
async function loadModelCatalog() {
  state.catalog = await api("/api/model-catalog");
  renderDevices();
}
function fillModelKnobs(options) {
  for (const field of document.querySelectorAll('[data-knob]')) {
    const value = options?.[field.dataset.knob];
    if (value !== undefined) field.value = String(value);
  }
}
function readModelKnobs() {
  const values = {};
  for (const field of document.querySelectorAll('[data-knob]')) {
    const key = field.dataset.knob;
    values[key] = key === 'use_cache' ? field.value === 'true' : ['precision', 'attention'].includes(key) ? field.value : Number(field.value);
  }
  return values;
}
function updateModelChoice(resetSource = false) {
  const key = $("#model-choice").value;
  const selected = state.catalog?.models.find(model => model.key === key);
  $("#model-custom-field").classList.toggle("hidden", key !== 'custom');
  $("#model-custom-id").required = key === 'custom';
  $("#model-choice-help").textContent = selected ? `${selected.description} · 다운로드 ${(selected.bytes / 1024 ** 3).toFixed(2)} GB` : '호환되는 MOSS 모델 저장소를 직접 지정합니다.';
  if (selected && resetSource) {
    const precision = document.querySelector('[data-knob=precision]');
    precision.value = selected.precision;
    $('#model-sources input[value=cache]').checked = true;
  }
  renderLocalModel();
}
function renderModelRecommendation() {
  const local = state.modelDeviceId === 'local';
  const metadata = state.modelMetadata[state.modelDeviceId] || (local ? state.catalog : null);
  const rec = metadata?.recommendation;
  const hardware = metadata?.hardware;
  const recommended = state.catalog?.models.find(model => model.key === rec?.model_key);
  const title = local ? `${hardware?.name || 'Apple Silicon'}${hardware?.memory_gib ? ` · 메모리 ${hardware.memory_gib} GB` : ''}` : '선택한 장치에 맞는 시작 설정';
  $("#model-recommendation").innerHTML = rec ? `<div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(recommended?.name || '')} · ${rec.options.chunk_minutes}분 청크 권장<br>용량 기준의 시작값입니다. 실제 메모리 사용량은 녹음 길이에 따라 달라집니다.</p></div><button class="ghost-button" type="button" data-apply-profile="default">권장값 적용</button>` : '';
  $("#model-profiles").innerHTML = (metadata?.profiles || []).map((profile, index) => `<button class="model-profile" type="button" data-apply-profile="${index}" title="${escapeHtml(profile.name)}의 권장값 적용">GPU #${profile.index} · ${profile.memory_gib ?? '?'} GB · ${profile.options.chunk_minutes}분 권장</button>`).join('');
}
function applyModelProfile(key) {
  const metadata = state.modelMetadata[state.modelDeviceId] || state.catalog;
  const rec = key === 'default' ? metadata?.recommendation : metadata?.profiles[Number(key)];
  if (!rec || modelBusy(currentModelState())) return;
  $("#model-choice").value = rec.model_key;
  fillModelKnobs(rec.options);
  updateModelChoice(true);
  toast('권장값을 채웠습니다. 적용하고 모델을 준비해 주세요.');
}
$("#model-choice").addEventListener("change", () => updateModelChoice(true));
$("#model-recommendation").addEventListener("click", event => {
  const button = event.target.closest('[data-apply-profile]');
  if (button) applyModelProfile(button.dataset.applyProfile);
});
$("#model-profiles").addEventListener("click", event => {
  const button = event.target.closest('[data-apply-profile]');
  if (button) applyModelProfile(button.dataset.applyProfile);
});
document.querySelector('[data-knob=precision]').addEventListener('change', () => {
  if (state.modelDeviceId === 'local') return;
  if ($("#model-choice").value !== 'custom') $("#model-choice").value = 'cuda-original';
  updateModelChoice();
});
$("#registered-devices").addEventListener('change', async event => {
  const select = event.target.closest('[data-device-model]');
  if (select) {
    const requested = select.value;
    const id = select.dataset.deviceModel;
    const device = state.devices.find(device => device.id === id);
    if (requested === 'custom') return openLocalModel(id, requested);
    state.modelSelectingId = id;
    renderDevices();
    try {
      const payload = requested.startsWith('speech:') ? {pipeline:requested.slice(7)} : {model_key:requested};
      const result = await api(`/api/devices/${encodeURIComponent(id)}/model/selection`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      state.deviceModelRevisions[id] = (state.deviceModelRevisions[id] || 0) + 1;
      Object.assign(device,result);
      if (id === 'local') { state.localModel = result.setup; delete state.deviceStatus.local; }
      else { state.workerStates[id] = result.setup; pollWorker(id); }
      toast('모델을 선택했습니다. 왼쪽에서 다운로드해 주세요.');
    } catch (error) { toast(error.message,true); }
    finally { state.modelSelectingId = null; renderDevices(); }
  }
});

const localModelPhases = {
  checking: ["모델 확인 중", "선택한 모델과 실행 환경을 확인하고 있어요."],
  connecting: ["장치 연결 중", "선택한 장치의 실행 환경을 확인하고 있어요."],
  checking_environment: ["기존 환경 확인 중", "이 계정에 준비된 실행 환경을 찾아 재사용합니다."],
  waiting_environment: ["다른 장치의 환경 준비 대기 중", "먼저 시작한 설치가 끝나면 같은 실행 환경을 사용합니다."],
  installing_uv: ["uv 준비 중", "실행 환경 도구를 설치합니다."],
  checking_model: ["모델 확인 중", "이미 받은 모델을 확인하고 있어요."],
  installing: ["실행 환경 준비 중", "처음 한 번 필요한 패키지를 설치합니다. 잠시 기다려 주세요."],
  downloading: ["모델 다운로드 중", "선택한 장치의 Hugging Face 캐시에 저장합니다. 1초마다 갱신합니다."],
  loading: ["모델 불러오는 중", "모델을 메모리에 올리고 있어요."],
  error: ["모델 준비를 완료하지 못했어요", ""],
  interrupted: ["모델 준비 상태를 확인해 주세요", ""],
};

function modelBusy(model) {
  return ["checking", "connecting", "checking_environment", "waiting_environment", "checking_model", "installing_uv", "installing", "downloading", "loading", "stopping"].includes(model?.phase);
}
function localModelBusy() { return modelBusy(state.localModel); }
function currentModelState() { return state.modelDeviceId === "local" ? state.localModel : state.workerStates[state.modelDeviceId]; }
function modelBytes(bytes) {
  const gib = bytes / (1024 ** 3);
  return gib >= 1 ? `${gib.toFixed(2)} GB` : `${(bytes / (1024 ** 2)).toFixed(1)} MB`;
}
function downloadProgressHtml(model, label = "모델 다운로드 진행률") {
  if (model?.phase !== "downloading") return "";
  const download = model.download;
  const percent = Number.isFinite(download?.percent) ? Math.max(0, Math.min(100, download.percent)) : null;
  const amount = download?.total_bytes > 0 ? `${modelBytes(download.downloaded_bytes)} / ${modelBytes(download.total_bytes)}` : "전체 용량 확인 중…";
  return `<div class="model-download"><div><strong>${percent == null ? "모델 다운로드 중…" : `${percent.toFixed(1)}%`}</strong><span>${amount}</span></div><progress max="100" ${percent == null ? "" : `value="${percent}"`} aria-label="${escapeHtml(label)}"></progress></div>`;
}

function renderLocalModel() {
  const model = currentModelState();
  const local = state.modelDeviceId === "local";
  const source = $('#model-sources input:checked').value;
  const busy = modelBusy(model) || state.localModelSubmitting || state.modelLoading;
  $('#save-chunk-default').disabled = busy || state.chunkDefaultSaving;
  for (const field of document.querySelectorAll('#model-options input, #model-options select, #model-choice, #model-custom-id, [data-apply-profile]')) field.disabled = busy;
  document.querySelector('[data-knob=chunk_minutes]').disabled = busy || state.chunkDefaultSaving;
  $("#model-sources").disabled = busy;
  $("#model-folder-fields").classList.toggle("hidden", source !== "folder");
  $("#model-cache-info").classList.toggle("hidden", source !== "cache");
  $("#model-cache-path").textContent = model?.cache_dir || model?.model_cache || "불러오는 중…";
  $("#model-folder-path").disabled = busy;
  $("#model-folder-path").required = source === "folder";
  $("#choose-model-folder").disabled = busy;
  $("#choose-model-folder").classList.toggle("hidden", !local || !window.webkit?.messageHandlers?.chooseModelFolder);
  $("#prepare-local-model").disabled = busy || state.chunkDefaultSaving || !model || !!state.modelError;
  $("#prepare-local-model").textContent = state.modelLoading ? "위치 확인 중…" : busy ? "준비 중…" : "모델 준비";
  const phase = state.modelError ? "error" : model?.phase;
  $("#model-preparation").classList.toggle("hidden", !localModelPhases[phase]);
  $("#model-preparation").classList.toggle("is-busy", modelBusy(model));
  $("#model-preparation").classList.toggle("is-error", ["error", "interrupted"].includes(phase));
  $("#model-preparation-title").textContent = localModelPhases[phase]?.[0] || "";
  $("#model-preparation-detail").textContent = state.modelError || (["error", "interrupted"].includes(phase) ? model.error : localModelPhases[phase]?.[1] || "");
  $("#model-download-progress").innerHTML = downloadProgressHtml(model);
}

async function openLocalModel(id = "local", preferredModel = "") {
  const request = ++state.modelRequest;
  state.modelDeviceId = id;
  state.modelLoading = true;
  state.modelError = null;
  $('#chunk-default-status').textContent = '';
  const local = id === "local";
  const device = state.devices.find(device => device.id === id);
  $("#model-choice").innerHTML = modelOptionsHtml(id, preferredModel || device?.model_key || (local ? 'mlx-8bit' : 'cuda-original'));
  $("#model-knobs").open = false;
  for (const field of document.querySelectorAll('.mlx-knob')) field.classList.toggle('hidden', !local);
  for (const field of document.querySelectorAll('.cuda-knob')) field.classList.toggle('hidden', local);
  $("#model-device-name").textContent = local ? "이 Mac에서 시작하기" : `${device?.name || "연결한 장치"} · 모델 준비`;
  $("#model-folder-description").textContent = local ? "Mac용 MOSS MLX 모델 폴더를 직접 선택합니다." : "이 장치에 이미 받은 MOSS 모델 폴더를 지정합니다.";
  $("#model-folder-help").textContent = local ? "config.json과 safetensors가 있는 폴더를 선택하세요. 파일은 복사하지 않고 그대로 사용합니다." : `${device?.host || "연결한 장치"}의 경로를 입력하세요. 예: ~/models/moss · 파일은 복사하지 않습니다.`;
  $("#model-folder-path").placeholder = local ? "/Users/…/모델 폴더" : "~/models/moss";
  $('#model-sources input[value="cache"]').checked = true;
  $("#model-folder-path").value = "";
  if (!$("#local-model-dialog").open) $("#local-model-dialog").showModal();
  renderLocalModel();
  try {
    const [result] = await Promise.all([api(local ? "/api/local-model" : `/api/devices/${encodeURIComponent(id)}/model`), state.catalog ? Promise.resolve() : loadModelCatalog()]);
    if (request !== state.modelRequest) return;
    if (local) state.localModel = result;
    else state.workerStates[id] = { ...state.workerStates[id], ...result };
    state.modelMetadata[id] = result.models ? result : { ...state.catalog, recommendation: local ? state.catalog.recommendation : {model_key: 'cuda-original', options: state.catalog.recommendation.options}, profiles: [] };
    $("#model-choice").innerHTML = modelOptionsHtml(id, preferredModel || result.model_key || device?.model_key || (local ? 'mlx-8bit' : 'cuda-original'));
    $("#model-custom-id").value = result.model_id || device?.model || '';
    fillModelKnobs(result.options || state.modelMetadata[id].recommendation?.options);
    updateModelChoice(!!preferredModel);
    renderModelRecommendation();
    const source = !preferredModel && result.source === "folder" ? "folder" : "cache";
    $(`#model-sources input[value="${source}"]`).checked = true;
    $("#model-folder-path").value = result.suggested_path || result.model_path || "";
    if (local) scheduleLocalModelPoll();
    else pollWorker(id);
  } catch (error) {
    if (request === state.modelRequest) state.modelError = error.message;
  } finally {
    if (request === state.modelRequest) { state.modelLoading = false; renderLocalModel(); }
  }
}

function scheduleLocalModelPoll() {
  if (localModelBusy() && !state.localModelTimer) state.localModelTimer = setTimeout(pollLocalModel, 1000);
}

async function pollLocalModel() {
  const started = performance.now();
  try {
    state.localModel = await api("/api/local-model");
    renderLocalModel();
    renderDevices();
    if (state.localModel.phase === "ready") {
      toast("이 Mac의 모델이 준비됐습니다.");
      await checkHealth();
    }
  } catch (error) {
    state.localModel = { ...state.localModel, phase: "error", error: error.message };
    renderLocalModel();
    renderDevices();
  } finally {
    state.localModelTimer = null;
    if (localModelBusy()) state.localModelTimer = setTimeout(pollLocalModel, Math.max(0, 1000 - (performance.now() - started)));
  }
}

$("#close-local-model").addEventListener("click", () => $("#local-model-dialog").close());
$('#save-chunk-default').addEventListener('click', async () => {
  const input = document.querySelector('[data-knob=chunk_minutes]');
  if (!input.reportValidity() || state.chunkDefaultSaving) return;
  const id = state.modelDeviceId;
  state.chunkDefaultSaving = true;
  renderLocalModel();
  try {
    const saved = await api(`/api/devices/${encodeURIComponent(id)}/chunk-default`, {
      method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({chunk_minutes:input.valueAsNumber})});
    const device = state.devices.find(item => item.id === id);
    if (device) Object.assign(device, saved);
    if (state.modelMetadata[id]) state.modelMetadata[id].options = {...state.modelMetadata[id].options, chunk_minutes:saved.options.chunk_minutes};
    if (id === state.modelDeviceId) $('#chunk-default-status').textContent = '저장됨 · 다음 노트부터 적용됩니다.';
    renderDevices();
    if ($('#upload-dialog').open) renderFilePreparation();
  } catch (error) { toast(error.message, true); }
  finally { state.chunkDefaultSaving = false; renderLocalModel(); }
});
$("#model-sources").addEventListener("change", renderLocalModel);
$("#choose-model-folder").addEventListener("click", async () => {
  const request = state.modelRequest;
  try {
    const path = await window.webkit.messageHandlers.chooseModelFolder.postMessage({});
    if (path && request === state.modelRequest && state.modelDeviceId === "local") $("#model-folder-path").value = path;
  } catch (error) { toast(error.message || "폴더를 선택하지 못했습니다.", true); }
});
$("#local-model-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (modelBusy(currentModelState()) || state.localModelSubmitting || state.modelLoading) return;
  state.localModelSubmitting = true;
  const source = $('#model-sources input:checked').value;
  const id = state.modelDeviceId;
  const local = id === "local";
  try {
    renderLocalModel();
    await refreshAppSetup(true);
    const result = await api(local ? "/api/local-model/prepare" : `/api/devices/${encodeURIComponent(id)}/model/prepare`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, path: source === "folder" ? $("#model-folder-path").value.trim() : "", model_key: $("#model-choice").value, model_id: $("#model-custom-id").value.trim(), options: readModelKnobs() }),
    });
    const device = state.devices.find(device => device.id === id);
    if (device) { device.model_key = $("#model-choice").value; device.options = readModelKnobs(); device.model_selected = true; }
    if (local) { state.localModel = result; scheduleLocalModelPoll(); }
    else { state.workerStates[id] = { ...state.workerStates[id], ...result }; pollWorker(id); }
    renderDevices();
  } catch (error) {
    if (local) state.localModel = { ...state.localModel, phase: "error", error: error.message };
    else state.workerStates[id] = { ...state.workerStates[id], phase: "error", error: error.message };
  } finally {
    state.localModelSubmitting = false;
    renderLocalModel();
  }
});

function renderHostCandidates() {
  const registered = new Set(state.devices.filter(device => device.kind === "ssh").map(device => device.host));
  const hosts = state.sshHosts.filter(host => !registered.has(host));
  $("#ssh-host-empty").classList.toggle("hidden", !!hosts.length);
  $("#ssh-host-empty").textContent = state.sshHosts.length ? "SSH 설정의 장치가 모두 연결되어 있습니다." : "~/.ssh/config에 Host 별칭을 추가하면 여기에 표시됩니다.";
  $("#ssh-host-candidates").innerHTML = hosts.map(host => `<button class="ssh-host-candidate${$("#ssh-host").value === host ? " is-selected" : ""}" data-ssh-host="${escapeHtml(host)}" type="button" aria-label="${escapeHtml(host)} 장치 선택" aria-pressed="${$("#ssh-host").value === host}"><span class="host-candidate-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8 9 3 3-3 3m5 0h3"/></svg></span><span class="host-candidate-name">${escapeHtml(host)}</span><span class="host-candidate-arrow" aria-hidden="true">→</span></button>`).join("");
}

async function loadHostOptions(preferred) {
  if ($("#device-dialog").open && typeof preferred !== "string" && state.sshHosts.length) return;
  const selected = typeof preferred === "string" ? preferred : $("#ssh-host").value;
  const { hosts } = await api("/api/ssh/hosts");
  state.sshHosts = hosts;
  $("#ssh-host").innerHTML = '<option value="">호스트를 선택하세요</option>' + hosts.map(host => `<option value="${escapeHtml(host)}">${escapeHtml(host)}</option>`).join("");
  if (hosts.includes(selected)) $("#ssh-host").value = selected;
  if (typeof preferred === "string") $("#device-name").value = selected;
  $("#ssh-host-help").textContent = hosts.length ? "" : "~/.ssh/config에 Host 별칭을 추가한 뒤 다시 열어 주세요. Include 파일도 읽습니다.";
  renderHostCandidates();
  updateDeviceRegistration();
}

async function openDevices(host) {
  stopGpuMonitor();
  if (!$("#device-dialog").open) $("#device-dialog").showModal();
  try {
    await loadHostOptions(host);
  } catch (error) {
    $("#ssh-host-empty").classList.remove("hidden");
    $("#ssh-host-empty").textContent = "SSH 설정을 불러오지 못했습니다. 창을 다시 열어 주세요.";
    toast(error.message, true);
  }
}

$("#manage-devices").addEventListener("click", openDevices);
$("#ssh-host-candidates").addEventListener("click", event => {
  const button = event.target.closest("[data-ssh-host]");
  if (button) openDevices(button.dataset.sshHost);
});
$("#refresh-devices").addEventListener("click", () => refreshDevicePanel(true));
$("#close-device-dialog").addEventListener("click", () => $("#device-dialog").close());
$("#device-dialog").addEventListener("close", syncExecutionGpu);
$("#ssh-host").addEventListener("change", () => {
  $("#device-name").value = $("#ssh-host").value;
  renderHostCandidates();
  updateDeviceRegistration();
});

function mib(value) { return value == null ? "확인 불가" : `${value.toFixed(0)} MiB`; }

function updateDeviceRegistration() {
  $("#add-device").disabled = state.addingDevice || !$("#ssh-host").value;
}

async function refreshGpu() {
  const device = executionDevice();
  if (device?.backend !== "cuda" || !$("#upload-dialog").open || $("#device-dialog").open || state.uploading) return;
  clearTimeout(state.gpuTimer);
  state.gpuAbort?.abort();
  const controller = new AbortController();
  state.gpuAbort = controller;
  const request = ++state.sshRequest;
  if (!state.gpuInventory.length) $("#execution-gpu-meta").textContent = `${device.host} · GPU 확인 중…`;
  try {
    const result = await api(`/api/devices/${encodeURIComponent(device.id)}/gpus`, { method: "POST", signal: controller.signal });
    if (request !== state.sshRequest || executionDevice()?.id !== device.id) return;
    if (Number.isFinite(result.chunk_default_minutes)) device.options = {...device.options, chunk_minutes:result.chunk_default_minutes};
    state.gpuInventory = result.gpus;
    state.selectedGpu = result.gpus.find(gpu => gpu.uuid === state.selectedGpu?.uuid) || null;
    if (!state.selectedGpu || state.gpuApproval?.token !== state.selectedGpu.busy_confirmation || state.selectedGpu.idle_candidate) state.gpuApproval = null;
    const focused = $("#gpu-probe-result input:focus")?.value;
    const checked = new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(result.checked_at));
    const rows = result.gpus.map(gpu => {
      const activity = gpu.utilization_percent == null ? "확인 불가" : `${gpu.utilization_percent}%`;
      const processes = gpu.process_info_known ? `${gpu.processes.length}개` : "확인 불가";
      const tooltip = gpu.processes.map(process => `${process.name} (PID ${process.pid})`).join(", ");
      return `<tr><td><label title="${escapeHtml(gpu.name)}"><input type="radio" name="selected-gpu" value="${escapeHtml(gpu.uuid)}" ${state.selectedGpu?.uuid === gpu.uuid ? "checked" : ""}>GPU #${gpu.index}</label><small>${escapeHtml(gpu.name.replace(/^NVIDIA (GeForce )?/, ""))}</small></td>
        <td>${escapeHtml(mib(gpu.memory_used_mib))} / ${escapeHtml(mib(gpu.memory_total_mib))}</td>
        <td>${activity}</td><td title="${escapeHtml(tooltip)}">${processes}</td>
        <td class="${gpu.idle_candidate ? "gpu-idle" : "gpu-occupied"}">${gpu.idle_candidate ? "여유" : "사용 중 · 확인 필요"}</td></tr>`;
    }).join("");
    $("#execution-gpu-meta").textContent = `${device.host} · ${checked} 확인 · 3초마다 갱신`;
    $("#gpu-probe-result").innerHTML = `${rows ? `<div class="gpu-table-wrap"><table class="gpu-table"><thead><tr><th>GPU</th><th>사용 / 전체 VRAM</th><th>사용률</th><th>프로세스</th><th>상태</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <p>${escapeHtml(result.message)}</p>`;
    if (focused) [...$("#gpu-probe-result").querySelectorAll("input")].find(input => input.value === focused && !input.disabled)?.focus({ preventScroll: true });
  } catch (error) {
    if (request !== state.sshRequest || error.name === "AbortError") return;
    state.selectedGpu = null;
    $("#execution-gpu-meta").textContent = `${device.host} · 갱신 재시도 중`;
    $("#gpu-probe-result").textContent = error.message;
  } finally {
    if (request === state.sshRequest) {
      renderFilePreparation();
      if ($("#upload-dialog").open && !$("#device-dialog").open && executionDevice()?.id === device.id && !state.uploading) state.gpuTimer = setTimeout(refreshGpu, 3000);
    }
  }
}

$("#gpu-probe-result").addEventListener("change", async event => {
  if (event.target.name !== "selected-gpu") return;
  const deviceId = state.gpuDeviceId;
  const previous = state.selectedGpu;
  const gpu = state.gpuInventory.find(gpu => gpu.uuid === event.target.value);
  if (!gpu) return;
  if (!gpu.idle_candidate) {
    stopGpuMonitor();
    const allowed = await confirmBusyGpu(gpu);
    if (state.gpuDeviceId !== deviceId || !$("#upload-dialog").open) return;
    if (!allowed) {
      state.selectedGpu = previous;
      await refreshGpu();
      return;
    }
    state.gpuApproval = { uuid: gpu.uuid, token: gpu.busy_confirmation };
  } else state.gpuApproval = null;
  state.selectedGpu = gpu;
  renderFilePreparation();
  if (!gpu.idle_candidate) refreshGpu();
});

function confirmBusyGpu(gpu) {
  if (state.gpuWarningResolve) return Promise.resolve(false);
  $("#gpu-warning-description").textContent = `GPU #${gpu.index} · ${gpu.name} · VRAM ${mib(gpu.memory_used_mib)} / ${mib(gpu.memory_total_mib)} 사용 중`;
  $("#gpu-warning-processes").innerHTML = gpu.processes?.length
    ? '<p>다음 프로세스가 이 GPU를 사용 중입니다.</p>' + gpu.processes.map(process => `<p><strong>${escapeHtml(process.name || '이름 확인 불가')}</strong> · PID ${escapeHtml(process.pid || '확인 불가')} · ${escapeHtml(mib(process.memory_mib))}</p>`).join('')
    : '<p>GPU 사용 프로세스를 확인할 수 없거나, 사용률·메모리가 여유 기준을 넘었습니다.</p>';
  $("#gpu-warning-dialog").showModal();
  $("#cancel-gpu-warning").focus({ preventScroll: true });
  return new Promise(resolve => { state.gpuWarningResolve = resolve; });
}
function closeGpuWarning(allowed = false) {
  const resolve = state.gpuWarningResolve;
  state.gpuWarningResolve = null;
  if ($("#gpu-warning-dialog").open) $("#gpu-warning-dialog").close();
  resolve?.(allowed);
}
$("#confirm-gpu-warning").addEventListener("click", () => closeGpuWarning(true));
$("#cancel-gpu-warning").addEventListener("click", () => closeGpuWarning(false));
$("#close-gpu-warning").addEventListener("click", () => closeGpuWarning(false));
bindConfirmationDialog("#gpu-warning-dialog", () => closeGpuWarning(false));

$("#device-backend").addEventListener("change", () => {
  const managed = $("#device-backend").value === "cuda";
  $("#device-model").value = $("#device-backend").value === "mlx" ? "moss-mtd" : "OpenMOSS-Team/MOSS-Transcribe-Diarize";
  $("#device-cache-field").classList.toggle("hidden", managed);
  $("#device-cache").disabled = managed;
  $("#device-model-field").classList.toggle('hidden', managed);
  $("#device-model").disabled = managed;
  $("#device-model").required = !managed;
  $("#worker-setup-help").textContent = managed
    ? "장치만 연결합니다. 연결 후 왼쪽에서 전사 모델을 선택하고 다운로드하세요."
    : "이 기기에서 직접 실행한 모델에 연결합니다. 처리 장치는 해당 기기의 설정을 따릅니다.";
});

$("#add-device-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.addingDevice) return;
  const host = $("#ssh-host").value;
  if (!host) return;
  const values = Object.fromEntries(new FormData(event.target));
  values.host = host;
  values.remote_port = Number(values.remote_port);
  state.addingDevice = true;
  updateDeviceRegistration();
  try {
    const device = await api("/api/devices", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
    await loadDevices();
    $("#device-dialog").close();
    toast("장치를 연결했습니다. 왼쪽에서 모델을 선택해 주세요.");
  } catch (error) { toast(error.message, true); }
  finally { state.addingDevice = false; updateDeviceRegistration(); }
});

$("#registered-devices").addEventListener("click", async event => {
  const button = event.target.closest("[data-device-action]");
  if (!button) return;
  const id = button.closest("[data-device-id]").dataset.deviceId;
  if (button.dataset.deviceAction === "use") return openUpload(id);
  if (button.dataset.deviceAction === "environment") return openEnvironment(id);
  if (["local-model", "install"].includes(button.dataset.deviceAction)) return openLocalModel(id);
  button.disabled = true;
  try {
    if (button.dataset.deviceAction === 'download') {
      const device = state.devices.find(item => item.id === id);
      if (device.model_selected === false) return toast('모델을 먼저 선택해 주세요.',true);
      const key = device.options?.speech_pipeline;
      let result;
      if (device.backend === 'cuda' && managedSpeechPipeline(key)) {
        result = await api(`/api/devices/${encodeURIComponent(id)}/speech-setup`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pipeline:key})});
      } else {
        const source = id === 'local' ? state.localModel : device;
        result = await api(id === 'local' ? '/api/local-model/prepare' : `/api/devices/${encodeURIComponent(id)}/model/prepare`,
          {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_key:device.model_key || (id === 'local' ? 'mlx-8bit' : 'cuda-original'),source:source?.source || source?.model_source || 'cache',path:source?.model_path || ''})});
      }
      if (id === 'local') { state.localModel = result; scheduleLocalModelPoll(); }
      else { state.workerStates[id] = result; pollWorker(id); }
      renderDevices();
    } else if (button.dataset.deviceAction === "stop") {
      state.workerStates[id] = await api(`/api/devices/${encodeURIComponent(id)}/worker/${button.dataset.deviceAction}`, { method: "POST" });
      renderDevices();
      pollWorker(id);
    } else if (button.dataset.deviceAction === "remove") {
      await api(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
      clearTimeout(state.workerTimers[id]);
      delete state.workerTimers[id];
      delete state.workerStates[id];
      delete state.deviceStatus[id];
      await loadDevices();
      syncExecutionGpu();
      toast("장치를 목록에서 제거했습니다.");
    } else {
      state.deviceStatus[id] = await api(`/api/devices/${encodeURIComponent(id)}/status`, { method: "POST" });
      renderDevices();
    }
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

async function pollWorker(id) {
  if (state.workerPolling.has(id)) return;
  state.workerPolling.add(id);
  const revision = state.deviceModelRevisions[id] || 0;
  const started = performance.now();
  clearTimeout(state.workerTimers[id]);
  delete state.workerTimers[id];
  try {
    let worker = await api(`/api/devices/${encodeURIComponent(id)}/worker`);
    if (worker.phase === "unknown") {
      const status = await api(`/api/devices/${encodeURIComponent(id)}/status`, { method: "POST" });
      worker = status.setup || { phase: "error", error: status.error };
    }
    if (revision !== (state.deviceModelRevisions[id] || 0)) return;
    state.workerStates[id] = { ...state.workerStates[id], ...worker };
    renderDevices();
    if (state.modelDeviceId === id) renderLocalModel();
    if (["unselected", "idle", "installed", "stopped", "error", "interrupted"].includes(worker.phase)) {
      delete state.workerTimers[id];
      return;
    }
    state.workerTimers[id] = setTimeout(() => pollWorker(id), Math.max(0, 1000 - (performance.now() - started)));
  } catch (error) {
    delete state.workerTimers[id];
    state.workerStates[id] = { ...state.workerStates[id], phase: "error", error: error.message };
    renderDevices();
    if (state.modelDeviceId === id) renderLocalModel();
  } finally {
    state.workerPolling.delete(id);
    if (revision !== (state.deviceModelRevisions[id] || 0)) pollWorker(id);
  }
}

$("#output-format").addEventListener("change", () => {
  try { localStorage.setItem("cosmoss.outputFormat", $("#output-format").value); } catch (_) {}
});

async function checkHealth() {
  if (state.healthChecking) return;
  state.healthChecking = true;
  try {
    const health = await api("/api/health");
    state.deviceStatus.local = health.moss;
    state.maxUploadBytes = health.max_upload_bytes;
    const defaultMinutes = health.default_chunk_seconds / 60;
    if (Number.isInteger(defaultMinutes) && defaultMinutes >= 1 && defaultMinutes <= 90) {
      state.defaultChunkMinutes = defaultMinutes;
    }
    if (!state.uploadPreparation && !fileInput.files.length) resetDropZone();
  } catch (_) { state.deviceStatus.local = { ok: false }; }
  finally { state.healthChecking = false; renderDevices(); }
}

async function refreshDevicePanel(manual = false) {
  if (state.panelRefreshing || (!manual && document.hidden)) return;
  state.panelRefreshing = true;
  $("#refresh-devices").disabled = true;
  try {
    const results = await Promise.allSettled([loadDevices(), checkHealth()]);
    if (results[0].status === "rejected") {
      $("#device-panel-summary").textContent = "장치 목록을 다시 확인해 주세요.";
      $("#device-panel-summary").classList.remove("hidden");
      if (manual) toast(results[0].reason.message, true);
      return;
    }
    await Promise.allSettled(state.devices.filter(device => device.kind === "ssh" && device.backend !== "cuda").map(async device => {
      state.deviceStatus[device.id] = await api(`/api/devices/${encodeURIComponent(device.id)}/status`, { method: "POST" });
    }));
    if (manual) await Promise.allSettled(state.devices.filter(device => device.kind === "local" || device.backend === "cuda").map(device => checkEnvironment(device.id)));
    renderDevices();
  } finally {
    state.panelRefreshing = false;
    $("#refresh-devices").disabled = false;
  }
}
window.addEventListener("beforeunload", event => {
  if (state.pendingChanges || state.savePromise || state.uploading || window.cosmossVoiceProfiles?.busy) event.preventDefault();
});
const saveLocationReady = loadSaveLocation();
refreshAppSetup();
loadModelCatalog().catch(error => toast(error.message, true));
loadNotes();
setInterval(() => { if (!document.hidden && !state.libraryLoading) loadNotes(); }, 5000);
loadSummaryProviders();
loadSummaryDefaultPrompt().catch(() => {});
renderSummary();
refreshDevicePanel();
setInterval(() => refreshDevicePanel(), 10000);

function renderTextFileActions() {
  const note = state.current;
  $('#open-diarization').disabled = note?.status !== 'done' || note?.source_type === 'text_file';
  $('#open-diarization').title = note?.source_type === 'text_file' ? '원본 녹음이 보관된 노트에서 사용할 수 있습니다.' : '';
  for (const selector of ["#retry-result-save", "#reveal-txt", "#edit-speakers"]) $(selector).disabled = note?.status !== "done";
  if (note?.source_type === "text_file") $("#edit-speakers").disabled = !note.segments.length;
  $("#retry-result-save").classList.toggle("hidden", note?.status !== "done" || !!note.text_path);
  $("#text-file-location").textContent = note?.text_save_error || note?.text_path || "";
  $("#text-file-location").title = note?.text_path || "";
}

async function ensureTextFile() {
  const id = state.current?.id;
  if (!id || state.current.status !== "done") throw new Error("전사가 완료된 노트를 선택해 주세요.");
  await flushSave();
  await saveLocationReady;
  if (state.current?.id === id && state.current.text_path) return state.current;
  const note = await api(`/api/notes/${encodeURIComponent(id)}/text-file`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ directory: state.saveDirectory }),
  });
  if (state.current?.id === id) {
    state.current = note.source_type === "text_file" ? note : { ...state.current, text_path: note.text_path, text_hash: note.text_hash, text_save_error: null };
    if (note.source_type === "text_file") renderEditor();
    renderTextFileActions();
  }
  return note;
}

$("#retry-result-save").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await ensureTextFile(); toast("파일을 저장했습니다."); }
  catch (error) { toast(error.message, true); }
  finally { renderTextFileActions(); }
});
$("#reveal-txt").addEventListener("click", async () => {
  try {
    const note = await ensureTextFile();
    const bridge = window.webkit?.messageHandlers?.revealTextFile;
    if (bridge) await bridge.postMessage({ path: note.text_path });
    else toast(`저장 위치: ${note.text_path}`);
  } catch (error) { toast(error.message, true); }
});

const speakerPreviewPlayback = (() => {
  const player = $('#speaker-preview-player');
  const playIcon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9-5.5z"/></svg>';
  const stopIcon = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1"/></svg>';
  let active = null, request = 0, timer = null;
  function render() {
    for (const button of $('#speaker-preview').querySelectorAll('[data-speaker-play]')) {
      const selected = active?.index === Number(button.dataset.speakerPlay);
      button.setAttribute('aria-pressed', String(selected));
      button.setAttribute('aria-label', `${button.dataset.playbackTime} 발화 ${selected ? '재생 정지' : '재생'}`);
      button.innerHTML = `${selected ? stopIcon : playIcon}<span>${selected ? (active.loading ? '로딩…' : '정지') : '재생'}</span>`;
    }
  }
  function stop(release = false) {
    ++request;
    active = null;
    clearTimeout(timer);
    player.onloadedmetadata = null;
    player.pause();
    render();
    if (release) { player.removeAttribute('src'); player.load(); }
  }
  function fail(token) {
    if (token !== request || !active) return;
    stop(true);
    toast('이 발화를 재생하지 못했습니다. 녹음 파일과 발화 시간을 확인해 주세요.', true);
  }
  function checkEnd() {
    clearTimeout(timer);
    if (!active || player.paused) return;
    const remaining = active.end - player.currentTime;
    if (remaining <= .015) return stop();
    timer = setTimeout(checkEnd, Math.min(80, Math.max(10, remaining * 1000)));
  }
  function play(index) {
    const draft = state.speakerDraft, segment = draft?.segments[index];
    if (!draft?.hasAudio || state.speakerSaving || !segment || !Number.isFinite(segment.start)
        || !Number.isFinite(segment.end) || segment.start < 0 || segment.end <= segment.start) return;
    if (active?.index === index) return stop();
    stop();
    for (const media of document.querySelectorAll('audio, video')) if (media !== player) media.pause();
    const token = request;
    active = {index, start: segment.start, end: segment.end, loading: true};
    render();
    const seek = () => {
      if (token !== request || !active) return;
      if (Number.isFinite(player.duration)) active.end = Math.min(active.end, player.duration);
      if (active.end <= active.start) return fail(token);
      try { player.currentTime = active.start; } catch (_) { fail(token); }
    };
    player.onloadedmetadata = seek;
    const url = `/api/notes/${encodeURIComponent(draft.id)}/media?playback=true`;
    if (player.getAttribute('src') !== url) { player.src = url; player.load(); }
    else if (player.readyState >= 1) seek();
    if (token !== request) return;
    // Keep play() in the button's user gesture for native WebKit. Metadata
    // positions the recording before it can play; opening/navigating never plays.
    player.play().catch(() => fail(token));
  }
  player.addEventListener('playing', () => {
    if (!active) return;
    active.loading = false;
    render();
    checkEnd();
  });
  player.addEventListener('timeupdate', () => {
    if (active && player.currentTime >= active.end) stop();
  });
  player.addEventListener('ended', () => { if (player.ended) stop(); });
  player.addEventListener('pause', () => { if (active && player.paused) stop(); });
  player.addEventListener('error', () => { if (player.error) fail(request); });
  document.addEventListener('play', event => { if (event.target !== player && active) stop(); }, true);
  $('#speaker-preview').addEventListener('click', event => {
    const button = event.target.closest('[data-speaker-play]');
    if (button && !button.disabled) play(Number(button.dataset.speakerPlay));
  });
  return {stop, render, play};
})();

function renderSpeakerDialog() {
  const draft = state.speakerDraft;
  if (!draft) return;
  speakerPreviewPlayback.stop(true);
  $("#speaker-choices").innerHTML = draft.speakers.map(speaker => `<button type="button" class="speaker-choice" draggable="${draft.speakers.length > 1}" data-select-speaker="${escapeHtml(speaker)}" aria-pressed="${speaker === draft.selected}" aria-describedby="speaker-merge-help"><small>${escapeHtml(speaker)} · ${draft.segments.filter(s => s.speaker === speaker).length}개 발화</small><strong>${escapeHtml(draft.names[speaker] || speaker)}</strong></button>`).join("");
  $("#speaker-name-label").textContent = `${draft.selected} 이름`;
  $("#speaker-name").value = draft.names[draft.selected] || "";
  draft.utterances = draft.segments.flatMap((segment, index) => segment.speaker === draft.selected ? [index] : []);
  draft.utterancePosition = 0;
  $("#speaker-navigation-help").textContent = "↑·↓ 발화 이동 · Space 재생·정지";
  $("#speaker-preview").innerHTML = draft.segments.map((segment, index) => {
    const playable = draft.hasAudio && Number.isFinite(segment.start) && Number.isFinite(segment.end) && segment.start >= 0 && segment.end > segment.start;
    return `<article class="speaker-preview-row ${segment.speaker === draft.selected ? "highlighted" : ""}" data-preview-index="${index}" data-preview-speaker="${escapeHtml(segment.speaker)}"><div><span>${clock(segment.start)}</span><span data-preview-name>${escapeHtml(draft.names[segment.speaker] || segment.speaker)}</span><button class="speaker-preview-play" type="button" data-speaker-play="${index}" data-playback-time="${clock(segment.start)}" title="${playable ? '이 발화만 재생' : draft.hasAudio ? '재생할 발화 시간이 없습니다.' : '원본 녹음이 없는 텍스트 파일은 재생할 수 없습니다.'}" ${playable ? '' : 'disabled'}></button></div><p>${escapeHtml(segment.text)}</p></article>`;
  }).join("");
  speakerPreviewPlayback.render();
}

function goToSpeakerUtterance(position, focus = false) {
  const draft = state.speakerDraft;
  if (!draft?.utterances.length) return;
  draft.utterancePosition = Math.max(0, Math.min(position, draft.utterances.length - 1));
  const preview = $("#speaker-preview");
  const row = preview.querySelector(`[data-preview-index="${draft.utterances[draft.utterancePosition]}"]`);
  preview.querySelectorAll('.is-current').forEach(item => { item.classList.remove('is-current'); item.removeAttribute('aria-current'); });
  row.classList.add('is-current');
  row.setAttribute('aria-current', 'true');
  if (focus) preview.focus({ preventScroll: true });
  const top = preview.scrollTop + row.getBoundingClientRect().top - preview.getBoundingClientRect().top - 6;
  preview.scrollTo({ top, behavior: 'instant' });
  $("#speaker-navigation-help").textContent = `↑·↓ 발화 이동 · Space 재생·정지 · ${draft.utterancePosition + 1}/${draft.utterances.length}`;
}

async function openSpeakerDialog(selected) {
  if (state.current?.status !== "done" || state.speakerSaving) return;
  try {
    await flushSave();
    const note = state.current;
    if (note?.status !== "done") return;
    const speakers = [...new Set([...note.segments, ...(note.raw_segments || [])].map(s => note.speaker_merges?.[s.speaker] || s.speaker))];
    if (!speakers.length) return toast("지정할 화자가 없습니다.");
    const voiceResult = note.voice_matches?.length || note.voice_merge_candidates?.length
      ? {matches: note.voice_matches || [], merge_candidates: note.voice_merge_candidates || []} : null;
    state.speakerDraft = { id: note.id, hasAudio: note.source_type !== 'text_file', speakers, selected: speakers.includes(selected) ? selected : speakers[0], names: structuredClone(note.speaker_names), segments: structuredClone(note.segments), merges: {}, voiceResult };
    $('#media-player').pause();
    $("#speaker-save-error").classList.add("hidden");
    renderSpeakerDialog();
    $("#speaker-dialog").showModal();
    $("#identify-voice-speakers").disabled = note.source_type === 'text_file';
    $("#identify-voice-speakers").textContent = voiceResult ? '목소리 추천 검토' : '목소리 비교·추천';
    $("#speaker-voice-status").textContent = note.voice_match_error || (voiceResult ? '목소리 비교 결과가 있습니다. 추천을 검토하고 승인해 주세요.' : '');
    $("#speaker-voice-status").classList.toggle('hidden', !$("#speaker-voice-status").textContent);
    goToSpeakerUtterance(0, true);
  } catch (error) { toast(error.message, true); }
}
$("#edit-speakers").addEventListener("click", () => openSpeakerDialog());
async function compareSpeakerVoices(refresh = false) {
  const draft = state.speakerDraft;
  if (!draft || state.speakerSaving || state.voiceIdentifying) return;
  speakerPreviewPlayback.stop(true);
  if (draft.voiceResult && !refresh) return voiceReview.open(draft.voiceResult);
  state.voiceIdentifying = true;
  const button = $("#identify-voice-speakers");
  button.disabled = true;
  button.textContent = '목소리 비교 중…';
  $('#save-speakers').disabled = true;
  try {
    const result = await api(`/api/notes/${encodeURIComponent(draft.id)}/identify-speakers`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({speaker_merges: draft.merges})});
    if (state.speakerDraft !== draft) return;
    draft.voiceResult = result;
    $("#speaker-voice-status").textContent = '비교가 완료되었습니다. 승인할 추천을 선택해 주세요.';
    $("#speaker-voice-status").classList.remove('hidden');
    state.voiceIdentifying = false;
    voiceReview.open(result);
  } catch (error) { if (state.speakerDraft === draft) toast(error.message, true); }
  finally { state.voiceIdentifying = false; $('#save-speakers').disabled = state.speakerSaving; button.disabled = state.current?.source_type === 'text_file'; button.textContent = state.speakerDraft?.voiceResult ? '목소리 추천 검토' : '목소리 비교·추천'; }
}
$("#identify-voice-speakers").addEventListener('click', () => compareSpeakerVoices());
let speakerDrag = null;
let speakerMerge = null;
function clearSpeakerDrag() {
  speakerDrag = null;
  document.querySelectorAll(".speaker-choice.is-dragging, .speaker-choice.is-drop-target").forEach(button => button.classList.remove("is-dragging", "is-drop-target"));
}
function closeSpeakerMerge() {
  speakerMerge = null;
  if ($("#speaker-merge-dialog").open) $("#speaker-merge-dialog").close();
}
function requestSpeakerMerge(source, target) {
  const draft = state.speakerDraft;
  if (!draft || state.speakerSaving || state.voiceIdentifying || source === target || !draft.speakers.includes(source) || !draft.speakers.includes(target)) return;
  speakerPreviewPlayback.stop();
  const label = speaker => {
    const name = draft.names[speaker]?.trim() || speaker;
    return name === speaker ? name : `${name} (${speaker})`;
  };
  speakerMerge = { id: draft.id, source, target };
  $("#speaker-merge-question").textContent = `‘${label(source)}’와 ‘${label(target)}’를 병합하시겠습니까?`;
  $("#speaker-merge-detail").textContent = `‘${label(source)}’의 발화를 ‘${label(target)}’로 합칩니다. 마지막에 ‘저장’을 누르면 파일에 반영됩니다.`;
  $("#speaker-merge-dialog").showModal();
  $("#speaker-merge-title").focus({ preventScroll: true });
}
$("#speaker-choices").addEventListener("dragstart", event => {
  const button = event.target.closest("[data-select-speaker]");
  const draft = state.speakerDraft;
  if (!button || !draft || state.speakerSaving || draft.speakers.length < 2) return event.preventDefault();
  speakerDrag = { id: draft.id, source: button.dataset.selectSpeaker };
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("application/x-cosmoss-speaker", button.dataset.selectSpeaker);
  event.dataTransfer.setData("text/plain", button.dataset.selectSpeaker);
  button.classList.add("is-dragging");
});
$("#speaker-choices").addEventListener("dragover", event => {
  const button = event.target.closest("[data-select-speaker]");
  if (!button || !speakerDrag || state.speakerSaving || speakerDrag.id !== state.speakerDraft?.id || button.dataset.selectSpeaker === speakerDrag.source) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  document.querySelectorAll(".speaker-choice.is-drop-target").forEach(other => { if (other !== button) other.classList.remove("is-drop-target"); });
  button.classList.add("is-drop-target");
});
$("#speaker-choices").addEventListener("dragleave", event => {
  const button = event.target.closest("[data-select-speaker]");
  if (button && !button.contains(event.relatedTarget)) button.classList.remove("is-drop-target");
});
$("#speaker-choices").addEventListener("drop", event => {
  const button = event.target.closest("[data-select-speaker]");
  const drag = speakerDrag;
  event.preventDefault();
  clearSpeakerDrag();
  if (button && drag && drag.id === state.speakerDraft?.id) requestSpeakerMerge(drag.source, button.dataset.selectSpeaker);
});
$("#speaker-choices").addEventListener("dragend", clearSpeakerDrag);
for (const id of ["#close-speaker-merge", "#cancel-speaker-merge"]) $(id).addEventListener("click", closeSpeakerMerge);
bindConfirmationDialog("#speaker-merge-dialog", closeSpeakerMerge);
$("#confirm-speaker-merge").addEventListener("click", () => {
  const pending = speakerMerge;
  const draft = state.speakerDraft;
  closeSpeakerMerge();
  if (!pending || !draft || state.speakerSaving || state.voiceIdentifying || pending.id !== draft.id || !draft.speakers.includes(pending.source) || !draft.speakers.includes(pending.target)) return;
  draft.merges[pending.source] = pending.target;
  for (const source of Object.keys(draft.merges)) if (draft.merges[source] === pending.source) draft.merges[source] = pending.target;
  draft.segments = draft.segments.map(segment => segment.speaker === pending.source ? { ...segment, speaker: pending.target } : segment);
  draft.speakers = draft.speakers.filter(speaker => speaker !== pending.source);
  draft.selected = pending.target;
  draft.voiceResult = null;
  $('#identify-voice-speakers').textContent = '목소리 비교·추천';
  renderSpeakerDialog();
  goToSpeakerUtterance(0);
  $("#speaker-name").focus({ preventScroll: true });
});
$("#speaker-choices").addEventListener("click", event => {
  const button = event.target.closest("[data-select-speaker]");
  if (!button || !state.speakerDraft || state.speakerSaving) return;
  state.speakerDraft.selected = button.dataset.selectSpeaker;
  renderSpeakerDialog();
  goToSpeakerUtterance(0, true);
});
$("#speaker-dialog").addEventListener("keydown", event => {
  if (!['ArrowUp', 'ArrowDown', ' '].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing || event.defaultPrevented) return;
  if (!state.speakerDraft || state.speakerSaving || $("#speaker-merge-dialog").open || event.target.closest('input, textarea, select, [contenteditable]')) return;
  if (event.key === ' ') {
    // Let focused buttons retain their normal Space activation. Preview focus
    // follows arrow navigation, so Space always plays the current utterance.
    if (event.target.closest('button') || !event.target.closest('#speaker-preview')) return;
    event.preventDefault();
    if (!event.repeat) speakerPreviewPlayback.play(state.speakerDraft.utterances[state.speakerDraft.utterancePosition]);
    return;
  }
  event.preventDefault();
  speakerPreviewPlayback.stop();
  goToSpeakerUtterance(state.speakerDraft.utterancePosition + (event.key === 'ArrowDown' ? 1 : -1), true);
});
$("#speaker-name").addEventListener("input", event => {
  const draft = state.speakerDraft;
  if (!draft || state.speakerSaving) return;
  draft.names[draft.selected] = event.target.value;
  for (const choice of document.querySelectorAll("[data-select-speaker]")) if (choice.dataset.selectSpeaker === draft.selected) choice.querySelector("strong").textContent = event.target.value.trim() || draft.selected;
  for (const row of document.querySelectorAll("[data-preview-speaker]")) if (row.dataset.previewSpeaker === draft.selected) row.querySelector("[data-preview-name]").textContent = event.target.value.trim() || draft.selected;
});
for (const id of ["#cancel-speakers", "#close-speakers"]) $(id).addEventListener("click", () => { if (!state.speakerSaving) $("#speaker-dialog").close(); });
$("#speaker-dialog").addEventListener("cancel", event => { if (state.speakerSaving) event.preventDefault(); });
$("#speaker-dialog").addEventListener("close", () => { speakerPreviewPlayback.stop(true); clearSpeakerDrag(); closeSpeakerMerge(); voiceReview.close(); state.speakerDraft = null; });
$("#save-speakers").addEventListener("click", async () => {
  const draft = state.speakerDraft;
  if (!draft || state.speakerSaving || state.voiceIdentifying) return;
  speakerPreviewPlayback.stop(true);
  state.speakerSaving = true;
  const controls = [...$("#speaker-dialog").querySelectorAll("button, input")];
  const disabledBeforeSave = new Map(controls.map(control => [control, control.disabled]));
  controls.forEach(control => control.disabled = true);
  $("#save-speakers").textContent = "저장 중…";
  $("#speaker-save-error").classList.add("hidden");
  try {
    await saveLocationReady;
    const note = await api(`/api/notes/${encodeURIComponent(draft.id)}/speakers`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ speaker_names: draft.names, speaker_merges: draft.merges, directory: state.saveDirectory, ...(state.current?.source_type === "text_file" ? { text_hash: state.current.text_hash } : {}) }) });
    if (state.current?.id === draft.id) { state.current = note; renderEditor(); renderTextFileActions(); renderSummary(); }
    $("#speaker-dialog").close();
    toast("화자 설정과 파일을 저장했습니다.");
    await loadNotes();
  } catch (error) { $("#speaker-save-error").textContent = error.message; $("#speaker-save-error").classList.remove("hidden"); }
  finally { state.speakerSaving = false; controls.forEach(control => control.disabled = disabledBeforeSave.get(control)); $("#save-speakers").textContent = "저장"; }
});

async function loadSummaryDefaultPrompt() {
  const result = await api("/api/summary/default-prompt");
  state.summaryDefaultPrompt = result.default_prompt;
  $("#default-summary-prompt-content").textContent = result.default_prompt;
}
function closeSummaryPromptDialog() {
  const dialog = $("#default-summary-prompt-dialog");
  if (!dialog.open) return;
  dialog.close();
  $("#show-default-summary-prompt").focus({preventScroll:true});
}
$("#show-default-summary-prompt").addEventListener("click", async () => {
  const dialog = $("#default-summary-prompt-dialog");
  if (!dialog.open) dialog.showModal();
  $("#default-summary-prompt-title").focus({preventScroll:true});
  if (!state.summaryDefaultPrompt) {
    try { await loadSummaryDefaultPrompt(); }
    catch (_) { $("#default-summary-prompt-content").textContent = "기본 프롬프트를 불러오지 못했습니다. 창을 닫고 다시 시도해 주세요."; }
  }
});
$("#close-default-summary-prompt").addEventListener("click", closeSummaryPromptDialog);
bindConfirmationDialog("#default-summary-prompt-dialog", closeSummaryPromptDialog);

async function loadSummaryProviders() {
  $("#refresh-summary-providers").disabled = true;
  try { state.summaryProviders = await api("/api/summary/providers"); }
  catch (error) { toast(error.message, true); }
  finally { $("#refresh-summary-providers").disabled = false; renderSummary(); }
}
$("#refresh-summary-providers").addEventListener("click", loadSummaryProviders);
$("#summary-provider").addEventListener("change", () => {
  localStorage.setItem("cosmoss.summaryProvider", $("#summary-provider").value);
  renderSummary();
});
try { const provider = localStorage.getItem("cosmoss.summaryProvider"); if (["codex", "claude"].includes(provider)) $("#summary-provider").value = provider; } catch (_) {}
function fitSummaryPrompt() {
  const input = $("#summary-prompt"), scroll = $(".summary-scroll");
  if (!input.clientWidth) return;
  const top = scroll.scrollTop;
  input.style.height = "auto";
  const style = getComputedStyle(input);
  input.style.height = `${Math.ceil(input.scrollHeight + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth))}px`;
  scroll.scrollTop = top;
}
let summaryPromptWidth = 0;
let summaryPromptResizeFrame = null;
new ResizeObserver(([entry]) => {
  if (entry.contentRect.width !== summaryPromptWidth) {
    summaryPromptWidth = entry.contentRect.width;
    if (summaryPromptResizeFrame === null) summaryPromptResizeFrame = requestAnimationFrame(() => {
      summaryPromptResizeFrame = null;
      fitSummaryPrompt();
    });
  }
}).observe($("#summary-prompt"));
function saveSummaryDraft() {
  const draft = {prompt:$("#summary-prompt").value, useDefault:$("#use-default-summary-prompt").checked};
  if (state.current) state.summaryDrafts[state.current.id] = draft;
  else state.summaryTemplate = draft;
  renderSummary();
  fitSummaryPrompt();
}
$("#summary-prompt").addEventListener("input", saveSummaryDraft);
$("#use-default-summary-prompt").addEventListener("change", saveSummaryDraft);

function summaryPromptOptions(result) {
  if (!result?.prompt && !result?.result?.prompt) return {...state.summaryTemplate};
  const prompt = result.prompt ?? result.result?.prompt;
  const useDefault = result.use_default_prompt ?? (prompt === result.default_prompt);
  return {useDefault, prompt:result.additional_prompt ?? (useDefault ? "" : prompt)};
}

function showSummaryDraft(draft) {
  $("#summary-prompt").value = draft.prompt || "";
  $("#use-default-summary-prompt").checked = draft.useDefault;
}

function selectSummaryNote(note) {
  const id = note?.id || null;
  if (state.summaryNoteId !== id) {
    clearTimeout(state.summaryTimer);
    ++state.summaryRequest;
    state.summaryNoteId = id;
    $("#corrected-transcript-preview").open = false;
    showSummaryDraft(id ? state.summaryDrafts[id] ?? summaryPromptOptions(state.summaryStates[id]) : state.summaryTemplate);
  }
  renderSummary();
  if (id) pollSummary(id);
}

async function pollSummary(id) {
  clearTimeout(state.summaryTimer);
  const request = ++state.summaryRequest;
  try {
    const result = await api(`/api/notes/${encodeURIComponent(id)}/summary`);
    if (request !== state.summaryRequest || state.current?.id !== id) return;
    state.summaryStates[id] = result;
    if (!(id in state.summaryDrafts)) {
      state.summaryDrafts[id] = summaryPromptOptions(result);
      showSummaryDraft(state.summaryDrafts[id]);
    }
    renderSummary();
    if (result.phase === "running") state.summaryTimer = setTimeout(() => pollSummary(id), 1000);
  } catch (error) {
    if (request !== state.summaryRequest || state.current?.id !== id) return;
    $("#summary-status").textContent = `요약 상태 확인 실패: ${error.message}`;
    $("#summary-status").className = "summary-status error";
    state.summaryTimer = setTimeout(() => pollSummary(id), 3000);
  }
}

function renderSummary() {
  const note = state.current;
  const info = state.summaryProviders.find(item => item.id === $("#summary-provider").value);
  const summary = note ? state.summaryStates[note.id] : null;
  const running = summary?.phase === "running";
  const useDefault = $("#use-default-summary-prompt").checked;
  if (note) $("#delete-note").disabled = running || state.summarySubmitting || Boolean(state.cancellingId || state.restartingId || state.deletingId) || ["queued", "processing"].includes(note.status);
  $("#summary-provider-status").textContent = info ? `${info.name} · ${info.message}` : "CLI 연결을 확인하고 있습니다…";
  $("#summary-provider-status").classList.toggle("ready", Boolean(info?.ready));
  $("#cli-setup").classList.toggle("hidden", !info || info.ready);
  if (info && !info.ready) {
    $("#cli-setup-copy").textContent = info.installed ? "터미널에서 로그인한 뒤 ‘연결 확인’을 눌러 주세요." : "터미널에서 CLI를 설치하고 로그인해 주세요.";
    $("#cli-setup-command").textContent = info.installed ? info.login : `${info.install}\n${info.login}`;
    $("#cli-docs").href = info.url;
  }
  $("#run-summary").disabled = !info?.ready || note?.status !== "done" || running || state.summarySubmitting || Boolean(state.openingNote) || (!useDefault && !$("#summary-prompt").value.trim());
  $("#run-summary span").textContent = state.openingNote ? "노트 불러오는 중…" : state.summarySubmitting ? "보정·요약 준비 중…" : running ? "기록을 정리하고 있어요…" : summary?.result ? "다시 보정·요약하기" : "전사문 보정·요약하기";
  $("#summary-provider").disabled = running || state.summarySubmitting;
  $("#summary-prompt").disabled = running || state.summarySubmitting;
  $("#use-default-summary-prompt").disabled = running || state.summarySubmitting;
  $("#summary-prompt-label").textContent = useDefault ? "추가 요청" : "요청 내용";
  $("#summary-prompt-optional").classList.toggle("hidden", !useDefault);
  $("#summary-prompt").placeholder = useDefault ? "추가로 요청할 내용을 적어 주세요." : "직접 요청할 내용을 적어 주세요.";
  $("#cancel-summary").classList.toggle("hidden", !running);
  $("#summary-source").textContent = !note ? "먼저 2단계에서 노트를 선택하세요." : note.status !== "done" ? "전사가 완료되면 이 노트를 요약할 수 있습니다." : note.text_path ? note.text_path.split("/").pop() : note.title;
  $("#summary-source").title = note?.status === "done" ? note.text_path || "실행할 때 전사 파일을 기본 경로에 저장합니다." : "";
  const message = summary?.phase === "error" ? summary.error : summary?.phase === "cancelled" ? "보정·요약을 취소했습니다. 다시 실행할 수 있습니다." : running ? `${summary.provider === "claude" ? "Claude" : "Codex"}가 전사문을 보정하고 요약하고 있습니다. 다른 노트를 확인해도 작업은 계속됩니다.` : "";
  $("#summary-status").textContent = message || "";
  $("#summary-status").className = `summary-status${!message ? " hidden" : ""}${running ? " running" : ""}${summary?.phase === "error" ? " error" : ""}`;
  $("#summary-result-wrap").classList.toggle("hidden", !summary?.result);
  $("#summary-result").textContent = summary?.result?.text || "";
  $("#corrected-transcript-preview").classList.toggle("hidden", !summary?.result?.corrected_text);
  $("#corrected-transcript-result").textContent = summary?.result?.corrected_text || "";
  for (const [selector, path] of [["#reveal-summary", summary?.result?.markdown_path], ["#reveal-corrected", summary?.result?.corrected_path]]) {
    $(selector).classList.toggle("hidden", !path);
    $(`${selector} span`).textContent = path ? path.split("/").pop() : "";
    $(selector).title = path ? `${path}\n파인더에서 보기` : "파인더에서 보기";
  }
  if (summary?.result) {
    $("#summary-result-label").textContent = `${summary.result.provider === "claude" ? "Claude" : "Codex"} · ${formatDate(summary.result.completed_at)}`;
    $("#summary-stale").classList.toggle("hidden", !(summary.stale || summary.result.source_hash !== note.text_hash || note.status !== "done"));
  }
  fitSummaryPrompt();
}

$("#run-summary").addEventListener("click", async () => {
  const id = state.current?.id;
  if (!id || state.summarySubmitting) return;
  state.summarySubmitting = true;
  const provider = $("#summary-provider").value;
  const prompt = $("#summary-prompt").value.trim();
  const useDefault = $("#use-default-summary-prompt").checked;
  renderSummary();
  try {
    await flushSave();
    await saveLocationReady;
    const result = await api(`/api/notes/${encodeURIComponent(id)}/summary`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, prompt, use_default_prompt:useDefault, directory: state.saveDirectory }) });
    state.summaryStates[id] = result;
    if (state.current?.id === id) {
      const note = await api(`/api/notes/${encodeURIComponent(id)}`);
      if (state.current?.id === id) {
        state.current = note.source_type === "text_file" ? note : { ...state.current, text_path: note.text_path, text_hash: note.text_hash };
        if (note.source_type === "text_file") renderEditor();
        renderTextFileActions(); pollSummary(id);
      }
    }
  } catch (error) { toast(error.message, true); }
  finally { state.summarySubmitting = false; renderSummary(); }
});
$("#cancel-summary").addEventListener("click", async event => {
  const id = state.current?.id;
  if (!id) return;
  event.currentTarget.disabled = true;
  try {
    const result = await api(`/api/notes/${encodeURIComponent(id)}/summary/cancel`, { method: "POST" });
    state.summaryStates[id] = result;
    if (state.current?.id === id) { clearTimeout(state.summaryTimer); ++state.summaryRequest; renderSummary(); }
  } catch (error) { toast(error.message, true); }
  finally { $("#cancel-summary").disabled = false; }
});
async function revealSummaryFile(key) {
  const path = state.current && state.summaryStates[state.current.id]?.result?.[key];
  if (!path) return;
  const bridge = window.webkit?.messageHandlers?.revealTextFile;
  if (!bridge) return toast(path);
  try { await bridge.postMessage({ path }); }
  catch (error) { toast(error.message || "결과 파일을 찾을 수 없습니다.", true); }
}
$("#reveal-summary").addEventListener("click", () => revealSummaryFile("markdown_path"));
$("#reveal-corrected").addEventListener("click", () => revealSummaryFile("corrected_path"));
$("#copy-corrected").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#corrected-transcript-result").textContent); toast("보정 전사문을 복사했습니다."); }
  catch (_) { toast("복사하지 못했습니다. 보정 전사문을 직접 선택해 복사해 주세요.", true); }
});

$("#copy-summary").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#summary-result").textContent); toast("요약을 복사했습니다."); }
  catch (_) { toast("복사하지 못했습니다. 요약 내용을 직접 선택해 복사해 주세요.", true); }
});

$('#auxiliary-transcript').addEventListener('toggle', async event => {
  if (!event.target.open || $('#auxiliary-transcript-text').textContent) return;
  const id = event.target.dataset.noteId;
  try {
    const value = await api(`/api/notes/${encodeURIComponent(id)}/auxiliary-transcript`);
    if (event.target.dataset.noteId === id) $('#auxiliary-transcript-text').textContent = value.text;
  } catch (error) { toast(error.message,true); }
});
