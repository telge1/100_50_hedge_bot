// Hedgebotpositions Calculator JavaScript

let burnResults = [];

function calculateHedgebotpositions() {
    // Get input values
    const longSize = parseFloat(document.getElementById('hedgeLongSize')?.value || 0);
    const longAvg = parseFloat(document.getElementById('hedgeLongAvg')?.value || 0);
    const shortEntry = parseFloat(document.getElementById('hedgeShortEntry')?.value || 0);
    const shortTPPercent = parseFloat(document.getElementById('hedgeShortTPPercent')?.value || 0);
    const reentryPercent = parseFloat(document.getElementById('hedgeReentryPercent')?.value || 0);
    
    // Get short size from input (if manually entered) or calculate (50% of long)
    const shortSizeInput = document.getElementById('hedgeShortSize');
    let shortSize = parseFloat(shortSizeInput?.value || 0);
    
    // If short size is empty or 0, calculate it as 50% of long
    if (!shortSize && longSize > 0) {
        shortSize = longSize * 0.5;
        if (shortSizeInput) {
            shortSizeInput.value = shortSize.toFixed(2);
        }
    }
    
    // Clear previous results
    burnResults = [];
    
    // Use calculated short size if not manually entered
    const finalShortSize = shortSize || (longSize > 0 ? longSize * 0.5 : 0);
    
    // Validate inputs (shortSize is now optional - will use calculated value if empty)
    if (!longSize || !longAvg || !shortEntry || !shortTPPercent || !reentryPercent || !finalShortSize) {
        renderBurnsTable();
        return;
    }
    
    // Initialize positions
    let currentLongSize = longSize;
    let currentLongAvg = longAvg;
    let currentShortEntry = shortEntry;
    let currentShortSize = finalShortSize; // Use entered value or calculated
    let currentPrice = shortEntry; // Start with short entry as current price
    
    // Process 4 burns
    for (let burnNumber = 1; burnNumber <= 4; burnNumber++) {
        // Calculate Short TP Price
        const shortTPPrice = currentShortEntry * (1 - shortTPPercent / 100);
        
        // WICHTIG: Burn passiert über Long-SL, aber für die Berechnung verwenden wir den Short-TP-Preis
        // (genau wie im Bot: burn_price = trigger_price = shortTPPrice)
        // Der Long-SL wird bei shortTPPrice * 0.9999 gesetzt, aber die Berechnung verwendet shortTPPrice
        const burnPrice = shortTPPrice; // Verwende Short-TP-Preis für Berechnung (wie im Bot)
        const longSLPrice = shortTPPrice * 0.9999; // Long-SL-Preis (nur für Anzeige)
        
        // Calculate Short Profit
        const shortProfitUSDT = (currentShortEntry - shortTPPrice) * currentShortSize;
        
        // Calculate Loss per Long Coin (using Short-TP-Preis, not Long-SL-Preis)
        const lossPerLong = currentLongAvg - burnPrice;
        
        // Calculate Burn Size in Coins
        let burnSizeCoins = 0;
        if (lossPerLong > 0) {
            burnSizeCoins = shortProfitUSDT / lossPerLong;
            
            // Limit to 90% of Long Size if needed
            if (burnSizeCoins > currentLongSize) {
                burnSizeCoins = currentLongSize * 0.9;
            }
        }
        
        // Calculate Burn Size in USDT (at burn price = Short-TP-Preis, wie im Bot)
        const burnSizeUSDT = burnSizeCoins * burnPrice;
        
        // Calculate positions after burn
        const longSizeAfter = currentLongSize - burnSizeCoins;
        
        // Calculate new Long Average after burn
        // New Average = (Old Cost Basis - Burn Cost Basis) / New Long Size
        // Old Cost Basis = currentLongSize * currentLongAvg
        // Burn Cost Basis = burnSizeCoins * burnPrice
        const oldCostBasis = currentLongSize * currentLongAvg;
        const burnCostBasis = burnSizeCoins * burnPrice;
        const newCostBasis = oldCostBasis - burnCostBasis;
        const longAvgAfter = longSizeAfter > 0 ? newCostBasis / longSizeAfter : currentLongAvg;
        
        // Calculate new short entry (reentry price)
        // Reentry is set at reentryPercent lower than current price (burn price)
        const newShortEntry = burnPrice * (1 - reentryPercent / 100);
        
        // Calculate new short size (50% of new long size)
        const newShortSize = longSizeAfter * 0.5;
        
        // Store burn result
        burnResults.push({
            burnNumber: burnNumber,
            shortEntry: currentShortEntry,
            shortSize: currentShortSize,
            shortTPPrice: shortTPPrice,
            shortTPPercent: shortTPPercent,
            burnPrice: burnPrice,
            shortProfitUSDT: shortProfitUSDT,
            burnSizeCoins: burnSizeCoins,
            burnSizeUSDT: burnSizeUSDT,
            longSizeAfter: longSizeAfter,
            longAvgAfter: longAvgAfter,
            newShortEntry: newShortEntry,
            newShortSize: newShortSize
        });
        
        // Update for next burn
        currentLongSize = longSizeAfter;
        currentLongAvg = longAvgAfter;
        currentShortEntry = newShortEntry;
        currentShortSize = newShortSize;
        currentPrice = burnPrice;
    }
    
    // Render table
    renderBurnsTable();
    
    // Calculate rebuy
    calculateRebuy();
}

