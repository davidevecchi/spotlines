import { drawLineState, detailState, selectionState, searchState } from './state.js';
import { map } from './map.js';
import { cmapColor } from './colormaps.js';
import { setStatus, setLoading, setProgress } from './status.js';
import { showDetail, buildTooltip } from './detail.js';
import { drawLegend } from './legend.js';

// Shared SSE stream parsing helper
export async function parseSseStream(resp, onMsg) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
            const line = part.trim();
            if (!line.startsWith('data: ')) continue;
            let msg;
            try {
                msg = JSON.parse(line.slice(6));
            } catch {
                continue;
            }
            await onMsg(msg);
        }
    }
}

const _DRAW_MARKER_STYLE = {
    radius: 6, color: '#b45309', fillColor: '#fff',
    fillOpacity: 1, weight: 2.5, pane: 'markerPane',
};

export function _clearDrawLine() {
    if (drawLineState.markerA) {
        map.removeLayer(drawLineState.markerA);
        drawLineState.markerA = null;
    }
    if (drawLineState.markerB) {
        map.removeLayer(drawLineState.markerB);
        drawLineState.markerB = null;
    }
    if (drawLineState.prevLayer) {
        map.removeLayer(drawLineState.prevLayer);
        drawLineState.prevLayer = null;
    }
    drawLineState.pointA = null;
}

function _clearPendingLine() {
    if (drawLineState.pendingLine) {
        map.removeLayer(drawLineState.pendingLine);
        drawLineState.pendingLine = null;
    }
}

// Return true if latA/lonA → latB/lonB duplicates a line already on screen
// (checks regular search results + existing custom lines, ±5 m tolerance).
function _isDuplicateLine(latA, lonA, latB, lonB) {
    const close = (ax, ay, bx, by) => {
        const dlat = (ay - by) * 111_111;
        const dlon = (ax - bx) * 111_111 * Math.cos(ay * Math.PI / 180);
        return Math.sqrt(dlat * dlat + dlon * dlon) < 5;
    };
    const matches = (coords) => {
        const [[c0x, c0y], [c1x, c1y]] = coords;
        return (close(lonA, latA, c0x, c0y) && close(lonB, latB, c1x, c1y))
            || (close(lonA, latA, c1x, c1y) && close(lonB, latB, c0x, c0y));
    };
    for (const f of (detailState.currentFeatures || [])) {
        if (f.geometry && matches(f.geometry.coordinates)) return true;
    }
    for (const {feature: f} of drawLineState.customLineData) {
        if (f.geometry && matches(f.geometry.coordinates)) return true;
    }
    return false;
}

function _updateColorScale() {
    const dists = [];
    (detailState.currentFeatures || []).forEach(f => dists.push(f.properties.distance_m));
    drawLineState.customLineData.forEach(({feature: f}) => dists.push(f.properties.distance_m));
    if (!dists.length) return;

    const globalMin = Math.min(...dists);
    const globalMax = Math.max(...dists);
    selectionState.lastRealMin = globalMin;
    selectionState.lastRealMax = globalMax;

    const tOf = d => globalMax > globalMin ? (d - globalMin) / (globalMax - globalMin) : 0.5;

    // Re-style regular lines
    if (selectionState.coloredGeoJson) {
        selectionState.coloredGeoJson.setStyle(f => ({color: cmapColor(tOf(f.properties.distance_m))}));
    }

    // Re-style custom lines: blocked → red, clear → cmap
    drawLineState.customLineData.forEach(({group, feature: f}) => {
        const col = f.properties.los_blocked ? '#ef4444' : cmapColor(tOf(f.properties.distance_m));
        group.getLayers()[1].setStyle({color: col});
    });

    drawLegend(Math.round(globalMin), Math.round(globalMax));
}

export { _updateColorScale };

