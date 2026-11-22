import { detailState, selectionState } from './state.js';
import { map } from './map.js';
import { elevSvg, elevProfileSvgCompact, _smoothElevs } from './elev-profile.js';
import { _updateCatenaryAt } from './catenary-ui.js';
import {
    _PLACE_CATS, featureColor, featureLabel, _escXml, _formatTag,
    featuresAtT, kindLabel, computeBearing, cardinal, compassSvg,
    anchorIcon, buildCorridorFeatureList
} from './corridor.js';
import { _dimOthers, _restoreAll } from './selection.js';

export function buildTooltip(p) {
    const rise = (p.elev_b != null && p.elev_a != null) ? (p.elev_b - p.elev_a) : 0;
    const dir = p.slope_deg != null ? (rise > 0.5 ? '↗' : rise < -0.5 ? '↘' : '→') : '';
    const sdeg = p.slope_deg != null ? ` <span style="font-size:10px;font-weight:300;color:#78716c">${dir}${p.slope_deg}°</span>` : '';
    const svg = elevProfileSvgCompact(p.terrain_elevs, p.elev_a, p.elev_b);
    return `<div class="tip-dist">${p.distance_m} m${sdeg}</div>${svg ? svg : ''}`;
}

export function buildSelectedTooltip(p, t) {
    const distFromA = Math.round(p.distance_m * t);
    const total = Math.round(p.distance_m);
    const distPart = `${distFromA}m <span style="font-weight:300">/ ${total}m</span>`;
    const at = featuresAtT(p.corridor_features, t);
    const terrainPart = at.length
        ? `<span style="font-weight:300;color:#78716c">&ensp;${_escXml(featureLabel(at[0]))}</span>`
        : '';
    return `<div class="tip-dist">${distPart}${terrainPart}</div>`;
}

function _svgDims(svg) {
    return {
        padLeft: +svg.dataset.pl, innerW: +svg.dataset.iw,
        padTop: +svg.dataset.pt, innerH: +svg.dataset.ih,
        minElev: +svg.dataset.mn, maxElev: +svg.dataset.mx,
    };
}

function _updateElevMarker(lat, lon) {
    if (!detailState.elevMarker) {
        detailState.elevMarker = L.circleMarker([lat, lon], {
            radius: 7, color: '#f97316', fillColor: '#f97316',
            fillOpacity: 0.85, weight: 2.5, pane: 'markerPane',
        }).addTo(map);
    } else {
        detailState.elevMarker.setLatLng([lat, lon]);
    }
}

function _removeElevMarker() {
    if (detailState.elevMarker) {
        map.removeLayer(detailState.elevMarker);
        detailState.elevMarker = null;
    }
}

function _updateElevCursorAt(t, tooltipClientX, tooltipClientY) {
    if (!detailState.svgCursorState || !detailState.currentPair) return;
    detailState.lastCatT = t;
    _updateCatenaryAt(t);
    const {cursorG, cLine, cDot, padLeft, innerW, padTop, innerH, minElev, maxElev, n, smoothed} = detailState.svgCursorState;
    const elevRange = maxElev - minElev;
    const {p} = detailState.currentPair;
    const cx = (padLeft + t * innerW).toFixed(2);
    cursorG.removeAttribute('display');
    cLine.setAttribute('x1', cx);
    cLine.setAttribute('x2', cx);

    const fi = t * (n - 1);
    const lo = Math.floor(fi), hi = Math.min(lo + 1, n - 1);
    const frac = fi - lo;
    const eL = (smoothed || [])[lo], eH = (smoothed || [])[hi];
    const elev = (eL != null && eH != null) ? eL + frac * (eH - eL) : (eL ?? eH);

    if (elev != null && elevRange > 0) {
        const cy = (padTop + innerH - ((elev - minElev) / elevRange) * innerH).toFixed(2);
        cDot.setAttribute('cx', cx);
        cDot.setAttribute('cy', cy);
        cDot.removeAttribute('display');
    } else {
        cDot.setAttribute('display', 'none');
    }

    const tip = document.getElementById('elev-tip');
    tip.innerHTML = buildSelectedTooltip(p, t);

    if (tooltipClientX != null) {
        const tipW = tip.offsetWidth || 170;
        tip.style.display = 'block';
        tip.style.left = (tooltipClientX + tipW > window.innerWidth ? tooltipClientX - tipW - 8 : tooltipClientX + 10) + 'px';
        tip.style.top = (tooltipClientY - 40) + 'px';
    } else {
        tip.style.display = 'none';
    }
}

