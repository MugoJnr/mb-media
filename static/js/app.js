// ============================================
// MB MEDIA — app.js
// ============================================

// ---------- Theme ----------
const themeToggle = document.getElementById('themeToggle');
const root = document.documentElement;
function applyTheme(t) {
  root.setAttribute('data-theme', t);
  if (themeToggle) themeToggle.textContent = t === 'dark' ? '🌙' : '☀️';
  localStorage.setItem('mb_theme', t);
}
(function initTheme() {
  const saved = localStorage.getItem('mb_theme');
  if (saved) { applyTheme(saved); return; }
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(prefersDark ? 'dark' : 'dark'); // default dark regardless of system, matches brand
})();
if (themeToggle) {
  themeToggle.addEventListener('click', () => {
    const current = root.getAttribute('data-theme');
    applyTheme(current === 'dark' ? 'light' : 'dark');
  });
}

// ---------- Mobile menu ----------
const hamburger = document.getElementById('hamburger');
const mobileMenu = document.getElementById('mobileMenu');
if (hamburger && mobileMenu) {
  hamburger.addEventListener('click', () => mobileMenu.classList.toggle('open'));
  mobileMenu.querySelectorAll('a').forEach(a => a.addEventListener('click', () => mobileMenu.classList.remove('open')));
}

// ---------- Toast ----------
function toast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

// ---------- Cookies modal ----------
const cookiesBtn = document.getElementById('cookiesBtn');
const cookiesModal = document.getElementById('cookiesModal');
const cookiesModalClose = document.getElementById('cookiesModalClose');
const cookiesUploadConfirm = document.getElementById('cookiesUploadConfirm');
const cookiesFileInput = document.getElementById('cookiesFileInput');
const cookiesStatus = document.getElementById('cookiesStatus');

if (cookiesBtn) {
  cookiesBtn.addEventListener('click', () => { cookiesModal.style.display = 'flex'; });
  cookiesModalClose.addEventListener('click', () => { cookiesModal.style.display = 'none'; });
  cookiesUploadConfirm.addEventListener('click', async () => {
    const file = cookiesFileInput.files[0];
    if (!file) { cookiesStatus.textContent = 'Choose a file first.'; return; }
    cookiesStatus.textContent = 'Uploading…';
    const formData = new FormData();
    formData.append('cookies', file);
    try {
      const res = await fetch('/api/cookies', { method: 'POST', body: formData });
      const data = await res.json();
      if (res.ok) {
        localStorage.setItem('mb_cookie_token', data.token);
        cookiesStatus.textContent = 'Uploaded. It will be used for the next 6 hours.';
        toast('Cookies file active', 'success');
        setTimeout(() => { cookiesModal.style.display = 'none'; }, 1200);
      } else {
        cookiesStatus.textContent = data.error || 'Upload failed.';
      }
    } catch (e) {
      cookiesStatus.textContent = 'Network error during upload.';
    }
  });
}

function getCookieToken() {
  return localStorage.getItem('mb_cookie_token') || null;
}

// ---------- FAQ accordion ----------
document.querySelectorAll('.faq-item').forEach(item => {
  const q = item.querySelector('.faq-q');
  if (!q) return;
  q.addEventListener('click', () => {
    const isOpen = item.classList.contains('open');
    document.querySelectorAll('.faq-item').forEach(i => i.classList.remove('open'));
    if (!isOpen) item.classList.add('open');
  });
});

// ============================================
// Downloader (only runs on pages with #urlInput)
// ============================================
const urlInput = document.getElementById('urlInput');

