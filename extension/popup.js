// popup 交互
const toggleBtn = document.getElementById("toggleBtn");
const shotBtn = document.getElementById("shotBtn");
const statusEl = document.getElementById("status");
const titleEl = document.getElementById("title");

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (resp) => {
    if (!resp) return;
    if (resp.active) {
      toggleBtn.textContent = "停止学习";
      toggleBtn.classList.add("active");
      statusEl.textContent = `学习中 · ${resp.subtitleCount} 条字幕 · ${resp.slideCount || 0} 张幻灯片`;
    } else {
      toggleBtn.textContent = "开始学习";
      toggleBtn.classList.remove("active");
      statusEl.textContent = "未在学习";
    }
    titleEl.textContent = resp.title || "";
  });
}

toggleBtn.addEventListener("click", async () => {
  const isActive = toggleBtn.classList.contains("active");
  await chrome.runtime.sendMessage({ type: isActive ? "STOP" : "START" });
  refreshStatus();
});

shotBtn.addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "SCREENSHOT" });
});

// 监听后台推送的状态更新
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "STATUS") {
    statusEl.textContent = `学习中 · ${msg.subtitleCount} 条字幕 · ${msg.slideCount || 0} 张幻灯片`;
  }
  if (msg.type === "SHOT_SAVED") {
    statusEl.textContent = `截图已保存: ${msg.name}`;
  }
});

refreshStatus();
