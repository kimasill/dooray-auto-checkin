document.querySelectorAll('[data-share]').forEach(button => {
  button.addEventListener('click', async () => {
    const status = document.getElementById('share-status');
    const url = 'https://kimasill.github.io/dooray-auto-checkin/';
    try {
      await navigator.clipboard.writeText(url);
      status.textContent = '소개 페이지 주소를 복사했습니다.';
    } catch {
      status.textContent = '주소를 복사해 주세요: ' + url;
    }
  });
});