if (urlInput) {
  const fetchBtn = document.getElementById('fetchBtn');
  const pasteBtn = document.getElementById('pasteBtn');
  const clipboardDetectBtn = document.getElementById('clipboardDetectBtn');
  const statusEl = document.getElementById('status');
  const previewCard = document.getElementById('previewCard');
  const previewSkeleton = document.getElementById('previewSkeleton');
  const pThumb = document.getElementById('pThumb');
  const pTitle = document.getElementById('pTitle');
  const pSub = document.getElementById('pSub');
  const pStats = document.getElementById('pStats');
  const videoQuality = document.getElementById('videoQuality');
  const videoSizeHint = document.getElementById('videoSizeHint');
  const audioQuality = document.getElementById('audioQuality');
  const audioFormat = document.getElementById('audioFormat');
  const downloadManager = document.getElementById('downloadManager');
  const thumbChip = document.getElementById('thumbChip');
  const subChip = document.getElementById('subChip');
  const playlistPanel = document.getElementById('playlistPanel');
  const playlistEntries = document.getElementById('playlistEntries');
  const downloadBatchBtn = document.getElementById('downloadBatchBtn');
  const emptyStateHTML = '<p style="text-align:center; color:var(--muted); font-size:0.85rem; padding:20px 0;">Your downloads will appear here.</p>';
  downloadManager.innerHTML = emptyStateHTML;

  const URL_PATTERN = /(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|twitter\.com|x\.com|facebook\.com|fb\.watch|vimeo\.com|dailymotion\.com)/i;
  let currentUrl = '';
  let currentInfo = null;
  let selectedExtras = new Set();

  function setStatus(msg, isError = false) {
    statusEl.textContent = msg;
    statusEl.className = isError ? 'error' : '';
  }

  function isSupported(url) {
    try {
      new URL(url);
    } catch (e) {
      return false;
    }
    return URL_PATTERN.test(url);
  }

  function formatDuration(sec) {
    if (!sec && sec !== 0) return '';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    return h > 0
      ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${m}:${String(s).padStart(2, '0')}`;
  }

  function formatCount(n) {
    if (n === null || n === undefined) return null;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
  }

  function formatBytes(bytes) {
    if (!bytes) return null;
    const mb = bytes / (1024 * 1024);
    return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB';
  }

  // ---------- Clipboard ----------
  if (pasteBtn) {
    pasteBtn.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        urlInput.value = text.trim();
        setStatus(isSupported(text) ? 'Link pasted. Tap Download to preview.' : '');
      } catch (e) {
        setStatus('Clipboard access denied. Paste manually instead.', true);
      }
    });
  }

  if (clipboardDetectBtn) {
    clipboardDetectBtn.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (isSupported(text.trim())) {
          urlInput.value = text.trim();
          toast('Supported link detected in clipboard', 'success');
          fetchInfo();
        } else {
          toast('No supported link found in clipboard', 'error');
        }
      } catch (e) {
        toast('Clipboard permission not granted', 'error');
      }
    });
  }

  // ---------- Fetch info ----------
  async function fetchInfo() {
    const url = urlInput.value.trim();
    if (!url) { setStatus('Paste a link first.', true); return; }
    if (!isSupported(url)) { setStatus('Unsupported or invalid URL.', true); return; }

    currentUrl = url;
    fetchBtn.disabled = true;
    previewCard.style.display = 'none';
    previewSkeleton.style.display = 'block';
    setStatus('Fetching preview…');

    try {
      const res = await fetch('/api/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, cookie_token: getCookieToken() })
      });
      const data = await res.json();

      if (!res.ok) {
        setStatus(data.error || 'Could not fetch that link.', true);
        return;
      }

      currentInfo = data;
      renderPreview(data);
      setStatus('');
    } catch (e) {
      setStatus('Network error — try again.', true);
    } finally {
      fetchBtn.disabled = false;
      previewSkeleton.style.display = 'none';
    }
  }

  function renderPreview(data) {
    pTitle.textContent = data.title || 'Untitled';
    pSub.textContent = [data.uploader, data.platform].filter(Boolean).join(' • ');
    if (data.thumbnail) { pThumb.src = data.thumbnail; pThumb.style.display = 'block'; }
    else { pThumb.style.display = 'none'; }

    pStats.innerHTML = '';
    const stats = [];
    if (data.duration) stats.push(formatDuration(data.duration));
    if (data.view_count) stats.push(formatCount(data.view_count) + ' views');
    if (data.like_count) stats.push(formatCount(data.like_count) + ' likes');
    if (data.upload_date) stats.push(data.upload_date);
    if (data.is_playlist) stats.push(`Playlist · ${data.entry_count || '?'} videos`);
    stats.forEach(s => {
      const span = document.createElement('span');
      span.textContent = s;
      pStats.appendChild(span);
    });

    videoQuality.innerHTML = '';
    (data.formats || []).forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.format_id;
      opt.dataset.size = f.filesize_approx || '';
      const size = formatBytes(f.filesize_approx);
      opt.textContent = `${f.height}p ${f.ext}${size ? ' · ' + size : ''}`;
      videoQuality.appendChild(opt);
    });
    if (!data.formats || data.formats.length === 0) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'Best available';
      videoQuality.appendChild(opt);
    }
    updateSizeHint();

    // Playlist batch selection
    if (data.is_playlist && data.playlist_entries && data.playlist_entries.length > 0) {
      playlistPanel.style.display = 'block';
      playlistEntries.innerHTML = '';
      data.playlist_entries.forEach((entry, i) => {
        const row = document.createElement('label');
        row.style.cssText = 'display:flex; align-items:center; gap:10px; font-size:0.85rem; cursor:pointer;';
        row.innerHTML = `
          <input type="checkbox" data-url="${encodeURIComponent(entry.url)}" data-title="${encodeURIComponent(entry.title || 'Untitled')}">
          <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${entry.title || 'Untitled'}</span>
        `;
        playlistEntries.appendChild(row);
      });
    } else {
      playlistPanel.style.display = 'none';
    }

    previewCard.style.display = 'block';
    previewCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function updateSizeHint() {
    const opt = videoQuality.selectedOptions[0];
    if (!opt) { videoSizeHint.textContent = ''; return; }
    const size = formatBytes(Number(opt.dataset.size));
    videoSizeHint.textContent = size ? `Estimated size: ${size}` : '';
  }
  videoQuality.addEventListener('change', updateSizeHint);

  fetchBtn.addEventListener('click', fetchInfo);
  urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') fetchInfo(); });
  urlInput.addEventListener('paste', () => {
    setTimeout(() => {
      if (isSupported(urlInput.value.trim())) {
        toast('Supported link recognized — fetching…', 'success');
        fetchInfo();
      }
    }, 50);
  });

  // ---------- Tabs ----------
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.querySelector(`.tab-panel[data-panel="${tab.dataset.tab}"]`).classList.add('active');
    });
  });

  // ---------- Extras chips ----------
  if (thumbChip) thumbChip.addEventListener('click', () => {
    thumbChip.classList.toggle('selected');
    selectedExtras.has('thumbnail') ? selectedExtras.delete('thumbnail') : selectedExtras.add('thumbnail');
  });
  if (subChip) subChip.addEventListener('click', () => {
    subChip.classList.toggle('selected');
    selectedExtras.has('subtitles') ? selectedExtras.delete('subtitles') : selectedExtras.add('subtitles');
  });

  // ---------- Download manager ----------
  function createJobCard(label) {
    if (downloadManager.innerHTML === emptyStateHTML) downloadManager.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'job-card';
    card.innerHTML = `
      <div class="job-top">
        <span class="job-title">${label}</span>
        <div class="job-actions"><button class="cancel-btn">Cancel</button></div>
      </div>
      <div class="progress-track"><div class="progress-fill"></div></div>
      <div class="job-stats"><span class="pct">0%</span><span class="speed"></span><span class="eta"></span></div>
    `;
    downloadManager.prepend(card);
    return card;
  }

  function alreadyDownloaded(url) {
    try {
      const hist = JSON.parse(localStorage.getItem('mb_history') || '[]');
      return hist.some(h => h.url === url);
    } catch (e) { return false; }
  }

  function saveHistory(entry) {
    try {
      const hist = JSON.parse(localStorage.getItem('mb_history') || '[]');
      hist.unshift(entry);
      localStorage.setItem('mb_history', JSON.stringify(hist.slice(0, 30)));
    } catch (e) { /* ignore */ }
  }

  async function startDownload(payload, label) {
    const card = createJobCard(label);
    const fill = card.querySelector('.progress-fill');
    const pct = card.querySelector('.pct');
    const speed = card.querySelector('.speed');
    const eta = card.querySelector('.eta');
    const cancelBtn = card.querySelector('.cancel-btn');

    let cancelled = false;
    cancelBtn.addEventListener('click', () => { cancelled = true; card.remove(); });

    try {
      const res = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, cookie_token: getCookieToken() })
      });
      const data = await res.json();
      if (!res.ok) {
        card.classList.add('error');
        pct.textContent = data.error || 'Failed';
        return;
      }

      const jobId = data.job_id;
      const poll = setInterval(async () => {
        if (cancelled) { clearInterval(poll); return; }
        try {
          const pres = await fetch(`/api/progress/${jobId}`);
          const p = await pres.json();
          if (p.status === 'queued') {
            card.classList.add('queued');
            pct.textContent = p.queue_position ? `Queued (#${p.queue_position})` : 'Queued…';
            speed.textContent = '';
            eta.textContent = '';
          } else if (p.status === 'checking') {
            card.classList.remove('queued');
            pct.textContent = 'Checking file…';
          } else if (p.status === 'downloading') {
            card.classList.remove('queued');
            const pctVal = p.percent || 0;
            fill.style.width = `${pctVal}%`;
            fill.style.background = pctVal >= 90 ? 'var(--teal)' : 'var(--gold)';
            pct.textContent = `${pctVal.toFixed(0)}%`;
            speed.textContent = p.speed || '';
            eta.textContent = p.eta ? `ETA ${p.eta}` : '';
          } else if (p.status === 'finished') {
            clearInterval(poll);
            fill.style.width = '100%';
            pct.textContent = '100%';
            speed.textContent = '';
            eta.textContent = '';
            card.classList.add('success');
            cancelBtn.textContent = 'Open';
            cancelBtn.onclick = () => window.location = p.download_url;
            toast(`${label} ready`, 'success');
            saveHistory({ title: label, url: payload.url, time: Date.now() });
            window.location = p.download_url;
          } else if (p.status === 'error') {
            clearInterval(poll);
            card.classList.add('error');
            pct.textContent = 'Failed';
            speed.textContent = p.error || '';
            toast(`${label} failed`, 'error');
          }
        } catch (e) {
          clearInterval(poll);
          card.classList.add('error');
          pct.textContent = 'Connection lost';
        }
      }, 1200);
    } catch (e) {
      card.classList.add('error');
      pct.textContent = 'Network error';
    }
  }

  const downloadVideoBtn = document.getElementById('downloadVideoBtn');
  const downloadAudioBtn = document.getElementById('downloadAudioBtn');
  const downloadExtraBtn = document.getElementById('downloadExtraBtn');

  function confirmIfDuplicate(url) {
    if (alreadyDownloaded(url)) {
      return window.confirm('You already downloaded this. Download again?');
    }
    return true;
  }

  if (downloadVideoBtn) downloadVideoBtn.addEventListener('click', () => {
    if (!confirmIfDuplicate(currentUrl)) return;
    startDownload({
      url: currentUrl,
      type: 'video',
      format_id: videoQuality.value
    }, currentInfo?.title || 'Video download');
  });

  if (downloadBatchBtn) downloadBatchBtn.addEventListener('click', () => {
    const checked = playlistEntries.querySelectorAll('input[type="checkbox"]:checked');
    if (checked.length === 0) { toast('Select at least one video', 'error'); return; }
    checked.forEach(cb => {
      const url = decodeURIComponent(cb.dataset.url);
      const title = decodeURIComponent(cb.dataset.title);
      if (confirmIfDuplicate(url)) {
        startDownload({ url, type: 'video' }, title);
      }
    });
  });

  if (downloadAudioBtn) downloadAudioBtn.addEventListener('click', () => {
    if (!confirmIfDuplicate(currentUrl)) return;
    startDownload({
      url: currentUrl,
      type: 'audio',
      audio_quality: audioQuality.value,
      audio_format: audioFormat.value
    }, `${currentInfo?.title || 'Audio'} (${audioFormat.value})`);
  });

  if (downloadExtraBtn) downloadExtraBtn.addEventListener('click', () => {
    if (selectedExtras.size === 0) { toast('Select at least one extra', 'error'); return; }
    selectedExtras.forEach(extra => {
      startDownload({ url: currentUrl, type: extra }, `${currentInfo?.title || 'File'} — ${extra}`);
    });
  });
}