function _onDrawLinePreview(e) {
    if (!drawLineState.pointA) return;
    const a = drawLineState.pointA;
    if (drawLineState.prevLayer) map.removeLayer(drawLineState.prevLayer);
    drawLineState.prevLayer = L.polyline([[a.lat, a.lng], [e.latlng.lat, e.latlng.lng]], {
        color: '#b45309', weight: 2, opacity: 0.5, dashArray: '5 5', pane: 'linesPane',
    }).addTo(map);
    const d = map.distance(a, e.latlng);
    const rad = Math.PI / 180;
    const dLon = (e.latlng.lng - a.lng) * rad;
    const y = Math.sin(dLon) * Math.cos(e.latlng.lat * rad);
    const x = Math.cos(a.lat * rad) * Math.sin(e.latlng.lat * rad)
        - Math.sin(a.lat * rad) * Math.cos(e.latlng.lat * rad) * Math.cos(dLon);
    const brg = (Math.atan2(y, x) / rad + 360) % 360;
    const len = d >= 1000 ? (d / 1000).toFixed(2) + ' km' : d.toFixed(1) + ' m';
    drawLineState.prevLayer.bindTooltip(`${len} · ${brg.toFixed(0)}°`, {
        permanent: true, direction: 'center', className: 'sl-tip', opacity: 1,
    }).openTooltip();
}

function _showCustomPairResult(feature, latA, lonA, latB, lonB) {
    const p = feature.properties;
    const coords = [[lonA, latA], [lonB, latB]];
    const blocked = p.los_blocked;

    _clearPendingLine();

    // Dedup: skip if this line already exists on screen (within 5 m tolerance).
    if (_isDuplicateLine(latA, lonA, latB, lonB)) {
        const distStr = p.distance_m >= 1000
            ? (p.distance_m / 1000).toFixed(2) + ' km'
            : Math.round(p.distance_m) + ' m';
        setStatus(`Custom line: ${distStr} — already on map`);
        if (drawLineState.markerA) {
            map.removeLayer(drawLineState.markerA);
            drawLineState.markerA = null;
        }
        if (drawLineState.markerB) {
            map.removeLayer(drawLineState.markerB);
            drawLineState.markerB = null;
        }
        return;
    }

    // Compute cmap colour upfront from the extended global scale (including this new line).
    const allDists = [
        ...(detailState.currentFeatures || []).map(f => f.properties.distance_m),
        ...drawLineState.customLineData.map(({feature: f}) => f.properties.distance_m),
        p.distance_m,
    ];
    const newMin = Math.min(...allDists);
    const newMax = Math.max(...allDists);
    const tNew = newMax > newMin ? (p.distance_m - newMin) / (newMax - newMin) : 0.5;
    const lineColor = blocked ? '#ef4444' : cmapColor(tNew);

    // Custom lines always use long dashes to stay visually distinct from solid search results.
    const committedGroup = L.layerGroup([
        L.polyline([[latA, lonA], [latB, lonB]], {
            color: 'white', weight: 5.5, opacity: 1, dashArray: '14 7', pane: 'linesPane',
        }),
        L.polyline([[latA, lonA], [latB, lonB]], {
            color: lineColor, weight: 3, opacity: 0.9, dashArray: '14 7', pane: 'linesPane',
        }),
    ]).addTo(map);

    drawLineState.customLineData.push({group: committedGroup, feature});

    // Make the coloured polyline interactive; stamp feature so _dimOthers can read it.
    const fakeLyr = committedGroup.getLayers()[1];
    fakeLyr.bindTooltip(buildTooltip(p), {
        direction: 'top', sticky: true, className: 'sl-tip', opacity: 1,
    });
    fakeLyr.feature = feature;
    fakeLyr.on('click', () => showDetail(p, coords, fakeLyr));

    // Add blocked warning to properties for detail panel
    if (blocked || p.blocked_by_landuse) p._custom_blocked_label = 'Blocked';

    // Recompute global scale (widens range to include this line, recolours all layers,
    // draws legend) BEFORE showDetail so _dimOthers sees correct lastRealMin/Max.
    _updateColorScale();

    showDetail(p, coords, fakeLyr);

    // Remove the temporary markers now that committed line is drawn
    if (drawLineState.markerA) {
        map.removeLayer(drawLineState.markerA);
        drawLineState.markerA = null;
    }
    if (drawLineState.markerB) {
        map.removeLayer(drawLineState.markerB);
        drawLineState.markerB = null;
    }

    const distStr = p.distance_m >= 1000
        ? (p.distance_m / 1000).toFixed(2) + ' km'
        : Math.round(p.distance_m) + ' m';
    const stateLabel = blocked ? 'blocked' : 'clear';
    setStatus(`Custom line: ${distStr} — ${stateLabel}`);
}

