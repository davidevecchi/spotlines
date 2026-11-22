import { searchState, drawLineState, mapState } from './state.js';
import { map, addBlankRect } from './map.js';
import { setStatus } from './status.js';
import { registerDrawnItems, updateTerrainLayer, updateContourLayer } from './overlays.js';

export const sliderEl = document.getElementById('length-slider');
noUiSlider.create(sliderEl, {
    start: [15, 50],
    connect: true,
    range: {min: [1], '20%': [10, 1], '45%': [50, 5], '70%': [200, 10], '85%': [500, 25], max: [4000]},
    tooltips: false,
});
sliderEl.noUiSlider.on('update', (vals) => {
    document.getElementById('slider-val').textContent = `${Math.round(vals[0])} – ${Math.round(vals[1])} m`;
});

// ── Draw rectangle ───────────────────────────────────────────────────────────
export const drawnItems = new L.FeatureGroup().addTo(map);

// Register drawnItems with overlays module
registerDrawnItems(() => drawnItems);

// Include Draw control for event system only (toolbar hidden via CSS)
map.addControl(new L.Control.Draw({
    draw: {
        rectangle: {shapeOptions: {color: '#b45309', weight: 2}},
        polyline: false,
        polygon: false,
        circle: false,
        circlemarker: false,
        marker: false
    },
    edit: false,
}));

export function toggleSelectArea() {
    const btn = document.getElementById('selectAreaBtn');
    if (searchState.drawHandler) {
        searchState.drawHandler.disable();
        searchState.drawHandler = null;
        btn.classList.remove('active');
        return;
    }
    // Cancel draw-line mode if active (cancelFn registered by draw-line.js)
    if (drawLineState.active && drawLineState.cancelFn) {
        drawLineState.cancelFn();
    }
    searchState.drawHandler = new L.Draw.Rectangle(map, {shapeOptions: {color: '#b45309', weight: 2, fillOpacity: 0.08}});
    searchState.drawHandler.enable();
    btn.classList.add('active');
}

map.on(L.Draw.Event.CREATED, e => {
    drawnItems.clearLayers();
    drawnItems.addLayer(e.layer);
    const b = e.layer.getBounds();
    searchState.currentBbox = {south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast()};
    if (mapState.blankOn) addBlankRect();
    updateTerrainLayer(searchState.currentBbox);
    updateContourLayer(searchState.currentBbox);
    searchState.drawHandler = null;
    document.getElementById('selectAreaBtn').classList.remove('active');
    document.getElementById('searchBtn').disabled = false;
    setStatus('Area selected — click Search');
});