function calculateRebuy() {
    // Calculate rebuy (brings Long to target notional)
    if (burnResults.length === 0) {
        document.getElementById('rebuyLongSize').textContent = '0.00';
        document.getElementById('rebuyNewAvgPrice').textContent = '0.00000';
        document.getElementById('rebuyLongSizeUSDT').textContent = '0.00';
        return;
    }
    
    const lastBurn = burnResults[burnResults.length - 1];
    const oldLongSize = lastBurn.longSizeAfter;
    const oldLongAvg = lastBurn.longAvgAfter;
    const lastBurnPrice = lastBurn.burnPrice;
    
    // Get target notional from config
    const targetLongNotional = parseFloat(document.getElementById('targetLongNotional')?.value || 500);
    
    // Calculate current long notional (at rebuy price = burn price)
    // Rebuy wird beim Burn-Preis ausgeführt (market order)
    const currentLongNotional = oldLongSize * lastBurnPrice;
    
    // Calculate required rebuy in USDT
    const rebuyUSDT = targetLongNotional - currentLongNotional;
    
    // Calculate rebuy size in coins
    let rebuySize = 0;
    if (rebuyUSDT > 0 && lastBurnPrice > 0) {
        rebuySize = rebuyUSDT / lastBurnPrice;
    }
    
    const newTotalLongSize = oldLongSize + rebuySize; // New total Long Size after rebuy
    
    // Zeige die Rebuy-Size (die Menge, die gekauft wird um auf Ziel-Notional zu kommen)
    document.getElementById('rebuyLongSize').textContent = rebuySize.toFixed(2);
    
    // Rebuy wird als Market Order beim aktuellen Preis (Burn-Preis) ausgeführt
    // Use burn price as rebuy entry price (market order executes at current price = burn price)
    const rebuyEntryPrice = lastBurnPrice;
    const rebuyEntryPriceInput = document.getElementById('rebuyEntryPrice');
    if (rebuyEntryPriceInput) {
        rebuyEntryPriceInput.value = rebuyEntryPrice.toFixed(5);
    }
    
    if (rebuySize > 0 && rebuyEntryPrice > 0) {
        // Calculate new average price after rebuy (Weighted Average)
        // New Average = (Old Cost Basis + Rebuy Cost Basis) / New Total Size
        // Old Cost Basis = oldLongSize * oldLongAvg
        // Rebuy Cost Basis = rebuySize * rebuyEntryPrice
        const oldCostBasis = oldLongSize * oldLongAvg;
        const rebuyCostBasis = rebuySize * rebuyEntryPrice;
        const newCostBasis = oldCostBasis + rebuyCostBasis;
        const newAveragePrice = newTotalLongSize > 0 ? newCostBasis / newTotalLongSize : oldLongAvg;
        document.getElementById('rebuyNewAvgPrice').textContent = newAveragePrice.toFixed(5);
        
        // Calculate new long size in USDT (should be target notional)
        const newLongSizeUSDT = newTotalLongSize * rebuyEntryPrice;
        document.getElementById('rebuyLongSizeUSDT').textContent = newLongSizeUSDT.toFixed(2);
    } else if (rebuyUSDT <= 0) {
        // No rebuy needed - already at or above target
        document.getElementById('rebuyLongSize').textContent = '0.00';
        document.getElementById('rebuyNewAvgPrice').textContent = oldLongAvg.toFixed(5);
        document.getElementById('rebuyLongSizeUSDT').textContent = currentLongNotional.toFixed(2);
    } else {
        document.getElementById('rebuyNewAvgPrice').textContent = '0.00000';
        document.getElementById('rebuyLongSizeUSDT').textContent = '0.00';
    }
}

