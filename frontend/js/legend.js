import { overlayState } from './state.js';
import { cmapColor, slopeColor, terrainColor } from './colormaps.js';
import { MAX_SLOPE_DEG } from './constants.js';

export function _fillBar(canvasId, colorFn) {
    const canvas = document.getElementById(canvasId);
    const w = canvas.offsetWidth || 160;
    canvas.width = w;
    const ctx = canvas.getContext('2d');
    for (let x = 0; x < w; x++) {
        ctx.fillStyle = colorFn(x / (w - 1));
        ctx.fillRect(x, 0, 1, canvas.height);
    }
}

export function drawLegend(minM, maxM) {
    document.getElementById('legend-wrap').style.display = 'block';
    document.getElementById('leg-min').textContent = minM + ' m';
    document.getElementById('leg-max').textContent = maxM + ' m';
    _fillBar('legend-bar', cmapColor);
}

export function showSlopeLegend(visible) {
    document.getElementById('slope-legend').style.display = visible ? 'block' : 'none';
    if (!visible) return;
    const {min, max} = overlayState.slopeStats;
    _fillBar('slope-bar', x => slopeColor((min + x * (max - min)) / MAX_SLOPE_DEG));
    document.getElementById('slope-leg-min').textContent = min.toFixed(1) + '°';
    document.getElementById('slope-leg-max').textContent = max.toFixed(1) + '°';
}

export function showElevLegend(visible) {
    document.getElementById('elev-legend').style.display = visible ? 'block' : 'none';
    if (!visible) return;
    const {min, max} = overlayState.elevStats;
    _fillBar('elev-bar', t => terrainColor(t));
    document.getElementById('elev-leg-min').textContent = min + ' m';
    document.getElementById('elev-leg-max').textContent = max + ' m';
}
