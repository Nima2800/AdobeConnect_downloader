const jobId = document.body.dataset.jobId;
const video = document.querySelector('#classVideo');
const skeleton = document.querySelector('#videoSkeleton');
const progressBar = document.querySelector('#progressBar');
const progressNumber = document.querySelector('#progressNumber');
const statusText = document.querySelector('#statusText');
const logsBox = document.querySelector('#logsBox');
const downloadBtn = document.querySelector('#downloadBtn');
const mp3Btn = document.querySelector('#mp3Btn');
const packageBtn = document.querySelector('#packageBtn');
const metaText = document.querySelector('#metaText');
const livePreviewWrap = document.querySelector('#livePreviewWrap');
const livePreview = document.querySelector('#livePreview');
const previewTime = document.querySelector('#previewTime');
const segmentPanel = document.querySelector('#segmentPanel');
const segmentList = document.querySelector('#segmentList');
const segmentStatus = document.querySelector('#segmentStatus');
const playFinalBtn = document.querySelector('#playFinalBtn');
const stageText = document.querySelector('#stageText');
const segmentText = document.querySelector('#segmentText');
const timeText = document.querySelector('#timeText');
const stagePills = document.querySelector('#stagePills');
const chapterList = document.querySelector('#chapterList');
const searchResults = document.querySelector('#searchResults');

let currentMode = 'waiting'; // waiting | segment | final
let currentSegmentIndex = -1;
let lastPreviewSrc = '';
let currentSpeed = 1;
let latestData = null;

function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function setStatus(data) {
  latestData = data;
  const pct = Math.round(data.progress || 0);
  progressBar.style.width = `${pct}%`;
  progressNumber.textContent = `${pct}%`;

  const statusMap = {
    queued: 'در صف پردازش…',
    running: data.segments_ready ? 'در حال ساخت ادامه ویدیو؛ بخش‌های آماده قابل پخش هستند…' : 'ویدیو در حال ساخته شدن است…',
    paused: 'پردازش Pause شده است.',
    interrupted: 'پردازش قبلی قطع شده؛ برای ادامه Resume را بزن.',
    cancelled: 'پردازش لغو شده است.',
    done: data.job_type === 'preview' ? 'Preview آماده است ✅' : 'فیلم و پکیج کامل آماده است ✅',
    error: 'پردازش با خطا متوقف شد.'
  };
  statusText.textContent = data.error || statusMap[data.status] || 'در حال بررسی وضعیت…';
  logsBox.textContent = (data.logs || []).join('\n');
  logsBox.scrollTop = logsBox.scrollHeight;

  const elements = (data.elements || []).join(' · ');
  const analysis = data.analysis || {};
  metaText.textContent = [
    elements ? `المان‌ها: ${elements}` : '',
    data.cache_used ? 'Cache استفاده شد ⚡' : '',
    data.quality_label ? `Preset: ${data.quality_label}` : '',
    analysis.duration_label ? `مدت: ${analysis.duration_label}` : ''
  ].filter(Boolean).join(' · ');

  stageText.textContent = `Stage: ${data.stage_label || data.stage || 'queued'}`;
  segmentText.textContent = data.segments_total
    ? `Segments: ${data.completed_segments || 0}/${data.segments_total}`
    : `Segments: ${data.segments_ready || 0}`;
  timeText.textContent = `Elapsed: ${data.elapsed_label || '00:00'} · ETA: ${data.eta_seconds ? data.eta_label : '--'}`;
  renderPills(data);
  renderSegments(data);
  renderChapters(data.chapters || []);

  const hasSegments = Array.isArray(data.segments) && data.segments.length > 0;
  if (hasSegments && currentMode === 'waiting') {
    playSegment(data.segments[0], false);
  }

  if (data.preview_ready && !data.video_ready && !hasSegments && currentMode === 'waiting') {
    const previewSrc = `${data.preview_url}?t=${Date.now()}`;
    if (previewSrc !== lastPreviewSrc) {
      livePreview.src = previewSrc;
      lastPreviewSrc = previewSrc;
    }
    livePreviewWrap.classList.remove('hidden');
    skeleton.classList.add('hidden');
    const st = data.preview_state || {};
    if (typeof st.time === 'number' && typeof st.duration === 'number') {
      previewTime.textContent = `تا ${formatTime(st.time)} از ${formatTime(st.duration)} رندر شده`;
    } else {
      previewTime.textContent = 'آخرین فریم ساخته‌شده نمایش داده می‌شود';
    }
  }

  if (data.video_url) {
    downloadBtn.href = data.download_url;
    downloadBtn.classList.remove('hidden');
    playFinalBtn.classList.toggle('hidden', !data.video_ready && data.status !== 'done');
    if (!hasSegments && currentMode === 'waiting' && (data.video_ready || data.status === 'done')) {
      playFinal(false);
    }
  }
  if (data.mp3_ready) {
    mp3Btn.href = data.mp3_url;
    mp3Btn.classList.remove('hidden');
  }
  if (data.package_ready) {
    packageBtn.href = data.package_url;
    packageBtn.classList.remove('hidden');
  }
}

