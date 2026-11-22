import { detailState, selectionState } from './state.js';
import { map } from './map.js';
import { cmapColor } from './colormaps.js';

export function _anchorLabelIcon(letter, color) {
    return L.divIcon({
        className: '',
        iconAnchor: [10, 27],
        iconSize: [20, 31],
        html: `<div style="pointer-events:none;display:flex;flex-direction:column;align-items:center;width:20px">
  <div style="
    width:20px;height:20px;border-radius:50%;
    background:rgba(250,246,240,0.97);
    border:2px solid ${color};
    box-shadow:0 2px 8px rgba(0,0,0,0.18);
    display:flex;align-items:center;justify-content:center;
    font-family:system-ui,sans-serif;font-size:12px;font-weight:700;
    color:${color};line-height:1;
  ">${letter}</div>
  <div style="height:3px;"></div>
  <div style="
    width:8px;height:8px;border-radius:50%;
    background:#fff;
    border:2px solid ${color};
    box-shadow:0 1px 3px rgba(0,0,0,0.2);
  "></div>
</div>`,
    });
}

export function _dimOthers(selectedLyr) {
    // Dim the entire linesPane with one CSS opacity change — no per-path iteration.
    map.getPane('linesPane').style.opacity = '0.5';

    // Lift the selected line into selectedPane at full opacity.
    const f = selectedLyr.feature;
    const t = selectionState.lastRealMax > selectionState.lastRealMin
        ? (f.properties.distance_m - selectionState.lastRealMin) / (selectionState.lastRealMax - selectionState.lastRealMin)
        : 0.5;
    selectionState.highlightOutline = L.geoJSON(f, {
        interactive: false,
        style: {
            color: 'white', weight: 9, opacity: 1.0,
            dashArray: f.properties.over_water ? '9 7' : null, pane: 'selectedPane'
        },
    }).addTo(map);
    selectionState.highlightColor = L.geoJSON(f, {
        interactive: false,
        style: {
            color: cmapColor(t), weight: 5.5, opacity: 1.0,
            dashArray: f.properties.over_water ? '9 7' : null, pane: 'selectedPane'
        },
    }).addTo(map);

    // A / B labels at the anchor endpoints, coloured as the line
    const lineColor = f.properties.los_blocked ? '#ef4444' : cmapColor(t);
    const coords = f.geometry && f.geometry.coordinates;
    if (coords && coords.length >= 2) {
        const [[lonA, latA], [lonB, latB]] = coords;
        selectionState.anchorLabelA = L.marker([latA, lonA], {
            icon: _anchorLabelIcon('A', lineColor), pane: 'markerPane', interactive: false,
        }).addTo(map);
        selectionState.anchorLabelB = L.marker([latB, lonB], {
            icon: _anchorLabelIcon('B', lineColor), pane: 'markerPane', interactive: false,
        }).addTo(map);
    }
}

export function _restoreAll() {
    map.getPane('linesPane').style.opacity = '1.0';
    if (selectionState.highlightOutline) {
        map.removeLayer(selectionState.highlightOutline);
        selectionState.highlightOutline = null;
    }
    if (selectionState.highlightColor) {
        map.removeLayer(selectionState.highlightColor);
        selectionState.highlightColor = null;
    }
    if (selectionState.anchorLabelA) {
        map.removeLayer(selectionState.anchorLabelA);
        selectionState.anchorLabelA = null;
    }
    if (selectionState.anchorLabelB) {
        map.removeLayer(selectionState.anchorLabelB);
        selectionState.anchorLabelB = null;
    }
    selectionState.selectedLayer = null;
}

export function focusLine() {
    if (!detailState.currentDetailCoords) return;
    const [[lonA, latA], [lonB, latB]] = detailState.currentDetailCoords;
    map.flyToBounds([[latA, lonA], [latB, lonB]], {padding: [60, 60], maxZoom: 18});
}

export function copyCoords(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
        btn.textContent = '✓';
        setTimeout(() => {
            btn.textContent = 'Copy';
        }, 1400);
    }).catch(() => {
    });
}
