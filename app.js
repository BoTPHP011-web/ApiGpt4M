/* ==========================================================================
 * AsiyaOS — app.js
 *
 * Everything here is vanilla JS, no framework. Sections:
 *   1. Utilities & global state
 *   2. Window Manager (drag / resize / focus / minimize / maximize / close)
 *   3. Taskbar, Start menu, Context menu, Clock, fake system tray graph
 *   4. Virtual file store (in-memory "disk" for uploaded files)
 *   5. Apps: File manager, DOS player (js-dos), System info, Settings, Terminal
 *   6. Boot sequence
 * ========================================================================== */

(() => {
  'use strict';

  /* ------------------------------------------------------------------ *
   * 1. Utilities & global state
   * ------------------------------------------------------------------ */
  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const desktop      = $('#desktop');
  const windowLayer  = $('#window-layer');
  const taskbarApps  = $('#taskbar-apps');

  let zCounter = 10;
  let winCounter = 0;
  const openWindows = new Map(); // id -> {el, appId, taskbarEl}

  // In-memory virtual filesystem: uploaded .exe/.com/.bat/.zip live only for this tab.
  const virtualFiles = []; // {id, name, ext, size, data: ArrayBuffer}
  let fileIdCounter = 0;

  function toast(message, icon = 'fa-circle-check') {
    let layer = $('#toast-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.id = 'toast-layer';
      desktop.appendChild(layer);
    }
    const el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = `<i class="fa-solid ${icon}"></i> ${message}`;
    layer.appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  /* App registry: how each app appears in Start menu / desktop and what it does when opened */
  const APP_REGISTRY = {
    files:    { title: 'Файловый менеджер', icon: 'fa-regular fa-folder-open', open: openFilesApp },
    dos:      { title: 'DOS-плеер',         icon: 'fa-solid fa-square-terminal', open: (opts) => openDosApp(opts) },
    linuxvm:  { title: 'Лёгкий Linux (v86)', icon: 'fa-brands fa-linux', open: (opts) => openLinuxVmApp(opts) },
    sysinfo:  { title: 'О системе',         icon: 'fa-solid fa-microchip', singleInstance: true, open: openSysInfoApp },
    settings: { title: 'Параметры',         icon: 'fa-solid fa-sliders', open: openSettingsApp },
    terminal: { title: 'Терминал',          icon: 'fa-solid fa-terminal', open: openTerminalApp },
  };

  /* ------------------------------------------------------------------ *
   * 2. Window Manager
   * ------------------------------------------------------------------ */
  function createWindow({ appId, title, icon, width = 560, height = 420, contentEl }) {
    winCounter += 1;
    const id = 'win-' + winCounter;

    const winEl = document.createElement('div');
    winEl.className = 'window';
    winEl.id = id;
    winEl.style.width = width + 'px';
    winEl.style.height = height + 'px';

    // Cascade new windows so they don't stack exactly on top of each other
    const openCount = openWindows.size;
    const offset = (openCount % 6) * 26;
    winEl.style.left = (90 + offset) + 'px';
    winEl.style.top  = (60 + offset) + 'px';

    winEl.innerHTML = `
      <div class="window-titlebar" data-role="drag-handle">
        <i class="win-icon ${icon}"></i>
        <span class="window-title">${title}</span>
        <div class="win-controls">
          <button class="win-min" title="Свернуть"><i class="fa-solid fa-minus"></i></button>
          <button class="win-max" title="Развернуть"><i class="fa-regular fa-square"></i></button>
          <button class="win-close" title="Закрыть"><i class="fa-solid fa-xmark"></i></button>
        </div>
      </div>
      <div class="window-body"></div>
      <div class="resize-handle"></div>
    `;
    $('.window-body', winEl).appendChild(contentEl);
    windowLayer.appendChild(winEl);

    // Taskbar entry
    const tbEl = document.createElement('button');
    tbEl.className = 'taskbar-app';
    tbEl.innerHTML = `<i class="${icon}"></i><span>${title}</span>`;
    tbEl.addEventListener('click', () => toggleMinimize(id));
    taskbarApps.appendChild(tbEl);

    openWindows.set(id, { el: winEl, appId, taskbarEl: tbEl, minimized: false, maximized: false });

    focusWindow(id);
    bindWindowInteractions(id);
    return id;
  }

  function focusWindow(id) {
    const win = openWindows.get(id);
    if (!win) return;
    zCounter += 1;
    win.el.style.zIndex = zCounter;
    $$('.window').forEach(w => w.classList.remove('is-focused'));
    win.el.classList.add('is-focused');
    $$('.taskbar-app').forEach(t => t.classList.remove('active'));
    win.taskbarEl.classList.add('active');
    win.el.classList.remove('is-minimized');
    win.minimized = false;
  }

  function toggleMinimize(id) {
    const win = openWindows.get(id);
    if (!win) return;
    const isFocused = win.el.classList.contains('is-focused') && !win.minimized;
    if (isFocused) {
      win.el.classList.add('is-minimized');
      win.minimized = true;
      win.taskbarEl.classList.remove('active');
    } else {
      focusWindow(id);
    }
  }

  function toggleMaximize(id) {
    const win = openWindows.get(id);
    if (!win) return;
    win.maximized = !win.maximized;
    win.el.classList.toggle('is-maximized', win.maximized);
  }

  function closeWindow(id) {
    const win = openWindows.get(id);
    if (!win) return;
    win.el.remove();
    win.taskbarEl.remove();
    openWindows.delete(id);
    // let apps clean up (e.g. stop a running js-dos instance)
    if (win.onClose) win.onClose();
  }

  function bindWindowInteractions(id) {
    const win = openWindows.get(id);
    const winEl = win.el;

    winEl.addEventListener('mousedown', () => focusWindow(id));

    $('.win-close', winEl).addEventListener('click', (e) => { e.stopPropagation(); closeWindow(id); });
    $('.win-min', winEl).addEventListener('click', (e) => { e.stopPropagation(); toggleMinimize(id); });
    $('.win-max', winEl).addEventListener('click', (e) => { e.stopPropagation(); toggleMaximize(id); });

    // Double-click titlebar to maximize/restore
    const titlebar = $('.window-titlebar', winEl);
    titlebar.addEventListener('dblclick', () => toggleMaximize(id));

    // Dragging
    let dragging = false, startX, startY, startLeft, startTop;
    titlebar.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.win-controls')) return;
      if (win.maximized) return;
      dragging = true;
      startX = e.clientX; startY = e.clientY;
      const rect = winEl.getBoundingClientRect();
      startLeft = rect.left; startTop = rect.top;
      titlebar.setPointerCapture(e.pointerId);
    });
    titlebar.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX, dy = e.clientY - startY;
      const taskbarH = 58;
      let newLeft = startLeft + dx;
      let newTop  = Math.max(0, Math.min(startTop + dy, window.innerHeight - taskbarH - 40));
      winEl.style.left = newLeft + 'px';
      winEl.style.top  = newTop + 'px';
    });
    titlebar.addEventListener('pointerup', (e) => { dragging = false; try { titlebar.releasePointerCapture(e.pointerId); } catch(_){} });

    // Resizing
    const handle = $('.resize-handle', winEl);
    let resizing = false, rStartX, rStartY, rStartW, rStartH;
    handle.addEventListener('pointerdown', (e) => {
      if (win.maximized) return;
      resizing = true;
      rStartX = e.clientX; rStartY = e.clientY;
      const rect = winEl.getBoundingClientRect();
      rStartW = rect.width; rStartH = rect.height;
      handle.setPointerCapture(e.pointerId);
      e.stopPropagation();
    });
    handle.addEventListener('pointermove', (e) => {
      if (!resizing) return;
      const dw = e.clientX - rStartX, dh = e.clientY - rStartY;
      winEl.style.width  = Math.max(320, rStartW + dw) + 'px';
      winEl.style.height = Math.max(220, rStartH + dh) + 'px';
    });
    handle.addEventListener('pointerup', (e) => { resizing = false; try { handle.releasePointerCapture(e.pointerId); } catch(_){} });
  }

  /** Open (or focus, if already open and single-instance) an app by id from the registry. */
  const singleInstanceOpen = {}; // appId -> windowId, for apps that shouldn't multi-open
  function launchApp(appId, opts = {}) {
    const def = APP_REGISTRY[appId];
    if (!def) return;
    if (def.singleInstance && singleInstanceOpen[appId] && openWindows.has(singleInstanceOpen[appId])) {
      focusWindow(singleInstanceOpen[appId]);
      return;
    }
    const winId = def.open(opts);
    if (def.singleInstance) singleInstanceOpen[appId] = winId;
    closeAllMenus();
  }

  /* ------------------------------------------------------------------ *
   * 3. Taskbar, Start menu, Context menu, Clock, fake system graph
   * ------------------------------------------------------------------ */
  const startMenu    = $('#start-menu');
  const contextMenu   = $('#context-menu');
  const startButton   = $('#start-button');

  function closeAllMenus() {
    startMenu.hidden = true;
    contextMenu.hidden = true;
    startButton.classList.remove('active');
  }

  startButton.addEventListener('click', (e) => {
    e.stopPropagation();
    const willOpen = startMenu.hidden;
    closeAllMenus();
    if (willOpen) {
      startMenu.hidden = false;
      startButton.classList.add('active');
      $('#start-search').value = '';
      filterStartGrid('');
      $('#start-search').focus();
    }
  });

  document.addEventListener('click', () => closeAllMenus());
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAllMenus(); });

  // Desktop right-click context menu
  desktop.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    closeAllMenus();
    contextMenu.hidden = false;
    const maxLeft = window.innerWidth - 240;
    const maxTop  = window.innerHeight - 220;
    contextMenu.style.left = Math.min(e.clientX, maxLeft) + 'px';
    contextMenu.style.top  = Math.min(e.clientY, maxTop) + 'px';
  });
  contextMenu.addEventListener('click', (e) => e.stopPropagation());
  startMenu.addEventListener('click', (e) => e.stopPropagation());

  $$('#context-menu button').forEach(btn => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      if (action === 'wallpaper') cycleWallpaper();
      if (action === 'fullscreen') toggleFullscreen();
      if (action === 'about') launchApp('sysinfo');
      if (action === 'arrange') toast('Значки уже упорядочены', 'fa-arrow-up-wide-short');
      closeAllMenus();
    });
  });

  // Build start menu grid from the app registry
  const startGrid = $('#start-grid');
  Object.entries(APP_REGISTRY).forEach(([id, def]) => {
    const btn = document.createElement('button');
    btn.className = 'start-app';
    btn.dataset.appId = id;
    btn.innerHTML = `<i class="${def.icon}"></i><span>${def.title}</span>`;
    btn.addEventListener('click', () => launchApp(id));
    startGrid.appendChild(btn);
  });
  function filterStartGrid(query) {
    const q = query.trim().toLowerCase();
    $$('.start-app', startGrid).forEach(btn => {
      const match = btn.textContent.toLowerCase().includes(q);
      btn.classList.toggle('hidden-by-search', !match);
    });
  }
  $('#start-search').addEventListener('input', (e) => filterStartGrid(e.target.value));

  $('#start-fullscreen').addEventListener('click', toggleFullscreen);
  $('#start-about').addEventListener('click', () => launchApp('sysinfo'));

  // Desktop icons (double click opens, matching typical desktop-icon conventions;
  // single click also opens since this is a simplified touch-friendly shell)
  $$('.desktop-icon').forEach(icon => {
    icon.addEventListener('click', () => launchApp(icon.dataset.app));
  });

  // Fullscreen toggle, wired to taskbar, start menu, and context menu
  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => toast('Полноэкранный режим недоступен', 'fa-triangle-exclamation'));
    } else {
      document.exitFullscreen();
    }
  }
  $('#tray-fullscreen').addEventListener('click', toggleFullscreen);

  // Wallpaper cycling: shifts the animated gradient's hue via a CSS filter
  let wallpaperIndex = 0;
  const wallpaperFilters = [
    'none',
    'hue-rotate(35deg)',
    'hue-rotate(-40deg) saturate(1.15)',
    'hue-rotate(160deg)',
  ];
  function cycleWallpaper() {
    wallpaperIndex = (wallpaperIndex + 1) % wallpaperFilters.length;
    $('#wallpaper').style.filter = wallpaperFilters[wallpaperIndex];
    toast('Обои изменены', 'fa-image');
  }

  // Live clock
  function tickClock() {
    const now = new Date();
    $('#clock-time').textContent = now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    $('#clock-date').textContent = now.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
  }
  tickClock();
  setInterval(tickClock, 1000 * 10);

  // Fake but plausible-looking CPU/RAM sparkline in the system tray.
  // This is a cosmetic system-profile widget, not a real hardware reading.
  const sparkCanvas = $('#tray-sparkline');
  const sparkCtx = sparkCanvas.getContext('2d');
  const sparkHistory = { cpu: new Array(30).fill(12), ram: new Array(30).fill(30) };
  let cpuTarget = 15, ramTarget = 32;

  function stepTowards(current, target, maxStep) {
    const d = target - current;
    if (Math.abs(d) < maxStep) return target;
    return current + Math.sign(d) * maxStep;
  }

  function updateFakeStats() {
    if (Math.random() < 0.15) cpuTarget = 8 + Math.random() * 70;
    if (Math.random() < 0.1)  ramTarget = 25 + Math.random() * 45;
    const lastCpu = sparkHistory.cpu[sparkHistory.cpu.length - 1];
    const lastRam = sparkHistory.ram[sparkHistory.ram.length - 1];
    const nextCpu = stepTowards(lastCpu, cpuTarget, 8);
    const nextRam = stepTowards(lastRam, ramTarget, 4);
    sparkHistory.cpu.push(nextCpu); sparkHistory.cpu.shift();
    sparkHistory.ram.push(nextRam); sparkHistory.ram.shift();

    $('#tray-cpu').textContent = `CPU ${Math.round(nextCpu)}%`;
    $('#tray-ram').textContent = `RAM ${Math.round(nextRam)}%`;
    const siRam = $('#si-ram');
    if (siRam) siRam.textContent = `${(nextRam / 100 * 16).toFixed(1)} / 16 ГБ (${Math.round(nextRam)}%)`;

    drawSparkline();
  }

  function drawSparkline() {
    const w = sparkCanvas.width, h = sparkCanvas.height;
    sparkCtx.clearRect(0, 0, w, h);
    const plot = (series, color) => {
      sparkCtx.beginPath();
      series.forEach((v, i) => {
        const x = (i / (series.length - 1)) * w;
        const y = h - (v / 100) * h;
        if (i === 0) sparkCtx.moveTo(x, y); else sparkCtx.lineTo(x, y);
      });
      sparkCtx.strokeStyle = color;
      sparkCtx.lineWidth = 1.5;
      sparkCtx.stroke();
    };
    plot(sparkHistory.ram, 'rgba(141,161,227,0.9)');
    plot(sparkHistory.cpu, 'rgba(227,163,99,0.95)');
  }
  updateFakeStats();
  setInterval(updateFakeStats, 1400);

  // Fake disk usage + uptime, shown in the System Info app
  const bootTime = Date.now();
  function tickUptime() {
    const el = $('#si-uptime');
    if (!el) return;
    const s = Math.floor((Date.now() - bootTime) / 1000);
    const hh = String(Math.floor(s / 3600)).padStart(2, '0');
    const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    el.textContent = `${hh}:${mm}:${ss}`;
    const disk = $('#si-disk');
    if (disk) {
      const usedGb = (12.4 + virtualFiles.reduce((a, f) => a + f.size, 0) / (1024 ** 3)).toFixed(1);
      disk.textContent = `${usedGb} / 128 ГБ занято (виртуальный диск)`;
    }
  }
  setInterval(tickUptime, 1000);
  tickUptime();

  /* ------------------------------------------------------------------ *
   * 4. Virtual file store
   * ------------------------------------------------------------------ */
  function addVirtualFile(file, arrayBuffer) {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    fileIdCounter += 1;
    const record = { id: 'f' + fileIdCounter, name: file.name, ext, size: file.size, data: arrayBuffer };
    virtualFiles.push(record);
    refreshFileManagers();
    refreshDosSelectors();
    return record;
  }

  function removeAllVirtualFiles() {
    virtualFiles.length = 0;
    refreshFileManagers();
    refreshDosSelectors();
  }

  const fileManagerRefreshers = new Set();
  const dosSelectorRefreshers = new Set();
  function refreshFileManagers() { fileManagerRefreshers.forEach(fn => fn()); }
  function refreshDosSelectors() { dosSelectorRefreshers.forEach(fn => fn()); }

  /* ------------------------------------------------------------------ *
   * 5a. App: File manager
   * ------------------------------------------------------------------ */
  function openFilesApp() {
    const tpl = $('#tpl-files');
    const content = tpl.content.cloneNode(true);
    const root = content.querySelector('.app-files');

    const uploadBtn   = $('.fm-upload-btn', root);
    const uploadInput = $('.fm-upload-input', root);
    const list        = $('.files-list', root);

    uploadBtn.addEventListener('click', () => uploadInput.click());
    uploadInput.addEventListener('change', async () => {
      for (const file of uploadInput.files) {
        const allowed = ['exe', 'com', 'bat', 'zip'];
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        if (!allowed.includes(ext)) {
          toast(`Формат .${ext} не поддерживается`, 'fa-triangle-exclamation');
          continue;
        }
        const buf = await file.arrayBuffer();
        addVirtualFile(file, buf);
        toast(`Загружено: ${file.name}`, 'fa-file-arrow-up');
      }
      uploadInput.value = '';
    });

    function render() {
      list.innerHTML = '';
      root.classList.toggle('is-empty', virtualFiles.length === 0);
      virtualFiles.forEach(f => {
        const row = document.createElement('div');
        row.className = 'file-row';
        const iconClass = f.ext === 'zip' ? 'fa-file-zipper' : 'fa-file-code';
        row.innerHTML = `
          <span class="file-name"><i class="fa-solid ${iconClass}"></i>${f.name}</span>
          <span class="file-type">.${f.ext}</span>
          <span class="file-size">${formatBytes(f.size)}</span>
          <button class="file-run-btn">Запустить</button>
        `;
        $('.file-run-btn', row).addEventListener('click', () => {
          if (f.ext === 'zip') {
            toast('Откройте .zip через DOS-плеер: он смонтирует архив как каталог', 'fa-circle-info');
          }
          launchApp('dos', { fileId: f.id });
        });
        list.appendChild(row);
      });
    }

    render();
    fileManagerRefreshers.add(render);

    const winId = createWindow({
      appId: 'files', title: 'Файловый менеджер', icon: 'fa-regular fa-folder-open',
      width: 620, height: 440, contentEl: root,
    });
    openWindows.get(winId).onClose = () => fileManagerRefreshers.delete(render);
    return winId;
  }

  /* ------------------------------------------------------------------ *
   * 5b. App: DOS player (js-dos / DOSBox WebAssembly)
   * ------------------------------------------------------------------ */
  function sanitizeDosName(name) {
    // Classic DOSBox autoexec is happiest with 8.3 uppercase names.
    const dot = name.lastIndexOf('.');
    let base = dot > -1 ? name.slice(0, dot) : name;
    let ext  = dot > -1 ? name.slice(dot + 1) : '';
    base = base.replace(/[^A-Za-z0-9_]/g, '').slice(0, 8).toUpperCase() || 'RUNME';
    ext  = ext.replace(/[^A-Za-z0-9_]/g, '').slice(0, 3).toUpperCase() || 'EXE';
    return `${base}.${ext}`;
  }

  function openDosApp(opts = {}) {
    const tpl = $('#tpl-dos');
    const content = tpl.content.cloneNode(true);
    const root = content.querySelector('.app-dos');
    const picker = $('.dos-picker', root);
    const select = $('.dos-select', root);
    const runBtn = $('.dos-run-btn', root);
    const stage  = $('.dos-stage', root);

    let dosInstance = null;

    function renderOptions() {
      const runnable = virtualFiles.filter(f => ['exe', 'com', 'bat', 'zip'].includes(f.ext));
      select.innerHTML = runnable.length
        ? runnable.map(f => `<option value="${f.id}">${f.name} (${formatBytes(f.size)})</option>`).join('')
        : `<option disabled selected>Нет загруженных файлов</option>`;
      runBtn.disabled = runnable.length === 0;
      if (opts.fileId) select.value = opts.fileId;
    }
    renderOptions();
    dosSelectorRefreshers.add(renderOptions);

    async function runSelected() {
      const record = virtualFiles.find(f => f.id === select.value);
      if (!record) { toast('Файл не найден', 'fa-triangle-exclamation'); return; }
      picker.hidden = true;
      stage.hidden = false;
      stage.innerHTML = '';

      const dosName = sanitizeDosName(record.name);
      const isZip = record.ext === 'zip';

      try {
        if (isZip) {
          // js-dos accepts a raw .jsdos/zip bundle directly via a Blob URL.
          const blob = new Blob([record.data], { type: 'application/zip' });
          const url = URL.createObjectURL(blob);
          dosInstance = window.Dos(stage, {
            url,
            autoStart: true,
            theme: 'dark',
            onEvent: (event) => { if (event === 'emu-ready') URL.revokeObjectURL(url); },
          });
        } else {
          // Single .exe/.com/.bat: seed the virtual C: drive with the file
          // and auto-run it through a generated autoexec section.
          dosInstance = window.Dos(stage, {
            dosboxConf: `
[autoexec]
mount c .
c:
${dosName}
`,
            initFs: [{ path: dosName, contents: new Uint8Array(record.data) }],
            autoStart: true,
            theme: 'dark',
          });
        }
        toast(`Запуск: ${record.name}`, 'fa-play');
      } catch (err) {
        console.error(err);
        toast('Не удалось запустить файл в эмуляторе', 'fa-triangle-exclamation');
        picker.hidden = false;
        stage.hidden = true;
      }
    }

    runBtn.addEventListener('click', runSelected);

    const winId = createWindow({
      appId: 'dos', title: 'DOS-плеер · DOSBox (WASM)', icon: 'fa-solid fa-square-terminal',
      width: 720, height: 500, contentEl: root,
    });
    openWindows.get(winId).onClose = () => {
      dosSelectorRefreshers.delete(renderOptions);
      if (dosInstance && dosInstance.stop) dosInstance.stop().catch(() => {});
    };
    return winId;
  }

  /* ------------------------------------------------------------------ *
   * 5b-2. App: Lightweight Linux VM (v86 — full x86 PC emulator)
   *
   * Unlike the DOS player, this boots a *whole disk image* (.img/.iso),
   * not a single executable. It ships with launch buttons for a couple
   * of freely-distributable, genuinely tiny systems (KolibriOS, TinyCore
   * Linux, FreeDOS), streamed from the v86 project's own public demo
   * host, plus a way to load a disk image the user supplies themselves.
   * ------------------------------------------------------------------ */
  function openLinuxVmApp() {
    const tpl = $('#tpl-linuxvm');
    const content = tpl.content.cloneNode(true);
    const root = content.querySelector('.app-linuxvm');
    const picker = $('.vm-picker', root);
    const stage  = $('.vm-stage', root);
    const screenContainer = $('.vm-screen-container', root);
    const uploadBtn = $('.vm-upload-btn', root);
    const uploadInput = $('.vm-upload-input', root);

    let emulator = null;

    function startVm({ name, kind, url, buffer }) {
      if (typeof window.V86 !== 'function') {
        toast('Библиотека v86 ещё не загрузилась, попробуйте ещё раз через пару секунд', 'fa-triangle-exclamation');
        return;
      }
      picker.hidden = true;
      stage.hidden = false;
      const loading = $('.vm-loading', stage);
      const loadingText = $('.vm-loading-text', stage);
      loading.style.display = 'flex';
      loadingText.textContent = `Загрузка: ${name}…`;

      // v86 expects screen_container to contain exactly a text div then a
      // canvas (it wires them up itself) — do not replace these nodes,
      // only pass the container element down.
      const diskConfig = {};
      const source = buffer ? { buffer } : { url, async: true };
      diskConfig[kind] = source; // kind is 'hda' or 'cdrom'

      try {
        emulator = new window.V86({
          screen_container: screenContainer,
          bios:     { url: 'https://cdn.jsdelivr.net/npm/v86@0.5.44/bios/seabios.bin' },
          vga_bios: { url: 'https://cdn.jsdelivr.net/npm/v86@0.5.44/bios/vgabios.bin' },
          memory_size: 128 * 1024 * 1024,
          vga_memory_size: 8 * 1024 * 1024,
          autostart: true,
          ...diskConfig,
        });
        emulator.add_listener('emulator-loaded', () => { loading.style.display = 'none'; });
        toast(`Запуск: ${name}`, 'fa-play');
      } catch (err) {
        console.error(err);
        toast('Не удалось запустить виртуальную машину', 'fa-triangle-exclamation');
        picker.hidden = false;
        stage.hidden = true;
      }
    }

    $$('.vm-preset', root).forEach(btn => {
      btn.addEventListener('click', () => startVm({
        name: btn.dataset.name, kind: btn.dataset.kind, url: btn.dataset.url,
      }));
    });

    uploadBtn.addEventListener('click', () => uploadInput.click());
    uploadInput.addEventListener('change', async () => {
      const file = uploadInput.files[0];
      if (!file) return;
      const ext = (file.name.split('.').pop() || '').toLowerCase();
      const kind = ext === 'iso' ? 'cdrom' : 'hda';
      const buffer = await file.arrayBuffer();
      startVm({ name: file.name, kind, buffer });
    });

    const winId = createWindow({
      appId: 'linuxvm', title: 'Лёгкий Linux · v86', icon: 'fa-brands fa-linux',
      width: 760, height: 560, contentEl: root,
    });
    openWindows.get(winId).onClose = () => {
      if (emulator && emulator.stop) { try { emulator.stop(); } catch (_) {} }
      if (emulator && emulator.destroy) { try { emulator.destroy(); } catch (_) {} }
    };
    return winId;
  }

  /* ------------------------------------------------------------------ *
   * 5c. App: System info
   * ------------------------------------------------------------------ */
  function openSysInfoApp() {
    const tpl = $('#tpl-sysinfo');
    const content = tpl.content.cloneNode(true);
    const root = content.querySelector('.app-sysinfo');
    const winId = createWindow({
      appId: 'sysinfo', title: 'О системе — AsiyaOS', icon: 'fa-solid fa-microchip',
      width: 500, height: 480, contentEl: root,
    });
    tickUptime();
    return winId;
  }

  /* ------------------------------------------------------------------ *
   * 5d. App: Settings
   * ------------------------------------------------------------------ */
  function openSettingsApp() {
    const tpl = $('#tpl-settings');
    const content = tpl.content.cloneNode(true);
    const root = content.querySelector('.app-settings');

    const themeBtn = $('.settings-theme-toggle', root);
    function syncThemeBtn() {
      const isLight = document.documentElement.dataset.theme === 'light';
      themeBtn.innerHTML = isLight
        ? '<i class="fa-solid fa-sun"></i>Светлая'
        : '<i class="fa-solid fa-moon"></i>Тёмная';
    }
    syncThemeBtn();
    themeBtn.addEventListener('click', () => {
      const isLight = document.documentElement.dataset.theme === 'light';
      document.documentElement.dataset.theme = isLight ? 'dark' : 'light';
      syncThemeBtn();
    });

    $('.settings-wallpaper-btn', root).addEventListener('click', cycleWallpaper);
    $('.settings-fullscreen-btn', root).addEventListener('click', toggleFullscreen);
    $('.settings-reset-btn', root).addEventListener('click', () => {
      removeAllVirtualFiles();
      toast('Виртуальные файлы очищены', 'fa-trash');
    });

    return createWindow({
      appId: 'settings', title: 'Параметры', icon: 'fa-solid fa-sliders',
      width: 440, height: 400, contentEl: root,
    });
  }

  /* ------------------------------------------------------------------ *
   * 5e. App: Terminal (a small set of decorative/informational commands)
   * ------------------------------------------------------------------ */
  function openTerminalApp() {
    const tpl = $('#tpl-terminal');
    const content = tpl.content.cloneNode(true);
    const root = content.querySelector('.app-terminal');
    const output = $('.term-output', root);
    const input  = $('.term-input', root);

    function print(text, cls) {
      const line = document.createElement('div');
      if (cls) line.className = cls;
      line.textContent = text;
      output.appendChild(line);
      output.scrollTop = output.scrollHeight;
    }

    print('AsiyaOS 1.0 «Рассвет» — псевдо-терминал. Введите help для списка команд.');

    const commands = {
      help: () => 'Доступно: help, neofetch, ls, date, whoami, echo <текст>, clear',
      neofetch: () => [
        'guest@asiyaos',
        '--------------',
        'OS: AsiyaOS 1.0 «Рассвет»',
        'Kernel: asiya-6.9.2-dawn',
        'Shell: Asiya Shell (веб)',
        'CPU: Asiya Vela M2 @ 3.40GHz',
        'Emulation: js-dos (DOSBox / DOSBox-X, WASM)',
      ].join('\n'),
      ls: () => virtualFiles.length ? virtualFiles.map(f => f.name).join('  ') : '(виртуальный диск пуст)',
      date: () => new Date().toString(),
      whoami: () => 'guest',
      clear: () => { output.innerHTML = ''; return null; },
    };

    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const raw = input.value;
      input.value = '';
      if (!raw.trim()) return;
      print('guest@asiyaos:~$ ' + raw, 'term-line-cmd');
      const [cmd, ...rest] = raw.trim().split(/\s+/);
      if (cmd === 'echo') { print(rest.join(' ')); return; }
      const handler = commands[cmd];
      if (!handler) { print(`команда не найдена: ${cmd}`); return; }
      const res = handler();
      if (res !== null && res !== undefined) print(res);
    });

    const winId = createWindow({
      appId: 'terminal', title: 'Терминал', icon: 'fa-solid fa-terminal',
      width: 520, height: 360, contentEl: root,
    });
    setTimeout(() => input.focus(), 50);
    return winId;
  }

  /* ------------------------------------------------------------------ *
   * 6. Boot sequence — small delay + fade so the shell feels like it's
   *    initializing, without blocking interaction for long.
   * ------------------------------------------------------------------ */
  function boot() {
    desktop.style.opacity = '0';
    desktop.style.transition = 'opacity 0.5s ease';
    requestAnimationFrame(() => { desktop.style.opacity = '1'; });
    setTimeout(() => toast('AsiyaOS готова к работе', 'fa-feather-pointed'), 700);
  }
  boot();

})();
