import { _normalizeLanduseSegs, featureColor, _escXml } from './corridor.js';

// Nice ticks: pick step from STEPS that gives count closest to n
export function _niceTicks(lo, hi, n = 5) {
    const range = hi - lo;
    if (range < 1) return [];
    const STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
    const step = STEPS.reduce((best, s) =>
        Math.abs(Math.round(range / s) - n) < Math.abs(Math.round(range / best) - n) ? s : best
    );
    const start = Math.ceil(lo / step) * step;
    const ticks = [];
    for (let v = start; v <= hi + 1e-9; v += step) ticks.push(Math.round(v));
    return ticks;
}

// Single-pass light smooth: [0.15, 0.7, 0.15], endpoints fixed
export function _smoothElevs(elevs) {
    if (!elevs || elevs.length < 3) return elevs;
    return elevs.map((v, i) => {
        if (i === 0 || i === elevs.length - 1 || v == null) return v;
        const a = elevs[i - 1], b = elevs[i + 1];
        if (a == null || b == null) return v;
        return 0.15 * a + 0.7 * v + 0.15 * b;
    });
}

// Catmull-Rom spline → SVG cubic bezier path
export function catmullRomPath(pts) {
    if (!pts || pts.length < 2) return '';
    if (pts.length === 2) return `M ${pts[0][0]},${pts[0][1]} L ${pts[1][0]},${pts[1][1]}`;
    let d = `M ${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[Math.max(0, i - 1)];
        const p1 = pts[i];
        const p2 = pts[i + 1];
        const p3 = pts[Math.min(pts.length - 1, i + 2)];
        const cp1x = (p1[0] + (p2[0] - p0[0]) / 6).toFixed(2);
        const cp1y = (p1[1] + (p2[1] - p0[1]) / 6).toFixed(2);
        const cp2x = (p2[0] - (p3[0] - p1[0]) / 6).toFixed(2);
        const cp2y = (p2[1] - (p3[1] - p1[1]) / 6).toFixed(2);
        d += ` C ${cp1x},${cp1y} ${cp2x},${cp2y} ${p2[0]},${p2[1]}`;
    }
    return d;
}

// Shared setup for both elevation SVG builders: smooth, compute min/max, find valid index bounds.
export function _buildElevBase(elevs, elevA, elevB) {
    elevs = _smoothElevs(elevs);
    const valid = elevs.filter(e => e != null);
    if (!valid.length) return null;
    const allVals = [...valid];
    if (elevA != null) allVals.push(elevA);
    if (elevB != null) allVals.push(elevB);
    const mn = Math.min(...allVals) - 1, mx = Math.max(...allVals) + 1;
    const firstIdx = elevs.findIndex(e => e != null);
    const lastIdx = elevs.length - 1 - [...elevs].reverse().findIndex(e => e != null);
    return {smoothed: elevs, mn, mx, n: elevs.length, firstIdx, lastIdx};
}

export function elevProfileSvgCompact(elevs, elevA, elevB) {
    if (!elevs || !elevs.length) return '';
    const base = _buildElevBase(elevs, elevA, elevB);
    if (!base) return '';
    const {smoothed, mn, mx, n, firstIdx, lastIdx} = base;
    elevs = smoothed;
    const W = 200, H = 68, PX = 4, PY = 18;
    const rng = mx - mn;
    const sx = i => PX + (i / (n - 1)) * (W - 2 * PX);
    const sy = e => e != null ? H - PY - ((e - mn) / rng) * (H - 2 * PY) : null;
    const pts = elevs.map((e, i) => e != null ? [sx(i), sy(e)] : null).filter(Boolean);
    const sp = catmullRomPath(pts);
    const fp = sp ? sp + ` L ${sx(lastIdx).toFixed(2)},${H} L ${sx(firstIdx).toFixed(2)},${H} Z` : '';
    let slackLine = '';
    if (elevA != null && elevB != null) {
        const x0 = sx(0).toFixed(2), x1 = sx(n - 1).toFixed(2);
        const y0 = sy(elevA).toFixed(2), y1 = sy(elevB).toFixed(2);
        slackLine = `<line x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}" stroke="#b45309" stroke-width="1.8" stroke-dasharray="4 2.5" opacity="0.85"/>` +
            `<circle cx="${x0}" cy="${y0}" r="2.5" fill="#b45309"/>` +
            `<circle cx="${x1}" cy="${y1}" r="2.5" fill="#b45309"/>` +
            `<text x="${x0}" y="${(parseFloat(y0) - 6).toFixed(2)}" text-anchor="middle" font-size="10" fill="#b45309" font-family="system-ui,sans-serif" font-weight="600">${Math.round(elevA)}m</text>` +
            `<text x="${x1}" y="${(parseFloat(y1) - 6).toFixed(2)}" text-anchor="middle" font-size="10" fill="#b45309" font-family="system-ui,sans-serif" font-weight="600">${Math.round(elevB)}m</text>`;
    }
    return `<svg width="${W}" height="${H}" style="display:block;margin-top:5px;border-radius:3px">
    <path d="${fp}" fill="rgba(13,148,136,0.08)"/>
    <path d="${sp}" fill="none" stroke="#0d9488" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    ${slackLine}</svg>`;
}

