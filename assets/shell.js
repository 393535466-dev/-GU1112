export const snapshotMode = document.documentElement.dataset.mode === 'snapshot';
const icons = {
  picker: '<path d="M12 3v4m0 10v4M3 12h4m10 0h4"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  market: '<path d="M3 3v18h18M7 15l4-5 4 2 5-6"/>',
  sectors: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20.9 13.4A9 9 0 0 1 10.6 3.1 9 9 0 1 0 20.9 13.4Z"/>',
};
const pages = [
  ['picker', './', '选股'],
  ['market', './market.html', '大盘'],
  ['sectors', './sectors.html', '板块'],
];
const icon = (key) => '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + icons[key] + '</svg>';
const header = document.querySelector('.site-header');
const active = header.dataset.page;
const links = pages.map(([key, href, label]) =>
  '<a href="' + href + '"' + (key === active ? ' aria-current="page"' : '') + '><span class="nav-icon">' + icon(key) + '</span><span>' + label + '</span></a>'
).join('');

header.innerHTML = '<div class="header-inner"><a class="brand" href="./"><img class="brand-mark" src="./assets/brand-mark.svg" alt="" width="36" height="36">' +
  '<span>懒人选股</span></a><nav class="desktop-nav" aria-label="功能">' + links +
  '</nav><div class="header-tools">' + (snapshotMode ? '<span class="source-pill">收盘快照</span>' : '') +
  '<button class="theme-button" type="button"></button></div></div>';
const bottom = document.createElement('nav');
bottom.className = 'mobile-nav';
bottom.setAttribute('aria-label', '底部导航');
bottom.innerHTML = links;
document.body.append(bottom);

const themeButton = header.querySelector('.theme-button');
function labelTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  themeButton.innerHTML = icon(dark ? 'sun' : 'moon');
  themeButton.setAttribute('aria-label', dark ? '切换到亮色' : '切换到暗色');
}
themeButton.addEventListener('click', () => document.dispatchEvent(new Event('picker-toggle-theme')));
document.addEventListener('picker-theme-change', labelTheme);
labelTheme();

if (window.visualViewport) {
  const syncKeyboard = () => {
    const element = document.activeElement;
    const editing = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement;
    document.body.classList.toggle('keyboard-open', editing && window.innerHeight - window.visualViewport.height > 140);
  };
  window.visualViewport.addEventListener('resize', syncKeyboard);
  document.addEventListener('focusin', syncKeyboard);
  document.addEventListener('focusout', () => requestAnimationFrame(syncKeyboard));
}

export async function fetchSnapshot(section) {
  const response = await fetch('./data/snapshot.json', { cache: 'no-cache', signal: AbortSignal.timeout(12000) });
  if (!response.ok) throw new Error('收盘快照暂不可用。');
  return (await response.json())[section];
}
