document.addEventListener("DOMContentLoaded", () => {
    const cache = {};
    const tradeLookup = new Map();
    const tradeBody = document.getElementById("profit-trades-body");
    const params = new URLSearchParams(window.location.search);
    const profile = params.get("profile") || window.PROFILE || "bot_1";
    const limit = params.get("limit") || "200";
    const refreshButton = document.getElementById("profit-refresh-btn");
    const lastUpdated = document.getElementById("profit-last-updated");
    console.log("[profit_trades] refresh button found", !!refreshButton);
    console.log("[profit_trades] trade body found", !!tradeBody);

    async function refreshTrades() {
        console.log("[profit_trades] refreshTrades started");
        if (!tradeBody) {
            console.warn("[profit_trades] trade body missing");
            return;
        }
        if (lastUpdated) {
            lastUpdated.textContent = "Lade...";
        }
        try {
            const url = `/api/dashboard/profit-trades?profile=${encodeURIComponent(profile)}&limit=${encodeURIComponent(limit)}`;
            console.log("[profit_trades] api url", url);
            const response = await fetch(url);
            if (!response.ok) throw new Error("Failed to load trades");
            const data = await response.json();
            console.log("[profit_trades] api response", data);
            const trades = data?.trades || data?.data?.trades || data?.results || [];
            updateSummaryCards(data.summary);
            if (!Array.isArray(trades) || trades.length === 0) {
                console.warn("[profit_trades] no trades found in response", data);
            }
            tradeBody.innerHTML = "";
            for (const key in cache) {
                delete cache[key];
            }
            tradeLookup.clear();
            trades.forEach((trade, idx) => {
                const row = document.createElement("tr");
                row.className = "trade-row";
                const tradeId = trade.trade_block_id || `trade-${idx}`;
                tradeLookup.set(tradeId, trade);
                row.dataset.tradeBlockId = tradeId;
                row.dataset.profile = profile;
                const statusValue = (trade.status || "").toLowerCase();
                const isProcess = trade.is_process || statusValue === "in_progress";
                const isClosed = statusValue === "closed";
                const statusLabel = isClosed ? "Closed" : isProcess ? "In Progress" : "Open";
                const endLabel = isProcess ? "-" : trade.end_label || "-";
                const walletAfter = isProcess ? "-" : trade.wallet_after != null ? trade.wallet_after : "-";
                const startLabel = trade.start_label || "-";
                const profitValue = isProcess ? trade.total_trade_pnl : trade.profit_usdt;
                const numericProfit =
                    profitValue != null && Number.isFinite(Number(profitValue))
                        ? Number(profitValue)
                        : null;
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

        if (!trade) {
            tableWrapper.innerHTML = "<div class='detail-message'>Noch keine gefüllten Orders gefunden.</div>";
            cache[tradeId] = tableWrapper.innerHTML;
            return;
        }

        const orders = Array.isArray(trade.filled_orders) ? trade.filled_orders : [];
        const headerNode = buildHeader();
        if (!orders.length) {
            tableWrapper.innerHTML = "";
            tableWrapper.appendChild(headerNode);
            const empty = document.createElement("div");
            empty.className = "detail-message";
            empty.textContent = "Noch keine gefüllten Orders gefunden.";
            tableWrapper.appendChild(empty);
            cache[tradeId] = tableWrapper.innerHTML;
            return;
        }

        const columns = ["Zeit", "Purpose", "Cycle", "PnL", "Scope"];
        const table = document.createElement("table");
        table.classList.add("detail-table");
        const thead = document.createElement("thead");
        const hr = document.createElement("tr");
        columns.forEach((title) => {
            const th = document.createElement("th");
            th.textContent = title;
            hr.appendChild(th);
        });
        thead.appendChild(hr);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        orders.forEach((order) => {
            const tr = document.createElement("tr");
            const timeValue = order.time_label || order.time || "-";
            const cycleValue = order.cycle_index != null ? order.cycle_index : "-";
            const pnlRaw = order.pnl;
            const pnlNumber = Number(pnlRaw);
            const pnlClass =
                !Number.isNaN(pnlNumber) && Number.isFinite(pnlNumber)
                    ? pnlNumber > 0
                        ? "profit-positive"
                        : pnlNumber < 0
                            ? "profit-negative"
                            : ""
                    : "";
            const pnlDisplay = !Number.isNaN(pnlNumber) && Number.isFinite(pnlNumber) ? pnlNumber : "-";
            tr.innerHTML = `
                <td>${timeValue}</td>
                <td>${order.purpose || "-"}</td>
                <td>${cycleValue}</td>
                <td class="${pnlClass}">${pnlDisplay}</td>
                <td>${order.scope || "-"}</td>
            `;
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        tableWrapper.innerHTML = "";
        tableWrapper.appendChild(headerNode);
        tableWrapper.appendChild(table);
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
            "summary-best-bot": summary.best_bot || "-",
        };
        Object.entries(mapping).forEach(([id, text]) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = text;
        });
    }

    if (refreshButton) {
        refreshButton.addEventListener("click", async () => {
            console.log("[profit_trades] manual refresh clicked");
            await refreshTrades();
        });
    }
    refreshTrades();
    setInterval(refreshTrades, 15000);

    bindDetailButtons();
});
