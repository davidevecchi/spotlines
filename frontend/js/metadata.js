import { meta } from './state.js';

fetch('/landuse/types').then(r => r.json()).then(d => {
    meta.landuseTypes = d;
}).catch(() => {});

fetch('/overpass/features').then(r => r.json()).then(d => {
    meta.overpasFeatures = d;
}).catch(() => {});
