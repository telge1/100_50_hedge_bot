// Dashboard JavaScript

// Auto-refresh control variables (declared at top to ensure availability)
let autoRefreshInterval = null;
let isModalOpen = false;

// Bot switching function
function switchBot(symbol) {
    console.log('switchBot called with symbol:', symbol);
    if (symbol) {
        // Stop auto-refresh before switching
        if (autoRefreshInterval) {
            clearInterval(autoRefreshInterval);
            autoRefreshInterval = null;
        }
        // Get bot_type from select element
        const selectElement = document.getElementById('botSelector');
        const selectedOption = selectElement.options[selectElement.selectedIndex];
        const botType = selectedOption ? selectedOption.getAttribute('data-bot-type') || 'long' : 'long';
        // Navigate to new bot - use full URL to ensure it works
        const newUrl = `/dashboard?symbol=${encodeURIComponent(symbol)}&bot_type=${encodeURIComponent(botType)}`;
        console.log('Navigating to:', newUrl);
        window.location.href = newUrl;
    }
}

async function startBot(symbol, botType = 'long', event) {
    // Get button element if event is provided
    let button = null;
    if (event && event.target) {
        button = event.target;
    } else if (window.event && window.event.target) {
        button = window.event.target;
    }
    
    const originalText = button ? button.innerHTML : null;
    
    // Disable button and show loading
    if (button) {
        button.disabled = true;
        button.innerHTML = '⏳ ...';
    }
    
    try {
        // Zuerst Profit + Rebuy in Config speichern (Standard 2%, Rebuy 2), dann Bot starten
        const configResponse = await fetch(`/api/hedge/set-tp-config/${symbol}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                long_tp_percentage: 2,
                short_tp_percentage: 2,
                burns_before_rebuy: 2
            })
        });
        const configData = await configResponse.json();
        if (!configResponse.ok || !configData.success) {
            console.warn('Config-Update vor Start fehlgeschlagen:', configData?.error || configData?.message);
            // Trotzdem Start versuchen
        }

        const response = await fetch(`/api/bots/${symbol}/start`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ bot_type: botType })
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        if (data.success) {
            alert(`✅ ${botType.toUpperCase()} Bot ${symbol} wurde gestartet!`);
            setTimeout(() => {
                location.reload();
            }, 500);
        } else {
            alert(`❌ Fehler: ${data.message}`);
            if (button) {
                button.disabled = false;
                button.innerHTML = originalText;
            }
        }
    } catch (error) {
        console.error('Error starting bot:', error);
        alert(`❌ Fehler beim Starten des Bots: ${error.message}`);
        if (button) {
            button.disabled = false;
            button.innerHTML = originalText;
        }
    }
}

async function stopBot(symbol, botType = 'long', event) {
    if (!confirm(`Möchtest du wirklich den ${botType.toUpperCase()} Bot ${symbol} stoppen?`)) {
        return;
    }
    
    // Get button element if event is provided
    let button = null;
    if (event && event.target) {
        button = event.target;
    } else if (window.event && window.event.target) {
        button = window.event.target;
    }
    
    const originalText = button ? button.innerHTML : null;
    
    // Disable button and show loading
    if (button) {
        button.disabled = true;
        button.innerHTML = '⏳ ...';
    }
    
    try {
        const response = await fetch(`/api/bots/${symbol}/stop`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ bot_type: botType })
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        if (data.success) {
            alert(`✅ ${botType.toUpperCase()} Bot ${symbol} wurde gestoppt!`);
            setTimeout(() => {
                location.reload();
            }, 500);
        } else {
            alert(`❌ Fehler: ${data.message}`);
            if (button) {
                button.disabled = false;
                button.innerHTML = originalText;
            }
        }
    } catch (error) {
        console.error('Error stopping bot:', error);
        alert(`❌ Fehler beim Stoppen des Bots: ${error.message}`);
        if (button) {
            button.disabled = false;
            button.innerHTML = originalText;
        }
    }
}

async function restartBot(symbol, botType = 'long', event) {
    if (!confirm(`Möchtest du wirklich den ${botType.toUpperCase()} Bot ${symbol} neu starten?`)) {
        return;
    }
    
    // Get button element if event is provided
    let button = null;
    if (event && event.target) {
        button = event.target;
    } else if (window.event && window.event.target) {
        button = window.event.target;
    }
    
    const originalText = button ? button.innerHTML : null;
    
    // Disable button and show loading
    if (button) {
        button.disabled = true;
        button.innerHTML = '⏳ ...';
    }
    
    try {
        const response = await fetch(`/api/bots/${symbol}/restart`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ bot_type: botType })
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        if (data.success) {
            alert(`✅ ${botType.toUpperCase()} Bot ${symbol} wurde neu gestartet!${data.message.includes('Backup') ? '\n' + data.message : ''}`);
            setTimeout(() => {
                location.reload();
            }, 500);
        } else {
            alert(`❌ Fehler: ${data.message}`);
            if (button) {
                button.disabled = false;
                button.innerHTML = originalText;
            }
        }
    } catch (error) {
        console.error('Error restarting bot:', error);
        alert(`❌ Fehler beim Neustarten des Bots: ${error.message}`);
        if (button) {
            button.disabled = false;
            button.innerHTML = originalText;
        }
    }
}

// Config Modal
let currentConfigSymbol = null;
let currentBotType = 'long';

async function showConfig(symbol, botType = 'long') {
    // Stop auto-refresh IMMEDIATELY - FIRST THING
    isModalOpen = true;
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
    
    currentConfigSymbol = symbol;
    currentBotType = botType;
    document.getElementById('configSymbol').textContent = `${botType.toUpperCase()} - ${symbol}`;
    
    // Show/hide fields based on bot type
    const longBotFields = document.getElementById('longBotFields');
    const shortBotFields = document.getElementById('shortBotFields');
    if (botType === 'short') {
        longBotFields.style.display = 'none';
        shortBotFields.style.display = 'block';
    } else {
        longBotFields.style.display = 'block';
        shortBotFields.style.display = 'none';
    }
    
    // Show modal first (so user sees it's loading)
    document.getElementById('configModal').style.display = 'block';
    
    // Wait a tiny bit to ensure page is not reloading
    await new Promise(resolve => setTimeout(resolve, 100));
    
    // Check if page is still loaded (not reloading)
    if (document.readyState === 'uninitialized' || document.readyState === 'loading') {
        // Page is reloading, close modal and show error
        document.getElementById('configModal').style.display = 'none';
        isModalOpen = false;
        alert('Bitte warten Sie, bis die Seite vollständig geladen ist, bevor Sie Config öffnen.');
        return;
    }
    
    try {
        // Create AbortController for timeout
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);
        
        const response = await fetch(`/api/bots/${symbol}/config?bot_type=${encodeURIComponent(botType)}`, {
            signal: controller.signal
        });
        
        clearTimeout(timeoutId);
        
        let data;
        try {
            data = await response.json();
        } catch (parseErr) {
            if (!response.ok) {
                const msg = response.status === 401 ? 'Bitte erneut einloggen.' : `Server-Fehler (HTTP ${response.status}).`;
                throw new Error(msg);
            }
            throw parseErr;
        }
        
        if (!response.ok) {
            const msg = (data && (data.detail || data.error)) || (response.status === 401 ? 'Bitte erneut einloggen.' : `HTTP ${response.status}`);
            throw new Error(msg);
        }
        
        // Show warning if using default config
        if (data.warning) {
            console.warn('Config warning:', data.warning);
        }
        
        if (data.error) {
            console.error('Config error:', data.error);
            // Still try to show default values
        }
        
        if (data.config) {
            // Fill form fields with config values
            const config = data.config;
            
            if (botType === 'short') {
                // Short-Bot fields
                document.getElementById('long_tp_percentage_short').value = config.long_tp_percentage || '';
                document.getElementById('long_reentry_step_percentage').value = config.long_reentry_step_percentage || '';
                document.getElementById('target_short_notional').value = config.initial_short_usdt ?? config.target_short_notional ?? '';
            } else {
                // Long-Bot fields
                document.getElementById('long_tp_percentage').value = config.long_tp_percentage || '';
                document.getElementById('short_tp_percentage').value = config.short_tp_percentage || '';
                document.getElementById('short_reentry_step_percentage').value = config.short_reentry_step_percentage || '';
                document.getElementById('target_long_notional').value = config.initial_long_usdt ?? config.target_long_notional ?? '';
            }
            
            // Common fields
            document.getElementById('burns_before_rebuy').value = config.burns_before_rebuy || '';
            document.getElementById('be_target_profit').value = config.be_target_profit || '';
            
            // Log for debugging
            console.log('Config loaded:', config);
        } else {
            throw new Error('Keine Config-Daten erhalten');
        }
    } catch (error) {
        // Only show error if it's not an abort (page reload)
        if (error.name !== 'AbortError' && error.name !== 'TimeoutError') {
            document.getElementById('configModal').style.display = 'none';
            isModalOpen = false;
            alert(`Fehler beim Laden der Config: ${error.message}`);
        } else {
            // Page is reloading, just close modal silently
            document.getElementById('configModal').style.display = 'none';
            isModalOpen = false;
        }
    }
}

function closeConfig() {
    document.getElementById('configModal').style.display = 'none';
    currentConfigSymbol = null;
    isModalOpen = false;
    
    // Resume auto-refresh when modal closes
    startAutoRefresh();
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('configModal');
    if (event.target == modal) {
        closeConfig();
    }
}

// Handle config form submission
document.addEventListener('DOMContentLoaded', function() {
    const configForm = document.getElementById('configForm');
    if (configForm) {
        configForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            
            if (!currentConfigSymbol) {
                alert('No symbol selected');
                return;
            }
            
            // Build form data based on bot type
            const formData = {
                burns_before_rebuy: parseInt(document.getElementById('burns_before_rebuy').value),
                be_target_profit: parseFloat(document.getElementById('be_target_profit').value),
                bot_type: currentBotType
            };
            
            if (currentBotType === 'short') {
                // Short-Bot fields
                formData.long_tp_percentage = parseFloat(document.getElementById('long_tp_percentage_short').value);
                formData.long_reentry_step_percentage = parseFloat(document.getElementById('long_reentry_step_percentage').value);
                formData.target_short_notional = parseFloat(document.getElementById('target_short_notional').value);
            } else {
                // Long-Bot fields
                formData.long_tp_percentage = parseFloat(document.getElementById('long_tp_percentage').value);
                formData.short_tp_percentage = parseFloat(document.getElementById('short_tp_percentage').value);
                formData.short_reentry_step_percentage = parseFloat(document.getElementById('short_reentry_step_percentage').value);
                formData.target_long_notional = parseFloat(document.getElementById('target_long_notional').value);
            }
            
            try {
                const response = await fetch(`/api/bots/${currentConfigSymbol}/config`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(formData)
                });
                
                const data = await response.json();
                if (data.success) {
                    alert(`Config für ${currentConfigSymbol} erfolgreich gespeichert!`);
                    closeConfig();
                    location.reload();
                } else {
                    alert(`Error: ${data.message}`);
                }
            } catch (error) {
                alert(`Error: ${error.message}`);
            }
        });
    }
});

// Auto-refresh function
function startAutoRefresh() {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
    
    // Only start auto-refresh if page is fully loaded
    if (document.readyState !== 'complete') {
        // Wait for page to load before starting auto-refresh
        window.addEventListener('load', function() {
            startAutoRefresh();
        });
        return;
    }
    
    autoRefreshInterval = setInterval(async () => {
        // Don't refresh if modal is open or page is still loading
        if (isModalOpen || document.readyState !== 'complete') {
            return;
        }
        
        // Check if log files have changed
        try {
            const response = await fetch('/api/logs/changed');
            const data = await response.json();
            
            // Get current symbol
            const urlParams = new URLSearchParams(window.location.search);
            let symbol = urlParams.get('symbol');
            
            if (!symbol) {
                const botSelector = document.getElementById('botSelector');
                if (botSelector && botSelector.value) {
                    symbol = botSelector.value;
                }
            }
            
            // Only update if logs changed for current symbol OR it's been more than 10 seconds
            const lastCheck = window.lastDataUpdate || 0;
            const timeSinceLastCheck = Date.now() / 1000 - lastCheck;
            const logsChanged = data.changed && data.changed.includes(symbol);
            const shouldUpdate = logsChanged || timeSinceLastCheck > 10;
            
            if (shouldUpdate && symbol) {
                window.lastDataUpdate = Date.now() / 1000;
                await updateBotData(symbol);
            }
        } catch (error) {
            console.error('Error checking for updates:', error);
            // Fallback: update every 10 seconds even if check fails
            const lastCheck = window.lastDataUpdate || 0;
            const timeSinceLastCheck = Date.now() / 1000 - lastCheck;
            
            if (timeSinceLastCheck > 10) {
                const urlParams = new URLSearchParams(window.location.search);
                let symbol = urlParams.get('symbol');
                let botType = urlParams.get('bot_type') || 'long';
                if (!symbol) {
                    const botSelector = document.getElementById('botSelector');
                    if (botSelector && botSelector.value) {
                        symbol = botSelector.value;
                        const selectedOption = botSelector.options[botSelector.selectedIndex];
                        botType = selectedOption ? selectedOption.getAttribute('data-bot-type') || 'long' : 'long';
                    }
                }
                if (symbol) {
                    window.lastDataUpdate = Date.now() / 1000;
                    await updateBotData(symbol, botType);
                }
            }
        }
    }, 2000); // Check every 2 seconds, update data if needed
}

// Update bot data without page reload
async function updateBotData(symbol) {
    try {
        const response = await fetch(`/api/bots/${symbol}`);
        const bot = await response.json();
        
        // Update position data
        if (bot.position) {
            updatePositionDisplay(bot.position);
        }
        
        // Update orders
        if (bot.position) {
            updateOrdersDisplay(bot.position);
        }
        
        // Update cycle
        if (bot.state) {
            updateCycleDisplay(bot.state);
        }
        
        // Update burns
        if (bot.state && bot.next_burn) {
            updateBurnsDisplay(bot.state, bot.next_burn);
        }
        
        // Update rebuy display
        if (bot.rebuy_info && bot.rebuy_info.valid && bot.state && bot.state.next_rebuy_in === 0) {
            updateRebuyDisplay(bot.rebuy_info);
        }
        
        // Update status
        if (bot.running !== undefined) {
            updateStatusDisplay(bot.running);
        }
        
        // Update spread
        if (bot.position) {
            updateSpreadDisplay(bot.position);
        }
        
    } catch (error) {
        console.error('Error updating bot data:', error);
    }
}

// Helper functions to update specific parts of the UI
function updatePositionDisplay(position) {
    // Update Long Position
    if (position.long && position.long.size) {
        const longSizeEl = document.querySelector('.position-size-coins-long');
        const longUsdtEl = document.querySelector('.position-long .position-value-usdt');
        if (longSizeEl) longSizeEl.textContent = `${position.long.size.toFixed(2)}`;
        if (longUsdtEl) longUsdtEl.textContent = `${position.long.value_usdt ? position.long.value_usdt.toFixed(2) : 'N/A'}`;
    }
    
    // Update Short Position
    if (position.short && position.short.size) {
        const shortSizeEl = document.querySelector('.position-size-coins-short');
        const shortUsdtEl = document.querySelector('.position-short .position-value-usdt');
        if (shortSizeEl) shortSizeEl.textContent = `${position.short.size.toFixed(2)}`;
        if (shortUsdtEl) shortUsdtEl.textContent = `${position.short.value_usdt ? position.short.value_usdt.toFixed(2) : 'N/A'}`;
    }
}

function updateOrdersDisplay(position) {
    // Update Long TP/SL
    if (position.long) {
        const longOrderCard = document.querySelector('.order-long');
        if (longOrderCard) {
            const orderItems = longOrderCard.querySelectorAll('.order-item');
            // TP is first item
            if (orderItems[0] && position.long.tp_set && position.long.tp_price) {
                const tpStatus = orderItems[0].querySelector('.order-status');
                if (tpStatus) {
                    tpStatus.innerHTML = `✅ ${position.long.tp_price.toFixed(5)}`;
                    tpStatus.className = 'order-status order-set';
                }
            }
            // SL is second item
            if (orderItems[1] && position.long.sl_set && position.long.sl_price) {
                const slStatus = orderItems[1].querySelector('.order-status');
                if (slStatus) {
                    slStatus.innerHTML = `✅ ${position.long.sl_price.toFixed(5)}`;
                    slStatus.className = 'order-status order-set';
                } else {
                    // Create SL element if it doesn't exist
                    const slItem = orderItems[1];
                    if (slItem) {
                        slItem.innerHTML = `
                            <span class="order-label">SL:</span>
                            <span class="order-status order-set">✅ ${position.long.sl_price.toFixed(5)}</span>
                        `;
                    }
                }
            }
        }
    }
    
    // Update Short TP/SL
    if (position.short) {
        const shortOrderCard = document.querySelector('.order-short');
        if (shortOrderCard) {
            const orderItems = shortOrderCard.querySelectorAll('.order-item');
            // TP is first item
            if (orderItems[0] && position.short.tp_set && position.short.tp_price) {
                const tpStatus = orderItems[0].querySelector('.order-status');
                if (tpStatus) {
                    tpStatus.innerHTML = `✅ ${position.short.tp_price.toFixed(5)}`;
                    tpStatus.className = 'order-status order-set';
                }
            }
            // SL is second item
            if (orderItems[1] && position.short.sl_set && position.short.sl_price) {
                const slStatus = orderItems[1].querySelector('.order-status');
                if (slStatus) {
                    slStatus.innerHTML = `✅ ${position.short.sl_price.toFixed(5)}`;
                    slStatus.className = 'order-status order-set';
                } else {
                    // Create SL element if it doesn't exist
                    const slItem = orderItems[1];
                    if (slItem) {
                        slItem.innerHTML = `
                            <span class="order-label">SL:</span>
                            <span class="order-status order-set">✅ ${position.short.sl_price.toFixed(5)}</span>
                        `;
                    }
                }
            }
        }
    }
}

function updateCycleDisplay(state) {
    const progressBar = document.querySelector('.cycle-progress-bar');
    const burnCount = document.querySelector('.cycle-burn-count');
    const nextRebuy = document.querySelector('.cycle-next-burn, .cycle-rebuy-badge');
    
    if (progressBar && state.burns_before_rebuy) {
        const progress = (state.burn_count / state.burns_before_rebuy) * 100;
        progressBar.style.width = `${progress}%`;
    }
    if (burnCount) {
        burnCount.textContent = `🔥 ${state.burn_count}/${state.burns_before_rebuy}`;
    }
    if (nextRebuy) {
        if (state.next_rebuy_in === 0) {
            nextRebuy.innerHTML = '<span class="cycle-rebuy-badge">⚡ Rebuy!</span>';
        } else {
            nextRebuy.textContent = `${state.next_rebuy_in} bis Rebuy`;
        }
    }
}

function updateBurnsDisplay(state, next_burn) {
    // Find all burns-item elements and update them
    const burnsItems = document.querySelectorAll('.burns-item');
    if (burnsItems.length >= 3) {
        // Total Burned (first item)
        const totalBurnedValue = burnsItems[0].querySelector('.burns-value');
        if (totalBurnedValue && state.total_burned !== undefined) {
            totalBurnedValue.textContent = state.total_burned.toFixed(2);
        }
        
        // Next Burn Coins (second item)
        const nextBurnCoins = burnsItems[1].querySelector('.burns-next-burn-coins');
        if (nextBurnCoins && next_burn.valid && next_burn.burn_size_coins) {
            nextBurnCoins.textContent = `${next_burn.burn_size_coins.toFixed(2)} Coins`;
        }
        
        // Next Burn USDT (third item)
        const nextBurnUsdt = burnsItems[2].querySelector('.burns-next-burn-usdt');
        if (nextBurnUsdt && next_burn.valid && next_burn.burn_size_usdt) {
            nextBurnUsdt.textContent = `${next_burn.burn_size_usdt.toFixed(2)} USDT`;
        }
    }
}

function updateRebuyDisplay(rebuy_info) {
    // Update rebuy display if rebuy is upcoming
    const rebuyCoins = document.querySelector('.burns-rebuy-coins');
    const rebuyUsdt = document.querySelector('.burns-rebuy-usdt');
    const rebuyNewSize = document.querySelector('.burns-rebuy-new-size');
    const rebuyInfoItem = document.querySelector('.rebuy-info-item');
    
    if (rebuy_info.not_needed) {
        // Hide rebuy info if not needed
        if (rebuyInfoItem) {
            rebuyInfoItem.style.display = 'none';
        }
    } else {
        // Show rebuy info
        if (rebuyInfoItem) {
            rebuyInfoItem.style.display = 'block';
        }
        if (rebuyCoins && rebuy_info.rebuy_size_coins !== undefined) {
            rebuyCoins.textContent = `${rebuy_info.rebuy_size_coins.toFixed(2)} Coins`;
        }
        if (rebuyUsdt && rebuy_info.rebuy_size_usdt !== undefined) {
            rebuyUsdt.textContent = `${rebuy_info.rebuy_size_usdt.toFixed(2)} USDT`;
        }
        if (rebuyNewSize && rebuy_info.new_long_size_usdt !== undefined) {
            rebuyNewSize.textContent = `${rebuy_info.new_long_size_usdt.toFixed(2)} USDT`;
        }
    }
}

function updateRebuyDisplay(rebuy_info) {
    // Update rebuy display if rebuy is upcoming
    const rebuyCoins = document.querySelector('.burns-rebuy-coins');
    const rebuyUsdt = document.querySelector('.burns-rebuy-usdt');
    const rebuyNewSize = document.querySelector('.burns-rebuy-new-size');
    const rebuyInfoItem = document.querySelector('.rebuy-info-item');
    
    if (rebuy_info.not_needed) {
        // Hide rebuy info if not needed
        if (rebuyInfoItem) {
            rebuyInfoItem.style.display = 'none';
        }
    } else {
        // Show rebuy info
        if (rebuyInfoItem) {
            rebuyInfoItem.style.display = 'block';
        }
        if (rebuyCoins && rebuy_info.rebuy_size_coins !== undefined) {
            rebuyCoins.textContent = `${rebuy_info.rebuy_size_coins.toFixed(2)} Coins`;
        }
        if (rebuyUsdt && rebuy_info.rebuy_size_usdt !== undefined) {
            rebuyUsdt.textContent = `${rebuy_info.rebuy_size_usdt.toFixed(2)} USDT`;
        }
        if (rebuyNewSize && rebuy_info.new_long_size_usdt !== undefined) {
            rebuyNewSize.textContent = `${rebuy_info.new_long_size_usdt.toFixed(2)} USDT`;
        }
    }
}

function updateStatusDisplay(running) {
    const statusEl = document.querySelector('.status-badge');
    if (statusEl) {
        if (running) {
            statusEl.innerHTML = '<span class="status-dot status-active"></span> Running';
            statusEl.className = 'status-badge status-running';
        } else {
            statusEl.innerHTML = '<span class="status-dot status-inactive"></span> Stopped';
            statusEl.className = 'status-badge status-stopped';
        }
    }
}

function updateSpreadDisplay(position) {
    if (position.long && position.long.entry_price && position.short && position.short.entry_price) {
        const spread = ((position.long.entry_price - position.short.entry_price) / position.short.entry_price) * 100;
        const spreadEl = document.querySelector('.spread-value');
        if (spreadEl) {
            spreadEl.textContent = `${spread.toFixed(2)}%`;
        }
    }
}

// Service control function
async function controlService(serviceKey, action, event) {
    // Get button element from event or use fallback
    let button;
    if (event && event.target) {
        button = event.target;
    } else if (window.event && window.event.target) {
        button = window.event.target;
    } else {
        // Fallback: find button by service key and action
        const buttons = document.querySelectorAll(`[onclick*="controlService('${serviceKey}', '${action}'"]`);
        button = buttons[0] || null;
    }
    
    if (!button) {
        console.error('Could not find button element');
        alert(`❌ Fehler: Button nicht gefunden`);
        return;
    }
    
    const originalText = button.innerHTML;
    
    // Disable button and show loading
    button.disabled = true;
    button.innerHTML = '⏳ ...';
    
    try {
        const response = await fetch(`/api/services/${serviceKey}/${action}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        
        if (data.success) {
            // Show success message
            alert(`✅ ${data.message}`);
            // Reload page after short delay to show updated status
            setTimeout(() => {
                location.reload();
            }, 1000);
        } else {
            alert(`❌ Fehler: ${data.message}`);
            button.disabled = false;
            button.innerHTML = originalText;
        }
    } catch (error) {
        console.error('Error controlling service:', error);
        alert(`❌ Fehler beim ${action === 'start' ? 'Starten' : action === 'stop' ? 'Stoppen' : 'Neustarten'} des Services: ${error.message}`);
        button.disabled = false;
        button.innerHTML = originalText;
    }
}

// Start auto-refresh on page load
startAutoRefresh();

// Add event listeners to all config buttons to stop auto-refresh immediately on click
document.addEventListener('DOMContentLoaded', function() {
    // Stop auto-refresh when any config button is clicked (before showConfig is called)
    document.addEventListener('click', function(event) {
        if (event.target && event.target.classList.contains('btn-config')) {
            // Stop auto-refresh IMMEDIATELY when config button is clicked
            isModalOpen = true;
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
                autoRefreshInterval = null;
            }
        }
    }, true); // Use capture phase to catch event before it reaches onclick handler
    
    // Add event listener for bot selector
    const botSelector = document.getElementById('botSelector');
    if (botSelector) {
        botSelector.addEventListener('change', function(e) {
            const symbol = e.target.value;
            const selectedOption = e.target.options[e.target.selectedIndex];
            const botType = selectedOption ? selectedOption.getAttribute('data-bot-type') || 'long' : 'long';
            console.log('Bot selector changed to:', symbol, 'bot_type:', botType);
            switchBot(symbol);
        });
        console.log('Bot selector event listener added');
    } else {
        console.warn('Bot selector not found!');
    }
});

// Test Alert function
async function testAlert() {
    try {
        const response = await fetch('/api/alerts/test', {
            method: 'POST'
        });
        const data = await response.json();
        if (data.success) {
            alert('✅ Test-Alert gesendet! Prüfe dein Handy.');
        } else {
            alert(`❌ Fehler: ${data.message}`);
        }
    } catch (error) {
        alert(`❌ Fehler: ${error.message}`);
    }
}

