document.addEventListener("DOMContentLoaded", () => {
    const params = new URLSearchParams(window.location.search);
    const refreshButton = document.getElementById("shadow-refresh-btn");
    const lastUpdated = document.getElementById("shadow-last-updated");
    const sideSelect = document.getElementById("shadow-side-select");
    const startFilterInput = document.getElementById("shadow-filter-start");
    const endFilterInput = document.getElementById("shadow-filter-end");
    const pageSizeSelect = document.getElementById("shadow-page-size");
    const applyFilterButton = document.getElementById("shadow-filter-apply");
    const resetFilterButton = document.getElementById("shadow-filter-reset");
    const prevPageButton = document.getElementById("shadow-page-prev");
    const nextPageButton = document.getElementById("shadow-page-next");
    const paginationInfo = document.getElementById("shadow-pagination-info");
    const tableBody = document.getElementById("shadow-signals-body");
    const offlineBanner = document.getElementById("shadow-offline-banner");
    const columns = Array.isArray(window.SHADOW_COLUMNS) ? window.SHADOW_COLUMNS : [];

    function normalizeSide(value) {
        const v = String(value || "long").trim().toLowerCase();
        return v === "short" ? "short" : "long";
    }

    let side = normalizeSide(params.get("side") || window.SHADOW_INITIAL?.side || "long");
    let currentPage = Number(params.get("page") ?? window.SHADOW_INITIAL?.page ?? 0);
    let currentPageSize = Number(
        params.get("page_size") ?? window.SHADOW_INITIAL?.pageSize ?? 50,
    );
    let currentStartFilter = params.get("start_time") ?? window.SHADOW_INITIAL?.startTime ?? "";
    let currentEndFilter = params.get("end_time") ?? window.SHADOW_INITIAL?.endTime ?? "";

    if (!Number.isFinite(currentPage) || currentPage < 0) {
        currentPage = 0;
    }
    if (!Number.isFinite(currentPageSize) || currentPageSize <= 0) {
        currentPageSize = 50;
    }

    if (sideSelect) {
        sideSelect.value = side;
        sideSelect.addEventListener("change", () => {
            const selected = normalizeSide(sideSelect.value);
            if (selected === side) {
                return;
            }
            side = selected;
            currentPage = 0;
            refreshData();
        });
    }

    function syncFilterInputs() {
        if (startFilterInput) {
            startFilterInput.value = currentStartFilter || "";
        }
        if (endFilterInput) {
            endFilterInput.value = currentEndFilter || "";
        }
        if (pageSizeSelect) {
            pageSizeSelect.value = String(currentPageSize);
        }
    }

    function syncUrlState() {
        const nextParams = new URLSearchParams();
        nextParams.set("side", side);
        nextParams.set("page", String(currentPage));
        nextParams.set("page_size", String(currentPageSize));
        if (currentStartFilter) {
            nextParams.set("start_time", currentStartFilter);
        }
        if (currentEndFilter) {
            nextParams.set("end_time", currentEndFilter);
        }
        window.history.replaceState({}, "", `${window.location.pathname}?${nextParams.toString()}`);
    }

    function updateSummaryCards(summary) {
        const map = {
            "summary-total-signals": summary?.total_signals,
            "summary-closed-signals": summary?.closed_signals,
            "summary-open-signals": summary?.open_signals,
            "summary-winrate": summary?.winrate,
            "summary-winning-signals": summary?.winning_signals,
            "summary-total-pnl": summary?.total_pnl,
        };
        Object.entries(map).forEach(([id, value]) => {
            const el = document.getElementById(id);
            if (el && value !== undefined && value !== null) {
                el.textContent = value;
            }
        });
    }

    function updatePaginationControls(pagination, rowCount) {
        if (paginationInfo) {
            if (!pagination || !pagination.total_signals) {
                paginationInfo.textContent = "Keine Signale";
            } else {
                const startIndex = pagination.page * pagination.page_size + 1;
                const endIndex = startIndex + Math.max(rowCount, 0) - 1;
                paginationInfo.textContent =
                    `Zeige ${startIndex}-${endIndex} von ${pagination.total_signals} Signalen`;
            }
        }
        if (prevPageButton) {
            prevPageButton.disabled = !pagination || !pagination.has_prev;
        }
        if (nextPageButton) {
            nextPageButton.disabled = !pagination || !pagination.has_next;
        }
    }

    function setOfflineVisible(message, visible) {
        if (!offlineBanner) {
            return;
        }
        if (visible) {
            offlineBanner.textContent = message || "Shadow-Signal-Daten aktuell nicht verfügbar";
            offlineBanner.style.display = "block";
        } else {
            offlineBanner.style.display = "none";
        }
    }

    function renderTable(rows) {
        if (!tableBody) {
            return;
        }
        tableBody.innerHTML = "";
        if (!Array.isArray(rows) || rows.length === 0) {
            const emptyRow = document.createElement("tr");
            const colspan = columns.length || 9;
            emptyRow.innerHTML = `<td colspan="${colspan}">Keine Signale im gewählten Filter gefunden.</td>`;
            tableBody.appendChild(emptyRow);
            return;
        }
        rows.forEach((row) => {
            const tr = document.createElement("tr");
            const cells = columns.length
                ? columns
                : [
                      "StartTime",
                      "EndTime",
                      "Coin",
                      "Status",
                      "Entry",
                      "SL",
                      "TP",
                      "PnL",
                      "Endprofit +/-",
                  ];
            tr.innerHTML = cells
                .map((key) => {
                    if (key === "PnL") {
                        const cls = row.pnl_class || "";
                        return `<td class="${cls}">${row[key] ?? ""}</td>`;
                    }
                    if (key === "Endprofit +/-") {
                        const cls = row.endprofit_class || "";
                        return `<td class="${cls}">${row[key] ?? ""}</td>`;
                    }
                    return `<td>${row[key] ?? ""}</td>`;
                })
                .join("");
            tableBody.appendChild(tr);
        });
    }

    async function refreshData() {
        syncFilterInputs();
        if (lastUpdated) {
            lastUpdated.textContent = "Lade...";
        }
        if (refreshButton) {
            refreshButton.disabled = true;
        }
        try {
            const query = new URLSearchParams({
                side,
                page: String(currentPage),
                page_size: String(currentPageSize),
            });
            if (currentStartFilter) {
                query.set("start_time", currentStartFilter);
            }
            if (currentEndFilter) {
                query.set("end_time", currentEndFilter);
            }
            const response = await fetch(`/api/dashboard/shadow-signals?${query.toString()}`);
            const data = await response.json();
            if (!response.ok || data.offline) {
                setOfflineVisible(data.message, true);
            } else {
                setOfflineVisible("", false);
            }
            const rows = data.rows || [];
            const pagination = data.pagination || null;
            if (pagination) {
                currentPage = Number(pagination.page || 0);
                currentPageSize = Number(pagination.page_size || currentPageSize);
            }
            updateSummaryCards(data.summary);
            updatePaginationControls(pagination, rows.length);
            renderTable(rows);
            syncUrlState();
            if (lastUpdated) {
                const now = new Date();
                lastUpdated.textContent = `Zuletzt aktualisiert: ${now.toLocaleTimeString("de-DE")}`;
            }
        } catch (error) {
            setOfflineVisible("Shadow-Signal-Daten aktuell nicht verfügbar", true);
            if (lastUpdated) {
                lastUpdated.textContent = "Refresh Fehler";
            }
            console.error("[shadow_signals] refresh failed", error);
        } finally {
            if (refreshButton) {
                refreshButton.disabled = false;
            }
        }
    }

    if (refreshButton) {
        refreshButton.addEventListener("click", () => refreshData());
    }
    if (applyFilterButton) {
        applyFilterButton.addEventListener("click", () => {
            currentStartFilter = startFilterInput?.value || "";
            currentEndFilter = endFilterInput?.value || "";
            currentPageSize = Number(pageSizeSelect?.value || currentPageSize);
            currentPage = 0;
            refreshData();
        });
    }
    if (resetFilterButton) {
        resetFilterButton.addEventListener("click", () => {
            currentStartFilter = "";
            currentEndFilter = "";
            currentPage = 0;
            refreshData();
        });
    }
    if (prevPageButton) {
        prevPageButton.addEventListener("click", () => {
            if (currentPage > 0) {
                currentPage -= 1;
                refreshData();
            }
        });
    }
    if (nextPageButton) {
        nextPageButton.addEventListener("click", () => {
            currentPage += 1;
            refreshData();
        });
    }

    syncFilterInputs();
    refreshData();
    setInterval(refreshData, 300000);
});
