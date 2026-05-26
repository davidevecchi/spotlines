"""
catenary.py – Sag and catenary calculations for tensioned lines.

Two physical scenarios are supported:

1. **Point-load sag** – a concentrated load W [N] applied at the midpoint of a
   massless rope of span L [m] under horizontal tension T [N].

       sag = W · L / (4 · T)                    [exact, massless rope]

   This is the formula the project uses for slackline / hammock / rigging
   checks where the rope's own weight is negligible compared to the body load.

2. **Distributed-load (catenary)** – a uniform rope of linear weight density
   w [N/m] hanging between two equal-height supports separated by span L [m]
   under horizontal tension T_h [N].

   Exact:          sag = a · (cosh(L / (2a)) − 1)   where a = T_h / w
   Parabolic approx (small-sag):  sag ≈ w · L² / (8 · T_h)

   The parabolic approximation error is < 0.4 % for sag/L < 0.1
   (i.e. sag less than 10 % of span).

Relationship between the two for the same *total* weight W = w·L:

    point-load:   sag = W·L / (4·T)
    distributed:  sag ≈ W·L / (8·T)   (factor-of-2 difference)

The point-load formula is more conservative (predicts larger sag) because all
weight acts at the worst possible location.
"""

from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Point-load (body-weight) sag
# ---------------------------------------------------------------------------

def point_load_sag(body_weight_N: float, span_m: float, tension_N: float) -> float:
    """Return the sag [m] at the midpoint of a tensioned rope carrying a
    concentrated load *body_weight_N* [N] at its centre.

    Derivation
    ----------
    By symmetry each half-span makes the same angle θ with horizontal.
    Vertical equilibrium at the load:

        2 · T · sin θ = W
        sin θ ≈ tan θ = sag / (L/2)   (small-angle / geometry)

    Solving:

        sag = W · L / (4 · T)

    This is exact for a massless rope and remains a very good approximation
    when the rope's self-weight is small relative to W.

    Parameters
    ----------
    body_weight_N : float
        Concentrated load at midpoint [Newtons].
    span_m : float
        Horizontal distance between the two anchor points [metres].
    tension_N : float
        Horizontal component of tension in the rope [Newtons].

    Returns
    -------
    float
        Sag at midpoint [metres].

    Raises
    ------
    ValueError
        If any argument is non-positive.
    """
    if body_weight_N <= 0:
        raise ValueError(f"body_weight_N must be positive, got {body_weight_N}")
    if span_m <= 0:
        raise ValueError(f"span_m must be positive, got {span_m}")
    if tension_N <= 0:
        raise ValueError(f"tension_N must be positive, got {tension_N}")

    return (body_weight_N * span_m) / (4.0 * tension_N)


# ---------------------------------------------------------------------------
# Distributed-load sag (catenary / parabolic)
# ---------------------------------------------------------------------------

def catenary_sag_exact(
    linear_weight_N_per_m: float, span_m: float, horizontal_tension_N: float
) -> float:
    """Return the exact catenary sag [m] at midspan for a uniformly loaded rope.

    The catenary profile is y(x) = a·(cosh(x/a) − 1) with origin at the
    lowest point, where a = T_h / w.  The sag is y evaluated at x = L/2.

    Parameters
    ----------
    linear_weight_N_per_m : float
        Weight per unit length of the rope [N/m].
    span_m : float
        Horizontal distance between the two (equal-height) anchor points [m].
    horizontal_tension_N : float
        Horizontal component of tension (constant along the rope) [N].

    Returns
    -------
    float
        Exact catenary sag at midspan [metres].
    """
    if linear_weight_N_per_m <= 0:
        raise ValueError(f"linear_weight_N_per_m must be positive, got {linear_weight_N_per_m}")
    if span_m <= 0:
        raise ValueError(f"span_m must be positive, got {span_m}")
    if horizontal_tension_N <= 0:
        raise ValueError(f"horizontal_tension_N must be positive, got {horizontal_tension_N}")

    a = horizontal_tension_N / linear_weight_N_per_m  # catenary parameter [m]
    return a * (math.cosh(span_m / (2.0 * a)) - 1.0)


def catenary_sag_parabolic(
    linear_weight_N_per_m: float, span_m: float, horizontal_tension_N: float
) -> float:
    """Return the parabolic-approximation sag [m] for a uniformly loaded rope.

    Equivalent to the small-angle / small-sag approximation of the exact
    catenary:  sag ≈ w · L² / (8 · T_h).

    Accurate to within 0.4 % when sag/span < 0.1; grows to ~1.5 % at
    sag/span = 0.2.

    Parameters
    ----------
    linear_weight_N_per_m : float
        Weight per unit length [N/m].
    span_m : float
        Horizontal span [m].
    horizontal_tension_N : float
        Horizontal tension component [N].

    Returns
    -------
    float
        Parabolic sag at midspan [metres].
    """
    if linear_weight_N_per_m <= 0:
        raise ValueError(f"linear_weight_N_per_m must be positive, got {linear_weight_N_per_m}")
    if span_m <= 0:
        raise ValueError(f"span_m must be positive, got {span_m}")
    if horizontal_tension_N <= 0:
        raise ValueError(f"horizontal_tension_N must be positive, got {horizontal_tension_N}")

    return (linear_weight_N_per_m * span_m ** 2) / (8.0 * horizontal_tension_N)


# ---------------------------------------------------------------------------
# Tension at support (catenary)
# ---------------------------------------------------------------------------

def catenary_support_tension(
    linear_weight_N_per_m: float, span_m: float, horizontal_tension_N: float
) -> float:
    """Return the tension magnitude at the anchor supports [N].

    At the support the rope is inclined at angle θ where tan θ = sinh(L/(2a)),
    a = T_h/w.  The tension magnitude follows from the catenary geometry:

        T_support = T_h · cosh(L / (2a))

    This equals hypot(T_h, V) where V = T_h·sinh(L/(2a)) is the exact
    vertical reaction (= w × arc-length/2, NOT w×span/2, which is only
    an approximation valid for small sag).

    Parameters
    ----------
    linear_weight_N_per_m : float
        Linear weight density [N/m].
    span_m : float
        Horizontal span [m].
    horizontal_tension_N : float
        Horizontal tension component [N].

    Returns
    -------
    float
        Tension magnitude at the support [N].
    """
    if linear_weight_N_per_m <= 0 or span_m <= 0 or horizontal_tension_N <= 0:
        raise ValueError("All arguments must be positive.")

    a = horizontal_tension_N / linear_weight_N_per_m  # catenary parameter
    return horizontal_tension_N * math.cosh(span_m / (2.0 * a))


# ---------------------------------------------------------------------------
# Utility: sag-to-tension inversion (point-load)
# ---------------------------------------------------------------------------

def required_tension(body_weight_N: float, span_m: float, max_sag_m: float) -> float:
    """Return the horizontal tension [N] required to keep the sag below
    *max_sag_m* for a concentrated midpoint load.

    Rearrangement of the point-load formula:

        T = W · L / (4 · sag)

    Parameters
    ----------
    body_weight_N : float
        Load at midpoint [N].
    span_m : float
        Span [m].
    max_sag_m : float
        Maximum allowable sag at midpoint [m].

    Returns
    -------
    float
        Minimum required horizontal tension [N].
    """
    if body_weight_N <= 0 or span_m <= 0 or max_sag_m <= 0:
        raise ValueError("All arguments must be positive.")

    return (body_weight_N * span_m) / (4.0 * max_sag_m)
