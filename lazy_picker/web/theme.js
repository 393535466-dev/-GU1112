(() => {
  const key = 'lazy-picker.theme';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let saved;
  try { saved = localStorage.getItem(key); } catch { saved = null; }
  let preference = saved === 'light' || saved === 'dark' ? saved : null;

  function apply() {
    const theme = preference ?? (media.matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name=theme-color]')?.setAttribute('content', theme === 'dark' ? '#0e1120' : '#f2f4ff');
    document.dispatchEvent(new Event('picker-theme-change'));
  }

  document.addEventListener('picker-toggle-theme', () => {
    preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(key, preference); } catch { /* Private browsing may block storage. */ }
    apply();
  });
  media.addEventListener('change', apply);
  apply();
})();
