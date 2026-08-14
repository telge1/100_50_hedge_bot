// Position Calculator JavaScript

let entries = [];

// Initialize with first entry
document.addEventListener('DOMContentLoaded', function() {
    addEntry();
});

function addEntry() {
    const entryIndex = entries.length;
    entries.push({
        index: entryIndex,
        price: null,
        size: null
    });
    
    renderTable();
    
    // Focus on the new entry price input
    const newPriceInput = document.getElementById(`entryPrice_${entryIndex}`);
    if (newPriceInput) {
        setTimeout(() => newPriceInput.focus(), 100);
    }
}

function removeEntry(index) {
    if (entries.length <= 1) {
        alert('Mindestens ein Entry muss vorhanden sein.');
        return;
    }
    
    entries = entries.filter(entry => entry.index !== index);
    
    // Reindex entries
    entries.forEach((entry, idx) => {
        entry.index = idx;
    });
    
    renderTable();
    calculateAverages();
}

function updateEntry(index, field, value) {
    const entry = entries.find(e => e.index === index);
    if (entry) {
        if (field === 'price') {
            entry.price = value ? parseFloat(value) : null;
        } else if (field === 'size') {
            entry.size = value ? parseFloat(value) : null;
        }
        
        calculateAverages();
    }
}

function calculateAverages() {
    // Calculate cumulative averages for each row
    let totalSize = 0;
    let totalValue = 0;
    
    entries.forEach((entry, idx) => {
        if (entry.price && entry.size) {
            totalSize += entry.size;
            totalValue += entry.price * entry.size;
            
            // Update average price for this row (cumulative average)
            const averagePrice = totalSize > 0 ? totalValue / totalSize : 0;
            updateAveragePriceDisplay(idx, averagePrice);
        } else {
            updateAveragePriceDisplay(idx, null);
        }
    });
    
    // Update summary
    const finalAverage = totalSize > 0 ? totalValue / totalSize : 0;
    updateSummary(totalSize, finalAverage, totalValue);
}

function updateAveragePriceDisplay(index, averagePrice) {
    const avgCell = document.getElementById(`averagePrice_${index}`);
    if (avgCell) {
        if (averagePrice !== null && averagePrice > 0) {
            avgCell.textContent = averagePrice.toFixed(5);
        } else {
            avgCell.textContent = '-';
        }
    }
}

function updateSummary(totalSize, averagePrice, totalValue) {
    const totalSizeEl = document.getElementById('totalSize');
    const finalAverageEl = document.getElementById('finalAveragePrice');
    const totalValueEl = document.getElementById('totalValue');
    
    if (totalSizeEl) {
        totalSizeEl.textContent = totalSize.toFixed(2);
    }
    
    if (finalAverageEl) {
        if (averagePrice > 0) {
            finalAverageEl.textContent = averagePrice.toFixed(5);
        } else {
            finalAverageEl.textContent = '0.00000';
        }
    }
    
    if (totalValueEl) {
        totalValueEl.textContent = totalValue.toFixed(2);
    }
}

function renderTable() {
    const tbody = document.getElementById('entriesTableBody');
    if (!tbody) return;
    
    tbody.innerHTML = '';
    
    entries.forEach((entry, idx) => {
        const row = document.createElement('tr');
        row.className = 'entry-row';
        
        const isFirst = idx === 0;
        const isLast = idx === entries.length - 1;
        
        row.innerHTML = `
            <td class="entry-number">${idx + 1}</td>
            <td>
                <input 
                    type="number" 
                    step="0.00001" 
                    class="entry-input" 
                    id="entryPrice_${entry.index}" 
                    placeholder="0.00000"
                    value="${entry.price || ''}"
                    oninput="updateEntry(${entry.index}, 'price', this.value)"
                />
            </td>
            <td>
                <input 
                    type="number" 
                    step="0.01" 
                    class="entry-input" 
                    id="entrySize_${entry.index}" 
                    placeholder="0.00"
                    value="${entry.size || ''}"
                    oninput="updateEntry(${entry.index}, 'size', this.value)"
                />
            </td>
            <td class="average-price-cell">
                <span id="averagePrice_${entry.index}" class="average-price-value">-</span>
            </td>
            <td class="entry-actions-cell">
                ${!isFirst ? `<button class="btn-remove-entry" onclick="removeEntry(${entry.index})" title="Remove Entry">➖</button>` : ''}
                ${isLast ? `<button class="btn-add-entry-inline" onclick="addEntry()" title="Add Entry">➕</button>` : ''}
            </td>
        `;
        
        tbody.appendChild(row);
    });
    
    // Recalculate after rendering
    calculateAverages();
}

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
