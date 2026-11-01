// A link from the embedded cover opens the same summary tab as a click on the full site.
const summaryTab = document.getElementById('cosmoss-feature-3');
function showSummary() {
  summaryTab.click();
  summaryTab.focus({ preventScroll: true });
  summaryTab.scrollIntoView({ block: 'start' });
}
if (summaryTab) {
  document.querySelector('[data-show-summary]')?.addEventListener('click', event => {
    event.preventDefault();
    showSummary();
  });
  if (location.hash === '#' + summaryTab.id) showSummary();
}
