try {
  const choice = localStorage.getItem('nasAiTheme') || 'system';
  document.documentElement.dataset.theme = choice === 'system'
    ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
    : choice;
} catch (_error) {
  document.documentElement.dataset.theme = 'dark';
}
