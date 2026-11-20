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

// Saving a password updates every app before the page answers, so show that the save is under way.
for (const form of document.querySelectorAll('.busy-form')) {
  const submit = form.querySelector('button[type="submit"]');
  const label = submit.querySelector('.button-label');
  const spinner = submit.querySelector('.button-spinner');
  const progress = form.querySelector('.form-progress');
  const idle = label.textContent;
  const setBusy = busy => {
    form.toggleAttribute('data-submitting', busy);
    submit.setAttribute('aria-disabled', String(busy));
    submit.classList.toggle('is-busy', busy);
    spinner.hidden = !busy;
    label.textContent = busy ? 'Saving…' : idle;
    progress.hidden = !busy;
  };
  form.addEventListener('submit', event => {
    if (form.hasAttribute('data-submitting')) {
      event.preventDefault();
      return;
    }
    setBusy(true);
  });
  // Going back to this page restores it from the cache with the busy state still showing.
  window.addEventListener('pageshow', event => {
    if (event.persisted) setBusy(false);
  });
}
