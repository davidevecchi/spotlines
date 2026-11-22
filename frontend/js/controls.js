import { detailState } from './state.js';
import { map } from './map.js';
import { _updateCatenaryAt } from './catenary-ui.js';
import { sliderEl } from './draw.js';
import { LINE_PRESETS, GRAVITY } from './constants.js';

const divider = ' · ';

export function updateLineHint() {
    const preset = document.getElementById('linePreset');
    const tension = document.getElementById('preTension');
    const walker = document.getElementById('walkerKg');
    const hint = document.getElementById('line-hint');
    if (!hint) return;
    const label = preset.options[preset.selectedIndex].text;
    hint.textContent = `${label}${divider}${tension.value} kN${divider}${walker.value} kg`;
    // Only visible when the section is collapsed
    const det = hint.closest('details');
    hint.style.display = (!det || det.open) ? 'none' : '';
}

// Preset dropdown handler
export function onLinePresetChange() {
    const preset = document.getElementById('linePreset').value;
    const isCustom = preset === 'custom';
    document.querySelectorAll('.line-custom input').forEach(input => {
        input.disabled = !isCustom;
    });
    if (!isCustom) {
        const p = LINE_PRESETS[preset] || LINE_PRESETS.tubular25;
        document.getElementById('lineEA').value = +(p.EA / 1000).toFixed(1);
        document.getElementById('lineW').value  = +(p.w * 1000 / GRAVITY).toFixed(1);
    }
    detailState.catState = null;
    _updateCatenaryAt(detailState.lastCatT);
    updateLineHint();
}

// Re-draw on any line/load param change
['preTension', 'walkerKg', 'lineEA', 'lineW'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => {
        detailState.catState = null;
        _updateCatenaryAt(detailState.lastCatT);
        if (id === 'preTension') updateLineHint();
    });
});

export function updateFiltersHint() {
    const hint = document.getElementById('filters-hint');
    if (!hint) return;
    const vals = sliderEl.noUiSlider.get();
    const minM = Math.round(vals[0]);
    const maxM = Math.round(vals[1]);
    const slope = document.getElementById('maxSlope').value;
    const clr = document.getElementById('clearanceM').value;
    const water = document.getElementById('water').value;
    const waterTxt = water === 'only' ? `${divider}water only` : water === 'exclude' ? `${divider}no water` : '';
    hint.textContent = `${minM}–${maxM} m${divider}≤ ${slope}°${divider}←${clr} m→${waterTxt}`;
}

// Toggle hint visibility on open/close
(function () {
    const filtersDet = document.querySelector('#filters-hint')?.closest('details');
    const lineDet = document.querySelector('#line-hint')?.closest('details');

    if (filtersDet) {
        filtersDet.addEventListener('toggle', () => {
            const h = document.getElementById('filters-hint');
            if (h) h.style.display = filtersDet.open ? 'none' : '';
        });
    }
    if (lineDet) {
        lineDet.addEventListener('toggle', () => {
            const h = document.getElementById('line-hint');
            if (h) h.style.display = lineDet.open ? 'none' : '';
        });
    }
})();

// Keep filters hint up-to-date whenever values change
sliderEl.noUiSlider.on('update', updateFiltersHint);
['maxSlope', 'clearanceM', 'water'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', updateFiltersHint);
    document.getElementById(id)?.addEventListener('change', updateFiltersHint);
});

export function geolocate() {
    if (!navigator.geolocation) return;
    const btn = document.getElementById('geolocate-btn');
    btn.style.opacity = '0.5';
    navigator.geolocation.getCurrentPosition(
        pos => {
            map.setView([pos.coords.latitude, pos.coords.longitude], 15);
            btn.style.opacity = '1';
        },
        () => {
            btn.style.opacity = '1';
        },
        {timeout: 8000}
    );
}
