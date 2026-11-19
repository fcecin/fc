// The about page: FC.md, drawn with the chat's markdown (fc/md.js).
document.addEventListener('DOMContentLoaded', () => {
  const el = document.querySelector('[data-about]');
  if (el) el.replaceChildren(fc.md.render(el.textContent));
});
