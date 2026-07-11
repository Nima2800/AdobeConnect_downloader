const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const toast = (msg) => {
  const el = $('#toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 3200);
};

const zipFile = $('#zipFile');
const zipLabel = $('#zipLabel');
const dropZone = $('.drop-zone');
let lastAnalysis = null;
let generatedDownloadUrl = '';

function formatSize(bytes) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let n = Number(bytes || 0);
  for (const unit of units) {
    if (n < 1024 || unit === units[units.length - 1]) return `${n.toFixed(unit === 'B' ? 0 : 1)} ${unit}`;
    n /= 1024;
  }
}

function showStep(id) {
  document.body.dataset.step = id;
  $$('.wizard-step').forEach(step => step.classList.toggle('hidden', step.id !== id));

  const historyShell = $('.history-shell');
  if (historyShell) historyShell.classList.toggle('hidden', id !== 'stepLink');

  const current = $(`#${id}`);
  current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;'
  }[ch]));
}

function isLikelyZipSelected() {
  return Boolean($('#uploadId')?.value || zipFile?.files?.length);
}

$('#classUrl')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    $('#continueToDownload')?.click();
  }
});

$('#toggleHistory')?.addEventListener('click', () => {
  const panel = $('#historyPanel');
  const btn = $('#toggleHistory');
  if (!panel || !btn) return;
  const isOpen = !panel.classList.contains('hidden');
  panel.classList.toggle('hidden', isOpen);
  btn.setAttribute('aria-expanded', String(!isOpen));
});

zipFile?.addEventListener('change', () => {
  zipLabel.textContent = zipFile.files[0]?.name || 'فایل ZIP را انتخاب کن';
  $('#uploadId').value = '';
  $('#zipLinkBox')?.classList.add('hidden');
  $('#analysisBox')?.classList.add('hidden');
});

['dragenter', 'dragover'].forEach(evt => dropZone?.addEventListener(evt, e => {
  e.preventDefault();
  dropZone.classList.add('dragover');
}));
['dragleave', 'drop'].forEach(evt => dropZone?.addEventListener(evt, e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
}));
dropZone?.addEventListener('drop', (e) => {
  if (e.dataTransfer.files.length) {
    const dt = new DataTransfer();
    dt.items.add(e.dataTransfer.files[0]);
    zipFile.files = dt.files;
    zipLabel.textContent = zipFile.files[0].name;
    $('#uploadId').value = '';
    $('#zipLinkBox')?.classList.add('hidden');
    $('#analysisBox')?.classList.add('hidden');
  }
});

async function ensureUploaded() {
  if ($('#uploadId').value) return $('#uploadId').value;
  if (!zipFile.files.length) throw new Error('اول فایل ZIP دانلودشده را انتخاب کن.');
  const fd = new FormData();
  fd.append('zip_file', zipFile.files[0]);
  const res = await fetch('/api/upload_zip', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || 'خطای آپلود');
  $('#uploadId').value = data.upload_id;
  const box = $('#zipLinkBox');
  if (box) {
    box.innerHTML = `ZIP آپلود شد: <a href="${data.download_url}">${escapeHtml(data.filename)}</a><br><small>Hash: ${String(data.input_hash || '').slice(0, 12)}… · ${formatSize(data.size)}</small>`;
    box.classList.remove('hidden');
  }
  return data.upload_id;
}

async function analyzeUploadedZip() {
  const uploadId = await ensureUploaded();
  const fd = new FormData();
  fd.append('upload_id', uploadId);
  const res = await fetch('/api/analyze_upload', { method: 'POST', body: fd });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || 'تحلیل ناموفق بود.');
  renderAnalysis(data);
  return data;
}

$('#continueToDownload')?.addEventListener('click', async () => {
  const btn = $('#continueToDownload');
  const classUrl = $('#classUrl')?.value.trim() || '';
  if (!classUrl) {
    toast('لطفاً لینک کلاس را وارد کنید.');
    return;
  }
  btn.disabled = true;
  const old = btn.innerHTML;
  btn.innerHTML = 'در حال ساخت لینک…';
  try {
    const fd = new FormData();
    fd.append('class_url', classUrl);
    const res = await fetch('/api/download_link', { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'ساخت لینک دانلود ناموفق بود.');
    generatedDownloadUrl = data.download_url;
    const box = $('#downloadLinkBox');
    box.dataset.url = data.download_url;
    box.innerHTML = `
      <strong>لینک دانلود ZIP آماده است:</strong>
      <a class="manual-download-link" href="${data.download_url}" target="_blank" rel="noopener">دانلود فایل ZIP کلاس</a>
      <button type="button" id="copyDownloadLink" class="chip-btn">کپی لینک</button>
    `;
    $('#copyDownloadLink')?.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(generatedDownloadUrl);
        toast('لینک دانلود کپی شد.');
      } catch (_) {
        toast('کپی خودکار نشد؛ لینک را دستی کپی کنید.');
      }
    });
    showStep('stepDownload');
  } catch (err) {
    toast(err.message || 'ساخت لینک دانلود ناموفق بود.');
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
});

