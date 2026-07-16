// ============================================
// MB MEDIA — app.js
// ============================================

// ---------- Footer year (CSP blocks inline scripts) ----------
(function initYear() {
  const yearEl = document.getElementById('year');
  if (yearEl) yearEl.textContent = String(new Date().getFullYear());
})();

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

// ---------- Active nav tab ----------
(function initActiveNav() {
  function markActiveNav() {
    const path = (window.location.pathname || '/').replace(/\/+$/, '') || '/';
    const hash = (window.location.hash || '').replace(/^#/, '');
    let key = 'home';
    if (path.startsWith('/contact')) key = 'contact';
    else if (path.startsWith('/about')) key = 'about';
    else if (path.startsWith('/donate')) key = 'donate';
    else if (path.startsWith('/history')) key = 'history';
    else if (hash === 'platforms') key = 'platforms';
    else if (hash === 'features') key = 'features';
    else if (hash === 'faq') key = 'faq';
    else if (path === '/' || path === '') key = 'home';

    document.querySelectorAll('[data-nav]').forEach((a) => {
      a.classList.toggle('active', a.getAttribute('data-nav') === key);
    });
  }
  markActiveNav();
  window.addEventListener('hashchange', markActiveNav);
})();

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

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function isMobileDevice() {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
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
  cookiesModal.addEventListener('click', (e) => {
    if (e.target === cookiesModal) cookiesModal.style.display = 'none';
  });
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
  const emptyStateHTML = '<p class="download-empty">Your downloads will appear here.</p>';
  downloadManager.innerHTML = emptyStateHTML;

  function isManagerEmpty() {
    return !downloadManager.querySelector('.job-card');
  }

  function restoreEmptyState() {
    if (isManagerEmpty()) downloadManager.innerHTML = emptyStateHTML;
  }

  function setDownloadControlsDisabled(disabled) {
    ['downloadVideoBtn', 'downloadAudioBtn', 'downloadExtraBtn', 'downloadBatchBtn'].forEach((id) => {
      const btn = document.getElementById(id);
      if (btn) btn.disabled = disabled;
    });
  }

  const URL_PATTERN = /(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|twitter\.com|x\.com|facebook\.com|fb\.watch|fb\.gg|vimeo\.com|dailymotion\.com|dai\.ly|vm\.tiktok\.com|vt\.tiktok\.com)/i;
  const TRACKING_PARAMS = new Set([
    'si', 'feature', 'pp', 'bpctr', 'spfreload', 'rc', 'source', 'src',
    'igshid', 'igsh', 'img_index', 's', 'ref_src', 'ref_url',
    'fbclid', 'gclid', 'mc_cid', 'mc_eid',
    'is_from_webapp', 'sender_device', 'sender_web_id', 'share_app_id',
    'share_item_type', 'share_link_id', 'share_author_id',
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id',
    'start_radio', 'index',
  ]);
  let currentUrl = '';
  let currentInfo = null;
  let selectedExtras = new Set();

  function setStatus(msg, isError = false) {
    statusEl.textContent = msg;
    statusEl.className = isError ? 'error' : '';
  }

  /** Detect and repair common broken/share/mix URLs before fetch (mirrors server). */
  function sanitizeMediaUrl(raw) {
    const changes = [];
    let text = (raw || '').trim().replace(/^['"]+|['"]+$/g, '');
    if (!text) return { url: '', changes };

    const embedded = text.match(/https?:\/\/[^\s<>"']+/i);
    if (embedded) {
      const extracted = embedded[0].replace(/[).,}\]>"']+$/g, '');
      if (extracted !== text) changes.push('extracted link from pasted text');
      text = extracted;
    } else if (/^(www\.|[a-z0-9-]+\.)?[a-z0-9-]+\.[a-z]{2,}/i.test(text) && !/^https?:\/\//i.test(text)) {
      text = 'https://' + text.replace(/^\/+/, '');
      changes.push('added https://');
    }

    if (!/^https?:\/\//i.test(text)) {
      text = 'https://' + text.replace(/^\/+/, '');
      changes.push('added https://');
    }

    let u;
    try { u = new URL(text); } catch (_) { return { url: text, changes }; }

    let host = u.hostname.toLowerCase().replace(/^www\./, '');
    const path = u.pathname || '';

    const stripTracking = () => {
      let removed = false;
      [...u.searchParams.keys()].forEach((k) => {
        const low = k.toLowerCase();
        if (TRACKING_PARAMS.has(low) || low.startsWith('utm_')) {
          u.searchParams.delete(k);
          removed = true;
        }
      });
      return removed;
    };

    // YouTube
    if (/youtube\.com|youtu\.be|youtube-nocookie\.com/i.test(host)) {
      if (host.startsWith('music.') || host.startsWith('m.')) {
        host = 'youtube.com';
        changes.push('converted mobile/music YouTube host');
      }
      const shortMatch = path.match(/\/(shorts|embed|live|v)\/([A-Za-z0-9_-]{6,})/);
      if (shortMatch) {
        const clean = new URL('https://www.youtube.com/watch');
        clean.searchParams.set('v', shortMatch[2]);
        const t = u.searchParams.get('t') || u.searchParams.get('start');
        if (t) clean.searchParams.set('t', t);
        changes.push(`converted /${shortMatch[1]}/ link to watch URL`);
        return { url: clean.toString(), changes };
      }
      if (host.includes('youtu.be')) {
        const vid = path.replace(/^\/+|\/+$/g, '').split('/')[0];
        if (vid) {
          const clean = new URL('https://www.youtube.com/watch');
          clean.searchParams.set('v', vid);
          if (u.searchParams.get('t')) clean.searchParams.set('t', u.searchParams.get('t'));
          changes.push('expanded youtu.be short link');
          return { url: clean.toString(), changes };
        }
      }
      if (path.includes('/watch') && u.searchParams.get('v')) {
        const clean = new URL('https://www.youtube.com/watch');
        clean.searchParams.set('v', u.searchParams.get('v'));
        if (u.searchParams.get('t')) clean.searchParams.set('t', u.searchParams.get('t'));
        const listId = u.searchParams.get('list') || '';
        if (listId.startsWith('RD')) changes.push('removed YouTube Mix (list=RD...) to avoid stall');
        else if (listId) changes.push('removed playlist list= param; using this video only');
        const keep = new Set(['v', 't', 'list']);
        for (const k of u.searchParams.keys()) {
          if (!keep.has(k)) { changes.push('removed tracking parameters'); break; }
        }
        return { url: clean.toString(), changes };
      }
    }

    // TikTok
    if (host.includes('tiktok.com')) {
      const m = path.match(/(?:\/@([^/]+))?\/video\/(\d+)/);
      if (m) {
        const user = m[1] || '';
        const id = m[2];
        const newPath = user ? `/@${user}/video/${id}` : `/video/${id}`;
        const cleaned = `https://www.tiktok.com${newPath}`;
        if (stripTracking() || cleaned !== text) {
          if ([...u.searchParams.keys()].length || u.search) changes.push('removed TikTok share/tracking params');
          if (cleaned !== text) changes.push('normalized TikTok video path');
        }
        return { url: cleaned, changes: [...new Set(changes)] };
      }
      if (stripTracking()) {
        changes.push('removed TikTok share/tracking params');
        return { url: u.toString(), changes };
      }
    }

    // Instagram
    if (host.includes('instagram.com')) {
      const m = path.match(/\/(reel|reels|p|tv)\/([^/?#]+)/);
      if (m) {
        let kind = m[1] === 'reels' ? 'reel' : m[1];
        const cleaned = `https://www.instagram.com/${kind}/${m[2]}/`;
        if (stripTracking()) changes.push('removed Instagram tracking params');
        if (cleaned !== text) changes.push('normalized Instagram media path');
        return { url: cleaned, changes };
      }
      if (stripTracking()) {
        changes.push('removed Instagram tracking params');
        return { url: u.toString(), changes };
      }
    }

    // X / Twitter
    if (host === 'x.com' || host === 'twitter.com' || host === 'mobile.twitter.com') {
      const m = path.match(/\/([^/]+)\/status\/(\d+)/);
      if (m) {
        const cleaned = `https://x.com/${m[1]}/status/${m[2]}`;
        if (stripTracking() || u.searchParams.has('t')) changes.push('removed X/Twitter tracking params');
        if (cleaned !== text) changes.push('normalized X status URL');
        return { url: cleaned, changes };
      }
      if (stripTracking()) {
        changes.push('removed X/Twitter tracking params');
        u.hostname = 'x.com';
        return { url: u.toString(), changes };
      }
    }

    if (stripTracking()) {
      changes.push('removed tracking parameters');
      return { url: u.toString(), changes };
    }

    return { url: text, changes };
  }

  function applyUrlFixes(original, data, alreadyToasted) {
    const fixed = (data && data.normalized_url) || original;
    if (fixed && fixed !== urlInput.value.trim()) {
      urlInput.value = fixed;
    }
    const fixes = (data && data.url_fixes) || [];
    // Only toast server-only fixes if the client hadn't already rewritten the URL.
    if (!alreadyToasted && fixes.length && fixed && fixed !== original) {
      toast('Link fixed: ' + fixes[0], 'success');
    }
    return fixed;
  }

  function isSupported(url) {
    const { url: cleaned } = sanitizeMediaUrl(url);
    try {
      new URL(cleaned);
    } catch (e) {
      return false;
    }
    return URL_PATTERN.test(cleaned);
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
    const pasted = urlInput.value.trim();
    if (!pasted) { setStatus('Paste a link first.', true); return; }

    const local = sanitizeMediaUrl(pasted);
    if (!local.url || !URL_PATTERN.test(local.url)) {
      setStatus('Unsupported or invalid URL.', true);
      return;
    }
    const alreadyToasted = !!(local.changes.length && local.url !== pasted);
    if (alreadyToasted) {
      urlInput.value = local.url;
      toast('Link fixed: ' + local.changes[0], 'success');
    }

    currentUrl = local.url;
    const prevLabel = fetchBtn.textContent;
    fetchBtn.disabled = true;
    fetchBtn.textContent = 'Loading…';
    previewCard.style.display = 'none';
    previewSkeleton.style.display = 'block';
    setStatus('Fetching preview…');

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 90000);

    try {
      const res = await fetch('/api/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: local.url, cookie_token: getCookieToken() }),
        signal: controller.signal,
      });
      const data = await res.json();

      if (!res.ok) {
        setStatus(data.error || 'Could not fetch that link.', true);
        return;
      }

      currentInfo = data;
      currentUrl = applyUrlFixes(local.url, data, alreadyToasted);
      renderPreview(data);
      setStatus('');
    } catch (e) {
      if (e && e.name === 'AbortError') {
        setStatus('Preview timed out. Try again, or upload cookies with the 🍪 button.', true);
      } else {
        setStatus('Network error — try again.', true);
      }
    } finally {
      clearTimeout(timeoutId);
      fetchBtn.disabled = false;
      fetchBtn.textContent = prevLabel;
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
      const tag = f.compatible ? ' · phone-friendly' : '';
      opt.textContent = `${f.height}p ${f.ext}${tag}${size ? ' · ' + size : ''}`;
      videoQuality.appendChild(opt);
    });
    if (!data.formats || data.formats.length === 0) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'Best compatible (MP4)';
      videoQuality.appendChild(opt);
    }
    updateSizeHint();

    // Playlist batch selection
    if (data.is_playlist && data.playlist_entries && data.playlist_entries.length > 0) {
      playlistPanel.style.display = 'block';
      playlistEntries.innerHTML = '';
      data.playlist_entries.forEach((entry) => {
        const row = document.createElement('label');
        row.className = 'playlist-row';
        const safeTitle = escapeHtml(entry.title || 'Untitled');
        const safeUrl = encodeURIComponent(entry.url || '');
        row.innerHTML = `
          <input type="checkbox" data-url="${safeUrl}" data-title="${encodeURIComponent(entry.title || 'Untitled')}">
          <span class="playlist-title">${safeTitle}</span>
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
  async function triggerFileDownload(downloadUrl) {
    if (isMobileDevice()) {
      try {
        const res = await fetch(downloadUrl);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const rawName = (res.headers.get('Content-Disposition') || '')
          .match(/filename\*?=(?:UTF-8''|"?)([^";]+)/i)?.[1]
          || 'download.bin';
        const fileName = decodeURIComponent(rawName.replace(/"/g, '').trim()) || 'download.bin';
        const file = new File([blob], fileName, { type: blob.type || 'application/octet-stream' });
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: fileName });
          return;
        }
        const objectUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = fileName;
        a.rel = 'noopener';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
        return;
      } catch (e) {
        // Last resort: open file URL in a new tab (never replace this page).
        window.open(downloadUrl, '_blank', 'noopener');
        toast('Opened file in a new tab — use Share/Save there if needed.', 'info');
        return;
      }
    }
    const a = document.createElement('a');
    a.href = downloadUrl;
    a.download = '';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function createJobCard(label) {
    if (isManagerEmpty()) downloadManager.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'job-card';
    card.innerHTML = `
      <div class="job-top">
        <span class="job-title">${escapeHtml(label)}</span>
        <div class="job-actions"><button type="button" class="cancel-btn">Cancel</button></div>
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
    let finished = false;
    let poll = null;
    let downloadUrl = null;

    const onActionClick = async (ev) => {
      ev.preventDefault();
      if (finished) {
        if (!downloadUrl) return;
        cancelBtn.disabled = true;
        cancelBtn.textContent = isMobileDevice() ? 'Saving…' : 'Save file';
        try {
          await triggerFileDownload(downloadUrl);
        } finally {
          cancelBtn.disabled = false;
          cancelBtn.textContent = 'Save file';
        }
        return;
      }
      cancelled = true;
      if (poll) clearInterval(poll);
      card.remove();
      restoreEmptyState();
    };
    cancelBtn.addEventListener('click', onActionClick);

    setDownloadControlsDisabled(true);

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
        speed.textContent = '';
        eta.textContent = '';
        toast(data.error || 'Download failed to start', 'error');
        return;
      }

      const jobId = data.job_id;
      poll = setInterval(async () => {
        if (cancelled) { clearInterval(poll); return; }
        try {
          const pres = await fetch(`/api/progress/${jobId}`);
          const p = await pres.json();
          if (!pres.ok) {
            clearInterval(poll);
            card.classList.add('error');
            pct.textContent = p.error || 'Job not found';
            return;
          }
          if (p.status === 'queued') {
            card.classList.add('queued');
            pct.textContent = p.queue_position ? `Queued (#${p.queue_position})` : 'Queued…';
            speed.textContent = '';
            eta.textContent = '';
          } else if (p.status === 'checking') {
            card.classList.remove('queued');
            pct.textContent = 'Checking file…';
            speed.textContent = '';
            eta.textContent = '';
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
            finished = true;
            downloadUrl = p.download_url;
            fill.style.width = '100%';
            pct.textContent = '100%';
            speed.textContent = '';
            eta.textContent = isMobileDevice() ? 'Tap Save file' : 'Ready — play below';
            card.classList.add('success');
            cancelBtn.textContent = 'Save file';
            cancelBtn.classList.add('save-btn');
            toast(`${label} ready`, 'success');
            saveHistory({ title: label, url: payload.url, time: Date.now() });
            mountJobPlayer(card, downloadUrl, payload.type);
            if (!isMobileDevice()) triggerFileDownload(downloadUrl);
          } else if (p.status === 'error') {
            clearInterval(poll);
            card.classList.add('error');
            pct.textContent = 'Failed';
            speed.textContent = p.error || 'Download failed';
            eta.textContent = '';
            toast(`${label} failed`, 'error');
          }
        } catch (e) {
          clearInterval(poll);
          card.classList.add('error');
          pct.textContent = 'Connection lost';
          speed.textContent = '';
          eta.textContent = '';
        }
      }, 1000);
    } catch (e) {
      card.classList.add('error');
      pct.textContent = 'Network error';
      speed.textContent = '';
      eta.textContent = '';
      toast('Network error while starting download', 'error');
    } finally {
      setDownloadControlsDisabled(false);
    }
  }

  async function mountJobPlayer(card, downloadUrl, kind) {
    if (!downloadUrl || (kind !== 'video' && kind !== 'audio')) return;
    if (card.querySelector('.job-player')) return;
    try {
      const res = await fetch(downloadUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      const player = document.createElement(kind === 'audio' ? 'audio' : 'video');
      player.className = 'job-player';
      player.controls = true;
      player.playsInline = true;
      player.preload = 'metadata';
      player.src = objectUrl;
      if (kind === 'video') {
        player.setAttribute('controlsList', 'nodownload');
      }
      card.appendChild(player);
      player.addEventListener('error', () => {
        toast('Preview player could not open this file — use Save file instead.', 'info');
      }, { once: true });
    } catch (e) {
      /* Player is optional; Save file still works */
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
    if (!currentUrl || !currentInfo) { toast('Fetch a preview first', 'error'); return; }
    if (!confirmIfDuplicate(currentUrl)) return;
    const formatId = videoQuality.value || null;
    startDownload({
      url: currentUrl,
      type: 'video',
      format_id: formatId,
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
    if (!currentUrl || !currentInfo) { toast('Fetch a preview first', 'error'); return; }
    if (!confirmIfDuplicate(currentUrl)) return;
    startDownload({
      url: currentUrl,
      type: 'audio',
      audio_quality: audioQuality.value,
      audio_format: audioFormat.value
    }, `${currentInfo?.title || 'Audio'} (${audioFormat.value})`);
  });

  if (downloadExtraBtn) downloadExtraBtn.addEventListener('click', () => {
    if (!currentUrl || !currentInfo) { toast('Fetch a preview first', 'error'); return; }
    if (selectedExtras.size === 0) { toast('Select at least one extra', 'error'); return; }
    selectedExtras.forEach(extra => {
      startDownload({ url: currentUrl, type: extra }, `${currentInfo?.title || 'File'} — ${extra}`);
    });
  });
}

// ============================================
// Contact form — report / suggestion / question
// ============================================
const contactForm = document.getElementById('contactForm');
if (contactForm) {
  const TYPE_LABELS = {
    report: 'Report an error',
    suggestion: 'Suggestion',
    question: 'Question',
    takedown: 'Takedown / DMCA',
    other: 'Other',
  };
  const fields = {
    cName: { el: document.getElementById('fieldName'), validate: v => v.trim().length > 0 },
    cEmail: { el: document.getElementById('fieldEmail'), validate: v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) },
    cSubject: { el: document.getElementById('fieldSubject'), validate: v => v.trim().length > 0 },
    cMessage: { el: document.getElementById('fieldMessage'), validate: v => v.trim().length > 0 },
  };

  function contactPayload() {
    const type = (document.getElementById('cType')?.value || 'other').trim();
    const subjectRaw = document.getElementById('cSubject').value.trim();
    const label = TYPE_LABELS[type] || 'Contact';
    return {
      type,
      name: document.getElementById('cName').value.trim(),
      email: document.getElementById('cEmail').value.trim(),
      subject: `[${label}] ${subjectRaw}`,
      message: document.getElementById('cMessage').value.trim(),
    };
  }

  const mailtoBtn = document.getElementById('mailtoFallbackBtn');
  if (mailtoBtn) {
    mailtoBtn.addEventListener('click', () => {
      const p = contactPayload();
      const body = `Name: ${p.name}\nEmail: ${p.email}\nType: ${p.type}\n\n${p.message}`;
      const href = `mailto:admin@mugobyte.com?subject=${encodeURIComponent(p.subject)}&body=${encodeURIComponent(body)}`;
      window.location.href = href;
    });
  }

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
    const submitBtn = document.getElementById('contactSubmitBtn');
    const payload = contactPayload();
    statusEl.textContent = 'Sending…';
    if (submitBtn) submitBtn.disabled = true;

    try {
      const res = await fetch('/api/contact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok) {
        statusEl.textContent = data.emailed
          ? 'Message sent — thanks for reaching out.'
          : 'Message received. We also saved it on the server. Thanks for helping improve MB MEDIA.';
        toast('Thanks — your message was submitted.', 'success');
        contactForm.reset();
        const typeEl = document.getElementById('cType');
        if (typeEl) typeEl.value = 'suggestion';
      } else {
        statusEl.textContent = data.error || 'Something went wrong.';
      }
    } catch (err) {
      statusEl.textContent = 'Network error — try again, or use the email fallback.';
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

// ---------- History page (CSP blocks inline scripts — must live in app.js) ----------
(function initHistoryPage() {
  const list = document.getElementById('historyList');
  if (!list) return;

  function renderHistory() {
    let hist = [];
    try {
      hist = JSON.parse(localStorage.getItem('mb_history') || '[]');
      if (!Array.isArray(hist)) hist = [];
    } catch (e) {
      hist = [];
    }
    list.innerHTML = '';
    if (hist.length === 0) {
      list.innerHTML = '<p style="color:var(--muted); text-align:center; font-size:0.9rem;">No downloads yet. Your history will appear here after a download finishes on this browser.</p>';
      return;
    }
    hist.forEach((item) => {
      const date = new Date(item.time).toLocaleString();
      const row = document.createElement('div');
      row.className = 'card';
      row.style.padding = '14px 18px';
      row.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; gap:12px;">
          <div style="overflow:hidden; min-width:0;">
            <div style="font-size:0.9rem; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(item.title || 'Untitled')}</div>
            <div style="font-size:0.75rem; color:var(--muted); margin-top:2px;">${escapeHtml(date)}</div>
          </div>
        </div>
      `;
      list.appendChild(row);
    });
  }

  const clearBtn = document.getElementById('clearHistoryBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      localStorage.removeItem('mb_history');
      renderHistory();
      toast('History cleared', 'success');
    });
  }
  renderHistory();
})();