async function _runCustomPairAnalysis(latA, lonA, latB, lonB) {
    const clearanceM = (parseFloat(document.getElementById('clearanceM').value) || 2.0) / 2;
    const qs = new URLSearchParams({
        lat_a: latA, lon_a: lonA, lat_b: latB, lon_b: lonB,
        clearance_m: clearanceM,
    });

    setLoading(true);
    setStatus('Analysing custom line…');

    try {
        const resp = await fetch('/spot/pair?' + qs);
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.detail || `HTTP ${resp.status}`);
        }

        await parseSseStream(resp, async (msg) => {
            if (msg.error) throw new Error(msg.error);
            if (msg.status) setStatus(msg.status);
            if (msg.pct != null) setProgress(msg.pct);
            if (msg.result) {
                _showCustomPairResult(msg.result, latA, lonA, latB, lonB);
            }
        });
    } catch (err) {
        setStatus('Error: ' + err.message);
        _clearDrawLine();
    } finally {
        setLoading(false);
    }
}

function _onDrawLineClick(e) {
    if (!drawLineState.pointA) {
        // First click — place marker A
        drawLineState.pointA = e.latlng;
        drawLineState.markerA = L.circleMarker([e.latlng.lat, e.latlng.lng], _DRAW_MARKER_STYLE).addTo(map);
        setStatus('Click second anchor point…');
        map.on('mousemove', _onDrawLinePreview);
    } else {
        // Second click — finalize
        const a = drawLineState.pointA;
        const b = e.latlng;

        map.off('click', _onDrawLineClick);
        map.off('mousemove', _onDrawLinePreview);
        drawLineState.active = false;
        document.body.classList.remove('draw-mode');
        document.getElementById('draw-line-btn').classList.remove('active');

        // Replace the live preview with a static pending line that stays until results arrive
        if (drawLineState.prevLayer) {
            map.removeLayer(drawLineState.prevLayer);
            drawLineState.prevLayer = null;
        }
        drawLineState.pendingLine = L.layerGroup([
            L.polyline([[a.lat, a.lng], [b.lat, b.lng]], {
                color: 'white', weight: 5.5, opacity: 1, pane: 'linesPane',
            }),
            L.polyline([[a.lat, a.lng], [b.lat, b.lng]], {
                color: '#b45309', weight: 3, opacity: 0.6,
                dashArray: '6 5', pane: 'linesPane',
            }),
        ]).addTo(map);
        drawLineState.markerB = L.circleMarker([b.lat, b.lng], _DRAW_MARKER_STYLE).addTo(map);

        _runCustomPairAnalysis(a.lat, a.lng, b.lat, b.lng);
    }
}

function _cancelDrawLine() {
    drawLineState.active = false;
    document.getElementById('draw-line-btn').classList.remove('active');
    document.body.classList.remove('draw-mode');
    map.off('click', _onDrawLineClick);
    map.off('mousemove', _onDrawLinePreview);
    _clearDrawLine();
    _clearPendingLine();
}

// Register cancel function so draw.js can call it without circular import
drawLineState.cancelFn = _cancelDrawLine;

export function toggleDrawLine() {
    const btn = document.getElementById('draw-line-btn');
    if (drawLineState.active) {
        _cancelDrawLine();
        setStatus('Draw cancelled');
        return;
    }
    // Cancel select-area mode if active
    if (searchState.drawHandler) {
        searchState.drawHandler.disable();
        searchState.drawHandler = null;
        document.getElementById('selectAreaBtn').classList.remove('active');
    }
    drawLineState.active = true;
    btn.classList.add('active');
    document.body.classList.add('draw-mode');
    _clearDrawLine();
    setStatus('Click first anchor point…');
    map.on('click', _onDrawLineClick);
}