function renderBurnsTable() {
    const tbody = document.getElementById('burnsTableBody');
    if (!tbody) return;
    
    tbody.innerHTML = '';
    
    if (burnResults.length === 0) {
        const row = document.createElement('tr');
        row.innerHTML = '<td colspan="11" style="text-align: center; color: var(--text-secondary); padding: 24px;">Bitte fülle alle Felder aus, um die Burns zu berechnen</td>';
        tbody.appendChild(row);
        return;
    }
    
    burnResults.forEach((burn, index) => {
        const row = document.createElement('tr');
        row.className = `burn-row burn-${burn.burnNumber}`;
        
        row.innerHTML = `
            <td class="burn-number">${burn.burnNumber}</td>
            <td>${burn.shortEntry.toFixed(5)}</td>
            <td>${burn.shortSize.toFixed(2)}</td>
            <td>${burn.shortTPPercent.toFixed(2)}%</td>
            <td>${burn.burnPrice.toFixed(5)}</td>
            <td class="profit-value">${burn.shortProfitUSDT.toFixed(2)}</td>
            <td class="burn-coins">${burn.burnSizeCoins.toFixed(2)}</td>
            <td class="burn-usdt">${burn.burnSizeUSDT.toFixed(2)}</td>
            <td>${burn.longSizeAfter.toFixed(2)}</td>
            <td>${burn.newShortEntry.toFixed(5)}</td>
            <td>${burn.newShortSize.toFixed(2)}</td>
        `;
        
        tbody.appendChild(row);
    });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Watch for changes in position calculator values
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            if (mutation.type === 'childList' || mutation.type === 'characterData') {
                // Values changed, update hedgebotpositions
                setTimeout(() => {
                    const totalSize = parseFloat(document.getElementById('totalSize')?.textContent || 0);
                    const avgPrice = parseFloat(document.getElementById('finalAveragePrice')?.textContent || 0);
                    if (totalSize > 0 && avgPrice > 0) {
                        updateHedgebotpositionsFromCalculator(totalSize, avgPrice);
                        calculateHedgebotpositions();
                    }
                }, 100);
            }
        });
    });
    
    // Observe totalSize and finalAveragePrice elements
    const totalSizeEl = document.getElementById('totalSize');
    const avgPriceEl = document.getElementById('finalAveragePrice');
    
    if (totalSizeEl) {
        observer.observe(totalSizeEl, { childList: true, characterData: true, subtree: true });
    }
    if (avgPriceEl) {
        observer.observe(avgPriceEl, { childList: true, characterData: true, subtree: true });
    }
    
    // Initial calculation
    calculateHedgebotpositions();
});

// Load current positions from Bybit
async function loadCurrentPositions() {
    const symbolSelector = document.getElementById('positionSymbolSelector');
    const loadBtn = document.getElementById('loadPositionsBtn');
    
    if (!symbolSelector || !loadBtn) {
        alert('Fehler: Symbol-Auswahl oder Button nicht gefunden');
        return;
    }
    
    const symbol = symbolSelector.value;
    
    if (!symbol) {
        alert('Bitte wähle zuerst ein Symbol aus');
        return;
    }
    
    // Show loading state
    const originalText = loadBtn.textContent;
    loadBtn.textContent = '⏳ Lade...';
    loadBtn.disabled = true;
    
    try {
        const response = await fetch(`/api/positions/${symbol}`);
        const data = await response.json();
        
        if (!data.success) {
            throw new Error(data.error || 'Fehler beim Laden der Positionsdaten');
        }
        
        // Fill Long Position fields
        const longAvgInput = document.getElementById('hedgeLongAvg');
        const longSizeInput = document.getElementById('hedgeLongSize');
        
        if (data.long.entry_price && longAvgInput) {
            longAvgInput.value = data.long.entry_price.toFixed(5);
        }
        
        if (data.long.size && longSizeInput) {
            longSizeInput.value = data.long.size.toFixed(2);
        }
        
        // Fill Short Position fields
        const shortEntryInput = document.getElementById('hedgeShortEntry');
        const shortSizeInput = document.getElementById('hedgeShortSize');
        
        if (data.short.entry_price && shortEntryInput) {
            shortEntryInput.value = data.short.entry_price.toFixed(5);
        }
        
        if (data.short.size && shortSizeInput) {
            shortSizeInput.value = data.short.size.toFixed(2);
        }
        
        // Trigger calculation after loading
        calculateHedgebotpositions();
        
        // Show success message
        loadBtn.textContent = '✅ Geladen!';
        setTimeout(() => {
            loadBtn.textContent = originalText;
            loadBtn.disabled = false;
        }, 2000);
        
    } catch (error) {
        console.error('Error loading positions:', error);
        alert('Fehler beim Laden der Positionsdaten: ' + error.message);
        loadBtn.textContent = originalText;
        loadBtn.disabled = false;
    }
}