// ============================================
// Contact form validation
// ============================================
const contactForm = document.getElementById('contactForm');
if (contactForm) {
  const fields = {
    cName: { el: document.getElementById('fieldName'), validate: v => v.trim().length > 0 },
    cEmail: { el: document.getElementById('fieldEmail'), validate: v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) },
    cSubject: { el: document.getElementById('fieldSubject'), validate: v => v.trim().length > 0 },
    cMessage: { el: document.getElementById('fieldMessage'), validate: v => v.trim().length > 0 },
  };

  contactForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    let valid = true;
    Object.entries(fields).forEach(([id, f]) => {
      const val = document.getElementById(id).value;
      const ok = f.validate(val);
      f.el.classList.toggle('invalid', !ok);
      if (!ok) valid = false;
    });
    if (!valid) return;

    const statusEl = document.getElementById('contactStatus');
    statusEl.textContent = 'Sending…';

    try {
      const res = await fetch('/api/contact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: document.getElementById('cName').value,
          email: document.getElementById('cEmail').value,
          subject: document.getElementById('cSubject').value,
          message: document.getElementById('cMessage').value,
        })
      });
      const data = await res.json();
      if (res.ok) {
        statusEl.textContent = 'Message sent — thanks for reaching out.';
        contactForm.reset();
      } else {
        statusEl.textContent = data.error || 'Something went wrong.';
      }
    } catch (e) {
      statusEl.textContent = 'Network error — try again.';
    }
  });
}