function renderPills(data) {
  if (!stagePills) return;
  const values = [
    data.search_count ? `Search items: ${data.search_count}` : '',
    data.cache_used ? 'Cache hit' : ''
  ].filter(Boolean);
  stagePills.innerHTML = values.map(v => `<span>${v}</span>`).join('');
}

function renderSegments(data) {
  const segments = Array.isArray(data.segments) ? data.segments : [];
  if (!segments.length) return;

  segmentPanel.classList.remove('hidden');
  segmentStatus.textContent = data.video_ready
    ? `همه بخش‌ها آماده‌اند؛ نسخه کامل هم ساخته شده است.`
    : `${segments.length} بخش آماده شده؛ می‌توانی همین‌ها را پخش کنی تا بخش بعدی آماده شود.`;

  segmentList.innerHTML = '';
  for (const seg of segments) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segment-btn';
    btn.dataset.index = String(seg.index);
    btn.classList.toggle('active', currentMode === 'segment' && currentSegmentIndex === Number(seg.index));
    const range = (typeof seg.start === 'number' && typeof seg.end === 'number')
      ? `${formatTime(seg.start)} تا ${formatTime(seg.end)}`
      : `بخش ${seg.index}`;
    const size = seg.size ? ` · ${formatBytes(seg.size)}` : '';
    btn.innerHTML = `<strong>بخش ${seg.index}</strong><span>${range}${size}</span>`;
    btn.addEventListener('click', () => playSegment(seg, true));
    segmentList.appendChild(btn);
  }
}

function renderChapters(chapters) {
  if (!chapterList) return;
  if (!chapters.length) {
    chapterList.className = 'chapter-list empty-state';
    chapterList.textContent = 'هنوز chapter آماده نیست.';
    return;
  }
  chapterList.className = 'chapter-list';
  chapterList.innerHTML = chapters.slice(0, 80).map(ch => `
    <button type="button" class="chapter-item" data-time="${ch.time || 0}">
      <strong>${formatTime(ch.time || 0)}</strong>
      <span>${escapeHtml(ch.title || 'Chapter')}</span>
      <em>${escapeHtml(ch.type || '')}</em>
    </button>
  `).join('');
  chapterList.querySelectorAll('.chapter-item').forEach(btn => btn.addEventListener('click', () => seekTo(Number(btn.dataset.time || 0))));
}

function playSegment(seg, autoplay = true) {
  if (!seg || !seg.url) return;
  currentMode = 'segment';
  currentSegmentIndex = Number(seg.index);
  video.src = seg.url;
  video.playbackRate = currentSpeed;
  video.classList.remove('hidden');
  skeleton.classList.add('hidden');
  livePreviewWrap.classList.add('hidden');
  updateActiveSegmentButtons();
  if (autoplay) video.play().catch(() => {});
}

function playFinal(autoplay = true) {
  if (!latestData?.video_url) return;
  currentMode = 'final';
  currentSegmentIndex = -1;
  video.src = latestData.video_url;
  video.playbackRate = currentSpeed;
  video.classList.remove('hidden');
  skeleton.classList.add('hidden');
  livePreviewWrap.classList.add('hidden');
  updateActiveSegmentButtons();
  if (autoplay) video.play().catch(() => {});
}

function updateActiveSegmentButtons() {
  document.querySelectorAll('.segment-btn').forEach(btn => {
    btn.classList.toggle('active', currentMode === 'segment' && Number(btn.dataset.index) === currentSegmentIndex);
  });
}

function seekTo(seconds) {
  seconds = Number(seconds || 0);
  if (latestData?.video_url && currentMode !== 'final') {
    playFinal(false);
    setTimeout(() => { video.currentTime = seconds; video.play().catch(() => {}); }, 350);
    return;
  }
  if (currentMode === 'final') {
    video.currentTime = seconds;
    video.play().catch(() => {});
    return;
  }
  const seg = latestData?.segments?.find(s => typeof s.start === 'number' && typeof s.end === 'number' && seconds >= s.start && seconds < s.end);
  if (seg) {
    playSegment(seg, false);
    setTimeout(() => { video.currentTime = Math.max(0, seconds - seg.start); video.play().catch(() => {}); }, 250);
  } else {
    toast('برای این زمان هنوز segment آماده نیست.');
  }
}

