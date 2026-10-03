/*
  TokenFish 作品页（play.html）：读 URL 的 # 片段（浏览器不会把它发给服务器），
  显示音频播放器或图片预览。

  参数（#src=...&title=...&dur=... 或 #img=...）：
    src   音频地址，只接受 https: / http:
    img   图片地址，只接受 https: / http:
    title 标题（可选）
    dur   时长，单位秒（可选）

  安全约定：所有来自 URL 的内容只通过 textContent / setAttribute 进入页面，
  从不拼 HTML 字符串，也不执行任何来自 URL 的代码。
  纯原生 JS，无依赖，无外部资源。
*/
(function () {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function readParams() {
    var raw = window.location.hash || "";
    if (raw.charAt(0) === "#") {
      raw = raw.slice(1);
    }
    try {
      return new URLSearchParams(raw);
    } catch (err) {
      return new URLSearchParams("");
    }
  }

  function isHttpUrl(value) {
    if (typeof value !== "string" || value === "" || value.length > 4000) {
      return false;
    }
    try {
      var u = new URL(value);
      return u.protocol === "https:" || u.protocol === "http:";
    } catch (err) {
      return false;
    }
  }

  function formatDuration(value) {
    var n = Number(value);
    if (!value || !isFinite(n) || n <= 0 || n > 86400) {
      return "";
    }
    var total = Math.round(n);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function safeFileName(title, ext) {
    var base = String(title || "").replace(/[\\/:*?"<>|\r\n\t]+/g, " ").trim();
    if (!base) {
      base = "tokenfish";
    }
    return base.slice(0, 60) + "." + ext;
  }

  function show(el) {
    el.removeAttribute("hidden");
  }

  function showMessage(text) {
    var box = byId("play-message");
    box.textContent = text;
    show(box);
  }

  function addDownloadLink(container, url, fileName) {
    var wrap = document.createElement("p");
    wrap.setAttribute("class", "tf-play-links");
    var a = document.createElement("a");
    a.setAttribute("href", url);
    a.setAttribute("download", fileName);
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
    a.textContent = "下载";
    wrap.appendChild(a);
    container.appendChild(wrap);
  }

  function render() {
    var params = readParams();
    var src = params.get("src");
    var img = params.get("img");
    var title = (params.get("title") || "").slice(0, 120);
    var dur = formatDuration(params.get("dur"));
    var media = byId("play-media");

    if (src === null && img === null) {
      showMessage(
        "这里是作品播放页。请回到 AI 对话，点击它给你的「作品链接」打开；直接访问这个页面是看不到内容的。"
      );
      return;
    }

    var isImage = src === null;
    var url = isImage ? img : src;
    if (!isHttpUrl(url)) {
      showMessage("链接无效：只支持 https 开头的作品地址。请回到 AI 对话重新获取作品链接。");
      return;
    }

    byId("play-title").textContent = title || (isImage ? "你的图片" : "你的歌曲");
    document.title = (title || "作品") + " — TokenFish";
    if (dur) {
      var meta = byId("play-meta");
      meta.textContent = "时长 " + dur;
      show(meta);
    }

    if (isImage) {
      var pic = document.createElement("img");
      pic.setAttribute("src", url);
      pic.setAttribute("alt", title || "生成的图片");
      pic.setAttribute("referrerpolicy", "no-referrer");
      media.appendChild(pic);
      addDownloadLink(media, url, safeFileName(title, "png"));
      byId("play-again").textContent = "再做一张";
    } else {
      var audio = document.createElement("audio");
      audio.setAttribute("controls", "");
      audio.setAttribute("preload", "metadata");
      audio.setAttribute("src", url);
      media.appendChild(audio);
      addDownloadLink(media, url, safeFileName(title, "mp3"));
    }
    show(byId("play-expiry"));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
