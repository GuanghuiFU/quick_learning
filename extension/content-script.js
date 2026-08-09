// 内容脚本：监听 B站视频状态，上报给后台
(() => {
  let video = null;
  let observed = false;

  function findVideo() {
    video = document.querySelector("video");
    return video;
  }

  function reportState() {
    if (!video) return;
    chrome.runtime.sendMessage({
      type: "VIDEO_STATE",
      playing: !video.paused && !video.ended,
      currentTime: video.currentTime,
      duration: video.duration,
    });
  }

  function watchVideo() {
    if (observed || !video) return;
    observed = true;
    video.addEventListener("play", () => {
      reportState();
      chrome.runtime.sendMessage({ type: "VIDEO_PLAYING" });
    });
    video.addEventListener("pause", () => {
      reportState();
      chrome.runtime.sendMessage({ type: "VIDEO_PAUSED" });
    });
    video.addEventListener("ended", () => {
      chrome.runtime.sendMessage({ type: "VIDEO_ENDED" });
    });
    // 兜底：currentTime 接近 duration
    video.addEventListener("timeupdate", () => {
      if (video.duration && video.currentTime >= video.duration - 0.5 && !video.ended) {
        chrome.runtime.sendMessage({ type: "VIDEO_ENDED" });
      }
    });
  }

  // 等待视频元素出现（B站 SPA 动态挂载）
  const observer = new MutationObserver(() => {
    if (findVideo()) watchVideo();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  if (findVideo()) watchVideo();

  // 供 SW 查询视频信息
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "GET_VIDEO_INFO") {
      const v = document.querySelector("video");
      sendResponse({
        hasVideo: !!v,
        title: document.title?.split(/[_-|]/)[0]?.trim() || "未命名视频",
        url: location.href,
        playing: !!v && !v.paused && !v.ended,
      });
    }
  });
})();
