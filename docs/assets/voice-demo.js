// Native radios handle touch, click, Space and the arrow keys, even without JS.
document.querySelectorAll('[data-voice-demo]').forEach(demo => {
  const radios = Array.from(demo.querySelectorAll('.voice-demo__radio'));

  demo.addEventListener('keydown', event => {
    if (!radios.includes(event.target) || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key !== 'Home' && event.key !== 'End') return;

    event.preventDefault();
    const radio = event.key === 'Home' ? radios[0] : radios[radios.length - 1];
    radio.focus({ preventScroll: true });
    radio.click();
  });
});