function _hideCursor() {
    if (detailState.svgCursorState) detailState.svgCursorState.cursorG.setAttribute('display', 'none');
    document.getElementById('elev-tip').style.display = 'none';
    _removeElevMarker();
    if (selectionState.selectedLayer && selectionState.selectedLayer.getTooltip() && detailState.currentPair) {
        selectionState.selectedLayer.setTooltipContent(buildTooltip(detailState.currentPair.p));
    }
}

const _MAP_HOVER_PX = 12; // pixel snap distance

function _onMapHover(e) {
    if (!detailState.svgCursorState || !detailState.currentPair) return;
    const {coords} = detailState.currentPair;
    if (!coords || coords.length < 2) return;
    const [[lonA, latA], [lonB, latB]] = coords;

    // Work in pixel space for threshold check and t computation
    const pA = map.latLngToContainerPoint([latA, lonA]);
    const pB = map.latLngToContainerPoint([latB, lonB]);
    const pM = e.containerPoint;
    const dx = pB.x - pA.x, dy = pB.y - pA.y;
    const len2 = dx * dx + dy * dy;
    const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((pM.x - pA.x) * dx + (pM.y - pA.y) * dy) / len2));
    const nearX = pA.x + t * dx, nearY = pA.y + t * dy;
    const dist = Math.hypot(pM.x - nearX, pM.y - nearY);

    if (dist > _MAP_HOVER_PX) {
        _hideCursor();
        return;
    }

    _updateElevCursorAt(t, e.originalEvent.clientX, e.originalEvent.clientY);
    _updateElevMarker(latA + t * (latB - latA), lonA + t * (lonB - lonA));
    if (selectionState.selectedLayer && selectionState.selectedLayer.getTooltip()) {
        selectionState.selectedLayer.setTooltipContent(buildSelectedTooltip(detailState.currentPair.p, t));
    }
}

export function _attachElevHover() {
    detailState.svgCursorState = null;
    if (!detailState.currentPair) return;
    const {coords} = detailState.currentPair;
    const svg = document.querySelector('#detail-content svg[data-elev]');
    if (!svg) return;

    const {padLeft, innerW, padTop, innerH, minElev, maxElev} = _svgDims(svg);
    const n = +svg.dataset.n;

    const cursorG = svg.querySelector('.elev-cursor');
    const cLine = svg.querySelector('.elev-cursor-line');
    const cDot = svg.querySelector('.elev-cursor-dot');
    const overlay = svg.querySelector('.elev-overlay');
    if (!overlay) return;

    const smoothed = _smoothElevs(detailState.currentPair.p.terrain_elevs || []);
    detailState.svgCursorState = {cursorG, cLine, cDot, padLeft, innerW, padTop, innerH, minElev, maxElev, n, smoothed};

    overlay.addEventListener('mousemove', e => {
        const svgPt = svg.createSVGPoint();
        svgPt.x = e.clientX;
        svgPt.y = e.clientY;
        const pt = svgPt.matrixTransform(svg.getScreenCTM().inverse());
        const t = Math.max(0, Math.min(1, (pt.x - padLeft) / innerW));
        _updateElevCursorAt(t, e.clientX, e.clientY);
        if (coords && coords.length >= 2) {
            const [[lonA, latA], [lonB, latB]] = coords;
            _updateElevMarker(latA + t * (latB - latA), lonA + t * (lonB - lonA));
        }
    });

    overlay.addEventListener('mouseleave', _hideCursor);
}

function _showDetailWrap() {
    document.getElementById('panel-detail-wrap').style.display = 'block';
    document.getElementById('detail-reopen-tab').style.display = 'none';
}

function _hideDetailWrap(keepReopen = false) {
    document.getElementById('panel-detail-wrap').style.display = 'none';
    document.getElementById('detail-reopen-tab').style.display = keepReopen ? 'flex' : 'none';
}

