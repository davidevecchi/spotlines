export function setStatus(msg) {
    const el = document.getElementById('status');
    el.textContent = msg;
    let state = 'idle';
    if (msg.startsWith('Error:')) state = 'error';
    else if (/\d+ pairs? found/.test(msg)) state = 'done';
    else if (msg.includes('Area selected')) state = 'ready';
    else if (msg.includes('too large') || msg.includes('must be less')) state = 'error';
    else if (msg.includes('Draw') || msg.includes('first')) state = 'idle';
    el.dataset.state = state;
}

const _PHASES = [3, 35, 45, 55, 63, 75, 100];
const _tween = {current: 0, target: 0, raf: null};

function _tweenTick() {
    const gap = _tween.target - _tween.current;
    if (gap < 0.05) return;
    _tween.current += gap * 0.015;
    document.getElementById('progress-bar').style.width = _tween.current.toFixed(2) + '%';
    _tween.raf = requestAnimationFrame(_tweenTick);
}

export function setProgress(pct) {
    if (_tween.raf) {
        cancelAnimationFrame(_tween.raf);
        _tween.raf = null;
    }
    const bar = document.getElementById('progress-bar');
    bar.style.opacity = '1';
    bar.style.transition = 'none';
    bar.style.width = pct + '%';
    _tween.current = pct;
    const next = _PHASES.find(p => p > pct);
    _tween.target = next != null ? next - 0.5 : pct;
    if (_tween.target > pct) _tween.raf = requestAnimationFrame(_tweenTick);
}

export function setLoading(on) {
    document.getElementById('searchBtn').disabled = on;
    if (_tween.raf) {
        cancelAnimationFrame(_tween.raf);
        _tween.raf = null;
    }
    const bar = document.getElementById('progress-bar');
    if (on) {
        bar.style.transition = 'none';
        bar.style.width = '0%';
        bar.style.opacity = '1';
        _tween.current = 0;
        document.getElementById('status').dataset.state = 'loading';
    } else {
        bar.style.transition = 'width 0.2s ease, opacity 0.5s ease 0.25s';
        bar.style.width = '100%';
        setTimeout(() => {
            bar.style.opacity = '0';
        }, 50);
    }
}
