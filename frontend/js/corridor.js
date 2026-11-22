import { meta } from './state.js';

export const _PLACE_CATS = new Set(['place', 'boundary']);

export function featureColor(f, fallback = '#64748b') {
    if (!f) return fallback;
    if (f.category === 'osmlanduse') {
        const key = (f.label || '').toLowerCase().replace(/\s+/g, '_');
        return meta.landuseTypes[key]?.color || fallback;
    }
    return meta.overpasFeatures[`${f.category}:${f.label}`.toLowerCase()]?.color || fallback;
}

export function _formatTag(val) {
    if (!val) return val;
    const s = val.replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
}

export function featureLabel(f) {
    if (!f) return '';
    if (f.label) {
        const l = _formatTag(f.label);
        return f.name ? `${l}: ${f.name}` : l;
    }
    return f.category || '';
}

export function _escXml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Return corridor features that overlap t (0–1 along line), excluding place/boundary
export function featuresAtT(features, t) {
    if (!features) return [];
    return features.filter(f => !_PLACE_CATS.has(f.category) && f.segments && f.segments.some(s => s.t_start <= t && t <= s.t_end));
}

export function kindLabel(k) {
    return (k || '').replace(/_/g, ' ');
}

// ── Bearing / compass ────────────────────────────────────────────────────────
export function computeBearing(lonA, latA, lonB, latB) {
    const toRad = d => d * Math.PI / 180;
    const y = Math.sin(toRad(lonB - lonA)) * Math.cos(toRad(latB));
    const x = Math.cos(toRad(latA)) * Math.sin(toRad(latB))
        - Math.sin(toRad(latA)) * Math.cos(toRad(latB)) * Math.cos(toRad(lonB - lonA));
    return ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
}

export function cardinal(deg) {
    return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(deg / 45) % 8];
}

export function compassSvg(deg) {
    const R = 13, cx = R + 1, cy = R + 1, sz = 2 * R + 2;
    const a = (deg - 90) * Math.PI / 180;
    const tx = (cx + R * 0.65 * Math.cos(a)).toFixed(1);
    const ty = (cy + R * 0.65 * Math.sin(a)).toFixed(1);
    const bx = (cx - R * 0.42 * Math.cos(a)).toFixed(1);
    const by = (cy - R * 0.42 * Math.sin(a)).toFixed(1);
    const ticks = [[0, 0, -R + 1.5], [90, R - 1.5, 0], [180, 0, R - 1.5], [270, -R + 1.5, 0]].map(([d, dx, dy]) =>
        `<text x="${(cx + dx).toFixed(1)}" y="${(cy + dy + 2).toFixed(1)}" font-size="5" fill="#a8a29e" text-anchor="middle" dominant-baseline="middle" font-family="system-ui">${['N', 'E', 'S', 'W'][d / 90]}</text>`
    ).join('');
    return `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}" style="vertical-align:middle;flex-shrink:0">
    <circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="rgba(180,140,90,0.3)" stroke-width="1"/>
    ${ticks}
    <line x1="${bx}" y1="${by}" x2="${tx}" y2="${ty}" stroke="#b45309" stroke-width="2" stroke-linecap="round"/>
    <circle cx="${tx}" cy="${ty}" r="2.5" fill="#b45309"/>
  </svg>`;
}

export function anchorCard(label, id, kind, tags, copyBtn, terrain) {
    tags = tags || {};
    const rows = [];
    const add = (k, ...keys) => {
        for (const key of keys) if (tags[key]) {
            rows.push(`<div class="ac-row">${k}: ${tags[key]}</div>`);
            return;
        }
    };
    if (terrain) rows.push(`<div class="ac-row">Terrain: ${terrain.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}</div>`);
    add('Species', 'species', 'species:en');
    add('Name', 'name');
    add('Circumference', 'circumference');
    add('Height', 'height');
    const osmLink = id > 0
        ? `<a href="https://www.openstreetmap.org/node/${id}" target="_blank">View on OSM ↗</a>`
        : '';
    return `<div class="anchor-card">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:4px">
      <div class="ac-title">${label}: ${kindLabel(kind)}</div>
      ${copyBtn || ''}
    </div>
    ${rows.join('')}${osmLink}
  </div>`;
}

// ── Anchor Material Symbol icon ───────────────────────────────────────────────
export function anchorIcon(kind, tags) {
    tags = tags || {};
    const MAP = {
        tree: 'nature',
        mast: 'cell_tower',
        flagpole: 'flag',
        street_lamp: 'table_lamp',
        pylon: 'gondola_lift',
        custom: 'not_listed_location',
    };
    if (kind === 'tree') {
        const lt = tags.leaf_type || '';
        const lc = tags.leaf_cycle || '';
        if (lt === 'needleleaved' || lc === 'evergreen') return 'park';
        if (lt === 'mixed' || lc === 'mixed') return 'forest';
        return 'nature';
    }
    return MAP[kind] || 'not_listed_location';
}

export function _normalizeLanduseSegs(landuseFeats) {
    const segs = [];
    for (const f of landuseFeats) {
        const col = featureColor(f);
        const label = _formatTag(f.label || f.category || '');
        for (const s of (f.segments || []))
            segs.push({t_start: s.t_start, t_end: s.t_end, col, label});
    }
    return segs.sort((a, b) => a.t_start - b.t_start);
}