export function buildDetailHtml(p, coords) {
    // ── Coords-dependent data ────────────────────────────────────────────────
    let deg = null, copyA = '', copyB = '';
    if (coords && coords.length >= 2) {
        const [[lonA, latA], [lonB, latB]] = coords;
        deg = computeBearing(lonA, latA, lonB, latB);
        copyA = `<button class="copy-btn" onclick="copyCoords('${latA.toFixed(6)},${lonA.toFixed(6)}',this)">Copy</button>`;
        copyB = `<button class="copy-btn" onclick="copyCoords('${latB.toFixed(6)},${lonB.toFixed(6)}',this)">Copy</button>`;
    }

    // ── Slope direction ──────────────────────────────────────────────────────
    const rise = (p.elev_b != null && p.elev_a != null) ? (p.elev_b - p.elev_a) : 0;
    const sDir = p.slope_deg != null ? (rise > 0.5 ? '↗' : rise < -0.5 ? '↘' : '→') : '';
    const slopeTxt = p.slope_deg != null ? ` <span class="slope-suf">${sDir}${p.slope_deg}°</span>` : '';

    // ── Anchor summary line ──────────────────────────────────────────────────
    const iconA = anchorIcon(p.anchor_a_kind, p.anchor_a_tags);
    const iconB = anchorIcon(p.anchor_b_kind, p.anchor_b_tags);
    const elevA = p.elev_a != null ? `<span class="anc-elev">${Math.round(p.elev_a)} m</span>` : '';
    const elevB = p.elev_b != null ? `<span class="anc-elev">${Math.round(p.elev_b)} m</span>` : '';
    const compassBtn = deg != null
        ? `<button class="compass-btn" onclick="focusLine()" title="Focus · ${Math.round(deg)}° ${cardinal(deg)}">${compassSvg(deg)}</button>`
        : '';
    const waterBadge = p.over_water ? `<div style="margin-bottom:5px">${'<span class="water-tag">Water crossing</span>'}</div>` : '';
    const blockedBadge = p._custom_blocked_label
        ? `<div style="margin-bottom:5px"><span style="display:inline-block;background:#fee2e2;color:#991b1b;border:1px solid #fecaca;border-radius:3px;padding:1px 7px;font-size:11px;font-weight:500">⚠ ${_escXml(p._custom_blocked_label)}</span></div>`
        : (p.custom_pair && !p.los_blocked ? `<div style="margin-bottom:5px"><span style="display:inline-block;background:#d1fae5;color:#065f46;border:1px solid #a7f3d0;border-radius:3px;padding:1px 7px;font-size:11px;font-weight:500">✓ Clear</span></div>` : '');

    const summaryLine = `<div class="anchor-summary-line">
      <i class="anc-icon material-symbols-rounded">${iconA}</i>
      ${elevA}
      <div class="line-spine"><span class="metrics-pill">${p.distance_m} m${slopeTxt}</span></div>
      ${elevB}
      <i class="anc-icon material-symbols-rounded">${iconB}</i>
      ${compassBtn}
    </div>`;

    // ── Anchor tag rows (always open) ────────────────────────────────────────
    const _tagRow = (label, id, kind, tags, copyBtn) => {
        tags = tags || {};
        const parts = [];
        const addT = (key, ...names) => {
            for (const n of names) if (tags[n]) {
                parts.push(`<span class="tag-key">${key}</span> ${_escXml(tags[n])}`);
                return;
            }
        };
        addT('Species', 'species', 'species:en');
        addT('Name', 'name');
        addT('Circ', 'circumference');
        addT('Height', 'height');
        const kindStr = kindLabel(kind);
        const tagsHtml = parts.length
            ? `<span class="at-tags"><b>${kindStr}</b><span style="color:rgba(120,90,50,0.4)"> · </span>${parts.join('<span style="color:rgba(120,90,50,0.4)"> · </span>')}</span>`
            : `<span class="at-tags"><b>${kindStr || '—'}</b></span>`;
        const osmLink = id > 0
            ? `<a href="https://www.openstreetmap.org/node/${id}" target="_blank">OSM↗</a>`
            : '';
        return `<div class="anchor-tag-row">
        <span class="at-label">${label}</span>
        ${tagsHtml}
        <div class="at-actions">${copyBtn || ''}${osmLink}</div>
      </div>`;
    };

    const anchorTagsSection = `<div class="anchor-tags">
      ${_tagRow('A', p.anchor_a_id, p.anchor_a_kind, p.anchor_a_tags, copyA)}
      ${_tagRow('B', p.anchor_b_id, p.anchor_b_kind, p.anchor_b_tags, copyB)}
    </div>`;

    // ── Corridor features (unchanged) ────────────────────────────────────────
    const feats = p.corridor_features || [];
    const corridorFeats = feats.filter(f => !_PLACE_CATS.has(f.category));
    const placeFeats = feats.filter(f => _PLACE_CATS.has(f.category));
    const _collapseCard = (title, list, open) => {
        if (!list.length) return '';
        const openAttr = open ? ' open' : '';
        return `<details${openAttr} style="margin-top:8px;border-top:1px solid rgba(180,140,90,0.15);padding-top:7px">` +
            `<summary style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.07em;color:#a8a29e;margin-bottom:4px;cursor:pointer;list-style:none;-webkit-appearance:none">` +
            `<i class="sl-chev">›</i>` + title + `</summary>` +
            buildCorridorFeatureList(list) + `</details>`;
    };
    const corridorSection = _collapseCard('In corridor', corridorFeats, true) +
        _collapseCard('Places', placeFeats, false);

    return `<div class="sl-detail">
    ${blockedBadge}${waterBadge}${summaryLine}
    ${anchorTagsSection}
    ${elevSvg(p.terrain_elevs, p.elev_a, p.elev_b, p.corridor_features, p.distance_m)}
    ${corridorSection}
  </div>`;
}

