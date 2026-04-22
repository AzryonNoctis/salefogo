function activateTimer(resultUrl) {
  const timerEl = document.getElementById('timer');
  if (!timerEl) return;

  let remaining = Number(timerEl.dataset.seconds || timerEl.textContent || 0);

  function formatSeconds(total) {
    const min = Math.floor(total / 60);
    const sec = total % 60;
    return `${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  }

  timerEl.textContent = formatSeconds(remaining);

  const interval = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      timerEl.textContent = '00:00';
      clearInterval(interval);
      window.location.href = resultUrl;
      return;
    }
    timerEl.textContent = formatSeconds(remaining);
  }, 1000);
}