export function elevSvg(terrain, elevA, elevB, corridorFeatures, distance_m, W, H) {
    if (!terrain) return '';
    const base = _buildElevBase(terrain, elevA, elevB);
    if (!base) return '';
    const {smoothed, mn, mx, n, firstIdx, lastIdx} = base;
    terrain = smoothed;
    W = W || 292;
    H = H || 130;
    const PL = 2, PR = 2, PT = 8, PB = 32;
    const iW = W - PL - PR, iH = H - PT - PB;
    const xAxisY = PT + iH;
    const rng = mx - mn;
    const px = i => PL + (i / (n - 1)) * iW;
    const py = e => PT + iH - ((e - mn) / rng) * iH;

    // Grid lines only, no Y-axis labels
    let grid = '';
    const niceTicks = _niceTicks(mn + 1, mx - 1, 3);
    for (const v of niceTicks) {
        const gy = py(v).toFixed(1);
        grid += `<line x1="${PL}" y1="${gy}" x2="${W - PR}" y2="${gy}" stroke="rgba(120,90,50,0.2)" stroke-width="1"/>`;
    }

    // X-axis ticks and labels (~5 ticks, plus full-length mark if gap >= step/2)
    let xTickMarks = '', xLabels = '';
    if (distance_m > 0) {
        const ticks = _niceTicks(0, distance_m);
        const step = ticks.length >= 2 ? ticks[1] - ticks[0] : 0;
        for (const d of ticks) {
            const tx = (PL + (d / distance_m) * iW).toFixed(1);
            xTickMarks += `<line x1="${tx}" y1="${xAxisY}" x2="${tx}" y2="${(xAxisY + 3).toFixed(1)}" stroke="rgba(120,90,50,0.45)" stroke-width="1"/>`;
            xLabels += `<text x="${tx}" y="${(xAxisY + 12).toFixed(1)}" font-size="9.5" fill="#78716c" text-anchor="${d === 0 ? 'start' : 'middle'}" font-family="system-ui,sans-serif">${d}m</text>`;
        }
        const last = ticks[ticks.length - 1] ?? 0;
        if (Math.round(distance_m) !== last && (step === 0 || Math.round(distance_m) - last >= step / 2)) {
            const tx = (W - PR).toFixed(1);
            xTickMarks += `<line x1="${tx}" y1="${xAxisY}" x2="${tx}" y2="${(xAxisY + 3).toFixed(1)}" stroke="rgba(120,90,50,0.45)" stroke-width="1"/>`;
            xLabels += `<text x="${tx}" y="${(xAxisY + 12).toFixed(1)}" font-size="9.5" fill="#78716c" text-anchor="end" font-family="system-ui,sans-serif">${Math.round(distance_m)}m</text>`;
        }
    }

    // Terrain strips — osmlanduse only, same normalization as the strip bar.
    let strips = '';
    if (corridorFeatures && corridorFeatures.length) {
        const luFeats = corridorFeatures.filter(f => f.category === 'osmlanduse');
        for (const s of _normalizeLanduseSegs(luFeats)) {
            const x1 = PL + s.t_start * iW;
            const w = (s.t_end - s.t_start) * iW;
            strips += `<rect x="${x1.toFixed(2) + 1}" y="${PT}" width="${Math.max(w, 0).toFixed(2) - 2}" height="${iH}" fill="${s.col}" opacity="0.15"/>`;
        }
    }

    // Smooth profile path
    const pts = terrain.map((e, i) => e != null ? [px(i), py(e)] : null).filter(Boolean);
    const sp = catmullRomPath(pts);
    const fp = sp + ` L ${px(lastIdx).toFixed(2)},${xAxisY.toFixed(2)} L ${px(firstIdx).toFixed(2)},${xAxisY.toFixed(2)} Z`;

    // Slackline with inline elevation labels above anchor circles
    let slackLine = '';
    if (elevA != null && elevB != null) {
        const yA = py(elevA).toFixed(2), yB = py(elevB).toFixed(2);
        const xA = PL.toFixed(2), xB = (W - PR).toFixed(2);
        slackLine =
            `<line x1="${xA}" y1="${yA}" x2="${xB}" y2="${yB}" stroke="#b45309" stroke-width="2" stroke-dasharray="5 3" opacity="0.85"/>` +
            `<circle cx="${xA}" cy="${yA}" r="3.5" fill="#b45309"/>` +
            `<circle cx="${xB}" cy="${yB}" r="3.5" fill="#b45309"/>` +
            `<text x="${(PL + 7).toFixed(1)}" y="${(parseFloat(yA) - 7).toFixed(1)}" font-size="9.5" fill="#b45309" text-anchor="start" font-family="system-ui,sans-serif" font-weight="600">${Math.round(elevA)}m</text>` +
            `<text x="${(W - PR - 7).toFixed(1)}" y="${(parseFloat(yB) - 7).toFixed(1)}" font-size="9.5" fill="#b45309" text-anchor="end" font-family="system-ui,sans-serif" font-weight="600">${Math.round(elevB)}m</text>`;
    }

    // Interactive cursor group (hidden until hover)
    const cursor = `<g class="elev-cursor" display="none">
    <line class="elev-cursor-line" x1="${PL}" y1="${PT}" x2="${PL}" y2="${xAxisY}" stroke="rgba(180,140,90,0.5)" stroke-width="1" stroke-dasharray="3 2"/>
    <circle class="elev-cursor-dot" cx="${PL}" cy="${PT + iH / 2}" r="3.5" fill="#b45309" stroke="rgba(250,246,240,0.9)" stroke-width="1.5"/>
  </g>`;

    // Catenary overlay (populated dynamically on hover)
    const tY = (xAxisY + 20).toFixed(0);
    const catOverlay =
        `<polyline id="cat-locus" points="" fill="none" stroke="#ff6b35" stroke-width="1.5" stroke-dasharray="3 2" opacity="0.45"/>` +
        `<polyline id="cat-line" points="" fill="none" stroke="#ff6b35" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>` +
        `<circle id="cat-walker" cx="-100" cy="-100" r="4" fill="#ff6b35" stroke="rgba(250,246,240,0.9)" stroke-width="1.5"/>` +
        `<text id="cat-tension" x="${PL}" y="${tY}" font-size="9" fill="#a8a29e" font-family="system-ui,sans-serif"></text>`;

    // Transparent mouse-capture rect
    const overlay = `<rect class="elev-overlay" x="${PL}" y="${PT}" width="${iW}" height="${iH}" fill="transparent" style="cursor:crosshair"/>`;

    return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg"
    data-elev="1" data-pl="${PL}" data-pr="${PR}" data-pt="${PT}" data-pb="${PB}"
    data-iw="${iW}" data-ih="${iH}" data-mn="${mn}" data-mx="${mx}" data-n="${n}" data-dist="${distance_m}"
    style="display:block;width:100%;border-radius:4px;overflow:hidden">
    ${strips}${grid}
    <path d="${fp}" fill="rgba(13,148,136,0.07)"/>
    <path d="${sp}" fill="none" stroke="#0d9488" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    ${slackLine}
    ${catOverlay}
    <line x1="${PL}" y1="${xAxisY}" x2="${W - PR}" y2="${xAxisY}" stroke="rgba(120,90,50,0.4)" stroke-width="1"/>
    ${xTickMarks}${xLabels}
    ${cursor}${overlay}
  </svg>`;
}
