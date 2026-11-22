// All shared mutable state

export const meta = {
    landuseTypes: {},
    overpasFeatures: {},
};

export const mapState = {
    activeBaseKey: 'osm',
    blankOn: false,
    blankRect: null,
};

export const overlayState = {
    terrainLayer: null,
    terrainBbox: null,
    slopeStats: { min: 0, max: 45 },
    contourLayer: null,
    contourSvgEl: null,
    elevStats: { min: 0, max: 0 },
    OV: { slope: false, elev: false, contour: false, hillshade: false, names: false, landuse: false },
};

export const drawLineState = {
    active: false,
    pointA: null,
    markerA: null,
    markerB: null,
    prevLayer: null,
    pendingLine: null,
    customLineData: [],
    // Registered by draw-line.js so draw.js can cancel without circular import
    cancelFn: null,
};

export const searchState = {
    drawHandler: null,
    currentBbox: null,
};

export const detailState = {
    currentPair: null,
    currentPairIdx: -1,
    currentFeatures: [],
    elevMarker: null,
    svgCursorState: null,
    catState: null,
    lastCatT: 0.5,
    collapsedPair: null,
    currentDetailCoords: null,
};

export const selectionState = {
    selectedLayer: null,
    outlineGeoJson: null,
    coloredGeoJson: null,
    lastRealMin: 0,
    lastRealMax: 1,
    highlightOutline: null,
    highlightColor: null,
    anchorLabelA: null,
    anchorLabelB: null,
};
