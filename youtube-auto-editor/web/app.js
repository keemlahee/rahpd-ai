"use strict";

const $ = (id) => document.getElementById(id);
const STORE_KEY = "editor-options";
const VERSION_CHECK_MS = 2500;
const state = {
  options: {}, labels: {}, videos: [], selected: "", logCursor: 0, polling: false,
  edit: null, addedMosaics: [], removedMosaics: new Set(), editDirty: false, uploading: false,
};

const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clock = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;

async function api(path, options = {}) {
  if (options.json !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `요청 실패 (${response.status})`);
  }
  return response.json();
}

const notify = (error) => alert(error.message || error);

/* ---------- 옵션 ---------- */
function loadOptions(defaults) {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY)) || {}; } catch { /* 저장소를 못 쓰면 기본값 */ }
  state.options = { ...defaults, ...saved };
  document.querySelectorAll("[data-opt]").forEach((el) => {
    const value = state.options[el.dataset.opt];
    if (el.type === "checkbox") el.checked = Boolean(value); else el.value = value;
    el.addEventListener("change", () => {
      state.options[el.dataset.opt] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
      try { localStorage.setItem(STORE_KEY, JSON.stringify(state.options)); } catch { /* 저장 실패는 무시 */ }
      loadVideos();
    });
  });
}

/* ---------- 영상 목록 ---------- */
async function loadVideos() {
  const data = await api("/api/videos", { method: "POST", json: { options: state.options } });
  state.videos = data.videos;
  state.labels = data.stages;
  if (!state.videos.some((v) => v.id === state.selected)) state.selected = state.videos[0]?.id || "";
  renderVideos();
  renderStageButtons();
}

function renderVideos() {
  $("videos").innerHTML = state.videos.map((v) => `
    <button class="video ${v.id === state.selected ? "on" : ""}" data-id="${v.id}">
      <strong>${esc(v.name)}</strong><small>${esc(v.folder)}/ · ${v.size_mb} MB</small>
      <span class="chips">${Object.entries(state.labels).map(([key, label]) =>
        `<span class="chip ${v.stages[key]}" title="${{ done: "완료", stale: "옵션이 바뀌어 다시 실행 필요", none: "아직 실행 안 함" }[v.stages[key]]}">${label}</span>`).join("")}</span>
    </button>`).join("") || '<p class="hint">영상을 올려 주세요.</p>';
  document.querySelectorAll(".video").forEach((el) => el.addEventListener("click", () => {
    state.selected = el.dataset.id;
    renderVideos();
    loadResult();
  }));
}

function renderStageButtons() {
  const notes = { transcribe: "Whisper", cut: "무음·추임새", plan: "Claude", images: "일러스트·아이콘", pii: "OCR", render: "모자이크+합성" };
  $("stageButtons").innerHTML = Object.entries(state.labels).map(([key, label]) =>
    `<button class="btn stage" data-stage="${key}">${label}<small>${notes[key]}</small></button>`).join("");
  document.querySelectorAll(".stage").forEach((el) => el.addEventListener("click", () => run(el.dataset.stage)));
  setRunning(state.polling);
}

const selectedVideo = () => state.videos.find((v) => v.id === state.selected);

/* ---------- 업로드 ---------- */
function upload(file) {
  const bar = $("uploadBar");
  const form = new FormData();
  form.append("file", file);
  const request = new XMLHttpRequest();
  request.open("POST", "/api/upload");
  bar.hidden = false;
  state.uploading = true;
  request.upload.onprogress = (event) => { bar.firstElementChild.style.width = `${(event.loaded / event.total) * 100}%`; };
  request.onload = async () => {
    bar.hidden = true;
    state.uploading = false;
    if (request.status !== 200) {
      notify(new Error(JSON.parse(request.responseText).detail || "업로드 실패"));
      return;
    }
    const body = JSON.parse(request.responseText);
    $("dropInfo").textContent = `업로드 완료: ${body.name}`;
    state.selected = body.id;
    await loadVideos();
    loadResult();
  };
  request.onerror = () => { bar.hidden = true; state.uploading = false; notify(new Error("업로드 실패")); };
  request.send(form);
}

/* ---------- 실행 ---------- */
function setRunning(running) {
  document.querySelectorAll("#runAll, .stage, #clearCache").forEach((el) => { el.disabled = running; });
  $("stop").hidden = !running;
}

