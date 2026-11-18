'use strict';

for (const button of document.querySelectorAll('.password-toggle')) {
  const field = document.getElementById(button.getAttribute('aria-controls'));
  button.hidden = false;
  button.addEventListener('click', () => {
    const reveal = field.type === 'password';
    field.type = reveal ? 'text' : 'password';
    button.setAttribute('aria-pressed', String(reveal));
  });
}
