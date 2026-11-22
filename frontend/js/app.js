import './metadata.js';
import { map } from './map.js';
import { toggleOverlay } from './overlays.js';
import { toggleSelectArea } from './draw.js';
import { toggleDrawLine } from './draw-line.js';
import { doSearch } from './search.js';
import { collapseDetail, reopenDetail, hideLegend } from './detail.js';
import { geolocate, onLinePresetChange, updateLineHint, updateFiltersHint } from './controls.js';
import { focusLine, copyCoords } from './selection.js';

// Expose for static onclick= attributes in HTML and dynamically-injected innerHTML
window.map               = map;
window.toggleSelectArea  = toggleSelectArea;
window.doSearch          = doSearch;
window.toggleDrawLine    = toggleDrawLine;
window.collapseDetail    = collapseDetail;
window.reopenDetail      = reopenDetail;
window.hideLegend        = hideLegend;
window.toggleOverlay     = toggleOverlay;
window.geolocate         = geolocate;
window.onLinePresetChange = onLinePresetChange;
window.focusLine         = focusLine;
window.copyCoords        = copyCoords;

// Boot
updateLineHint();
updateFiltersHint();
