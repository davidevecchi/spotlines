import { mapState, overlayState, searchState } from './state.js';

const TRENTO = [46.0667, 11.1167];
export const map = L.map('map', {center: TRENTO, zoom: 14, zoomControl: false});

// Zoom is handled by custom #zoom-combo buttons in the right column

map.createPane('labelsPane');
map.getPane('labelsPane').style.zIndex = 450;
map.getPane('labelsPane').style.pointerEvents = 'none';

map.createPane('blankPane');
map.getPane('blankPane').style.zIndex = 399;
map.getPane('blankPane').style.pointerEvents = 'none';

map.createPane('terrainPane');
map.getPane('terrainPane').style.zIndex = 400;
map.getPane('terrainPane').style.pointerEvents = 'none';

map.createPane('landusePane');
map.getPane('landusePane').style.zIndex = 401;
map.getPane('landusePane').style.pointerEvents = 'none';

map.createPane('contourPane');
map.getPane('contourPane').style.zIndex = 402;
map.getPane('contourPane').style.pointerEvents = 'none';

map.createPane('linesPane');
map.getPane('linesPane').style.zIndex = 404;

map.createPane('selectedPane');
map.getPane('selectedPane').style.zIndex = 405;

map.createPane('markerPane');
map.getPane('markerPane').style.zIndex = 500;
map.getPane('markerPane').style.pointerEvents = 'none';

// Base layers
export const LAYERS = {
    osm: {
        label: 'OSM',
        color: '#b5c99a',
        layer: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a>', maxZoom: 19,
        }),
    },
    sat: {
        label: 'Satellite',
        color: '#1a2535',
        layer: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            {attribution: 'Tiles &copy; Esri', maxZoom: 19}),
    },
    topo: {
        label: 'Topo',
        color: '#c4b49a',
        layer: L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://opentopomap.org">OpenTopoMap</a>', maxZoom: 17,
        }),
    },
};

export const labelsLayer = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png',
    {attribution: '&copy; CARTO', maxZoom: 19, pane: 'labelsPane'}
);

LAYERS.osm.layer.addTo(map);

export const landuseLayer = L.tileLayer.wms('https://maps.heigit.org/osmlanduse/wms', {
    layers: 'osmlanduse:osm_lulc_combined_osm4eo,osmlanduse:osm_lulc',
    styles: '',
    format: 'image/png',
    transparent: true,
    version: '1.1.1',
    opacity: 0.65,
    pane: 'landusePane',
    attribution: 'Land use &copy; <a href="https://osmlanduse.org" target="_blank">osmlanduse.org</a> / HeiGIT',
});

// Build layer thumbnail grid
const layerGrid = document.getElementById('layer-grid');
Object.entries(LAYERS).forEach(([key, cfg]) => {
    const el = document.createElement('div');
    el.className = 'layer-thumb' + (key === 'osm' ? ' active' : '');
    el.dataset.key = key;
    el.innerHTML = `<div class="thumb-img" style="background:${cfg.color}"></div><span>${cfg.label}</span><div class="thumb-check">✓</div>`;
    el.addEventListener('click', () => toggleBase(key));
    layerGrid.appendChild(el);
});
const blankThumb = document.createElement('div');
blankThumb.className = 'layer-thumb';
blankThumb.id = 'base-blank-thumb';
blankThumb.dataset.key = 'blank';
blankThumb.innerHTML = `<div class="thumb-img" style="background:repeating-conic-gradient(#ddd8d0 0% 25%,#f5f1ec 0% 50%) 0 0/8px 8px"></div><span>None</span><div class="thumb-check">✓</div>`;
blankThumb.addEventListener('click', () => toggleBase('blank'));
layerGrid.appendChild(blankThumb);

function _addBlankRect() {
    _removeBlankRect();
    if (!searchState.currentBbox) return;
    const bbox = searchState.currentBbox;
    mapState.blankRect = L.rectangle(
        [[bbox.south, bbox.west], [bbox.north, bbox.east]],
        {stroke: false, fillColor: '#ffffff', fillOpacity: 1.0, pane: 'blankPane', interactive: false}
    ).addTo(map);
}

function _removeBlankRect() {
    if (mapState.blankRect) {
        map.removeLayer(mapState.blankRect);
        mapState.blankRect = null;
    }
}

export function addBlankRect() {
    _addBlankRect();
}

export function removeBlankRect() {
    _removeBlankRect();
}

export function setHeatmapOpacity(full) {
    if (overlayState.terrainLayer) {
        overlayState.terrainLayer.setOpacity(overlayState.OV.elev && !full ? 0.6 : 1.0);
    }
}

export function toggleBase(key) {
    if (key === 'blank') {
        mapState.blankOn = !mapState.blankOn;
        document.getElementById('base-blank-thumb').classList.toggle('active', mapState.blankOn);
        if (mapState.blankOn) _addBlankRect(); else _removeBlankRect();
        setHeatmapOpacity(mapState.blankOn);
        return;
    }
    const thumbs = document.querySelectorAll('.layer-thumb[data-key]:not([data-key="blank"])');
    if (key === mapState.activeBaseKey) {
        map.removeLayer(LAYERS[key].layer);
        mapState.activeBaseKey = null;
        thumbs.forEach(el => el.classList.remove('active'));
        return;
    }
    if (mapState.activeBaseKey) map.removeLayer(LAYERS[mapState.activeBaseKey].layer);
    LAYERS[key].layer.addTo(map);
    mapState.activeBaseKey = key;
    thumbs.forEach(el => el.classList.toggle('active', el.dataset.key === key));
}
