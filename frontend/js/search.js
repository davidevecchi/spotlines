import { searchState, detailState, selectionState } from './state.js';
import { map } from './map.js';
import { cmapColor } from './colormaps.js';
import { setStatus, setLoading, setProgress } from './status.js';
import { showDetail, closeDetail, buildDetailHtml, buildTooltip, _attachElevHover } from './detail.js';
import { _updateCatenaryAt } from './catenary-ui.js';
import { updateTerrainLayer, updateContourLayer, updateLanduseLayer } from './overlays.js';
import { sliderEl } from './draw.js';
import { parseSseStream, _updateColorScale } from './draw-line.js';

const _resultsLayer = L.layerGroup([], {pane: 'linesPane'}).addTo(map);

export function renderResults(geojson, minM, maxM) {
    _resultsLayer.clearLayers();
    selectionState.outlineGeoJson = null;
    selectionState.coloredGeoJson = null;
    selectionState.selectedLayer = null;
    detailState.currentDetailCoords = null;
    detailState.currentFeatures = geojson.features;
    const dists = geojson.features.map(f => f.properties.distance_m);
    const realMin = dists.length ? Math.min(...dists) : minM;
    const realMax = dists.length ? Math.max(...dists) : maxM;
    selectionState.lastRealMin = realMin;
    selectionState.lastRealMax = realMax;
    const outline = L.geoJSON(geojson, {
        style: f => ({
            color: 'white', weight: 5.5, opacity: 1,
            dashArray: f.properties.over_water ? '9 7' : null, pane: 'linesPane',
        }),
    });
    const layer = L.geoJSON(geojson, {
        style: f => {
            const t = realMax > realMin ? (f.properties.distance_m - realMin) / (realMax - realMin) : 0.5;
            return {
                color: cmapColor(t),
                weight: 3,
                opacity: 0.9,
                dashArray: f.properties.over_water ? '9 7' : null,
                pane: 'linesPane'
            };
        },
        onEachFeature: (f, lyr) => {
            lyr.bindTooltip(buildTooltip(f.properties), {
                direction: 'top', sticky: true, className: 'sl-tip', opacity: 1,
            });
            lyr.on('click', () => showDetail(f.properties, f.geometry.coordinates, lyr));
        },
    });
    selectionState.outlineGeoJson = outline;
    selectionState.coloredGeoJson = layer;
    _resultsLayer.addLayer(outline);
    _resultsLayer.addLayer(layer);
    return {realMin, realMax};
}

export async function doSearch() {
    if (!searchState.currentBbox) {
        setStatus('Draw an area first');
        return;
    }

    const vals = sliderEl.noUiSlider.get();
    const minM = parseFloat(vals[0]);
    const maxM = parseFloat(vals[1]);
    const water = document.getElementById('water').value;
    const maxSlope = parseFloat(document.getElementById('maxSlope').value);
    const clearanceM = (parseFloat(document.getElementById('clearanceM').value) || 0) / 2;

    if (minM >= maxM) {
        setStatus('Min must be less than max');
        return;
    }

    const {south, west, north, east} = searchState.currentBbox;
    if (Math.abs(north - south) > 0.1 || Math.abs(east - west) > 0.1) {
        setStatus('Area too large — max 0.1° per side');
        return;
    }

    setLoading(true);
    closeDetail();
    _resultsLayer.clearLayers();

    try {
        const qs = new URLSearchParams({
            south,
            west,
            north,
            east,
            min_m: minM,
            max_m: maxM,
            water,
            max_slope: maxSlope,
            clearance_m: clearanceM
        });
        const resp = await fetch('/spots?' + qs);
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.detail || `HTTP ${resp.status}`);
        }

        await parseSseStream(resp, async (msg) => {
            if (msg.error) throw new Error(msg.error);
            if (msg.status) setStatus(msg.status);
            if (msg.pct != null) setProgress(msg.pct);
            if (msg.result) {
                const geojson = msg.result;
                renderResults(geojson, minM, maxM);
                const n = geojson.features.length;
                setStatus(`${n} pair${n !== 1 ? 's' : ''} found`);
                _updateColorScale(); // widens range to include any existing custom lines
                updateTerrainLayer(searchState.currentBbox);
                updateContourLayer(searchState.currentBbox);
                updateLanduseLayer(searchState.currentBbox);
            }
            if (msg.landuse_update) {
                msg.landuse_update.forEach((feat, i) => {
                    if (detailState.currentFeatures[i]) {
                        detailState.currentFeatures[i].properties.corridor_features = feat.properties.corridor_features;
                        if (detailState.currentPairIdx === i && detailState.currentPair) {
                            detailState.currentPair.p.corridor_features = feat.properties.corridor_features;
                            const coords = detailState.currentPair.coords;
                            document.getElementById('detail-content').innerHTML =
                                buildDetailHtml(detailState.currentPair.p, coords);
                            _attachElevHover();
                            _updateCatenaryAt(detailState.lastCatT);
                        }
                    }
                });
            }
        });
    } catch (err) {
        setStatus('Error: ' + err.message);
    } finally {
        setLoading(false);
    }
}