export function showDetail(p, coords, lyr) {
    // If reopening the same pair after collapse, just restore the panel
    if (detailState.collapsedPair && detailState.collapsedPair.p === p) {
        detailState.collapsedPair = null;
        selectionState.selectedLayer = lyr;
        if (lyr) _dimOthers(lyr);
        _showDetailWrap();
        map.on('mousemove', _onMapHover);
        map.on('mouseout', _hideCursor);
        return;
    }
    detailState.collapsedPair = null;
    if (selectionState.selectedLayer) _restoreAll();
    map.off('mousemove', _onMapHover);
    map.off('mouseout', _hideCursor);
    selectionState.selectedLayer = lyr;
    detailState.currentDetailCoords = coords || null;
    detailState.currentPair = {p, coords: coords || null};
    detailState.currentPairIdx = detailState.currentFeatures.findIndex(f => f.properties === p);
    if (lyr) _dimOthers(lyr);
    detailState.catState = null;
    detailState.lastCatT = 0.5;
    document.getElementById('detail-content').innerHTML = buildDetailHtml(p, coords);
    _showDetailWrap();
    _attachElevHover();
    _updateCatenaryAt(0.5);
    map.on('mousemove', _onMapHover);
    map.on('mouseout', _hideCursor);
}

// Collapse (< tab): deselects line but saves state; shows > reopen tab
export function collapseDetail() {
    map.off('mousemove', _onMapHover);
    map.off('mouseout', _hideCursor);
    detailState.collapsedPair = detailState.currentPair;
    _restoreAll();
    if (detailState.elevMarker) {
        map.removeLayer(detailState.elevMarker);
        detailState.elevMarker = null;
    }
    document.getElementById('elev-tip').style.display = 'none';
    _hideDetailWrap(true);
}

// Reopen (> tab): restores the collapsed panel
export function reopenDetail() {
    if (!detailState.collapsedPair) return;
    const {p, coords} = detailState.collapsedPair;
    detailState.collapsedPair = null;
    document.getElementById('detail-content').innerHTML = buildDetailHtml(p, coords);
    _showDetailWrap();
    _attachElevHover();
    _updateCatenaryAt(0.5);
    map.on('mousemove', _onMapHover);
    map.on('mouseout', _hideCursor);
}

export function closeDetail() {
    map.off('mousemove', _onMapHover);
    map.off('mouseout', _hideCursor);
    _restoreAll();
    detailState.collapsedPair = null;
    detailState.currentDetailCoords = null;
    detailState.currentPair = null;
    detailState.currentPairIdx = -1;
    detailState.svgCursorState = null;
    detailState.catState = null;
    if (detailState.elevMarker) {
        map.removeLayer(detailState.elevMarker);
        detailState.elevMarker = null;
    }
    document.getElementById('elev-tip').style.display = 'none';
    _hideDetailWrap(false);
    document.getElementById('detail-content').innerHTML = '';
}

export function hideLegend() {
    document.getElementById('legend-wrap').style.display = 'none';
}