async function run(stage) {
  if (!state.selected) return notify(new Error("영상을 먼저 선택해 주세요."));
  try {
    await api("/api/run", { method: "POST", json: { video_id: state.selected, stage, options: state.options, force: $("force").checked } });
    $("force").checked = false;
    $("log").textContent = "";
    state.logCursor = 0;
    startPolling();
  } catch (error) { notify(error); }
}

function setJobPill(result, running) {
  const names = { idle: "대기", running: "실행 중", done: "완료", stopped: "중지됨", error: "오류" };
  const pill = $("jobState");
  pill.textContent = running ? names.running : names[result] || "대기";
  pill.className = `pill ${running ? "running" : result}`;
}

async function poll() {
  const data = await api(`/api/job?since=${state.logCursor}`);
  state.logCursor = data.next;
  if (data.logs.length) {
    const log = $("log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent += data.logs.join("\n") + "\n";
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  setJobPill(data.result, data.running);
  return data;
}

async function startPolling() {
  if (state.polling) return;
  state.polling = true;
  setRunning(true);
  for (;;) {
    const data = await poll().catch(() => ({ running: true }));
    if (!data.running) break;
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  state.polling = false;
  setRunning(false);
  await loadVideos();
  loadResult();
}

/* ---------- 결과 ---------- */
async function loadResult() {
  const video = selectedVideo();
  $("resultName").textContent = video ? video.name : "";
  state.addedMosaics = [];
  state.removedMosaics = new Set();
  state.editDirty = false;
  if (!video) return;
  const vid = video.id;
  const stamp = Date.now();
  const result = await api(`/api/videos/${vid}/result`);
  const player = $("player");
  const source = result.final ? `/files/${vid}/final.mp4?t=${stamp}` : result.cut ? `/files/${vid}/cut.mp4?t=${stamp}` : "";
  if (source) player.src = source; else player.removeAttribute("src");
  player.load();
  player.hidden = !source;
  $("videoEmpty").hidden = Boolean(source);
  const links = [];
  if (result.final) links.push(`<a href="/files/${vid}/final.mp4" download="${esc(video.name.replace(/\.[^.]+$/, ""))}_편집본.mp4">편집 영상 다운로드</a>`);
  if (result.srt) links.push(`<a href="/files/${vid}/final.srt" download="${esc(video.name.replace(/\.[^.]+$/, ""))}.srt">자막(SRT)</a>`);
  if (result.report) links.push(`<a href="/files/${vid}/report.md" download="리포트.md">리포트(MD)</a>`);
  links.push(`<a href="/source/${vid}" target="_blank" rel="noopener">원본 보기</a>`);
  $("links").innerHTML = links.join("");
  renderReport(result);
  state.edit = await api(`/api/videos/${vid}/edit`);
  renderEdit();
  renderImages();
  renderScript();
}

function renderReport(result) {
  const s = result.summary;
  if (!s) { $("tab-report").innerHTML = '<p class="empty">최종 렌더 뒤에 리포트가 만들어집니다.</p>'; return; }
  const stat = (value, label) => `<div class="stat"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`;
  const cuts = Object.entries(s.cuts).map(([name, count]) => stat(`${count}곳`, name)).join("");
  $("tab-report").innerHTML = `
    ${s.warnings.length ? `<div class="warn">${s.warnings.map((w) => `<p>${esc(w)}</p>`).join("")}</div>` : ""}
    <div class="stats">
      ${stat(`${s.source_duration}s → ${s.final_duration}s`, `${s.saved}초 단축`)}${cuts}
      ${stat(`${s.illustrations}개`, "설명 일러스트")}${stat(`${s.overlays}개`, "로고·아이콘")}
      ${stat(`${s.mosaics}곳`, "화면 모자이크")}${stat(`${s.muted}곳`, "음성 음소거")}
    </div>
    <div class="md">${esc(result.report_md)}</div>`;
}

const timeInput = (value, field, index, group) =>
  `<input class="time" type="number" step="0.1" value="${value}" data-group="${group}" data-index="${index}" data-field="${field}">`;
const textInput = (value, field, index, group, cls = "") =>
  `<input class="${cls}" value="${esc(value)}" data-group="${group}" data-index="${index}" data-field="${field}">`;
const checkbox = (on, group, index) =>
  `<input type="checkbox" ${on ? "checked" : ""} data-group="${group}" data-index="${index}" data-field="enabled">`;

function renderEdit() {
  const edit = state.edit;
  const box = $("tab-edit");
  if (!edit || !edit.info || !edit.ready) { box.innerHTML = '<p class="empty">전체 실행이 끝나면 수정할 수 있습니다.</p>'; return; }
  const plan = edit.plan;
  const cuts = edit.cuts.cuts.filter((c) => c.type !== "silence");
  const silences = edit.cuts.cuts.length - cuts.length;
  const names = { silence: "무음", filler: "추임새", stutter: "말더듬", sound: "짧은 소리" };

  box.innerHTML = `
    <div class="edit-section">
      <h3>설명 일러스트 (${plan.illustrations.length}) <button class="btn tiny" id="addIllust">+ 추가</button></h3>
      <div class="scroll"><table class="edit-table">
        <tr><th></th><th>시작</th><th>끝</th><th>개념</th><th>라벨</th><th>로고</th><th>이미지 프롬프트</th></tr>
        ${plan.illustrations.map((item, i) => `<tr class="${item.enabled === false ? "off" : ""}">
          <td class="check">${checkbox(item.enabled !== false, "illustrations", i)}</td>
          <td>${timeInput(item.start, "start", i, "illustrations")}</td>
          <td>${timeInput(item.end, "end", i, "illustrations")}</td>
          <td><textarea data-group="illustrations" data-index="${i}" data-field="concept">${esc(item.concept)}</textarea></td>
          <td>${textInput(item.label, "label", i, "illustrations", "label")}</td>
          <td>${textInput(item.logo, "logo", i, "illustrations", "label")}</td>
          <td><textarea data-group="illustrations" data-index="${i}" data-field="image_prompt">${esc(item.image_prompt)}</textarea></td>
        </tr>`).join("") || '<tr><td colspan="7">없음</td></tr>'}
      </table></div>
    </div>
    <div class="edit-section">
      <h3>로고·아이콘 오버레이 (${plan.overlays.length}) <button class="btn tiny" id="addOverlay">+ 추가</button></h3>
      <div class="scroll"><table class="edit-table">
        <tr><th></th><th>시작</th><th>끝</th><th>종류</th><th>이름</th><th>로고 파일</th><th>이미지 프롬프트</th></tr>
        ${plan.overlays.map((item, i) => `<tr class="${item.enabled === false ? "off" : ""}">
          <td class="check">${checkbox(item.enabled !== false, "overlays", i)}</td>
          <td>${timeInput(item.start, "start", i, "overlays")}</td>
          <td>${timeInput(item.end, "end", i, "overlays")}</td>
          <td>${esc(item.kind === "logo" ? "로고" : "아이콘")}</td>
          <td>${textInput(item.name, "name", i, "overlays", "label")}</td>
          <td>${textInput(item.logo, "logo", i, "overlays", "label")}</td>
          <td><textarea data-group="overlays" data-index="${i}" data-field="image_prompt">${esc(item.image_prompt)}</textarea></td>
        </tr>`).join("") || '<tr><td colspan="7">없음</td></tr>'}
      </table></div>
    </div>
    <div class="edit-section">
      <h3>개인정보 모자이크 (${edit.pii.tracks.length}) <button class="btn tiny" id="addMosaic">+ 직접 추가</button>
        <small>끄면 그 부분은 가리지 않습니다</small></h3>
      <div id="mosaicMarker"></div>
      <div class="scroll"><table class="edit-table">
        <tr><th></th><th>시각</th><th>종류</th><th>내용</th><th></th></tr>
        ${edit.pii.tracks.map((t) => `<tr class="${t.enabled ? "" : "off"}">
          <td class="check"><input type="checkbox" ${t.enabled ? "checked" : ""} data-track="${t.id}"></td>
          <td><button class="btn tiny" data-seek="${t.start}">${clock(t.start)}</button> ~ ${clock(t.end)}</td>
          <td>${esc(t.kind)}</td><td>${esc(t.text)}</td>
          <td>${t.manual ? `<button class="btn tiny" data-remove-track="${t.id}">삭제</button>` : ""}</td>
        </tr>`).join("") || '<tr><td colspan="5">탐지된 개인정보가 없습니다.</td></tr>'}
      </table></div>
    </div>
    <div class="edit-section">
      <h3>지운 말 (${cuts.length}) <small>체크를 끄면 되살립니다. 무음 컷 ${silences}곳은 세부 조절에서 바꿉니다.
        컷을 바꾸면 영상 시각이 달라져 개인정보 탐지가 다시 실행됩니다.</small></h3>
      <div class="scroll"><table class="edit-table">
        <tr><th></th><th>원본 시각</th><th>종류</th><th>내용</th><th>이유</th></tr>
        ${cuts.map((c) => `<tr class="${c.enabled ? "" : "off"}">
          <td class="check"><input type="checkbox" ${c.enabled ? "checked" : ""} data-cut="${c.id}"></td>
          <td>${clock(c.start)}</td><td>${names[c.type]}</td><td>${esc(c.text)}</td><td>${esc(c.reason)}</td>
        </tr>`).join("") || '<tr><td colspan="5">없음</td></tr>'}
      </table></div>
    </div>
    <div class="edit-actions">
      <button class="btn primary" id="saveEdit">저장 후 다시 렌더</button>
      <span class="hint">바뀐 프롬프트의 그림만 새로 만들고, 나머지는 재사용합니다.</span>
    </div>`;

  box.querySelectorAll("[data-group]").forEach((el) => el.addEventListener("change", () => {
    const item = edit.plan[el.dataset.group][Number(el.dataset.index)];
    item[el.dataset.field] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    if (el.type === "checkbox") el.closest("tr").classList.toggle("off", !el.checked);
  }));
  box.querySelectorAll("[data-track], [data-cut]").forEach((el) => el.addEventListener("change", () => {
    el.closest("tr").classList.toggle("off", !el.checked);
  }));
  box.querySelectorAll("[data-seek]").forEach((el) => el.addEventListener("click", () => {
    showTab("video");
    $("player").currentTime = Number(el.dataset.seek);
  }));
  box.querySelectorAll("[data-remove-track]").forEach((el) => el.addEventListener("click", () => {
    state.removedMosaics.add(Number(el.dataset.removeTrack));
    el.closest("tr").remove();
  }));
  $("addIllust").addEventListener("click", () => {
    const start = currentCutTime();
    plan.illustrations.push({ start, end: start + 5, concept: "", label: "", logo: "", image_prompt: "", enabled: true });
    state.editDirty = true;
    renderEdit();
  });
  $("addOverlay").addEventListener("click", () => {
    const start = currentCutTime();
    plan.overlays.push({ start, end: start + 2, kind: "icon", name: "", logo: "", image_prompt: "", enabled: true });
    state.editDirty = true;
    renderEdit();
  });
  $("addMosaic").addEventListener("click", openMosaicMarker);
  $("saveEdit").addEventListener("click", saveEdit);
}

const currentCutTime = () => Math.round(($("player").currentTime || 0) * 10) / 10;

async function saveEdit() {
  const vid = state.selected;
  const box = $("tab-edit");
  const edit = state.edit;
  try {
    const plan = edit.plan;
    const planPayload = { illustrations: plan.illustrations, overlays: plan.overlays };
    await api(`/api/videos/${vid}/plan`, { method: "PUT", json: planPayload });

    const enabled = {};
    box.querySelectorAll("[data-track]").forEach((el) => { enabled[el.dataset.track] = el.checked; });
    await api(`/api/videos/${vid}/pii`, {
      method: "PUT",
      json: { enabled, added: state.addedMosaics, removed: [...state.removedMosaics] },
    });

    const cutState = {};
    let cutChanged = false;
    box.querySelectorAll("[data-cut]").forEach((el) => {
      cutState[el.dataset.cut] = el.checked;
      if (edit.cuts.cuts.find((c) => c.id === Number(el.dataset.cut)).enabled !== el.checked) cutChanged = true;
    });
    if (cutChanged) await api(`/api/videos/${vid}/cuts`, { method: "PUT", json: { enabled: cutState } });

    state.editDirty = false;
    await run("render");
  } catch (error) { notify(error); }
}

/* 영상 위에서 드래그해 가릴 영역을 직접 지정한다. */
function openMosaicMarker() {
  const holder = $("mosaicMarker");
  if (holder.childElementCount) { holder.innerHTML = ""; return; }
  const vid = state.selected;
  const { width, height } = state.edit.info;
  holder.innerHTML = `
    <div class="marker"><video id="markVideo" src="/files/${vid}/cut.mp4" preload="auto" muted></video><canvas id="markCanvas" width="${width}" height="${height}"></canvas></div>
    <div class="marker-bar">
      <label>시각 <input id="markSeek" type="range" min="0" max="${state.edit.info.duration}" step="0.1" value="0" style="width:220px"></label>
      <label>시작 <input id="markStart" type="number" step="0.1" value="0"></label>
      <button class="btn tiny" id="markSetStart">현재 시각</button>
      <label>끝 <input id="markEnd" type="number" step="0.1" value="3"></label>
      <button class="btn tiny" id="markSetEnd">현재 시각</button>
      <button class="btn tiny" id="markAdd">이 영역 추가</button>
      <span class="hint" id="markInfo">영상 위에서 드래그해 영역을 고르세요.</span>
    </div>`;
  const video = $("markVideo");
  const canvas = $("markCanvas");
  const context = canvas.getContext("2d");
  let rect = null;
  let anchor = null;
  const toPixel = (event) => {
    const box = canvas.getBoundingClientRect();
    return [Math.round((event.clientX - box.left) * width / box.width), Math.round((event.clientY - box.top) * height / box.height)];
  };
  const draw = () => {
    context.clearRect(0, 0, width, height);
    if (!rect) return;
    context.lineWidth = Math.max(3, width / 300);
    context.strokeStyle = "#f2765a";
    context.fillStyle = "rgba(242,118,90,.3)";
    context.fillRect(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]);
    context.strokeRect(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]);
  };
  canvas.addEventListener("pointerdown", (event) => { anchor = toPixel(event); canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener("pointermove", (event) => {
    if (!anchor) return;
    const [x, y] = toPixel(event);
    rect = [Math.min(anchor[0], x), Math.min(anchor[1], y), Math.max(anchor[0], x), Math.max(anchor[1], y)];
    draw();
  });
  canvas.addEventListener("pointerup", () => { anchor = null; });
  $("markSeek").addEventListener("input", (event) => { video.currentTime = Number(event.target.value); });
  $("markSetStart").addEventListener("click", () => { $("markStart").value = video.currentTime.toFixed(1); });
  $("markSetEnd").addEventListener("click", () => { $("markEnd").value = video.currentTime.toFixed(1); });
  $("markAdd").addEventListener("click", () => {
    const start = Number($("markStart").value);
    const end = Number($("markEnd").value);
    if (!rect || end <= start) { $("markInfo").textContent = "영역을 그리고, 끝 시각을 시작보다 뒤로 정해 주세요."; return; }
    state.addedMosaics.push({ start, end, rect });
    $("markInfo").textContent = `${state.addedMosaics.length}개 추가됨. 저장하면 반영됩니다.`;
    rect = null;
    draw();
  });
}

function renderImages() {
  const box = $("tab-images");
  const vid = state.selected;
  const plan = state.edit?.plan;
  const items = plan ? [
    ...plan.illustrations.map((item) => ({ ...item, title: item.label || item.concept, kind: "일러스트" })),
    ...plan.overlays.map((item) => ({ ...item, title: item.name, kind: item.kind === "logo" ? "로고" : "아이콘" })),
  ].filter((item) => item.image) : [];
  box.innerHTML = items.length ? `<div class="gallery">${items.map((item) => `
    <div class="tile"><div class="pic"><img loading="lazy" src="/files/${vid}/images/${esc(item.image)}" alt="${esc(item.title)}"></div>
      <div class="meta"><span>${item.kind} · ${clock(item.start)}</span>${esc(item.title)}
        <button class="btn tiny" data-regen="${esc(item.image)}">다시 생성</button></div></div>`).join("")}</div>`
    : '<p class="empty">생성된 이미지가 없습니다. Claude 또는 OpenAI 키를 넣고 실행해 주세요.</p>';
  box.querySelectorAll("[data-regen]").forEach((el) => el.addEventListener("click", async () => {
    try {
      await api(`/api/videos/${vid}/regen-image`, { method: "POST", json: { image: el.dataset.regen } });
      run("images");
    } catch (error) { notify(error); }
  }));
}

function renderScript() {
  const box = $("tab-script");
  const segments = state.edit?.transcript.segments || [];
  box.innerHTML = segments.length ? `<div class="script">${segments.map((s) => `
    <div class="line"><button data-seek="${s.start ?? 0}">${s.start === null ? "--:--" : clock(s.start)}</button>
      <span>${s.words.map((w) => (w.removed ? `<del>${esc(w.word)}</del>` : esc(w.word))).join(" ")}</span></div>`).join("")}</div>
    <p class="hint">빨간 취소선은 컷으로 지워진 말입니다.</p>` : '<p class="empty">전사 뒤에 대본이 표시됩니다.</p>';
  box.querySelectorAll("[data-seek]").forEach((el) => el.addEventListener("click", () => {
    showTab("video");
    const player = $("player");
    player.currentTime = Number(el.dataset.seek);
    player.play();
  }));
}

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((el) => el.classList.toggle("on", el.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((el) => { el.hidden = el.id !== `tab-${name}`; });
}

/* ---------- 시작 ---------- */
async function init() {
  const status = await api("/api/status");
  const device = status.device;
  $("device").textContent = `장치: ${device.name}${device.kind === "cpu" ? " (CPU 전사)" : ""}`;
  $("whisperSelect").options[0].textContent = `자동 (${device.default_whisper})`;
  showKeys(status.keys);
  loadOptions(status.defaults);
  await loadVideos();
  await loadResult();
  const job = await api("/api/job?since=0");
  if (job.running) startPolling();
  watchVersion();
  ["input", "change"].forEach((name) => $("tab-edit").addEventListener(name, () => { state.editDirty = true; }));

  const drop = $("drop");
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (event) => { if (event.dataTransfer.files[0]) upload(event.dataTransfer.files[0]); });
  $("file").addEventListener("change", (event) => { if (event.target.files[0]) upload(event.target.files[0]); });
  $("refresh").addEventListener("click", async () => { await loadVideos(); loadResult(); });
  $("clearCache").addEventListener("click", async () => {
    const video = selectedVideo();
    if (!video || !confirm(`'${video.name}'의 작업 결과를 모두 지울까요? 원본 영상은 남습니다.`)) return;
    try {
      await api(`/api/videos/${video.id}/cache`, { method: "DELETE" });
      await loadVideos();
      loadResult();
    } catch (error) { notify(error); }
  });
  $("saveKeys").addEventListener("click", async () => {
    try {
      showKeys(await api("/api/keys", { method: "POST", json: { claude: $("claudeKey").value, openai: $("openaiKey").value } }));
      $("claudeKey").value = "";
      $("openaiKey").value = "";
      loadVideos();
    } catch (error) { notify(error); }
  });
  $("runAll").addEventListener("click", () => run("all"));
  $("stop").addEventListener("click", () => api("/api/stop", { method: "POST", json: {} }).catch(notify));
  document.querySelectorAll("#tabs button").forEach((el) => el.addEventListener("click", () => showTab(el.dataset.tab)));
}

function showKeys(keys) {
  $("claudeState").textContent = keys.claude ? `설정됨 (${keys.claude})` : "미설정";
  $("openaiState").textContent = keys.openai ? `설정됨 (${keys.openai})` : "미설정";
}

/* 코드가 바뀌면 서버가 스스로 다시 켜지므로 화면도 따라서 새로 읽는다.
   쓰던 내용이 날아가면 안 되니, 작업 중이면 끝날 때까지 미룬다. */
const hasWorkInProgress = () => state.editDirty || state.uploading || state.addedMosaics.length > 0
  || !$("player").paused || $("mosaicMarker")?.childElementCount > 0
  || $("claudeKey").value !== "" || $("openaiKey").value !== "";

async function watchVersion() {
  let current = "";
  setInterval(async () => {
    const data = await api("/api/version").catch(() => null);
    if (!data) return;  // 서버가 다시 켜지는 중
    current ||= data.version;
    if (data.version !== current && !hasWorkInProgress()) location.reload();
  }, VERSION_CHECK_MS);
}

init().catch(notify);
