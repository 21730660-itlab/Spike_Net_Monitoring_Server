/* Confirmation with a countdown for forms marked data-countdown="message" (e.g. Clear history). */
(() => {
  const SECONDS = 5;
  const dialog = document.getElementById("confirm-dialog");
  if (!dialog) return;
  const text = dialog.querySelector("[data-text]");
  const yes = dialog.querySelector("[data-yes]");
  const no = dialog.querySelector("[data-no]");
  let pending = null;
  let timer = null;

  function stop() { clearInterval(timer); timer = null; }

  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (!form.dataset || !form.dataset.countdown) return;
    ev.preventDefault();
    pending = form;
    text.textContent = form.dataset.countdown;
    let left = SECONDS;
    yes.disabled = true;
    yes.textContent = `Yes, clear (${left})`;
    stop();
    timer = setInterval(() => {
      left -= 1;
      if (left > 0) { yes.textContent = `Yes, clear (${left})`; return; }
      stop();
      yes.disabled = false;
      yes.textContent = "Yes, clear";
    }, 1000);
    dialog.showModal();
  });

  yes.addEventListener("click", () => {
    if (yes.disabled || !pending) return;
    stop();
    dialog.close();
    pending.submit();  // form.submit() skips the submit event, so no second dialog
  });
  no.addEventListener("click", () => { stop(); pending = null; dialog.close(); });
  dialog.addEventListener("cancel", () => { stop(); pending = null; });
})();
