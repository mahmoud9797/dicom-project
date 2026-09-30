/* Four synchronized scan views. CT and mask slices share a 1 GiB cache. */
(() => {
    const config = JSON.parse(document.getElementById('viewer-config').textContent);
    const names = ['axial', 'coronal', 'sagittal'];
    const presets = {
        maxillofacial: [300, 3000],
        bone: [300, 3000], dental: [700, 3000], air: [-1000, -350],
        soft: [-150, 250],
    };
    const colors = {
        maxillofacial: [0.91, 0.86, 0.76],
        bone: [0.86, 0.86, 0.82], dental: [0.95, 0.81, 0.58],
        air: [0.40, 0.78, 0.96], soft: [0.94, 0.54, 0.62],
        custom: [0.71, 0.87, 0.89],
    };
    const grid = document.getElementById('viewer-grid');
    const surfacePanel = grid.querySelector('[data-view="surface"]');
    const surfaceStage = surfacePanel.querySelector('[data-stage]');
    const surfaceCanvas = surfacePanel.querySelector('[data-canvas]');
    const surfaceMessage = surfacePanel.querySelector('[data-message]');
    const presetInput = document.getElementById('threshold-preset');
    const minimumInput = document.getElementById('threshold-min');
    const maximumInput = document.getElementById('threshold-max');
    const smoothingInput = document.getElementById('surface-smoothing');
    const exportButton = document.getElementById('export-stl');
    const exportObjButton = document.getElementById('export-obj');
    const exportMaskButton = document.getElementById('export-mask');
    const maskSummary = document.getElementById('mask-summary');
    const showMaskInput = document.getElementById('show-mask-overlay');
    const maskOpacityInput = document.getElementById('mask-opacity');
    const locatorCoordinates = document.getElementById('locator-coordinates');
    const clearLocatorButton = document.getElementById('clear-locator');
    const centerInput = document.getElementById('window-center');
    const widthInput = document.getElementById('window-width');
    const brightnessInput = document.getElementById('gray-brightness');
    const contrastInput = document.getElementById('gray-contrast');
    const littleEndian = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
    const maxCacheBytes = 1024 * 1024 * 1024;
    const cache = new Map();
    const pending = new Map();
    let cacheBytes = 0;
    let lookup = null;
    let lookupKey = '';
    let activeName = 'axial';
    const gray = {
        center: Number.isFinite(config.windowCenter) ? config.windowCenter : NaN,
        width: Number.isFinite(config.windowWidth) && config.windowWidth > 0 ? config.windowWidth : NaN,
        brightness: 0, contrast: 1,
        invert: config.photometric === 'MONOCHROME1',
    };
    const views = {};
    let locator = null;
    const surface = {
        gl: null, program: null, buffer: null, count: 0,
        yaw: 0.55, pitch: -0.35, zoom: 1, drag: null,
        request: null, sequence: 0, applied: null,
    };
    const mask = {request: null, sequence: 0, applied: null};
    let thresholdTimer = null;

    function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
    function setError(view, text) { view.message.textContent = text; }
    function remember(key, pixels) {
        if (cache.has(key)) cacheBytes -= cache.get(key).byteLength;
        cache.delete(key);
        cache.set(key, pixels);
        cacheBytes += pixels.byteLength;
        while (cacheBytes > maxCacheBytes && cache.size > 1) {
            const oldest = cache.keys().next().value;
            cacheBytes -= cache.get(oldest).byteLength;
            cache.delete(oldest);
        }
    }
    function fetchPixels(name, index) {
        const key = `${name}:${index}`;
        if (cache.has(key)) {
            const pixels = cache.get(key);
            remember(key, pixels);
            return Promise.resolve(pixels);
        }
        if (pending.has(key)) return pending.get(key);
        const plane = config.planes[name];
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), name === 'axial' ? 30000 : 120000);
        const promise = (async () => {
            try {
                const response = await fetch(`${plane.urlBase}${index}/`, {
                    credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                });
                if (response.redirected && response.url.includes('/login/')) {
                    window.location.assign(response.url);
                    throw new Error('Sign in again to view this scan.');
                }
                if (!response.ok) throw new Error('Unable to load this DICOM slice.');
                const buffer = await response.arrayBuffer();
                if (buffer.byteLength !== plane.rows * plane.columns * plane.bytesPerPixel) {
                    throw new Error('DICOM pixel data has an unexpected size.');
                }
                let pixels;
                if (plane.bytesPerPixel === 1) {
                    pixels = config.signed ? new Int8Array(buffer) : new Uint8Array(buffer);
                } else if (littleEndian) {
                    pixels = config.signed ? new Int16Array(buffer) : new Uint16Array(buffer);
                } else {
                    const source = new DataView(buffer);
                    pixels = config.signed ? new Int16Array(buffer.byteLength / 2) : new Uint16Array(buffer.byteLength / 2);
                    for (let i = 0; i < pixels.length; i++) {
                        pixels[i] = config.signed ? source.getInt16(i * 2, true) : source.getUint16(i * 2, true);
                    }
                }
                remember(key, pixels);
                return pixels;
            } catch (error) {
                if (error.name === 'AbortError') throw new Error('The scan request timed out.');
                if (error instanceof TypeError) throw new Error('Cannot reach the scan server.');
                throw error;
            } finally {
                clearTimeout(timeout);
                pending.delete(key);
            }
        })();
        pending.set(key, promise);
        return promise;
    }

    function fetchMaskPixels(name, index, params) {
        const key = `mask:${params.minimum}:${params.maximum}:${params.preset}:${name}:${index}`;
        if (cache.has(key)) {
            const pixels = cache.get(key);
            remember(key, pixels);
            return Promise.resolve(pixels);
        }
        if (pending.has(key)) return pending.get(key);
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 120000);
        const promise = (async () => {
            try {
                const url = `${config.maskPlanes[name]}${index}/?${new URLSearchParams(params)}`;
                const response = await fetch(url, {
                    credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                });
                if (response.redirected && response.url.includes('/login/')) {
                    window.location.assign(response.url);
                    throw new Error('Sign in again to view this mask.');
                }
                if (!response.ok) throw new Error('Unable to load the mask overlay.');
                const pixels = new Uint8Array(await response.arrayBuffer());
                const plane = config.planes[name];
                if (pixels.length !== plane.rows * plane.columns) throw new Error('Mask dimensions do not match the CT image.');
                remember(key, pixels);
                return pixels;
            } finally {
                clearTimeout(timeout);
                pending.delete(key);
            }
        })();
        pending.set(key, promise);
        return promise;
    }

    function updatePosition(view) {
        const text = `${view.index + 1} / ${view.plane.count}`;
        view.range.value = String(view.index + 1);
        view.count.textContent = text;
        view.position.textContent = text;
    }
    function fitView(view) {
        if (!view.stage.clientWidth || !view.stage.clientHeight) return;
        const width = view.plane.columns * view.plane.columnSpacing;
        const height = view.plane.rows * view.plane.rowSpacing;
        const factor = Math.min((view.stage.clientWidth - 20) / width, (view.stage.clientHeight - 20) / height);
        view.canvas.style.width = `${Math.max(1, width * factor)}px`;
        view.canvas.style.height = `${Math.max(1, height * factor)}px`;
        transformView(view);
    }
    function transformView(view) {
        view.canvas.style.transform = `translate(-50%, -50%) translate(${view.panX}px, ${view.panY}px) scale(${view.zoom})`;
    }
    function syncGrayInputs() {
        centerInput.value = Number.isFinite(gray.center) ? String(Math.round(gray.center)) : '';
        widthInput.value = Number.isFinite(gray.width) ? String(Math.round(gray.width)) : '';
    }
    function autoGray() {
        const view = views[activeName]?.pixels ? views[activeName] : names.map(name => views[name]).find(item => item.pixels);
        if (!view) return;
        const samples = [];
        const step = Math.max(1, Math.floor(view.pixels.length / 20000));
        for (let i = 0; i < view.pixels.length; i += step) {
            samples.push(view.pixels[i] * config.slope + config.intercept);
        }
        samples.sort((a, b) => a - b);
        gray.center = (samples[Math.floor(samples.length * .02)] + samples[Math.floor(samples.length * .98)]) / 2;
        gray.width = Math.max(1, samples[Math.floor(samples.length * .98)] - samples[Math.floor(samples.length * .02)]);
        syncGrayInputs();
        renderAll();
    }
    function grayLookup(bytesPerPixel) {
        const key = [bytesPerPixel, config.signed, config.slope, config.intercept,
            gray.center, gray.width, gray.brightness, gray.contrast, gray.invert].join(':');
        if (key === lookupKey) return lookup;
        const bits = bytesPerPixel * 8;
        const offset = config.signed ? 1 << (bits - 1) : 0;
        const table = new Uint8Array(1 << bits);
        const lower = gray.center - gray.width / 2;
        for (let i = 0; i < table.length; i++) {
            const value = (i - offset) * config.slope + config.intercept;
            let level = clamp((value - lower) * 255 / gray.width, 0, 255);
            level = clamp((level - 127.5) * gray.contrast + 127.5 + gray.brightness * 2.55, 0, 255);
            table[i] = gray.invert ? 255 - Math.round(level) : Math.round(level);
        }
        lookup = table;
        lookupKey = key;
        return table;
    }
    function renderView(view) {
        if (!view.pixels || view.renderQueued || !Number.isFinite(gray.width)) return;
        view.renderQueued = true;
        requestAnimationFrame(() => {
            view.renderQueued = false;
            if (!view.pixels) return;
            const table = grayLookup(view.plane.bytesPerPixel);
            const offset = config.signed ? 1 << (view.plane.bytesPerPixel * 8 - 1) : 0;
            const rgba = view.imageData.data;
            const overlay = showMaskInput.checked ? view.maskPixels : null;
            const opacity = Number(maskOpacityInput.value) / 100;
            for (let i = 0, j = 0; i < view.pixels.length; i++, j += 4) {
                const level = table[view.pixels[i] + offset];
                if (overlay && overlay[i]) {
                    rgba[j] = Math.round(level * (1 - opacity) + 255 * opacity);
                    rgba[j + 1] = Math.round(level * (1 - opacity) + 120 * opacity);
                    rgba[j + 2] = Math.round(level * (1 - opacity) + 62 * opacity);
                } else {
                    rgba[j] = rgba[j + 1] = rgba[j + 2] = level;
                }
                rgba[j + 3] = 255;
            }
            view.context.putImageData(view.imageData, 0, 0);
            drawLocator(view);
        });
    }
    function renderAll() { names.forEach(name => renderView(views[name])); }
    function screenZ(z) {
        return config.superiorLast ? config.planes.axial.count - 1 - z : z;
    }
    function drawLocator(view) {
        if (!locator) return;
        const x = view.name === 'sagittal' ? locator.y : locator.x;
        const y = view.name === 'axial' ? locator.y : screenZ(locator.z);
        const context = view.context;
        context.save();
        context.strokeStyle = '#00e5ff';
        context.lineWidth = 1;
        context.setLineDash([4, 3]);
        context.beginPath();
        context.moveTo(x + .5, 0);
        context.lineTo(x + .5, view.canvas.height);
        context.moveTo(0, y + .5);
        context.lineTo(view.canvas.width, y + .5);
        context.stroke();
        context.setLineDash([]);
        context.fillStyle = '#00e5ff';
        context.beginPath();
        context.arc(x + .5, y + .5, 2.5, 0, Math.PI * 2);
        context.fill();
        context.restore();
    }
    function refreshLocator() {
        locatorCoordinates.textContent = locator
            ? `Voxel X ${locator.x + 1} · Y ${locator.y + 1} · Z ${locator.z + 1}`
            : 'No locator';
        clearLocatorButton.disabled = !locator;
        renderAll();
    }
    function placeLocator(view, event) {
        const rect = view.canvas.getBoundingClientRect();
        const px = Math.floor((event.clientX - rect.left) * view.canvas.width / rect.width);
        const py = Math.floor((event.clientY - rect.top) * view.canvas.height / rect.height);
        if (px < 0 || py < 0 || px >= view.canvas.width || py >= view.canvas.height) return;
        const z = view.name === 'axial' ? view.index
            : config.superiorLast ? config.planes.axial.count - 1 - py : py;
        locator = {
            x: view.name === 'sagittal' ? view.index : px,
            y: view.name === 'axial' ? py : view.name === 'coronal' ? view.index : px,
            z,
        };
        refreshLocator();
        const target = {axial: locator.z, coronal: locator.y, sagittal: locator.x};
        names.forEach(name => {
            if (views[name].index !== target[name]) requestSlice(views[name], target[name]);
        });
    }
    async function loadMaskOverlay(view) {
        const params = mask.applied;
        if (!params || !view.pixels || view.displayedIndex !== view.index) return;
        const sequence = view.sequence;
        const index = view.index;
        try {
            const pixels = await fetchMaskPixels(view.name, index, params);
            if (view.sequence !== sequence || mask.applied !== params || view.displayedIndex !== index) return;
            view.maskPixels = pixels;
            setError(view, '');
            renderView(view);
        } catch (error) {
            if (view.sequence === sequence && mask.applied === params) {
                setError(view, error.message || 'Unable to load the mask overlay.');
            }
        }
    }
    async function requestSlice(view, index) {
        const previousIndex = view.index;
        view.index = clamp(index, 0, view.plane.count - 1);
        updatePosition(view);
        setError(view, '');
        const sequence = ++view.sequence;
        try {
            const selectedMask = mask.applied;
            const [pixels, maskPixels] = await Promise.all([
                fetchPixels(view.name, view.index),
                selectedMask ? fetchMaskPixels(view.name, view.index, selectedMask) : Promise.resolve(null),
            ]);
            if (sequence !== view.sequence) return;
            view.pixels = pixels;
            view.displayedIndex = view.index;
            view.maskPixels = mask.applied === selectedMask ? maskPixels : null;
            if (mask.applied !== selectedMask && mask.applied) loadMaskOverlay(view);
            if (!Number.isFinite(gray.width) || gray.width <= 0) autoGray();
            else renderView(view);
            if (locator) {
                if (view.name === 'axial') locator.z = view.index;
                else if (view.name === 'coronal') locator.y = view.index;
                else locator.x = view.index;
                refreshLocator();
            }
            const neighbor = view.index + 1;
            if (neighbor < view.plane.count) {
                fetchPixels(view.name, neighbor).catch(() => {});
                if (selectedMask) fetchMaskPixels(view.name, neighbor, selectedMask).catch(() => {});
            }
        } catch (error) {
            if (sequence === view.sequence) {
                view.index = view.displayedIndex ?? previousIndex;
                updatePosition(view);
                setError(view, error.message || 'Unable to display this slice.');
            }
        }
    }
    function setupView(name) {
        const panel = grid.querySelector(`[data-view="${name}"]`);
        const plane = config.planes[name];
        const canvas = panel.querySelector('[data-canvas]');
        canvas.width = plane.columns;
        canvas.height = plane.rows;
        const context = canvas.getContext('2d', {alpha: false});
        const view = {
            name, panel, plane, canvas, context,
            imageData: context.createImageData(plane.columns, plane.rows),
            stage: panel.querySelector('[data-stage]'), message: panel.querySelector('[data-message]'),
            range: panel.querySelector('[data-range]'), count: panel.querySelector('[data-count]'),
            position: panel.querySelector('[data-position]'), index: Math.floor(plane.count / 2),
            pixels: null, maskPixels: null, displayedIndex: null, sequence: 0,
            renderQueued: false, zoom: 1, panX: 0, panY: 0, drag: null,
        };
        views[name] = view;
        view.range.max = String(plane.count);
        view.range.addEventListener('input', () => requestSlice(view, Number(view.range.value) - 1));
        view.stage.addEventListener('wheel', event => {
            event.preventDefault();
            activeName = name;
            if (event.ctrlKey) {
                view.zoom = clamp(view.zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15), .5, 8);
                transformView(view);
            } else {
                requestSlice(view, view.index + (event.deltaY < 0 ? -1 : 1));
            }
        }, {passive: false});
        view.stage.addEventListener('keydown', event => {
            const direction = ['ArrowDown', 'ArrowRight'].includes(event.key) ? 1
                : ['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 0;
            if (direction) { event.preventDefault(); requestSlice(view, view.index + direction); }
        });
        view.stage.addEventListener('pointerdown', event => {
            if (event.button !== 0) return;
            if (event.shiftKey) {
                event.preventDefault();
                placeLocator(view, event);
                return;
            }
            activeName = name;
            view.stage.focus();
            view.drag = {x: event.clientX, y: event.clientY, panX: view.panX, panY: view.panY};
            view.stage.setPointerCapture(event.pointerId);
        });
        view.stage.addEventListener('pointermove', event => {
            if (!view.drag) return;
            view.panX = view.drag.panX + event.clientX - view.drag.x;
            view.panY = view.drag.panY + event.clientY - view.drag.y;
            transformView(view);
        });
        for (const type of ['pointerup', 'pointercancel']) view.stage.addEventListener(type, () => { view.drag = null; });
        fitView(view);
        requestSlice(view, view.index);
    }

    function shader(gl, type, source) {
        const part = gl.createShader(type);
        gl.shaderSource(part, source);
        gl.compileShader(part);
        if (!gl.getShaderParameter(part, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(part));
        return part;
    }
    function setup3D() {
        const gl = surfaceCanvas.getContext('webgl', {alpha: false, antialias: true});
        if (!gl) { surfaceMessage.textContent = '3D display needs WebGL in this browser.'; return; }
        const vertex = shader(gl, gl.VERTEX_SHADER, `
            attribute vec3 aPosition;
            attribute vec3 aNormal;
            uniform vec2 uRotation;
            uniform float uZoom;
            uniform float uAspect;
            varying float vLight;
            varying float vHighlight;
            void main() {
                float cy = cos(uRotation.x), sy = sin(uRotation.x);
                float cx = cos(uRotation.y), sx = sin(uRotation.y);
                vec3 p = vec3(cy*aPosition.x + sy*aPosition.z, aPosition.y,
                              -sy*aPosition.x + cy*aPosition.z);
                vec3 n = vec3(cy*aNormal.x + sy*aNormal.z, aNormal.y,
                              -sy*aNormal.x + cy*aNormal.z);
                p = vec3(p.x, cx*p.y - sx*p.z, sx*p.y + cx*p.z);
                n = vec3(n.x, cx*n.y - sx*n.z, sx*n.y + cx*n.z);
                vec3 litNormal = normalize(n);
                vLight = 0.30 + 0.66*abs(dot(litNormal, normalize(vec3(0.4, 0.7, 1.0))));
                vHighlight = 0.13*pow(abs(dot(litNormal, normalize(vec3(0.1, 0.2, 1.0)))), 16.0);
                gl_Position = vec4(p.x * 1.15 * uZoom / uAspect, p.y * 1.15 * uZoom, p.z * 0.85, 1.0);
            }
        `);
        const fragment = shader(gl, gl.FRAGMENT_SHADER, `
            precision mediump float;
            uniform vec3 uColor;
            varying float vLight;
            varying float vHighlight;
            void main() { gl_FragColor = vec4(uColor * vLight + vec3(vHighlight), 1.0); }
        `);
        const program = gl.createProgram();
        gl.attachShader(program, vertex);
        gl.attachShader(program, fragment);
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
        gl.useProgram(program);
        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        const pos = gl.getAttribLocation(program, 'aPosition');
        const normal = gl.getAttribLocation(program, 'aNormal');
        gl.enableVertexAttribArray(pos);
        gl.enableVertexAttribArray(normal);
        gl.vertexAttribPointer(pos, 3, gl.FLOAT, false, 24, 0);
        gl.vertexAttribPointer(normal, 3, gl.FLOAT, false, 24, 12);
        gl.enable(gl.DEPTH_TEST);
        gl.clearColor(.02, .035, .05, 1);
        surface.gl = gl;
        surface.program = program;
        surface.buffer = buffer;
        surfaceStage.addEventListener('pointerdown', event => {
            if (event.button !== 0) return;
            surface.drag = {x: event.clientX, y: event.clientY, yaw: surface.yaw, pitch: surface.pitch};
            surfaceStage.setPointerCapture(event.pointerId);
        });
        surfaceStage.addEventListener('pointermove', event => {
            if (!surface.drag) return;
            surface.yaw = surface.drag.yaw + (event.clientX - surface.drag.x) * .01;
            surface.pitch = clamp(surface.drag.pitch + (event.clientY - surface.drag.y) * .01, -Math.PI / 2, Math.PI / 2);
            render3D();
        });
        for (const type of ['pointerup', 'pointercancel']) surfaceStage.addEventListener(type, () => { surface.drag = null; });
        surfaceStage.addEventListener('wheel', event => {
            event.preventDefault();
            surface.zoom = clamp(surface.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12), .5, 3);
            render3D();
        }, {passive: false});
        render3D();
    }
    function render3D() {
        const gl = surface.gl;
        if (!gl || !surfaceStage.clientWidth || !surfaceStage.clientHeight) return;
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const width = Math.max(1, Math.round(surfaceStage.clientWidth * ratio));
        const height = Math.max(1, Math.round(surfaceStage.clientHeight * ratio));
        if (surfaceCanvas.width !== width || surfaceCanvas.height !== height) {
            surfaceCanvas.width = width; surfaceCanvas.height = height;
        }
        gl.viewport(0, 0, width, height);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        if (!surface.count) return;
        gl.useProgram(surface.program);
        gl.bindBuffer(gl.ARRAY_BUFFER, surface.buffer);
        gl.uniform2f(gl.getUniformLocation(surface.program, 'uRotation'), surface.yaw, surface.pitch);
        gl.uniform1f(gl.getUniformLocation(surface.program, 'uZoom'), surface.zoom);
        gl.uniform1f(gl.getUniformLocation(surface.program, 'uAspect'), width / height);
        gl.uniform3fv(gl.getUniformLocation(surface.program, 'uColor'), colors[surface.applied.preset]);
        gl.drawArrays(gl.TRIANGLES, 0, surface.count);
    }
    function thresholdParams() {
        const minimum = Number(minimumInput.value);
        const maximum = Number(maximumInput.value);
        if (!minimumInput.value || !maximumInput.value
                || !Number.isFinite(minimum) || !Number.isFinite(maximum) || minimum >= maximum
                || minimum < -100000 || maximum > 100000) {
            throw new Error('Enter a valid minimum and maximum threshold.');
        }
        return {minimum, maximum, preset: presetInput.value, smoothing: smoothingInput.value};
    }
    function surfaceUrl(base, params) { return `${base}?${new URLSearchParams(params)}`; }
    async function updateSurface() {
        if (surface.request) surface.request.abort();
        const sequence = ++surface.sequence;
        surface.applied = null;
        exportButton.disabled = true;
        exportObjButton.disabled = true;
        surfaceMessage.textContent = '';
        surface.count = 0;
        render3D();
        let params;
        try { params = thresholdParams(); }
        catch (error) { surfaceMessage.textContent = error.message; return; }
        if (!mask.applied || mask.applied.minimum !== params.minimum
                || mask.applied.maximum !== params.maximum || mask.applied.preset !== params.preset) return;
        const controller = new AbortController();
        surface.request = controller;
        const timeout = setTimeout(() => controller.abort(), 120000);
        try {
            const response = await fetch(surfaceUrl(config.surfaceUrl, params), {
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
            });
            if (response.redirected && response.url.includes('/login/')) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) throw new Error('Unable to build the 3D surface for this scan.');
            const bytes = await response.arrayBuffer();
            if (sequence !== surface.sequence) return;
            if (!bytes.byteLength) {
                surfaceMessage.textContent = 'No 3D surface matches this threshold.';
                return;
            }
            if (bytes.byteLength % 24) throw new Error('The 3D surface data is invalid.');
            surface.count = bytes.byteLength / 24;
            surface.applied = params;
            if (surface.gl) {
                surface.gl.bindBuffer(surface.gl.ARRAY_BUFFER, surface.buffer);
                surface.gl.bufferData(surface.gl.ARRAY_BUFFER, new Float32Array(bytes), surface.gl.STATIC_DRAW);
                render3D();
            } else {
                surfaceMessage.textContent = '3D display needs WebGL in this browser.';
            }
            exportButton.disabled = false;
            exportObjButton.disabled = false;
        } catch (error) {
            if (sequence === surface.sequence) {
                surfaceMessage.textContent = error.name === 'AbortError'
                    ? 'The 3D request timed out.' : error.message || 'Unable to display the 3D surface.';
            }
        } finally {
            clearTimeout(timeout);
            if (surface.request === controller) surface.request = null;
        }
    }

    function invalidateMask() {
        if (mask.request) mask.request.abort();
        mask.request = null;
        mask.sequence++;
        mask.applied = null;
        maskSummary.textContent = '';
        exportMaskButton.disabled = true;
        if (surface.request) surface.request.abort();
        surface.request = null;
        surface.sequence++;
        surface.applied = null;
        surface.count = 0;
        surfaceMessage.textContent = '';
        exportButton.disabled = true;
        exportObjButton.disabled = true;
        names.forEach(name => {
            const view = views[name];
            if (view) { view.maskPixels = null; renderView(view); }
        });
        render3D();
    }

    async function calculateMask() {
        invalidateMask();
        let params;
        try { params = thresholdParams(); }
        catch (error) { maskSummary.textContent = error.message; return; }
        const sequence = mask.sequence;
        const controller = new AbortController();
        mask.request = controller;
        const timeout = setTimeout(() => controller.abort(), 120000);
        try {
            const response = await fetch(surfaceUrl(config.maskUrl, params), {
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
            });
            if (response.redirected && response.url.includes('/login/')) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) throw new Error('Unable to calculate the binary mask.');
            const result = await response.json();
            if (sequence !== mask.sequence) return;
            mask.applied = {minimum: params.minimum, maximum: params.maximum, preset: params.preset};
            const count = new Intl.NumberFormat().format(result.selectedVoxels);
            const total = new Intl.NumberFormat().format(result.totalVoxels);
            const percent = (100 * result.selectedVoxels / result.totalVoxels).toFixed(2);
            maskSummary.textContent = `Binary mask: ${count} / ${total} voxels (${percent}%).`;
            exportMaskButton.disabled = false;
            if (result.selectedVoxels) {
                names.forEach(name => loadMaskOverlay(views[name]));
                updateSurface();
            }
            else surfaceMessage.textContent = 'No voxels match this threshold.';
        } catch (error) {
            if (sequence === mask.sequence) maskSummary.textContent = error.name === 'AbortError'
                ? 'Mask calculation timed out.' : error.message || 'Unable to calculate the binary mask.';
        } finally {
            clearTimeout(timeout);
            if (mask.request === controller) mask.request = null;
        }
    }

    for (const name of names) setupView(name);
    try { setup3D(); }
    catch (error) { surfaceMessage.textContent = `3D display failed: ${error.message}`; }
    syncGrayInputs();
    calculateMask();
    presetInput.addEventListener('change', () => {
        clearTimeout(thresholdTimer);
        if (presets[presetInput.value]) {
            [minimumInput.value, maximumInput.value] = presets[presetInput.value];
        }
        calculateMask();
    });
    smoothingInput.addEventListener('change', () => { if (mask.applied) updateSurface(); });
    for (const input of [minimumInput, maximumInput]) input.addEventListener('input', () => {
        presetInput.value = 'custom';
        invalidateMask();
        clearTimeout(thresholdTimer);
        thresholdTimer = setTimeout(calculateMask, 350);
    });
    document.getElementById('apply-threshold').addEventListener('click', () => {
        clearTimeout(thresholdTimer);
        calculateMask();
    });
    showMaskInput.addEventListener('change', renderAll);
    clearLocatorButton.addEventListener('click', () => { locator = null; refreshLocator(); });
    maskOpacityInput.addEventListener('input', () => {
        document.getElementById('mask-opacity-value').textContent = `${maskOpacityInput.value}%`;
        renderAll();
    });
    exportMaskButton.addEventListener('click', () => {
        if (mask.applied && !exportMaskButton.disabled) {
            window.location.assign(surfaceUrl(config.maskUrl, {...mask.applied, download: 1}));
        }
    });
    exportButton.addEventListener('click', () => {
        if (surface.applied && !exportButton.disabled) window.location.assign(surfaceUrl(config.stlUrl, surface.applied));
    });
    exportObjButton.addEventListener('click', () => {
        if (surface.applied && !exportObjButton.disabled) window.location.assign(surfaceUrl(config.objUrl, surface.applied));
    });
    centerInput.addEventListener('input', () => {
        const value = Number(centerInput.value);
        if (centerInput.value && Number.isFinite(value)) { gray.center = value; renderAll(); }
    });
    widthInput.addEventListener('input', () => {
        const value = Number(widthInput.value);
        if (widthInput.value && Number.isFinite(value) && value >= 1) { gray.width = value; renderAll(); }
    });
    brightnessInput.addEventListener('input', () => {
        gray.brightness = Number(brightnessInput.value);
        document.getElementById('brightness-value').textContent = String(gray.brightness);
        renderAll();
    });
    contrastInput.addEventListener('input', () => {
        gray.contrast = Number(contrastInput.value) / 100;
        document.getElementById('contrast-value').textContent = `${contrastInput.value}%`;
        renderAll();
    });
    document.getElementById('auto-window').addEventListener('click', autoGray);
    document.getElementById('invert-view').addEventListener('click', () => { gray.invert = !gray.invert; renderAll(); });

    grid.addEventListener('click', event => {
        const button = event.target.closest('[data-action]');
        if (!button) return;
        const panel = button.closest('[data-view]');
        const name = panel.dataset.view;
        const action = button.dataset.action;
        if (action === 'minimize') {
            if (panel.classList.contains('is-maximized')) {
                panel.classList.remove('is-maximized');
                grid.classList.remove('has-maximized');
                panel.querySelector('[data-action="maximize"]').setAttribute('aria-label', `Maximize ${name}`);
            }
            panel.classList.toggle('is-minimized');
            const minimized = panel.classList.contains('is-minimized');
            button.textContent = minimized ? '+' : '−';
            button.setAttribute('aria-label', `${minimized ? 'Restore' : 'Minimize'} ${name}`);
        } else if (action === 'maximize') {
            const maximized = panel.classList.contains('is-maximized');
            grid.querySelectorAll('.is-maximized').forEach(item => {
                item.classList.remove('is-maximized');
                item.querySelector('[data-action="maximize"]').setAttribute('aria-label', `Maximize ${item.dataset.view}`);
            });
            grid.classList.toggle('has-maximized', !maximized);
            if (!maximized) {
                panel.classList.remove('is-minimized');
                const minimize = panel.querySelector('[data-action="minimize"]');
                minimize.textContent = '−';
                minimize.setAttribute('aria-label', `Minimize ${name}`);
                panel.classList.add('is-maximized');
            }
            button.setAttribute('aria-label', `${maximized ? 'Maximize' : 'Restore'} ${name}`);
        } else if (action === 'reset') {
            if (name === 'surface') {
                surface.yaw = .55; surface.pitch = -.35; surface.zoom = 1; render3D();
            } else {
                const view = views[name];
                view.zoom = 1; view.panX = 0; view.panY = 0; transformView(view);
            }
        }
        requestAnimationFrame(() => {
            names.forEach(plane => fitView(views[plane]));
            render3D();
        });
    });
    if ('ResizeObserver' in window) {
        const observer = new ResizeObserver(() => {
            names.forEach(name => fitView(views[name]));
            render3D();
        });
        grid.querySelectorAll('[data-stage]').forEach(stage => observer.observe(stage));
    } else {
        window.addEventListener('resize', () => {
            names.forEach(name => fitView(views[name])); render3D();
        });
    }
})();
