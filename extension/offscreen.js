// 离屏文档：接管标签页音频 -> 回显（让用户仍能听到）-> 分段录制 -> 每段转写
// 分段：千问 ASR 单次限 ~5 分钟音频，播放中每 CHUNK_MS 转写一段（后台，不打断播放）
const SERVER = "http://127.0.0.1:8787";
const CHUNK_MS = 4.5 * 60 * 1000;  // 4.5 分钟一段（留余量）

let stream = null;
let audioEl = null;
let mediaRecorder = null;
let chunks = [];
let startTime = 0;        // 会话开始（音频 0s 基准）
let chunkStartTime = 0;   // 当前分段的开始（相对 startTime）
let chunkFlushTimer = null;
let flushing = false;

function handleMessage(msg) {
  if (msg.type === "START_CAPTURE") startCapture(msg);
  else if (msg.type === "STOP_CAPTURE") stopCapture();
  else if (msg.type === "GET_RECORDING") getFinalRecording();
}

async function startCapture(msg) {
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: msg.streamId },
      },
    });
    // 回显：否则 tabCapture 会静音原视频
    audioEl = document.createElement("audio");
    audioEl.srcObject = stream;
    audioEl.autoplay = true;
    audioEl.play().catch((e) => console.warn("echo playback:", e));

    chunks = [];
    startTime = Date.now();
    chunkStartTime = 0;
    startRecorder();

    // 定时 flush 已录分段（后台转写，不暂停视频）
    chunkFlushTimer = setInterval(() => {
      if (!flushing) flushChunk();
    }, CHUNK_MS);
    console.log("capture started (chunked)");
    chrome.runtime.sendMessage({ type: "CAPTURE_STARTED" });
  } catch (e) {
    console.error("startCapture failed:", e);
  }
}

function startRecorder() {
  mediaRecorder = new MediaRecorder(stream);
  chunks = [];
  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };
  mediaRecorder.start(1000);
}

function stopRecorder() {
  if (mediaRecorder) {
    mediaRecorder.stop();
    mediaRecorder = null;
  }
}

async function blobFromChunks() {
  const blob = new Blob(chunks, { type: "audio/webm" });
  chunks = [];
  return blob;
}

async function transcribeBlob(blob, startOffsetSec) {
  const form = new FormData();
  form.append("file", blob, "chunk.webm");
  form.append("start_offset", String(startOffsetSec || 0));
  const resp = await fetch(`${SERVER}/api/transcribe`, { method: "POST", body: form });
  if (!resp.ok) throw new Error(`transcribe -> ${resp.status}`);
  const data = await resp.json();
  return data.text || "";
}

// 转写当前累积的分段，并把文字+起始时间回传 SW
async function flushChunk() {
  if (!mediaRecorder || chunks.length === 0) return;
  flushing = true;
  stopRecorder();
  const offsetSec = Math.floor(chunkStartTime / 1000);
  const blob = await blobFromChunks();
  // 重新开始下一段
  chunkStartTime = Date.now() - startTime;
  startRecorder();
  try {
    const text = await transcribeBlob(blob, offsetSec);
    if (text.trim()) {
      chrome.runtime.sendMessage({ type: "SPEECH_CHUNK", text, startOffset: offsetSec });
    }
  } catch (e) {
    console.warn("chunk transcribe failed:", e);
  } finally {
    flushing = false;
  }
}

// 停止：转写剩余尾部，标记会话结束
async function getFinalRecording() {
  if (chunkFlushTimer) {
    clearInterval(chunkFlushTimer);
    chunkFlushTimer = null;
  }
  if (!mediaRecorder || chunks.length === 0) {
    chrome.runtime.sendMessage({ type: "RECORDING_READY", speechText: "", segments: [], durationMs: 0 });
    cleanup();
    return;
  }
  stopRecorder();
  const offsetSec = Math.floor(chunkStartTime / 1000);
  const blob = await blobFromChunks();
  const durationMs = Date.now() - startTime;
  let tailText = "";
  try {
    tailText = await transcribeBlob(blob, offsetSec);
  } catch (e) {
    console.warn("tail transcribe failed:", e);
  }
  cleanup();
  chrome.runtime.sendMessage({
    type: "RECORDING_READY",
    speechText: tailText,
    startOffset: offsetSec,
    durationMs,
  });
}

function stopCapture() {
  if (chunkFlushTimer) {
    clearInterval(chunkFlushTimer);
    chunkFlushTimer = null;
  }
  stopRecorder();
  cleanup();
}

function cleanup() {
  if (audioEl) {
    audioEl.pause();
    audioEl.srcObject = null;
    audioEl = null;
  }
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }
  chunks = [];
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg);
  sendResponse({ ok: true });
});
