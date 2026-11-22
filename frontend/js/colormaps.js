// Cyan → blue → violet → magenta
export const PLASMA = [
    [0.00, [0, 220, 240]],
    [0.33, [0, 100, 255]],
    [0.67, [130, 0, 255]],
    [1.00, [255, 0, 180]],
];

export const JET = [ // YlOrRd: yellow (flat) → red (steep)
    [0.000, [255, 255, 178]],
    [0.250, [254, 204, 92]],
    [0.500, [253, 141, 60]],
    [0.750, [240, 59, 32]],
    [1.000, [189, 0, 38]],
];

// gist_earth (land portion, t=0.22→1.0) — deep green valleys → olive → sandy tan → near-white peaks
export const TERRAIN = [
    [0.00, [37, 102, 124]],
    [0.14, [53, 134, 112]],
    [0.28, [66, 151, 78]],
    [0.42, [120, 166, 82]],
    [0.57, [169, 179, 91]],
    [0.71, [191, 165, 100]],
    [0.85, [220, 185, 164]],
    [1.00, [253, 250, 250]],
];

export function _interp(cmap, t) {
    t = Math.max(0, Math.min(1, t));
    let i = 0;
    while (i < cmap.length - 2 && t >= cmap[i + 1][0]) i++;
    const [t0, c0] = cmap[i];
    const [t1, c1] = cmap[Math.min(i + 1, cmap.length - 1)];
    const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
    const ch = v => Math.round(v).toString(16).padStart(2, '0');
    return '#' + [0, 1, 2].map(k => ch(c0[k] + f * (c1[k] - c0[k]))).join('');
}

export const cmapColor = t => _interp(PLASMA, t);
export const slopeColor = t => _interp(JET, t);
export const terrainColor = t => _interp(TERRAIN, t);
