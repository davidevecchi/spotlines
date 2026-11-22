export const MAX_SLOPE_DEG = 45;      // slope colour scale ceiling (matches backend)
export const GRAVITY = 9.81;          // m/s²
export const NR_MAX_ITERS = 60;       // Newton-Raphson iteration limit
export const NR_EPSILON = 1e-7;       // Newton-Raphson convergence threshold
export const WALKER_CLAMP = [0.02, 0.98];     // fraction range for walker along line
export const SMOOTH_KERNEL = [0.15, 0.70, 0.15]; // elevation profile smoothing weights
export const WALKER_SAMPLES = 21;     // points sampled along the solved catenary curve (post-solve, not per NR iteration)
export const LINE_PRESETS = {
    tubular25: {EA: 150_000, w: 0.70},
    dyneema1: {EA: 1_500_000, w: 0.07},
};