video.addEventListener('ended', () => {
  if (currentMode !== 'segment' || !latestData?.segments) return;
  const next = latestData.segments.find(seg => Number(seg.index) === currentSegmentIndex + 1);
  if (next) {
    playSegment(next, true);
  } else if (latestData.video_ready) {
    playFinal(true);
  }
});

playFinalBtn?.addEventListener('click', () => playFinal(true));

async function poll() {
  try {
    const res = await fetch(`/api/jobs/${jobId}`, { cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'job پیدا نشد');
    setStatus(data);
    if (!['done', 'error', 'cancelled'].includes(data.status)) {
      setTimeout(poll, data.status === 'paused' ? 3500 : 1400);
    }
  } catch (err) {
    statusText.textContent = err.message || 'ارتباط با سرور قطع شد.';
    setTimeout(poll, 2500);
  }
}

function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = n => String(n).padStart(2, '0');
  return h ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function formatBytes(bytes) {
  let n = Number(bytes || 0);
  const units = ['B', 'KB', 'MB', 'GB'];
  for (const u of units) {
    if (n < 1024 || u === units[units.length - 1]) return `${n.toFixed(u === 'B' ? 0 : 1)} ${u}`;
    n /= 1024;
  }
}

function setSpeed(speed) {
  currentSpeed = Number(speed);
  video.playbackRate = currentSpeed;
  document.querySelectorAll('.speed-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.speed === String(speed));
  });
}

document.querySelectorAll('.speed-btn').forEach(btn => {
  btn.addEventListener('click', () => setSpeed(btn.dataset.speed));
});


document.querySelectorAll('.compact-tool-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const panel = document.getElementById(btn.dataset.panel || '');
    if (!panel) return;
    const willOpen = panel.classList.contains('hidden');
    document.querySelectorAll('.compact-panel').forEach(p => p.classList.add('hidden'));
    document.querySelectorAll('.compact-tool-btn').forEach(b => {
      b.classList.remove('active');
      b.setAttribute('aria-expanded', 'false');
    });
    if (willOpen) {
      panel.classList.remove('hidden');
      btn.classList.add('active');
      btn.setAttribute('aria-expanded', 'true');
    }
  });
});

document.querySelector('#toggleLogs')?.addEventListener('click', () => {
  logsBox.classList.toggle('hidden');
});

async function jobAction(action, options = {}) {
  const res = await fetch(`/api/jobs/${jobId}/${action}`, { method: 'POST', body: options.body });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || `${action} ناموفق بود.`);
  return data;
}

document.querySelector('#pauseBtn')?.addEventListener('click', async () => {
  try { await jobAction('pause'); toast('Pause شد.'); poll(); } catch (e) { toast(e.message); }
});
document.querySelector('#resumeBtn')?.addEventListener('click', async () => {
  try { await jobAction('resume'); toast('Resume شد.'); poll(); } catch (e) { toast(e.message); }
});
document.querySelector('#cancelBtn')?.addEventListener('click', async () => {
  try { await jobAction('cancel'); toast('Cancel شد.'); poll(); } catch (e) { toast(e.message); }
});
document.querySelector('#cleanupBtn')?.addEventListener('click', async () => {
  try {
    const fd = new FormData(); fd.set('keep_cache', '1');
    await jobAction('cleanup', { body: fd });
    toast('Cleanup انجام شد.');
  } catch (e) { toast(e.message); }
});

async function runSearch() {
  const q = document.querySelector('#recordingSearch')?.value.trim() || '';
  try {
    const res = await fetch(`/api/jobs/${jobId}/search?q=${encodeURIComponent(q)}`, { cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'Search failed');
    if (!data.results.length) {
      searchResults.className = 'search-results empty-state';
      searchResults.textContent = 'نتیجه‌ای پیدا نشد.';
      return;
    }
    searchResults.className = 'search-results';
    searchResults.innerHTML = data.results.map(item => `
      <button class="search-item" type="button" data-time="${item.time || 0}">
        <strong>${formatTime(item.time || 0)} · ${escapeHtml(item.type || '')}</strong>
        <span>${escapeHtml(item.title || '')}</span>
        <p>${escapeHtml(item.text || '')}</p>
      </button>
    `).join('');
    searchResults.querySelectorAll('.search-item').forEach(btn => btn.addEventListener('click', () => seekTo(Number(btn.dataset.time || 0))));
  } catch (e) {
    searchResults.textContent = e.message || 'Search failed';
  }
}

document.querySelector('#searchBtn')?.addEventListener('click', runSearch);
document.querySelector('#recordingSearch')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') runSearch();
});

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
}

poll();
