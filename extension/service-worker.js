// 视频学习助手 —— 扩展后台编排
// 职责：状态机、命令路由、offscreen 调度、帧检测(字幕OCR+幻灯片检测)、分段转写聚合、总结
let SERVER = "http://127.0.0.1:8787";
let OCR_INTERVAL = 2000;

// 用户配置（options 页设置）
const config = {
  enableOcr: true,        // 字幕 OCR 通道
  enableStt: true,        // 语音转写通道
  priority: "ocr",        // ocr | stt | both
  noteMode: "both",       // note | speedread | both
  captureSlides: true,    // 自动截取幻灯片（帧差检测）
};

function loadConfig() {
  chrome.storage.local.get(
    ["serverUrl", "ocrInterval", "enableOcr", "enableStt", "priority", "noteMode", "captureSlides"],
    (d) => {
      if (d.serverUrl) SERVER = d.serverUrl;
      if (d.ocrInterval) OCR_INTERVAL = d.ocrInterval * 1000;
      if (d.enableOcr != null) config.enableOcr = d.enableOcr;
      if (d.enableStt != null) config.enableStt = d.enableStt;
      if (d.priority) config.priority = d.priority;
      if (d.noteMode) config.noteMode = d.noteMode;
      if (d.captureSlides != null) config.captureSlides = d.captureSlides;
    }
  );
}
loadConfig();

const state = {
  active: false,
  tabId: null,
  videoTitle: "",
  videoUrl: "",
  subtitles: [],       // [{time, text}] 字幕 OCR（去重，带时间戳）
  speechSegs: [],      // [{start, text}] 分段转写（带起始时间戳）
  slides: [],          // [{time, filename, ocr_text}] 自动截取的幻灯片
  frameTimer: null,
  lastSubtitleText: "",
};

// ---------- 工具 ----------
async function sendToServer(path, opts = {}) {
  const resp = await fetch(`${SERVER}${path}`, opts);
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

async function ensureOffscreen() {
  const has = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (has.length > 0) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "捕获标签页音频用于学习记录",
  });
}

async function queryVideoInfo(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: () => {
      const video = document.querySelector("video");
      const title = document.title?.split(/[_-|]/)[0]?.trim() || "未命名视频";
      return {
        hasVideo: !!video,
        title,
        url: location.href,
        playing: !!video && !video.paused && !video.ended,
      };
    },
  });
  const hit = results.find((r) => r.result && r.result.hasVideo)?.result;
  return hit || results[0]?.result || { hasVideo: false, title: "", url: "" };
}

async function getVideoTime(tabId) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const v = document.querySelector("video");
        return v && !v.paused ? v.currentTime : null;
      },
    });
    const hit = results.find((r) => r.result != null);
    return hit ? hit.result : null;
  } catch {
    return null;
  }
}

function fmtTime(sec) {
  if (sec == null) return "";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// ---------- 帧检测循环：字幕 OCR + 评分式关键画面截取 ----------
async function getTranscriptNearTime(t) {
  // 找到当前视频时间附近的转写分句（用于讲解重要度信号 Stranscript）
  if (!state.speechSegs.length) return "";
  // speechSegs: [{start, text}]；start 是分句起始秒
  let best = "";
  for (const seg of state.speechSegs) {
    if (seg.start <= t && t - seg.start < 30) {
      best = seg.text;
      break;
    }
  }
  // 取当前时间前 60s 的文本做讲解上下文
  const recent = state.speechSegs
    .filter((s) => s.start <= t && t - s.start < 60)
    .map((s) => s.text)
    .join(" ");
  return recent || best;
}

async function processFrame() {
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab({ format: "png" });
    const resp = await fetch(dataUrl);
    const blob = await resp.blob();
    const t = await getVideoTime(state.tabId);
    const transcript = await getTranscriptNearTime(t);
    const form = new FormData();
    form.append("file", blob, "frame.png");
    form.append("transcript", transcript);
    const data = await sendToServer("/api/frame", { method: "POST", body: form });

    // 1) 字幕 OCR（若启用）：/api/frame 已返回字幕文本
    if (config.enableOcr && data.subtitle) {
      const text = (data.subtitle || "").trim();
      if (text && text !== state.lastSubtitleText) {
        state.lastSubtitleText = text;
        state.subtitles.push({ time: fmtTime(t), text });
      }
    }

    // 2) 关键画面截取：评分达到阈值才保存
    if (config.captureSlides && data.capture) {
      state.slides.push({
        time: fmtTime(t),
        filename: data.name,
        ocr_text: data.ocr_text || "",
      });
    }

    chrome.runtime.sendMessage({ type: "STATUS", subtitleCount: state.subtitles.length, slideCount: state.slides.length });
  } catch (e) {
    console.warn("frame process failed:", e);
  }
}

function startFrameLoop() {
  if (state.frameTimer) return;
  state.frameTimer = setInterval(processFrame, OCR_INTERVAL);
}

function stopFrameLoop() {
  if (state.frameTimer) {
    clearInterval(state.frameTimer);
    state.frameTimer = null;
  }
}

