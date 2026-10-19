/* App-rendered selects. The hidden select remains the form's source of truth. */
(() => {
  const widgets = new Set();
  const registered = new WeakMap();
  let serial = 0;
  let opened = null;
  let typeBuffer = "";
  let typeTimer;
  const chevron = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>';
  const check = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>';

  function optionParts(option) {
    const text = option?.label || "선택하세요";
    return { text: text.replace(/ · 추천$/, ""), recommended: text.endsWith(" · 추천") };
  }
  function key(select) { return select.id || (select.dataset.deviceModel ? `device:${select.dataset.deviceModel}` : null); }
  function enabled(option) { return !!option && !option.disabled && !option.hidden && !option.closest("optgroup[disabled], optgroup[hidden]"); }
  function visible(widget) { return widget.button.isConnected && widget.button.getClientRects().length && !widget.button.disabled; }
  function setText(element, text) { if (element.textContent !== text) element.textContent = text; }

  function sync(widget) {
    const { select, button, value, badge } = widget;
    const option = select.selectedOptions[0];
    const parts = optionParts(option);
    setText(value, parts.text);
    badge.hidden = !parts.recommended;
    const disabled = select.matches(":disabled");
    if (button.disabled !== disabled) button.disabled = disabled;
    button.title = option?.label || "선택하세요";
    button.setAttribute("aria-required", String(select.required));
    const description = select.getAttribute("aria-describedby");
    if (description !== button.getAttribute("aria-describedby")) {
      if (description) button.setAttribute("aria-describedby", description);
      else button.removeAttribute("aria-describedby");
    }
  }

  function close(restoreFocus = false) {
    if (!opened) return;
    const { widget, menu } = opened;
    opened = null;
    menu.remove();
    widget.button.setAttribute("aria-expanded", "false");
    widget.button.removeAttribute("aria-controls");
    widget.button.removeAttribute("aria-activedescendant");
    typeBuffer = "";
    clearTimeout(typeTimer);
    if (restoreFocus && visible(widget)) widget.button.focus({ preventScroll: true });
  }

  function position() {
    if (!opened) return;
    const { widget, menu } = opened;
    if (!visible(widget)) return close();
    const anchor = widget.button.getBoundingClientRect();
    const dialog = widget.select.closest("dialog");
    const bounds = dialog?.getBoundingClientRect();
    const viewport = window.visualViewport;
    const leftEdge = Math.max(8, (bounds?.left || 0) + 8, viewport?.offsetLeft || 0);
    const rightEdge = Math.min(innerWidth - 8, bounds ? bounds.right - 8 : innerWidth, (viewport?.offsetLeft || 0) + (viewport?.width || innerWidth) - 8);
    const topEdge = Math.max(8, (bounds?.top || 0) + 8, viewport?.offsetTop || 0);
    const bottomEdge = Math.min(innerHeight - 8, bounds ? bounds.bottom - 8 : innerHeight, (viewport?.offsetTop || 0) + (viewport?.height || innerHeight) - 8);
    if (anchor.bottom <= topEdge || anchor.top >= bottomEdge) return close();
    const below = bottomEdge - anchor.bottom - 7;
    const above = anchor.top - topEdge - 7;
    const upwards = below < Math.min(menu.scrollHeight, 260) && above > below;
    menu.style.width = `${Math.min(Math.max(anchor.width, 220), rightEdge - leftEdge)}px`;
    menu.style.maxHeight = `${Math.max(44, Math.min(360, upwards ? above : below))}px`;
    menu.style.left = `${Math.max(leftEdge, Math.min(anchor.left, rightEdge - menu.offsetWidth))}px`;
    menu.style.top = `${upwards ? Math.max(topEdge, anchor.top - menu.offsetHeight - 7) : anchor.bottom + 7}px`;
    menu.dataset.placement = upwards ? "above" : "below";
  }

  function activate(index, scroll = false) {
    if (!opened) return;
    const options = [...opened.widget.select.options];
    if (index < 0 || !options[index] || !enabled(options[index])) return;
    opened.active = index;
    for (const row of opened.menu.querySelectorAll("[role=option]")) row.classList.toggle("is-active", Number(row.dataset.index) === index);
    const row = opened.menu.querySelector(`[data-index="${index}"]`);
    if (!row) return;
    opened.widget.button.setAttribute("aria-activedescendant", row.id);
    if (scroll) {
      const top = row.offsetTop;
      if (top < opened.menu.scrollTop + 4) opened.menu.scrollTop = top - 4;
      else if (top + row.offsetHeight > opened.menu.scrollTop + opened.menu.clientHeight - 4) opened.menu.scrollTop = top + row.offsetHeight - opened.menu.clientHeight + 4;
    }
  }

  function renderMenu() {
    if (!opened) return;
    const { widget, menu } = opened;
    const signature = JSON.stringify([widget.select.selectedIndex, ...[...widget.select.options].map(option => [option.value, option.label, enabled(option), option.hidden, !!option.closest("optgroup[hidden]")])]);
    if (opened.signature === signature) { position(); return; }
    opened.signature = signature;
    menu.replaceChildren();
    for (const [index, option] of [...widget.select.options].entries()) {
      if (option.hidden || option.closest("optgroup[hidden]")) continue;
      const row = document.createElement("div");
      row.id = `${widget.id}-option-${index}`;
      row.className = "custom-select-option";
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(index === widget.select.selectedIndex));
      row.setAttribute("aria-disabled", String(!enabled(option)));
      row.dataset.index = index;
      row.dataset.value = option.value;
      const text = document.createElement("span");
      text.className = "custom-select-option-text";
      text.textContent = optionParts(option).text;
      row.append(text);
      if (optionParts(option).recommended) {
        const badge = document.createElement("span");
        badge.className = "custom-select-badge";
        badge.textContent = "추천";
        row.append(badge);
      }
      const tick = document.createElement("span");
      tick.className = "custom-select-check";
      tick.innerHTML = check;
      row.append(tick);
      menu.append(row);
    }
    position();
    if (!opened || opened.widget !== widget) return;
    const options = [...widget.select.options];
    if (!options[opened.active] || !enabled(options[opened.active])) opened.active = options.findIndex(enabled);
    activate(opened.active, true);
  }

  function open(widget) {
    if (!visible(widget)) return;
    close();
    const menu = document.createElement("div");
    menu.id = `${widget.id}-menu`;
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", widget.label);
    (widget.select.closest("dialog") || document.body).append(menu);
    opened = { widget, menu, active: widget.select.selectedIndex };
    widget.button.setAttribute("aria-expanded", "true");
    widget.button.setAttribute("aria-controls", menu.id);
    renderMenu();
    menu.addEventListener("pointerdown", event => event.preventDefault());
    menu.addEventListener("pointermove", event => {
      const row = event.target.closest("[role=option]");
      if (row) activate(Number(row.dataset.index));
    });
    menu.addEventListener("click", event => {
      const row = event.target.closest("[role=option]");
      if (row?.getAttribute("aria-disabled") === "false") commit(Number(row.dataset.index));
    });
  }

  function commit(index) {
    if (!opened) return;
    const { widget } = opened;
    if (!enabled(widget.select.options[index])) return;
    const changed = widget.select.selectedIndex !== index;
    widget.select.selectedIndex = index;
    widget.button.removeAttribute("aria-invalid");
    sync(widget);
    close(true);
    if (changed) {
      widget.select.dispatchEvent(new Event("input", { bubbles: true }));
      widget.select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function onKey(event, widget) {
    if (event.isComposing || event.ctrlKey || event.metaKey) return;
    const navigation = ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "];
    if (event.key === "Escape" && opened?.widget === widget) {
      event.preventDefault(); event.stopPropagation(); close(true); return;
    }
    if (event.key === "Tab") { close(); return; }
    if (!navigation.includes(event.key) && (event.key.length !== 1 || event.altKey)) return;
    event.preventDefault(); event.stopPropagation();
    if (opened?.widget !== widget) {
      open(widget);
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) return;
    } else if (["Enter", " "].includes(event.key)) { commit(opened.active); return; }
    if (!opened) return;
    const options = [...widget.select.options];
    const available = options.map((option, index) => enabled(option) ? index : -1).filter(index => index >= 0);
    if (event.key === "Home") return activate(available[0], true);
    if (event.key === "End") return activate(available.at(-1), true);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const current = available.indexOf(opened.active);
      const next = Math.max(0, Math.min(available.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
      return activate(available[next], true);
    }
    clearTimeout(typeTimer);
    typeBuffer += event.key.toLocaleLowerCase();
    typeTimer = setTimeout(() => { typeBuffer = ""; }, 700);
    const query = [...typeBuffer].every(char => char === typeBuffer[0]) ? typeBuffer[0] : typeBuffer;
    const start = query.length === 1 ? opened.active + 1 : opened.active;
    for (let step = 0; step < options.length; step++) {
      const index = (start + step) % options.length;
      if (enabled(options[index]) && options[index].label.trim().toLocaleLowerCase().startsWith(query)) return activate(index, true);
    }
  }

  function enhance(select) {
    if (registered.has(select) || select.multiple || select.size > 1) return;
    const id = `custom-select-${++serial}`;
    const label = select.closest("label");
    const caption = label?.querySelector(":scope > span");
    const captionCopy = caption?.cloneNode(true);
    captionCopy?.querySelectorAll("button, small").forEach(node => node.remove());
    const labelText = select.getAttribute("aria-label") || captionCopy?.textContent.trim() || "항목 선택";
    const wrapper = document.createElement("div");
    wrapper.className = "custom-select";
    const button = document.createElement("button");
    button.type = "button";
    button.id = id;
    button.className = "custom-select-trigger";
    button.setAttribute("role", "combobox");
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = `<span class="custom-select-value" id="${id}-value"></span><span class="custom-select-badge" hidden>추천</span><span class="custom-select-chevron">${chevron}</span>`;
    if (caption && !select.hasAttribute("aria-label")) {
      if (!caption.id) caption.id = `${id}-label`;
      button.setAttribute("aria-labelledby", `${caption.id} ${id}-value`);
    } else button.setAttribute("aria-label", labelText);
    if (label) label.htmlFor = id;
    select.before(wrapper);
    wrapper.append(button, select);
    select.classList.add("custom-select-native");
    select.hidden = true;
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    const widget = { id, select, button, wrapper, label: labelText, value: button.querySelector(".custom-select-value"), badge: button.querySelector(".custom-select-badge"), key: key(select) };
    registered.set(select, widget);
    widgets.add(widget);
    // Keep direct .value assignments from existing forms reflected immediately.
    for (const property of ["value", "selectedIndex"]) {
      const native = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, property);
      Object.defineProperty(select, property, { configurable: true, get() { return native.get.call(this); }, set(value) { native.set.call(this, value); sync(widget); if (opened?.widget === widget) renderMenu(); } });
    }
    select.addEventListener("change", () => { sync(widget); if (opened?.widget === widget) renderMenu(); });
    select.addEventListener("invalid", event => { event.preventDefault(); button.setAttribute("aria-invalid", "true"); button.focus(); open(widget); });
    button.addEventListener("click", () => { if (opened?.widget === widget) close(); else open(widget); });
    button.addEventListener("keydown", event => onKey(event, widget));
    button.addEventListener("blur", () => queueMicrotask(() => {
      if (opened?.widget === widget && select.isConnected && document.activeElement !== button) close();
    }));
    sync(widget);
  }

  function refresh() {
    document.querySelectorAll("select").forEach(enhance);
    for (const widget of widgets) {
      if (!widget.select.isConnected) widgets.delete(widget);
      else sync(widget);
    }
    if (!opened) return;
    if (!opened.widget.select.isConnected) {
      const previous = opened;
      const replacement = [...widgets].find(widget => widget.key && widget.key === previous.widget.key && visible(widget));
      close();
      if (replacement) {
        const activeValue = previous.widget.select.options[previous.active]?.value;
        replacement.button.focus({ preventScroll: true });
        open(replacement);
        const index = [...replacement.select.options].findIndex(option => option.value === activeValue);
        activate(index, true);
      }
    } else if (!visible(opened.widget)) close();
    else renderMenu();
  }

  new MutationObserver(records => {
    if (records.some(record => {
      const target = record.target.nodeType === Node.TEXT_NODE ? record.target.parentElement : record.target;
      if (target?.closest?.("select")) return true;
      if (record.type === "attributes") return target?.tagName === "FIELDSET" || (opened && target?.contains(opened.widget.button));
      return [...record.addedNodes, ...record.removedNodes].some(node => node.nodeType === Node.ELEMENT_NODE && (node.matches("select") || node.querySelector("select")));
    })) refresh();
  }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["disabled", "selected", "value", "label", "hidden", "open", "class", "aria-label", "aria-describedby", "required"] });
  document.addEventListener("pointerdown", event => {
    if (opened && !opened.menu.contains(event.target) && !opened.widget.wrapper.contains(event.target)) close();
  }, true);
  document.addEventListener("scroll", event => { if (opened && !opened.menu.contains(event.target)) position(); }, true);
  document.addEventListener("close", event => { if (opened?.widget.select.closest("dialog") === event.target) close(); }, true);
  document.addEventListener("reset", () => { close(); queueMicrotask(refresh); }, true);
  window.addEventListener("resize", position);
  window.visualViewport?.addEventListener("resize", position);
  refresh();
})();
