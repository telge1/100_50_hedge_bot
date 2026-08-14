// Cobertura calculator — mirrors research/cobertura_calc.py

const COBERTURA_FEE_PERCENT = 0.055;

function parsePositive(value) {
    const n = parseFloat(String(value).replace(',', '.'));
    return Number.isFinite(n) && n > 0 ? n : null;
}

function positionPnl(side, entryPrice, exitPrice, qty) {
    if (side === 'long') {
        return qty * (exitPrice - entryPrice);
    }
    return qty * (entryPrice - exitPrice);
}

function closeFee(qty, price, feeRatePercent) {
    return qty * price * feeRatePercent / 100.0;
}

function formatNum(value, digits) {
    if (value === null || value === undefined || !Number.isFinite(value)) {
        return '—';
    }
    return value.toFixed(digits);
}

function formatUsdt(value, digits = 2) {
    if (value === null || value === undefined || !Number.isFinite(value)) {
        return '—';
    }
    return `${value.toFixed(digits)} USDT`;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) {
        el.textContent = text;
    }
}

function clearResultFields() {
    setText('coberturaTargetPrice', '—');
    setText('coberturaCloseCoins', '—');
    setText('coberturaCloseUsd', '—');
    setText('coberturaRemainCoins', '—');
    setText('coberturaRemainUsd', '—');
    setText('coberturaRemainPercent', '—');
    setText('coberturaUnrealizedPnl', '—');
}

function showCoberturaMessage(text, kind, keepResults = false) {
    const el = document.getElementById('coberturaMessage');
    const results = document.getElementById('coberturaResults');
    if (!el) return;

    if (!text) {
        el.hidden = true;
        el.textContent = '';
        el.className = 'cobertura-message';
        return;
    }

    el.hidden = false;
    el.textContent = text;
    el.className = `cobertura-message cobertura-message-${kind || 'info'}`;
    if (results && !keepResults) {
        results.hidden = true;
    }
}

function calculateCobertura() {
    const longQty = parsePositive(document.getElementById('coberturaLongQty')?.value);
    const longEntry = parsePositive(document.getElementById('coberturaLongEntry')?.value);
    const shortQty = parsePositive(document.getElementById('coberturaShortQty')?.value);
    const shortEntry = parsePositive(document.getElementById('coberturaShortEntry')?.value);
    const movePercent = parsePositive(document.getElementById('coberturaMovePercent')?.value);
    const closeSide = document.getElementById('coberturaCloseSide')?.value || 'short';

    const results = document.getElementById('coberturaResults');

    if (!longQty || !longEntry || !shortQty || !shortEntry || !movePercent) {
        showCoberturaMessage('', 'info');
        if (results) results.hidden = true;
        return;
    }

    let targetPrice;
    let winnerSide;
    let loserSide;
    let winnerQty;
    let loserQty;
    let loserEntry;
    let winnerEntry;

    if (closeSide === 'short') {
        targetPrice = shortEntry * (1 - movePercent / 100);
        winnerSide = 'short';
        loserSide = 'long';
        winnerQty = shortQty;
        loserQty = longQty;
        loserEntry = longEntry;
        winnerEntry = shortEntry;
    } else {
        targetPrice = longEntry * (1 + movePercent / 100);
        winnerSide = 'long';
        loserSide = 'short';
        winnerQty = longQty;
        loserQty = shortQty;
        loserEntry = shortEntry;
        winnerEntry = longEntry;
    }

    const winnerGrossPnl = positionPnl(winnerSide, winnerEntry, targetPrice, winnerQty);
    const winnerCloseFee = closeFee(winnerQty, targetPrice, COBERTURA_FEE_PERCENT);
    const availableProfit = winnerGrossPnl - winnerCloseFee;
    const lossPerCoin = -positionPnl(loserSide, loserEntry, targetPrice, 1.0);

    clearResultFields();
    setText('coberturaTargetPrice', formatNum(targetPrice, 8));

    if (results) {
        results.hidden = false;
    }

    if (winnerGrossPnl <= 0) {
        showCoberturaMessage(
            'Die ausgewählte Position ist am Zielpreis nicht im Gewinn.',
            'warning',
            true
        );
        return;
    }

    if (availableProfit <= 0) {
        showCoberturaMessage(
            'Nach Gebühren bleibt kein Gewinn zur Reduzierung der Gegenposition übrig.',
            'warning',
            true
        );
        return;
    }

    if (lossPerCoin <= 0) {
        showCoberturaMessage(
            'Die Gegenposition befindet sich am Zielpreis nicht im Verlust. Eine verlustfinanzierte Reduzierung ist nicht notwendig.',
            'info',
            true
        );
        return;
    }

    const closeCostPerCoin = lossPerCoin + targetPrice * COBERTURA_FEE_PERCENT / 100.0;
    const closeQty = Math.min(availableProfit / closeCostPerCoin, loserQty);
    const remainQty = loserQty - closeQty;
    const remainPercent = loserQty > 0 ? (remainQty / loserQty) * 100 : 0;
    const unrealizedPnl = positionPnl(loserSide, loserEntry, targetPrice, remainQty);

    setText('coberturaCloseCoins', formatNum(closeQty, 8));
    setText('coberturaCloseUsd', formatUsdt(closeQty * loserEntry));
    setText('coberturaRemainCoins', formatNum(remainQty, 8));
    setText('coberturaRemainUsd', formatUsdt(remainQty * loserEntry));
    setText('coberturaRemainPercent', `${formatNum(remainPercent, 2)} %`);
    setText('coberturaUnrealizedPnl', formatUsdt(unrealizedPnl));

    showCoberturaMessage('', 'info');
    if (results) {
        results.hidden = false;
    }
}

document.addEventListener('DOMContentLoaded', function () {
    calculateCobertura();
});
