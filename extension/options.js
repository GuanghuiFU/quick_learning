// 设置页
const ids = ["serverUrl", "ocrInterval", "enableOcr", "enableStt", "priority", "noteMode", "captureSlides"];
const saveBtn = document.getElementById("save");
const msgEl = document.getElementById("msg");

chrome.storage.local.get(ids, (d) => {
  serverUrl.value = d.serverUrl || "http://127.0.0.1:8787";
  ocrInterval.value = d.ocrInterval || 2;
  enableOcr.value = String(d.enableOcr != null ? d.enableOcr : true);
  enableStt.value = String(d.enableStt != null ? d.enableStt : true);
  priority.value = d.priority || "ocr";
  noteMode.value = d.noteMode || "both";
  captureSlides.value = String(d.captureSlides != null ? d.captureSlides : true);
});

saveBtn.addEventListener("click", () => {
  chrome.storage.local.set({
    serverUrl: serverUrl.value.trim(),
    ocrInterval: parseInt(ocrInterval.value, 10) || 2,
    enableOcr: enableOcr.value === "true",
    enableStt: enableStt.value === "true",
    priority: priority.value,
    noteMode: noteMode.value,
    captureSlides: captureSlides.value === "true",
  });
  msgEl.textContent = "已保存（重新开始学习生效）";
  setTimeout(() => (msgEl.textContent = ""), 2000);
});
