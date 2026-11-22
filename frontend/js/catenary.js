import { WALKER_SAMPLES } from './constants.js';

// Elastic catenary position at natural arc-length s from start.
// H = horizontal tension (N), V = upward reaction at s=0 (N),
// w = weight per unit natural length (N/m), EA = axial stiffness (N).
export function _catAt(H, V, s, w, EA) {
    if (s === 0) return {x: 0, y: 0};
    const T0 = Math.hypot(H, V);
    const Vs = V - w * s;
    const Ts = Math.hypot(H, Vs);
    return {
        x: H * s / EA + H / w * (Math.asinh(V / H) - Math.asinh(Vs / H)),
        y: (V * s - w * s * s / 2) / EA + (T0 - Ts) / w,
    };
}

// 2×2 Newton–Raphson. f(x,y) returns [r1,r2]. Returns [x,y] or null.
export function _nr2(f, x0, y0) {
    let [x, y] = [x0, y0];
    const d = 1e-6, eps = 1e-7;
    for (let i = 0; i < 60; i++) {
        const r = f(x, y);
        if (Math.abs(r[0]) < eps && Math.abs(r[1]) < eps) return [x, y];
        const rx = f(x + d, y), ry = f(x, y + d);
        const J11 = (rx[0] - r[0]) / d, J12 = (ry[0] - r[0]) / d;
        const J21 = (rx[1] - r[1]) / d, J22 = (ry[1] - r[1]) / d;
        const det = J11 * J22 - J12 * J21;
        if (Math.abs(det) < 1e-14) return null;
        x -= (J22 * r[0] - J12 * r[1]) / det;
        y -= (J11 * r[1] - J21 * r[0]) / det;
    }
    return null;
}

// 3×3 Newton–Raphson. f(x,y,z) returns [r1,r2,r3]. Returns [x,y,z] or null.
export function _nr3(f, x0, y0, z0) {
    let [x, y, z] = [x0, y0, z0];
    const d = 1e-6, eps = 1e-7;
    for (let i = 0; i < 80; i++) {
        const r = f(x, y, z);
        if (r.every(v => Math.abs(v) < eps)) return [x, y, z];
        const rx = f(x + d, y, z), ry = f(x, y + d, z), rz = f(x, y, z + d);
        const [a, b, c] = [(rx[0] - r[0]) / d, (ry[0] - r[0]) / d, (rz[0] - r[0]) / d];
        const [dd, e, ff] = [(rx[1] - r[1]) / d, (ry[1] - r[1]) / d, (rz[1] - r[1]) / d];
        const [g, hh, k] = [(rx[2] - r[2]) / d, (ry[2] - r[2]) / d, (rz[2] - r[2]) / d];
        const det = a * (e * k - ff * hh) - b * (dd * k - ff * g) + c * (dd * hh - e * g);
        if (Math.abs(det) < 1e-14) return null;
        const inv = [
            [(e * k - ff * hh) / det, -(b * k - c * hh) / det, (b * ff - c * e) / det],
            [-(dd * k - ff * g) / det, (a * k - c * g) / det, -(a * ff - c * dd) / det],
            [(dd * hh - e * g) / det, -(a * hh - b * g) / det, (a * e - b * dd) / det],
        ];
        const dr = inv.map(row => row[0] * r[0] + row[1] * r[1] + row[2] * r[2]);
        x -= dr[0];
        y -= dr[1];
        z -= dr[2];
    }
    return null;
}

// Find natural length L0 and vertical reaction V0 for an unloaded catenary
// spanning (0,0)→(span,rise) with horizontal tension H0.
export function _solveUnloaded(span, rise, H0, w, EA) {
    const Lgeom = Math.hypot(span, rise);
    const r = _nr2(
        (V, L) => {
            if (L <= 0 || H0 <= 0) return [1e9, 1e9];
            const e = _catAt(H0, V, L, w, EA);
            return [e.x - span, e.y - rise];
        },
        H0 * rise / span + w * Lgeom * 0.5,
        Lgeom * (1 - H0 / EA),
    );
    return r ? {V0: r[0], L0: r[1]} : null;
}

// Solve two coupled elastic catenaries with walker at horizontal position xw (m).
// d = span, h = rise (elev_b − elev_a), L0 = natural length, W = walker load (N).
// Returns { pts[21], walkerPt, TA, TB } or null.
export function _solveLoaded(d, h, L0, w, EA, xw, W, H0) {
    if (xw < 0.02 * d || xw > 0.98 * d) return null;
    const L1i = L0 * (xw / d);
    const Hi = H0 || 2000;
    // Taut-string approximation: V1 ≈ H·(h/d) + W·(d−xw)/d  (downward tension component at A)
    const V1i = Hi * h / d + W * (d - xw) / d;
    const res = _nr3(
        (H, V1, L1) => {
            if (H < 1 || L1 < 0.005 * L0 || L1 > 0.995 * L0) return [1e9, 1e9, 1e9];
            const L2 = L0 - L1, V2 = V1 - w * L1 - W;
            const e1 = _catAt(H, V1, L1, w, EA);
            const e2 = _catAt(H, V2, L2, w, EA);
            return [e1.x - xw, e2.x - (d - xw), e1.y + e2.y + h];
        },
        Hi, V1i, L1i,
    );
    if (!res) return null;
    const [H, V1, L1] = res;
    if (H < 0 || L1 <= 0 || L1 >= L0) return null;
    const L2 = L0 - L1, V2 = V1 - w * L1 - W;
    const walkerPt = _catAt(H, V1, L1, w, EA);

    // Sample WALKER_SAMPLES points evenly along natural length (post-solve curve reconstruction)
    const pts = [];
    for (let i = 0; i < WALKER_SAMPLES; i++) {
        const s = (i / (WALKER_SAMPLES - 1)) * L0;
        if (s <= L1) {
            pts.push(_catAt(H, V1, s, w, EA));
        } else {
            const p2 = _catAt(H, V2, s - L1, w, EA);
            pts.push({x: walkerPt.x + p2.x, y: walkerPt.y + p2.y});
        }
    }
    return {pts, walkerPt, TA: Math.hypot(H, V1), TB: Math.hypot(H, V2 - w * L2)};
}
