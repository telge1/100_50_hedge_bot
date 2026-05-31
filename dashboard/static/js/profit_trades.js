document.addEventListener("DOMContentLoaded", () => {
    const cache = {};
    const tradeLookup = new Map();
    const tradeBody = document.getElementById("profit-trades-body");
    const params = new URLSearchParams(window.location.search);
    const profile = params.get("profile") || window.PROFILE || "bot_1";
    const limit = params.get("limit") || "50";
    const refreshButton = document.getElementById("profit-refresh-btn");
    const lastUpdated = document.getElementById("profit-last-updated");
    const startFilterInput = document.getElementById("trade-filter-start");
    const endFilterInput = document.getElementById("trade-filter-end");
    const pageSizeSelect = document.getElementById("trade-page-size");
    const applyFilterButton = document.getElementById("trade-filter-apply");
    const resetFilterButton = document.getElementById("trade-filter-reset");
    const prevPageButton = document.getElementById("trade-page-prev");
    const nextPageButton = document.getElementById("trade-page-next");
    const paginationInfo = document.getElementById("trade-pagination-info");
    const sortProfitButton = document.getElementById("trade-sort-profit");
    const sortStatusButton = document.getElementById("trade-sort-status");
    const profitChartToggle = document.getElementById("profit-chart-toggle");
    const profitChartPanel = document.getElementById("profit-chart-panel");
    const profitChartGroupBySelect = document.getElementById("profit-chart-group-by");
    const profitChartCanvas = document.getElementById("profit-chart-canvas");
    const profitChartStatus = document.getElementById("profit-chart-status");
    const walletToggle = document.getElementById("summary-wallet-toggle");
    const walletList = document.getElementById("summary-wallet-list");
    let currentTradePage = Number(
        params.get("page") ?? window.INITIAL_TRADE_FILTERS?.page ?? 0,
    );
    let currentTradePageSize = Number(
        params.get("page_size") ??
            window.INITIAL_TRADE_FILTERS?.pageSize ??
            params.get("limit") ??
            50,
    );
    let currentTradeStartFilter =
        params.get("start_time") ?? window.INITIAL_TRADE_FILTERS?.startTime ?? "";
    let currentTradeEndFilter =
        params.get("end_time") ?? window.INITIAL_TRADE_FILTERS?.endTime ?? "";
    let latestPagination = null;
    let currentTrades = [];
    let currentSortKey = null;
    let currentSortDirection = "asc";
    let profitChartVisible = false;
    let profitChartGroupBy = "day";
    let profitChartInstance = null;
    let latestProfitChartData = [];
    if (!Number.isFinite(currentTradePage) || currentTradePage < 0) {
        currentTradePage = 0;
    }
    if (!Number.isFinite(currentTradePageSize) || currentTradePageSize <= 0) {
        currentTradePageSize = Number(window.INITIAL_TRADE_FILTERS?.pageSize || 50);
    }
    console.log("[profit_trades] refresh button found", !!refreshButton);
    console.log("[profit_trades] trade body found", !!tradeBody);

    function formatWalletValue(value) {
        return Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} USDT` : "-";
    }

    function formatProfitChartValue(value) {
        return Number.isFinite(Number(value)) ? `${Number(value).toFixed(4)} USDT` : "-";
    }

    function getTradeProfitValue(trade) {
        const statusValue = String(trade?.status || "").toLowerCase();
        const isProcess = isOpenTrade(trade) || statusValue === "in_progress";
        const profitValue = isProcess ? trade?.total_trade_pnl : trade?.profit_usdt;
        return profitValue != null && Number.isFinite(Number(profitValue))
            ? Number(profitValue)
            : null;
    }

    function isOpenTrade(trade) {
        const status = (trade?.status || "").toLowerCase();
        return (
            trade?.is_process ||
            status === "in_progress" ||
            status === "in progress" ||
            status === "open" ||
            status === "running" ||
            status === "progress"
        );
    }

    function getStatusSortRank(trade) {
        if (isOpenTrade(trade)) {
            return 0;
        }
        if (String(trade?.status || "").toLowerCase() === "closed") {
            return 1;
        }
        return 2;
    }

    function getSortedTrades(trades) {
        const items = Array.isArray(trades) ? trades.slice() : [];
        if (!currentSortKey) {
            return items;
        }
        return items.sort((left, right) => {
            if (currentSortKey === "status") {
                const leftRank = getStatusSortRank(left);
                const rightRank = getStatusSortRank(right);
                return currentSortDirection === "desc"
                    ? rightRank - leftRank
                    : leftRank - rightRank;
            }
            if (currentSortKey === "profit") {
                const leftProfit = getTradeProfitValue(left);
                const rightProfit = getTradeProfitValue(right);
                if (leftProfit == null && rightProfit == null) {
                    return 0;
                }
                if (leftProfit == null) {
                    return 1;
                }
                if (rightProfit == null) {
                    return -1;
                }
                return currentSortDirection === "desc"
                    ? rightProfit - leftProfit
                    : leftProfit - rightProfit;
            }
            return 0;
        });
    }

    function updateSortButtons() {
        const indicator = currentSortDirection === "desc" ? "▼" : "▲";
        if (sortProfitButton) {
            sortProfitButton.classList.toggle("active", currentSortKey === "profit");
            sortProfitButton.textContent =
                currentSortKey === "profit" ? `Endprofit ${indicator}` : "Endprofit";
        }
        if (sortStatusButton) {
            sortStatusButton.classList.toggle("active", currentSortKey === "status");
            sortStatusButton.textContent =
                currentSortKey === "status" ? `Status ${indicator}` : "Status";
        }
    }

    function renderTradesTable(trades) {
        tradeBody.innerHTML = "";
        for (const key in cache) {
            delete cache[key];
        }
        tradeLookup.clear();
        if (!Array.isArray(trades) || trades.length === 0) {
            const emptyRow = document.createElement("tr");
            emptyRow.innerHTML = "<td colspan='9'>Keine Trades im gewählten Filter gefunden.</td>";
            tradeBody.appendChild(emptyRow);
            return;
        }
        trades.forEach((trade, idx) => {
            const row = document.createElement("tr");
            row.className = "trade-row";
            const tradeId = trade.trade_block_id || `trade-${idx}`;
            tradeLookup.set(tradeId, trade);
            row.dataset.tradeBlockId = tradeId;
            row.dataset.profile = profile;
            const statusValue = String(trade.status || "").toLowerCase();
            const isProcess = isOpenTrade(trade) || statusValue === "in_progress";
            const isClosed = statusValue === "closed";
            const statusLabel = isClosed ? "Closed" : isProcess ? "In Progress" : "Open";
            const endLabel = isProcess ? "-" : trade.end_label || "-";
            const walletAfter = isProcess ? "-" : trade.wallet_after != null ? trade.wallet_after : "-";
            const startLabel = trade.start_label || "-";
            const numericProfit = getTradeProfitValue(trade);
            const profitClass =
                numericProfit != null
                    ? numericProfit > 0
                        ? "profit-positive"
                        : numericProfit < 0
                            ? "profit-negative"
                            : ""
                    : "";
            const profitDisplay = numericProfit != null ? numericProfit : "-";
            row.innerHTML = `
                <td>${trade.bot_name || "-"}</td>
                <td>${trade.symbol || "-"}</td>
                <td>${startLabel}</td>
                <td>${endLabel}</td>
                <td class="${profitClass}">
                    ${profitDisplay}
                </td>
                <td>${walletAfter}</td>
                <td>${trade.cycle_count != null ? trade.cycle_count : 0}</td>
                <td>${statusLabel}</td>
                <td><button class="details-btn" data-trade-id="${tradeId}">Details</button></td>
            `;
            tradeBody.appendChild(row);
            const detailRow = document.createElement("tr");
            detailRow.className = "detail-row";
            detailRow.dataset.detailsFor = tradeId;
            detailRow.innerHTML = `
                <td colspan="9">
                    <div class="detail-content">
                        <div class="detail-loader">Lade Details...</div>
                        <div class="detail-table-wrapper" style="display:none;"></div>
                    </div>
                </td>
            `;
            tradeBody.appendChild(detailRow);
        });
        bindDetailButtons();
    }

    function syncFilterInputs() {
        if (startFilterInput) {
            startFilterInput.value = currentTradeStartFilter || "";
        }
        if (endFilterInput) {
            endFilterInput.value = currentTradeEndFilter || "";
        }
        if (pageSizeSelect) {
            pageSizeSelect.value = String(currentTradePageSize);
        }
    }

    function updatePaginationControls(pagination, tradeCount) {
        latestPagination = pagination || null;
        if (paginationInfo) {
            if (!pagination || !pagination.total_filtered_trades) {
                paginationInfo.textContent = "Keine Trades";
            } else {
                const startIndex = pagination.page * pagination.page_size + 1;
                const endIndex = startIndex + Math.max(tradeCount, 0) - 1;
                paginationInfo.textContent =
                    `Zeige ${startIndex}-${endIndex} von ${pagination.total_filtered_trades} Trades`;
            }
        }
        if (prevPageButton) {
            prevPageButton.disabled = !pagination || !pagination.has_prev;
        }
        if (nextPageButton) {
            nextPageButton.disabled = !pagination || !pagination.has_next;
        }
    }

    function syncUrlState() {
        const nextParams = new URLSearchParams(window.location.search);
        nextParams.set("profile", profile);
        nextParams.set("page", String(currentTradePage));
        nextParams.set("page_size", String(currentTradePageSize));
        if (currentTradeStartFilter) {
            nextParams.set("start_time", currentTradeStartFilter);
        } else {
            nextParams.delete("start_time");
        }
        if (currentTradeEndFilter) {
            nextParams.set("end_time", currentTradeEndFilter);
        } else {
            nextParams.delete("end_time");
        }
        window.history.replaceState({}, "", `${window.location.pathname}?${nextParams.toString()}`);
    }

    function buildProfitChartUrl() {
        const query = new URLSearchParams({
            profile,
            group_by: profitChartGroupBy,
        });
        if (currentTradeStartFilter) {
            query.set("start_time", currentTradeStartFilter);
        }
        if (currentTradeEndFilter) {
            query.set("end_time", currentTradeEndFilter);
        }
        return `/api/dashboard/profit-trades/chart?${query.toString()}`;
    }

    function destroyProfitChart() {
        if (profitChartInstance) {
            profitChartInstance.destroy();
            profitChartInstance = null;
        }
    }

    function renderProfitChart(chartData) {
        latestProfitChartData = Array.isArray(chartData) ? chartData : [];
        destroyProfitChart();
        if (!profitChartCanvas || !window.Chart) {
            if (profitChartStatus) {
                profitChartStatus.textContent = "Chart.js nicht verfügbar";
            }
            return;
        }
        if (!latestProfitChartData.length) {
            if (profitChartStatus) {
                profitChartStatus.textContent = "Keine Chart-Daten verfügbar";
            }
            return;
        }
        const labels = latestProfitChartData.map((item) => item.label);
        const values = latestProfitChartData.map((item) => Number(item.profit) || 0);
        const backgroundColors = values.map((value) =>
            value >= 0 ? "rgba(34, 197, 94, 0.65)" : "rgba(239, 68, 68, 0.65)",
        );
        const borderColors = values.map((value) =>
            value >= 0 ? "rgba(34, 197, 94, 1)" : "rgba(239, 68, 68, 1)",
        );
        const canvasContext = profitChartCanvas.getContext("2d");
        if (!canvasContext) {
            if (profitChartStatus) {
                profitChartStatus.textContent = "Chart-Canvas konnte nicht initialisiert werden";
            }
            return;
        }
        profitChartInstance = new window.Chart(canvasContext, {
            type: "bar",
            data: {
                labels,
                datasets: [
                    {
                        label: "Profit",
                        data: values,
                        backgroundColor: backgroundColors,
                        borderColor: borderColors,
                        borderWidth: 1.5,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: false,
                    },
                    tooltip: {
                        backgroundColor: "rgba(17, 24, 39, 0.95)",
                        titleColor: "#f5f5f5",
                        bodyColor: "#f5f5f5",
                        callbacks: {
                            label(context) {
                                const point = latestProfitChartData[context.dataIndex] || {};
                                return [
                                    `Profit: ${formatProfitChartValue(point.profit)}`,
                                    `Trades: ${point.trade_count ?? 0}`,
                                    `Winning: ${point.winning_trades ?? 0}`,
                                    `Losing: ${point.losing_trades ?? 0}`,
                                ];
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: {
                            color: "#d1d5db",
                        },
                        grid: {
                            display: false,
                        },
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            color: "#d1d5db",
                            callback(value) {
                                return `${value} USDT`;
                            },
                        },
                        grid: {
                            color(context) {
                                const tickValue = Number(context?.tick?.value);
                                return tickValue === 0
                                    ? "rgba(229, 231, 235, 0.45)"
                                    : "rgba(148, 163, 184, 0.16)";
                            },
                            lineWidth(context) {
                                const tickValue = Number(context?.tick?.value);
                                return tickValue === 0 ? 2 : 1;
                            },
                        },
                    },
                },
            },
        });
        if (profitChartStatus) {
            profitChartStatus.textContent =
                `${latestProfitChartData.length} Balken geladen (${profitChartGroupBy === "month" ? "Monatlich" : "Täglich"})`;
        }
    }

    async function refreshProfitChart() {
        if (!profitChartVisible) {
            return;
        }
        if (profitChartStatus) {
            profitChartStatus.textContent = "Chart wird geladen...";
        }
        try {
            const response = await fetch(buildProfitChartUrl());
            if (!response.ok) {
                throw new Error(`Failed to load profit chart (${response.status})`);
            }
            const data = await response.json();
            renderProfitChart(data?.chart || []);
        } catch (error) {
            destroyProfitChart();
            latestProfitChartData = [];
            if (profitChartStatus) {
                profitChartStatus.textContent = `Fehler beim Laden des Profit-Charts${error?.message ? `: ${error.message}` : ""}`;
            }
            console.error("[profit_trades] chart refresh failed", error);
        }
    }

    function setupProfitChartControls() {
        if (profitChartGroupBySelect) {
            profitChartGroupBySelect.value = profitChartGroupBy;
            profitChartGroupBySelect.addEventListener("change", async () => {
                profitChartGroupBy = profitChartGroupBySelect.value || "day";
                if (profitChartVisible) {
                    await refreshProfitChart();
                }
            });
        }
        if (profitChartToggle && profitChartPanel) {
            profitChartToggle.addEventListener("click", async () => {
                profitChartVisible = !profitChartVisible;
                profitChartPanel.classList.toggle("visible", profitChartVisible);
                profitChartToggle.textContent = profitChartVisible
                    ? "Profit Chart ausblenden ▲"
                    : "Profit Chart anzeigen ▼";
                if (profitChartVisible) {
                    await refreshProfitChart();
                } else {
                    destroyProfitChart();
                    latestProfitChartData = [];
                    if (profitChartStatus) {
                        profitChartStatus.textContent = "";
                    }
                }
            });
        }
    }

    async function refreshTrades() {
        console.log("[profit_trades] refreshTrades started");
        if (!tradeBody) {
            console.warn("[profit_trades] trade body missing");
            return;
        }
        syncFilterInputs();
        if (lastUpdated) {
            lastUpdated.textContent = "Lade...";
        }
        try {
            const query = new URLSearchParams({
                profile,
                limit,
                page: String(currentTradePage),
                page_size: String(currentTradePageSize),
            });
            if (currentTradeStartFilter) {
                query.set("start_time", currentTradeStartFilter);
            }
            if (currentTradeEndFilter) {
                query.set("end_time", currentTradeEndFilter);
            }
            const url = `/api/dashboard/profit-trades?${query.toString()}`;
            console.log("[profit_trades] api url", url);
            const response = await fetch(url);
            if (!response.ok) throw new Error("Failed to load trades");
            const data = await response.json();
            console.log("[profit_trades] api response", data);
            const trades = data?.trades || data?.data?.trades || data?.results || [];
            const pagination = data?.pagination || null;
            if (pagination) {
                currentTradePage = Number(pagination.page || 0);
                currentTradePageSize = Number(pagination.page_size || currentTradePageSize);
            }
            updateSummaryCards(data.summary);
            updatePaginationControls(pagination, Array.isArray(trades) ? trades.length : 0);
            syncUrlState();
            if (!Array.isArray(trades) || trades.length === 0) {
                console.warn("[profit_trades] no trades found in response", data);
            }
            currentTrades = Array.isArray(trades) ? trades : [];
            updateSortButtons();
            renderTradesTable(getSortedTrades(currentTrades));
            if (lastUpdated) {
                const now = new Date();
                lastUpdated.textContent = `Zuletzt aktualisiert: ${now.toLocaleTimeString("de-DE")}`;
            }
        } catch (error) {
            if (lastUpdated) {
                lastUpdated.textContent = "Refresh Fehler";
            }
            console.error("[profit_trades] refresh failed", error);
        }
    }

    function bindDetailButtons() {
        document.querySelectorAll(".details-btn[data-trade-id]").forEach((btn) => {
            btn.removeEventListener("click", toggleDetails);
            btn.addEventListener("click", toggleDetails);
        });
    }

    function toggleDetails() {
        const btn = this;
        const tradeId = btn.dataset.tradeId;
        const trade = tradeLookup.get(tradeId);
        const isProcess = trade?.is_process || (trade?.status || "").toLowerCase() === "in_progress";
        const detailRow = document.querySelector(`[data-details-for="${tradeId}"]`);
        if (!detailRow) return;
        const tableWrapper = detailRow.querySelector(".detail-table-wrapper");
        const loader = detailRow.querySelector(".detail-loader");
        const isOpen = detailRow.classList.contains("detail-loaded");
        if (isOpen) {
            detailRow.classList.remove("detail-loaded");
            tableWrapper.style.display = "none";
            loader.style.display = "none";
            return;
        }
        detailRow.classList.add("detail-loaded");
        if (cache[tradeId]) {
            tableWrapper.innerHTML = cache[tradeId];
            loader.style.display = "none";
            tableWrapper.style.display = "";
            return;
        }
        if (isProcess) {
            loader.style.display = "none";
            tableWrapper.style.display = "";
            renderProcessDetails(tradeId, trade, tableWrapper);
            return;
        }
        loader.style.display = "";
        tableWrapper.style.display = "none";
        fetch(`/api/dashboard/profit-trades/${tradeId}/details?profile=${profile}`)
            .then((response) => response.json())
            .then((data) => {
                const rows = data.rows || [];
                if (!rows.length) {
                    tableWrapper.innerHTML = "<div class='detail-message'>Keine Details gefunden.</div>";
                } else {
                    const header = ["Zeit", "Symbol", "Order-ID", "PnL", "Wallet danach", "Purpose"];
                    const table = document.createElement("table");
                    table.classList.add("detail-table");
                    const thead = document.createElement("thead");
                    const hr = document.createElement("tr");
                    header.forEach((title) => {
                        const th = document.createElement("th");
                        th.textContent = title;
                        hr.appendChild(th);
                    });
                    thead.appendChild(hr);
                    table.appendChild(thead);
                    const tbody = document.createElement("tbody");
                    rows.forEach((row) => {
                        const tr = document.createElement("tr");
                        tr.innerHTML = `
                            <td>${row.time_label || "-"}</td>
                            <td>${row.symbol || "-"}</td>
                            <td>${row.order_id || "-"}</td>
                            <td class="${row.pnl_usdt > 0 ? "profit-positive" : row.pnl_usdt < 0 ? "profit-negative" : ""}">
                                ${row.pnl_usdt || 0}
                            </td>
                            <td>${row.wallet_after || "-"}</td>
                            <td>${row.purpose || "-"}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                    table.appendChild(tbody);
                    tableWrapper.innerHTML = "";
                    tableWrapper.appendChild(table);
                }
                cache[tradeId] = tableWrapper.innerHTML;
                loader.style.display = "none";
                tableWrapper.style.display = "";
            })
            .catch((err) => {
                loader.style.display = "none";
                tableWrapper.innerHTML = "<div class='detail-message'>Fehler beim Laden der Details.</div>";
                tableWrapper.style.display = "";
                console.error("Detail fetch failed", err);
            });
    }

    function renderProcessDetails(tradeId, trade, tableWrapper) {
        const buildHeader = () => {
            const summary = document.createElement("div");
            summary.className = "detail-process-summary";
            summary.innerHTML = `
                <div><strong>Bot:</strong> ${trade?.bot_name || "-"}</div>
                <div><strong>Symbol:</strong> ${trade?.symbol || "-"}</div>
                <div><strong>Status:</strong> In Progress</div>
                <div><strong>Trade ID:</strong> ${trade?.trade_block_id || "-"}</div>
                <div><strong>Realisiert:</strong> ${
                    trade?.total_trade_pnl != null ? trade.total_trade_pnl : "-"
                }</div>
            `;
            return summary;
        };

        const normalizeList = (value) => (Array.isArray(value) ? value : []);
        const dedupeOrders = (orders) => {
            const seen = new Set();
            const deduped = [];
            for (const order of orders) {
                if (!order || typeof order !== "object") continue;
                const key =
                    order.order_id ||
                    order.exchange_order_id ||
                    order.client_order_id ||
                    [
                        order.purpose || "",
                        order.side || "",
                        order.qty ?? order.exec_qty ?? "",
                        order.price ?? order.avg_price ?? order.exec_price ?? order.trigger_price ?? "",
                    ].join("|");
                if (seen.has(key)) continue;
                seen.add(key);
                deduped.push(order);
            }
            return deduped;
        };
        const toNumberOrNull = (value) => {
            const parsed = Number(value);
            return Number.isFinite(parsed) ? parsed : null;
        };
        const createSectionTitle = (text) => {
            const title = document.createElement("h4");
            title.style.margin = "12px 0 8px";
            title.textContent = text;
            return title;
        };
        const createEmptyMessage = (text) => {
            const empty = document.createElement("div");
            empty.className = "detail-message";
            empty.textContent = text;
            return empty;
        };
        const renderOrdersTable = (orders, columns) => {
            const table = document.createElement("table");
            table.classList.add("detail-table");
            const thead = document.createElement("thead");
            const headerRow = document.createElement("tr");
            columns.forEach((title) => {
                const th = document.createElement("th");
                th.textContent = title;
                headerRow.appendChild(th);
            });
            thead.appendChild(headerRow);
            table.appendChild(thead);
            const tbody = document.createElement("tbody");
            orders.forEach((order) => {
                const tr = document.createElement("tr");
                const pnlValue = toNumberOrNull(
                    order.realized_pnl ?? order.pnl ?? order.pnl_usdt,
                );
                const pnlClass =
                    pnlValue == null
                        ? ""
                        : pnlValue > 0
                            ? "profit-positive"
                            : pnlValue < 0
                                ? "profit-negative"
                                : "";
                const qty = order.qty ?? order.exec_qty ?? "-";
                const price =
                    order.price ??
                    order.avg_price ??
                    order.exec_price ??
                    order.trigger_price ??
                    "-";
                const timeValue =
                    order.time_label ||
                    order.time ||
                    order.timestamp ||
                    order.updated_at ||
                    "-";
                tr.innerHTML = `
                    <td>${timeValue}</td>
                    <td>${order.purpose || "-"}</td>
                    <td>${order.side || "-"}</td>
                    <td>${qty}</td>
                    <td>${price}</td>
                    <td class="${pnlClass}">${pnlValue != null ? pnlValue : "-"}</td>
                    <td>${order.status || order.scope || "-"}</td>
                    <td>${order.order_id || order.exchange_order_id || order.client_order_id || "-"}</td>
                `;
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            return table;
        };

        if (!trade) {
            tableWrapper.innerHTML = "<div class='detail-message'>Keine aktiven Orders gefunden.</div>";
            cache[tradeId] = tableWrapper.innerHTML;
            return;
        }

        const headerNode = buildHeader();
        const activeOrders = dedupeOrders([
            ...normalizeList(trade.active_orders),
            ...normalizeList(trade.open_orders),
            ...normalizeList(trade.orders),
        ]);
        const filledOrders = dedupeOrders([
            ...normalizeList(trade.filled_orders),
            ...normalizeList(trade.details),
        ]);

        tableWrapper.innerHTML = "";
        tableWrapper.appendChild(headerNode);

        tableWrapper.appendChild(createSectionTitle("Aktive Orders"));
        if (activeOrders.length) {
            tableWrapper.appendChild(
                renderOrdersTable(activeOrders, [
                    "Zeit",
                    "Purpose",
                    "Side",
                    "Qty",
                    "Price/Trigger",
                    "PnL",
                    "Status",
                    "Order-ID",
                ]),
            );
        } else {
            tableWrapper.appendChild(createEmptyMessage("Keine aktiven Orders gefunden"));
        }

        tableWrapper.appendChild(createSectionTitle("Gefüllte Orders"));
        if (filledOrders.length) {
            tableWrapper.appendChild(
                renderOrdersTable(filledOrders, [
                    "Zeit",
                    "Purpose",
                    "Side",
                    "Qty",
                    "Price/Trigger",
                    "PnL",
                    "Status",
                    "Order-ID",
                ]),
            );
        } else {
            tableWrapper.appendChild(createEmptyMessage("Keine gefüllten Orders gefunden"));
        }

        cache[tradeId] = tableWrapper.innerHTML;
    }

    function updateSummaryCards(summary) {
        if (!summary || typeof summary !== "object") {
            return;
        }
        const mapping = {
            "summary-total-profit":
                summary.total_profit != null ? `${Number(summary.total_profit).toFixed(4)} USDT` : "-",
            "summary-closed-trades": summary.closed_trades != null ? summary.closed_trades : "-",
            "summary-open-trades": summary.open_trades != null ? summary.open_trades : "-",
            "summary-winrate": summary.winrate != null ? `${summary.winrate} %` : "-",
            "summary-winning-trades": summary.winning_trades != null ? summary.winning_trades : "-",
            "summary-total-wallet": formatWalletValue(summary.total_wallet_usdt),
        };
        Object.entries(mapping).forEach(([id, text]) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = text;
        });

        if (!walletList) {
            return;
        }
        walletList.innerHTML = "";
        if (!Array.isArray(summary.bot_wallets) || summary.bot_wallets.length === 0) {
            const empty = document.createElement("div");
            empty.className = "summary-wallet-empty";
            empty.textContent = "Keine Wallet-Daten verfügbar";
            walletList.appendChild(empty);
            return;
        }
        summary.bot_wallets.forEach((entry) => {
            const item = document.createElement("div");
            item.className = "summary-wallet-item";

            const name = document.createElement("span");
            name.textContent = entry?.bot_name || "-";

            const value = document.createElement("span");
            value.textContent = formatWalletValue(entry?.wallet_usdt);

            item.appendChild(name);
            item.appendChild(value);
            walletList.appendChild(item);
        });
    }

    if (applyFilterButton) {
        applyFilterButton.addEventListener("click", async () => {
            currentTradeStartFilter = startFilterInput?.value || "";
            currentTradeEndFilter = endFilterInput?.value || "";
            currentTradePage = 0;
            await refreshTrades();
            if (profitChartVisible) {
                await refreshProfitChart();
            }
        });
    }

    if (resetFilterButton) {
        resetFilterButton.addEventListener("click", async () => {
            currentTradeStartFilter = "";
            currentTradeEndFilter = "";
            currentTradePage = 0;
            syncFilterInputs();
            await refreshTrades();
            if (profitChartVisible) {
                await refreshProfitChart();
            }
        });
    }

    if (pageSizeSelect) {
        pageSizeSelect.addEventListener("change", async () => {
            const nextSize = Number(pageSizeSelect.value || currentTradePageSize);
            currentTradePageSize = Number.isFinite(nextSize) ? nextSize : currentTradePageSize;
            currentTradePage = 0;
            await refreshTrades();
        });
    }

    if (prevPageButton) {
        prevPageButton.addEventListener("click", async () => {
            if (!latestPagination?.has_prev) {
                return;
            }
            currentTradePage = Math.max(0, currentTradePage - 1);
            await refreshTrades();
        });
    }

    if (nextPageButton) {
        nextPageButton.addEventListener("click", async () => {
            if (!latestPagination?.has_next) {
                return;
            }
            currentTradePage += 1;
            await refreshTrades();
        });
    }

    if (sortStatusButton) {
        sortStatusButton.addEventListener("click", () => {
            if (currentSortKey === "status") {
                currentSortDirection = currentSortDirection === "asc" ? "desc" : "asc";
            } else {
                currentSortKey = "status";
                currentSortDirection = "asc";
            }
            updateSortButtons();
            renderTradesTable(getSortedTrades(currentTrades));
        });
    }

    if (sortProfitButton) {
        sortProfitButton.addEventListener("click", () => {
            if (currentSortKey === "profit") {
                currentSortDirection = currentSortDirection === "asc" ? "desc" : "asc";
            } else {
                currentSortKey = "profit";
                currentSortDirection = "desc";
            }
            updateSortButtons();
            renderTradesTable(getSortedTrades(currentTrades));
        });
    }

    if (walletToggle && walletList) {
        walletToggle.addEventListener("click", (event) => {
            event.stopPropagation();
            walletList.classList.toggle("visible");
        });
        document.addEventListener("click", (event) => {
            if (!walletList.classList.contains("visible")) {
                return;
            }
            if (walletList.contains(event.target) || walletToggle.contains(event.target)) {
                return;
            }
            walletList.classList.remove("visible");
        });
    }

    if (refreshButton) {
        refreshButton.addEventListener("click", async () => {
            console.log("[profit_trades] manual refresh clicked");
            await refreshTrades();
            if (profitChartVisible) {
                await refreshProfitChart();
            }
        });
    }
    syncFilterInputs();
    updateSortButtons();
    setupProfitChartControls();
    refreshTrades();
    setInterval(refreshTrades, 15000);

    bindDetailButtons();
});
