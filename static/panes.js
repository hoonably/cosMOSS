/* Adjustable workflow columns, with equal defaults and persistent proportions. */
(() => {
  const shell = document.querySelector(".workflow-shell");
  const panes = ["#stage-device", "#stage-review", "#stage-summary"].map(id => document.querySelector(id));
  const splitters = [...shell.querySelectorAll(".stage-splitter")];
  const properties = ["--stage-device-width", "--stage-review-width", "--stage-summary-width"];
  const storageKey = "cosmoss.stageWidths.v1";
  const desktop = matchMedia("(min-width: 951px)");
  const equal = () => [1 / 3, 1 / 3, 1 / 3];
  let proportions = equal();
  let drag = null;
  let resizeFrame = null;

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    const sum = Array.isArray(saved) && saved.reduce((total, value) => total + value, 0);
    if (saved?.length === 3 && saved.every(value => Number.isFinite(value) && value > 0) && Number.isFinite(sum)) proportions = saved.map(value => value / sum);
  } catch (_) {}

  const widths = () => panes.map(pane => pane.getBoundingClientRect().width);
  const minimums = total => [260, 320, 280].map(value => Math.min(value, total / 3));
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function notifyResize() {
    if (resizeFrame !== null) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = null;
      // Refit transcript textareas and reposition any open custom dropdown.
      window.dispatchEvent(new Event("resize"));
    });
  }

  function apply(sizes) {
    const total = sizes.reduce((sum, size) => sum + size, 0);
    sizes.forEach((size, index) => shell.style.setProperty(properties[index], `${size / total}fr`));
    const mins = minimums(total);
    splitters.forEach((splitter, index) => {
      const pair = sizes[index] + sizes[index + 1];
      splitter.setAttribute("aria-valuemin", String(Math.round(100 * mins[index] / pair)));
      splitter.setAttribute("aria-valuemax", String(Math.round(100 * (pair - mins[index + 1]) / pair)));
      splitter.setAttribute("aria-valuenow", String(Math.round(100 * sizes[index] / pair)));
      splitter.setAttribute("aria-valuetext", `${index + 1}단계 ${Math.round(100 * sizes[index] / total)}%, ${index + 2}단계 ${Math.round(100 * sizes[index + 1] / total)}%`);
    });
    notifyResize();
  }

  function layout() {
    if (!desktop.matches) return;
    const total = shell.clientWidth - splitters.reduce((sum, splitter) => sum + splitter.offsetWidth, 0);
    if (total <= 0) return;
    const mins = minimums(total);
    const sizes = [0, 0, 0];
    let free = [0, 1, 2];
    let remaining = total;
    // Only clamp columns that would become unreadable after shrinking the window.
    while (free.length) {
      const weight = free.reduce((sum, index) => sum + proportions[index], 0);
      const constrained = free.filter(index => remaining * proportions[index] / weight < mins[index]);
      if (!constrained.length) {
        free.forEach(index => { sizes[index] = remaining * proportions[index] / weight; });
        break;
      }
      constrained.forEach(index => { sizes[index] = mins[index]; remaining -= mins[index]; });
      free = free.filter(index => !constrained.includes(index));
    }
    apply(sizes);
  }

  function remember() {
    try { localStorage.setItem(storageKey, JSON.stringify(proportions)); } catch (_) {}
  }

  function moveBoundary(index, sizes, delta) {
    const total = sizes.reduce((sum, size) => sum + size, 0);
    const mins = minimums(total);
    const pair = sizes[index] + sizes[index + 1];
    sizes[index] = clamp(sizes[index] + delta, mins[index], pair - mins[index + 1]);
    sizes[index + 1] = pair - sizes[index];
    proportions = sizes.map(size => size / total);
    apply(sizes);
  }

  function finish(cancelled = false) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    previous.splitter.classList.remove("is-dragging");
    document.body.classList.remove("resizing-stages");
    if (previous.splitter.hasPointerCapture(previous.pointerId)) previous.splitter.releasePointerCapture(previous.pointerId);
    if (cancelled) { proportions = previous.proportions; layout(); }
    else remember();
  }

  function reset() {
    finish(true);
    proportions = equal();
    try { localStorage.removeItem(storageKey); } catch (_) {}
    layout();
  }

  splitters.forEach((splitter, index) => {
    splitter.addEventListener("pointerdown", event => {
      if (!desktop.matches || !event.isPrimary || event.button !== 0) return;
      event.preventDefault();
      splitter.focus({ preventScroll: true });
      drag = { splitter, index, pointerId: event.pointerId, x: event.clientX, sizes: widths(), proportions: [...proportions] };
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add("is-dragging");
      document.body.classList.add("resizing-stages");
    });
    splitter.addEventListener("pointermove", event => {
      if (drag?.pointerId === event.pointerId) moveBoundary(drag.index, [...drag.sizes], event.clientX - drag.x);
    });
    splitter.addEventListener("pointerup", event => { if (drag?.pointerId === event.pointerId) finish(); });
    splitter.addEventListener("pointercancel", event => { if (drag?.pointerId === event.pointerId) finish(true); });
    splitter.addEventListener("lostpointercapture", () => finish(true));
    splitter.addEventListener("dblclick", reset);
    splitter.addEventListener("keydown", event => {
      if (!desktop.matches || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "Escape" && drag) { event.preventDefault(); finish(true); return; }
      if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Enter") return reset();
      const delta = event.key === "Home" ? -Infinity : event.key === "End" ? Infinity : (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 60 : 20);
      moveBoundary(index, widths(), delta);
      remember();
    });
  });
  window.addEventListener("blur", () => finish(true));
  new ResizeObserver(() => {
    finish(true);
    layout();
  }).observe(shell);
  desktop.addEventListener("change", () => { finish(true); layout(); });
  layout();
})();