$('#backToLink')?.addEventListener('click', () => showStep('stepLink'));
$('#continueToUpload')?.addEventListener('click', () => showStep('stepUpload'));
$('#backToUpload')?.addEventListener('click', () => showStep('stepUpload'));

$('#continueToSettings')?.addEventListener('click', async () => {
  const btn = $('#continueToSettings');
  if (!isLikelyZipSelected()) {
    toast('اول فایل ZIP دانلودشده را انتخاب یا آپلود کن.');
    return;
  }
  btn.disabled = true;
  const old = btn.innerHTML;
  btn.innerHTML = 'در حال آماده‌سازی…';
  try {
    if (!$('#uploadId').value) await analyzeUploadedZip();
    showStep('stepSettings');
  } catch (err) {
    toast(err.message || 'آپلود یا تحلیل ZIP ناموفق بود.');
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
});

$('#makeZipLink')?.addEventListener('click', async () => {
  const btn = $('#makeZipLink');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = 'در حال آپلود…';
  try {
    await ensureUploaded();
    toast('فایل ZIP آپلود شد.');
  } catch (err) {
    toast(err.message || 'آپلود ناموفق بود.');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

function selectQuality(key) {
  const input = $(`input[name="quality"][value="${key}"]`);
  if (!input) return;
  input.checked = true;
  $$('.quality-option').forEach(x => x.classList.toggle('selected', x.dataset.quality === key));
}

$$('.quality-option').forEach(label => {
  label.addEventListener('click', () => selectQuality(label.dataset.quality));
});

function renderAnalysis(data) {
  lastAnalysis = data;
  const box = $('#analysisBox');
  if (!box) return;
  if (!data.ok) {
    box.innerHTML = `<strong>تحلیل ناموفق:</strong> ${escapeHtml(data.error || 'خطای نامشخص')}`;
    box.classList.remove('hidden');
    return;
  }
  box.innerHTML = `
    <div class="analysis-head">
      <strong>${data.cache_hit ? 'از cache خوانده شد ⚡' : 'تحلیل سریع انجام شد ✅'}</strong>
      <span>${escapeHtml(data.filename || '')}</span>
    </div>
    <div class="stat-grid">
      <div><b>${escapeHtml(data.duration_label)}</b><span>مدت recording</span></div>
      <div><b>${escapeHtml(data.slide_or_document_count)}</b><span>PDF/Slide</span></div>
      <div><b>${data.screen_share_detected ? 'دارد' : 'ندارد'}</b><span>Screen Share</span></div>
      <div><b>${data.chat_detected ? 'دارد' : 'ندارد'}</b><span>Chat</span></div>
      <div><b>${escapeHtml(data.estimated_output_label)}</b><span>حجم تقریبی خروجی</span></div>
      <div><b>${escapeHtml(data.estimated_processing_label)}</b><span>زمان تقریبی</span></div>
    </div>
    <p class="recommendation">Preset پیشنهادی: <button type="button" id="applySuggested" class="chip-btn active">${escapeHtml(data.suggested_preset_label)}</button></p>
  `;
  box.classList.remove('hidden');
  $('#applySuggested')?.addEventListener('click', () => {
    selectQuality(data.suggested_preset);
    toast(`Preset پیشنهادی اعمال شد: ${data.suggested_preset_label}`);
  });
  selectQuality(data.suggested_preset);
}

$('#smartAnalyzeBtn')?.addEventListener('click', async () => {
  const btn = $('#smartAnalyzeBtn');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = 'در حال تحلیل…';
  try {
    await analyzeUploadedZip();
    toast('تحلیل ZIP آماده شد.');
  } catch (err) {
    toast(err.message || 'تحلیل ناموفق بود.');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

const refreshToggleState = () => {
  $$('.toggle-card').forEach(card => {
    const input = card.querySelector('input');
    if (input) card.classList.toggle('checked', input.checked);
  });
};
$$('.toggle-card input').forEach(input => input.addEventListener('change', refreshToggleState));

$('#gpu1050')?.addEventListener('change', () => {
  const select = $('#hardwareAccel');
  if ($('#gpu1050').checked) {
    if (select) select.value = 'nvidia';
    toast('رندر با GPU NVIDIA فعال شد. اگر ffmpeg یا کارت گرافیک پشتیبانی نکند، برنامه خطا را در صفحه پلیر نشان می‌دهد.');
  } else if (select?.value === 'nvidia') {
    select.value = 'auto';
  }
  refreshToggleState();
});

$('#hardwareAccel')?.addEventListener('change', () => {
  const gpu = $('#gpu1050');
  if (gpu) gpu.checked = $('#hardwareAccel').value === 'nvidia';
  refreshToggleState();
});

$('#selectAllElements')?.addEventListener('click', () => {
  $$('.element-grid .toggle-card input').forEach(i => i.checked = true);
  refreshToggleState();
});
$('#clearElements')?.addEventListener('click', () => {
  $$('.element-grid .toggle-card input').forEach(i => i.checked = false);
  refreshToggleState();
});

function formDataForSubmit() {
  const fd = new FormData($('#renderForm'));
  if ($('#uploadId').value && zipFile.files.length) {
    fd.delete('zip_file');
  }
  if ($('#gpu1050')?.checked) fd.set('hardware_accel', 'nvidia');
  if ($('#useSuggested')?.checked) fd.set('use_suggested', '1');
  const startMin = Number($('#previewStart')?.value || 0);
  fd.set('preview_start', String(Math.max(0, startMin * 60)));
  fd.set('preview_duration', String($('#previewDuration')?.value || 60));
  return fd;
}

async function startJob(endpoint, button) {
  const hasZip = $('#uploadId').value || zipFile.files.length;
  if (!hasZip) {
    toast('برای ساخت کلاس، اول فایل ZIP دانلودشده را آپلود کن.');
    showStep('stepUpload');
    return;
  }
  const checked = $$('input[name="elements"]:checked');
  if (!checked.length) {
    toast('حداقل یک المان برای خروجی انتخاب کن.');
    showStep('stepSettings');
    return;
  }
  button.disabled = true;
  const old = button.innerHTML;
  button.innerHTML = 'در حال ساخت job…';
  try {
    if (zipFile.files.length && !$('#uploadId').value) await ensureUploaded();
    const fd = formDataForSubmit();
    const res = await fetch(endpoint, { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'شروع پردازش ناموفق بود.');
    window.location.href = data.watch_url;
  } catch (err) {
    toast(err.message || 'شروع پردازش ناموفق بود.');
    button.disabled = false;
    button.innerHTML = old;
  }
}

$('#renderForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const currentStep = document.body.dataset.step || 'stepLink';
  if (currentStep === 'stepLink') {
    $('#continueToDownload')?.click();
    return;
  }
  if (currentStep !== 'stepSettings') return;
  startJob('/api/start', $('#startBtn'));
});

async function loadHistory() {
  const box = $('#historyList');
  const notice = $('#activeJobNotice');
  if (!box) return;
  try {
    const res = await fetch('/api/jobs', { cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'History failed');

    const active = data.jobs.filter(job => ['queued', 'running', 'paused', 'interrupted'].includes(job.status));
    if (notice) {
      if (active.length) {
        const first = active[0];
        notice.innerHTML = `اکنون ${active.length} کلاس در وضعیت پردازش/ادامه قرار دارد. می‌توانی دانلود کلاس دیگری را در تب جدید شروع کنی، اما بهتر است رندر کلاس بعدی را بعد از کامل شدن رندر فعلی شروع کنی. <a href="/watch/${first.id}">مشاهده کلاس فعلی</a>`;
        notice.classList.remove('hidden');
      } else {
        notice.classList.add('hidden');
      }
    }

    if (!data.jobs.length) {
      box.className = 'history-list empty-state';
      box.textContent = 'هنوز job ذخیره‌شده‌ای نیست.';
      return;
    }
    box.className = 'history-list';
    box.innerHTML = data.jobs.map(job => `
      <article class="history-item">
        <div>
          <strong>${job.job_type === 'preview' ? 'Preview' : 'Class Package'} · ${escapeHtml(job.quality_label || job.quality || '')}</strong>
          <small>${escapeHtml(job.created_at || '')} · ${escapeHtml(job.status)} · ${escapeHtml(job.analysis?.duration_label || '')}</small>
          <span>${escapeHtml(job.stage_label || '')} ${job.cache_used ? '· cache' : ''}</span>
        </div>
        <div class="history-actions">
          <a class="chip-btn" href="/watch/${job.id}">Open</a>
          ${job.package_ready ? `<a class="chip-btn active" href="${job.package_url}">Package</a>` : ''}
          ${['error','interrupted','cancelled','paused'].includes(job.status) ? `<button class="chip-btn" data-resume="${job.id}">Resume</button>` : ''}
          <button class="chip-btn danger" data-delete="${job.id}">Delete</button>
        </div>
      </article>
    `).join('');
    $$('[data-resume]', box).forEach(btn => btn.addEventListener('click', async () => {
      const res = await fetch(`/api/jobs/${btn.dataset.resume}/resume`, { method: 'POST' });
      const data = await res.json();
      if (data.ok) window.location.href = `/watch/${btn.dataset.resume}`; else toast(data.error || 'Resume ناموفق بود.');
    }));
    $$('[data-delete]', box).forEach(btn => btn.addEventListener('click', async () => {
      const res = await fetch(`/api/jobs/${btn.dataset.delete}`, { method: 'DELETE' });
      const data = await res.json();
      if (data.ok) loadHistory(); else toast(data.error || 'Delete ناموفق بود.');
    }));
  } catch (err) {
    box.textContent = 'خواندن history ناموفق بود.';
  }
}

$('#refreshHistory')?.addEventListener('click', loadHistory);
refreshToggleState();
loadHistory();
