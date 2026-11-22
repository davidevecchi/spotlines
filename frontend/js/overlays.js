import { overlayState, mapState } from './state.js';
import { map, labelsLayer, landuseLayer } from './map.js';
import { showSlopeLegend, showElevLegend } from './legend.js';

// drawnItems is created in draw.js but we need a reference here.
// We use a lazy getter pattern via a callback registered from draw.js.
let _getDrawnItems = () => null;
export function registerDrawnItems(getter) {
    _getDrawnItems = getter;
}

export function _setOverlayActive(key, on) {
    overlayState.OV[key] = on;
    document.getElementById(`ov-${key}-thumb`).classList.toggle('active', on);
}

export function toggleOverlay(key) {
    const on = !overlayState.OV[key];
    overlayState.OV[key] = on;
    document.getElementById(`ov-${key}-thumb`).classList.toggle('active', on);
    if (key === 'slope') {
        const di = _getDrawnItems();
        if (di) di.setStyle({fillOpacity: on ? 0 : 0.2});
        showSlopeLegend(on);
        _rebuildTerrainLayer();
    } else if (key === 'elev') {
        showElevLegend(on);
        _rebuildTerrainLayer();
    } else if (key === 'hillshade') {
        _rebuildTerrainLayer();
    } else if (key === 'contour') {
        if (!overlayState.contourLayer) return;
        if (on) overlayState.contourLayer.addTo(map); else map.removeLayer(overlayState.contourLayer);
    } else if (key === 'names') {
        if (on) labelsLayer.addTo(map); else map.removeLayer(labelsLayer);
    } else if (key === 'landuse') {
        if (on) landuseLayer.addTo(map); else map.removeLayer(landuseLayer);
    }
}

export function _rebuildTerrainLayer() {
    if (overlayState.terrainLayer) {
        map.removeLayer(overlayState.terrainLayer);
        overlayState.terrainLayer = null;
    }
    if (!overlayState.terrainBbox) return;
    const {south, west, north, east} = overlayState.terrainBbox;
    const anyOn = overlayState.OV.slope || overlayState.OV.elev || overlayState.OV.hillshade;
    if (!anyOn) return;
    const qs = new URLSearchParams({
        south, west, north, east,
        slope: overlayState.OV.slope ? 1 : 0,
        elev: overlayState.OV.elev ? 1 : 0,
        hillshade: overlayState.OV.hillshade ? 1 : 0,
    });
    const opacity = overlayState.OV.elev && !mapState.blankOn ? 0.6 : 1.0;
    overlayState.terrainLayer = L.imageOverlay(`/terrain/image?${qs}`, [[south, west], [north, east]], {
        opacity, pane: 'terrainPane',
        attribution: 'Terrain &copy; Copernicus GLO-30 / TINItaly INGV',
    });
    overlayState.terrainLayer.addTo(map);
}

export async function updateTerrainLayer(bbox) {
    overlayState.terrainBbox = bbox;
    _rebuildTerrainLayer();
    const qs = new URLSearchParams({south: bbox.south, west: bbox.west, north: bbox.north, east: bbox.east});
    try {
        const res = await fetch('/slope/stats?' + qs);
        if (res.ok) {
            const {min_deg, max_deg} = await res.json();
            overlayState.slopeStats = {min: min_deg, max: max_deg};
            showSlopeLegend(overlayState.OV.slope);
        }
    } catch {
    }
    try {
        const res = await fetch('/elevation/stats?' + qs);
        if (res.ok) {
            const {min_m, max_m} = await res.json();
            overlayState.elevStats = {min: min_m, max: max_m};
            showElevLegend(overlayState.OV.elev);
        }
    } catch {
    }
}

export async function updateContourLayer(bbox) {
    const wasOn = (overlayState.contourLayer && map.hasLayer(overlayState.contourLayer)) || overlayState.OV.contour;
    if (overlayState.contourLayer) {
        map.removeLayer(overlayState.contourLayer);
        overlayState.contourLayer = null;
    }
    overlayState.contourSvgEl = null;
    try {
        const qs = new URLSearchParams({south: bbox.south, west: bbox.west, north: bbox.north, east: bbox.east});
        const res = await fetch('/slope/contours?' + qs);
        if (!res.ok) return;
        const svgText = await res.text();
        overlayState.contourSvgEl = new DOMParser().parseFromString(svgText, 'image/svg+xml').documentElement;
        overlayState.contourLayer = L.svgOverlay(overlayState.contourSvgEl, [[bbox.south, bbox.west], [bbox.north, bbox.east]], {
            interactive: false,
            pane: 'contourPane'
        });
        if (wasOn || overlayState.OV.contour) {
            overlayState.contourLayer.addTo(map);
            _setOverlayActive('contour', true);
        }
    } catch {
    }
}

export function updateLanduseLayer(_bbox) {
    // WMS tile layer covers the whole map — no per-bbox refresh needed.
    // Restore visibility if it was on before the search.
    if (overlayState.OV.landuse && !map.hasLayer(landuseLayer)) {
        landuseLayer.addTo(map);
    }
}