// Render a single combined strip bar for all osmlanduse segments + a color legend below.
export function _buildLanduseStrip(landuseFeats) {
    if (!landuseFeats.length) return '';

    const segments = _normalizeLanduseSegs(landuseFeats);
    if (!segments.length) return '';

    // Legend — derived from surviving segments in appearance order.
    const legendOrder = [];
    const legendSeen = new Map();
    for (const s of segments) {
        if (!legendSeen.has(s.label)) {
            legendSeen.set(s.label, s.col);
            legendOrder.push([s.label, s.col]);
        }
    }

    const _n = segments.length;
    const fills = segments.map((s, i) => {
        const left = (s.t_start * 100).toFixed(3);
        const right = ((1 - s.t_end) * 100).toFixed(3);
        const leftVal = i > 0 ? `calc(${left}% + 1px)` : `${left}%`;
        const rightVal = i < _n - 1 ? `calc(${right}% + 1px)` : `${right}%`;
        return `<div title="${_escXml(s.label)}" style="position:absolute;left:${leftVal};right:${rightVal};height:100%;background:${s.col}"></div>`;
    }).join('');

    const strip = `<div style="position:relative;height:7px;background:rgba(180,140,90,0.13);border-radius:3px;overflow:hidden;margin:3px 0 4px">${fills}</div>`;

    const legendItems = legendOrder.map(([label, col]) =>
        `<span style="display:inline-flex;align-items:center;gap:3px;margin-right:7px;white-space:nowrap">` +
        `<span style="display:inline-block;width:7px;height:7px;border-radius:1px;background:${col};flex-shrink:0"></span>` +
        `<span style="font-size:9px;color:#57534e">${_escXml(label)}</span></span>`
    ).join('');
    const legend = `<div style="display:flex;flex-wrap:wrap;gap:1px 0;margin-top:1px">${legendItems}</div>`;

    return `<div style="margin-bottom:5px">` +
        `<div style="font-size:9px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#a8a29e;margin-bottom:2px">Land use</div>` +
        strip + legend + `</div>`;
}

export function buildCorridorFeatureList(feats) {
    if (!feats || !feats.length) return '';

    // Split into osmlanduse vs overpass features
    const landuseRaw = feats.filter(f => f.category === 'osmlanduse');
    const overpassRaw = feats.filter(f => f.category !== 'osmlanduse');

    // ── Overpass: aggregate duplicates by (category, label), render one row each ──
    let overpassHtml = '';
    if (overpassRaw.length) {
        const merged = [];
        const mergeKey = f => `${f.category}|${featureLabel(f)}`;
        const seen = new Map();
        for (const f of overpassRaw) {
            const k = mergeKey(f);
            if (seen.has(k)) {
                const m = merged[seen.get(k)];
                for (const s of (f.segments || [])) m.segments.push(s);
            } else {
                seen.set(k, merged.length);
                merged.push({...f, segments: [...(f.segments || [])]});
            }
        }

        let rows = '';
        for (const f of merged) {
            const col = featureColor(f);
            const segsArr = (f.segments || []).slice().sort((a, b) => a.t_start - b.t_start);
            const flagBlocker = f.is_blocker ? '<span style="color:#dc2626;font-size:9px;margin-left:4px">blocker</span>' : '';
            const flagWater = f.is_water ? '<span style="color:#0369a1;font-size:9px;margin-left:4px">water</span>' : '';

            let barHtml = '';
            if (segsArr.length) {
                const _m = segsArr.length;
                const fills = segsArr.map((s, i) => {
                    const barX = (s.t_start * 100).toFixed(1);
                    const barW = Math.max((s.t_end - s.t_start) * 100, 0.5).toFixed(1);
                    const xVal = i > 0 ? `calc(${barX}% + 1px)` : `${barX}%`;
                    const wAdj = (i > 0 ? 1 : 0) + (i < _m - 1 ? 1 : 0);
                    const wVal = wAdj > 0 ? `calc(${barW}% - ${wAdj}px)` : `${barW}%`;
                    return `<div style="position:absolute;left:${xVal};width:${wVal};height:100%;background:${col};border-radius:2px"></div>`;
                }).join('');
                barHtml = `<div style="margin-top:2px;position:relative;height:5px;background:rgba(180,140,90,0.15);border-radius:2px;overflow:hidden">${fills}</div>`;
            }

            const valuePart = f.label ? _escXml(_formatTag(f.label)) : _escXml(f.category || '');
            const namePart = f.label && f.name ? `<span style="font-weight:300">: ${_escXml(f.name)}</span>` : '';
            const keyPart = f.label ? `<span style="color:#a8a29e;font-weight:300;font-size:9px;margin-left:4px">${_escXml(f.category || '')}</span>` : '';
            rows += `<div style="margin-bottom:5px">` +
                `<div style="font-size:10px;font-weight:500;color:${col}">${valuePart}${namePart}${keyPart}` +
                flagBlocker + flagWater + `</div>` +
                barHtml + `</div>`;
        }
        overpassHtml = rows;
    }

    // ── osmlanduse: single merged strip + legend ──
    const landuseHtml = _buildLanduseStrip(landuseRaw);

    const inner = (overpassHtml ? `<div style="margin-bottom:${landuseHtml ? 7 : 0}px">${overpassHtml}</div>` : '') +
        landuseHtml;

    return `<div style="margin-top:4px;padding:7px 8px;background:rgba(0,0,0,0.03);border-radius:5px;border:1px solid rgba(180,140,90,0.15)">${inner}</div>`;
}