// ---------- 开始 / 停止学习 ----------
async function startCapture(tabId) {
  const info = await queryVideoInfo(tabId);
  if (!info.hasVideo) {
    console.warn("当前标签页没有视频");
    return;
  }
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
  await ensureOffscreen();
  await chrome.runtime.sendMessage({
    type: "START_CAPTURE",
    streamId,
    tabId,
    title: info.title,
    url: info.url,
    enableStt: config.enableStt,
  });
  state.active = true;
  state.tabId = tabId;
  state.videoTitle = info.title;
  state.videoUrl = info.url;
  state.subtitles = [];
  state.speechSegs = [];
  state.slides = [];
  state.lastSubtitleText = "";
  startFrameLoop();
  chrome.action.setBadgeText({ text: "学" });
}

async function stopCapture() {
  stopFrameLoop();
  try {
    await chrome.runtime.sendMessage({ type: "STOP_CAPTURE" });
  } catch (e) {
    console.warn("stop offscreen:", e);
  }
  state.active = false;
  chrome.action.setBadgeText({ text: "" });
}

// ---------- 手动截图 ----------
async function captureScreenshot(tabId) {
  const dataUrl = await chrome.tabs.captureVisibleTab({ format: "png" });
  const resp = await fetch(dataUrl);
  const blob = await resp.blob();
  const name = `shot_${Date.now()}.png`;
  const form = new FormData();
  form.append("file", blob, name);
  const data = await sendToServer("/api/screenshot", { method: "POST", body: form });
  const t = await getVideoTime(tabId);
  state.slides.push({ time: fmtTime(t), filename: data.name, ocr_text: "", manual: true });
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/128.png",
    title: "截图已保存",
    message: `已保存到 ${data.name}`,
  });
  chrome.runtime.sendMessage({ type: "SHOT_SAVED", name: data.name });
}

// ---------- 视频结束处理 ----------
async function onVideoEnded() {
  if (!state.active) return;
  console.log("视频结束，开始处理...");
  stopFrameLoop();
  try {
    await chrome.runtime.sendMessage({ type: "GET_RECORDING" });
  } catch (e) {
    console.warn("获取录音失败:", e);
  }
}

// ---------- 事件 ----------
chrome.commands.onCommand.addListener(async (command) => {
  if (command === "toggle-capture") {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (state.active) await stopCapture();
    else await startCapture(tab.id);
  } else if (command === "capture-screenshot") {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    await captureScreenshot(tab.id);
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "START") {
    const [tab] = chrome.tabs.query({ active: true, currentWindow: true });
    tab.then(([t]) => startCapture(t.id)).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "STOP") {
    stopCapture().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "SCREENSHOT") {
    const [tab] = chrome.tabs.query({ active: true, currentWindow: true });
    tab.then(([t]) => captureScreenshot(t.id)).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "VIDEO_ENDED") {
    if (sender.tab?.id === state.tabId) onVideoEnded();
    sendResponse({ ok: true });
  }
  if (msg.type === "SPEECH_CHUNK") {
    // 分段转写完成，累积（带起始时间戳）
    if (msg.text && msg.text.trim()) {
      state.speechSegs.push({ start: msg.startOffset, text: msg.text.trim() });
      console.log(`speech chunk @${msg.startOffset}s: ${msg.text.slice(0, 40)}...`);
    }
  }
  if (msg.type === "RECORDING_READY") {
    // offscreen 尾部转写完成，触发总结
    if (msg.speechText && msg.speechText.trim()) {
      state.speechSegs.push({ start: msg.startOffset || 0, text: msg.speechText.trim() });
    }
    finalizeNote(msg.durationMs);
  }
  if (msg.type === "GET_STATUS") {
    sendResponse({
      active: state.active,
      subtitleCount: state.subtitles.length,
      slideCount: state.slides.length,
      title: state.videoTitle,
    });
  }
});

// ---------- 结束总结 ----------
async function finalizeNote(durationMs) {
  // 字幕文本（带时间戳）
  const subtitleText = state.subtitles
    .map((s) => (s.time ? `[${s.time}] ${s.text}` : s.text))
    .join("\n");

  // 语音转写：按 start 排序拼接，带时间戳
  const sorted = [...state.speechSegs].sort((a, b) => a.start - b.start);
  const speechText = sorted
    .map((s) => `[${fmtTime(s.start)}] ${s.text}`)
    .join("\n");

  const slides = state.slides.map((s) => ({
    time: s.time,
    filename: s.filename,
    ocr_text: s.ocr_text,
  }));

  const makeNote = (mode) =>
    sendToServer("/api/notes/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: state.videoTitle,
        source_url: state.videoUrl,
        subtitle_text: subtitleText,
        speech_text: speechText,
        mode,
        priority: config.priority,
        slides,
      }),
    });

  try {
    let paths = [];
    if (config.noteMode === "speedread") {
      paths.push((await makeNote("speedread")).path);
    } else if (config.noteMode === "note") {
      paths.push((await makeNote("note")).path);
    } else {
      // both：先速览后笔记
      paths.push((await makeNote("speedread")).path);
      paths.push((await makeNote("note")).path);
    }
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/128.png",
      title: "学习文档已生成",
      message: paths.join("\n"),
    });
  } catch (e) {
    console.error("finalize failed:", e);
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/128.png",
      title: "生成失败",
      message: String(e),
    });
  } finally {
    state.active = false;
    chrome.action.setBadgeText({ text: "" });
  }
}
