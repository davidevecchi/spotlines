import { detailState } from './state.js';
import { _solveUnloaded, _solveLoaded } from './catenary.js';
import { GRAVITY, LINE_PRESETS } from './constants.js';

// Read current line params from toolbar inputs.
export function _getLineParams() {
    const preset = document.getElementById('linePreset')?.value || 'tubular25';
    const {EA, w} = preset === 'custom'
        ? {
            EA: (+(document.getElementById('lineEA')?.value) || LINE_PRESETS.tubular25.EA / 1000) * 1000,
            w: (+(document.getElementById('lineW')?.value) || LINE_PRESETS.tubular25.w * 1000 / GRAVITY) * GRAVITY / 1000
        }
        : (LINE_PRESETS[preset] || LINE_PRESETS.tubular25);
    const H0 = (+(document.getElementById('preTension')?.value) || 8) * 1000;
    const W = (+(document.getElementById('walkerKg')?.value) || 80) * GRAVITY;
    return {EA, w, H0, W};
}

function _svgDims(svg) {
    return {
        padLeft: +svg.dataset.pl, innerW: +svg.dataset.iw,
        padTop: +svg.dataset.pt, innerH: +svg.dataset.ih,
        minElev: +svg.dataset.mn, maxElev: +svg.dataset.mx,
    };
}

// Clear catenary overlay elements in the open SVG.
export function _clearCatenary(svg) {
    svg = svg || document.querySelector('#detail-content svg[data-elev]');
    if (!svg) return;
    svg.querySelector('#cat-locus')?.setAttribute('points', '');
    svg.querySelector('#cat-line')?.setAttribute('points', '');
    const cw = svg.querySelector('#cat-walker');
    if (cw) {
        cw.setAttribute('cx', '-100');
        cw.setAttribute('cy', '-100');
    }
    const ct = svg.querySelector('#cat-tension');
    if (ct) ct.textContent = '';
}

// Draw the walker-position locus: sample 25 walker positions across the span,
// plot where the walker's foot would be for each position.
export function _drawLocus(svg, d, h, elevA, toSvg, W) {
    const locus = svg.querySelector('#cat-locus');
    if (!locus || !detailState.catState) return;
    const {L0, w, EA, H0} = detailState.catState;
    const pts = [];
    for (let i = 1; i <= 24; i++) {      // 24 interior positions (skip endpoints)
        const xw = (i / 25) * d;
        const r = _solveLoaded(d, h, L0, w, EA, xw, W, H0);
        if (r) pts.push(toSvg(r.walkerPt.x, r.walkerPt.y));
    }
    locus.setAttribute('points', pts.map(p => p.join(',')).join(' '));
}

// Recompute and draw the loaded catenary with walker at horizontal fraction t.
export function _updateCatenaryAt(t) {
    if (!detailState.currentPair) return;
    const svg = document.querySelector('#detail-content svg[data-elev]');
    if (!svg) return;

    const p = detailState.currentPair.p;
    const d = p.distance_m;
    const h = (p.elev_b != null && p.elev_a != null) ? p.elev_b - p.elev_a : 0;
    const elevA = p.elev_a ?? 0;
    const xw = Math.max(0.02, Math.min(0.98, t)) * d;

    const {padLeft, innerW, padTop, innerH, minElev, maxElev} = _svgDims(svg);
    const elevRange = maxElev - minElev;
    if (elevRange <= 0) return;

    const {EA, w, H0, W} = _getLineParams();

    // Physical → SVG pixel coordinates (shared by locus and catenary)
    const toSvg = (xm, ym) => [
        (padLeft + (xm / d) * innerW).toFixed(2),
        (padTop + innerH - ((elevA - ym - minElev) / elevRange) * innerH).toFixed(2),
    ];

    // Recompute unloaded natural length when params or pair change; also redraw locus
    const paramsChanged = !detailState.catState || detailState.catState.d !== d || detailState.catState.h !== h ||
        detailState.catState.H0 !== H0 || detailState.catState.w !== w || detailState.catState.EA !== EA;
    if (paramsChanged) {
        const r = _solveUnloaded(d, h, H0, w, EA);
        if (!r) {
            _clearCatenary(svg);
            return;
        }
        detailState.catState = {d, h, H0, w, EA, L0: r.L0};
        _drawLocus(svg, d, h, elevA, toSvg, W);
    }

    const result = _solveLoaded(d, h, detailState.catState.L0, w, EA, xw, W, H0);
    const catLine = svg.querySelector('#cat-line');
    const catWalker = svg.querySelector('#cat-walker');
    const catTension = svg.querySelector('#cat-tension');
    if (!result) {
        catLine?.setAttribute('points', '');
        catWalker && (catWalker.setAttribute('cx', '-100'), catWalker.setAttribute('cy', '-100'));
        return;
    }

    const {pts, walkerPt, TA, TB} = result;

    catLine.setAttribute('points', pts.map(({x, y}) => toSvg(x, y).join(',')).join(' '));
    const [wx, wy] = toSvg(walkerPt.x, walkerPt.y);
    catWalker.setAttribute('cx', wx);
    catWalker.setAttribute('cy', wy);
    catTension.textContent = `T_A ${(TA / 1000).toFixed(2)} kN  ·  T_B ${(TB / 1000).toFixed(2)} kN`;
}
