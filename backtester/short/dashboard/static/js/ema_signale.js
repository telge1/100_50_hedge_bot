document.addEventListener("DOMContentLoaded", function () {
  const form = document.getElementById("emaFilterForm");
  const pageInput = document.getElementById("emaFilterPage");
  const applyBtn = document.getElementById("emaFilterApply");
  const prevBtn = document.getElementById("emaPagePrev");
  const nextBtn = document.getElementById("emaPageNext");
  const pageSizeSelect = document.getElementById("emaPageSize");
  let page = Number(window.EMA_SIGNAL_PAGE || 0);

  function submitAt(nextPage) {
    if (pageInput) pageInput.value = String(Math.max(0, nextPage));
    if (form) form.submit();
  }

  if (applyBtn) {
    applyBtn.addEventListener("click", function () {
      page = 0;
      if (pageInput) pageInput.value = "0";
    });
  }
  if (pageSizeSelect) {
    pageSizeSelect.addEventListener("change", function () {
      submitAt(0);
    });
  }
  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      if (!window.EMA_SIGNAL_HAS_PREV) return;
      submitAt(page - 1);
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      if (!window.EMA_SIGNAL_HAS_NEXT) return;
      submitAt(page + 1);
    });
  }
});
