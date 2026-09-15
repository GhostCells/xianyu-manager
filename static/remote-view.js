// Viewer only: no business API calls, credentials, or cross-origin SSH controls.
(() => {
  const panel = document.getElementById('remoteViewHelper');
  const frame = document.getElementById('remoteViewFrame');
  const reload = document.getElementById('reloadViewHelper');
  if (!panel || !frame || !reload) return;
  const load = () => { frame.src = frame.dataset.src; };
  panel.addEventListener('toggle', () => {
    if (panel.open && !frame.getAttribute('src')) load();
  });
  reload.addEventListener('click', load);
})();
